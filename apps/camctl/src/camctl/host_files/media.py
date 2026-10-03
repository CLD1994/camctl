"""受管媒体工具执行与结构化事实。

ffprobe 只解析业务需要的时长字段并保持全精度；ffmpeg 成品经实际
退出、存在与非空校验、摘要及同步分别确认，工具失败但文件存在不
算完成。媒体子进程统一经 O3 受管执行，实际收场后才释放文件；本
模块不决定拍摄成功，也不改变动作终态或源文件。
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation

from camctl.contracts.json_values import parse_exact_json
from camctl.host_files.io import (
    DirectorySyncStage,
    HashResult,
    SyncResult,
    hash_target,
    sync_target,
)
from camctl.host_files.models import BoundDirectories, FileObservation, FileObservationKind, FileRef
from camctl.host_files.paths import observe_file, resolve_file
from camctl.host_files.tasks import AsyncFileControl, AsyncFileTask, FileTaskExecutor, FileTaskId, FileTaskResult
from camctl.session.supervision import ResponsibilityOwner
from camctl.operations.process import (
    RawToolOutcome,
    StopSignal,
    ToolSpec,
    ToolStartError,
    execute_tool,
)

__all__ = [
    "MediaArtifact",
    "MediaProbe",
    "ProbeRequest",
    "RepairRequest",
    "probe_media",
    "repair_media",
]

#: 媒体工具终止宽限；正常调用无期限，仅由取消终止。
_MEDIA_TERMINATE_GRACE_S = Decimal("5")


@dataclass(frozen=True)
class ProbeRequest:
    """一次 ffprobe 请求；工具路径与附加参数由任务保存的决定提供。"""

    ffprobe: str = "ffprobe"
    extra_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class MediaProbe:
    """ffprobe 取得的必要媒体事实；时长保持全精度。"""

    duration_s: Decimal | None
    error: str | None


@dataclass(frozen=True)
class RepairRequest:
    """一次 ffmpeg 修复请求；输出参数由任务保存的决定提供。"""

    ffmpeg: str = "ffmpeg"
    output_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class MediaArtifact:
    """ffmpeg 实际成品及其大小、摘要与同步证据。

    complete 表示工具正常完成、普通非空文件和无错误的完整摘要
    长度相符，不表示拍摄成功或同步成功。各阶段结果独立保留；
    摘要关闭失败时仍可保留已算出的摘要，但完整性资格不成立。
    """

    tool_error: str | None
    observation: FileObservation
    checksum: HashResult | None = None
    synchronization: SyncResult | None = None
    postprocessing_stopped: bool = False

    @property
    def exists(self) -> bool | None:
        if self.observation.kind is FileObservationKind.ERROR:
            return None
        return self.observation.kind is not FileObservationKind.MISSING

    @property
    def size_bytes(self) -> int | None:
        return self.observation.size_bytes

    @property
    def digest(self) -> str | None:
        return self.checksum.digest if self.checksum is not None else None

    @property
    def file_synced(self) -> bool:
        return self.synchronization is not None and self.synchronization.file_synced

    @property
    def directory(self) -> DirectorySyncStage:
        return (self.synchronization.directory if self.synchronization is not None
                else DirectorySyncStage.NOT_ATTEMPTED)

    @property
    def complete(self) -> bool:
        return (
            self.tool_error is None and self._file_error is None
            and self.checksum is not None and self._checksum_error is None
        )

    @property
    def _file_error(self) -> str | None:
        kind = self.observation.kind
        if kind is FileObservationKind.ERROR:
            return f"inspect_failed: {self.observation.error}"
        if kind is FileObservationKind.MISSING:
            return "output_missing: 成品不存在"
        if kind is FileObservationKind.TYPE_MISMATCH:
            return "output_type_mismatch: 成品不是普通文件"
        if self.size_bytes is None:
            return "output_size_unknown: 未取得成品大小"
        if self.size_bytes == 0:
            return "output_empty: 成品大小为零"
        return None

    @property
    def _checksum_error(self) -> str | None:
        checksum = self.checksum
        if checksum is None:
            return None
        errors = []
        if checksum.error is not None:
            errors.append(f"hash_failed: {checksum.error}")
        if checksum.digest is None or checksum.size_bytes is None:
            if checksum.error is None:
                errors.append("hash_incomplete: 缺少完整摘要或读取长度")
        elif checksum.size_bytes != self.size_bytes:
            errors.append(f"size_mismatch: observed={self.size_bytes}; hashed={checksum.size_bytes}")
        return "; ".join(errors) or None

    @property
    def error(self) -> str | None:
        errors = (
            self.tool_error, self._file_error, self._checksum_error,
            self.synchronization.error if self.synchronization is not None else None,
            "postprocessing_stopped: 已停止后续文件检查" if self.postprocessing_stopped else None,
        )
        return "; ".join(error for error in errors if error is not None) or None


# 窄注入点：仅测试替换。
async def _execute_tool(
    spec: ToolSpec, *, stop: StopSignal, spawner=None
) -> RawToolOutcome:
    return await execute_tool(spec, stop=stop)


def _file_digest(ref: FileRef, roots: BoundDirectories) -> HashResult:
    return hash_target(ref, roots)


def _file_sync(ref: FileRef, roots: BoundDirectories) -> SyncResult:
    return sync_target(ref, roots)


async def probe_media(
    input: FileRef,
    roots: BoundDirectories,
    request: ProbeRequest,
    *,
    executor: FileTaskExecutor,
    task_id: FileTaskId,
    owner: ResponsibilityOwner,
) -> FileTaskResult:
    """在共享文件责任中执行检查，返回任务结果及 MediaProbe。

    停止通过 executor.request_stop(task_id) 请求；等待取消后的实际
    结果交给 owner。领取结果不表示业务保存已经完成。
    """
    resolve_file(input, roots)
    return await executor.run_async_file_task(
        AsyncFileTask(task_id, (input.file_id,), "media_probe", "recording",
                      lambda control: _probe(input, roots, request, control)), owner,
    )


async def _probe(
    input: FileRef, roots: BoundDirectories, request: ProbeRequest, control: AsyncFileControl,
) -> MediaProbe:
    """经 ffprobe 取得媒体时长等必要事实。

    工具不可用、退出失败、输出结构非法与字段缺失分别分类；时长
    以 Decimal 保留全部精度，不舍入到任何目标粒度。
    """
    host = resolve_file(input, roots)
    argv = (
        (request.ffprobe, "-v", "error")
        + request.extra_args
        + ("-show_entries", "format=duration", "-of", "json", str(host.path))
    )
    outcome = await _run_tool(argv, control)
    if isinstance(outcome, str):
        return MediaProbe(duration_s=None, error=outcome)
    if outcome.error is not None or outcome.exit is None or outcome.exit.exit_code != 0:
        return MediaProbe(duration_s=None, error=_tool_failure("tool_failed", outcome))
    try:
        payload = parse_exact_json((outcome.output or b"").decode("utf-8"))
    except (ValueError, InvalidOperation):
        return MediaProbe(duration_s=None, error="invalid_structure: 输出不是预期结构")
    if not isinstance(payload, dict) or not isinstance(payload.get("format"), dict):
        return MediaProbe(duration_s=None, error="invalid_structure: 输出不是预期结构")
    if "duration" not in payload["format"]:
        return MediaProbe(duration_s=None, error="missing_duration: 缺少时长字段")
    value = payload["format"]["duration"]
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return MediaProbe(duration_s=None, error="invalid_structure: 时长不是数字")
    try:
        duration = Decimal(value)
    except (InvalidOperation, ValueError):
        return MediaProbe(duration_s=None, error="invalid_structure: 时长不是数字")
    if not duration.is_finite() or duration < 0:
        return MediaProbe(duration_s=None, error="invalid_structure: 时长必须是有限非负数")
    return MediaProbe(duration_s=duration, error=None)


async def repair_media(
    input: FileRef,
    output: FileRef,
    roots: BoundDirectories,
    request: RepairRequest,
    *,
    executor: FileTaskExecutor,
    task_id: FileTaskId,
    owner: ResponsibilityOwner,
) -> FileTaskResult:
    """修复全程占用输入和输出，返回任务结果及 MediaArtifact。"""
    resolve_file(input, roots)
    resolve_file(output, roots)
    return await executor.run_async_file_task(
        AsyncFileTask(task_id, (input.file_id, output.file_id), "media_repair", "recording",
                      lambda control: _repair(input, output, roots, request, control)), owner,
    )


async def _repair(
    input: FileRef, output: FileRef, roots: BoundDirectories,
    request: RepairRequest, control: AsyncFileControl,
) -> MediaArtifact:
    """经 ffmpeg 生成修复成品并分别确认退出、校验、同步与摘要。

    工具失败但输出文件存在时 exists 保留实际观察，complete 为
    False：部分成品不作为正式产物。
    """
    input_host = resolve_file(input, roots)
    output_host = resolve_file(output, roots)
    argv = (
        (request.ffmpeg, "-nostdin", "-y", "-i", str(input_host.path))
        + request.output_args
        + (str(output_host.path),)
    )
    outcome = await _run_tool(argv, control)
    # 外层执行任务由文件执行器保留；等待者取消不会取消该线程等待。
    return await asyncio.to_thread(_collect_artifact, outcome, output, roots, control.stop_event)


def _collect_artifact(
    outcome: RawToolOutcome | str, output: FileRef, roots: BoundDirectories,
    stop: threading.Event,
) -> MediaArtifact:
    observation = observe_file(output, roots)
    if isinstance(outcome, str):
        return MediaArtifact(tool_error=outcome, observation=observation)
    if outcome.error is not None or outcome.exit is None or outcome.exit.exit_code != 0:
        return MediaArtifact(
            tool_error=_tool_failure("tool_failed", outcome), observation=observation,
        )
    artifact = MediaArtifact(tool_error=None, observation=observation)
    if artifact._file_error is not None:
        return artifact
    if stop.is_set():
        return replace(artifact, postprocessing_stopped=True)
    digest = _file_digest(output, roots)
    artifact = replace(artifact, checksum=digest)
    if stop.is_set():
        return replace(artifact, postprocessing_stopped=True)
    synced = _file_sync(output, roots)
    return replace(artifact, synchronization=synced)


async def _run_tool(
    argv: tuple[str, ...], control: AsyncFileControl
) -> RawToolOutcome | str:
    if control.stop_requested:
        return "tool_cancelled: 工具尚未启动，停止请求已生效"
    spec = ToolSpec(
        argv=argv, timeout_s=None, terminate_grace_s=_MEDIA_TERMINATE_GRACE_S
    )
    try:
        return await _execute_tool(spec, stop=control)
    except ToolStartError as error:
        return f"tool_unavailable: {error}"


def _tool_failure(kind: str, outcome: RawToolOutcome) -> str:
    exit_value = outcome.exit
    detail = (
        f"exit={exit_value.exit_code}"
        if exit_value is not None and exit_value.exit_code is not None
        else f"signal={exit_value.signal}" if exit_value is not None else "no-exit"
    )
    if outcome.error is not None:
        detail = f"{outcome.error}; {detail}"
    for failure in outcome.signal_failures:
        detail += f"; {failure.stage.value}: {failure.message}"
    if outcome.output_failure is not None:
        detail += f"; output: {outcome.output_failure}"
    return f"{kind}: {detail}"

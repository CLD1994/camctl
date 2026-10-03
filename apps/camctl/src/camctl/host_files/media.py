"""受管媒体工具执行与结构化事实。

ffprobe 只解析业务需要的时长字段并保持全精度；ffmpeg 成品经实际
退出、存在与非空校验、摘要及同步分别确认，工具失败但文件存在不
算完成。媒体子进程统一经 O3 受管执行，实际收场后才释放文件；本
模块不决定拍摄成功，也不改变动作终态或源文件。
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from camctl.contracts.json_values import parse_exact_json
from camctl.host_files.io import (
    DirectorySyncStage,
    HashResult,
    SyncResult,
    hash_target,
    sync_target,
)
from camctl.host_files.models import BoundDirectories, FileRef
from camctl.host_files.paths import resolve_file
from camctl.operations.process import (
    RawToolOutcome,
    StopSignal,
    ToolSpec,
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

    complete 只表示工具正常退出且必要成品校验通过，不表示拍摄成
    功；同步与摘要是独立证据字段，分别表达。
    """

    exists: bool
    complete: bool
    size_bytes: int | None
    digest: str | None
    file_synced: bool
    directory: DirectorySyncStage
    error: str | None


class _NeverStop:
    async def requested(self) -> None:
        await asyncio.Future()


# 窄注入点：仅测试替换。
async def _execute_tool(
    spec: ToolSpec, *, stop: StopSignal, spawner=None
) -> RawToolOutcome:
    return await execute_tool(spec, stop=stop)


def _file_exists(path: Path) -> bool:
    return os.path.exists(path)


def _file_size(path: Path) -> int:
    return os.path.getsize(path)


def _file_digest(ref: FileRef, roots: BoundDirectories) -> HashResult:
    return hash_target(ref, roots)


def _file_sync(ref: FileRef, roots: BoundDirectories) -> SyncResult:
    return sync_target(ref, roots)


async def probe_media(
    input: FileRef,
    roots: BoundDirectories,
    request: ProbeRequest,
    *,
    stop: StopSignal | None = None,
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
    outcome = await _run_tool(argv, stop)
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
    stop: StopSignal | None = None,
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
    outcome = await _run_tool(argv, stop)
    exists = _file_exists(output_host.path)
    if isinstance(outcome, str):
        return _incomplete(
            exists=exists, error=outcome, directory=DirectorySyncStage.NOT_ATTEMPTED
        )
    if outcome.error is not None or outcome.exit is None or outcome.exit.exit_code != 0:
        return _incomplete(
            exists=exists,
            error=_tool_failure("tool_failed", outcome),
            directory=DirectorySyncStage.NOT_ATTEMPTED,
        )
    if not exists:
        return _incomplete(
            exists=False,
            error="output_missing: 工具正常退出但成品不存在",
            directory=DirectorySyncStage.NOT_ATTEMPTED,
        )
    size = _file_size(output_host.path)
    if size == 0:
        return _incomplete(
            exists=True,
            error="output_empty: 成品大小为零",
            directory=DirectorySyncStage.NOT_ATTEMPTED,
        )
    digest = _file_digest(output, roots)
    synced = _file_sync(output, roots)
    if digest.error is not None:
        return _incomplete(
            exists=True,
            error=f"hash_failed: {digest.error}",
            directory=synced.directory,
            size_bytes=size,
            file_synced=synced.file_synced,
        )
    return MediaArtifact(
        exists=True,
        complete=True,
        size_bytes=size,
        digest=digest.digest,
        file_synced=synced.file_synced,
        directory=synced.directory,
        error=None,
    )


async def _run_tool(
    argv: tuple[str, ...], stop: StopSignal | None
) -> RawToolOutcome | str:
    spec = ToolSpec(
        argv=argv, timeout_s=None, terminate_grace_s=_MEDIA_TERMINATE_GRACE_S
    )
    try:
        return await _execute_tool(spec, stop=stop or _NeverStop())
    except FileNotFoundError:
        return "tool_unavailable: 工具不存在或不可执行"
    except PermissionError:
        return "tool_unavailable: 工具不可执行"


def _tool_failure(kind: str, outcome: RawToolOutcome) -> str:
    exit_value = outcome.exit
    detail = (
        f"exit={exit_value.exit_code}"
        if exit_value is not None and exit_value.exit_code is not None
        else f"signal={exit_value.signal}" if exit_value is not None else "no-exit"
    )
    if outcome.error is not None:
        detail = f"{outcome.error}; {detail}"
    return f"{kind}: {detail}"


def _incomplete(
    *,
    exists: bool,
    error: str,
    directory: DirectorySyncStage,
    size_bytes: int | None = None,
    file_synced: bool = False,
) -> MediaArtifact:
    return MediaArtifact(
        exists=exists,
        complete=False,
        size_bytes=size_bytes,
        digest=None,
        file_synced=file_synced,
        directory=directory,
        error=error,
    )

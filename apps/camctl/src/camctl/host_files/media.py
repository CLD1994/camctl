"""受管媒体工具执行与结构化事实。

ffprobe 读取容器时长及完整逐流事实，并保持时长全精度；ffmpeg
成品经实际退出、存在与非空校验、摘要及同步分别确认，工具失败但
文件存在不算完成。媒体子进程统一经 O3 受管执行，实际收场后才
释放文件；本模块不决定拍摄成功，也不改变动作终态或源文件。
"""

from __future__ import annotations

import asyncio
import re
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
    "MediaStream",
    "ProbeRequest",
    "RepairRequest",
    "probe_media",
    "repair_media",
    "select_main_video_stream",
]

#: 媒体工具终止宽限；正常调用无期限，仅由取消终止。
_MEDIA_TERMINATE_GRACE_S = Decimal("5")
_SAFE_CONTAINER_FORMAT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_FRAME_RATE = re.compile(r"([0-9]+)/([0-9]+)\Z")


@dataclass(frozen=True)
class ProbeRequest:
    """一次 ffprobe 请求；工具路径与附加参数由任务保存的决定提供。"""

    ffprobe: str = "ffprobe"
    extra_args: tuple[str, ...] = ()
    count_frames: bool = False

    def __post_init__(self) -> None:
        if type(self.count_frames) is not bool:
            raise ValueError("count_frames 必须为布尔值")


@dataclass(frozen=True)
class MediaStream:
    """ffprobe 取得的单条媒体流事实；未提供的新字段保持未知。"""

    index: int | None
    codec_type: str
    codec_name: str | None
    width: int | None
    height: int | None
    duration_s: Decimal | None
    attached_pic: bool | None
    frame_count: int | None = None
    fps: Decimal | None = None


@dataclass(frozen=True)
class MediaProbe:
    """ffprobe 取得的容器与逐流事实；时长保持全精度。"""

    duration_s: Decimal | None
    error: str | None
    streams: tuple[MediaStream, ...] = ()
    format_name: str | None = None


@dataclass(frozen=True)
class RepairRequest:
    """一次 ffmpeg 修复请求；输出参数由任务保存的决定提供。"""

    ffmpeg: str = "ffmpeg"
    output_args: tuple[str, ...] = ()
    stream_indices: tuple[int, ...] | None = None
    output_format: str | None = None


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


def _media_duration(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("时长不是数字")
    try:
        duration = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError("时长不是数字") from None
    if not duration.is_finite() or duration < 0:
        raise ValueError("时长必须是有限非负数")
    return duration


def _stream_integer(stream: dict, name: str, *, minimum: int) -> int | None:
    if name not in stream:
        return None
    value = stream[name]
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"流字段 {name} 不是合法整数")
    return value


def _stream_string(stream: dict, name: str) -> str | None:
    if name not in stream:
        return None
    value = stream[name]
    if not isinstance(value, str) or not value:
        raise ValueError(f"流字段 {name} 不是非空字符串")
    return value


def _stream_frame_count(stream: dict) -> int | None:
    """解析 ffprobe 的实际读取帧数；不从时长和帧率估算。"""
    if "nb_read_frames" not in stream:
        return None
    value = stream["nb_read_frames"]
    if value == "N/A":
        return None
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return int(value)
    raise ValueError("流字段 nb_read_frames 不是合法非负整数")


def _stream_fps(stream: dict) -> Decimal | None:
    """把 ffprobe 的 avg_frame_rate 有理数转为 Decimal 帧率事实。"""
    if "avg_frame_rate" not in stream:
        return None
    value = stream["avg_frame_rate"]
    if value == "N/A":
        return None
    if not isinstance(value, str):
        raise ValueError("流字段 avg_frame_rate 不是字符串")
    match = _FRAME_RATE.fullmatch(value)
    if match is None:
        raise ValueError("流字段 avg_frame_rate 不是分子/分母")
    numerator, denominator = (int(part) for part in match.groups())
    if denominator == 0:
        if numerator == 0:
            return None
        raise ValueError("流字段 avg_frame_rate 分母为零")
    if numerator == 0:
        return None
    return Decimal(numerator) / Decimal(denominator)


def _parse_streams(value: object) -> tuple[MediaStream, ...]:
    if not isinstance(value, list):
        raise ValueError("输出缺少流信息段")
    parsed: list[MediaStream] = []
    seen_indices: set[int] = set()
    for stream in value:
        if not isinstance(stream, dict):
            raise ValueError("流信息不是对象")
        codec_type = stream.get("codec_type")
        if not isinstance(codec_type, str) or not codec_type:
            raise ValueError("流类别不是非空字符串")
        index = _stream_integer(stream, "index", minimum=0)
        if index is not None:
            if index in seen_indices:
                raise ValueError("流索引重复")
            seen_indices.add(index)
        codec_name = _stream_string(stream, "codec_name")
        width = _stream_integer(stream, "width", minimum=1)
        height = _stream_integer(stream, "height", minimum=1)
        duration = (
            _media_duration(stream["duration"])
            if "duration" in stream else None
        )
        frame_count = _stream_frame_count(stream)
        fps = _stream_fps(stream)

        disposition = stream.get("disposition")
        attached_pic = None
        if "disposition" in stream:
            if not isinstance(disposition, dict):
                raise ValueError("流 disposition 不是对象")
            if "attached_pic" in disposition:
                attached_value = disposition["attached_pic"]
                if (
                    isinstance(attached_value, bool)
                    or not isinstance(attached_value, int)
                    or attached_value not in (0, 1)
                ):
                    raise ValueError("attached_pic 不是 0 或 1")
                attached_pic = bool(attached_value)

        parsed.append(MediaStream(
            index=index,
            codec_type=codec_type,
            codec_name=codec_name,
            width=width,
            height=height,
            duration_s=duration,
            attached_pic=attached_pic,
            frame_count=frame_count,
            fps=fps,
        ))
    return tuple(parsed)


def select_main_video_stream(probe: MediaProbe) -> MediaStream | None:
    """仅在主视频唯一且所有视频流用途明确时返回主视频流。

    封面图不计入主视频；未标明用途的视频流和多路主视频均保持未知，
    调用者不得改用容器时长或把多路时长相加。主流选择与容器时长
    错误独立，调用者按所需逐流字段判断指标是否可靠。
    """
    videos = tuple(stream for stream in probe.streams if stream.codec_type == "video")
    if any(stream.attached_pic is None for stream in videos):
        return None
    primary = tuple(stream for stream in videos if stream.attached_pic is False)
    return primary[0] if len(primary) == 1 else None


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
    """经 ffprobe 取得容器时长、容器格式及每条流的媒体事实。

    新增逐流字段缺失时保留未知；存在但类型、数值或索引非法时拒绝
    整份事实。工具不可用、退出失败、输出结构非法与必需字段缺失
    分别分类。容器时长仍作为独立事实保存；视频覆盖使用唯一主视频
    流的逐流时长。仅在请求时，`-count_frames` 提供实际帧数。
    """
    host = resolve_file(input, roots)
    argv = (
        (request.ffprobe, "-v", "error")
        + request.extra_args
        + (("-count_frames",) if request.count_frames else ())
        + ("-show_entries",
           "format=duration,format_name:stream=index,codec_type,codec_name,width,height,duration,"
           "avg_frame_rate,nb_read_frames:"
           "stream_disposition=attached_pic",
           "-of", "json", str(host.path))
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
    format_facts = payload["format"]
    try:
        format_name = None
        if "format_name" in format_facts:
            format_name = format_facts["format_name"]
            if not isinstance(format_name, str) or not format_name:
                raise ValueError("format_name 不是非空字符串")
        if "duration" not in format_facts and "streams" not in payload:
            # 保留原单文件检查对旧版最小 ffprobe 输出的错误分类。
            return MediaProbe(
                duration_s=None,
                error="missing_duration: 缺少时长字段",
                format_name=format_name,
            )
        streams = _parse_streams(payload.get("streams"))
    except ValueError as error:
        return MediaProbe(
            duration_s=None, error=f"invalid_structure: {error}"
        )
    if "duration" not in format_facts:
        return MediaProbe(
            duration_s=None,
            error="missing_duration: 缺少时长字段",
            streams=streams,
            format_name=format_name,
        )
    try:
        duration = _media_duration(format_facts["duration"])
    except ValueError as error:
        return MediaProbe(
            duration_s=None, error=f"invalid_structure: {error}"
        )
    if not any(stream.codec_type == "video" for stream in streams):
        return MediaProbe(
            duration_s=None,
            error="no_video_stream: 容器没有视频流",
            streams=streams,
            format_name=format_name,
        )
    return MediaProbe(
        duration_s=duration,
        error=None,
        streams=streams,
        format_name=format_name,
    )


def _validate_repair_request(request: RepairRequest) -> None:
    indices = request.stream_indices
    if indices is not None:
        if not isinstance(indices, tuple) or not indices:
            raise ValueError("stream_indices 必须是非空索引元组")
        if any(
            isinstance(index, bool) or not isinstance(index, int) or index < 0
            for index in indices
        ):
            raise ValueError("stream_indices 只允许非负整数")
        if len(set(indices)) != len(indices):
            raise ValueError("stream_indices 不能重复")

    output_format = request.output_format
    if output_format is not None and (
        not isinstance(output_format, str)
        or _SAFE_CONTAINER_FORMAT.fullmatch(output_format) is None
    ):
        raise ValueError("output_format 必须是安全的容器格式标识")


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
    _validate_repair_request(request)
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
    """经 ffmpeg 显式映射可选流并分别确认退出、校验、同步与摘要。

    工具失败但输出文件存在时 exists 保留实际观察，complete 为
    False：部分成品不作为正式产物。
    """
    input_host = resolve_file(input, roots)
    output_host = resolve_file(output, roots)
    stream_args = tuple(
        argument
        for index in request.stream_indices or ()
        for argument in ("-map", f"0:{index}")
    )
    format_args = (
        ("-f", request.output_format)
        if request.output_format is not None else ()
    )
    argv = (
        (request.ffmpeg, "-nostdin", "-y", "-i", str(input_host.path))
        + stream_args
        + request.output_args
        + format_args
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

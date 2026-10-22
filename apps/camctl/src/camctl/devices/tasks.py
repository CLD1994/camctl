"""驱动在首次受理时提供的固定拍摄任务事实，不执行设备操作。"""
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Mapping, Protocol

from camctl.contracts.json_values import JsonValue


class EndControl(IntEnum):
    DEVICE = 1
    HOST_TIMER = 2


class StartReturn(IntEnum):
    SENT = 1
    STARTED = 2
    COMPLETED = 3


class CompletionMode(IntEnum):
    DEVICE_EVIDENCE = 1
    TIME_AND_OUTPUTS = 2


class CaptureResultPolicy(StrEnum):
    """显式固定拍摄结果的设备专属解释方式。"""

    ACTION6_VIDEO_SEGMENTS_V1 = "action6_video_segments_v1"
    OSMO360II_RECORD_SEGMENTS_V1 = "osmo360ii_record_segments_v1"
    OSMO_EMPTY_TAIL_CLASSIFICATION_V1 = "osmo_empty_tail_classification_v1"


class VideoResultMode(StrEnum):
    """按设备部署选择视频结果的单文件或分段处理流程。"""

    SINGLE_FILE = "single_file"
    SEGMENTED = "segmented"


def video_result_mode_for_declaration(
    declaration: Mapping[str, object],
) -> VideoResultMode:
    """读取已校验的设备视频结果模式；未声明时采用部署默认值。"""
    video = declaration.get("video", {})
    if not isinstance(video, Mapping):
        raise ValueError("video 部署选择必须是表")
    try:
        return VideoResultMode(video.get("result_mode", VideoResultMode.SINGLE_FILE.value))
    except (TypeError, ValueError) as error:
        raise ValueError("video.result_mode 必须是 single_file 或 segmented") from error


class CaptureStorage(StrEnum):
    """拍摄任务正式产物由设备保留还是写入主机。"""

    HOST = "host"
    DEVICE = "device"


class CancelCompletionPolicy(StrEnum):
    """停止未确认后的有限任务收场方式；缺省保持原停止契约。"""

    FINITE_DEVICE_END = "finite_device_end"


@dataclass(frozen=True)
class CaptureTask:
    """驱动从生效参数取得任务事实；目标与结果余量用秒，启动余量用整数毫秒。"""
    action_type: str
    target_duration_s: JsonValue = None
    stop_supported: bool | None = None
    duration_based: bool | None = None
    wait_after_send: bool | None = None
    end_control: EndControl | None = None
    start_return_meaning: StartReturn | None = None
    completion_mode: CompletionMode | None = None
    result_wait_margin_s: JsonValue = None
    start_call_timeout_s: JsonValue = None
    ownership_mode: int | None = None
    output_scope: dict[str, JsonValue] | None = None
    product_rules: tuple[Mapping[str, JsonValue], ...] | None = None
    video_result_mode: VideoResultMode | None = None
    result_policy: CaptureResultPolicy | None = None
    file_completion_wait_s: JsonValue = None
    output_storage: CaptureStorage | None = None
    cancel_completion_policy: CancelCompletionPolicy | None = None
    startup_margin_ms: int | None = None


class CaptureTaskFactory(Protocol):
    def __call__(self, effective_params: Mapping[str, JsonValue]) -> CaptureTask: ...


class CaptureStorageResolver(Protocol):
    """从单个设备的部署声明纯解析拍摄任务正式产物源。"""

    def __call__(self, device_declaration: Mapping[str, object]) -> CaptureStorage: ...


class DeploymentTaskFactory(Protocol):
    """从单个设备的部署声明绑定纯任务工厂。"""

    def __call__(self, declaration: Mapping[str, object]) -> CaptureTaskFactory: ...

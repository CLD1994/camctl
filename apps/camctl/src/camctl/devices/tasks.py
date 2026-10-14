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


class CaptureStorage(StrEnum):
    """拍摄任务正式产物由设备保留还是写入主机。"""

    HOST = "host"
    DEVICE = "device"


@dataclass(frozen=True)
class CaptureTask:
    """驱动从生效参数取得任务事实；时长与余量明确使用秒。"""
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
    file_completion_wait_s: JsonValue = None
    output_storage: CaptureStorage | None = None


class CaptureTaskFactory(Protocol):
    def __call__(self, effective_params: Mapping[str, JsonValue]) -> CaptureTask: ...


class DeploymentTaskFactory(Protocol):
    """从单个设备的部署声明绑定纯任务工厂。"""

    def __call__(self, declaration: Mapping[str, object]) -> CaptureTaskFactory: ...

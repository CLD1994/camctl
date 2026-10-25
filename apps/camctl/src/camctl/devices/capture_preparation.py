"""拍摄准备的公共动作、完成证据和会话内凭据。"""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal
from camctl.devices.bindings import DeviceBinding
from camctl.devices.evidence import EvidenceContract
from camctl.devices.ports import ControlRequest
from camctl.operations.models import AttemptTicket


_ACTION = enum_for("actions.type")
CAPTURE_OPERATIONS = MappingProxyType({
    int(_ACTION.CAMERA_TAKE_PHOTO): "take_photo",
    int(_ACTION.CAMERA_RECORD): "start_recording",
    int(_ACTION.CAMERA_TIMELAPSE): "start_timelapse",
})
CAPTURE_ACTION_TYPES = tuple(CAPTURE_OPERATIONS)
DEFAULT_PREPARE_LEAD_S = Decimal("10")
CAPTURE_PREPARATION_COMPLETED = EvidenceContract(
    "capture_preparation_completed", 1, "control", frozenset({"activity_id"}),
    identity_field="activity_id")


def capture_operation(action_type: int) -> str:
    """由已登记的拍摄动作取得控制操作，不为其他动作推测默认值。"""
    try:
        return CAPTURE_OPERATIONS[action_type]
    except KeyError as error:
        raise ValueError("该动作没有拍摄控制操作") from error


@dataclass(frozen=True)
class _PreparedCapture:
    binding: DeviceBinding
    serial: str
    operation: str
    params: Mapping
    ticket: AttemptTicket


class PreparedCaptureCache:
    """每个设备只保留最近一次成功准备；不访问设备或持久化状态。"""

    def __init__(self):
        self._captures: dict[str, _PreparedCapture] = {}

    def __len__(self) -> int:
        return len(self._captures)

    def clear(self, binding: DeviceBinding | None = None) -> None:
        if binding is None:
            self._captures.clear()
        else:
            self._captures.pop(binding.device_id, None)

    def remember(self, request: ControlRequest, serial: str) -> None:
        """调用方在组合可靠成功后登记调用前冻结的原参数。"""
        if request.ticket is None or request.ticket.operation != "control":
            raise ValueError("准备凭据必须属于原 control 尝试")
        self._captures[request.binding.device_id] = _PreparedCapture(
            request.binding, serial, request.operation, deepcopy(dict(request.params)), request.ticket)

    def matches(self, request: ControlRequest, serial: str) -> bool:
        ready = self._captures.get(request.binding.device_id)
        return (ready is not None and request.ticket is not None
                and request.prepared_by is not None
                and request.ticket.operation == "control"
                and ready.binding == request.binding and ready.serial == serial
                and ready.ticket == request.prepared_by
                and ready.ticket.target_id == request.ticket.target_id
                and ready.operation == request.operation
                and json_equal(ready.params, request.params))

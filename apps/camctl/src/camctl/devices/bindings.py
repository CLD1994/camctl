"""原绑定与本次配置的核对。

绑定是已保存事实：动作首次受理及文件首次记录时的 device_id 与
driver_id 不因新配置改写。核对结果分区表达匹配、配置缺失、驱动
不一致与配置不可靠读取；原绑定始终原样保留，供取回、删除及独立
收场按原驱动解释历史证据。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

__all__ = [
    "BindingResult",
    "BindingStatus",
    "DeviceBinding",
    "check_binding",
]


@dataclass(frozen=True)
class DeviceBinding:
    """已保存的设备绑定。"""

    device_id: str
    driver_id: str

    def __post_init__(self) -> None:
        if not self.device_id or not isinstance(self.device_id, str):
            raise ValueError(f"device_id 必须是非空标识: {self.device_id!r}")
        if not self.driver_id or not isinstance(self.driver_id, str):
            raise ValueError(f"driver_id 必须是非空标识: {self.driver_id!r}")


class BindingStatus(Enum):
    """绑定核对结果分区。"""

    MATCHED = "matched"
    DEVICE_MISSING = "device_missing"
    DRIVER_MISMATCH = "driver_mismatch"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class BindingResult:
    """核对结果：原绑定不变，另表达本次配置中的驱动。"""

    status: BindingStatus
    binding: DeviceBinding
    current_driver_id: str | None


def check_binding(
    saved: DeviceBinding,
    current: Any,
) -> BindingResult:
    """核对已保存绑定与本次配置；绑定错误只影响相应任务及收场。"""

    devices: Mapping[str, Any] = current.devices
    declaration = devices.get(saved.device_id)
    if declaration is None:
        return BindingResult(
            status=BindingStatus.DEVICE_MISSING,
            binding=saved,
            current_driver_id=None,
        )
    if not isinstance(declaration, Mapping):
        return BindingResult(
            status=BindingStatus.UNAVAILABLE,
            binding=saved,
            current_driver_id=None,
        )
    current_driver = declaration.get("driver")
    if not isinstance(current_driver, str) or not current_driver:
        # 声明存在但驱动不可靠读取：不折叠为缺失，也不猜默认。
        return BindingResult(
            status=BindingStatus.UNAVAILABLE,
            binding=saved,
            current_driver_id=None,
        )
    status = (
        BindingStatus.MATCHED
        if current_driver == saved.driver_id
        else BindingStatus.DRIVER_MISMATCH
    )
    return BindingResult(
        status=status, binding=saved, current_driver_id=current_driver
    )

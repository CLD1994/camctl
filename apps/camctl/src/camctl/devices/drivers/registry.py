"""驱动接入登记与能力受检访问。

登记项区分软件契约状态与实际设备验收状态：前者由受约束替身的
契约测试取得，后者记录实际设备联调的可复查输入，两者不互相代
替。消费者经 port_for 按静态声明取得端口；声明不支持的能力不可
调用，能力缺失与调用失败分开。第一版不登记任何具体厂商映射，
取得实际接口证据后另行编写。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from camctl.devices.evidence import EvidenceRegistry
from camctl.devices.ports import DriverDeclaration

__all__ = [
    "CapabilityNotDeclaredError",
    "DriverEntry",
    "DriverRegistry",
    "DriverStatus",
    "UnknownOperationError",
    "port_for",
]


class DriverStatus(Enum):
    """驱动接入项的证据状态。"""

    SOFTWARE_CONTRACT_VERIFIED = "software_contract_verified"
    DEVICE_VERIFICATION_PENDING = "device_verification_pending"


@dataclass(frozen=True)
class DriverEntry:
    """一个已登记驱动接入项：端口实现、声明、证据与验收状态。

    verified_inputs 保存已核验输入的可复查记录（操作、目标身份与
    输入形态）；没有记录不得宣称对应范围已经验证。
    """

    driver_id: str
    driver: Any
    declaration: DriverDeclaration
    evidence: EvidenceRegistry
    status: DriverStatus
    verified_inputs: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.driver_id, str) or not self.driver_id:
            raise ValueError(f"driver_id 必须是非空标识: {self.driver_id!r}")
        if not isinstance(self.evidence, EvidenceRegistry):
            raise ValueError("evidence 必须是 EvidenceRegistry")
        if not isinstance(self.declaration, DriverDeclaration):
            raise ValueError("declaration 必须是 DriverDeclaration")
        if not isinstance(self.status, DriverStatus):
            raise ValueError("status 必须是 DriverStatus")


class DriverRegistry:
    """已登记驱动接入项的唯一登记处。"""

    def __init__(self, entries: tuple[DriverEntry, ...] = ()) -> None:
        self._entries: dict[str, DriverEntry] = {}
        for entry in entries:
            if entry.driver_id in self._entries:
                raise ValueError(f"驱动重复登记: {entry.driver_id!r}")
            self._entries[entry.driver_id] = entry

    def entry(self, driver_id: str) -> DriverEntry | None:
        return self._entries.get(driver_id)


#: 普通操作及目录准备端口对应的声明；目录准备不建立普通尝试。
_DECLARATION_MEMBERS: Mapping[str, str] = {
    "control": "control_supported",
    "stop": "stop_supported",
    "query": "query_supported",
    "result": "result_supported",
    "read": "read_supported",
    "digest": "digest_supported",
    "delete": "delete_supported",
    "directory": "directory_supported",
}


class UnknownOperationError(ValueError):
    """操作名不在登记的端口集合中。"""


class CapabilityNotDeclaredError(Exception):
    """驱动静态声明不支持该操作；消费者不得调用对应端口。"""


def port_for(entry: DriverEntry, operation: str) -> Any:
    """按静态声明返回驱动端口；不支持时明确拒绝，不尝试调用。

    声明支持但驱动对象缺少对应方法属于装配错误，由属性访问直接
    暴露；本函数不检查验收状态——状态只表达证据范围。
    """
    member = _DECLARATION_MEMBERS.get(operation)
    if member is None:
        raise UnknownOperationError(f"未登记的操作: {operation!r}")
    if not getattr(entry.declaration, member):
        raise CapabilityNotDeclaredError(
            f"驱动 {entry.driver_id!r} 声明不支持 {operation!r}"
        )
    return entry.driver

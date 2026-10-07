"""按能力划分的设备驱动端口与可选能力声明。

驱动按控制、停止、查询、结果、读取、摘要和删除分别提供端口；
能力缺失（静态声明不支持）与调用失败分开表达。框架按声明选择
执行、完成判定和恢复方式，不统一要求设备具有状态查询等接口。
单次调用的传输与收场由 D3 受管调用承接，读取会话由 D4 承接；
本模块固定各端口的公共形状与声明。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

from camctl.devices.bindings import DeviceBinding
from camctl.devices.evidence import DeviceObservation

__all__ = [
    "DriverDeclaration",
    "ControlRequest",
    "DeviceCallResult",
    "ControlDriver",
    "StopDriver",
    "StateQueryDriver",
    "ResultDriver",
    "ReadDriver",
    "DigestDriver",
    "DeleteDriver",
]


@dataclass(frozen=True)
class DriverDeclaration:
    """驱动对七类操作及设备兼容性的显式能力声明。

    不支持以 False 表达，不是失败；声明支持但调用失败由调用结果
    的错误分区表达，不降级为不支持。capture_read_parallel_supported
    声明同设备拍摄与文件读取可并行；未验证并行控制的驱动保持缺省
    False，调度按拍摄与读取不并行的保守方式让路。
    """

    control_supported: bool
    stop_supported: bool
    query_supported: bool
    result_supported: bool
    read_supported: bool
    digest_supported: bool
    delete_supported: bool
    capture_read_parallel_supported: bool = False


@dataclass(frozen=True)
class ControlRequest:
    """一次单次操作的输入：操作名、原绑定与已确认的参数。"""

    operation: str
    binding: DeviceBinding
    params: Mapping[str, Any]


@dataclass(frozen=True)
class DeviceCallResult:
    """一次设备端口调用的结果分区：可靠观察与调用错误并存。

    已取得可靠事实后的通信异常同时保留两者；没有可靠业务观察时
    为空观察元组，不伪造“无效果”观察。
    """

    observations: tuple[DeviceObservation, ...]
    error: Mapping[str, Any] | None


@runtime_checkable
class ControlDriver(Protocol):
    """单次控制（如拍摄启动）端口。"""

    declaration: DriverDeclaration

    async def control(self, request: ControlRequest) -> DeviceCallResult:
        ...


@runtime_checkable
class StopDriver(Protocol):
    """设备明确支持的停止操作端口。"""

    declaration: DriverDeclaration

    async def stop(self, request: ControlRequest) -> DeviceCallResult:
        ...


@runtime_checkable
class StateQueryDriver(Protocol):
    """状态查询端口；无查询能力不是失败。"""

    declaration: DriverDeclaration

    async def query_state(self, request: ControlRequest) -> DeviceCallResult:
        ...


@runtime_checkable
class ResultDriver(Protocol):
    """分批结果列举端口。"""

    declaration: DriverDeclaration

    async def list_results(
        self, request: ControlRequest, batch: int
    ) -> DeviceCallResult:
        ...


@runtime_checkable
class ReadDriver(Protocol):
    """连续文件读取端口：打开可停止的读取会话（形状见 D4）。"""

    declaration: DriverDeclaration

    async def open_read(
        self, source: "object", offset: int, ticket: "object"
    ) -> "object":
        ...


@runtime_checkable
class DigestDriver(Protocol):
    """单文件摘要端口。"""

    declaration: DriverDeclaration

    async def digest(self, request: ControlRequest) -> DeviceCallResult:
        ...


@runtime_checkable
class DeleteDriver(Protocol):
    """单文件删除端口。"""

    declaration: DriverDeclaration

    async def delete(self, request: ControlRequest) -> DeviceCallResult:
        ...

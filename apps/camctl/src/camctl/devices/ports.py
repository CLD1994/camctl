"""按能力划分的设备驱动端口与可选能力声明。

驱动按控制、停止、查询、结果、读取、摘要和删除分别提供端口；
能力缺失（静态声明不支持）与调用失败分开表达。框架按声明选择
执行、完成判定和恢复方式，不统一要求设备具有状态查询等接口。
单次调用的传输与收场由 D3 受管调用承接，读取会话由 D4 承接；
本模块固定各端口的公共形状与声明。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping, Protocol, runtime_checkable

from camctl.devices.bindings import DeviceBinding
from camctl.devices.evidence import DeviceObservation, OPERATIONS

if TYPE_CHECKING:
    from camctl.operations.models import AttemptTicket, CallOutcome

__all__ = [
    "DriverDeclaration",
    "ControlRequest",
    "DeviceCallResult",
    "ControlDriver",
    "StopDriver",
    "StateQueryDriver",
    "FileQueryDriver",
    "ResultDriver",
    "ReadDriver",
    "DigestDriver",
    "DeleteDriver",
]


@dataclass(frozen=True)
class DriverDeclaration:
    """驱动对各类操作及设备兼容性的显式能力声明。

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
    #: 逐操作声明普通前台命令的恢复假设；缺省不授权恢复。
    adb_foreground_recovery_operations: frozenset[str] = frozenset()
    #: 启动前完整目录准备，独立于任务结果列举能力。
    directory_supported: bool = False
    #: 仅声明 READ 的本地主机收场恢复，不推断设备效果或独立活动结束。
    local_read_recovery_supported: bool = False
    #: 按文件身份查询存在性；独立于拍摄活动状态查询。
    file_query_supported: bool = False

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if name != "adb_foreground_recovery_operations" and type(value) is not bool:
                raise ValueError(f"驱动声明字段 {name} 必须是布尔值")
        operations = self.adb_foreground_recovery_operations
        if not isinstance(operations, frozenset) or not operations <= OPERATIONS:
            raise ValueError("前台命令恢复适用范围必须是已登记操作的 frozenset")
        if self.delete_supported and not self.file_query_supported:
            raise ValueError("删除能力声明必须同时支持独立文件查询")


@dataclass(frozen=True)
class ControlRequest:
    """单次操作的输入：原绑定、参数、已提交尝试与本次调用期限。

    元数据控制调用的 timeout_s 交给实际受管调用计时，不限制后续
    收场时间。摘要等没有此类期限的操作可以省略该值。
    """

    operation: str
    binding: DeviceBinding
    params: Mapping[str, Any]
    ticket: AttemptTicket | None = None
    timeout_s: Decimal | None = None
    #: 多步启动在新设备派发前调用；None 返回值表示仍有原业务资格。
    dispatch_check: Callable[[], str | None] | None = None
    #: 普通录像原受理定义中的返回含义；省略保持旧 STARTED 合同。
    start_return_meaning: int | None = None
    #: 结果适配器核验后的同尝试前页历史；只读回调不进入 JSON params。
    prior_result_entries: Callable[[], Iterator[Mapping[str, Any]]] | None = None


@dataclass(frozen=True)
class DeviceCallResult:
    """一次设备端口调用的结果分区：可靠观察与调用错误并存。

    已取得可靠事实后的通信异常同时保留两者；没有可靠业务观察时
    为空观察元组，不伪造“无效果”观察。
    """

    observations: tuple[DeviceObservation, ...]
    error: Mapping[str, Any] | None
    #: 受管调用的完整权威结果；观察和错误是兼容业务端口的同源视图。
    outcome: CallOutcome | None = None
    #: 仅本次内部诊断构造约束；冻结上下文，原拥有者持有结果后才调用。
    diagnostics_check: Callable[[CallOutcome], None] | None = None

    def __post_init__(self) -> None:
        if self.outcome is not None:
            if (self.observations != self.outcome.observations
                    or self.error != self._error_view(self.outcome)):
                raise ValueError("设备端口的观察及错误必须与完整调用结果一致")

    @staticmethod
    def _error_view(outcome: CallOutcome) -> Mapping[str, Any] | None:
        if outcome.error is None:
            return None
        return {"code": outcome.error.code, "stage": outcome.error.stage,
                "details": dict(outcome.error.details)}

    @classmethod
    def from_outcome(cls, outcome: CallOutcome, *,
                     diagnostics_check: Callable[[CallOutcome], None] | None = None) -> DeviceCallResult:
        """保留原效果、收场、错误详情和调用信息，不重新解释结果。"""
        return cls(outcome.observations, cls._error_view(outcome), outcome, diagnostics_check)


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
class FileQueryDriver(Protocol):
    """按原文件身份查询存在性；与拍摄活动状态查询分离。"""

    declaration: DriverDeclaration

    async def query_file(self, request: ControlRequest) -> DeviceCallResult:
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
        self, source: "object", offset: int, ticket: "object", *,
        idle_timeout_s: Decimal,
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

"""取回推进运行时的会话级装配。

按设备声明与登记驱动解析读取协作者：驱动声明读取能力且设备装
配完成时构造该设备的读取端口、绑定、证据与源端摘要工厂；读取
重试间隔取自 devices.<id>.copy.retry_interval_s，拍摄与读取的并
行兼容取自驱动能力声明（缺省不并行）。会话内共享重试间隔时间
门槛；未声明读取能力的设备不进入读取推进，其取回条目保持已建
档状态等待后续会话。
"""

from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from camctl.bootstrap.capture_assembly import (
    _DEFAULT_RETRY_INTERVAL_S,
    _device_seconds,
    _device_attempts,
    _digest_reader_factory,
)
from camctl.devices.bindings import DeviceBinding, DeviceConfigurationError, check_binding
from camctl.capture.recovery import RecoveryBoundary, RecoveryDiagnostic, recovery_registry
from camctl.devices.drivers.registry import (
    CapabilityNotDeclaredError, DriverRegistry, port_for,
)
from camctl.operations.attempts import RetryWaitGate
from camctl.host_files.tasks import FileTaskExecutor
from camctl.outputs.handoff import DeliveryDirectories
from camctl.outputs.obtain_flow import (
    DeviceReadAssembly, ObtainRuntime, advance_obtain,
)
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import OutputsRepository

__all__ = ["obtain_flow", "session_obtain_assembly"]

#: 拷贝推进段大小的装配默认值；生产装配传入配置的 copy.segment_size。
_DEFAULT_SEGMENT_SIZE = 1024 * 1024


def session_obtain_assembly(
    *,
    devices: Mapping[str, Mapping[str, Any]],
    drivers: DriverRegistry,
    staging: Path,
    ready: Path,
    processing: Path,
    segment_size: int = _DEFAULT_SEGMENT_SIZE,
    monotonic_ns: Callable[[], int] | None = None,
    occurred_at: Callable[[], int] | None = None,
    recovery_boundary: RecoveryBoundary = RecoveryBoundary.UNCONFIRMED,
    recovery_max_event_id: Callable[[], int | None] | None = None,
    on_recovery_diagnostic: Callable[[RecoveryDiagnostic], None] | None = None,
    file_executor: FileTaskExecutor | None = None,
) -> Callable[[Any], ObtainRuntime]:
    """构造会话级取回推进工厂：解析各设备读取协作者并组装运行时。

    会话共享重试间隔时间门槛；原文件设备缺失或驱动改变由逐项
    绑定核对处理。不可可靠读取的当前设备声明或未登记驱动属于
    配置前提失败。时钟读数缺省使用真实系统钟，测试可注入受控读数。
    """
    retry_gate = RetryWaitGate()
    monotonic = monotonic_ns if monotonic_ns is not None else time.monotonic_ns
    wall = occurred_at if occurred_at is not None \
        else (lambda: int(time.time() * 1_000_000))
    directories = DeliveryDirectories(
        staging=staging, ready=ready, processing=processing)
    continuing_read_tickets: dict = {}
    pending_read_results: dict = {}
    pending_read_business: dict = {}
    pending_read_ends: dict = {}

    def factory(owned: Any) -> ObtainRuntime:
        read_ports: dict[str, DeviceReadAssembly] = {}
        for device_id, declaration in devices.items():
            if not isinstance(declaration, Mapping):
                raise DeviceConfigurationError(f"设备声明不可可靠读取: {device_id}")
            driver_id = declaration.get("driver")
            entry = drivers.entry(driver_id) if isinstance(
                driver_id, str) else None
            if entry is None:
                raise DeviceConfigurationError(f"本次设备驱动未登记: {device_id} driver={driver_id!r}")
            try:
                read_port = port_for(entry, "read")
            except CapabilityNotDeclaredError:
                continue
            binding = DeviceBinding(
                device_id=device_id, driver_id=str(driver_id))
            digest_supported = bool(entry.declaration.digest_supported)
            read_ports[device_id] = DeviceReadAssembly(
                driver=read_port,
                binding=binding,
                evidence=entry.evidence,
                digest_supported=digest_supported,
                digest_for=(
                    _digest_reader_factory(
                        owned, entry.driver, binding, entry.evidence)
                    if digest_supported else None),
                retry_interval_s=_device_seconds(
                    declaration, "copy", "retry_interval_s",
                    _DEFAULT_RETRY_INTERVAL_S),
                capture_read_parallel=bool(
                    entry.declaration.capture_read_parallel_supported),
                max_read_attempts=_device_attempts(declaration, "copy", 3, "max_read_attempts"),
                read_idle_timeout_s=_device_seconds(declaration, "copy", "read_idle_timeout_s", Decimal("10")),
                max_recopies=_device_attempts(declaration, "copy", 1, "max_recopies"),
            )
        return ObtainRuntime(
            owned=owned,
            devices=read_ports,
            directories=directories,
            segment_size=segment_size,
            retry_gate=retry_gate,
            monotonic_ns=monotonic,
            occurred_at=wall,
            outputs=OutputsRepository(),
            operations=OperationRepository(),
            binding_check=lambda saved: check_binding(saved, SimpleNamespace(devices=devices)),
            recovery_boundary=recovery_boundary,
            recovery_max_event_id=None if recovery_max_event_id is None else recovery_max_event_id(),
            recovery_evidence_for=lambda saved, operation: recovery_registry(drivers.entry(saved.driver_id), operation),
            on_recovery_diagnostic=on_recovery_diagnostic,
            continuing_read_tickets=continuing_read_tickets,
            pending_read_results=pending_read_results,
            pending_read_business=pending_read_business,
            pending_read_ends=pending_read_ends,
            file_executor=file_executor,
        )

    return factory


def obtain_flow(factory: Callable[[Any], ObtainRuntime]):
    """构造推进取回执行的流程端口；每轮驱动一次。"""

    async def flow(context: Any) -> None:
        owned = context.open_connection()
        try:
            await advance_obtain(factory(owned))
        finally:
            owned.connection.close()

    return flow

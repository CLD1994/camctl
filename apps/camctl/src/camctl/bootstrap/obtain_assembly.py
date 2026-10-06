"""取回推进运行时的会话级装配。

按设备声明与登记驱动解析读取协作者：驱动声明读取能力且设备装
配完成时构造该设备的读取端口、绑定、证据与源端摘要工厂；读取
重试间隔取自 devices.<id>.copy.retry_interval_s。会话内共享重试
间隔时间门槛；未声明读取能力的设备不进入读取推进，其取回条目
保持已建档状态等待后续会话。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Mapping

from camctl.bootstrap.capture_assembly import (
    _DEFAULT_RETRY_INTERVAL_S,
    _device_seconds,
    _digest_reader_factory,
)
from camctl.devices.bindings import DeviceBinding
from camctl.devices.drivers.registry import (
    CapabilityNotDeclaredError, DriverRegistry, port_for,
)
from camctl.operations.attempts import RetryWaitGate
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
) -> Callable[[Any], ObtainRuntime]:
    """构造会话级取回推进工厂：解析各设备读取协作者并组装运行时。

    会话共享重试间隔时间门槛；设备未声明、驱动未登记或读取能力
    未声明时该设备不进入读取推进，取回条目保持已建档状态等待后
    续会话。时钟读数缺省使用真实系统钟，测试可注入受控读数。
    """
    retry_gate = RetryWaitGate()
    monotonic = monotonic_ns if monotonic_ns is not None else time.monotonic_ns
    wall = occurred_at if occurred_at is not None \
        else (lambda: int(time.time() * 1_000_000))
    directories = DeliveryDirectories(
        staging=staging, ready=ready, processing=processing)

    def factory(owned: Any) -> ObtainRuntime:
        read_ports: dict[str, DeviceReadAssembly] = {}
        for device_id, declaration in devices.items():
            if not isinstance(declaration, Mapping):
                continue
            driver_id = declaration.get("driver")
            entry = drivers.entry(driver_id) if isinstance(
                driver_id, str) else None
            if entry is None:
                continue
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

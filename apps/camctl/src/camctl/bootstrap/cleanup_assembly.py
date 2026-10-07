"""清理推进运行时的会话级装配。

按设备声明与登记驱动解析删除与查询协作者：驱动同时声明删除与查
询能力且设备装配完成时，以该设备构造清理运行时；删除与查询的尝
试上限取自 cleanup 配置，调用时限与重试间隔取自
devices.<id>.cleanup.*。第一版单相机运行假设下装配首个可用设备；
成员目标绑定其他设备或主机派生成品时该成员保持等待，待对应链路
接入。会话内共享重试间隔时间门槛；未声明删除或查询能力的设备不
进入清理推进。
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Callable, Mapping

from camctl.bootstrap.capture_assembly import (
    _DEFAULT_RETRY_INTERVAL_S,
    _device_seconds,
)
from camctl.devices.bindings import DeviceBinding
from camctl.devices.drivers.registry import (
    CapabilityNotDeclaredError, DriverRegistry, port_for)
from camctl.operations.attempts import AttemptConfig, RetryWaitGate
from camctl.outputs.cleanup_flow import CleanupRuntime, advance_cleanup
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import OutputsRepository

__all__ = ["cleanup_flow", "session_cleanup_assembly"]

#: 删除与查询调用时限的规格默认秒数（configuration.md 设备文件删
#: 除与查询的计时）。
_DEFAULT_TIMEOUT_S = Decimal("10")


def session_cleanup_assembly(
    *,
    devices: Mapping[str, Mapping[str, Any]],
    drivers: DriverRegistry,
    max_delete_attempts: int,
    max_query_attempts: int,
    monotonic_ns: Callable[[], int] | None = None,
    occurred_at: Callable[[], int] | None = None,
) -> Callable[[Any], CleanupRuntime | None]:
    """构造会话级清理推进工厂：解析删除/查询协作者并组装运行时。

    会话共享重试间隔时间门槛；设备未声明、驱动未登记或删除/查询
    能力未声明时本会话不装配清理推进，成员保持已保存状态等待后
    续会话。时钟读数缺省使用真实系统钟，测试可注入受控读数。
    """
    retry_gate = RetryWaitGate()
    monotonic = monotonic_ns if monotonic_ns is not None else time.monotonic_ns
    wall = occurred_at if occurred_at is not None \
        else (lambda: int(time.time() * 1_000_000))

    def factory(owned: Any) -> CleanupRuntime | None:
        for device_id, declaration in devices.items():
            if not isinstance(declaration, Mapping):
                continue
            driver_id = declaration.get("driver")
            entry = drivers.entry(driver_id) if isinstance(
                driver_id, str) else None
            if entry is None:
                continue
            try:
                port_for(entry, "delete")
                port_for(entry, "query")
            except CapabilityNotDeclaredError:
                continue
            connection = owned.connection

            def binding_of(item_id: int, _device=device_id,
                           _driver=str(driver_id)):
                return _member_binding(
                    connection, item_id, _device, _driver)

            return CleanupRuntime(
                owned=owned,
                outputs=OutputsRepository(),
                operations=OperationRepository(),
                driver=entry.driver,
                evidence=entry.evidence,
                binding_of=binding_of,
                occurred_at=wall,
                delete_config=AttemptConfig(
                    max_attempts=max_delete_attempts,
                    timeout_s=_device_seconds(
                        declaration, "cleanup", "delete_timeout_s",
                        _DEFAULT_TIMEOUT_S),
                    retry_interval_s=_device_seconds(
                        declaration, "cleanup", "delete_retry_interval_s",
                        _DEFAULT_RETRY_INTERVAL_S)),
                query_config=AttemptConfig(
                    max_attempts=max_query_attempts,
                    timeout_s=_device_seconds(
                        declaration, "cleanup", "query_timeout_s",
                        _DEFAULT_TIMEOUT_S),
                    retry_interval_s=_device_seconds(
                        declaration, "cleanup", "query_retry_interval_s",
                        _DEFAULT_RETRY_INTERVAL_S)),
                monotonic_ns=monotonic,
                retry_gate=retry_gate,
            )
        return None

    return factory


def _member_binding(connection, item_id: int, device_id: str,
                    driver_id: str) -> DeviceBinding | None:
    """按成员产物解析设备绑定；主机派生成品或他设备目标返回 None。"""
    row = connection.execute(
        "SELECT a.device_id FROM cleanup_items c"
        " JOIN outputs o ON o.id = c.output_id"
        " JOIN device_files f ON f.id = o.device_file_id"
        " JOIN actions a ON a.id = f.observer_action_id"
        " WHERE c.id = ?", (item_id,)).fetchone()
    if row is None or row[0] != device_id:
        return None
    return DeviceBinding(device_id=device_id, driver_id=driver_id)


def cleanup_flow(factory: Callable[[Any], CleanupRuntime | None]):
    """构造推进清理执行的流程端口；每轮驱动一次。"""

    async def flow(context: Any) -> None:
        owned = context.open_connection()
        try:
            runtime = factory(owned)
            if runtime is not None:
                await advance_cleanup(runtime)
        finally:
            owned.connection.close()

    return flow

"""清理推进运行时的会话级装配。

按设备声明与登记驱动解析删除与查询协作者，每个清理成员使用原
文件观察者保存的设备身份和驱动。删除与查询的尝试上限取自 cleanup
配置，调用时限与重试间隔取自对应设备的 devices.<id>.cleanup.*。
主机派生成品经 staging 工作根的本地删除协作者清理，独立于设备
目录是否为空。会话内共享重试间隔时间门槛。
"""

from __future__ import annotations

import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from camctl.bootstrap.capture_assembly import (
    _DEFAULT_RETRY_INTERVAL_S,
    _device_seconds,
)
from camctl.devices.bindings import DeviceBinding, DeviceConfigurationError, check_binding
from camctl.devices.drivers.registry import (
    CapabilityNotDeclaredError, DriverRegistry, port_for)
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult
from camctl.operations.attempts import AttemptConfig, RetryWaitGate
from camctl.outputs.cleanup_flow import (
    CleanupRuntime, HostArtifactPort, LocalArtifactRequest, advance_cleanup)
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import OutputsRepository

__all__ = [
    "HostArtifacts", "cleanup_flow", "session_cleanup_assembly"]

#: 删除与查询调用时限的规格默认秒数（configuration.md 设备文件删
#: 除与查询的计时）。
_DEFAULT_TIMEOUT_S = Decimal("10")

_LOCAL_EVIDENCE = EvidenceRegistry((
    EvidenceContract(type="delete_returned", version=1, operation="delete", fields=frozenset()),
    EvidenceContract(type="file_absent", version=1, operation="delete",
                     fields=frozenset({"cleanup_item_id"}), identity_field="cleanup_item_id"),
    EvidenceContract(type="file_presence", version=1, operation="query",
                     fields=frozenset({"cleanup_item_id", "present"}), identity_field="cleanup_item_id"),
))


class HostArtifacts(HostArtifactPort):
    """主机派生成品的本地删除与存在性查询；观察与设备调用同形。

    删除目标相对 staging 工作根解析；删除成功与目标本就不在同样
    产出可靠缺席观察，输入输出失败保留调用错误进入查询核实。
    """

    def __init__(self, staging: Path) -> None:
        self._staging = Path(staging)

    async def delete(
            self, request: LocalArtifactRequest) -> DeviceCallResult:
        target = self._staging / request.path
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return DeviceCallResult(
                observations=(),
                error={"code": "local_io_failed", "stage": "delete"})
        return DeviceCallResult(
            observations=(DeviceObservation(
                type="file_absent", version=1,
                data={"cleanup_item_id": str(request.cleanup_item_id)}),),
            error=None)

    async def query_state(
            self, request: LocalArtifactRequest) -> DeviceCallResult:
        target = self._staging / request.path
        try:
            target.stat()
            present = True
        except FileNotFoundError:
            present = False
        except OSError:
            return DeviceCallResult(
                observations=(),
                error={"code": "local_io_failed", "stage": "query"})
        return DeviceCallResult(
            observations=(DeviceObservation(
                type="file_presence", version=1,
                data={"cleanup_item_id": str(request.cleanup_item_id),
                      "present": present}),),
            error=None)


def session_cleanup_assembly(
    *,
    devices: Mapping[str, Mapping[str, Any]],
    drivers: DriverRegistry,
    max_delete_attempts: int,
    max_query_attempts: int,
    staging: Path | None = None,
    monotonic_ns: Callable[[], int] | None = None,
    occurred_at: Callable[[], int] | None = None,
) -> Callable[[Any], CleanupRuntime | None]:
    """构造会话级清理推进工厂：解析删除/查询协作者并组装运行时。

    会话共享重试间隔时间门槛；每个设备成员分别核对原绑定并选择
    本次配置。不可可靠读取的设备声明或未登记的当前驱动属于配置
    前提失败。主机派生成品的本地协作者按 staging 工作根装配。
    时钟读数缺省使用真实系统钟，测试可注入受控读数。
    """
    retry_gate = RetryWaitGate()
    monotonic = monotonic_ns if monotonic_ns is not None else time.monotonic_ns
    wall = occurred_at if occurred_at is not None \
        else (lambda: int(time.time() * 1_000_000))

    def factory(owned: Any) -> CleanupRuntime:
        device_ports = {}
        for device_id, declaration in devices.items():
            if not isinstance(declaration, Mapping):
                raise DeviceConfigurationError(f"设备声明不可可靠读取: {device_id}")
            driver_id = declaration.get("driver")
            entry = drivers.entry(driver_id) if isinstance(
                driver_id, str) else None
            if entry is None:
                raise DeviceConfigurationError(f"本次设备驱动未登记: {device_id} driver={driver_id!r}")
            try:
                port_for(entry, "delete")
                port_for(entry, "query")
            except CapabilityNotDeclaredError:
                continue
            device_ports[device_id] = (
                entry, AttemptConfig(
                    max_attempts=max_delete_attempts,
                    timeout_s=_device_seconds(
                        declaration, "cleanup", "delete_timeout_s",
                        _DEFAULT_TIMEOUT_S),
                    retry_interval_s=_device_seconds(
                        declaration, "cleanup", "delete_retry_interval_s",
                        _DEFAULT_RETRY_INTERVAL_S)),
                AttemptConfig(
                    max_attempts=max_query_attempts,
                    timeout_s=_device_seconds(
                        declaration, "cleanup", "query_timeout_s",
                        _DEFAULT_TIMEOUT_S),
                    retry_interval_s=_device_seconds(
                        declaration, "cleanup", "query_retry_interval_s",
                        _DEFAULT_RETRY_INTERVAL_S)))
        base = CleanupRuntime(
            owned=owned, outputs=OutputsRepository(), operations=OperationRepository(),
            driver=None, evidence=_LOCAL_EVIDENCE,
            binding_of=lambda item_id: _member_binding(owned.connection, item_id),
            binding_check=lambda saved: check_binding(saved, SimpleNamespace(devices=devices)),
            occurred_at=wall,
            local_files=HostArtifacts(staging) if staging is not None else None,
            delete_config=AttemptConfig(max_delete_attempts, _DEFAULT_TIMEOUT_S, _DEFAULT_RETRY_INTERVAL_S),
            query_config=AttemptConfig(max_query_attempts, _DEFAULT_TIMEOUT_S, _DEFAULT_RETRY_INTERVAL_S),
            monotonic_ns=monotonic, retry_gate=retry_gate,
        )

        def for_item(item_id):
            binding = base.binding_of(item_id)
            assembly = device_ports.get(binding.device_id) if binding is not None else None
            if assembly is None:
                return base
            entry, delete_config, query_config = assembly
            return replace(base, driver=entry.driver, evidence=entry.evidence,
                           delete_config=delete_config, query_config=query_config)

        base.for_item = for_item
        return base

    return factory


def _member_binding(connection, item_id: int) -> DeviceBinding | None:
    """设备成员使用原文件观察者的绑定；主机成员没有设备绑定。"""
    row = connection.execute(
        "SELECT a.device_id,a.driver_id FROM cleanup_items c"
        " JOIN outputs o ON o.id = c.output_id"
        " JOIN device_files f ON f.id = o.device_file_id"
        " JOIN actions a ON a.id = f.observer_action_id"
        " WHERE c.id = ?", (item_id,)).fetchone()
    if row is None:
        return None
    return DeviceBinding(device_id=row[0], driver_id=row[1])


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

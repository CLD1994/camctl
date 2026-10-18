"""清理推进运行时的会话级装配。

按设备声明与登记驱动解析删除与文件查询协作者，每个清理成员使用原
文件观察者保存的设备身份和驱动。设备删除与查询的次数上限取自
cleanup 配置，调用时限与重试间隔取自对应设备的
devices.<id>.cleanup.*。主机派生成品使用 staging 根和会话共享文件
执行器，次数上限取自 cleanup 配置，不设置应用层时限或定时重试。
"""

from __future__ import annotations

import time
import asyncio
import sqlite3
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from camctl.bootstrap.capture_assembly import (
    _DEFAULT_RETRY_INTERVAL_S,
    _device_seconds,
)
from camctl.contracts.values import ConsistencyError
from camctl.devices.bindings import (
    DeviceBinding, DeviceConfigurationError, binding_failure_details, check_binding)
from camctl.devices.drivers.registry import (
    CapabilityNotDeclaredError, DriverRegistry, port_for)
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult
from camctl.host_files.declarations import HOST_FILE_DECLARATION
from camctl.host_files.models import (
    BoundDirectories, FileObservationKind, FileRef)
from camctl.host_files.paths import observe_file, resolve_file
from camctl.host_files.tasks import (
    FileTask, FileTaskExecutor, FileTaskId, FileTaskResult, FileTaskHandle)
from camctl.operations.attempts import AttemptConfig, RetryWaitGate
from camctl.operations.owned_calls import check_call_interruption, current_call_scope
from camctl.operations.recovery import RecoveryBoundary, recovery_registry
from camctl.outputs.cleanup_flow import (
    CleanupRuntime, HostArtifactPort, LocalArtifactRequest, LocalArtifactTaskFailure,
    LocalArtifactTargetError,
    PendingCleanupCompanion, PendingCleanupQuery, PendingCleanupIntent,
    PendingCleanupDelete, advance_cleanup)
from camctl.outputs.cleanup_ownership import PendingCleanupSave
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import OutputsRepository

__all__ = [
    "HostArtifacts", "cleanup_flow", "session_cleanup_assembly"]

#: 删除与查询调用时限的规格默认秒数（configuration.md 设备文件删
#: 除与查询的计时）。
_DEFAULT_TIMEOUT_S = Decimal("10")

_LOCAL_EVIDENCE = EvidenceRegistry((
    EvidenceContract("local_file_recovery", 1, "delete", frozenset()),
    EvidenceContract(type="delete_returned", version=1, operation="delete", fields=frozenset()),
    EvidenceContract(type="file_absent", version=1, operation="delete",
                     fields=frozenset({"cleanup_item_id"}), identity_field="cleanup_item_id"),
    EvidenceContract(type="dispatch_prevented", version=1, operation="delete", fields=frozenset()),
    EvidenceContract(type="file_presence", version=1, operation="query",
                     fields=frozenset({"cleanup_item_id", "present"}), identity_field="cleanup_item_id"),
))


class HostArtifacts(HostArtifactPort):
    """主机派生成品的本地删除与存在性查询；观察与设备调用同形。

    删除目标相对 staging 工作根解析；删除成功与目标本就不在同样
    产出可靠缺席观察，输入输出失败保留调用错误进入查询核实。
    """

    declaration = HOST_FILE_DECLARATION
    evidence = _LOCAL_EVIDENCE

    def __init__(self, staging: Path, executor: FileTaskExecutor | None = None) -> None:
        self._staging = Path(staging)
        self._executor = executor

    @property
    def staging(self) -> Path:
        return self._staging

    async def delete(
            self, request: LocalArtifactRequest) -> DeviceCallResult:
        return await self._run(request)

    async def query_file(
            self, request: LocalArtifactRequest) -> DeviceCallResult:
        return await self._run(request)

    async def _run(self, request: LocalArtifactRequest) -> DeviceCallResult:
        check_call_interruption()
        if self._executor is None:
            raise ConsistencyError("主机清理未装配共享文件执行器")
        self._validate_request(request)
        task_id = FileTaskId(
            f"cleanup:{request.database_id}:{request.operation}:"
            f"{request.ticket.run_id}:{request.ticket.attempt_id}")
        task = FileTask(
            task_id=task_id, file_id=request.file_ref.file_id,
            stage=f"cleanup_{request.operation}", business="host_artifact_cleanup",
            body=lambda _stop_event: self._execute(request),
            resources=(f"database:{request.database_id}",
                       f"cleanup:{request.cleanup_item_id}"))
        owner = _LocalArtifactResultOwner()
        interruption = None
        runner_error = None
        try:
            result = await self._executor.run_file_task(
                task, owner, handoff_withdrawn=True)
        except asyncio.CancelledError as error:
            interruption = error
            result = None
        except Exception as error:
            if owner.handle is None:
                # Admission/framework 拒绝不说明原任务没有被其他 owner 派发。
                raise ConsistencyError(
                    "主机文件任务未能取得可验证的原 completion") from error
            runner_error = error
            result = None

        if interruption is not None or runner_error is not None:
            result, interruption, drain_error = await _collect_file_result(
                self._executor, owner, task_id, interruption)
            if result is None:
                error = drain_error or runner_error or interruption
                raise ConsistencyError(
                    "主机文件任务监督收场未取得原 completion") from error
        else:
            drain_error = None

        actual = _device_result(request, result)
        if interruption is not None:
            scope = current_call_scope()
            if scope is not None:
                scope.interrupted = interruption
                if drain_error is not None:
                    interruption.add_note(
                        f"主机文件任务 drain 故障: {type(drain_error).__name__}: {drain_error}")
                    interruption.__cause__ = drain_error
                return actual
            diagnostic = "主机文件等待已取消，原 completion 已取回"
            if drain_error is not None:
                diagnostic += f"；drain 收场失败: {drain_error}"
            raise LocalArtifactTaskFailure(actual, diagnostic) from interruption
        if runner_error is not None or drain_error is not None:
            diagnostic = "文件执行器 runner/supervisor 收场失败；原实际结果已保留"
            cause = runner_error or drain_error
            if runner_error is not None and drain_error is not None:
                cause = BaseExceptionGroup("文件任务收场故障", [runner_error, drain_error])
            raise LocalArtifactTaskFailure(actual, diagnostic) from cause
        return actual

    def _validate_request(self, request: LocalArtifactRequest) -> None:
        if request.operation not in ("delete", "query"):
            raise ConsistencyError("主机文件请求操作未登记")
        expected_key = (f"delete/{request.cleanup_item_id}"
                        if request.operation == "delete"
                        else f"exists/{request.cleanup_item_id}")
        if (request.database_id == "" or request.ticket.operation != request.operation
                or request.ticket.target_id != str(request.cleanup_item_id)
                or request.ticket.responsibility_key != expected_key):
            raise ConsistencyError("主机文件请求与原票据不对应")
        if request.file_ref.root != self._staging:
            raise LocalArtifactTargetError("主机文件请求绑定了不同 staging 根")
        try:
            resolve_file(request.file_ref, BoundDirectories(self._staging))
        except (TypeError, ValueError) as error:
            raise LocalArtifactTargetError(
                f"主机文件请求身份或路径不合法: {error}") from error

    def _execute(self, request: LocalArtifactRequest) -> DeviceCallResult:
        """在线程任务中观察目标，并按保存身份执行单文件操作。"""
        observed = observe_file(request.file_ref, BoundDirectories(self._staging))
        if request.operation == "query":
            if observed.kind is FileObservationKind.ERROR:
                return _local_io_error("query", observed.error)
            if observed.kind is FileObservationKind.TYPE_MISMATCH:
                return _local_type_error("query")
            present = observed.kind is FileObservationKind.VALID_OBJECT
            return DeviceCallResult((DeviceObservation(
                type="file_presence", version=1,
                data={"cleanup_item_id": str(request.cleanup_item_id),
                      "present": present}),), None)

        if request.operation != "delete":
            raise ConsistencyError(f"主机文件任务操作未登记: {request.operation}")
        if observed.kind is FileObservationKind.ERROR:
            return _local_io_error("delete", observed.error)
        if observed.kind is FileObservationKind.TYPE_MISMATCH:
            return _local_type_error("delete")
        if observed.kind is FileObservationKind.MISSING:
            return _local_absent(request.cleanup_item_id)
        target = resolve_file(request.file_ref, BoundDirectories(self._staging)).path
        try:
            target.unlink()
        except FileNotFoundError:
            return _local_absent(request.cleanup_item_id)
        except OSError as error:
            after = observe_file(request.file_ref, BoundDirectories(self._staging))
            if after.kind is FileObservationKind.MISSING:
                return _local_absent(request.cleanup_item_id, error)
            return _local_io_error("delete", error)
        return _local_absent(request.cleanup_item_id)


class _LocalArtifactResultOwner:
    """只暂存本次任务 handle 与其 FileTaskResult，不接触清理运行时。"""

    def __init__(self) -> None:
        self.handle: FileTaskHandle | None = None
        self.result: FileTaskResult | None = None

    def retain_handle(self, handle: FileTaskHandle) -> None:
        self.handle = handle

    def release_handle(self) -> None:
        """清理长寿命 supervisor owner 中已完成的文件任务引用。"""
        self.handle = None

    async def take_over(self, task) -> None:
        self.retain_handle(task.pending)
        self.result = await task.pending.wait()


async def _collect_file_result(executor, owner, task_id, interruption):
    """shield 同一个共享 drain，并在 owner 故障后从原 handle 补取 completion。"""
    drain = asyncio.create_task(executor.drain_required())
    drain_error = None
    supervision = None
    while True:
        try:
            supervision = await asyncio.shield(drain)
            break
        except asyncio.CancelledError as error:
            if interruption is None:
                interruption = error
            if drain.done() and drain.cancelled():
                drain_error = error
                break
        except Exception as error:
            drain_error = error
            break

    result = owner.result
    if result is None and owner.handle is not None:
        take = asyncio.create_task(owner.handle.wait())
        while True:
            try:
                result = await asyncio.shield(take)
                break
            except asyncio.CancelledError as error:
                if interruption is None:
                    interruption = error
                if take.done() and take.cancelled():
                    drain_error = drain_error or error
                    break
            except Exception as error:
                drain_error = drain_error or error
                break
    if supervision is not None:
        failed = next((failure for failure in supervision.failed
                       if failure.record.identity == task_id.value), None)
        if failed is not None:
            drain_error = drain_error or RuntimeError(failed.error)
    if result is not None:
        owner.result = result
        owner.release_handle()
    return result, interruption, drain_error


def _device_result(request: LocalArtifactRequest, result: FileTaskResult | None) -> DeviceCallResult:
    from camctl.outputs.cleanup_flow import _not_dispatched

    expected_id = FileTaskId(
        f"cleanup:{request.database_id}:{request.operation}:"
        f"{request.ticket.run_id}:{request.ticket.attempt_id}")
    if not isinstance(result, FileTaskResult) or result.task_id != expected_id:
        raise ConsistencyError("主机文件任务 completion 与原请求不对应")
    if not result.ran:
        return _not_dispatched(request.operation)
    if result.error is not None:
        return DeviceCallResult((), {
            "code": "local_io_failed", "stage": request.operation,
            "details": {"task_error": result.error},
        })
    if not isinstance(result.value, DeviceCallResult):
        raise ConsistencyError("主机文件任务未返回完整 DeviceCallResult")
    return result.value


def _local_absent(item_id: int, error: OSError | None = None) -> DeviceCallResult:
    return DeviceCallResult((DeviceObservation(
        type="file_absent", version=1,
        data={"cleanup_item_id": str(item_id)}),),
        None if error is None else {
            "code": "local_io_failed", "stage": "delete",
            "details": {"error": f"{type(error).__name__}: {error}"},
        })


def _local_io_error(operation: str, error: OSError | None) -> DeviceCallResult:
    return DeviceCallResult((), {
        "code": "local_io_failed", "stage": operation,
        "details": {"error": ("unknown local filesystem error" if error is None else
                               f"{type(error).__name__}: {error}")},
    })


def _local_type_error(operation: str) -> DeviceCallResult:
    return DeviceCallResult((), {
        "code": "local_io_failed", "stage": operation,
        "details": {"reason": "target is not a regular file"},
    })


def session_cleanup_assembly(
    *,
    devices: Mapping[str, Mapping[str, Any]],
    drivers: DriverRegistry,
    max_delete_attempts: int,
    max_query_attempts: int,
    staging: Path | None = None,
    file_executor: FileTaskExecutor | None = None,
    monotonic_ns: Callable[[], int] | None = None,
    occurred_at: Callable[[], int] | None = None,
    pending_queries: dict[tuple[int, int], PendingCleanupQuery] | None = None,
    pending_companions: dict[int, PendingCleanupCompanion] | None = None,
    pending_intents: dict[int, PendingCleanupIntent] | None = None,
    pending_deletes: dict[int, PendingCleanupDelete] | None = None,
    pending_saves: dict[tuple[str, int], PendingCleanupSave] | None = None,
    recovery_boundary: RecoveryBoundary = RecoveryBoundary.UNCONFIRMED,
    recovery_max_event_id: int | Callable[[], int | None] | None = None,
    on_recovery_diagnostic: Callable | None = None,
    discovery_batch_size: int = 100,
    consumed_queries: dict | None = None,
    active_calls: set | None = None,
) -> Callable[[Any], CleanupRuntime | None]:
    """构造会话级清理推进工厂：解析删除/查询协作者并组装运行时。

    会话共享重试间隔时间门槛；每个设备成员分别核对原绑定并选择
    本次配置。不可可靠读取的设备声明或未登记的当前驱动属于配置
    前提失败。主机文件协作者使用会话共享 FileTaskExecutor；主机尝试
    只采用正式次数预算，不设置应用层时限或定时重试间隔。
    时钟读数缺省使用真实系统钟，测试可注入受控读数。
    """
    retry_gate = RetryWaitGate()
    queries = pending_queries if pending_queries is not None else {}
    companions = pending_companions if pending_companions is not None else {}
    consumed_queries = consumed_queries if consumed_queries is not None else {}
    active = active_calls if active_calls is not None else set()
    intents = pending_intents if pending_intents is not None else {}
    deletes = pending_deletes if pending_deletes is not None else {}
    saves = pending_saves if pending_saves is not None else {}
    monotonic = monotonic_ns if monotonic_ns is not None else time.monotonic_ns
    wall = occurred_at if occurred_at is not None \
        else (lambda: int(time.time() * 1_000_000))

    def factory(owned: Any) -> CleanupRuntime:
        device_ports = None

        def load_ports():
            nonlocal device_ports
            if device_ports is None:
                device_ports = _device_ports()
            return device_ports

        def operation_config(declaration, operation):
            return AttemptConfig(
                max_attempts=max_delete_attempts if operation == "delete" else max_query_attempts,
                timeout_s=_device_seconds(declaration, "cleanup", f"{operation}_timeout_s", _DEFAULT_TIMEOUT_S),
                retry_interval_s=_device_seconds(declaration, "cleanup", f"{operation}_retry_interval_s", _DEFAULT_RETRY_INTERVAL_S))

        def _device_ports():
            assembled = {}
            for device_id, declaration in devices.items():
                if not isinstance(declaration, Mapping):
                    raise DeviceConfigurationError(f"设备声明不可可靠读取: {device_id}")
                driver_id = declaration.get("driver")
                entry = drivers.entry(driver_id) if isinstance(driver_id, str) else None
                if entry is None:
                    raise DeviceConfigurationError(f"本次设备驱动未登记: {device_id} driver={driver_id!r}")
                try:
                    port_for(entry, "delete")
                    port_for(entry, "file_query")
                except CapabilityNotDeclaredError:
                    continue
                assembled[device_id] = (entry, operation_config(declaration, "delete"),
                                        operation_config(declaration, "query"))
            return assembled

        # 原历史与本地保存先交接；当前端口仅在需要新调用时解析。
        from camctl.outputs.cleanup_discovery import has_cleanup_handoff
        if not any((queries, companions, intents, deletes, saves)) and not has_cleanup_handoff(owned.connection):
            load_ports()
        base = CleanupRuntime(
            owned=owned, outputs=OutputsRepository(), operations=OperationRepository(),
            driver=None, evidence=_LOCAL_EVIDENCE,
            binding_of=lambda item_id: _member_binding(owned.connection, item_id),
            binding_check=lambda saved: check_binding(saved, SimpleNamespace(devices=devices)),
            occurred_at=wall,
            local_files=(HostArtifacts(staging, file_executor)
                         if staging is not None else None),
            local_file_root=staging,
            delete_config=AttemptConfig(max_delete_attempts, None, None),
            query_config=AttemptConfig(max_query_attempts, None, None),
            monotonic_ns=monotonic, retry_gate=retry_gate,
            pending_queries=queries, pending_companions=companions,
            pending_intents=intents, pending_deletes=deletes, pending_saves=saves,
            consumed_queries=consumed_queries,
            recovery_boundary=recovery_boundary,
            recovery_max_event_id=(recovery_max_event_id() if callable(recovery_max_event_id)
                                   else recovery_max_event_id),
            recovery_evidence_for=lambda binding, operation: (
                recovery_registry(HostArtifacts, operation) if binding is None
                else recovery_registry(drivers.entry(binding.driver_id), operation)),
            on_recovery_diagnostic=on_recovery_diagnostic,
            discovery_batch_size=discovery_batch_size, active_calls=active,
        )

        def for_item(item_id):
            binding = base.binding_of(item_id)
            if binding is None or binding_failure_details(check_binding(
                    binding, SimpleNamespace(devices=devices))) is not None:
                return base
            assembly = load_ports().get(binding.device_id)
            if assembly is None:
                entry = drivers.entry(binding.driver_id)
                if entry is None:
                    raise DeviceConfigurationError(f"原清理驱动未登记: {binding.driver_id}")
                if not entry.declaration.delete_supported:
                    return replace(base, delete_supported=False)
                # 删除能力存在但独立文件查询端口缺失是装配前提错误。
                port_for(entry, "file_query")
                raise DeviceConfigurationError(f"清理协作者未装配: {binding.device_id}")
            entry, delete_config, query_config = assembly
            return replace(base, driver=entry.driver, evidence=entry.evidence,
                           delete_config=delete_config, query_config=query_config,
                           delete_supported=True, query_supported=True)

        def for_operation(item_id, operation):
            binding = base.binding_of(item_id)
            if binding is None or binding_failure_details(check_binding(
                    binding, SimpleNamespace(devices=devices))) is not None:
                return base
            declaration = devices[binding.device_id]
            entry = drivers.entry(binding.driver_id)
            if entry is None:
                raise DeviceConfigurationError(f"原清理驱动未登记: {binding.driver_id}")
            supported = (entry.declaration.delete_supported if operation == "delete"
                         else entry.declaration.file_query_supported)
            config = {}
            if supported:
                port_for(entry, "delete" if operation == "delete" else "file_query")
                config[f"{operation}_config"] = operation_config(declaration, operation)
            return replace(base, driver=entry.driver, evidence=entry.evidence,
                delete_supported=entry.declaration.delete_supported,
                query_supported=entry.declaration.file_query_supported, **config)

        base.for_operation = for_operation
        base.for_item = for_item
        return base

    factory.pending_collections = (intents, deletes, queries, companions, saves, active)
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


class CleanupFlow:
    """会话拥有全部原保存集合；在途调用的 await 范围负责实际收场。"""

    def __init__(self, factory, pending_collections=None, *, state_db_errors=False):
        self._factory = factory
        self._pending = tuple(pending_collections if pending_collections is not None
                              else getattr(factory, "pending_collections", ()))
        self._state_db_errors = state_db_errors
        self._active = False
        self._settled = None
        self._stopping = False

    def required_settlements(self):
        return int(self._active) + sum(len(collection) for collection in self._pending)

    def stop_new_work(self):
        self._stopping = True

    async def settle(self):
        if self._active:
            await self._settled.wait()
        if self.required_settlements():
            from camctl.session.service import StateDbFailure
            raise StateDbFailure("原清理保存责任尚未可靠交接")

    async def __call__(self, context):
        if self._stopping or self._active:
            return
        self._active = True
        self._settled = asyncio.Event()
        try:
            owned = context.open_connection()
            try:
                runtime = self._factory(owned)
                if runtime is not None:
                    await advance_cleanup(runtime)
            finally:
                owned.connection.close()
        except (sqlite3.Error, ConsistencyError) as error:
            if not self._state_db_errors:
                raise
            from camctl.session.service import StateDbFailure
            raise StateDbFailure(f"清理无法可靠读取或保存状态库事实: {error}") from error
        finally:
            self._active = False
            self._settled.set()


def cleanup_flow(factory: Callable[[Any], CleanupRuntime | None], *, pending_collections=None,
                 state_db_errors=False):
    """构造会话实际清理拥有者；消费标记不构成待存责任。"""
    return CleanupFlow(factory, pending_collections, state_db_errors=state_db_errors)

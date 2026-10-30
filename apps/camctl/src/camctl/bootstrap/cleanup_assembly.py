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
from typing import Any, Awaitable, Callable, Mapping

from camctl.bootstrap.background_flow import BackgroundFlow, CombinedLocalWork, FlowWakeup
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
from camctl.operations.error_diagnostics import exception_diagnostic
from camctl.operations.attempts import AttemptConfig, RetryWaitGate
from camctl.operations.owned_calls import check_call_interruption, current_call_scope
from camctl.operations.recovery import RecoveryBoundary, recovery_registry
from camctl.outputs.cleanup_flow import (
    CleanupRuntime, CleanupSupervisionDiagnostic, CleanupSupervisionFault,
    HostArtifactPort, LocalArtifactRequest, LocalArtifactTaskFailure, LocalArtifactTargetError,
    PendingCleanupCompanion, PendingCleanupQuery, PendingCleanupIntent,
    PendingCleanupDelete, advance_cleanup)
from camctl.outputs.cleanup_ownership import PendingCleanupSave
from camctl.persistence.models import DbReadError
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.scheduling.notifications import WakeReason

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
            result, interruption, supervision_diagnostic = await _collect_file_result(
                self._executor, owner, task, interruption, runner_error)
            if result is None:
                failures = []
                if interruption is not None:
                    failures.append(interruption)
                if supervision_diagnostic is not None:
                    failures.append(supervision_diagnostic.exception or RuntimeError(supervision_diagnostic.render()))
                error = (BaseExceptionGroup(
                    "主机文件收场未取得原 completion，等待控制与监督诊断均须保留",
                    failures) if len(failures) > 1 else
                    failures[0] if failures else None)
                raise ConsistencyError(
                    "主机文件任务监督收场未取得原 completion") from error
        else:
            supervision_diagnostic = None

        actual = _device_result(request, result)
        scope = current_call_scope()
        if interruption is not None:
            if scope is not None:
                if scope.interrupted is None:
                    scope.interrupted = interruption
            if supervision_diagnostic is None:
                if scope is not None:
                    return actual
                raise LocalArtifactTaskFailure(
                    actual, None, "主机文件等待已取消，原 completion 已取回") from interruption
        if supervision_diagnostic is not None:
            failure = LocalArtifactTaskFailure(actual, supervision_diagnostic)
            if interruption is not None and scope is None:
                raise failure from interruption
            raise failure from (supervision_diagnostic.exception or RuntimeError(supervision_diagnostic.render()))
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
            if after.kind is FileObservationKind.ERROR and isinstance(after.error, BaseException):
                error = BaseExceptionGroup("删除及后续文件观察均失败", [error, after.error])
            return _local_io_error("delete", error)
        return _local_absent(request.cleanup_item_id)


class _LocalArtifactResultOwner:
    """只暂存本次任务 handle 与其 FileTaskResult，不接触清理运行时。"""

    def __init__(self) -> None:
        self.handle: FileTaskHandle | None = None
        self.result: FileTaskResult | None = None
        self._settled = False

    def retain_handle(self, handle: FileTaskHandle) -> None:
        if not self._settled:
            self.handle = handle

    def release_handle(self, handle: FileTaskHandle | None = None) -> None:
        """清理长寿命 supervisor owner 中已完成的文件任务引用。"""
        if handle is None or self.handle is handle:
            self.handle = None

    def record_result(self, result: FileTaskResult) -> None:
        self.result = result
        self._settled = True
        self.release_handle()

    async def take_over(self, task) -> None:
        if self._settled:
            return
        handle = task.pending
        self.retain_handle(handle)
        try:
            self.record_result(await handle.wait())
        finally:
            self._settled = True
            self.release_handle(handle)


def _task_context(task):
    return (
        task.task_id.value, task.stage, task.business,
        (f"file:{task.file_id}", *task.resources),
    )


def _safe_error_text(error):
    try:
        return str(error)
    except BaseException as formatting_error:
        return f"<诊断格式化失败: {type(formatting_error).__name__}>"


def _exception_facts(error, source, context=None):
    """将异常及其 cause/context 转成不携带 traceback 的事实。"""
    facts = []
    pending = [(error, source)]
    seen = set()
    while pending:
        current, current_source = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        identity, stage, business, resources = context or (None, None, None, ())
        facts.append(CleanupSupervisionFault(
            source=current_source,
            identity=identity,
            stage=stage,
            business=business,
            resources=resources,
            error_type=type(current).__name__,
            error_message=_safe_error_text(current),
            notes=tuple(_safe_error_text(note)
                        for note in getattr(current, "__notes__", ())),
        ))
        if isinstance(current, BaseExceptionGroup):
            pending.extend((child, f"{current_source}.nested")
                           for child in current.exceptions)
        if current.__cause__ is not None:
            pending.append((current.__cause__, f"{current_source}.cause"))
        if current.__context__ is not None:
            pending.append((current.__context__, f"{current_source}.context"))
    return facts


def _has_supervision_error(error):
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        elif isinstance(current, Exception):
            return True
        pending.extend(value for value in (current.__cause__, current.__context__) if value is not None)
    return False


def _interruption_facts(error, context):
    """只记录取消携带的真实故障，不把取消或说明备注改成监督错误。"""
    if not _has_supervision_error(error):
        return []
    facts = []
    notes = tuple(_safe_error_text(note)
                  for note in getattr(error, "__notes__", ()))
    if notes:
        identity, stage, business, resources = context
        facts.append(CleanupSupervisionFault(
            source="wait_cancellation", identity=identity, stage=stage,
            business=business, resources=resources,
            error_type=type(error).__name__, error_message="等待被取消",
            notes=notes))
    if error.__cause__ is not None:
        facts.extend(_exception_facts(
            error.__cause__, "wait_cancellation.cause", context))
    if error.__context__ is not None:
        facts.extend(_exception_facts(
            error.__context__, "wait_cancellation.context", context))
    return facts


def _supervision_diagnostic(facts, errors=()):
    unique = tuple(dict.fromkeys(facts))
    originals = []
    for error in errors:
        if all(error is not original for original in originals):
            originals.append(error)
    if len(originals) == 1:
        actual = originals[0]
    elif originals:
        actual = BaseExceptionGroup("清理文件任务监督发生多个实际错误", originals)
    else:
        actual = None
    return None if not unique and actual is None else CleanupSupervisionDiagnostic(unique, actual)


async def _collect_file_result(executor, owner, task, interruption, runner_error):
    """shield 同一共享 drain，完整保留全部失败并从原 handle 补取 completion。"""
    task_context = _task_context(task)
    facts = []
    errors = []
    if runner_error is not None:
        errors.append(runner_error)
        facts.extend(_exception_facts(runner_error, "runner", task_context))
    if interruption is not None:
        if _has_supervision_error(interruption):
            errors.append(interruption)
        facts.extend(_interruption_facts(interruption, task_context))
    # fallback 必须使用进入共享 drain 前接管的局部引用；callback 可能在
    # drain 期间结束并主动释放 owner.handle。
    handle = owner.handle
    drain = asyncio.create_task(executor.drain_required())
    supervision = None
    while True:
        try:
            supervision = await asyncio.shield(drain)
            break
        except asyncio.CancelledError as error:
            if interruption is None:
                interruption = error
            facts.extend(_interruption_facts(error, task_context))
            if _has_supervision_error(error):
                errors.append(error)
            if drain.done() and drain.cancelled():
                if _has_supervision_error(error):
                    facts.extend(_exception_facts(error, "shared_drain_cancelled"))
                break
        except Exception as error:
            errors.append(error)
            facts.extend(_exception_facts(error, "shared_drain"))
            break

    result = owner.result
    if result is None and handle is not None:
        take = asyncio.create_task(handle.wait())
        while True:
            try:
                result = await asyncio.shield(take)
                break
            except asyncio.CancelledError as error:
                if interruption is None:
                    interruption = error
                facts.extend(_interruption_facts(error, task_context))
                if _has_supervision_error(error):
                    errors.append(error)
                if take.done() and take.cancelled():
                    if _has_supervision_error(error):
                        facts.extend(_exception_facts(error, "fallback_cancelled", task_context))
                    break
            except Exception as error:
                errors.append(error)
                facts.extend(_exception_facts(error, "fallback", task_context))
                break
    if supervision is not None:
        for failure in supervision.failed:
            record = failure.record
            if failure.exception is not None:
                errors.append(failure.exception)
            error_type, separator, error_message = failure.error.partition(": ")
            facts.append(CleanupSupervisionFault(
                source="supervisor_owner",
                identity=record.identity,
                stage=record.stage,
                business=record.business,
                resources=record.resources,
                error_type=error_type if separator else "TaskFailure",
                error_message=error_message if separator else failure.error,
            ))
    if result is not None:
        owner.record_result(result)
    return result, interruption, _supervision_diagnostic(facts, errors)


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
            "details": {"error": f"{type(error).__name__}: {error}",
                        "diagnostic": exception_diagnostic(error)},
        })


def _local_io_error(operation: str, error: BaseException | None) -> DeviceCallResult:
    return DeviceCallResult((), {
        "code": "local_io_failed", "stage": operation,
        "details": {"error": ("unknown local filesystem error" if error is None else
                               f"{type(error).__name__}: {error}"),
                    "diagnostic": exception_diagnostic(error)},
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
) -> Callable[[BusinessDatabase], Awaitable[CleanupRuntime | None]]:
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
    pending_file_batches = {}
    monotonic = monotonic_ns if monotonic_ns is not None else time.monotonic_ns
    wall = occurred_at if occurred_at is not None \
        else (lambda: int(time.time() * 1_000_000))

    async def settle_file_batches(database):
        from camctl.outputs.batches import retry_file_batches
        await retry_file_batches(database, pending_file_batches)

    async def factory(database) -> CleanupRuntime:
        await settle_file_batches(database)
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
        if not any((queries, companions, intents, deletes, saves)) and not await database.read(
                lambda owned: has_cleanup_handoff(owned.connection), description="检查原清理交接责任"):
            load_ports()
        async def binding_of(item_id):
            return await database.read(
                lambda owned: _member_binding(owned.connection, item_id),
                description="读取清理文件原绑定")

        base = CleanupRuntime(
            database=database, outputs=OutputsRepository(), operations=OperationRepository(),
            driver=None, evidence=_LOCAL_EVIDENCE,
            binding_of=binding_of,
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
            pending_file_batches=pending_file_batches,
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

        async def for_item(item_id):
            binding = await base.binding_of(item_id)
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

        async def for_operation(item_id, operation):
            binding = await base.binding_of(item_id)
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

    factory.pending_collections = (intents, deletes, queries, companions, saves, active, pending_file_batches)
    factory.settle_file_batches = settle_file_batches
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


class _CleanupRound:
    """持有异步数据库端口及全部原保存集合，直至实际调用可靠交接。"""

    def __init__(self, factory, pending_collections=None, *, state_db_errors=False):
        self._factory = factory
        self._pending = tuple(pending_collections if pending_collections is not None
                              else getattr(factory, "pending_collections", ()))
        self._state_db_errors = state_db_errors
        self._active = False
        self._settled = None
        self._stopping = False
        self._database = None

    def required_settlements(self):
        return int(self._active) + sum(len(collection) for collection in self._pending)

    def stop_new_work(self):
        self._stopping = True

    async def settle(self):
        if self._active:
            await self._settled.wait()
        settle_batches = getattr(self._factory, "settle_file_batches", None)
        if settle_batches is not None and self._database is not None:
            await settle_batches(self._database)
        if self.required_settlements():
            from camctl.session.service import StateDbFailure
            raise StateDbFailure("原清理保存责任尚未可靠交接")

    async def __call__(self, context):
        if self._stopping or self._active:
            return FlowWakeup.IDLE
        self._active = True
        self._settled = asyncio.Event()
        try:
            self._database = context.database
            runtime = await self._factory(context.database)
            if runtime is not None:
                required = await advance_cleanup(runtime)
                return FlowWakeup.IDLE if required is False else FlowWakeup.RECHECK
            return FlowWakeup.IDLE
        except (sqlite3.Error, ConsistencyError, DbReadError) as error:
            if not self._state_db_errors:
                raise
            from camctl.session.service import StateDbFailure
            raise StateDbFailure(f"清理无法可靠读取或保存状态库事实: {error}") from error
        finally:
            self._active = False
            self._settled.set()


class CleanupFlow:
    """独立推进唯一清理轮次，组合实际任务与原保存责任。"""

    def __init__(self, factory, pending_collections=None, *, state_db_errors=False):
        self._round = _CleanupRound(
            factory, pending_collections, state_db_errors=state_db_errors)
        self._background = BackgroundFlow(self._round)
        self._local_work = CombinedLocalWork((self._background, self._round))
        self._stop_work = CombinedLocalWork((self._round, self._background))
        self._notification_task = None
        self._notified = False

    async def __call__(self, context):
        try:
            await self._background(context)
        finally:
            self._release_consumed_notification()
            task = self._background.unconsumed_task
            if task is not None and task is not self._notification_task:
                self._notification_task = task
                self._notified = False
                wake = getattr(context, "wake", None)

                def completed(actual):
                    if (actual is self._notification_task
                            and actual is self._background.unconsumed_task
                            and actual.done() and not self._notified):
                        self._notified = True
                        if (wake is not None
                                and self._background.completion_requires_wakeup(actual)):
                            wake.mark_changed(WakeReason.ACTIVITY_FINISHED)

                task.add_done_callback(completed)

    def check_completed(self):
        try:
            self._background.check_completed()
        finally:
            self._release_consumed_notification()

    def _release_consumed_notification(self):
        if self._background.unconsumed_task is None:
            self._notification_task = None
            self._notified = False

    def required_settlements(self):
        return self._local_work.required_settlements()

    def stop_new_work(self):
        self._stop_work.stop_new_work()

    async def settle(self):
        try:
            await self._local_work.settle()
        finally:
            self._release_consumed_notification()


def cleanup_flow(factory: Callable[[Any], CleanupRuntime | None], *, pending_collections=None,
                 state_db_errors=False):
    """构造会话实际清理拥有者；消费标记不构成待存责任。"""
    return CleanupFlow(factory, pending_collections, state_db_errors=state_db_errors)

"""取回动作执行编排：开始、来源固定、选择、建档、读取与发布。

每个推进轮次先开始到时的取回动作，再对执行中的动作按已保存事
实幂等推进：执行期来源解析一次固定，来源终态且产物处理完成后
固定选择，逐选中条目申请读取资格建档（主机源与设备源产物共同
建档），全部条目取得最终结果后统一发布成功交付并保存动作终
态。设备源拷贝的实际读取按统一设备工作计划推进：设备存在到时
拍摄、占用拍摄或让路中的在途读取时本轮不推进读取；空闲设备上
已建档拷贝经读取机会事务取得归属后，在读取尝试预算内打开设备
会话完成可靠分段拷贝与完整性收尾，预算耗尽把交付结束为终局失
败并交还机会。主机源拷贝不占用相机机会，也不采用设备配置：按
中间文件登记事实打开本地顺序读取，单次尝试预算内完成同样的分
段与完整性收尾，失败即终局失败交付。
"""

from __future__ import annotations

import re
from contextlib import closing
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Callable, Mapping

from camctl.capture.files import FileChecksumSave
from camctl.capture.recovery import RecoveryBoundary, RecoveryDiagnostic
from camctl.capture.input_copy import (
    InputPhase, InputStep, RecordingCopies, RecordingSource)
from camctl.capture.media_flow import DriverReadSessions, RecordingInputCopies
from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import (
    ConsistencyError, new_operation_key)
from camctl.devices.bindings import BindingResult, BindingStatus, DeviceBinding
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.devices.read_session import ReadChunk, ReadEnd
from camctl.host_files.io import LocalSourceReader
from camctl.host_files.models import BoundDirectories
from camctl.host_files.tasks import FileTaskExecutor
from camctl.operations.attempts import (
    AttemptConfig, AttemptFinish, AttemptIntent, AttemptTarget,
    BeginDisposition, OperationKind, RunFinish, RunOutcome, RetryWaitGate)
from camctl.operations.models import (
    AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue,
    Settlement, SettlementBasis)
from camctl.operations.validation import validate_outcome
from camctl.outputs.copy import (
    CompletionPhase, CopyCompletionError, CopyPreparationError,
    CopySegmentError, RecopyExhausted, ResumeOutcome, SegmentOutcome,
    SegmentSaveDisposition)
from camctl.outputs.dispatch import plan_device_work
from camctl.outputs.handoff import (
    DeliveryContext, DeliveryDirectories, DeliveryPhase, publish_delivery)
from camctl.outputs.qualification import (
    FileCandidate, OperationConfig, QualificationOutcome)
from camctl.outputs.read_attempts import acquire_read_attempt, recover_unavailable_read
from camctl.outputs.slots import SlotOutcome, SlotRequest
from camctl.outputs.sources import (
    ResolutionState, SelectionSnapshot, SourceResolution, SourceSpec,
    select_outputs)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import (
    FailReadBinding, FailReadDelivery, FinishObtain, FixSelection, OutputsRepository,
    ResolveSources, StartObtainAction, load_obtain_facts,
    load_selection_facts)

__all__ = [
    "DeviceReadAssembly",
    "ObtainRuntime",
    "advance_obtain",
]

_ACTION_STATUS = enum_for("actions.status")
_RESOLUTION_STATE = enum_for("actions.source_resolution_state")
_SELECTION_STATUS = enum_for("obtain_source_selections.status")
_ITEM_STATUS = enum_for("obtain_items.status")
_DELIVERY_STATUS = enum_for("deliveries.status")
_VERIFICATION = enum_for("file_copies.verification_state")

#: 已完成副本校验的固定事实组合（与拷贝收尾口径一致）。
_VERIFICATION_DONE = (3, 5)

#: 交付不再有读取意义的终态（失败、取消与撤回；与读取事实口径一致）。
_INACTIVE_DELIVERIES = tuple(
    int(_DELIVERY_STATUS[member])
    for member in ("FAILED", "CANCELED", "WITHDRAWN"))

#: 交付扩展名的安全字符集；设备原始名称不满足时按未知类型处理。
_SAFE_EXTENSION = re.compile(r"[A-Za-z0-9]+")
_UNKNOWN_EXTENSION = "bin"

#: 独立装配的设备读取默认值；正式会话从设备配置取得采用值。
_READ_MAX_ATTEMPTS = 3
_READ_TIMEOUT_S = Decimal("10")

#: 主机源读取的证据契约：读取操作专属命名，与设备读取同名同版。
_LOCAL_READ_EVIDENCE = EvidenceRegistry(contracts=(
    EvidenceContract(
        type="read_returned", version=1, operation="read",
        fields=frozenset()),
))


@dataclass(frozen=True)
class DeviceReadAssembly:
    """一台设备的读取协作者；装配层按登记驱动解析。"""

    driver: Any
    binding: Any
    evidence: Any
    digest_supported: bool
    digest_for: Callable[[int], Any] | None
    retry_interval_s: Decimal
    #: 驱动声明该设备拍摄与读取可并行（缺省不并行）。
    capture_read_parallel: bool = False
    max_read_attempts: int = _READ_MAX_ATTEMPTS
    read_idle_timeout_s: Decimal = _READ_TIMEOUT_S
    max_recopies: int = 1


@dataclass
class ObtainRuntime:
    """一次取回推进轮次的协作者集合。"""

    owned: Any
    devices: Mapping[str, DeviceReadAssembly]
    directories: DeliveryDirectories
    segment_size: int
    retry_gate: RetryWaitGate
    monotonic_ns: Callable[[], int]
    occurred_at: Callable[[], int]
    outputs: OutputsRepository
    operations: OperationRepository
    binding_check: Callable[[DeviceBinding], BindingResult] | None = None
    recovery_boundary: RecoveryBoundary = RecoveryBoundary.UNCONFIRMED
    recovery_max_event_id: int | None = None
    recovery_evidence_for: Callable | None = None
    on_recovery_diagnostic: Callable[[RecoveryDiagnostic], None] | None = None
    last_recovery_diagnostic: RecoveryDiagnostic | None = None
    continuing_read_tickets: dict = field(default_factory=dict)
    pending_read_results: dict = field(default_factory=dict)
    file_executor: FileTaskExecutor | None = None
    pending_read_business: dict = field(default_factory=dict)
    pending_read_ends: dict = field(default_factory=dict)


async def advance_obtain(runtime: ObtainRuntime) -> None:
    """推进一个轮次的取回执行链；各步按已保存事实幂等。"""
    from camctl.outputs.read_attempts import retry_read_business, retry_read_ends

    retry_read_business(runtime)
    _retry_read_results(runtime)
    await retry_read_ends(runtime)
    _settle_failed_reads(runtime)
    now = runtime.occurred_at()
    _start_due(runtime, now)
    for action_id in _running_actions(runtime.owned.connection):
        await _advance_action(runtime, action_id, now)
    await _advance_reads(runtime, now)
    await _advance_local_reads(runtime, now)


# ---- 动作生命周期 ----


def _start_due(runtime: ObtainRuntime, now: int) -> None:
    """开始到时的待执行取回动作；未到时与已取消动作不进入。"""
    with closing(runtime.owned.connection.execute(
        "SELECT id FROM actions"
        " WHERE status = 1 AND cancel_requested = 0 AND type = 4"
        " AND scheduled_at <= ? ORDER BY plan_id, input_index", (now,),
    )) as cursor:
        due = [int(row[0]) for row in cursor.fetchall()]
    for action_id in due:
        outcome = runtime.outputs.start_obtain_action(
            StartObtainAction(action_id=action_id, occurred_at=now),
            new_operation_key(), runtime.owned)
        if outcome.kind is not DbOutcomeKind.COMPLETED:
            raise ConsistencyError(
                f"取回开始事务未完成（{outcome.kind.value}）:"
                f" {outcome.error}")


def _running_actions(connection) -> list[int]:
    """执行中且未请求取消的取回动作。"""
    with closing(connection.execute(
        "SELECT id FROM actions"
        " WHERE status = 2 AND cancel_requested = 0 AND type = 4"
        " ORDER BY plan_id, input_index",
    )) as cursor:
        return [int(row[0]) for row in cursor.fetchall()]


async def _advance_action(
        runtime: ObtainRuntime, action_id: int, now: int) -> None:
    """按已保存事实推进一个执行中的取回动作的下一个阶段。"""
    connection = runtime.owned.connection
    action = _action_facts(connection, action_id)
    if action["source_resolution_state"] == int(
            _RESOLUTION_STATE.PENDING):
        if not _resolve_sources(runtime, action, now):
            return
        action = _action_facts(connection, action_id)
    if action["source_resolution_state"] != int(_RESOLUTION_STATE.FIXED):
        return
    _fix_pending_selections(runtime, action, now)
    _qualify_selected_items(runtime, action, now)
    await _publish_and_finish(runtime, action_id, now)


def _action_facts(connection, action_id: int) -> dict:
    with closing(connection.execute(
        "SELECT a.id, a.name, a.plan_id, a.input_fields_json,"
        " a.execution_spec_json, a.source_resolution_state,"
        " a.resolved_source_plan_id, p.name AS plan_name"
        " FROM actions a JOIN plans p ON p.id = a.plan_id WHERE a.id = ?",
        (action_id,),
    )) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError(f"取回动作不存在: {action_id}")
    columns = (
        "id", "name", "plan_id", "input_fields_json", "execution_spec_json",
        "source_resolution_state", "resolved_source_plan_id", "plan_name")
    return dict(zip(columns, row))


def _resolve_sources(
        runtime: ObtainRuntime, action: Mapping, now: int) -> bool:
    """执行期来源解析；失败保存动作终态后返回假。"""
    outcome = runtime.outputs.resolve_sources(
        ResolveSources(
            action_id=action["id"], spec=_source_spec(action),
            occurred_at=now),
        new_operation_key(), runtime.owned)
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(
            f"来源解析事务未完成（{outcome.kind.value}）:"
            f" {outcome.error}")
    return bool(outcome.value.fixed)


def _source_spec(action: Mapping) -> SourceSpec:
    """从原请求重建来源引用；不可解释的输入按状态库错误拒绝。"""
    from camctl.contracts.values import parse_object_id

    fields = parse_exact_json(action["input_fields_json"])
    params = fields.get("params") if isinstance(fields, dict) else None
    source = params.get("source") if isinstance(params, dict) else None
    if not isinstance(source, dict):
        raise ConsistencyError("取回动作缺少来源引用")
    values = dict(source)
    for key in ("action_instance_id", "plan_instance_id"):
        if values.get(key) is not None:
            try:
                values[key] = parse_object_id(values[key])
            except (TypeError, ValueError) as error:
                raise ConsistencyError(
                    f"取回来源引用不可解释: {error}") from error
    try:
        return SourceSpec(**values)
    except (TypeError, ValueError) as error:
        raise ConsistencyError(f"取回来源引用不可解释: {error}") from error


def _fix_pending_selections(
        runtime: ObtainRuntime, action: Mapping, now: int) -> None:
    """对每个待固定的选择计算快照并保存；来源未完成保持等待。"""
    from camctl.outputs.definitions import read_selection_request

    connection = runtime.owned.connection
    with closing(connection.execute(
        "SELECT s.id, d.depends_on_action_id FROM obtain_source_selections s"
        " JOIN action_dependencies d ON d.id = s.dependency_id"
        " WHERE d.action_id = ? AND s.status = ? ORDER BY s.id",
        (action["id"], int(_SELECTION_STATUS.PENDING)),
    )) as cursor:
        pending = cursor.fetchall()
    if not pending:
        return
    mode, requested = read_selection_request(
        parse_exact_json(action["execution_spec_json"]),
        parse_exact_json(action["input_fields_json"]))
    for selection_id, member_id in pending:
        facts = load_selection_facts(
            connection, int(member_id),
            requested_output_ids=requested)
        snapshot = select_outputs(
            SourceResolution(
                state=ResolutionState.FIXED,
                member_action_ids=(int(member_id),),
                source_plan_id=action["resolved_source_plan_id"]),
            facts, mode, requested)
        if not isinstance(snapshot, SelectionSnapshot) or not snapshot.is_fixed:
            # 来源尚未终态或产物处理未完成：本来源等待，其余来源同轮继续。
            continue
        outcome = runtime.outputs.fix_selection(
            FixSelection(
                selection_id=int(selection_id), snapshot=snapshot,
                occurred_at=now),
            new_operation_key(), runtime.owned)
        if outcome.kind is not DbOutcomeKind.COMPLETED:
            raise ConsistencyError(
                f"选择固定事务未完成（{outcome.kind.value}）:"
                f" {outcome.error}")


def _qualify_selected_items(
        runtime: ObtainRuntime, action: Mapping, now: int) -> None:
    """为没有交付的已选中条目申请读取资格建档；等待不阻塞其他条目。"""
    connection = runtime.owned.connection
    with closing(connection.execute(
        "SELECT i.id, i.output_id FROM obtain_items i"
        " JOIN obtain_source_selections s ON s.id = i.selection_id"
        " JOIN action_dependencies d ON d.id = s.dependency_id"
        " WHERE d.action_id = ? AND i.status = ? AND i.delivery_id IS NULL"
        " ORDER BY i.id",
        (action["id"], int(_ITEM_STATUS.SELECTED)),
    )) as cursor:
        pending = cursor.fetchall()
    for item_id, output_id in pending:
        outcome = runtime.outputs.grant_file(
            _delivery_candidate(runtime, action, int(item_id),
                                int(output_id), now),
            new_operation_key(), runtime.owned)
        if outcome.kind is not DbOutcomeKind.COMPLETED:
            raise ConsistencyError(
                f"读取资格事务未完成（{outcome.kind.value}）:"
                f" {outcome.error}")


def _delivery_candidate(
        runtime: ObtainRuntime, action: Mapping, item_id: int,
        output_id: int, now: int,
) -> FileCandidate:
    """从产物事实构造交付建档候选；来源与可读名称由产物行推导。"""
    connection = runtime.owned.connection
    with closing(connection.execute(
        "SELECT source_action_id, device_file_id, intermediate_file_id"
        " FROM outputs WHERE id = ?", (output_id,),
    )) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError(f"取回条目的产物缺失: {output_id}")
    source_action_id, device_file_id, intermediate_file_id = row
    with closing(connection.execute(
        "SELECT a.name, p.name FROM actions a"
        " JOIN plans p ON p.id = a.plan_id WHERE a.id = ?",
        (source_action_id,),
    )) as cursor:
        names = cursor.fetchone()
    if names is None:
        raise ConsistencyError(f"产物来源动作缺失: {source_action_id}")
    source_action_name, plan_name = names
    if device_file_id is not None:
        return _device_source_candidate(
            runtime, action, item_id, output_id, device_file_id, now,
            plan_name, source_action_name)
    if intermediate_file_id is None:
        raise ConsistencyError(f"产物缺少读取来源: {output_id}")
    return _host_source_candidate(
        runtime, action, item_id, output_id, intermediate_file_id, now,
        plan_name, source_action_name)


def _device_source_candidate(
        runtime: ObtainRuntime, action: Mapping, item_id: int,
        output_id: int, device_file_id: int, now: int,
        plan_name: str, source_action_name: str,
) -> FileCandidate:
    connection = runtime.owned.connection
    with closing(connection.execute(
        "SELECT f.original_name, a.device_id, a.driver_id FROM device_files f"
        " JOIN actions a ON a.id = f.observer_action_id WHERE f.id = ?",
        (device_file_id,),
    )) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError(f"产物源文件缺失: {device_file_id}")
    original_name, device_id, driver_id = row
    binding = DeviceBinding(device_id=device_id, driver_id=driver_id)
    binding_result = runtime.binding_check(binding) if runtime.binding_check is not None else None
    assembly = runtime.devices.get(device_id)
    failed_binding = binding_result is not None and binding_result.status in (
        BindingStatus.DEVICE_MISSING, BindingStatus.DRIVER_MISMATCH)
    if assembly is None and not failed_binding:
        raise ConsistencyError(
            f"读取设备未装配读取协作者: {device_id!r}")
    stem = _original_stem(original_name)
    extension = _extension_of(original_name)
    return FileCandidate(
        action_id=action["id"], item_id=item_id, processing_id=None,
        output_id=output_id, source_device_file_id=device_file_id,
        target_extension="part",
        delivery_extension=extension,
        delivery_display_name=(
            f"{stem}-{plan_name}-{source_action_name}.{extension}"),
        config=None if failed_binding else OperationConfig(
            max_attempts=assembly.max_read_attempts,
            timeout_s=assembly.read_idle_timeout_s,
            retry_interval_s=assembly.retry_interval_s),
        occurred_at=now, binding_result=binding_result)


def _host_source_candidate(
        runtime: ObtainRuntime, action: Mapping, item_id: int,
        output_id: int, intermediate_file_id: int, now: int,
        plan_name: str, source_action_name: str,
) -> FileCandidate:
    """主机源产物建档：不采用设备配置，不占用相机读取机会。"""
    with closing(runtime.owned.connection.execute(
        "SELECT relative_path FROM intermediate_files WHERE id = ?",
        (intermediate_file_id,),
    )) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError(f"中间文件缺失: {intermediate_file_id}")
    extension = _extension_of(row[0])
    return FileCandidate(
        action_id=action["id"], item_id=item_id, processing_id=None,
        output_id=output_id, source_device_file_id=None,
        source_intermediate_file_id=intermediate_file_id,
        target_extension="part",
        delivery_extension=extension,
        delivery_display_name=(
            f"repaired-{plan_name}-{source_action_name}.{extension}"),
        config=None, occurred_at=now)


def _original_stem(original_name: Any) -> str:
    """设备原始名称的文件名部分；无名称时按未知来源表达。"""
    if not isinstance(original_name, str) or not original_name:
        return "output"
    name = original_name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem, _, _ = name.rpartition(".")
    return stem or name


def _extension_of(name: Any) -> str:
    """与文件类型对应的交付扩展名；不合规后缀按未知类型处理。"""
    if isinstance(name, str) and "." in name:
        suffix = name.rsplit(".", 1)[1]
        if _SAFE_EXTENSION.fullmatch(suffix):
            return suffix
    return _UNKNOWN_EXTENSION


async def _publish_and_finish(
        runtime: ObtainRuntime, action_id: int, now: int) -> None:
    """汇总确定后发布成功交付并保存动作终态；未确定保持等待。"""
    from camctl.outputs.obtain_summary import decide_obtain_finish

    connection = runtime.owned.connection
    decision = decide_obtain_finish(load_obtain_facts(connection, action_id))
    if not decision.may_publish:
        return
    for delivery_id in _prepared_deliveries(connection, action_id):
        await _publish_delivery(runtime, int(delivery_id), now)
    outcome = runtime.outputs.finish_obtain(
        FinishObtain(action_id=action_id, occurred_at=now),
        new_operation_key(), runtime.owned)
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(
            f"取回完成事务未完成（{outcome.kind.value}）:"
            f" {outcome.error}")


def _prepared_deliveries(connection, action_id: int) -> list[int]:
    """汇总确定后仍待发布的成功交付。"""
    with closing(connection.execute(
        "SELECT d.id FROM deliveries d"
        " JOIN obtain_items i ON i.delivery_id = d.id"
        " JOIN obtain_source_selections s ON s.id = i.selection_id"
        " JOIN action_dependencies dep ON dep.id = s.dependency_id"
        " WHERE dep.action_id = ? AND d.status = ? ORDER BY d.id",
        (action_id, int(_DELIVERY_STATUS.PREPARED)),
    )) as cursor:
        return [int(row[0]) for row in cursor.fetchall()]


async def _publish_delivery(
        runtime: ObtainRuntime, delivery_id: int, now: int) -> None:
    """发布一份准备完成的交付；终局失败按已保存事实保留。"""
    result = await publish_delivery(delivery_id, DeliveryContext(
        repository=runtime.outputs, owned=runtime.owned,
        directories=runtime.directories, occurred_at=now, executor=runtime.file_executor))
    if result.phase not in (
            DeliveryPhase.PUBLISHED, DeliveryPhase.FAILED_FINAL,
            DeliveryPhase.NOT_ACTIVE):
        raise ConsistencyError(
            f"交付发布处于未决状态: delivery={delivery_id}"
            f" phase={result.phase.value}")


# ---- 读取推进（统一设备工作计划消费） ----


async def _advance_reads(runtime: ObtainRuntime, now: int) -> None:
    """按设备工作计划推进读取；未声明并行的设备在拍摄工作时让路。

    新授予的候选与已持有机会的在途读取共同推进：在途读取继续占
    用机会直至副本完成或终局失败，重试等待的间隔判定在逐拷贝推
    进内完成。驱动声明拍摄与读取并行的设备上，到时拍摄的同轮派
    发不阻塞读取推进。
    """
    connection = runtime.owned.connection
    if runtime.binding_check is not None:
        with closing(connection.execute(
            "SELECT c.id,c.source_device_file_id,a.device_id,a.driver_id"
            " FROM file_copies c JOIN deliveries d ON d.id=c.delivery_id"
            " JOIN actions owner ON owner.id=d.action_id"
            " JOIN device_files f ON f.id=c.source_device_file_id"
            " JOIN actions a ON a.id=f.observer_action_id"
            " WHERE owner.status=2 AND owner.cancel_requested=0 AND d.status IN (1,2)"
            " AND c.verification_state NOT IN (3,5) ORDER BY c.id",
        )) as cursor:
            candidates = cursor.fetchall()
        for copy_id, source_id, device_id, driver_id in candidates:
            result = runtime.binding_check(DeviceBinding(device_id, driver_id))
            if result.status in (BindingStatus.DEVICE_MISSING, BindingStatus.DRIVER_MISMATCH):
                from camctl.outputs.read_attempts import PendingReadBusiness, held_binding_read_result

                business = PendingReadBusiness(runtime.outputs.fail_read_binding,
                    FailReadBinding(copy_id, source_id, result, now), new_operation_key())
                pending = held_binding_read_result(runtime, copy_id, result, business)
                if pending is not None:
                    _save_read_result_and_settle(runtime, pending)
                    continue
                if not recover_unavailable_read(runtime, copy_id, DeviceBinding(device_id, driver_id), now):
                    continue
                from camctl.outputs.read_attempts import save_read_business

                failed = save_read_business(runtime, copy_id, "read_binding_failure", FailReadBinding(
                    copy_id, source_id, result, now), runtime.outputs.fail_read_binding)
                if failed.kind is not DbOutcomeKind.COMPLETED:
                    raise ConsistencyError(f"读取绑定失败事务未完成（{failed.kind.value}）: {failed.error}")
                runtime.retry_gate.cleared(f"read/{copy_id}")
    parallel = frozenset(
        device_id for device_id, assembly in runtime.devices.items()
        if assembly.capture_read_parallel)
    for work in plan_device_work(
            connection, now, capture_read_parallel=parallel):
        if work.yield_reads or (
                work.dispatch_captures and not work.capture_read_parallel):
            continue
        assembly = runtime.devices.get(work.device_id)
        if assembly is None:
            continue
        for copy_id in (*work.grant_reads, *work.resume_reads):
            await _advance_read(runtime, assembly, copy_id, now)


def _read_result_pending(connection, copy_id: int) -> bool:
    """原读取存在尚未可靠结束的尝试；不把合法未完当作仓储错误。"""
    with closing(connection.execute(
        "SELECT 1 FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
        " WHERE r.copy_id=? AND (a.status=1 OR a.result_json IS NULL) LIMIT 1", (copy_id,),
    )) as cursor:
        return cursor.fetchone() is not None


async def _advance_read(
        runtime: ObtainRuntime, assembly: DeviceReadAssembly,
        copy_id: int, now: int) -> None:
    """在读取机会与尝试预算内推进一份交付拷贝到准备完成。"""
    connection = runtime.owned.connection
    facts = _read_facts(connection, copy_id)
    if facts is None:
        return
    if facts["verification_state"] in _VERIFICATION_DONE:
        # 已完成校验的副本只余发布；读取责任不再占用设备。
        return
    with closing(connection.execute(
        "SELECT a.device_id,a.driver_id FROM device_files f"
        " JOIN actions a ON a.id=f.observer_action_id WHERE f.id=?",
        (facts["source_device_file_id"],),
    )) as cursor:
        saved_binding = cursor.fetchone()
    if saved_binding is None:
        raise ConsistencyError(f"读取源文件的原绑定缺失: {facts['source_device_file_id']}")
    if assembly.binding != DeviceBinding(*saved_binding):
        # 本次设备标识相同仍可能更换驱动；实际意图和调用只采用原绑定。
        return
    if _read_wait_remaining(runtime, copy_id, assembly.retry_interval_s, assembly.max_read_attempts) \
            is not None:
        return
    slot = runtime.outputs.grant_read_slot(
        SlotRequest(copy_id=copy_id, occurred_at=now),
        new_operation_key(), runtime.owned)
    if slot.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(
            f"读取机会事务未完成（{slot.kind.value}）: {slot.error}")
    if slot.value.outcome in (SlotOutcome.WAIT, SlotOutcome.FINISHED):
        return
    ticket = _begin_read_attempt(runtime, assembly, facts, now)
    if ticket is None:
        return
    await _run_read(runtime, assembly, facts, ticket, now)


def _read_facts(connection, copy_id: int, *, include_terminal: bool = False) -> dict | None:
    """一份交付拷贝的读取推进事实。"""
    with closing(connection.execute(
        "SELECT c.id, c.round, c.verification_state,"
        " c.source_device_file_id, c.source_intermediate_file_id,"
        " d.id AS delivery_id, d.status AS delivery_status,"
        " d.action_id, i.id AS item_id, i.output_id"
        " FROM file_copies c"
        " JOIN deliveries d ON d.id = c.delivery_id"
        " JOIN obtain_items i ON i.delivery_id = d.id"
        " WHERE c.id = ?", (copy_id,),
    )) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError(f"读取拷贝记录缺失: {copy_id}")
    columns = (
        "id", "round", "verification_state", "source_device_file_id",
        "source_intermediate_file_id", "delivery_id", "delivery_status",
        "action_id", "item_id", "output_id")
    facts = dict(zip(columns, row))
    if not include_terminal and facts["delivery_status"] in (
            int(_DELIVERY_STATUS.FAILED), int(_DELIVERY_STATUS.CANCELED),
            int(_DELIVERY_STATUS.WITHDRAWN)):
        return None
    return facts


def _read_wait_remaining(
        runtime: ObtainRuntime, copy_id: int, interval_s: Decimal, maximum: int | None = None,
) -> Decimal | None:
    """读取责任的重试等待剩余秒数；可开始下一次尝试时为空。"""
    responsibility = f"read/{copy_id}"
    with closing(runtime.owned.connection.execute(
        "SELECT attempts_used, retry_wait_required, max_attempts_used"
        " FROM operation_runs WHERE responsibility_key = ?",
        (responsibility,),
    )) as cursor:
        row = cursor.fetchone()
    if row is None:
        return None
    return runtime.retry_gate.remaining(
        responsibility,
        attempts_used=int(row[0]),
        retry_wait_required=int(row[1]) == 1,
        max_attempts_used=int(row[2]) if maximum is None else maximum,
        interval_s=interval_s,
        now_ns=runtime.monotonic_ns())


def _begin_read_attempt(
        runtime: ObtainRuntime, assembly: DeviceReadAssembly,
        facts: Mapping, now: int):
    """提交读取意图；预算耗尽把交付结束为终局失败并交还机会。"""
    intent = AttemptIntent(
        operation="read",
        action_id=facts["action_id"],
        kind=OperationKind.READ_FILE,
        target=AttemptTarget(copy_id=facts["id"]),
        query_purpose=None,
        config=AttemptConfig(
            max_attempts=assembly.max_read_attempts,
            timeout_s=assembly.read_idle_timeout_s,
            retry_interval_s=assembly.retry_interval_s),
        occurred_at=now,
        copy_round=facts["round"])
    ticket, reason = acquire_read_attempt(runtime, intent)
    if ticket is not None:
        return ticket
    if reason == "budget_exhausted":
        _fail_read_delivery(runtime, facts, now)
    return None


def _fail_read_delivery(
        runtime: ObtainRuntime, facts: Mapping, now: int, *, checksum_mismatch=False) -> None:
    """读取预算耗尽：交付终局失败后释放读取机会。"""
    from camctl.outputs.read_attempts import save_read_business

    save_read_business(runtime, facts["id"], "fail_delivery",
        FailReadDelivery(delivery_id=facts["delivery_id"], occurred_at=now,
                         checksum_mismatch=checksum_mismatch), runtime.outputs.fail_read_delivery)


def _settle_failed_reads(runtime: ObtainRuntime) -> None:
    """新会话只消费原终态依据，不用本次上限重开已失败读取。"""
    rows = runtime.owned.connection.execute(
        "SELECT c.id,r.error_json FROM file_copies c JOIN operation_runs r ON r.copy_id=c.id"
        " JOIN deliveries d ON d.id=c.delivery_id JOIN actions a ON a.id=d.action_id"
        " WHERE r.kind=3 AND r.status=4 AND d.status IN (1,2)"
        " AND a.status=2 AND a.cancel_requested=0 ORDER BY c.id").fetchall()
    for copy_id, error_json in rows:
        if error_json is None:
            raise ConsistencyError("原失败读取缺少可靠终局错误")
        error = parse_exact_json(error_json)
        if error["code"] not in ("read_attempts_exhausted", "checksum_mismatch"):
            raise ConsistencyError("原失败读取的所属错误不属于读取耗尽或摘要不一致")
        facts = _read_facts(runtime.owned.connection, copy_id)
        if facts is None:
            raise ConsistencyError("原失败读取的未终态交付不可解释")
        _fail_read_delivery(runtime, facts, runtime.occurred_at(), checksum_mismatch=error["code"] == "checksum_mismatch")


async def _run_read(
        runtime: ObtainRuntime, assembly: DeviceReadAssembly,
        facts: Mapping, ticket, now: int) -> None:
    """一次读取尝试：摘要能力固定、会话拷贝与完整性收尾。"""
    from camctl.outputs.read_attempts import run_owned_read

    async def body():
        await _run_read_and_save(runtime, assembly, facts, ticket, now)

    await run_owned_read(runtime, facts["id"], body)


async def _run_read_and_save(runtime, assembly, facts, ticket, now):
    from camctl.outputs.read_attempts import hold_read_end

    device_file_id = facts["source_device_file_id"]
    _ensure_checksum_support(runtime, assembly, device_file_id, now)
    digest = (
        assembly.digest_for(device_file_id)
        if assembly.digest_supported and assembly.digest_for is not None
        else None)
    step = await _copy_delivery(
        runtime,
        DriverReadSessions(runtime.owned, assembly.driver, ticket=ticket,
                           idle_timeout_s=assembly.read_idle_timeout_s),
        facts, digest, max_recopies=assembly.max_recopies,
        read_end=(runtime.pending_read_ends[facts["id"]].end if facts["id"] in runtime.pending_read_ends else None),
        on_read_end=lambda end, complete: hold_read_end(runtime, ticket, end, complete,
            resume=lambda current, held: _resume_local_delivery_end(current, held, runtime, assembly), evidence=assembly.evidence),
        on_read_start=lambda: runtime.pending_read_ends.pop(facts["id"], None))
    _finish_read_attempt(runtime, assembly, facts, ticket, step)


async def _resume_local_delivery_end(current, held, original_runtime, assembly):
    """只核实原完整副本；原会话的设备结果不再调用源端口。"""
    from camctl.outputs.read_attempts import run_owned_read

    runtime = replace(original_runtime, owned=current.owned)
    copy_id = int(held.ticket.target_id)
    facts = _read_facts(runtime.owned.connection, copy_id, include_terminal=True)
    if facts is None:
        raise ConsistencyError("原本地副本校验缺少所属交付")

    async def complete_and_save():
        copies = RecordingInputCopies(runtime.owned, BoundDirectories(staging=runtime.directories.staging),
            runtime.occurred_at, max_recopies=assembly.max_recopies, file_executor=runtime.file_executor)
        step = replace(await _complete_copy(copies, copy_id, None), read_end=held.end,
                       stop_requested=True, content_complete=True)
        _finish_read_attempt(runtime, assembly, facts, held.ticket, step)

    await run_owned_read(runtime, copy_id, complete_and_save)


async def _copy_delivery(
        runtime: ObtainRuntime, sessions, facts: Mapping, digest, *, max_recopies: int = 1,
        read_end: ReadEnd | None = None, on_read_end=None, on_read_start=None,
) -> InputStep:
    """复用资格、续传、分段与完整性事务推进交付拷贝。"""
    copies: RecordingCopies = RecordingInputCopies(
        runtime.owned, BoundDirectories(staging=runtime.directories.staging),
        runtime.occurred_at, max_recopies=max_recopies, file_executor=runtime.file_executor)
    state = copies.copy_state(facts["id"])
    if (state.verification_state in _VERIFICATION_DONE
            and state.target_sha256 is not None):
        return InputStep(InputPhase.INPUT_READY, copy_id=facts["id"])
    try:
        prepared = await copies.prepare(facts["id"])
    except (CopyPreparationError, ConsistencyError) as error:
        return InputStep(InputPhase.PREPARE_FAILED, copy_id=facts["id"],
                         error=error)
    if prepared.decision.outcome is ResumeOutcome.VERIFY and (read_end is not None or on_read_end is None):
        return replace(await _complete_copy(copies, facts["id"], digest), read_end=read_end,
                       stop_requested=read_end is not None, content_complete=read_end is not None)
    try:
        if on_read_start is not None:
            on_read_start()
        session = await sessions.open_session(
            _source_file_id(facts), state.source_size if prepared.decision.outcome is ResumeOutcome.VERIFY
            else prepared.decision.offset)
    except Exception as error:
        # 打开会话也是一次源读取调用：按读取失败保存本次尝试。
        return InputStep(InputPhase.SEGMENT_FAILED, copy_id=facts["id"],
                         error=error, source_failed=True)
    result = None
    try:
        if prepared.decision.outcome is ResumeOutcome.VERIFY:
            from camctl.capture.input_copy import confirm_read_at_full_offset

            await confirm_read_at_full_offset(session)
        while True:
            step = await copies.transfer(
                facts["id"], session, runtime.segment_size)
            if step.plan.outcome is SegmentOutcome.ALL_COMMITTED:
                break
            if step.saved is None:
                raise ConsistencyError(
                    "段推进未携带保存事实，拷贝端口契约不一致")
            if step.saved.disposition is SegmentSaveDisposition.SKIPPED:
                result = InputStep(
                    InputPhase.OWNER_SKIPPED, copy_id=facts["id"],
                    reason=step.saved.reason)
                break
    except (CopySegmentError, ConsistencyError) as error:
        result = InputStep(InputPhase.SEGMENT_FAILED, copy_id=facts["id"],
                         error=error, source_failed=isinstance(error, CopySegmentError) and error.source_failed)
    finally:
        from camctl.capture.input_copy import require_read_stopped

        session.request_stop()
        end = await session.wait_stopped()
        require_read_stopped(end)
        if on_read_end is not None:
            on_read_end(end, session.position() == state.source_size)
    if result is not None:
        return replace(result, read_end=end, stop_requested=True, content_complete=session.position() == state.source_size)
    if end.error is not None:
        return InputStep(InputPhase.SEGMENT_FAILED, copy_id=facts["id"], read_end=end,
                         error=RuntimeError(end.error), source_failed=end.error != "stopped", stop_requested=True,
                         content_complete=session.position() == state.source_size)
    return replace(await _complete_copy(copies, facts["id"], digest), read_end=end, stop_requested=True, content_complete=True)


async def _complete_copy(
        copies: RecordingCopies, copy_id: int, digest) -> InputStep:
    """完整性收尾：准备完成或登记新一轮重拷。"""
    try:
        completion = await copies.complete(copy_id, digest)
    except RecopyExhausted as error:
        return InputStep(InputPhase.CHECKSUM_EXHAUSTED, copy_id=copy_id, error=error)
    except (CopyCompletionError, ConsistencyError) as error:
        return InputStep(InputPhase.COMPLETION_FAILED, copy_id=copy_id,
                         error=error)
    if completion.phase is CompletionPhase.PREPARED:
        return InputStep(InputPhase.INPUT_READY, copy_id=copy_id)
    return InputStep(InputPhase.RECOPY_PENDING, copy_id=copy_id)


def _finish_read_attempt(
        runtime: ObtainRuntime, assembly: DeviceReadAssembly,
        facts: Mapping, ticket, step: InputStep) -> None:
    """保存尝试结果；预算耗尽按流程失败终局交付并交还机会。"""
    if step.phase is InputPhase.RECOPY_PENDING:
        # 新轮次已经提交且本连接已关闭；读取尝试和原次数继续承担责任。
        runtime.continuing_read_tickets[facts["id"]] = ticket
        runtime.retry_gate.cleared(f"read/{facts['id']}")
        return
    from camctl.outputs.read_attempts import stopped_read_result

    stopped = stopped_read_result(runtime, ticket, step, assembly.evidence, runtime.occurred_at())
    if stopped is not None:
        _save_read_result_and_settle(runtime, stopped, facts)
        return
    from camctl.outputs.read_attempts import canceled_complete_read_result

    completed_cancel = canceled_complete_read_result(runtime, ticket, assembly.evidence)
    if completed_cancel is not None:
        _save_read_result_and_settle(runtime, completed_cancel, facts)
        return
    checksum_failed = step.phase is InputPhase.CHECKSUM_EXHAUSTED
    if step.phase is not InputPhase.INPUT_READY and not step.source_failed and not checksum_failed:
        raise ConsistencyError(f"读取的本地或保存前提未完成，保留原尝试: {step.phase.value}: {step.error}")
    succeeded = step.phase is InputPhase.INPUT_READY or checksum_failed
    # 原 ticket 的连续 attempt_no 已在取得／恢复时核实为原累计次数。
    # 实际返回后先持有结果，不能为读取预算再访问数据库而丢失结果。
    exhausted = ticket.attempt_id >= assembly.max_read_attempts
    outcome = CallOutcome(
        status=(AttemptStatus.SUCCEEDED if succeeded
                else AttemptStatus.FAILED),
        error=None if succeeded else ErrorValue(
            code="device_error", stage="read"),
        effect=EffectState.UNKNOWN,
        settlement=Settlement(
            basis=SettlementBasis.OBSERVED,
            evidence=EvidenceValue(
                type="read_returned", version=1, data={})),
        observations=())
    from camctl.outputs.read_attempts import hold_read_result

    pending = hold_read_result(runtime, AttemptFinish(
            ticket=ticket,
            outcome=validate_outcome(ticket, outcome, assembly.evidence),
            occurred_at=runtime.occurred_at(),
            retry_wait=(not succeeded) and not exhausted,
            run_finish=(RunFinish(RunOutcome.FAILED, ErrorValue(
                "checksum_mismatch", "source_read", {
                    "max_recopies": step.error.max_recopies,
                    "recopies_used": step.error.recopies_used})) if checksum_failed else _run_finish(succeeded, exhausted))))
    _save_read_result_and_settle(runtime, pending, facts)


def _retry_read_results(runtime: ObtainRuntime) -> None:
    from camctl.outputs.read_attempts import PendingStoppedRead, forget_read_result, save_read_result

    for pending in tuple(runtime.pending_read_results.values()):
        if not isinstance(pending, PendingStoppedRead) and pending.finish.outcome.outcome.settlement.basis is SettlementBasis.ASSUMED:
            save_read_result(runtime, pending)
            forget_read_result(runtime, pending)
            continue
        _save_read_result_and_settle(runtime, pending)


def _save_read_result_and_settle(runtime: ObtainRuntime, pending, facts=None) -> None:
    from camctl.outputs.read_attempts import forget_read_result, save_read_result

    pending = save_read_result(runtime, pending)
    if pending is None:
        return
    if pending.business is not None:
        from camctl.outputs.read_attempts import save_prepared_read_business

        copy_id = int(pending.finish.ticket.target_id)
        forget_read_result(runtime, pending)
        save_prepared_read_business(runtime, copy_id, "read_binding_failure", pending.business)
        runtime.retry_gate.cleared(f"read/{copy_id}")
        return
    if facts is None:
        facts = _read_facts(runtime.owned.connection, int(pending.finish.ticket.target_id), include_terminal=True)
    if facts is None:
        raise ConsistencyError("原读取结果的所属交付已终态，尚未核实业务收场")
    finish = pending.finish
    if facts["delivery_status"] == int(_DELIVERY_STATUS.FAILED):
        stored = runtime.owned.connection.execute(
            "SELECT d.error_json,i.source_dependency,c.slot_device_id FROM deliveries d"
            " JOIN obtain_items i ON i.delivery_id=d.id JOIN file_copies c ON c.delivery_id=d.id"
            " WHERE d.id=? AND c.id=?", (facts["delivery_id"], facts["id"])).fetchone()
        if (finish.run_finish is None or finish.run_finish.status is not RunOutcome.FAILED
                or stored is None or stored[0] is None
                or parse_exact_json(stored[0])["code"] != finish.run_finish.error.code
                or stored[1:] != (0, None)):
            raise ConsistencyError("原读取业务失败的终态与保护收场未闭合")
        forget_read_result(runtime, pending)
        return
    if finish.run_finish is not None and finish.run_finish.status is RunOutcome.CANCELED:
        runtime.retry_gate.cleared(f"read/{int(finish.ticket.target_id)}")
        forget_read_result(runtime, pending)
        return
    succeeded = finish.outcome.outcome.status is AttemptStatus.SUCCEEDED
    exhausted = finish.run_finish is not None and finish.run_finish.status is RunOutcome.FAILED
    if exhausted and finish.run_finish.error.code == "checksum_mismatch":
        _fail_read_delivery(runtime, facts, runtime.occurred_at(), checksum_mismatch=True)
        forget_read_result(runtime, pending)
        return
    if succeeded:
        released = runtime.outputs.release_read_slot(
            SlotRequest(copy_id=facts["id"],
                        occurred_at=runtime.occurred_at()),
            new_operation_key(), runtime.owned)
        if released.kind is not DbOutcomeKind.COMPLETED:
            raise ConsistencyError(f"原读取机会释放未可靠保存: {released.error}")
        runtime.retry_gate.cleared(f"read/{facts['id']}")
        forget_read_result(runtime, pending)
        return
    if exhausted:
        _fail_read_delivery(runtime, facts, runtime.occurred_at())
        forget_read_result(runtime, pending)
        return
    runtime.retry_gate.established(
        f"read/{facts['id']}", runtime.monotonic_ns())
    forget_read_result(runtime, pending)


def _run_finish(succeeded: bool, exhausted: bool) -> RunFinish | None:
    """读取流程的本次收场决定：副本就绪即成功，耗尽即失败。"""
    if succeeded:
        return RunFinish(status=RunOutcome.SUCCEEDED)
    if exhausted:
        return RunFinish(
            status=RunOutcome.FAILED,
            error=ErrorValue(code="read_attempts_exhausted",
                             stage="source_read"))
    return None


def _attempts_exhausted(runtime: ObtainRuntime, copy_id: int) -> bool:
    """读取流程的累计尝试是否已达到预算上限。"""
    with closing(runtime.owned.connection.execute(
        "SELECT attempts_used, max_attempts_used FROM operation_runs"
        " WHERE responsibility_key = ?", (f"read/{copy_id}",),
    )) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError(f"读取流程缺失: read/{copy_id}")
    return int(row[0]) >= int(row[1])


# ---- 主机源读取推进（不占相机机会） ----


def _source_file_id(facts: Mapping) -> int:
    """拷贝的读取源身份：设备源与主机源互斥，取实际存在的一方。"""
    if facts["source_device_file_id"] is not None:
        return int(facts["source_device_file_id"])
    if facts["source_intermediate_file_id"] is not None:
        return int(facts["source_intermediate_file_id"])
    raise ConsistencyError(f"拷贝缺少读取来源: {facts['id']}")


class LocalReadSessions:
    """主机源读取会话：按中间文件登记路径打开本地顺序读取。

    不经驱动、不占用相机读取机会；中间文件物理位于 staging 工作
    根下的登记相对路径（derived/ 等），读取身份是登记事实。
    """

    def __init__(self, owned: Any, directories: DeliveryDirectories) -> None:
        self._owned = owned
        self._directories = directories

    async def open_session(self, source_intermediate_file_id: int,
                           offset: int) -> "_LocalReadSession":
        with closing(self._owned.connection.execute(
            "SELECT relative_path FROM intermediate_files WHERE id = ?",
            (source_intermediate_file_id,),
        )) as cursor:
            row = cursor.fetchone()
        if row is None:
            raise ConsistencyError(
                f"中间文件缺失: {source_intermediate_file_id}")
        path = self._directories.staging / str(row[0])
        return _LocalReadSession(LocalSourceReader(path, offset))


class _LocalReadSession:
    """本地顺序读取的控制包装：关闭即停止，结束事实随后可得。"""

    def __init__(self, reader: LocalSourceReader) -> None:
        self._reader = reader

    def position(self) -> int:
        return self._reader.position()

    def read_chunk(self, limit: int) -> ReadChunk:
        return self._reader.read_chunk(limit)

    def poll_stopped(self) -> ReadEnd | None:
        return self._reader.poll_stopped()

    def request_stop(self) -> None:
        self._reader.close()

    async def wait_stopped(self) -> ReadEnd:
        self._reader.close()
        end = self._reader.poll_stopped()
        if end is None:
            raise ConsistencyError("本地读取关闭缺少结束事实")
        return end


async def _advance_local_reads(runtime: ObtainRuntime, now: int) -> None:
    """推进全部主机源交付拷贝：不依赖设备装配，逐份独立推进。"""
    connection = runtime.owned.connection
    with closing(connection.execute(
        "SELECT c.id FROM file_copies c"
        " JOIN deliveries d ON d.id = c.delivery_id"
        " JOIN actions a ON a.id = d.action_id"
        " WHERE c.source_device_file_id IS NULL"
        " AND c.verification_state NOT IN (?, ?)"
        " AND d.status NOT IN (?, ?, ?)"
        " AND a.status = 2 AND a.cancel_requested = 0"
        " ORDER BY c.id",
        (*_VERIFICATION_DONE, *_INACTIVE_DELIVERIES),
    )) as cursor:
        copy_ids = tuple(int(row[0]) for row in cursor.fetchall())
    sessions = LocalReadSessions(runtime.owned, runtime.directories)
    for copy_id in copy_ids:
        await _advance_local_read(runtime, sessions, copy_id, now)


async def _advance_local_read(
        runtime: ObtainRuntime, sessions: LocalReadSessions,
        copy_id: int, now: int) -> None:
    """在单次尝试预算内推进一份主机源交付拷贝到准备完成。"""
    connection = runtime.owned.connection
    facts = _read_facts(connection, copy_id)
    if facts is None:
        return
    if facts["verification_state"] in _VERIFICATION_DONE:
        # 已完成校验的副本只余发布；读取责任已完成。
        return
    if _read_wait_remaining(runtime, copy_id, None) is not None:
        return
    ticket = _begin_local_attempt(runtime, facts, now)
    if ticket is None:
        return
    step = await _copy_delivery(runtime, sessions, facts, None)
    _finish_local_attempt(runtime, facts, ticket, step)


def _begin_local_attempt(runtime: ObtainRuntime, facts: Mapping, now: int):
    """提交主机源读取意图；本地预算单次，耗尽即终局失败交付。

    预算与期限随授予拷贝时保存的本地配置（一次、无时限、无定时
    重试），不套用设备源默认值。
    """
    intent = AttemptIntent(
        operation="read",
        action_id=facts["action_id"],
        kind=OperationKind.READ_FILE,
        target=AttemptTarget(copy_id=facts["id"]),
        query_purpose=None,
        config=AttemptConfig(max_attempts=1, timeout_s=None,
                             retry_interval_s=None),
        occurred_at=now,
        copy_round=facts["round"])
    outcome = runtime.operations.begin_attempt(
        intent, new_operation_key(), runtime.owned)
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(
            f"读取意图事务未完成（{outcome.kind.value}）: {outcome.error}")
    if outcome.value.disposition is BeginDisposition.GRANTED:
        return outcome.value.ticket
    if outcome.value.reason == "budget_exhausted":
        _fail_local_delivery(runtime, facts, now)
    return None


def _fail_local_delivery(runtime: ObtainRuntime, facts: Mapping, now: int) -> None:
    """本地读取预算耗尽：交付终局失败；主机源没有相机机会可交还。"""
    failure = runtime.outputs.fail_read_delivery(
        FailReadDelivery(delivery_id=facts["delivery_id"], occurred_at=now),
        new_operation_key(), runtime.owned)
    if failure.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(
            f"读取耗尽失败事务未完成（{failure.kind.value}）:"
            f" {failure.error}")


def _finish_local_attempt(
        runtime: ObtainRuntime, facts: Mapping, ticket, step: InputStep) -> None:
    """保存主机源读取尝试结果；失败即预算耗尽并终局失败交付。"""
    succeeded = step.phase is InputPhase.INPUT_READY
    exhausted = _attempts_exhausted(runtime, facts["id"])
    outcome = CallOutcome(
        status=(AttemptStatus.SUCCEEDED if succeeded
                else AttemptStatus.FAILED),
        error=None if succeeded else ErrorValue(
            code="local_read_failed", stage="source_read",
            details={
                "phase": step.phase.value,
                "reason": (
                    str(step.error) if step.error is not None else None)}),
        effect=EffectState.UNKNOWN,
        settlement=Settlement(
            basis=SettlementBasis.OBSERVED,
            evidence=EvidenceValue(
                type="read_returned", version=1, data={})),
        observations=())
    finish = runtime.operations.finish_attempt(
        AttemptFinish(
            ticket=ticket,
            outcome=validate_outcome(ticket, outcome, _LOCAL_READ_EVIDENCE),
            occurred_at=runtime.occurred_at(),
            retry_wait=False,
            run_finish=_run_finish(succeeded, exhausted)),
        new_operation_key(), runtime.owned)
    if finish.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(
            f"读取尝试收尾事务未完成（{finish.kind.value}）:"
            f" {finish.error}")
    if succeeded:
        runtime.retry_gate.cleared(f"read/{facts['id']}")
        return
    _fail_local_delivery(runtime, facts, runtime.occurred_at())


def _ensure_checksum_support(
        runtime: ObtainRuntime, assembly: DeviceReadAssembly,
        device_file_id: int, now: int) -> None:
    """按绑定声明一次固定来源文件的摘要能力；已决定不重复声明。"""
    with closing(runtime.owned.connection.execute(
        "SELECT checksum_support FROM device_files WHERE id = ?",
        (device_file_id,),
    )) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError(f"设备文件不存在: {device_file_id}")
    if row[0] != 1:
        return
    outcome = CaptureRepository().save_file_checksum(
        FileChecksumSave(
            file_id=device_file_id,
            support=2 if assembly.digest_supported else 3,
            occurred_at=now),
        new_operation_key(), runtime.owned)
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(f"摘要能力声明未完成: {outcome.error}")

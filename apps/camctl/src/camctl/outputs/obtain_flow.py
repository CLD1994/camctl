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
from types import SimpleNamespace
from typing import Any, Callable, Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from camctl.outputs.read_attempts import ReadDispatchPrevented

from camctl.capture.files import FileChecksumSave
from camctl.capture.recovery import RecoveryBoundary, RecoveryDiagnostic
from camctl.capture.input_copy import (
    InputPhase, InputStep, RecordingCopies)
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
    OperationKind, RunFinish, RunOutcome, RetryWaitGate)
from camctl.operations.models import (
    AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue,
    Settlement, SettlementBasis)
from camctl.operations.validation import validate_outcome
from camctl.outputs.copy import (
    CompletionPhase, CopyCompletionError, CopyPreparationError,
    CopySegmentError, RecopyExhausted, ResumeOutcome, SegmentOutcome,
    SegmentSaveDisposition)
from camctl.outputs.dispatch import plan_device_work
from camctl.outputs.diagnostics import record_qualification, record_selection, record_source_resolution
from camctl.outputs.read_discovery import ReadDiscoveryOwner
from camctl.outputs.handoff import (
    DeliveryContext, DeliveryDirectories, DeliveryPhase, publish_delivery)
from camctl.outputs.qualification import (
    FileCandidate, OperationConfig)
from camctl.outputs.read_attempts import acquire_read_attempt, recover_unavailable_read, read_failure_details
from camctl.outputs.slots import SlotOutcome, SlotRequest
from camctl.outputs.sources import (
    ResolutionState, SelectionSnapshot, SourceResolution, SourceSpec,
    read_source_spec, select_outputs)
from camctl.persistence.models import DbOutcomeKind, db_failure, db_outcome_for
from camctl.persistence.business import settle_required
from camctl.logging_runtime.diagnostics import DiagnosticState, record_error
from camctl.logging_runtime.events import measure_stage, record_event
from camctl.outputs.database_reads import read_one
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import (
    FailReadBinding, FailReadDelivery, FinishObtain, FixSelection,
    OutputsRepository, ReadSourceFallback,
    ResolveSources, StartObtainAction, ObtainStartDisposition, ObtainFinishDisposition, load_obtain_facts,
    load_selection_facts, read_source_fallback_key)

__all__ = [
    "DeviceReadAssembly",
    "ObtainRuntime",
    "advance_obtain",
    "settle_obtain_diagnostics",
]

_ACTION_STATUS = enum_for("actions.status")
_RESOLUTION_STATE = enum_for("actions.source_resolution_state")
_SELECTION_STATUS = enum_for("obtain_source_selections.status")
_ITEM_STATUS = enum_for("obtain_items.status")
_DELIVERY_STATUS = enum_for("deliveries.status")
_VERIFICATION = enum_for("file_copies.verification_state")
_OUTPUT_KIND = enum_for("outputs.kind")
_RUN_STATUS = enum_for("operation_runs.status")
_ATTEMPT_STATUS = enum_for("operation_attempts.status")

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
    query_driver: Any = None
    query_config: AttemptConfig | None = None


@dataclass
class ObtainRuntime:
    """一次取回推进轮次的协作者集合。"""

    database: Any
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
    pending_segment_selections: dict = field(default_factory=dict)
    pending_segment_queries: dict = field(default_factory=dict)
    discovery: ReadDiscoveryOwner = field(default_factory=ReadDiscoveryOwner)
    availability: Any = None
    on_read_retry_deadline: Callable[[int, int], None] | None = None


@dataclass
class PendingSegmentSelection:
    """原选择申请与其固定范围诊断回执的本次交接位置。"""

    request: Any
    key: Any
    saved: Any = None
    cursor: Any = None
    diagnostics_done: bool = False
    diagnostic: DiagnosticState = field(default_factory=DiagnosticState, compare=False, repr=False)


async def _consume_selection_diagnostics(runtime, pending):
    from camctl.persistence.repositories.segment_selections import read_original_selection_error_page

    if pending.diagnostics_done:
        return
    ref = pending.saved.diagnostic_ref
    while True:
        try:
            page = await runtime.database.read(
                lambda owned: read_original_selection_error_page(ref, pending.cursor, owned),
                description="原分段选择的固定范围错误回执")
        except Exception as error:
            record_error(error, stage="selection_diagnostic_receipt",
                context={"action_id": ref.action_id, "selection_id": ref.selection_id,
                         "operation_key": ref.operation_key}, state=pending.diagnostic)
            pending.diagnostics_done = True
            return
        if page.items:
            record_error({"failures": page.items}, stage="output_selection",
                context={"action_id": ref.action_id, "selection_id": ref.selection_id,
                         "operation_key": ref.operation_key}, state=pending.diagnostic)
        if page.exhausted:
            pending.diagnostics_done = True
            return
        pending.cursor = page.next_cursor
        pending.diagnostic = DiagnosticState()


async def settle_obtain_diagnostics(runtime):
    """必要业务停止后，只交接已提交选择的原诊断回执，不启动或重试业务。"""
    for identity, pending in tuple(runtime.pending_segment_selections.items()):
        if pending.saved is None or pending.diagnostics_done:
            continue
        await _consume_selection_diagnostics(runtime, pending)
        if pending.diagnostics_done:
            del runtime.pending_segment_selections[identity]


async def advance_obtain(runtime: ObtainRuntime) -> None:
    """推进一个轮次的取回执行链；各步按已保存事实幂等。"""
    from camctl.outputs.read_attempts import retry_read_business, retry_read_ends

    await retry_read_business(runtime)
    await _retry_read_results(runtime)
    await retry_read_ends(runtime)
    now = runtime.occurred_at()
    await runtime.discovery.begin_round(runtime.database, now)
    await _settle_canceled_read_ends(runtime, now)
    await _settle_ready_read_ends(runtime, now)
    await _settle_failed_reads(runtime)
    await _start_due(runtime, now)
    for (action_id,) in (await runtime.discovery.rows(runtime.database, "actions", now,
            "SELECT id FROM actions WHERE status=2 AND cancel_requested=0 AND type=4"
            " AND source_resolution_state=1")).items:
        action = await runtime.database.read(lambda owned: _action_facts(owned.connection, action_id))
        await _resolve_sources(runtime, action, now)
    await _fix_pending_selections(runtime, now)
    from camctl.outputs.segment_query import advance_segment_queries

    await advance_segment_queries(runtime, now)
    await _qualify_selected_items(runtime, now)
    publish_remaining = runtime.discovery.batch_size
    for (action_id,) in (await runtime.discovery.rows(runtime.database, "finish", now,
            "SELECT id FROM actions WHERE status=2 AND cancel_requested=0 AND type=4")).items:
        publish_remaining -= await _publish_and_finish(runtime, action_id, now, publish_limit=publish_remaining)
    await _advance_reads(runtime, now)
    await _advance_local_reads(runtime, now)


async def _save_repository(runtime, save, request, key):
    return await runtime.database.write(
        lambda owned: save(request, key, owned), key=key)


# ---- 动作生命周期 ----


async def _start_due(runtime: ObtainRuntime, now: int) -> None:
    """开始到时的待执行取回动作；未到时与已取消动作不进入。"""
    page = (await runtime.discovery.rows(runtime.database, "start", now,
        "SELECT id FROM actions WHERE status=1 AND cancel_requested=0 AND type=4"
        " AND scheduled_at<=?", lambda fixed: (fixed,)))
    due = (row[0] for row in page.items)
    for action_id in due:
        outcome = await _save_repository(runtime, runtime.outputs.start_obtain_action, StartObtainAction(action_id=action_id, occurred_at=now), new_operation_key())
        if outcome.kind is not DbOutcomeKind.COMPLETED:
            raise db_failure(ConsistencyError,
                f"取回开始事务未完成（{outcome.kind.value}）:"
                f" {outcome.error}", outcome)
        if outcome.value.disposition is ObtainStartDisposition.SAVED:
            record_event("obtain.action", event="started", action_id=action_id,
                         database_id=runtime.database.metadata.instance_id)


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


async def _resolve_sources(
        runtime: ObtainRuntime, action: Mapping, now: int) -> bool:
    """执行期来源解析；失败保存动作终态后返回假。"""
    outcome = await _save_repository(runtime, runtime.outputs.resolve_sources, ResolveSources(
            action_id=action["id"], spec=_source_spec(action),
            occurred_at=now), new_operation_key())
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise db_failure(ConsistencyError,
            f"来源解析事务未完成（{outcome.kind.value}）:"
            f" {outcome.error}", outcome)
    record_source_resolution(outcome.value, action["id"])
    return bool(outcome.value.fixed)


def _source_spec(action: Mapping) -> SourceSpec:
    """从原请求重建来源引用；不可解释的输入按状态库错误拒绝。"""
    return read_source_spec(action["input_fields_json"])


async def _fix_pending_selections(
        runtime: ObtainRuntime, now: int) -> None:
    """先恢复原保存，再按来源直接事实推进选择；来源未完成保持等待。"""
    from camctl.outputs.definitions import read_excluded_output_ids, read_selection_request

    from camctl.persistence.repositories.segment_selections import (
        FixSegmentSelection,
        fix_segment_selection,
        is_segmented_result_policy,
    )
    from camctl.persistence.repositories.outputs import _processing_completed_facts

    async def save_selection(identity, pending):
        async def save_and_apply():
            if pending.saved is None:
                try:
                    receipt = await _save_repository(runtime, fix_segment_selection, pending.request, pending.key)
                except BaseException as error:
                    actual = db_outcome_for(error)
                    if actual is not None and actual.kind is DbOutcomeKind.COMPLETED:
                        pending.saved = actual.value
                    raise
                if receipt.kind is not DbOutcomeKind.COMPLETED:
                    if receipt.kind is DbOutcomeKind.ROLLED_BACK:
                        del runtime.pending_segment_selections[identity]
                    raise db_failure(ConsistencyError, f"原分段选择尚未可靠固定: {receipt.error}", receipt)
                pending.saved = receipt.value
            if pending.saved is not None:
                await _consume_selection_diagnostics(runtime, pending)
            del runtime.pending_segment_selections[identity]
        await settle_required(save_and_apply())

    for identity, pending in tuple(runtime.pending_segment_selections.items()):
        await save_selection(identity, pending)
    page = await runtime.discovery.rows(runtime.database, "selections", now,
        "SELECT s.id,d.depends_on_action_id,d.action_id,source.status,"
        "json_extract(source.execution_spec_json,'$.result_policy'),"
        "p.id,p.check_state,p.repair_state,p.discard_state FROM obtain_source_selections s"
        " JOIN action_dependencies d ON d.id=s.dependency_id"
        " JOIN actions a ON a.id=d.action_id"
        " LEFT JOIN actions source ON source.id=d.depends_on_action_id"
        " LEFT JOIN recording_processing p ON p.action_id=source.id"
        " WHERE a.status=2 AND a.cancel_requested=0 AND a.source_resolution_state=2"
        " AND s.status=?", (int(_SELECTION_STATUS.PENDING),), identity="s.id")
    for (selection_id, member_id, action_id, source_status, result_policy,
         processing_id, check_state, repair_state, discard_state) in page.items:
        if is_segmented_result_policy(result_policy):
            if source_status in (int(_ACTION_STATUS.PENDING), int(_ACTION_STATUS.RUNNING)):
                continue
            processing = None if processing_id is None else {
                "check_state": check_state, "repair_state": repair_state, "discard_state": discard_state}
            if not _processing_completed_facts(processing):
                continue
            request, key = FixSegmentSelection(int(selection_id), now), new_operation_key()
            pending = PendingSegmentSelection(request, key)
            runtime.pending_segment_selections[int(selection_id)] = pending
            await save_selection(int(selection_id), pending)
            continue
        def inputs(owned):
            connection = owned.connection
            action = _action_facts(connection, action_id)
            mode, requested = read_selection_request(
                parse_exact_json(action["execution_spec_json"]),
                parse_exact_json(action["input_fields_json"]))
            facts = load_selection_facts(connection, int(member_id),
                requested_output_ids=requested)
            return action, mode, requested, facts
        inputs_value = await runtime.database.read(inputs)
        action, mode, requested, facts = inputs_value
        input_fields = parse_exact_json(action["input_fields_json"])
        snapshot = select_outputs(
            SourceResolution(state=ResolutionState.FIXED,
                member_action_ids=(int(member_id),),
                source_plan_id=action["resolved_source_plan_id"]),
            facts, mode, requested,
            excluded_output_ids=read_excluded_output_ids(input_fields))
        if not isinstance(snapshot, SelectionSnapshot) or not snapshot.is_fixed:
            continue
        record_selection(snapshot, action_id=action_id, selection_id=selection_id,
                         source_action_id=int(member_id))
        outcome = await _save_repository(runtime, runtime.outputs.fix_selection,
            FixSelection(int(selection_id), snapshot, now), new_operation_key())
        if outcome.kind is not DbOutcomeKind.COMPLETED:
            raise db_failure(ConsistencyError,
                f"选择固定事务未完成（{outcome.kind.value}）: {outcome.error}", outcome)


async def _qualify_selected_items(
        runtime: ObtainRuntime, now: int) -> None:
    """为没有交付的已选中条目申请读取资格建档；等待不阻塞其他条目。"""
    page = (await runtime.discovery.rows(runtime.database, "items", now,
        "SELECT i.id,i.output_id,d.action_id,i.selection_id FROM obtain_items i"
        " JOIN obtain_source_selections s ON s.id=i.selection_id"
        " JOIN action_dependencies d ON d.id=s.dependency_id"
        " JOIN actions a ON a.id=d.action_id"
        " WHERE a.status=2 AND a.cancel_requested=0 AND a.source_resolution_state=2"
        " AND i.status=? AND i.delivery_id IS NULL", (int(_ITEM_STATUS.SELECTED),), identity="i.id"))
    for item_id, output_id, action_id, selection_id in page.items:
        action = await runtime.database.read(lambda owned: _action_facts(owned.connection, action_id))
        candidate = await _delivery_candidate(
            runtime, action, int(item_id), int(output_id), int(selection_id), now)
        if candidate is None:
            continue
        outcome = await _save_repository(runtime, runtime.outputs.grant_file, candidate, new_operation_key())
        if outcome.kind is not DbOutcomeKind.COMPLETED:
            raise db_failure(ConsistencyError,
                f"读取资格事务未完成（{outcome.kind.value}）:"
                f" {outcome.error}", outcome)
        diagnostic = None
        if candidate.binding_result is not None and getattr(runtime, "availability", None) is not None:
            from camctl.contracts.workflow_errors import item_error_id
            from camctl.devices.bindings import binding_failure_details
            if (outcome.value.error_code == item_error_id("obtain_items", "device_binding_unavailable")
                    and outcome.value.error_details == binding_failure_details(candidate.binding_result)):
                diagnostic = runtime.availability.binding_diagnostic(action_id, item_id, has_read_copy=False)
        record_qualification(outcome.value, candidate, state=diagnostic)


async def _delivery_candidate(
        runtime: ObtainRuntime, action: Mapping, item_id: int,
        output_id: int, selection_id: int, now: int,
) -> FileCandidate:
    """从产物事实构造交付建档候选；来源与可读名称由产物行推导。"""
    def inputs(owned):
        connection = owned.connection
        with closing(connection.execute(
            "SELECT o.source_action_id,o.device_file_id,o.intermediate_file_id,"
            "o.original_name,a.name,p.name FROM outputs o"
            " JOIN actions a ON a.id=o.source_action_id"
            " JOIN plans p ON p.id=a.plan_id WHERE o.id=?", (output_id,))) as cursor:
            row = cursor.fetchone()
        if row is None:
            raise ConsistencyError(f"取回条目的产物或来源动作缺失: {output_id}")
        source_action_id, device_file_id = row[:2]
        bound = None
        photo_bound = None
        if device_file_id is not None:
            from camctl.persistence.repositories.segment_selections import is_segmented_source
            from camctl.outputs.segment_copy_reuse import read_bound_check_copy
            if is_segmented_source(connection, int(source_action_id)):
                bound = read_bound_check_copy(
                    source_action_id=int(source_action_id),
                    source_device_file_id=int(device_file_id),
                    obtain_action_id=int(action["id"]), selection_id=selection_id, owned=owned)
            else:
                from camctl.outputs.photo_cache_reuse import read_bound_photo_cache
                photo_bound = read_bound_photo_cache(output_id=output_id,
                    obtain_action_id=int(action["id"]), selection_id=selection_id, owned=owned)
        return row, bound, photo_bound
    row, bound, photo_bound = await runtime.database.read(inputs)
    (source_action_id, device_file_id, intermediate_file_id, original_name,
     source_action_name, plan_name) = row

    if device_file_id is not None:
        if photo_bound is not None and runtime.file_executor is not None:
            stem = _original_stem(original_name)
            extension = _extension_of(original_name)
            return FileCandidate(action_id=action["id"], item_id=item_id, processing_id=None,
                output_id=output_id, source_device_file_id=None,
                source_intermediate_file_id=photo_bound.intermediate_file_id,
                target_extension="part", delivery_extension=extension,
                delivery_display_name=f"{stem}-{plan_name}-{source_action_name}.{extension}",
                config=await _inspection_copy_read_config(runtime, int(device_file_id)), occurred_at=now,
                reuse_photo_page_event_id=photo_bound.result_page_event_id)
        from camctl.outputs.segment_copy_reuse import check_copy_is_locally_usable
        if (bound is not None and runtime.file_executor is not None
                and await check_copy_is_locally_usable(
                    bound.copy, runtime.directories.staging,
                    runtime.file_executor)):
            stem = _original_stem(original_name)
            extension = _extension_of(original_name)
            return FileCandidate(
                action_id=action["id"], item_id=item_id,
                processing_id=None, output_id=output_id,
                source_device_file_id=None,
                source_intermediate_file_id=bound.copy.target_file_id,
                target_extension="part", delivery_extension=extension,
                delivery_display_name=(
                    f"{stem}-{plan_name}-{source_action_name}.{extension}"),
                config=await _inspection_copy_read_config(runtime, int(device_file_id)),
                occurred_at=now,
                reuse_inspection_id=bound.inspection.inspection_id)
        return await _device_source_candidate(
            runtime, action, item_id, output_id, device_file_id, now,
            plan_name, source_action_name)
    if intermediate_file_id is None:
        raise ConsistencyError(f"产物缺少读取来源: {output_id}")
    return await _host_source_candidate(
        runtime, action, item_id, output_id, intermediate_file_id, now,
        plan_name, source_action_name)


async def _device_source_candidate(
        runtime: ObtainRuntime, action: Mapping, item_id: int,
        output_id: int, device_file_id: int, now: int,
        plan_name: str, source_action_name: str,
) -> FileCandidate:
    row = await read_one(runtime.database,
        "SELECT f.original_name,a.device_id,a.driver_id FROM device_files f"
        " JOIN actions a ON a.id=f.observer_action_id WHERE f.id=?", (device_file_id,))
    if row is None:
        raise ConsistencyError(f"产物源文件缺失: {device_file_id}")
    original_name, device_id, driver_id = row
    binding = DeviceBinding(device_id=device_id, driver_id=driver_id)
    from camctl.outputs.physical_binding import saved_source_binding
    binding_result = await saved_source_binding(runtime, device_file_id, action["id"], device_id, driver_id)
    if binding_result is None:
        binding_result = runtime.binding_check(binding) if runtime.binding_check is not None else None
    assembly = runtime.devices.get(device_id)
    failed_binding = binding_result is not None and binding_result.status in (
        BindingStatus.DEVICE_MISSING, BindingStatus.DRIVER_MISMATCH,
        BindingStatus.PHYSICAL_IDENTITY_MISMATCH, BindingStatus.PHYSICAL_IDENTITY_CONFLICT)
    if assembly is None and not failed_binding:
        raise ConsistencyError(
            f"读取设备未装配读取协作者: {device_id!r}")
    if binding_result is not None and binding_result.status is BindingStatus.MATCHED:
        from camctl.outputs.physical_binding import source_physical_binding
        physical = await source_physical_binding(runtime, device_file_id, device_id, driver_id)
        if physical is not None:
            binding_result = physical
    if (binding_result is not None and binding_result.status in (
            BindingStatus.DEVICE_MISSING, BindingStatus.DRIVER_MISMATCH,
            BindingStatus.PHYSICAL_IDENTITY_MISMATCH, BindingStatus.PHYSICAL_IDENTITY_CONFLICT)
            and getattr(runtime, "availability", None) is not None):
        from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID as HASSELBLAD_DRIVER_ID
        if driver_id == HASSELBLAD_DRIVER_ID:
            from types import SimpleNamespace
            candidate = SimpleNamespace(action_id=action["id"], copy_id=item_id,
                device_id=device_id, source_file_id=device_file_id, has_read_copy=False)
            if not await runtime.availability.finish_read_binding(runtime, candidate, binding_result):
                return None
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


async def _inspection_copy_read_config(
        runtime: ObtainRuntime, device_file_id: int) -> OperationConfig | None:
    """把可用原设备装配配置冻结到检查副本的正式取回流程。"""
    row = await read_one(runtime.database, "SELECT a.device_id FROM device_files f"
        " JOIN actions a ON a.id=f.observer_action_id WHERE f.id=?", (device_file_id,))
    if row is None:
        raise ConsistencyError(f"检查副本的原设备文件缺失: {device_file_id}")
    assembly = runtime.devices.get(row[0])
    if assembly is None:
        # 本地检查副本仍能独立完成取回；缺少设备读取装配时，不凭空
        # 扩大其已保存为单次的本地读取预算。
        return None
    return OperationConfig(
        max_attempts=assembly.max_read_attempts,
        timeout_s=assembly.read_idle_timeout_s,
        retry_interval_s=assembly.retry_interval_s)


async def _host_source_candidate(
        runtime: ObtainRuntime, action: Mapping, item_id: int,
        output_id: int, intermediate_file_id: int, now: int,
        plan_name: str, source_action_name: str,
) -> FileCandidate:
    """主机源产物建档：不采用设备配置，不占用相机读取机会。"""
    row = await read_one(runtime.database, "SELECT f.relative_path, o.original_name, o.kind FROM intermediate_files f"
        " JOIN outputs o ON o.intermediate_file_id = f.id WHERE f.id = ? AND o.id = ?", (intermediate_file_id, output_id))
    if row is None:
        raise ConsistencyError(f"中间文件缺失: {intermediate_file_id}")
    extension = _extension_of(row[0])
    stem = "repaired" if row[2] == int(_OUTPUT_KIND.REPAIRED) else _original_stem(row[1])
    return FileCandidate(
        action_id=action["id"], item_id=item_id, processing_id=None,
        output_id=output_id, source_device_file_id=None,
        source_intermediate_file_id=intermediate_file_id,
        target_extension="part",
        delivery_extension=extension,
        delivery_display_name=(
            f"{stem}-{plan_name}-{source_action_name}.{extension}"),
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
        runtime: ObtainRuntime, action_id: int, now: int, *, publish_limit: int) -> int:
    """汇总确定后发布成功交付并保存动作终态；未确定保持等待。"""
    from camctl.outputs.obtain_summary import decide_obtain_finish

    def inputs(owned):
        facts = load_obtain_facts(owned.connection, action_id)
        deliveries = (_prepared_deliveries(owned.connection, action_id, limit=publish_limit, read_succeeded=True)
                      if publish_limit else ())
        return facts, deliveries
    facts, deliveries = await runtime.database.read(inputs)
    decision = decide_obtain_finish(facts)
    if not decision.may_publish:
        return 0
    for delivery_id in deliveries:
        from camctl.persistence.repositories.outputs import delivery_read_succeeded
        if not await runtime.database.read(lambda owned: delivery_read_succeeded(owned.connection, delivery_id)):
            continue
        await _publish_delivery(runtime, int(delivery_id), now)
    if await runtime.database.read(lambda owned: _prepared_deliveries(owned.connection, action_id, limit=1)):
        return len(deliveries)
    outcome = await _save_repository(runtime, runtime.outputs.finish_obtain, FinishObtain(action_id=action_id, occurred_at=now), new_operation_key())
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise db_failure(ConsistencyError,
            f"取回完成事务未完成（{outcome.kind.value}）:"
            f" {outcome.error}", outcome)
    if outcome.value.disposition is ObtainFinishDisposition.SAVED:
        record_event("obtain.action", action_id=action_id,
                     database_id=runtime.database.metadata.instance_id,
                     action_status=outcome.value.action_status,
                     prepared=outcome.value.prepared, failed=outcome.value.failed)
    return len(deliveries)


def _prepared_deliveries(connection, action_id: int, *, limit: int, read_succeeded: bool = False) -> list[int]:
    """汇总确定后仍待发布的成功交付。"""
    with closing(connection.execute(
        "SELECT d.id FROM deliveries d"
        " JOIN obtain_items i ON i.delivery_id = d.id"
        " JOIN obtain_source_selections s ON s.id = i.selection_id"
        " JOIN action_dependencies dep ON dep.id = s.dependency_id"
        " WHERE dep.action_id = ? AND d.status = ?"
        + (" AND EXISTS (SELECT 1 FROM file_copies c JOIN operation_runs r ON r.copy_id=c.id"
           " WHERE c.delivery_id=d.id AND r.kind=3 AND r.status=3)" if read_succeeded else "")
        + " ORDER BY d.id LIMIT ?",
        (action_id, int(_DELIVERY_STATUS.PREPARED), limit),
    )) as cursor:
        return [int(row[0]) for row in cursor.fetchall()]


async def _publish_delivery(
        runtime: ObtainRuntime, delivery_id: int, now: int) -> None:
    """发布一份准备完成的交付；终局失败按已保存事实保留。"""
    with measure_stage("obtain.publication", delivery_id=delivery_id,
                       database_id=runtime.database.metadata.instance_id):
        result = await publish_delivery(delivery_id, DeliveryContext(
            repository=runtime.outputs, database=runtime.database,
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
    page = (await runtime.discovery.reads(runtime.database, "reads", now))
    binding_blocked = set()
    local_end = set()
    digest_end = set()
    for item in page.items:
        if getattr(item, "read_end_event_id", None) is None:
            continue
        assembly = runtime.devices.get(item.device_id)
        if assembly is not None and assembly.binding != DeviceBinding(item.device_id, item.driver_id):
            evidence = _original_read_evidence(runtime, item.device_id, item.driver_id)
            if evidence is None:
                continue
            assembly = replace(assembly, evidence=evidence)
        if assembly is None:
            if runtime.binding_check is None or runtime.recovery_evidence_for is None:
                continue
            binding = DeviceBinding(item.device_id, item.driver_id)
            binding_result = runtime.binding_check(binding)
            if binding_result.status is not BindingStatus.DEVICE_MISSING:
                continue
            evidence = runtime.recovery_evidence_for(binding, "read")
            if evidence is None:
                continue
            assembly = SimpleNamespace(evidence=evidence, max_recopies=None,
                                       max_read_attempts=None, binding_failure=binding_result)
        from camctl.persistence.repositories.read_end import load_read_end
        from camctl.outputs.read_attempts import running_read_ticket
        def current_end(owned):
            if running_read_ticket(owned, item.copy_id) is None:
                return None
            return load_read_end(item.copy_id, owned)
        saved = await runtime.database.read(current_end)
        if saved is None:
            local_end.add(item.copy_id)
            continue
        ticket, end, occurred_at, slot = saved
        from camctl.outputs.read_attempts import HeldReadEnd
        held = runtime.pending_read_ends.get(item.copy_id)
        if held is not None and (held.ticket != ticket or held.end != end):
            raise ConsistencyError("已保存读取结束与原拥有者持有的返回不一致")
        if held is None:
            held = HeldReadEnd(ticket, end, occurred_at,
                resume=lambda current, value, current_assembly=assembly:
                    _resume_local_delivery_end(current, value, runtime, current_assembly),
                evidence=assembly.evidence)
            runtime.pending_read_ends[item.copy_id] = held
        if slot is not None:
            digest_end.add(item.copy_id)
            continue
        await _resume_local_delivery_end(runtime, held, runtime, assembly)
        local_end.add(item.copy_id)
    if runtime.binding_check is not None or getattr(runtime, "availability", None) is not None:
        candidates = (item for item in page.items if item.copy_id not in local_end and item.copy_id not in digest_end)
        for item in candidates:
            copy_id, source_id, device_id, driver_id = item.copy_id, item.source_file_id, item.device_id, item.driver_id
            from camctl.outputs.physical_binding import saved_source_binding
            result = await saved_source_binding(runtime, source_id, item.action_id, device_id, driver_id)
            if result is None:
                if runtime.binding_check is None:
                    continue
                result = runtime.binding_check(DeviceBinding(device_id, driver_id))
            if result.status is BindingStatus.MATCHED:
                from camctl.outputs.physical_binding import source_physical_binding
                physical = await source_physical_binding(runtime, source_id, device_id, driver_id)
                if (physical is not None and (copy_id not in runtime.pending_read_ends
                        or physical.status is BindingStatus.PHYSICAL_IDENTITY_MISMATCH)):
                    result = physical
            if result.status in (BindingStatus.DEVICE_MISSING, BindingStatus.DRIVER_MISMATCH,
                                 BindingStatus.PHYSICAL_IDENTITY_MISMATCH, BindingStatus.PHYSICAL_IDENTITY_CONFLICT):
                binding_blocked.add(copy_id)
                from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID as HASSELBLAD_DRIVER_ID
                if (driver_id == HASSELBLAD_DRIVER_ID and getattr(runtime, "availability", None) is not None
                        and not await runtime.availability.finish_read_binding(runtime, item, result)):
                    continue
                from camctl.outputs.read_attempts import PendingReadBusiness, held_binding_read_result
                from camctl.outputs.handoff import FailureSaveDisposition

                diagnostic = (None if getattr(runtime, "availability", None) is None else
                              runtime.availability.binding_diagnostic(item.action_id, copy_id))
                business = PendingReadBusiness(runtime.outputs.fail_read_binding,
                    FailReadBinding(copy_id, source_id, result, now), new_operation_key(),
                    lambda value, identity=copy_id: runtime.retry_gate.cleared(f"read/{identity}")
                        if value.disposition is not FailureSaveDisposition.RETIRED else None,
                    diagnostic=diagnostic or DiagnosticState())
                pending = await held_binding_read_result(runtime, copy_id, result, business)
                if pending is not None:
                    await _save_read_result_and_settle(runtime, pending)
                    continue
                if not await recover_unavailable_read(runtime, copy_id, DeviceBinding(device_id, driver_id), now):
                    continue
                from camctl.outputs.read_attempts import save_read_business

                failed = await save_read_business(runtime, copy_id, "read_binding_failure", FailReadBinding(
                    copy_id, source_id, result, now), runtime.outputs.fail_read_binding, diagnostic=diagnostic)
                if failed.kind is not DbOutcomeKind.COMPLETED:
                    raise db_failure(ConsistencyError,
                        f"读取绑定失败事务未完成（{failed.kind.value}）: {failed.error}", failed)
                if failed.value.disposition is not FailureSaveDisposition.RETIRED:
                    runtime.retry_gate.cleared(f"read/{copy_id}")
    # 摘要仍属于持槽读取的原责任，应先收场，不能被新拍摄计划跳过。
    for item in page.items:
        if item.copy_id not in digest_end or item.copy_id in binding_blocked:
            continue
        assembly = runtime.devices.get(item.device_id)
        if assembly is not None:
            await _advance_read(runtime, assembly, item.copy_id, now)
    deferred_waits = {}
    exhausted_reads = set()
    # 读取机会保留原顺序，间隔完成与其他设备占用分别处理。
    for item in page.items:
        if item.copy_id in binding_blocked or item.copy_id in local_end or item.copy_id in digest_end:
            continue
        assembly = runtime.devices.get(item.device_id)
        if assembly is None:
            continue
        if (not item.held and (item.attempts_used == 0
                or (item.retry_wait_required == 0 and item.attempts_used < assembly.max_read_attempts))):
            continue
        run = await read_one(runtime.database,
            "SELECT r.id,a.id,r.attempts_used,r.retry_wait_required,a.status,a.result_json,a.result_event_id FROM operation_runs r"
            " LEFT JOIN operation_attempts a ON a.run_id=r.id AND a.attempt_no=r.attempts_used"
            " WHERE r.copy_id=? AND r.kind=3", (item.copy_id,))
        if run is None:
            raise ConsistencyError("已持有机会的读取缺少原流程")
        run_id, attempt_id, attempts_used, waiting, attempt_status, result, result_event_id = run
        if (run_id, attempts_used) in runtime.pending_read_results:
            continue
        if (attempt_id is None or attempt_status == int(_ATTEMPT_STATUS.RUNNING)
                or result is None or result_event_id is None):
            continue
        if attempt_status == int(_ATTEMPT_STATUS.SUCCEEDED):
            continue
        if attempts_used >= assembly.max_read_attempts:
            exhausted_reads.add(item.copy_id)
        if not item.held:
            if waiting != 1 and attempts_used < assembly.max_read_attempts:
                continue
            from camctl.persistence.repositories.retry_wait_reads import RetryWaitReads
            def eligible_read(owned):
                reads = RetryWaitReads(owned.connection)
                if attempt_status == int(_ATTEMPT_STATUS.UNKNOWN) and attempts_used >= assembly.max_read_attempts:
                    return reads.read_eligible(run_id, runtime.occurred_at(), allow_unheld=True)
                return reads.read_eligible(run_id, runtime.occurred_at())
            eligible = await runtime.database.read(eligible_read)
            if not eligible:
                continue
        from camctl.outputs.read_attempts import save_read_business
        if attempts_used >= assembly.max_read_attempts:
            facts = await runtime.database.read(lambda owned: _read_facts(owned.connection, item.copy_id))
            if facts is None:
                continue
            intent = _read_intent(assembly, facts, runtime.occurred_at())
            result = await save_read_business(runtime, item.copy_id, "exhaust_read", (intent, attempt_id),
                lambda request, key, owned: runtime.operations.exhaust_blocked_read(*request, key, owned))
            if result.value.reason == "budget_exhausted":
                runtime.retry_gate.cleared(f"read/{item.copy_id}")
                await _fail_read_delivery(runtime, facts, runtime.occurred_at())
            continue
        if waiting != 1 or await _read_wait_remaining(
                runtime, item.copy_id, assembly.retry_interval_s, assembly.max_read_attempts) is not None:
            continue
        deferred_waits[item.copy_id] = (run_id, attempt_id)
    candidates = tuple(item for item in page.items
                       if item.copy_id not in binding_blocked and item.copy_id not in exhausted_reads
                       and item.copy_id not in local_end and item.copy_id not in digest_end)
    if getattr(runtime, "availability", None) is not None:
        from camctl.operations.availability import AvailabilityProgress
        ready = []
        for candidate in candidates:
            if await runtime.availability.read(runtime, candidate) is AvailabilityProgress.READY:
                ready.append(candidate)
        candidates = tuple(ready)
    parallel = frozenset(
        device_id for device_id, assembly in runtime.devices.items()
        if assembly.capture_read_parallel)
    selections = ({item.device_id: runtime.availability.capture_selection(item.device_id, now)
                   for item in candidates if item.device_id is not None}
                  if runtime.availability is not None else None)
    work_items = await runtime.database.read(lambda owned: plan_device_work(
        owned.connection, now, capture_read_parallel=parallel,
        candidates=candidates, capture_selections=selections))
    selected = {copy_id for work in work_items
                if not work.yield_reads and not (work.dispatch_captures and not work.capture_read_parallel)
                for copy_id in (*work.grant_reads, *work.resume_reads)}
    for copy_id, wait in deferred_waits.items():
        if copy_id not in selected:
            await _complete_read_wait(runtime, copy_id, *wait)
    for work in work_items:
        if work.yield_reads or (
                work.dispatch_captures and not work.capture_read_parallel):
            continue
        assembly = runtime.devices.get(work.device_id)
        if assembly is None:
            continue
        for copy_id in (*work.grant_reads, *work.resume_reads):
            started = await _advance_read(runtime, assembly, copy_id, now)
            if not started and copy_id in deferred_waits:
                await _complete_read_wait(runtime, copy_id, *deferred_waits[copy_id])


async def _complete_read_wait(runtime, copy_id, run_id, attempt_id):
    from camctl.outputs.read_attempts import save_read_business

    def cleared(value):
        if value:
            runtime.retry_gate.cleared(f"read/{copy_id}")

    await save_read_business(runtime, copy_id, "complete_retry_wait",
        (run_id, attempt_id, runtime.occurred_at()),
        lambda request, key, owned: runtime.operations.complete_read_retry_wait(*request, key, owned),
        on_completed=cleared)


async def _advance_read(
        runtime: ObtainRuntime, assembly: DeviceReadAssembly,
        copy_id: int, now: int) -> bool:
    """推进一份拷贝；返回是否取得读取尝试。"""
    def inputs(owned):
        facts = _read_facts(owned.connection, copy_id)
        if facts is None or facts["verification_state"] in _VERIFICATION_DONE:
            return facts, None
        with closing(owned.connection.execute(
            "SELECT a.device_id,a.driver_id FROM device_files f"
            " JOIN actions a ON a.id=f.observer_action_id WHERE f.id=?",
            (facts["source_device_file_id"],))) as cursor:
            return facts, cursor.fetchone()
    facts, saved_binding = await runtime.database.read(inputs)
    if facts is None or facts["verification_state"] in _VERIFICATION_DONE:
        return False
    if saved_binding is None:
        raise ConsistencyError(f"读取源文件的原绑定缺失: {facts['source_device_file_id']}")
    if assembly.binding != DeviceBinding(*saved_binding):
        # 本次设备标识相同仍可能更换驱动；实际意图和调用只采用原绑定。
        return False
    from camctl.persistence.repositories.read_end import load_read_end
    saved = await runtime.database.read(lambda owned: load_read_end(copy_id, owned))
    if saved is not None:
        ticket, end, occurred_at, _ = saved
        from camctl.outputs.read_attempts import HeldReadEnd
        runtime.pending_read_ends[copy_id] = HeldReadEnd(ticket, end, occurred_at,
            resume=lambda current, held: _resume_local_delivery_end(current, held, runtime, assembly),
            evidence=assembly.evidence)
        await _run_read(runtime, assembly, facts, ticket, now)
        return True
    if await _read_wait_remaining(runtime, copy_id, assembly.retry_interval_s, assembly.max_read_attempts) \
            is not None:
        return False
    current = runtime.occurred_at()
    selection = (None if runtime.availability is None else
                 runtime.availability.capture_selection(assembly.binding.device_id, current))
    slot = await _save_repository(runtime, runtime.outputs.grant_read_slot,
        SlotRequest(copy_id=copy_id, occurred_at=current, capture_selection=selection), new_operation_key())
    if slot.kind is not DbOutcomeKind.COMPLETED:
        raise db_failure(ConsistencyError,
            f"读取机会事务未完成（{slot.kind.value}）: {slot.error}", slot)
    if slot.value.outcome in (SlotOutcome.WAIT, SlotOutcome.FINISHED):
        return False
    ticket = await _begin_read_attempt(runtime, assembly, facts, now)
    if ticket is None:
        return False
    await _run_read(runtime, assembly, facts, ticket, now)
    return True


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


async def _read_wait_remaining(
        runtime: ObtainRuntime, copy_id: int, interval_s: Decimal, maximum: int | None = None,
) -> Decimal | None:
    """读取责任的重试等待剩余秒数；可开始下一次尝试时为空。"""
    responsibility = f"read/{copy_id}"
    row = await read_one(runtime.database, "SELECT attempts_used, retry_wait_required, max_attempts_used"
        " FROM operation_runs WHERE responsibility_key = ?", (responsibility,))
    if row is None:
        return None
    now_ns = runtime.monotonic_ns()
    remaining = runtime.retry_gate.remaining(
        responsibility,
        attempts_used=int(row[0]),
        retry_wait_required=int(row[1]) == 1,
        max_attempts_used=int(row[2]) if maximum is None else maximum,
        interval_s=interval_s,
        now_ns=now_ns)
    if remaining is not None and runtime.on_read_retry_deadline is not None:
        from decimal import ROUND_CEILING

        target_ns = now_ns + int((remaining * 1_000_000_000).to_integral_value(rounding=ROUND_CEILING))
        runtime.on_read_retry_deadline(target_ns, now_ns)
    return remaining


async def _begin_read_attempt(
        runtime: ObtainRuntime, assembly: DeviceReadAssembly,
        facts: Mapping, now: int):
    """提交读取意图；预算耗尽把交付结束为终局失败并交还机会。"""
    current = runtime.occurred_at()
    selection = (None if runtime.availability is None else
                 runtime.availability.capture_selection(assembly.binding.device_id, current))
    intent = _read_intent(assembly, facts, current, selection)
    ticket, reason = await acquire_read_attempt(runtime, intent)
    if ticket is not None:
        return ticket
    if reason == "budget_exhausted":
        await _fail_read_delivery(runtime, facts, now)
    return None


def _read_intent(assembly, facts, now, capture_selection=None):
    return AttemptIntent(
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
        copy_round=facts["round"], capture_selection=capture_selection)


async def _fail_read_delivery(
        runtime: ObtainRuntime, facts: Mapping, now: int, *,
        checksum_mismatch=False, source_verification_failed=False,
        on_completed=None, business_saves=None, retry=False) -> None:
    """交付读取终局失败后释放读取机会及原源文件保护。"""
    from camctl.outputs.read_attempts import save_read_business

    await save_read_business(runtime, facts["id"], "fail_delivery",
        FailReadDelivery(delivery_id=facts["delivery_id"], occurred_at=now,
                         checksum_mismatch=checksum_mismatch,
                         source_verification_failed=source_verification_failed),
        runtime.outputs.fail_read_delivery, on_completed=on_completed,
        business_saves=business_saves, retry=retry)


def _original_read_evidence(runtime, device_id, driver_id):
    """结果契约属于源文件的原驱动，不能由同名设备的新驱动替代。"""
    binding = DeviceBinding(device_id, driver_id)
    assembly = runtime.devices.get(device_id)
    if assembly is not None and assembly.binding == binding:
        return assembly.evidence
    if runtime.recovery_evidence_for is None:
        return None
    return runtime.recovery_evidence_for(binding, "read")


async def _settle_ready_read_ends(runtime: ObtainRuntime, now: int) -> None:
    """已校验交付仍有原读取时，先按原 End 补存读取结果。"""
    from camctl.outputs.read_attempts import HeldReadEnd, unfinished_read_end

    page = await runtime.discovery.rows(runtime.database, "ready_read_ends", now,
        "SELECT c.id,c.source_device_file_id,ob.device_id,ob.driver_id"
        " FROM file_copies c JOIN deliveries d ON d.id=c.delivery_id"
        " JOIN actions a ON a.id=d.action_id"
        " JOIN operation_runs r ON r.copy_id=c.id AND r.kind=3"
        " LEFT JOIN device_files df ON df.id=c.source_device_file_id"
        " LEFT JOIN actions ob ON ob.id=df.observer_action_id"
        " WHERE a.status=2 AND a.cancel_requested=0 AND d.status IN (1,2,3)"
        " AND r.status IN (1,2) AND c.verification_state IN (3,5)"
        " AND c.read_end_event_id IS NOT NULL", identity="c.id")
    for copy_id, source_id, device_id, driver_id in page.items:
        saved = await runtime.database.read(lambda owned: unfinished_read_end(owned, copy_id))
        if saved is None or saved[1] is None or saved[1][3] is not None:
            continue
        ticket, (_ticket, end, occurred_at, _slot) = saved
        if source_id is None:
            evidence = _LOCAL_READ_EVIDENCE
            config = await runtime.database.read(lambda owned: _stored_read_attempt_config(owned.connection, copy_id))
            resume = lambda current, value, original=runtime, cfg=config: _resume_local_host_end(current, value, original, cfg)
        else:
            assembly = runtime.devices.get(device_id)
            evidence = _original_read_evidence(runtime, device_id, driver_id)
            if evidence is None:
                continue
            if assembly is None:
                assembly = SimpleNamespace(evidence=evidence, max_recopies=None, max_read_attempts=1)
            elif assembly.evidence is not evidence:
                assembly = replace(assembly, evidence=evidence)
            resume = lambda current, value, original=runtime, current_assembly=assembly: _resume_local_delivery_end(current, value, original, current_assembly)
        held = runtime.pending_read_ends.get(copy_id)
        if held is None:
            held = HeldReadEnd(ticket, end, occurred_at, resume=resume, evidence=evidence)
            runtime.pending_read_ends[copy_id] = held
        elif held.ticket != ticket or held.end != end:
            raise ConsistencyError("已校验交付的读取结束与原拥有者不一致")
        await resume(runtime, held)


async def _settle_canceled_read_ends(runtime: ObtainRuntime, now: int) -> None:
    """重启后按原完整结束与原票据完成已取消交付的读取结果。"""
    from camctl.outputs.read_attempts import HeldReadEnd, unfinished_read_end

    page = await runtime.discovery.rows(runtime.database, "canceled_read_ends", now,
        "SELECT c.id,c.source_device_file_id,ob.device_id,ob.driver_id"
        " FROM file_copies c JOIN deliveries d ON d.id=c.delivery_id"
        " JOIN actions a ON a.id=d.action_id"
        " JOIN operation_runs r ON r.copy_id=c.id AND r.kind=3"
        " LEFT JOIN device_files df ON df.id=c.source_device_file_id"
        " LEFT JOIN actions ob ON ob.id=df.observer_action_id"
        " WHERE a.status=2 AND a.cancel_requested=1 AND d.status IN (1,2,3)"
        " AND r.status IN (1,2) AND c.read_end_event_id IS NOT NULL", identity="c.id")
    for copy_id, source_id, device_id, driver_id in page.items:
        saved = await runtime.database.read(lambda owned: unfinished_read_end(owned, copy_id))
        if saved is None or saved[1] is None:
            continue
        ticket, (saved_ticket, end, occurred_at, _slot) = saved
        if ticket != saved_ticket:
            raise ConsistencyError("取消交付的原读取票据与完整结束不一致")
        if source_id is None:
            evidence = _LOCAL_READ_EVIDENCE
        else:
            evidence = _original_read_evidence(runtime, device_id, driver_id)
            if evidence is None:
                continue
        held = runtime.pending_read_ends.get(copy_id)
        if held is None:
            held = HeldReadEnd(ticket, end, occurred_at,
                resume=lambda current, value, original=runtime, registry=evidence:
                    _resume_canceled_delivery_end(current, value, original, registry),
                evidence=evidence)
            runtime.pending_read_ends[copy_id] = held
        elif held.ticket != ticket or held.end != end:
            raise ConsistencyError("取消交付的读取结束与原拥有者不一致")

        await _resume_canceled_delivery_end(runtime, held, runtime, evidence)


async def _resume_canceled_delivery_end(current, held, original_runtime, evidence):
    from camctl.outputs.read_attempts import (
        canceled_complete_read_result, current_local_read_end, run_owned_read,
    )

    runtime = replace(original_runtime, database=current.database)
    copy_id = int(held.ticket.target_id)
    async def finish(_control):
        if not await current_local_read_end(runtime, copy_id, held, allow_held_slot=True):
            return
        pending = await canceled_complete_read_result(runtime, held.ticket, evidence)
        if pending is not None:
            await _save_read_result_and_settle(runtime, pending)

    await run_owned_read(runtime, copy_id, finish)


async def _settle_failed_reads(runtime: ObtainRuntime) -> None:
    """新会话只消费原终态依据，不用本次上限重开已失败读取。"""
    now = runtime.occurred_at()
    page = (await runtime.discovery.rows(runtime.database, "failed", now,
        "SELECT c.id,r.error_json FROM file_copies c JOIN operation_runs r ON r.copy_id=c.id"
        " JOIN deliveries d ON d.id=c.delivery_id JOIN actions a ON a.id=d.action_id"
        " WHERE r.kind=3 AND r.status=4 AND d.status IN (1,2)"
        " AND a.status=2 AND a.cancel_requested=0", identity="c.id"))
    rows = page.items
    for copy_id, error_json in rows:
        if error_json is None:
            raise ConsistencyError("原失败读取缺少可靠终局错误")
        error = parse_exact_json(error_json)
        if error["code"] == "device_binding_unavailable":
            await _settle_saved_read_binding_failure(runtime, copy_id, error)
            continue
        if error["code"] not in (
                "read_attempts_exhausted", "checksum_mismatch",
                "source_file_unconfirmed"):
            raise ConsistencyError("原失败读取的所属错误不属于已登记读取终局失败")
        facts = await runtime.database.read(lambda owned: _read_facts(owned.connection, copy_id))
        if facts is None:
            raise ConsistencyError("原失败读取的未终态交付不可解释")
        await _fail_read_delivery(
            runtime, facts, runtime.occurred_at(),
            checksum_mismatch=error["code"] == "checksum_mismatch",
            source_verification_failed=error["code"] == "source_file_unconfirmed")


async def _settle_saved_read_binding_failure(runtime, copy_id, error):
    """原读取失败已保存时，恢复其所属绑定业务收场。"""
    from camctl.persistence.repositories.binding_read_cancel import load_saved_binding_read_failure
    from camctl.outputs.read_attempts import save_read_business

    facts = await runtime.database.read(lambda owned: _read_facts(owned.connection, copy_id, include_terminal=True))
    if facts is None or facts["source_device_file_id"] is None:
        raise ConsistencyError("原绑定失败缺少设备源交付拷贝")
    key, identity, occurred_at = await runtime.database.read(lambda owned: load_saved_binding_read_failure(
        owned.connection, facts["action_id"], copy_id, error))
    details = error.get("details")
    if not isinstance(details, Mapping):
        raise ConsistencyError("原绑定失败缺少保存的详情")
    statuses = {"missing": BindingStatus.DEVICE_MISSING, "mismatch": BindingStatus.DRIVER_MISMATCH,
                "physical_identity_mismatch": BindingStatus.PHYSICAL_IDENTITY_MISMATCH,
                "physical_identity_conflict": BindingStatus.PHYSICAL_IDENTITY_CONFLICT}
    status = statuses.get(details.get("reason"))
    if status is None:
        raise ConsistencyError("原绑定失败原因不属于已登记分区")
    binding = BindingResult(status, DeviceBinding(details["device_id"], details["expected_driver_id"]),
        details.get("actual_driver_id"), identity)
    request = FailReadBinding(copy_id, facts["source_device_file_id"], binding, occurred_at, key)
    result = await save_read_business(runtime, copy_id, "read_binding_failure", request, runtime.outputs.fail_read_binding)
    if result.kind is not DbOutcomeKind.COMPLETED:
        raise db_failure(ConsistencyError, f"原绑定失败业务事务未可靠保存: {result.error}", result)


async def _run_read(
        runtime: ObtainRuntime, assembly: DeviceReadAssembly,
        facts: Mapping, ticket, now: int) -> None:
    """一次读取尝试：摘要能力固定、会话拷贝与完整性收尾。"""
    from camctl.outputs.read_attempts import run_owned_read

    async def body(control):
        await _run_read_and_save(runtime, assembly, facts, ticket, now, control=control)

    await run_owned_read(runtime, facts["id"], body)


async def _run_read_and_save(runtime, assembly, facts, ticket, now, *, control=None):
    from camctl.outputs.read_attempts import persist_read_end

    device_file_id = facts["source_device_file_id"]
    await _ensure_checksum_support(runtime, assembly, device_file_id, now)
    digest = (
        await assembly.digest_for(device_file_id)
        if assembly.digest_supported and assembly.digest_for is not None
        else None)
    step = await _copy_delivery(
        runtime,
        DriverReadSessions(runtime.database, assembly.driver, ticket=ticket,
            idle_timeout_s=assembly.read_idle_timeout_s, control=control,
            capture_selection=(None if runtime.availability is None else
                lambda now: runtime.availability.capture_selection(assembly.binding.device_id, now)),
            continuations=runtime.continuing_read_tickets, occurred_at=runtime.occurred_at),
        facts, digest, max_recopies=assembly.max_recopies,
        read_end=(runtime.pending_read_ends[facts["id"]].end if facts["id"] in runtime.pending_read_ends else None),
        on_read_end=lambda end, complete, state: persist_read_end(runtime, ticket, end, complete,
            state=state, source_device_file_id=facts["source_device_file_id"],
            source_intermediate_file_id=facts["source_intermediate_file_id"],
            resume=lambda current, held: _resume_local_delivery_end(current, held, runtime, assembly), evidence=assembly.evidence),
        on_read_start=lambda: runtime.pending_read_ends.pop(facts["id"], None))
    await _finish_read_attempt(runtime, assembly, facts, ticket, step)


async def _resume_local_delivery_end(current, held, original_runtime, assembly):
    """只核实原完整副本；原会话的设备结果不再调用源端口。"""
    from camctl.outputs.read_attempts import current_local_read_end, run_owned_read

    runtime = replace(original_runtime, database=current.database)
    copy_id = int(held.ticket.target_id)
    facts = await runtime.database.read(lambda owned: _read_facts(owned.connection, copy_id, include_terminal=True))
    if facts is None:
        raise ConsistencyError("原本地副本校验缺少所属交付")

    async def complete_and_save(_control):
        if not await current_local_read_end(runtime, copy_id, held):
            return
        copies = RecordingInputCopies(runtime.database, BoundDirectories(staging=runtime.directories.staging),
            runtime.occurred_at, max_recopies=assembly.max_recopies, file_executor=runtime.file_executor,
            read_runtime=runtime)
        step = replace(await _complete_copy(copies, copy_id, None), read_end=held.end,
                       stop_requested=True, content_complete=True)
        if (step.phase is InputPhase.COMPLETION_FAILED
                and getattr(step.error, "stage", None) == "source_binding_unavailable"):
            from camctl.outputs.read_attempts import PendingReadBusiness, held_binding_read_result

            binding = assembly.binding_failure
            business = PendingReadBusiness(runtime.outputs.fail_read_binding,
                FailReadBinding(copy_id, facts["source_device_file_id"], binding, runtime.occurred_at()),
                new_operation_key())
            pending = await held_binding_read_result(runtime, copy_id, binding, business)
            if pending is not None:
                await _save_read_result_and_settle(runtime, pending, facts)
            return
        await _finish_read_attempt(runtime, assembly, facts, held.ticket, step)

    await run_owned_read(runtime, copy_id, complete_and_save)


async def _copy_delivery(
        runtime: ObtainRuntime, sessions, facts: Mapping, digest, *, max_recopies: int = 1,
        read_end: ReadEnd | None = None, on_read_end=None, on_read_start=None,
) -> InputStep:
    """复用资格、续传、分段与完整性事务推进交付拷贝。"""
    copies: RecordingCopies = RecordingInputCopies(
        runtime.database, BoundDirectories(staging=runtime.directories.staging),
        runtime.occurred_at, max_recopies=max_recopies, file_executor=runtime.file_executor,
        read_runtime=runtime)
    state = await copies.copy_state(facts["id"])
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
    def read_started():
        if on_read_start is not None:
            on_read_start()
        from camctl.outputs.read_attempts import ReadContinuation
        original = runtime.continuing_read_tickets.get(facts["id"])
        if original is not None:
            runtime.continuing_read_tickets[facts["id"]] = ReadContinuation(original.ticket, False)
    try:
        session = await sessions.open_session(
            _source_file_id(facts), state.source_size if prepared.decision.outcome is ResumeOutcome.VERIFY
            else prepared.decision.offset, on_start=read_started)
        from camctl.outputs.read_attempts import ReadDispatchPrevented
        if isinstance(session, ReadDispatchPrevented):
            return InputStep(InputPhase.READ_NOT_STARTED, copy_id=facts["id"],
                reason=session.reason, dispatch_prevented=session)
    except Exception as error:
        from camctl.devices.read_session import ReadPreparationError, ReadPreparationSettlement

        settlement = (error.settlement if isinstance(error, ReadPreparationError)
                      else ReadPreparationSettlement.SETTLED)
        return InputStep(InputPhase.WAITING if settlement is ReadPreparationSettlement.WAITING else InputPhase.SEGMENT_FAILED,
                         copy_id=facts["id"],
                         error=error, source_failed=settlement is ReadPreparationSettlement.SETTLED,
                         preparation_settlement=settlement)
    result = None
    execution_error = None
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
    except BaseException as error:
        execution_error = error
        raise
    finally:
        from camctl.capture.input_copy import stop_read_session

        original_error = (execution_error if execution_error is not None else
                          result.error if result is not None else None)
        end = await stop_read_session(session, original_error=original_error)
        if on_read_end is not None:
            import inspect
            saved = on_read_end(end, session.position() == state.source_size, state)
            if inspect.isawaitable(saved):
                from camctl.persistence.business import settle_required
                await settle_required(saved)
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
        phase = (InputPhase.VERIFICATION_FAILED
                 if isinstance(error, CopyCompletionError)
                 and error.stage == "verification_failed"
                 else InputPhase.COMPLETION_FAILED)
        return InputStep(phase, copy_id=copy_id, error=error)
    if completion.phase is CompletionPhase.PREPARED:
        return InputStep(InputPhase.INPUT_READY, copy_id=copy_id)
    return InputStep(InputPhase.RECOPY_PENDING, copy_id=copy_id)


async def _finish_read_attempt(
        runtime: ObtainRuntime, assembly: DeviceReadAssembly,
        facts: Mapping, ticket, step: InputStep) -> None:
    """保存尝试结果；预算耗尽按流程失败终局交付并交还机会。"""
    return await settle_required(_finish_read_attempt_phase(runtime, assembly, facts, ticket, step))


async def _finish_read_attempt_phase(
        runtime: ObtainRuntime, assembly: DeviceReadAssembly,
        facts: Mapping, ticket, step: InputStep) -> None:
    """保存尝试结果；预算耗尽按流程失败终局交付并交还机会。"""
    from camctl.outputs.read_attempts import require_preparation_settled

    if step.phase is InputPhase.READ_NOT_STARTED:
        from camctl.outputs.read_attempts import prepare_prevented_read_result
        pending = await prepare_prevented_read_result(runtime, ticket, step.dispatch_prevented, assembly.evidence)
        if pending is not None:
            await _save_read_result_and_settle(runtime, pending, facts)
        return

    if not require_preparation_settled(step):
        return
    if step.phase is InputPhase.RECOPY_PENDING:
        # 新轮次已经提交且文件任务已结束；读取尝试和原次数继续承担责任。
        runtime.pending_read_ends.pop(facts["id"], None)
        from camctl.outputs.read_attempts import ReadContinuation
        runtime.continuing_read_tickets[facts["id"]] = ReadContinuation(ticket, False, step, runtime.occurred_at(), evidence=assembly.evidence)
        runtime.retry_gate.cleared(f"read/{facts['id']}")
        return
    from camctl.outputs.read_attempts import stopped_read_result

    stopped = stopped_read_result(runtime, ticket, step, assembly.evidence, runtime.occurred_at())
    if stopped is not None:
        await _save_read_result_and_settle(runtime, stopped, facts)
        return
    from camctl.outputs.read_attempts import canceled_complete_read_result

    completed_cancel = await canceled_complete_read_result(runtime, ticket, assembly.evidence)
    if completed_cancel is not None:
        await _save_read_result_and_settle(runtime, completed_cancel, facts)
        return
    from camctl.outputs.read_attempts import source_verification_run_finish

    verification_failed = step.phase is InputPhase.VERIFICATION_FAILED
    verification_finish = (await source_verification_run_finish(
        runtime, ticket, step, output_id=facts["output_id"])
        if verification_failed else None)
    checksum_failed = step.phase is InputPhase.CHECKSUM_EXHAUSTED
    if (step.phase is not InputPhase.INPUT_READY and not step.source_failed
            and not checksum_failed and not verification_failed):
        raise ConsistencyError(f"读取的本地或保存前提未完成，保留原尝试: {step.phase.value}: {step.error}") from step.error
    succeeded = (step.phase is InputPhase.INPUT_READY
                 or checksum_failed or verification_failed)
    # 原 ticket 的连续 attempt_no 已在取得／恢复时核实为原累计次数。
    # 实际返回后先持有结果，不能为读取预算再访问数据库而丢失结果。
    exhausted = not succeeded and ticket.attempt_id >= assembly.max_read_attempts
    outcome = CallOutcome(
        status=(AttemptStatus.SUCCEEDED if succeeded
                else AttemptStatus.FAILED),
        error=None if succeeded else ErrorValue(
            code="device_error", stage="read", details=read_failure_details(step)),
        effect=EffectState.UNKNOWN,
        settlement=Settlement(
            basis=SettlementBasis.OBSERVED,
            evidence=EvidenceValue(
                type="read_returned", version=1, data={})),
        observations=())
    if verification_failed:
        run_finish = verification_finish
    elif checksum_failed:
        run_finish = RunFinish(RunOutcome.FAILED, ErrorValue(
            "checksum_mismatch", "source_read", {
                "max_recopies": step.error.max_recopies,
                "recopies_used": step.error.recopies_used}))
    else:
        run_finish = _run_finish(succeeded, exhausted)
    from camctl.outputs.read_attempts import hold_read_result

    pending = hold_read_result(runtime, AttemptFinish(
            ticket=ticket,
            outcome=validate_outcome(ticket, outcome, assembly.evidence),
            occurred_at=runtime.occurred_at(),
            retry_wait=(not succeeded) and not exhausted,
            run_finish=run_finish),
        context={name: facts[name] for name in (
            "action_id", "output_id", "source_device_file_id", "source_intermediate_file_id") if name in facts},
        diagnostic_error=step.error if checksum_failed or verification_failed else None,
        diagnostic_stage=step.phase.value)
    await _save_read_result_and_settle(runtime, pending, facts)


async def _retry_read_results(runtime: ObtainRuntime) -> None:
    from camctl.outputs.read_attempts import PendingStoppedRead, forget_read_result, save_read_result

    for pending in tuple(runtime.pending_read_results.values()):
        if not isinstance(pending, PendingStoppedRead) and pending.finish.outcome.outcome.settlement.basis is SettlementBasis.ASSUMED:
            async def save_and_forget():
                await save_read_result(runtime, pending, retry=True)
                forget_read_result(runtime, pending)
            await settle_required(save_and_forget())
            continue
        await _save_read_result_and_settle(runtime, pending, retry=True)


async def _save_read_result_and_settle(runtime: ObtainRuntime, pending, facts=None, *, retry=False) -> None:
    return await settle_required(_save_read_result_and_settle_phase(runtime, pending, facts, retry=retry))


async def _save_read_result_and_settle_phase(runtime: ObtainRuntime, pending, facts=None, *, retry=False) -> None:
    from camctl.outputs.read_attempts import forget_read_result, save_read_result

    pending = await save_read_result(runtime, pending, retry=retry)
    if pending is None:
        return
    if pending.preserved_run_terminal:
        forget_read_result(runtime, pending)
        return
    def complete_business(_):
        runtime.retry_gate.cleared(f"read/{int(pending.finish.ticket.target_id)}")
        forget_read_result(runtime, pending)

    if pending.business is not None:
        from camctl.outputs.read_attempts import hold_prepared_read_business, save_prepared_read_business

        copy_id = int(pending.finish.ticket.target_id)
        hold_prepared_read_business(runtime, copy_id, "read_binding_failure", pending.business)
        forget_read_result(runtime, pending)
        await save_prepared_read_business(runtime, copy_id, "read_binding_failure", pending.business, retry=retry)
        runtime.retry_gate.cleared(f"read/{copy_id}")
        return
    if facts is None:
        facts = await runtime.database.read(lambda owned: _read_facts(owned.connection, int(pending.finish.ticket.target_id), include_terminal=True))
    if facts is None:
        raise ConsistencyError("原读取结果的所属交付已终态，尚未核实业务收场")
    finish = pending.finish
    if facts["delivery_status"] == int(_DELIVERY_STATUS.FAILED):
        stored = await read_one(runtime.database,
            "SELECT d.error_json,i.source_dependency,c.slot_device_id FROM deliveries d"
            " JOIN obtain_items i ON i.delivery_id=d.id JOIN file_copies c ON c.delivery_id=d.id"
            " WHERE d.id=? AND c.id=?", (facts["delivery_id"], facts["id"]))
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
    if exhausted:
        code = finish.run_finish.error.code
        if code == "checksum_mismatch":
            await _fail_read_delivery(runtime, facts, runtime.occurred_at(), checksum_mismatch=True,
                on_completed=complete_business, business_saves=pending.business_saves, retry=retry)
            return
        if code == "source_file_unconfirmed":
            await _fail_read_delivery(
                runtime, facts, runtime.occurred_at(),
                source_verification_failed=True, on_completed=complete_business,
                business_saves=pending.business_saves, retry=retry)
            return
    if succeeded:
        if facts["source_device_file_id"] is not None:
            from camctl.outputs.read_attempts import save_read_business

            released = await save_read_business(runtime, facts["id"], "release_read_slot",
                SlotRequest(copy_id=facts["id"], occurred_at=runtime.occurred_at()),
                runtime.outputs.release_read_slot, retry=retry, business_saves=pending.business_saves,
                on_completed=complete_business)
            if released.kind is not DbOutcomeKind.COMPLETED:
                raise db_failure(ConsistencyError, f"原读取机会释放未可靠保存: {released.error}", released)
        else:
            complete_business(None)
        return
    if exhausted:
        await _fail_read_delivery(runtime, facts, runtime.occurred_at(), on_completed=complete_business,
            business_saves=pending.business_saves, retry=retry)
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

    def __init__(self, database: Any, directories: DeliveryDirectories, *, ticket=None, control=None) -> None:
        self._database = database
        self._directories = directories
        self._ticket = ticket
        self._control = control

    def for_attempt(self, ticket, *, control=None):
        return LocalReadSessions(self._database, self._directories, ticket=ticket, control=control)

    async def open_session(self, source_intermediate_file_id: int,
                           offset: int, *, on_start=None) -> "_LocalReadSession | ReadDispatchPrevented":
        from camctl.outputs.read_attempts import read_dispatch_qualification, read_dispatch_gate

        if self._ticket is None:
            raise ConsistencyError("主机读取会话缺少已保存的尝试票据")
        def read(owned):
            with closing(owned.connection.execute(
                "SELECT relative_path,size_bytes FROM intermediate_files WHERE id = ?",
                (source_intermediate_file_id,))) as cursor:
                row = cursor.fetchone()
            qualification = read_dispatch_qualification(owned, self._ticket,
                source_intermediate_file_id, device_source=False)
            facts = _read_facts(owned.connection, int(self._ticket.target_id))
            photo = _is_photo_cache_source(owned.connection, facts)
            return row, qualification, photo
        try:
            row, qualification, photo = await self._database.read(read)
            if row is None:
                raise ConsistencyError(f"中间文件缺失: {source_intermediate_file_id}")
        except Exception as error:
            from camctl.devices.read_session import ReadPreparationError, ReadPreparationSettlement
            raise ReadPreparationError("主机读取源事实未取得，原尝试尚未调用",
                settlement=ReadPreparationSettlement.UNCONFIRMED) from error
        prevented = read_dispatch_gate(qualification, self._control)
        if prevented is not None:
            return prevented
        path = self._directories.staging / str(row[0])
        if on_start is not None:
            on_start()
        if photo:
            from camctl.host_files.downloads import DownloadWorkspace
            from camctl.host_files.io import OwnedPhotoCacheReader
            owner = DownloadWorkspace(self._directories.staging, self._ticket)
            return _LocalReadSession(OwnedPhotoCacheReader(path, offset, row[1], owner))
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
    page = (await runtime.discovery.reads(runtime.database, "local", now))
    copy_ids = (candidate.copy_id for candidate in page.items)
    sessions = LocalReadSessions(runtime.database, runtime.directories)
    for copy_id in copy_ids:
        await _advance_local_read(runtime, sessions, copy_id, now)


async def _advance_local_read(
        runtime: ObtainRuntime, sessions: LocalReadSessions,
        copy_id: int, now: int) -> None:
    """在原 READ_FILE 流程预算内推进一份主机源交付拷贝。"""
    def inputs(owned):
        facts = _read_facts(owned.connection, copy_id)
        if facts is None or facts["verification_state"] in _VERIFICATION_DONE:
            return facts, False, None
        return (facts, _is_reusable_device_cache_source(owned.connection, facts),
                _stored_read_attempt_config(owned.connection, copy_id))
    facts, is_inspection_source, config = await runtime.database.read(inputs)
    if facts is None or facts["verification_state"] in _VERIFICATION_DONE:
        return
    if is_inspection_source and await _retry_failed_local_source_fallback(runtime, facts):
        return
    if is_inspection_source and runtime.file_executor is None:
        return

    if await _read_wait_remaining(runtime, copy_id, config.retry_interval_s) is not None:
        return

    async def body(control):
        current = await runtime.database.read(lambda owned: _read_facts(owned.connection, copy_id))
        if current is None:
            return
        from camctl.persistence.repositories.read_end import load_read_end
        from camctl.outputs.read_attempts import persist_read_end
        saved = await runtime.database.read(lambda owned: load_read_end(copy_id, owned))
        ticket = saved[0] if saved is not None else await _begin_local_attempt(runtime, current, now, config)
        if ticket is None:
            return
        step = await _copy_delivery(runtime, sessions.for_attempt(ticket, control=control), current, None,
            read_end=None if saved is None else saved[1],
            on_read_end=lambda end, complete, state: persist_read_end(runtime, ticket, end, complete,
                state=state, source_device_file_id=current["source_device_file_id"],
                source_intermediate_file_id=current["source_intermediate_file_id"],
                resume=lambda current_runtime, held: _resume_local_host_end(current_runtime, held, runtime, config),
                                                               evidence=_LOCAL_READ_EVIDENCE))
        await _finish_local_attempt(runtime, current, ticket, step, config)

    from camctl.outputs.read_attempts import run_owned_read

    await run_owned_read(runtime, copy_id, body)


async def _resume_local_host_end(current, held, original_runtime, config):
    from camctl.outputs.read_attempts import current_local_read_end, run_owned_read

    runtime = replace(original_runtime, database=current.database)
    copy_id = int(held.ticket.target_id)
    async def complete_and_save(_control):
        if not await current_local_read_end(runtime, copy_id, held):
            return
        facts = await runtime.database.read(lambda owned: _read_facts(owned.connection, copy_id, include_terminal=True))
        copies = RecordingInputCopies(runtime.database, BoundDirectories(staging=runtime.directories.staging),
                                      runtime.occurred_at, file_executor=runtime.file_executor, read_runtime=runtime)
        step = replace(await _complete_copy(copies, copy_id, None), read_end=held.end,
                       stop_requested=True, content_complete=True)
        await _finish_local_attempt(runtime, facts, held.ticket, step, config)

    await run_owned_read(runtime, copy_id, complete_and_save)


def _is_inspection_copy_source(connection, facts: Mapping) -> bool:
    """当前主机源是否来自已登记检查副本，而不是普通本地产物。"""
    source_id = facts["source_intermediate_file_id"]
    if source_id is None:
        return False
    with closing(connection.execute(
        "SELECT inspection_id FROM file_copies WHERE target_file_id=? LIMIT 2",
        (source_id,),
    )) as cursor:
        rows = cursor.fetchall()
    if len(rows) > 1:
        raise ConsistencyError(f"检查输入文件存在重复读取拷贝: {source_id}")
    return bool(rows and rows[0][0] is not None)


def _is_reusable_device_cache_source(connection, facts: Mapping) -> bool:
    """识别检查副本或照片角色缓存；换源资格仍由独立事务守卫核对。"""
    if _is_inspection_copy_source(connection, facts):
        return True
    return _is_photo_cache_source(connection, facts)


def _is_photo_cache_source(connection, facts: Mapping) -> bool:
    source_id, output_id = facts["source_intermediate_file_id"], facts.get("output_id")
    if source_id is None or output_id is None:
        return False
    from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
    with closing(connection.execute(
        "SELECT a.driver_id,a.execution_spec_json,o.device_file_id,f.owner_action_id,o.source_action_id"
        " FROM outputs o JOIN actions a ON a.id=o.source_action_id"
        " JOIN intermediate_files f ON f.id=? WHERE o.id=?", (source_id, output_id))) as cursor:
        row = cursor.fetchone()
    if row is None:
        return False
    spec = row[1] if isinstance(row[1], Mapping) else parse_exact_json(row[1])
    return (row[0] == DRIVER_ID and isinstance(spec, Mapping) and spec.get("output_storage") == "device"
            and row[2] is not None and row[3] == row[4])


def _stored_read_attempt_config(connection, copy_id: int) -> AttemptConfig:
    """读取正式授予时冻结的 READ_FILE 预算与时限。"""
    with closing(connection.execute(
        "SELECT max_attempts_used,timeout_s_json,retry_interval_s_json"
        " FROM operation_runs WHERE responsibility_key=?",
        (f"read/{copy_id}",),
    )) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError(f"读取流程缺失: read/{copy_id}")
    timeout = None if row[1] is None else Decimal(str(row[1]))
    retry_interval = None if row[2] is None else Decimal(str(row[2]))
    return AttemptConfig(int(row[0]), timeout, retry_interval)


def _failed_inspection_read_request(connection, facts: Mapping):
    """恢复已有的本地源失败事实；只返回仍有预算的可靠终态尝试。"""
    with closing(connection.execute(
        "SELECT r.status,r.attempts_used,r.max_attempts_used,r.retry_wait_required,"
        " a.id,a.attempt_no,a.status,a.error_json,a.intent_event_id,"
        " a.result_event_id,a.copy_round,"
        " h.occurred_at FROM operation_runs r"
        " LEFT JOIN operation_attempts a ON a.run_id=r.id"
        " LEFT JOIN history_events h ON h.id=a.result_event_id"
        " WHERE r.responsibility_key=? ORDER BY a.attempt_no DESC LIMIT 1",
        (f"read/{facts['id']}",),
    )) as cursor:
        row = cursor.fetchone()
    if row is None or row[4] is None:
        return None
    (run_status, attempts_used, max_attempts, retry_wait_required,
     attempt_id, attempt_no, attempt_status, error_json, intent_event_id,
     result_event_id, copy_round, occurred_at) = row
    if (run_status != int(_RUN_STATUS.ACTIVE)
            or attempts_used != attempt_no
            or attempts_used >= max_attempts
            or retry_wait_required != 1
            or attempt_status != int(_ATTEMPT_STATUS.FAILED)
            or error_json is None or intent_event_id is None
            or result_event_id is None
            or copy_round is None or copy_round > facts["round"]
            or occurred_at is None):
        return None
    error = error_json if isinstance(error_json, Mapping) else parse_exact_json(error_json)
    details = error.get("details")
    if (error.get("code") != "local_read_failed"
            or error.get("stage") != "source_read"
            or not isinstance(details, Mapping)
            or details.get("source_failed") is not True):
        return None
    return ReadSourceFallback(
        copy_id=int(facts["id"]), failed_attempt_id=int(attempt_id),
        occurred_at=int(occurred_at))


async def _retry_failed_local_source_fallback(
        runtime: ObtainRuntime, facts: Mapping) -> bool:
    """用稳定事务键重开已落库的失败换源申请。"""
    request = await runtime.database.read(lambda owned: _failed_inspection_read_request(owned.connection, facts))
    if request is None:
        return False
    outcome = await _save_repository(runtime, runtime.outputs.fallback_read_source, request, read_source_fallback_key(request.copy_id))
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise db_failure(ConsistencyError,
            f"原读取源切换事务未可靠确认（{outcome.kind.value}）: {outcome.error}", outcome)
    from camctl.persistence.repositories.outputs import (
        ReadSourceFallbackDisposition,
    )
    return outcome.value.disposition is ReadSourceFallbackDisposition.SWITCHED


async def _begin_local_attempt(
        runtime: ObtainRuntime, facts: Mapping, now: int,
        config: AttemptConfig,
):
    """按已保存 READ_FILE 流程配置开始或续接主机源尝试。"""
    intent = AttemptIntent(
        operation="read",
        action_id=facts["action_id"],
        kind=OperationKind.READ_FILE,
        target=AttemptTarget(copy_id=facts["id"]),
        query_purpose=None,
        config=config,
        occurred_at=now,
        copy_round=facts["round"])
    ticket, reason = await acquire_read_attempt(runtime, intent)
    if ticket is not None:
        return ticket
    if reason == "budget_exhausted":
        await _fail_read_delivery(runtime, facts, now)
    return None


async def _finish_local_attempt(
        runtime: ObtainRuntime, facts: Mapping, ticket, step: InputStep, config: AttemptConfig) -> None:
    """先保存本地读取结果，再按同一 READ_FILE 预算续接或换源。"""
    return await settle_required(_finish_local_attempt_phase(runtime, facts, ticket, step, config))


async def _finish_local_attempt_phase(
        runtime: ObtainRuntime, facts: Mapping, ticket, step: InputStep, config: AttemptConfig) -> None:
    """先保存本地读取结果，再按同一 READ_FILE 预算续接或换源。"""
    from camctl.outputs.read_attempts import require_preparation_settled

    if step.phase is InputPhase.READ_NOT_STARTED:
        from camctl.outputs.read_attempts import prepare_prevented_read_result
        pending = await prepare_prevented_read_result(runtime, ticket, step.dispatch_prevented, _LOCAL_READ_EVIDENCE)
        if pending is not None:
            await _save_read_result_and_settle(runtime, pending, facts)
        return

    if not require_preparation_settled(step):
        return
    if step.phase is InputPhase.RECOPY_PENDING:
        # 新轮次已登记且本地文件任务已收场；原 READ_FILE attempt 继续负责。
        runtime.pending_read_ends.pop(facts["id"], None)
        from camctl.outputs.read_attempts import ReadContinuation
        runtime.continuing_read_tickets[facts["id"]] = ReadContinuation(ticket, False, step, runtime.occurred_at(), evidence=_LOCAL_READ_EVIDENCE)
        runtime.retry_gate.cleared(f"read/{facts['id']}")
        return
    checksum_failed = step.phase is InputPhase.CHECKSUM_EXHAUSTED
    succeeded = step.phase is InputPhase.INPUT_READY or checksum_failed
    exhausted = ticket.attempt_id >= config.max_attempts
    fallback_after_source_failure = step.source_failed and not exhausted
    outcome = CallOutcome(
        status=(AttemptStatus.SUCCEEDED if succeeded
                else AttemptStatus.FAILED),
        error=None if succeeded else ErrorValue(
            code="local_read_failed", stage="source_read",
            details={
                "phase": step.phase.value,
                "source_failed": step.source_failed,
                **read_failure_details(step)}),
        effect=EffectState.UNKNOWN,
        settlement=Settlement(
            basis=SettlementBasis.OBSERVED,
            evidence=EvidenceValue(
                type="read_returned", version=1, data={})),
        observations=())
    from camctl.outputs.read_attempts import controlled_read_stop, hold_read_result

    pending = hold_read_result(runtime, AttemptFinish(
            ticket=ticket,
            outcome=validate_outcome(ticket, outcome, _LOCAL_READ_EVIDENCE),
            occurred_at=runtime.occurred_at(),
            retry_wait=(not succeeded) and not exhausted,
            run_finish=(RunFinish(RunOutcome.FAILED, ErrorValue(
                "checksum_mismatch", "source_read", {
                    "max_recopies": step.error.max_recopies,
                    "recopies_used": step.error.recopies_used}))
                if checksum_failed else _run_finish(succeeded, exhausted))),
        context={name: facts[name] for name in (
            "action_id", "output_id", "source_device_file_id", "source_intermediate_file_id") if name in facts},
        diagnostic_error=step.error if checksum_failed else None,
        diagnostic_stage=step.phase.value, suppress_diagnostic=controlled_read_stop(step))
    await _save_read_result_and_settle(runtime, pending, facts)
    if succeeded:
        runtime.retry_gate.cleared(f"read/{facts['id']}")
        return
    if exhausted:
        return
    if fallback_after_source_failure and await runtime.database.read(
            lambda owned: _is_reusable_device_cache_source(owned.connection, facts)):
        await _retry_failed_local_source_fallback(runtime, facts)


async def _ensure_checksum_support(
        runtime: ObtainRuntime, assembly: DeviceReadAssembly,
        device_file_id: int, now: int) -> None:
    """按绑定声明一次固定来源文件的摘要能力；已决定不重复声明。"""
    row = await read_one(runtime.database, "SELECT checksum_support FROM device_files WHERE id = ?", (device_file_id,))
    if row is None:
        raise ConsistencyError(f"设备文件不存在: {device_file_id}")
    if row[0] != 1:
        return
    outcome = await _save_repository(runtime, CaptureRepository().save_file_checksum, FileChecksumSave(
            file_id=device_file_id,
            support=2 if assembly.digest_supported else 3,
            occurred_at=now), new_operation_key())
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise db_failure(ConsistencyError, f"摘要能力声明未完成: {outcome.error}", outcome)

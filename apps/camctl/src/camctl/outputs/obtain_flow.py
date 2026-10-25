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
from camctl.outputs.read_discovery import ReadDiscoveryOwner
from camctl.outputs.handoff import (
    DeliveryContext, DeliveryDirectories, DeliveryPhase, publish_delivery)
from camctl.outputs.qualification import (
    FileCandidate, OperationConfig)
from camctl.outputs.read_attempts import acquire_read_attempt, recover_unavailable_read
from camctl.outputs.slots import SlotOutcome, SlotRequest
from camctl.outputs.sources import (
    ResolutionState, SelectionSnapshot, SourceResolution, SourceSpec,
    read_source_spec, select_outputs)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.business import settle_required
from camctl.outputs.database_reads import read_one
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import (
    FailReadBinding, FailReadDelivery, FinishObtain, FixSelection,
    OutputsRepository, ReadSourceFallback,
    ResolveSources, StartObtainAction, load_obtain_facts,
    load_selection_facts, read_source_fallback_key)

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


async def advance_obtain(runtime: ObtainRuntime) -> None:
    """推进一个轮次的取回执行链；各步按已保存事实幂等。"""
    from camctl.outputs.read_attempts import retry_read_business, retry_read_ends

    await retry_read_business(runtime)
    await _retry_read_results(runtime)
    await retry_read_ends(runtime)
    now = runtime.occurred_at()
    await runtime.discovery.begin_round(runtime.database, now)
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
            raise ConsistencyError(
                f"取回开始事务未完成（{outcome.kind.value}）:"
                f" {outcome.error}")


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
        raise ConsistencyError(
            f"来源解析事务未完成（{outcome.kind.value}）:"
            f" {outcome.error}")
    return bool(outcome.value.fixed)


def _source_spec(action: Mapping) -> SourceSpec:
    """从原请求重建来源引用；不可解释的输入按状态库错误拒绝。"""
    return read_source_spec(action["input_fields_json"])


async def _fix_pending_selections(
        runtime: ObtainRuntime, now: int) -> None:
    """对每个待固定的选择计算快照并保存；来源未完成保持等待。"""
    from camctl.outputs.definitions import read_excluded_output_ids, read_selection_request

    from camctl.persistence.repositories.segment_selections import (
        FixSegmentSelection,
        fix_segment_selection,
        is_segmented_source,
    )

    async def save_selection(identity, request, key):
        async def save_and_apply():
            receipt = await _save_repository(runtime, fix_segment_selection, request, key)
            if receipt.kind is not DbOutcomeKind.COMPLETED:
                if receipt.kind is DbOutcomeKind.ROLLED_BACK:
                    del runtime.pending_segment_selections[identity]
                raise ConsistencyError(f"原分段选择尚未可靠固定: {receipt.error}")
            del runtime.pending_segment_selections[identity]
        await settle_required(save_and_apply())

    for identity, (request, key) in tuple(runtime.pending_segment_selections.items()):
        await save_selection(identity, request, key)
    page = await runtime.discovery.rows(runtime.database, "selections", now,
        "SELECT s.id,d.depends_on_action_id,d.action_id FROM obtain_source_selections s"
        " JOIN action_dependencies d ON d.id=s.dependency_id"
        " JOIN actions a ON a.id=d.action_id"
        " WHERE a.status=2 AND a.cancel_requested=0 AND a.source_resolution_state=2"
        " AND s.status=?", (int(_SELECTION_STATUS.PENDING),), identity="s.id")
    for selection_id, member_id, action_id in page.items:
        def inputs(owned):
            connection = owned.connection
            if is_segmented_source(connection, int(member_id)):
                return None
            action = _action_facts(connection, action_id)
            mode, requested = read_selection_request(
                parse_exact_json(action["execution_spec_json"]),
                parse_exact_json(action["input_fields_json"]))
            facts = load_selection_facts(connection, int(member_id),
                requested_output_ids=requested)
            return action, mode, requested, facts
        inputs_value = await runtime.database.read(inputs)
        if inputs_value is None:
            request, key = FixSegmentSelection(int(selection_id), now), new_operation_key()
            runtime.pending_segment_selections[int(selection_id)] = (request, key)
            await save_selection(int(selection_id), request, key)
            continue
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
        outcome = await _save_repository(runtime, runtime.outputs.fix_selection,
            FixSelection(int(selection_id), snapshot, now), new_operation_key())
        if outcome.kind is not DbOutcomeKind.COMPLETED:
            raise ConsistencyError(f"选择固定事务未完成（{outcome.kind.value}）: {outcome.error}")


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
        outcome = await _save_repository(runtime, runtime.outputs.grant_file, candidate, new_operation_key())
        if outcome.kind is not DbOutcomeKind.COMPLETED:
            raise ConsistencyError(
                f"读取资格事务未完成（{outcome.kind.value}）:"
                f" {outcome.error}")


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
        if device_file_id is not None:
            from camctl.persistence.repositories.segment_selections import is_segmented_source
            from camctl.outputs.segment_copy_reuse import read_bound_check_copy
            if is_segmented_source(connection, int(source_action_id)):
                bound = read_bound_check_copy(
                    source_action_id=int(source_action_id),
                    source_device_file_id=int(device_file_id),
                    obtain_action_id=int(action["id"]), selection_id=selection_id, owned=owned)
        return row, bound
    row, bound = await runtime.database.read(inputs)
    (source_action_id, device_file_id, intermediate_file_id, original_name,
     source_action_name, plan_name) = row

    if device_file_id is not None:
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
        deliveries = (_prepared_deliveries(owned.connection, action_id, limit=publish_limit)
                      if publish_limit else ())
        return facts, deliveries
    facts, deliveries = await runtime.database.read(inputs)
    decision = decide_obtain_finish(facts)
    if not decision.may_publish:
        return 0
    for delivery_id in deliveries:
        await _publish_delivery(runtime, int(delivery_id), now)
    if await runtime.database.read(lambda owned: _prepared_deliveries(owned.connection, action_id, limit=1)):
        return len(deliveries)
    outcome = await _save_repository(runtime, runtime.outputs.finish_obtain, FinishObtain(action_id=action_id, occurred_at=now), new_operation_key())
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(
            f"取回完成事务未完成（{outcome.kind.value}）:"
            f" {outcome.error}")
    return len(deliveries)


def _prepared_deliveries(connection, action_id: int, *, limit: int) -> list[int]:
    """汇总确定后仍待发布的成功交付。"""
    with closing(connection.execute(
        "SELECT d.id FROM deliveries d"
        " JOIN obtain_items i ON i.delivery_id = d.id"
        " JOIN obtain_source_selections s ON s.id = i.selection_id"
        " JOIN action_dependencies dep ON dep.id = s.dependency_id"
        " WHERE dep.action_id = ? AND d.status = ? ORDER BY d.id LIMIT ?",
        (action_id, int(_DELIVERY_STATUS.PREPARED), limit),
    )) as cursor:
        return [int(row[0]) for row in cursor.fetchall()]


async def _publish_delivery(
        runtime: ObtainRuntime, delivery_id: int, now: int) -> None:
    """发布一份准备完成的交付；终局失败按已保存事实保留。"""
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
    if runtime.binding_check is not None:
        candidates = ((item.copy_id, item.source_file_id, item.device_id, item.driver_id) for item in page.items)
        for copy_id, source_id, device_id, driver_id in candidates:
            result = runtime.binding_check(DeviceBinding(device_id, driver_id))
            if result.status in (BindingStatus.DEVICE_MISSING, BindingStatus.DRIVER_MISMATCH):
                from camctl.outputs.read_attempts import PendingReadBusiness, held_binding_read_result

                business = PendingReadBusiness(runtime.outputs.fail_read_binding,
                    FailReadBinding(copy_id, source_id, result, now), new_operation_key())
                pending = await held_binding_read_result(runtime, copy_id, result, business)
                if pending is not None:
                    await _save_read_result_and_settle(runtime, pending)
                    continue
                if not await recover_unavailable_read(runtime, copy_id, DeviceBinding(device_id, driver_id), now):
                    continue
                from camctl.outputs.read_attempts import save_read_business

                failed = await save_read_business(runtime, copy_id, "read_binding_failure", FailReadBinding(
                    copy_id, source_id, result, now), runtime.outputs.fail_read_binding)
                if failed.kind is not DbOutcomeKind.COMPLETED:
                    raise ConsistencyError(f"读取绑定失败事务未完成（{failed.kind.value}）: {failed.error}")
                runtime.retry_gate.cleared(f"read/{copy_id}")
    parallel = frozenset(
        device_id for device_id, assembly in runtime.devices.items()
        if assembly.capture_read_parallel)
    work_items = await runtime.database.read(lambda owned: plan_device_work(
        owned.connection, now, capture_read_parallel=parallel, candidates=page.items))
    for work in work_items:
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
        return
    if saved_binding is None:
        raise ConsistencyError(f"读取源文件的原绑定缺失: {facts['source_device_file_id']}")
    if assembly.binding != DeviceBinding(*saved_binding):
        # 本次设备标识相同仍可能更换驱动；实际意图和调用只采用原绑定。
        return
    if await _read_wait_remaining(runtime, copy_id, assembly.retry_interval_s, assembly.max_read_attempts) \
            is not None:
        return
    slot = await _save_repository(runtime, runtime.outputs.grant_read_slot, SlotRequest(copy_id=copy_id, occurred_at=runtime.occurred_at()), new_operation_key())
    if slot.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(
            f"读取机会事务未完成（{slot.kind.value}）: {slot.error}")
    if slot.value.outcome in (SlotOutcome.WAIT, SlotOutcome.FINISHED):
        return
    ticket = await _begin_read_attempt(runtime, assembly, facts, now)
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


async def _read_wait_remaining(
        runtime: ObtainRuntime, copy_id: int, interval_s: Decimal, maximum: int | None = None,
) -> Decimal | None:
    """读取责任的重试等待剩余秒数；可开始下一次尝试时为空。"""
    responsibility = f"read/{copy_id}"
    row = await read_one(runtime.database, "SELECT attempts_used, retry_wait_required, max_attempts_used"
        " FROM operation_runs WHERE responsibility_key = ?", (responsibility,))
    if row is None:
        return None
    return runtime.retry_gate.remaining(
        responsibility,
        attempts_used=int(row[0]),
        retry_wait_required=int(row[1]) == 1,
        max_attempts_used=int(row[2]) if maximum is None else maximum,
        interval_s=interval_s,
        now_ns=runtime.monotonic_ns())


async def _begin_read_attempt(
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
    ticket, reason = await acquire_read_attempt(runtime, intent)
    if ticket is not None:
        return ticket
    if reason == "budget_exhausted":
        await _fail_read_delivery(runtime, facts, now)
    return None


async def _fail_read_delivery(
        runtime: ObtainRuntime, facts: Mapping, now: int, *,
        checksum_mismatch=False, source_verification_failed=False) -> None:
    """交付读取终局失败后释放读取机会及原源文件保护。"""
    from camctl.outputs.read_attempts import save_read_business

    await save_read_business(runtime, facts["id"], "fail_delivery",
        FailReadDelivery(delivery_id=facts["delivery_id"], occurred_at=now,
                         checksum_mismatch=checksum_mismatch,
                         source_verification_failed=source_verification_failed),
        runtime.outputs.fail_read_delivery)


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


async def _run_read(
        runtime: ObtainRuntime, assembly: DeviceReadAssembly,
        facts: Mapping, ticket, now: int) -> None:
    """一次读取尝试：摘要能力固定、会话拷贝与完整性收尾。"""
    from camctl.outputs.read_attempts import run_owned_read

    async def body(control):
        await _run_read_and_save(runtime, assembly, facts, ticket, now, control=control)

    await run_owned_read(runtime, facts["id"], body)


async def _run_read_and_save(runtime, assembly, facts, ticket, now, *, control=None):
    from camctl.outputs.read_attempts import hold_read_end

    device_file_id = facts["source_device_file_id"]
    await _ensure_checksum_support(runtime, assembly, device_file_id, now)
    digest = (
        await assembly.digest_for(device_file_id)
        if assembly.digest_supported and assembly.digest_for is not None
        else None)
    step = await _copy_delivery(
        runtime,
        DriverReadSessions(runtime.database, assembly.driver, ticket=ticket,
                           idle_timeout_s=assembly.read_idle_timeout_s, control=control),
        facts, digest, max_recopies=assembly.max_recopies,
        read_end=(runtime.pending_read_ends[facts["id"]].end if facts["id"] in runtime.pending_read_ends else None),
        on_read_end=lambda end, complete: hold_read_end(runtime, ticket, end, complete,
            resume=lambda current, held: _resume_local_delivery_end(current, held, runtime, assembly), evidence=assembly.evidence),
        on_read_start=lambda: runtime.pending_read_ends.pop(facts["id"], None))
    await _finish_read_attempt(runtime, assembly, facts, ticket, step)


async def _resume_local_delivery_end(current, held, original_runtime, assembly):
    """只核实原完整副本；原会话的设备结果不再调用源端口。"""
    from camctl.outputs.read_attempts import run_owned_read

    runtime = replace(original_runtime, database=current.database)
    copy_id = int(held.ticket.target_id)
    facts = await runtime.database.read(lambda owned: _read_facts(owned.connection, copy_id, include_terminal=True))
    if facts is None:
        raise ConsistencyError("原本地副本校验缺少所属交付")

    async def complete_and_save(_control):
        copies = RecordingInputCopies(runtime.database, BoundDirectories(staging=runtime.directories.staging),
            runtime.occurred_at, max_recopies=assembly.max_recopies, file_executor=runtime.file_executor)
        step = replace(await _complete_copy(copies, copy_id, None), read_end=held.end,
                       stop_requested=True, content_complete=True)
        await _finish_read_attempt(runtime, assembly, facts, held.ticket, step)

    await run_owned_read(runtime, copy_id, complete_and_save)


async def _copy_delivery(
        runtime: ObtainRuntime, sessions, facts: Mapping, digest, *, max_recopies: int = 1,
        read_end: ReadEnd | None = None, on_read_end=None, on_read_start=None,
) -> InputStep:
    """复用资格、续传、分段与完整性事务推进交付拷贝。"""
    copies: RecordingCopies = RecordingInputCopies(
        runtime.database, BoundDirectories(staging=runtime.directories.staging),
        runtime.occurred_at, max_recopies=max_recopies, file_executor=runtime.file_executor)
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
        raise ConsistencyError(f"读取的本地或保存前提未完成，保留原尝试: {step.phase.value}: {step.error}")
    succeeded = (step.phase is InputPhase.INPUT_READY
                 or checksum_failed or verification_failed)
    # 原 ticket 的连续 attempt_no 已在取得／恢复时核实为原累计次数。
    # 实际返回后先持有结果，不能为读取预算再访问数据库而丢失结果。
    exhausted = ticket.attempt_id >= assembly.max_read_attempts
    outcome = CallOutcome(
        status=(AttemptStatus.SUCCEEDED if succeeded
                else AttemptStatus.FAILED),
        error=None if succeeded else ErrorValue(
            code="device_error", stage="read", details={"reason": str(step.error)}),
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
            run_finish=run_finish))
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
    if pending.business is not None:
        from camctl.outputs.read_attempts import save_prepared_read_business

        copy_id = int(pending.finish.ticket.target_id)
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
            await _fail_read_delivery(runtime, facts, runtime.occurred_at(), checksum_mismatch=True)
            forget_read_result(runtime, pending)
            return
        if code == "source_file_unconfirmed":
            await _fail_read_delivery(
                runtime, facts, runtime.occurred_at(),
                source_verification_failed=True)
            forget_read_result(runtime, pending)
            return
    if succeeded:
        if facts["source_device_file_id"] is not None:
            from camctl.outputs.read_attempts import save_read_business

            released = await save_read_business(runtime, facts["id"], "release_read_slot",
                SlotRequest(copy_id=facts["id"], occurred_at=runtime.occurred_at()),
                runtime.outputs.release_read_slot, retry=retry, business_saves=pending.business_saves)
            if released.kind is not DbOutcomeKind.COMPLETED:
                raise ConsistencyError(f"原读取机会释放未可靠保存: {released.error}")
        runtime.retry_gate.cleared(f"read/{facts['id']}")
        forget_read_result(runtime, pending)
        return
    if exhausted:
        await _fail_read_delivery(runtime, facts, runtime.occurred_at())
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
                "SELECT relative_path FROM intermediate_files WHERE id = ?",
                (source_intermediate_file_id,))) as cursor:
                row = cursor.fetchone()
            qualification = read_dispatch_qualification(owned, self._ticket,
                source_intermediate_file_id, device_source=False)
            return row, qualification
        try:
            row, qualification = await self._database.read(read)
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
        return (facts, _is_inspection_copy_source(owned.connection, facts),
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
        ticket = await _begin_local_attempt(runtime, current, now, config)
        if ticket is None:
            return
        step = await _copy_delivery(runtime, sessions.for_attempt(ticket, control=control), current, None)
        await _finish_local_attempt(runtime, current, ticket, step, config)

    from camctl.outputs.read_attempts import run_owned_read

    await run_owned_read(runtime, copy_id, body)


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
        raise ConsistencyError(
            f"原读取源切换事务未可靠确认（{outcome.kind.value}）: {outcome.error}")
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
                "reason": (
                    str(step.error) if step.error is not None else None)}),
        effect=EffectState.UNKNOWN,
        settlement=Settlement(
            basis=SettlementBasis.OBSERVED,
            evidence=EvidenceValue(
                type="read_returned", version=1, data={})),
        observations=())
    from camctl.outputs.read_attempts import hold_read_result

    pending = hold_read_result(runtime, AttemptFinish(
            ticket=ticket,
            outcome=validate_outcome(ticket, outcome, _LOCAL_READ_EVIDENCE),
            occurred_at=runtime.occurred_at(),
            retry_wait=(not succeeded) and not exhausted,
            run_finish=(RunFinish(RunOutcome.FAILED, ErrorValue(
                "checksum_mismatch", "source_read", {
                    "max_recopies": step.error.max_recopies,
                    "recopies_used": step.error.recopies_used}))
                if checksum_failed else _run_finish(succeeded, exhausted))))
    await _save_read_result_and_settle(runtime, pending, facts)
    if succeeded:
        runtime.retry_gate.cleared(f"read/{facts['id']}")
        return
    if exhausted:
        return
    if fallback_after_source_failure and await runtime.database.read(
            lambda owned: _is_inspection_copy_source(owned.connection, facts)):
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
        raise ConsistencyError(f"摘要能力声明未完成: {outcome.error}")

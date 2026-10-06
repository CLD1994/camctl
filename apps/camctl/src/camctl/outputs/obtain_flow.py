"""取回动作执行编排：开始、来源固定、选择、建档、读取与发布。

每个推进轮次先开始到时的取回动作，再对执行中的动作按已保存事
实幂等推进：执行期来源解析一次固定，来源终态且产物处理完成后
固定选择，逐选中条目申请读取资格建档（主机源与设备源产物共同
建档），全部条目取得最终结果后统一发布成功交付并保存动作终
态。设备源拷贝的实际读取按统一设备工作计划推进：设备存在到时
拍摄、占用拍摄或让路中的在途读取时本轮不推进读取；空闲设备上
已建档拷贝经读取机会事务取得归属后，在读取尝试预算内打开设备
会话完成可靠分段拷贝与完整性收尾，预算耗尽把交付结束为终局失
败并交还机会。主机源拷贝不占用相机机会，其本地推进入口由本地
读取链路另行接入。
"""

from __future__ import annotations

import re
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping

from camctl.capture.files import FileChecksumSave
from camctl.capture.input_copy import (
    InputPhase, InputStep, RecordingCopies, RecordingSource)
from camctl.capture.media_flow import DriverReadSessions, RecordingInputCopies
from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import (
    ConsistencyError, new_operation_key)
from camctl.host_files.models import BoundDirectories
from camctl.operations.attempts import (
    AttemptConfig, AttemptFinish, AttemptIntent, AttemptTarget,
    BeginDisposition, OperationKind, RunFinish, RunOutcome, RetryWaitGate)
from camctl.operations.models import (
    AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue,
    Settlement, SettlementBasis)
from camctl.operations.validation import validate_outcome
from camctl.outputs.copy import (
    CompletionPhase, CopyCompletionError, CopyPreparationError,
    CopySegmentError, ResumeOutcome, SegmentOutcome,
    SegmentSaveDisposition)
from camctl.outputs.dispatch import plan_device_work
from camctl.outputs.handoff import (
    DeliveryContext, DeliveryDirectories, DeliveryPhase, publish_delivery)
from camctl.outputs.qualification import (
    FileCandidate, OperationConfig, QualificationOutcome)
from camctl.outputs.slots import SlotOutcome, SlotRequest
from camctl.outputs.sources import (
    ResolutionState, SelectionSnapshot, SourceResolution, SourceSpec,
    select_outputs)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import (
    FailReadDelivery, FinishObtain, FixSelection, OutputsRepository,
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

#: 交付扩展名的安全字符集；设备原始名称不满足时按未知类型处理。
_SAFE_EXTENSION = re.compile(r"[A-Za-z0-9]+")
_UNKNOWN_EXTENSION = "bin"

#: 第一版设备源读取的固定默认值（与媒体链读取一致）。
_READ_MAX_ATTEMPTS = 3
_READ_TIMEOUT_S = Decimal("60")


@dataclass(frozen=True)
class DeviceReadAssembly:
    """一台设备的读取协作者；装配层按登记驱动解析。"""

    driver: Any
    binding: Any
    evidence: Any
    digest_supported: bool
    digest_for: Callable[[int], Any] | None
    retry_interval_s: Decimal


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


async def advance_obtain(runtime: ObtainRuntime) -> None:
    """推进一个轮次的取回执行链；各步按已保存事实幂等。"""
    now = runtime.occurred_at()
    _start_due(runtime, now)
    for action_id in _running_actions(runtime.owned.connection):
        await _advance_action(runtime, action_id, now)
    await _advance_reads(runtime, now)


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
        action, item_id, output_id, intermediate_file_id, now,
        plan_name, source_action_name)


def _device_source_candidate(
        runtime: ObtainRuntime, action: Mapping, item_id: int,
        output_id: int, device_file_id: int, now: int,
        plan_name: str, source_action_name: str,
) -> FileCandidate:
    connection = runtime.owned.connection
    with closing(connection.execute(
        "SELECT f.original_name, a.device_id FROM device_files f"
        " JOIN actions a ON a.id = f.observer_action_id WHERE f.id = ?",
        (device_file_id,),
    )) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError(f"产物源文件缺失: {device_file_id}")
    original_name, device_id = row
    assembly = runtime.devices.get(device_id)
    if assembly is None:
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
        config=OperationConfig(
            max_attempts=_READ_MAX_ATTEMPTS,
            timeout_s=_READ_TIMEOUT_S,
            retry_interval_s=assembly.retry_interval_s),
        occurred_at=now)


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
        directories=runtime.directories, occurred_at=now))
    if result.phase not in (
            DeliveryPhase.PUBLISHED, DeliveryPhase.FAILED_FINAL,
            DeliveryPhase.NOT_ACTIVE):
        raise ConsistencyError(
            f"交付发布处于未决状态: delivery={delivery_id}"
            f" phase={result.phase.value}")


# ---- 读取推进（统一设备工作计划消费） ----


async def _advance_reads(runtime: ObtainRuntime, now: int) -> None:
    """按设备工作计划推进读取；拍摄工作在时本轮不推进。

    新授予的候选与已持有机会的在途读取共同推进：在途读取继续占
    用机会直至副本完成或终局失败，重试等待的间隔判定在逐拷贝推
    进内完成。
    """
    connection = runtime.owned.connection
    for work in plan_device_work(connection, now):
        if work.dispatch_captures or work.yield_reads:
            continue
        assembly = runtime.devices.get(work.device_id)
        if assembly is None:
            continue
        for copy_id in (*work.grant_reads, *work.resume_reads):
            await _advance_read(runtime, assembly, copy_id, now)


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
    if _read_wait_remaining(runtime, copy_id, assembly.retry_interval_s) \
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


def _read_facts(connection, copy_id: int) -> dict | None:
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
    if facts["delivery_status"] in (
            int(_DELIVERY_STATUS.FAILED), int(_DELIVERY_STATUS.CANCELED),
            int(_DELIVERY_STATUS.WITHDRAWN)):
        return None
    return facts


def _read_wait_remaining(
        runtime: ObtainRuntime, copy_id: int, interval_s: Decimal,
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
        max_attempts_used=int(row[2]),
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
            max_attempts=_READ_MAX_ATTEMPTS,
            timeout_s=_READ_TIMEOUT_S,
            retry_interval_s=assembly.retry_interval_s),
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
        _fail_read_delivery(runtime, facts, now)
    return None


def _fail_read_delivery(
        runtime: ObtainRuntime, facts: Mapping, now: int) -> None:
    """读取预算耗尽：交付终局失败后释放读取机会。"""
    failure = runtime.outputs.fail_read_delivery(
        FailReadDelivery(delivery_id=facts["delivery_id"], occurred_at=now),
        new_operation_key(), runtime.owned)
    if failure.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(
            f"读取耗尽失败事务未完成（{failure.kind.value}）:"
            f" {failure.error}")
    runtime.outputs.release_read_slot(
        SlotRequest(copy_id=facts["id"], occurred_at=now),
        new_operation_key(), runtime.owned)


async def _run_read(
        runtime: ObtainRuntime, assembly: DeviceReadAssembly,
        facts: Mapping, ticket, now: int) -> None:
    """一次读取尝试：摘要能力固定、会话拷贝与完整性收尾。"""
    device_file_id = facts["source_device_file_id"]
    _ensure_checksum_support(runtime, assembly, device_file_id, now)
    digest = (
        assembly.digest_for(device_file_id)
        if assembly.digest_supported and assembly.digest_for is not None
        else None)
    step = await _copy_delivery(
        runtime,
        DriverReadSessions(runtime.owned, assembly.driver, ticket=ticket),
        facts, digest)
    _finish_read_attempt(runtime, assembly, facts, ticket, step)


async def _copy_delivery(
        runtime: ObtainRuntime, sessions, facts: Mapping, digest,
) -> InputStep:
    """复用资格、续传、分段与完整性事务推进交付拷贝。"""
    copies: RecordingCopies = RecordingInputCopies(
        runtime.owned, BoundDirectories(staging=runtime.directories.staging),
        runtime.occurred_at)
    state = copies.copy_state(facts["id"])
    if (state.verification_state in _VERIFICATION_DONE
            and state.target_sha256 is not None):
        return InputStep(InputPhase.INPUT_READY, copy_id=facts["id"])
    try:
        prepared = await copies.prepare(facts["id"])
    except (CopyPreparationError, ConsistencyError) as error:
        return InputStep(InputPhase.PREPARE_FAILED, copy_id=facts["id"],
                         error=error)
    if prepared.decision.outcome is ResumeOutcome.VERIFY:
        return await _complete_copy(copies, facts["id"], digest)
    try:
        session = await sessions.open_session(
            facts["source_device_file_id"], prepared.decision.offset)
    except Exception as error:
        # 打开会话也是一次设备读取调用：按读取失败保存本次尝试。
        return InputStep(InputPhase.SEGMENT_FAILED, copy_id=facts["id"],
                         error=error)
    try:
        while True:
            step = await copies.transfer(
                facts["id"], session, runtime.segment_size)
            if step.plan.outcome is SegmentOutcome.ALL_COMMITTED:
                break
            if step.saved is None:
                raise ConsistencyError(
                    "段推进未携带保存事实，拷贝端口契约不一致")
            if step.saved.disposition is SegmentSaveDisposition.SKIPPED:
                return InputStep(
                    InputPhase.OWNER_SKIPPED, copy_id=facts["id"],
                    reason=step.saved.reason)
    except (CopySegmentError, ConsistencyError) as error:
        return InputStep(InputPhase.SEGMENT_FAILED, copy_id=facts["id"],
                         error=error)
    finally:
        session.request_stop()
        await session.wait_stopped()
    return await _complete_copy(copies, facts["id"], digest)


async def _complete_copy(
        copies: RecordingCopies, copy_id: int, digest) -> InputStep:
    """完整性收尾：准备完成或登记新一轮重拷。"""
    try:
        completion = await copies.complete(copy_id, digest)
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
    succeeded = step.phase is InputPhase.INPUT_READY
    exhausted = _attempts_exhausted(runtime, facts["id"])
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
    finish = runtime.operations.finish_attempt(
        AttemptFinish(
            ticket=ticket,
            outcome=validate_outcome(ticket, outcome, assembly.evidence),
            occurred_at=runtime.occurred_at(),
            retry_wait=(not succeeded) and not exhausted,
            run_finish=_run_finish(succeeded, exhausted)),
        new_operation_key(), runtime.owned)
    if finish.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(
            f"读取尝试收尾事务未完成（{finish.kind.value}）:"
            f" {finish.error}")
    if succeeded:
        runtime.outputs.release_read_slot(
            SlotRequest(copy_id=facts["id"],
                        occurred_at=runtime.occurred_at()),
            new_operation_key(), runtime.owned)
        runtime.retry_gate.cleared(f"read/{facts['id']}")
        return
    if exhausted:
        _fail_read_delivery(runtime, facts, runtime.occurred_at())
        return
    runtime.retry_gate.established(
        f"read/{facts['id']}", runtime.monotonic_ns())


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

"""设备文件读取的原尝试选择；连接恢复不消耗新的读取次数。"""

from contextlib import closing
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from camctl.capture.recovery import RecoveryBoundary, RecoveryBlockedReason, RecoveryDiagnostic
from camctl.contracts.values import ConsistencyError, OperationKey, new_operation_key
from camctl.operations.attempts import (
    AttemptFinish, BeginDisposition, FinishDisposition, ReadResumeDisposition, ReadResumeRequest, RunFinish, RunOutcome, RunStatus,
)
from camctl.operations.models import (
    AttemptStatus, AttemptTicket, CallOutcome, EffectState, ErrorValue,
    EvidenceValue, Settlement, SettlementBasis,
)
from camctl.operations.validation import validate_outcome
from camctl.operations.error_diagnostics import exception_diagnostic
from camctl.persistence.models import DbOutcomeKind, db_failure, db_outcome_for
from camctl.persistence.business import SharedSave, settle_required
from camctl.outputs.database_reads import read_one
from camctl.devices.evidence import EvidenceError
from camctl.logging_runtime.diagnostics import (
    DiagnosticState, diagnostic_state_for, record_call_outcome, record_error,
)


def read_failure_details(step) -> dict:
    """原读取失败保留可读原因、完整异常及已取得的结束事实。"""
    details = {"reason": str(step.error) if step.error is not None else None}
    diagnostic = exception_diagnostic(step.error)
    if diagnostic is not None:
        details["exception"] = diagnostic
    if step.read_end is not None:
        details["read_end"] = {
            "bytes_read": step.read_end.bytes_read,
            "error": step.read_end.error,
            "stopped": step.read_end.stopped,
        }
    return details


@dataclass(frozen=True)
class ReadContinuation:
    """本会话可证明的续读身份；未派发事实只由新意图可靠回执建立。"""

    ticket: AttemptTicket
    never_dispatched: bool
    step: Any = None
    occurred_at: int | None = None
    result_key: OperationKey | None = None
    evidence: Any = None
    inspection_cancel_recovery: bool = False


@dataclass(frozen=True)
class ReadDispatchPrevented:
    """实际源尚未打开，最终资格或文件任务停止阻止了本次调用。"""

    reason: str
    canceled: bool = False
    terminal: bool = False


def read_dispatch_qualification(owned, ticket, source_id, *, device_source):
    """在源事实所属callback内复核原票据、当前来源和业务资格。"""
    from camctl.persistence.repositories.outputs import _read_owner_eligible
    from camctl.outputs.read_discovery import read_copy_dispatch_eligible
    from camctl.contracts.enums import enum_for

    with closing(owned.connection.execute(
        "SELECT a.status,a.cancel_requested,r.status,r.attempts_used,"
        "t.status,t.result_json,c.source_device_file_id,c.source_intermediate_file_id,"
        "c.slot_device_id FROM operation_runs r JOIN actions a ON a.id=r.action_id"
        " JOIN file_copies c ON c.id=r.copy_id"
        " JOIN operation_attempts t ON t.run_id=r.id AND t.attempt_no=?"
        " WHERE r.id=? AND c.id=? AND r.responsibility_key=?",
        (ticket.attempt_id, ticket.run_id, int(ticket.target_id), ticket.responsibility_key),
    )) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError("实际读取入口缺少原票据的业务归属")
    status, canceled, run_status, used, attempt_status, result, device_id, host_id, slot = row
    runs, attempts = enum_for("operation_runs.status"), enum_for("operation_attempts.status")
    if (run_status not in (int(runs.PENDING), int(runs.ACTIVE))
            or attempt_status != int(attempts.RUNNING) or result is not None):
        return ReadDispatchPrevented("run_ended", terminal=True)
    if canceled == 1:
        return ReadDispatchPrevented("canceled_before_dispatch", canceled=True)
    if not _read_owner_eligible({"status": status, "cancel_requested": canceled}):
        return ReadDispatchPrevented("owner_not_running")
    if used != ticket.attempt_id:
        raise ConsistencyError("实际读取入口的原尝试与累计次数不符")
    if (device_id if device_source else host_id) != source_id:
        raise ConsistencyError("实际读取入口的来源与原拷贝不符")
    if device_source and slot is None:
        return ReadDispatchPrevented("read_slot_not_held")
    if not device_source and slot is not None:
        raise ConsistencyError("主机源读取不能持有相机机会")
    if device_source:
        from camctl.persistence.transaction import row_facts
        from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
        source = row_facts(owned.connection, "device_files", source_id)
        if source is None:
            raise ConsistencyError("实际读取缺少原设备文件")
        observer = row_facts(owned.connection, "actions", source["observer_action_id"])
        origin = row_facts(owned.connection, "actions", source["source_action_id"])
        if observer is None or origin is None:
            raise ConsistencyError("实际读取缺少原设备绑定")
        identity = None
        if observer["driver_id"] == DRIVER_ID:
            from camctl.persistence.repositories.physical_alias import source_physical_alias_conflict, source_physical_identity
            if source_physical_alias_conflict(owned.connection, source, observer, origin):
                return ReadDispatchPrevented("physical_identity_conflict")
            identity = source_physical_identity(owned.connection, source, observer, origin)
        from camctl.persistence.repositories.preparation import PreparationReads
        if PreparationReads(owned.connection).availability_in_flight(observer["device_id"], identity):
            return ReadDispatchPrevented("device_busy")
    if not read_copy_dispatch_eligible(owned.connection, int(ticket.target_id)):
        return ReadDispatchPrevented("read_input_not_needed")
    return None


def read_dispatch_gate(qualification, control):
    """数据库等待结束后同步观察停止；调用者紧接着标记并打开源。"""
    if qualification is not None:
        return qualification
    if control is not None and control.stop_requested:
        return ReadDispatchPrevented("read_stopped_before_dispatch")
    return None


def prevented_read_result(runtime, ticket, prevented, evidence):
    """按原是否调用及实际结束事实形成结果；首个DB await之前持有原申请。"""
    from camctl.devices.evidence import EvidenceContract

    continuation = runtime.continuing_read_tickets.get(int(ticket.target_id))
    if prevented.terminal:
        if continuation is not None and continuation.ticket == ticket and continuation.never_dispatched:
            del runtime.continuing_read_tickets[int(ticket.target_id)]
        return None
    held = runtime.pending_read_ends.get(int(ticket.target_id))
    if held is not None and held.ticket == ticket and prevented.canceled:
        from camctl.capture.input_copy import InputStep, InputPhase
        continuation = ReadContinuation(ticket, False, InputStep(InputPhase.INPUT_READY,
            read_end=held.end, content_complete=True), held.occurred_at,
            result_key=continuation.result_key if continuation is not None else None, evidence=held.evidence)
    if continuation is None or continuation.ticket != ticket:
        return None
    if continuation.never_dispatched:
        evidence = evidence.with_contract(EvidenceContract("dispatch_prevented", 1, "read", frozenset()))
        outcome = CallOutcome(status=AttemptStatus.FAILED, effect=EffectState.NO_EFFECT,
            error=ErrorValue(prevented.reason, "read"),
            settlement=Settlement(SettlementBasis.NOT_DISPATCHED, EvidenceValue("dispatch_prevented", 1, {})))
        occurred_at = runtime.occurred_at()
    elif prevented.canceled:
        step = continuation.step
        if step is None or step.read_end is None or step.read_end.stopped is not True:
            return None
        end = step.read_end
        evidence = continuation.evidence if continuation.evidence is not None else evidence
        succeeded = end.error is None and step.content_complete
        outcome = CallOutcome(status=AttemptStatus.SUCCEEDED if succeeded else AttemptStatus.UNKNOWN,
            effect=EffectState.UNKNOWN,
            error=None if succeeded else ErrorValue("read_stopped", "read"),
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("read_returned", 1, {})))
        occurred_at = continuation.occurred_at
    else:
        return None
    return hold_read_result(runtime, AttemptFinish(ticket, validate_outcome(ticket, outcome, evidence),
        occurred_at, retry_wait=not prevented.canceled,
        run_finish=RunFinish(RunOutcome.CANCELED) if prevented.canceled else None), key=continuation.result_key)


async def prepare_prevented_read_result(runtime, ticket, prevented, evidence):
    pending = prevented_read_result(runtime, ticket, prevented, evidence)
    if pending is not None or not prevented.canceled or prevented.terminal:
        return pending
    # 没有本会话实际结束事实的旧票据仍须经过原冷恢复边界与驱动证据。
    original = await runtime.database.read(lambda owned: running_read_ticket(owned, int(ticket.target_id)))
    if original is None:
        return None
    if original[0] != ticket:
        raise ConsistencyError("阻止续读时的原票据发生变化")
    if not read_host_boundary(runtime, ticket, original[1], continuing=False):
        return None
    try:
        contract = evidence.contract("local_read_recovery", 1)
    except EvidenceError:
        try:
            contract = evidence.contract("adb_foreground_recovery", 1)
        except EvidenceError:
            record_read_diagnostic(runtime, RecoveryBlockedReason.EVIDENCE_UNAVAILABLE, ticket)
            return None
    outcome = CallOutcome(status=AttemptStatus.UNKNOWN, effect=EffectState.UNKNOWN,
        error=ErrorValue("result_not_saved", "recovery"),
        settlement=Settlement(SettlementBasis.ASSUMED, EvidenceValue.of(contract)))
    return hold_read_result(runtime, AttemptFinish(ticket, validate_outcome(ticket, outcome, evidence),
        runtime.occurred_at(), run_finish=RunFinish(RunOutcome.CANCELED)))


@dataclass(frozen=True)
class PendingReadResult:
    """实际读取结束后持有的原结果和完整提交身份。"""

    finish: AttemptFinish
    key: OperationKey
    business: "PendingReadBusiness | None" = None
    result_save: SharedSave = field(default_factory=SharedSave, compare=False, repr=False)
    settlement: SharedSave = field(default_factory=SharedSave, compare=False, repr=False)
    business_saves: dict[tuple[int, str], "PendingReadBusiness"] = field(
        default_factory=dict, compare=False, repr=False)
    diagnostic: DiagnosticState = field(default_factory=DiagnosticState, compare=False, repr=False)
    preserved_run_terminal: bool = False
    binding_read: bool = False


@dataclass(frozen=True)
class HeldReadEnd:
    ticket: AttemptTicket
    end: Any
    occurred_at: int
    resume: Callable | None = None
    evidence: Any = None


def hold_read_end(runtime, ticket, end, complete, *, resume=None, evidence=None):
    if end.stopped is True and end.error is None and complete:
        copy_id = int(ticket.target_id)
        held = runtime.pending_read_ends.get(copy_id)
        if held is not None and (held.ticket != ticket or held.end != end):
            raise ConsistencyError("原完整读取结束事实与已持有的返回不一致")
        runtime.pending_read_ends[copy_id] = HeldReadEnd(ticket, end,
            held.occurred_at if held is not None else runtime.occurred_at(),
            resume if resume is not None else (held.resume if held is not None else None),
            evidence if evidence is not None else (held.evidence if held is not None else None))


async def persist_read_end(runtime, ticket, end, complete, *, state, source_device_file_id,
                           source_intermediate_file_id, resume=None, evidence=None):
    """先持有原返回，再沿同一申请及操作键可靠保存。"""
    hold_read_end(runtime, ticket, end, complete, resume=resume, evidence=evidence)
    copy_id = int(ticket.target_id)
    held = runtime.pending_read_ends.get(copy_id)
    if held is None or held.ticket != ticket or held.end != end:
        return
    from camctl.persistence.repositories.read_end import SaveReadEnd, save_read_end

    pending = runtime.pending_read_business.get((copy_id, "read_end"))
    if pending is None:
        original = SaveReadEnd(ticket, state.round, source_device_file_id,
            source_intermediate_file_id, state.target.file_id, state.source_size, end, held.occurred_at)
    else:
        original = pending.request
    await save_read_business(runtime, copy_id, "read_end", original, save_read_end, retry=pending is not None)


async def retry_read_ends(runtime):
    """真实完整结束与必要摘要已取得时，先核实原副本并保存原结果。"""
    from camctl.outputs.copy import SourceChecksumSupport
    from camctl.contracts.enums import enum_for
    from camctl.persistence.repositories.outputs import OutputsRepository

    verification = enum_for("file_copies.verification_state")
    for copy_id, held in tuple(runtime.pending_read_ends.items()):
        facts = await runtime.database.read(lambda owned: OutputsRepository().load_copy_state(copy_id, owned))
        saved_source_failure = False
        if (facts.verification_state == int(verification.FAILED)
                and facts.source_support is SourceChecksumSupport.SUPPORTED
                and facts.source_sha256 is None and facts.target_sha256 is not None):
            row = await read_one(runtime.database, "SELECT verification_error_json FROM file_copies WHERE id=?", (copy_id,))
            saved_source_failure = row is not None and row[0] is not None
        if (facts.committed_bytes != facts.source_size
                or (facts.source_support is not SourceChecksumSupport.UNSUPPORTED
                    and facts.source_sha256 is None and not saved_source_failure)):
            continue
        original = await runtime.database.read(lambda owned: running_read_ticket(owned, copy_id))
        if original is None or original[0] != held.ticket:
            raise ConsistencyError("已持有的实际读取结束与原未完成尝试不符")
        if held.resume is None:
            raise ConsistencyError("原实际读取结束缺少本地校验续接责任")
        await held.resume(runtime, held)


async def current_local_read_end(runtime, copy_id, held):
    """取得文件资格后重新核对原票据及当前轮次的本机结束依据。"""
    from camctl.persistence.repositories.read_end import load_read_end

    def read(owned):
        original = running_read_ticket(owned, copy_id)
        if original is None:
            return False
        if original[0] != held.ticket:
            raise ConsistencyError("本机续接的原读取票据已经改变")
        saved = load_read_end(copy_id, owned)
        if saved is None:
            return False
        if saved[0] != held.ticket or saved[1] != held.end:
            raise ConsistencyError("本机续接的当前轮结束依据已经改变")
        return saved[3] is None

    return await runtime.database.read(read)


async def source_verification_run_finish(runtime, ticket, step, *, output_id=None):
    """确认无错误的原 ReadEnd 对应已保存的源摘要查询终局失败。"""
    from camctl.capture.input_copy import InputPhase
    from camctl.contracts.json_values import parse_exact_json
    from camctl.contracts.enums import enum_for
    from camctl.devices.read_session import ReadEnd
    from camctl.outputs.copy import SourceChecksumSupport
    from camctl.persistence.repositories.outputs import OutputsRepository

    verification = enum_for("file_copies.verification_state")
    run_statuses = enum_for("operation_runs.status")
    if step.phase is not InputPhase.VERIFICATION_FAILED:
        raise ConsistencyError("源摘要失败收场必须来自明确的校验失败阶段")
    end = step.read_end
    if (not isinstance(end, ReadEnd) or end.stopped is not True or end.error is not None
            or step.stop_requested is not True or step.content_complete is not True):
        raise ConsistencyError("源摘要失败收场缺少无错误的原 ReadEnd 或完整内容事实")

    copy_id = int(ticket.target_id)
    facts = await runtime.database.read(lambda owned: OutputsRepository().load_copy_state(copy_id, owned))
    row = await read_one(runtime.database, "SELECT c.verification_error_json,c.delivery_id,c.source_device_file_id,"
        " d.output_id,r.status,a.cancel_requested"
        " FROM file_copies c JOIN operation_runs r ON r.copy_id=c.id AND r.id=?"
        " JOIN actions a ON a.id=r.action_id"
        " LEFT JOIN deliveries d ON d.id=c.delivery_id"
        " WHERE c.id=?", (ticket.run_id, copy_id))
    if row is None:
        raise ConsistencyError("源摘要失败收场缺少原 READ 流程归属")
    error_json, delivery_id, source_id, saved_output_id, run_status, canceled = row
    if (facts.committed_bytes != facts.source_size
            or facts.source_support is not SourceChecksumSupport.SUPPORTED
            or facts.source_sha256 is not None or facts.target_sha256 is None
            or facts.verification_state != int(verification.FAILED) or source_id is None
            or error_json is None):
        raise ConsistencyError("源摘要失败收场与已保存的完整性事实不一致")
    error = parse_exact_json(error_json)
    if not isinstance(error, dict) or not isinstance(error.get("reason"), str):
        raise ConsistencyError("源摘要失败收场缺少已保存的查询错误")
    if delivery_id is None:
        if output_id is not None:
            raise ConsistencyError("内部读取不能携带取回交付的产物身份")
        details = {}
    else:
        if type(output_id) is not int or output_id != saved_output_id:
            raise ConsistencyError("源摘要失败收场的正式产物身份与交付不一致")
        details = {"output_id": str(output_id)}

    run_status = int(run_status)
    if run_status not in (
            int(run_statuses.PENDING), int(run_statuses.ACTIVE)):
        return None
    if canceled == 1:
        return RunFinish(RunOutcome.CANCELED)
    return RunFinish(
        RunOutcome.FAILED,
        ErrorValue("source_file_unconfirmed", "source_read", details),
    )


async def canceled_complete_read_result(runtime, ticket, evidence):
    """完整真实结束优先；尚未结束的副本业务按可靠取消收场。"""
    held = runtime.pending_read_ends.get(int(ticket.target_id))
    if held is None or held.ticket != ticket:
        return None
    row = await read_one(runtime.database, "SELECT a.cancel_requested,r.status FROM operation_runs r JOIN actions a ON a.id=r.action_id"
        " WHERE r.id=? AND r.copy_id=?", (ticket.run_id, int(ticket.target_id)))
    if row is None or row[0] not in (0, 1):
        raise ConsistencyError("原完整读取结束缺少可靠业务归属")
    if row[0] == 0:
        return None
    outcome = CallOutcome(status=AttemptStatus.SUCCEEDED, effect=EffectState.UNKNOWN,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("read_returned", 1, {})))
    return hold_read_result(runtime, AttemptFinish(ticket, validate_outcome(ticket, outcome, evidence),
        runtime.occurred_at(), run_finish=RunFinish(RunOutcome.CANCELED) if row[1] in (1, 2) else None))


@dataclass(frozen=True)
class PendingReadBusiness:
    """读取结束后的业务事务；未知提交按原申请和键核实。"""

    save: Callable[[Any, OperationKey, Any], Any]
    request: Any
    key: OperationKey
    on_completed: Callable[[Any], None] | None = None
    shared: SharedSave = field(default_factory=SharedSave, compare=False, repr=False)
    diagnostic: DiagnosticState = field(default_factory=DiagnosticState, compare=False, repr=False)
    inspection_cancel_recovery: list = field(default_factory=list, compare=False, repr=False)


async def retry_read_business(runtime):
    for identity, pending in tuple(runtime.pending_read_business.items()):
        await _save_pending_read_business(runtime, identity, pending, retry=True)


async def save_read_business(runtime, copy_id, operation, request, save, *, on_completed=None, retry=False,
                             business_saves=None):
    """共同等待原申请的保存与应用；明确恢复时才重新核实失败结果。"""
    identity = (copy_id, operation)
    # 原结果仍持有后续各申请；全局恢复完成并删除活动责任后也能交付原回执。
    pending = (business_saves.get(identity) if business_saves is not None else None)
    if pending is None:
        pending = runtime.pending_read_business.get(identity)
    if pending is None:
        pending = PendingReadBusiness(save, request, new_operation_key(), on_completed)
        runtime.pending_read_business[identity] = pending
    if business_saves is not None:
        business_saves[identity] = pending
    return await _save_pending_read_business(runtime, identity, pending, retry=retry)


async def _save_pending_read_business(runtime, identity, pending, *, retry=False):
    hold_prepared_read_business(runtime, identity[0], identity[1], pending)
    _record_read_business_error(pending)
    # 已完成的控制错误也属于原回执；只有仍未收场的业务责任可以重新核实。
    if retry and runtime.pending_read_business.get(identity) is pending:
        pending.shared.retry_failed()

    def apply_completed(receipt):
        if pending.on_completed is not None:
            pending.on_completed(receipt.value)
        del runtime.pending_read_business[identity]

    async def save_and_apply():
        held = runtime.pending_read_business.get(identity)
        if held is not None and held is not pending:
            raise ConsistencyError("原读取结束所属申请与已持有的申请不同")
        runtime.pending_read_business[identity] = pending
        try:
            receipt = await runtime.database.write(
                lambda owned: pending.save(pending.request, pending.key, owned), key=pending.key)
        except BaseException as error:
            actual = db_outcome_for(error)
            if actual is not None and actual.kind is DbOutcomeKind.COMPLETED:
                apply_completed(actual)
            raise
        if receipt.kind is not DbOutcomeKind.COMPLETED:
            raise db_failure(ConsistencyError,
                f"原读取业务结果未可靠保存（{receipt.kind.value}）: {receipt.error}", receipt)
        apply_completed(receipt)
        return receipt
    return await pending.shared.run(save_and_apply)


def _record_read_business_error(pending):
    """原绑定失败随完整业务申请持有，不依赖保存成功或恢复作用域。"""
    from camctl.devices.bindings import binding_failure_details
    from camctl.persistence.repositories.outputs import FailReadBinding
    from camctl.persistence.repositories.capture import FinishBindingFailure

    request = pending.request
    if isinstance(request, FailReadBinding):
        error = ErrorValue("device_binding_unavailable", "execution",
                           binding_failure_details(request.binding_result))
        context = {"copy_id": request.copy_id,
                   "source_device_file_id": request.source_device_file_id}
    elif isinstance(request, FinishBindingFailure):
        error = ErrorValue(request.failure.code, "execution", request.failure.details)
        context = {"action_id": request.action_id}
    else:
        return
    record_error(error, stage=error.stage, context=context, state=pending.diagnostic)


async def save_prepared_read_business(runtime, copy_id, operation, pending, *, retry=False):
    """完整结束结果预先确定的原业务申请，可靠结果之后才提交。"""
    identity = (copy_id, operation)
    return await _save_pending_read_business(runtime, identity, pending, retry=retry)


def hold_prepared_read_business(runtime, copy_id, operation, pending):
    """在交接诊断和释放原结果前持有原业务申请。"""
    identity = (copy_id, operation)
    held = runtime.pending_read_business.get(identity)
    if held is not None and held is not pending:
        raise ConsistencyError("原读取结束所属申请与已持有的申请不同")
    runtime.pending_read_business[identity] = pending


async def held_binding_read_result(runtime, copy_id, binding_result, business):
    """可靠源实际完成、必要源摘要未取得时分别记录调用成功和绑定失败。"""
    from camctl.devices.bindings import binding_failure_details
    from camctl.outputs.copy import SourceChecksumSupport
    from camctl.persistence.repositories.outputs import OutputsRepository

    def determined():
        matches = [pending for pending in runtime.pending_read_results.values()
                   if int((pending.ticket if isinstance(pending, PendingStoppedRead)
                           else pending.finish.ticket).target_id) == copy_id]
        if len(matches) > 1:
            raise ConsistencyError("同一读取具有多个已确定结束申请")
        return matches[0] if matches else None

    pending = determined()
    if pending is not None:
        return pending
    held = runtime.pending_read_ends.get(copy_id)
    if held is None:
        return None
    facts = await runtime.database.read(lambda owned: OutputsRepository().load_copy_state(copy_id, owned))
    pending = determined()
    if pending is not None:
        return pending
    original = await runtime.database.read(lambda owned: running_read_ticket(owned, copy_id))
    pending = determined()
    if pending is not None:
        return pending
    if original is None or original[0] != held.ticket or held.end.stopped is not True or held.end.error is not None:
        raise ConsistencyError("原必要摘要绑定失败缺少可靠原读取结束")
    if held.evidence is None:
        raise ConsistencyError("原实际读取结束缺少原驱动结果契约")
    status = await read_one(runtime.database,
        "SELECT r.status,a.cancel_requested FROM operation_runs r JOIN actions a ON a.id=r.action_id"
        " WHERE r.id=? AND r.copy_id=?", (held.ticket.run_id, copy_id))
    pending = determined()
    if pending is not None:
        return pending
    if runtime.pending_read_ends.get(copy_id) is not held:
        return None
    if status is None or status[0] not in (1, 2, 3, 4, 5, 6, 7) or status[1] not in (0, 1):
        raise ConsistencyError("原完整读取的流程状态或取消事实不可解释")
    if facts.committed_bytes != facts.source_size:
        return None
    binding_read = False
    if status[0] not in (1, 2):
        run_finish, selected_business = None, None
    elif status[1] == 1 and (facts.source_support is not SourceChecksumSupport.SUPPORTED
                            or facts.source_sha256 is not None):
        run_finish, selected_business = RunFinish(RunOutcome.CANCELED), None
    else:
        if (facts.source_support is not SourceChecksumSupport.SUPPORTED
                or facts.source_sha256 is not None):
            return None
        details = binding_failure_details(binding_result)
        if details is None:
            raise ConsistencyError("必要摘要绑定失败必须具有原绑定异常事实")
        run_finish = RunFinish(RunOutcome.FAILED, ErrorValue("device_binding_unavailable", "execution", details))
        selected_business = business
        binding_read = True
    outcome = CallOutcome(status=AttemptStatus.SUCCEEDED, effect=EffectState.UNKNOWN,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("read_returned", 1, {})))
    finish = AttemptFinish(held.ticket, validate_outcome(held.ticket, outcome, held.evidence),
        held.occurred_at if binding_read else runtime.occurred_at(),
        run_finish=run_finish)
    key = new_operation_key()
    if binding_read and isinstance(selected_business, PendingReadBusiness):
        from camctl.persistence.repositories.outputs import FailReadBinding
        from camctl.persistence.repositories.capture import FinishBindingFailure
        request = selected_business.request
        if isinstance(request, FailReadBinding):
            request = replace(request, read_result_key=key)
        elif isinstance(request, FinishBindingFailure):
            request = replace(request, read_result_key=key, read_copy_id=copy_id)
        else:
            raise ConsistencyError("原读取绑定业务申请类型不符")
        selected_business = replace(selected_business, request=request)
    return hold_read_result(runtime, finish, business=selected_business, key=key,
                            preserved_run_terminal=status[0] not in (1, 2),
                            binding_read=binding_read, suppress_diagnostic=binding_read)


@dataclass(frozen=True)
class PendingStoppedRead:
    """源实际停止后，先持有原观察，再读取所属业务取消事实。"""

    ticket: AttemptTicket
    step: object
    evidence: object
    occurred_at: int
    key: OperationKey
    result_save: SharedSave = field(default_factory=SharedSave, compare=False, repr=False)
    settlement: SharedSave = field(default_factory=SharedSave, compare=False, repr=False)
    diagnostic: DiagnosticState = field(default_factory=DiagnosticState, compare=False, repr=False)


def controlled_read_stop(step):
    """明确停止通知可作控制分支；同次实际写入、同步或关闭错误仍是错误。"""
    from camctl.outputs.copy import CopySegmentError
    from camctl.host_files.segments import SegmentFailure

    if (step.source_failed or step.read_end is None or step.read_end.error != "stopped"
            or not step.stop_requested):
        return False
    error = step.error
    if error is None or (type(error) is RuntimeError and error.args == ("stopped",)):
        return True
    if isinstance(error, CopySegmentError):
        result = error.result
        return ((result is None and error.detail == "stopped") or
                (result is not None and result.error is SegmentFailure.STOPPED
                 and result.failure is None and result.source_close_error is None))
    return False


def stopped_read_result(runtime, ticket, step, evidence, occurred_at):
    if (step.source_failed or step.read_end is None or step.read_end.error != "stopped"
            or not step.stop_requested or step.content_complete):
        return None
    identity = (ticket.run_id, ticket.attempt_id)
    if identity in runtime.pending_read_results:
        raise ConsistencyError("同一原读取已有待保存观察，不能覆盖")
    diagnostic = diagnostic_state_for(step.error) or DiagnosticState()
    if step.error is not None and not controlled_read_stop(step):
        record_error(step.error, stage=step.phase.value,
            context={"run_id": ticket.run_id, "attempt_id": ticket.attempt_id,
                     "copy_id": int(ticket.target_id)}, state=diagnostic)
    pending = PendingStoppedRead(ticket, step, evidence, occurred_at, new_operation_key(),
                                diagnostic=diagnostic)
    runtime.pending_read_results[identity] = pending
    return pending


async def _prepare_stopped_read(runtime, pending):
    ticket = pending.ticket
    row = await read_one(runtime.database, "SELECT a.cancel_requested,c.source_size FROM operation_runs r JOIN actions a ON a.id=r.action_id"
        " JOIN file_copies c ON c.id=r.copy_id WHERE r.id=? AND c.id=?", (ticket.run_id, int(ticket.target_id)))
    if row is None or row[0] not in (0, 1):
        raise ConsistencyError("实际停止观察的原读取归属或取消事实不可解释")
    if row[0] == 0:
        runtime.continuing_read_tickets[int(ticket.target_id)] = ReadContinuation(
            ticket, False, pending.step, pending.occurred_at, pending.key, pending.evidence)
        del runtime.pending_read_results[(ticket.run_id, ticket.attempt_id)]
        return None
    outcome = CallOutcome(status=AttemptStatus.UNKNOWN, effect=EffectState.UNKNOWN,
        error=ErrorValue("read_stopped", "read"),
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("read_returned", 1, {})))
    prepared = PendingReadResult(AttemptFinish(ticket, validate_outcome(ticket, outcome, pending.evidence),
        pending.occurred_at, run_finish=RunFinish(RunOutcome.CANCELED)), pending.key,
        result_save=pending.result_save, settlement=pending.settlement, diagnostic=pending.diagnostic)
    runtime.pending_read_results[(ticket.run_id, ticket.attempt_id)] = prepared
    return prepared


def hold_read_result(runtime, finish, *, business=None, key=None,
                     context=None, diagnostic_error=None, diagnostic_stage=None, suppress_diagnostic=False,
                     preserved_run_terminal=False, binding_read=False):
    identity = (finish.ticket.run_id, finish.ticket.attempt_id)
    if identity in runtime.pending_read_results:
        raise ConsistencyError("同一原读取已有待保存结果，不能覆盖")
    diagnostic = (business.diagnostic if business is not None else
                  diagnostic_state_for(diagnostic_error) or DiagnosticState())
    pending = PendingReadResult(finish, new_operation_key() if key is None else key, business,
        diagnostic=diagnostic, preserved_run_terminal=preserved_run_terminal, binding_read=binding_read)
    runtime.pending_read_ends.pop(int(finish.ticket.target_id), None)
    runtime.pending_read_results[identity] = pending
    details = {"copy_id": int(finish.ticket.target_id), **(context or {})}
    if suppress_diagnostic:
        return pending
    if diagnostic_error is not None:
        record_error(diagnostic_error, stage=diagnostic_stage, context={**details,
            "run_id": finish.ticket.run_id, "attempt_id": finish.ticket.attempt_id},
            state=pending.diagnostic)
    elif finish.outcome.outcome.settlement.basis is not SettlementBasis.NOT_DISPATCHED:
        outcome = finish.outcome.outcome
        if outcome.error is None or outcome.error.code != "read_stopped":
            record_call_outcome(outcome, ticket=finish.ticket,
                run_error=None if finish.run_finish is None else finish.run_finish.error,
                context=details, state=pending.diagnostic)
    return pending


async def save_read_result(runtime, pending, *, retry=False):
    """可靠保存原结果；业务收场完成前仍保留原结果与操作键。"""
    if retry:
        pending.result_save.retry_failed()

    async def save():
        prepared = pending
        if isinstance(pending, PendingStoppedRead):
            prepared = await _prepare_stopped_read(runtime, pending)
        if prepared is None:
            return None
        control_error = None
        try:
            outcome = await runtime.database.write(
                lambda owned: (runtime.operations.finish_binding_read(prepared.finish, prepared.key, owned)
                    if prepared.binding_read else runtime.operations.finish_attempt(prepared.finish, prepared.key, owned)), key=prepared.key)
        except BaseException as error:
            outcome = db_outcome_for(error)
            if outcome is None or outcome.kind is not DbOutcomeKind.COMPLETED:
                raise
            control_error = error
        if outcome.kind is not DbOutcomeKind.COMPLETED:
            raise db_failure(ConsistencyError,
                f"原读取结果未可靠保存（{outcome.kind.value}）: {outcome.error}", outcome)
        result = outcome.value
        if result is None:
            raise ConsistencyError("原读取结果缺少可靠事务回执")
        if prepared.binding_read and not result.run_was_terminal and result.disposition is not FinishDisposition.ALREADY_ENDED:
            if result.run_status is RunStatus.CANCELED:
                if control_error is None:
                    prepared = replace(prepared, finish=replace(prepared.finish, run_finish=RunFinish(RunOutcome.CANCELED)), business=None)
            elif result.run_status is not RunStatus.FAILED:
                raise ConsistencyError("绑定读取结果回执缺少已保存的流程处置")
            identity = (prepared.finish.ticket.run_id, prepared.finish.ticket.attempt_id)
            if runtime.pending_read_results.get(identity) is pending:
                runtime.pending_read_results[identity] = prepared
            if result.run_status is RunStatus.FAILED and prepared.business is not None:
                _record_read_business_error(prepared.business)
        if result.run_was_terminal or result.disposition is FinishDisposition.ALREADY_ENDED:
            prepared = replace(prepared, preserved_run_terminal=True)
            identity = (prepared.finish.ticket.run_id, prepared.finish.ticket.attempt_id)
            if runtime.pending_read_results.get(identity) is pending:
                runtime.pending_read_results[identity] = prepared
        if control_error is not None:
            raise control_error
        return prepared

    return await pending.result_save.run(save)


def forget_read_result(runtime, pending):
    ticket = pending.finish.ticket
    identity = (ticket.run_id, ticket.attempt_id)
    if runtime.pending_read_results.get(identity) is not pending:
        return
    del runtime.pending_read_results[identity]
    continuation = runtime.continuing_read_tickets.get(int(ticket.target_id))
    if continuation is not None and continuation.ticket == ticket:
        del runtime.continuing_read_tickets[int(ticket.target_id)]


def require_preparation_settled(step):
    """结果边界保留未收场责任；本地准备前提错误交付会话退出。"""
    from camctl.devices.read_session import ReadPreparationError, ReadPreparationSettlement

    if step.preparation_settlement is ReadPreparationSettlement.WAITING:
        if step.error is not None:
            raise step.error
        raise ReadPreparationError("原 READ 本地准备前提尚未满足",
                                   settlement=ReadPreparationSettlement.WAITING)
    if step.preparation_settlement is not ReadPreparationSettlement.SETTLED:
        raise ReadPreparationError(f"原 READ 准备尚未收场: {step.error}",
                                   settlement=step.preparation_settlement) from step.error
    return True


async def run_owned_read(runtime, copy_id, body):
    """同一文件资格持有到源实际结束及原读取结果保存完成。"""
    executor = getattr(runtime, "file_executor", None)
    if executor is None:
        return await body(None)
    from camctl.host_files.tasks import AsyncFileTask, FileTaskId

    row = await read_one(runtime.database, "SELECT target_file_id,source_intermediate_file_id"
        " FROM file_copies WHERE id=?", (copy_id,))
    if row is None or row[0] is None:
        raise ConsistencyError("实际读取拥有者缺少原目标文件")

    async def owned_body(control):
        return await body(control)

    pinned_files = tuple(dict.fromkeys(
        file_id for file_id in (row[1], row[0]) if file_id is not None))
    return await executor.run_owned_async_file_task(AsyncFileTask(
        FileTaskId(f"read/{new_operation_key()}"), pinned_files,
        "source_read", "read", owned_body))


def running_read_ticket(owned, copy_id: int):
    """读取原 RUNNING 身份与意图边界；不可解释的未结束结果明确拒绝。"""
    with closing(owned.connection.execute(
        "SELECT r.id,r.responsibility_key,r.attempts_used,a.attempt_no,a.status,"
        " a.result_json,a.intent_event_id FROM operation_runs r"
        " JOIN operation_attempts a ON a.run_id=r.id WHERE r.copy_id=?"
        " AND (a.status=1 OR a.result_json IS NULL) ORDER BY a.attempt_no", (copy_id,)
    )) as cursor:
        rows = cursor.fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise ConsistencyError("原文件具有多个未结束读取尝试")
    run_id, responsibility, used, attempt_no, status, result, intent_event_id = rows[0]
    if (responsibility != f"read/{copy_id}" or used != attempt_no
            or status != 1 or result is not None or intent_event_id is None):
        raise ConsistencyError("原读取尝试身份、结果或累计次数不可可靠解释")
    return AttemptTicket(attempt_no, "read", str(copy_id), responsibility, run_id), intent_event_id


def record_read_diagnostic(runtime, reason, ticket):
    diagnostic = RecoveryDiagnostic(reason, ticket.run_id, ticket.attempt_id)
    if diagnostic != runtime.last_recovery_diagnostic:
        runtime.last_recovery_diagnostic = diagnostic
        if runtime.on_recovery_diagnostic is not None:
            runtime.on_recovery_diagnostic(diagnostic)


def read_host_boundary(runtime, ticket, intent_event_id, *, continuing=True) -> bool:
    """只恢复启动边界之前的原意图；本次连接已关闭的重拷仍沿原尝试。"""
    from camctl.host_files.downloads import download_owned

    roots = getattr(runtime, "directories", None) or getattr(runtime, "roots", None)
    if download_owned(ticket, None if roots is None else roots.staging):
        record_read_diagnostic(runtime, RecoveryBlockedReason.ACTIVE_LOCAL_READ, ticket)
        return False
    held = getattr(runtime, "pending_read_ends", {}).get(int(ticket.target_id))
    if continuing and held is not None and held.ticket == ticket:
        return True
    continuation = runtime.continuing_read_tickets.get(int(ticket.target_id))
    if continuing and continuation is not None and continuation.ticket == ticket:
        return True
    if runtime.recovery_boundary is RecoveryBoundary.UNCONFIRMED:
        reason = RecoveryBlockedReason.UNCONFIRMED_BOUNDARY
    elif runtime.recovery_max_event_id is None:
        reason = RecoveryBlockedReason.MISSING_HORIZON
    elif intent_event_id > runtime.recovery_max_event_id:
        reason = RecoveryBlockedReason.INTENT_OUTSIDE_HORIZON
    else:
        return True
    record_read_diagnostic(runtime, reason, ticket)
    return False


async def acquire_read_attempt(runtime, intent):
    """选择原 ticket 或可靠提交新意图；原申请持续持有到回执应用。"""
    copy_id = intent.target.copy_id
    pending_begin = runtime.pending_read_business.get((copy_id, "read_intent"))
    if pending_begin is None:
        original = await runtime.database.read(lambda owned: running_read_ticket(owned, copy_id))
        if original is not None:
            ticket, event_id = original
            if not read_host_boundary(runtime, ticket, event_id):
                return None, "recovery_blocked"
            request = ReadResumeRequest(ticket, intent.config, intent.occurred_at)
            outcome = await save_read_business(runtime, copy_id, "read_resume", request,
                runtime.operations.resume_read, retry=True)
            if outcome.value.disposition is ReadResumeDisposition.NOT_RUNNING:
                return None, "run_ended"
            runtime.last_recovery_diagnostic = None
            return ticket, None
    granted_by_save = None
    def remember(value):
        nonlocal granted_by_save
        if value.disposition is BeginDisposition.GRANTED:
            granted_by_save = value.ticket
            original = runtime.pending_read_business.get((copy_id, "read_intent"))
            runtime.continuing_read_tickets[copy_id] = ReadContinuation(
                value.ticket, True, inspection_cancel_recovery=(
                    original is not None and bool(original.inspection_cancel_recovery)))
    try:
        outcome = await save_read_business(runtime, copy_id, "read_intent", intent,
            runtime.operations.begin_attempt, on_completed=remember, retry=pending_begin is not None)
    except BaseException:
        continuation = runtime.continuing_read_tickets.get(copy_id)
        if (granted_by_save is not None and continuation is not None
                and continuation.ticket == granted_by_save and continuation.never_dispatched):
            runtime.continuing_read_tickets[copy_id] = replace(continuation, inspection_cancel_recovery=True)
        raise
    if outcome.value.disposition is BeginDisposition.GRANTED:
        return outcome.value.ticket, None
    return None, outcome.value.reason


async def recover_unavailable_read(runtime, copy_id: int, binding, now: int) -> bool:
    """不能续传的原读取只按原驱动声明生成正式 UNKNOWN 恢复结果。"""
    for pending in tuple(runtime.pending_read_results.values()):
        ticket = pending.ticket if isinstance(pending, PendingStoppedRead) else pending.finish.ticket
        if int(ticket.target_id) == copy_id:
            prepared = await save_read_result(runtime, pending, retry=True)
            if prepared is None:
                record_read_diagnostic(runtime, RecoveryBlockedReason.UNCONFIRMED_BOUNDARY, ticket)
                return False
            forget_read_result(runtime, prepared)
    original = await runtime.database.read(lambda owned: running_read_ticket(owned, copy_id))
    if original is None:
        return True
    ticket, intent_id = original
    from camctl.persistence.repositories.read_end import load_read_end
    saved_end = await runtime.database.read(lambda owned: load_read_end(copy_id, owned))
    if saved_end is not None:
        saved_ticket, end, occurred_at, slot = saved_end
        if saved_ticket != ticket:
            raise ConsistencyError("已保存读取结束与原未完成尝试不一致")
        evidence = (None if runtime.recovery_evidence_for is None
                    else runtime.recovery_evidence_for(binding, "read"))
        if evidence is None:
            record_read_diagnostic(runtime, RecoveryBlockedReason.EVIDENCE_UNAVAILABLE, ticket)
            return False
        held = runtime.pending_read_ends.get(copy_id)
        if held is not None and (held.ticket != ticket or held.end != end):
            raise ConsistencyError("已保存读取结束与当前拥有者持有的返回不一致")
        if held is None and slot is not None:
            runtime.pending_read_ends[copy_id] = HeldReadEnd(ticket, end, occurred_at, evidence=evidence)
        return False
    if copy_id in runtime.pending_read_ends:
        raise ConsistencyError("原实际读取结束已持有，仍需设备摘要或重拷输入；保留原责任")
    if not read_host_boundary(runtime, ticket, intent_id, continuing=False):
        return False
    if runtime.recovery_evidence_for is None:
        record_read_diagnostic(runtime, RecoveryBlockedReason.MISSING_EVIDENCE_LOOKUP, ticket)
        return False
    evidence = runtime.recovery_evidence_for(binding, "read")
    if evidence is None:
        record_read_diagnostic(runtime, RecoveryBlockedReason.EVIDENCE_UNAVAILABLE, ticket)
        return False
    try:
        contract = evidence.contract("local_read_recovery", 1)
    except EvidenceError:
        contract = evidence.contract("adb_foreground_recovery", 1)
    recovered = CallOutcome(
        status=AttemptStatus.UNKNOWN, effect=EffectState.UNKNOWN,
        error=ErrorValue("result_not_saved", "recovery"),
        settlement=Settlement(SettlementBasis.ASSUMED,
                              EvidenceValue.of(contract)))
    finish = AttemptFinish(ticket, validate_outcome(ticket, recovered, evidence), now)
    pending = hold_read_result(runtime, finish)
    await save_read_result(runtime, pending)
    forget_read_result(runtime, pending)
    runtime.last_recovery_diagnostic = None
    return True

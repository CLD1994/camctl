"""设备文件读取的原尝试选择；连接恢复不消耗新的读取次数。"""

from contextlib import closing
from dataclasses import dataclass
from typing import Any, Callable

from camctl.capture.recovery import RecoveryBoundary, RecoveryBlockedReason, RecoveryDiagnostic
from camctl.contracts.values import ConsistencyError, OperationKey, new_operation_key
from camctl.operations.attempts import (
    AttemptFinish, BeginDisposition, ReadResumeDisposition, ReadResumeRequest, RunFinish, RunOutcome,
)
from camctl.operations.models import (
    AttemptStatus, AttemptTicket, CallOutcome, EffectState, ErrorValue,
    EvidenceValue, Settlement, SettlementBasis,
)
from camctl.operations.validation import validate_outcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.business import settle_required
from camctl.outputs.database_reads import read_one
from camctl.devices.evidence import EvidenceError


@dataclass(frozen=True)
class PendingReadResult:
    """实际读取结束后持有的原结果和完整提交身份。"""

    finish: AttemptFinish
    key: OperationKey
    business: "PendingReadBusiness | None" = None


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
        if held is not None and held.ticket != ticket:
            raise ConsistencyError("原完整读取结束事实属于其他尝试")
        runtime.pending_read_ends[copy_id] = HeldReadEnd(ticket, end, runtime.occurred_at(),
            resume if resume is not None else (held.resume if held is not None else None),
            evidence if evidence is not None else (held.evidence if held is not None else None))


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


async def retry_read_business(runtime):
    for identity, pending in tuple(runtime.pending_read_business.items()):
        await save_read_business(runtime, *identity, pending.request, pending.save)


async def save_read_business(runtime, copy_id, operation, request, save, *, on_completed=None):
    identity = (copy_id, operation)
    pending = runtime.pending_read_business.get(identity)
    if pending is None:
        pending = PendingReadBusiness(save, request, new_operation_key(), on_completed)
        runtime.pending_read_business[identity] = pending
    async def save_and_apply():
        receipt = await runtime.database.write(
            lambda owned: pending.save(pending.request, pending.key, owned), key=pending.key)
        if receipt.kind is not DbOutcomeKind.COMPLETED:
            raise ConsistencyError(f"原读取业务结果未可靠保存（{receipt.kind.value}）: {receipt.error}")
        if pending.on_completed is not None:
            pending.on_completed(receipt.value)
        del runtime.pending_read_business[identity]
        return receipt
    return await settle_required(save_and_apply())


async def save_prepared_read_business(runtime, copy_id, operation, pending):
    """完整结束结果预先确定的原业务申请，可靠结果之后才提交。"""
    identity = (copy_id, operation)
    held = runtime.pending_read_business.get(identity)
    if held is not None and held != pending:
        raise ConsistencyError("原读取结束所属申请与已持有的申请不同")
    runtime.pending_read_business[identity] = pending
    return await save_read_business(runtime, copy_id, operation, pending.request, pending.save)


async def held_binding_read_result(runtime, copy_id, binding_result, business):
    """可靠源实际完成、必要源摘要未取得时分别记录调用成功和绑定失败。"""
    from camctl.devices.bindings import binding_failure_details
    from camctl.outputs.copy import SourceChecksumSupport
    from camctl.persistence.repositories.outputs import OutputsRepository

    held = runtime.pending_read_ends.get(copy_id)
    if held is None:
        return None
    facts = await runtime.database.read(lambda owned: OutputsRepository().load_copy_state(copy_id, owned))
    if (facts.committed_bytes != facts.source_size or facts.source_support is not SourceChecksumSupport.SUPPORTED
            or facts.source_sha256 is not None):
        return None
    original = await runtime.database.read(lambda owned: running_read_ticket(owned, copy_id))
    if original is None or original[0] != held.ticket or held.end.stopped is not True or held.end.error is not None:
        raise ConsistencyError("原必要摘要绑定失败缺少可靠原读取结束")
    if held.evidence is None:
        raise ConsistencyError("原实际读取结束缺少原驱动结果契约")
    details = binding_failure_details(binding_result)
    if details is None:
        raise ConsistencyError("必要摘要绑定失败必须具有原绑定异常事实")
    status = await read_one(runtime.database, "SELECT status FROM operation_runs WHERE id=?", (held.ticket.run_id,))
    if status is None or status[0] not in (1, 2):
        raise ConsistencyError("原必要摘要绑定处置尚未确定，保留原读取流程终态")
    outcome = CallOutcome(status=AttemptStatus.SUCCEEDED, effect=EffectState.UNKNOWN,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("read_returned", 1, {})))
    finish = AttemptFinish(held.ticket, validate_outcome(held.ticket, outcome, held.evidence), runtime.occurred_at(),
        run_finish=RunFinish(RunOutcome.FAILED, ErrorValue("device_binding_unavailable", "execution", details)))
    return hold_read_result(runtime, finish, business=business)


@dataclass(frozen=True)
class PendingStoppedRead:
    """源实际停止后，先持有原观察，再读取所属业务取消事实。"""

    ticket: AttemptTicket
    step: object
    evidence: object
    occurred_at: int
    key: OperationKey


def stopped_read_result(runtime, ticket, step, evidence, occurred_at):
    if (step.source_failed or step.read_end is None or step.read_end.error != "stopped"
            or not step.stop_requested or step.content_complete):
        return None
    identity = (ticket.run_id, ticket.attempt_id)
    if identity in runtime.pending_read_results:
        raise ConsistencyError("同一原读取已有待保存观察，不能覆盖")
    pending = PendingStoppedRead(ticket, step, evidence, occurred_at, new_operation_key())
    runtime.pending_read_results[identity] = pending
    return pending


async def _prepare_stopped_read(runtime, pending):
    ticket = pending.ticket
    row = await read_one(runtime.database, "SELECT a.cancel_requested,c.source_size FROM operation_runs r JOIN actions a ON a.id=r.action_id"
        " JOIN file_copies c ON c.id=r.copy_id WHERE r.id=? AND c.id=?", (ticket.run_id, int(ticket.target_id)))
    if row is None or row[0] not in (0, 1):
        raise ConsistencyError("实际停止观察的原读取归属或取消事实不可解释")
    if row[0] == 0:
        runtime.continuing_read_tickets[int(ticket.target_id)] = ticket
        del runtime.pending_read_results[(ticket.run_id, ticket.attempt_id)]
        return None
    outcome = CallOutcome(status=AttemptStatus.UNKNOWN, effect=EffectState.UNKNOWN,
        error=ErrorValue("read_stopped", "read"),
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("read_returned", 1, {})))
    prepared = PendingReadResult(AttemptFinish(ticket, validate_outcome(ticket, outcome, pending.evidence),
        pending.occurred_at, run_finish=RunFinish(RunOutcome.CANCELED)), pending.key)
    runtime.pending_read_results[(ticket.run_id, ticket.attempt_id)] = prepared
    return prepared


def hold_read_result(runtime, finish, *, business=None):
    identity = (finish.ticket.run_id, finish.ticket.attempt_id)
    if identity in runtime.pending_read_results:
        raise ConsistencyError("同一原读取已有待保存结果，不能覆盖")
    pending = PendingReadResult(finish, new_operation_key(), business)
    runtime.pending_read_ends.pop(int(finish.ticket.target_id), None)
    runtime.pending_read_results[identity] = pending
    return pending


async def save_read_result(runtime, pending):
    """可靠保存原结果；业务收场完成前仍保留原结果与操作键。"""
    if isinstance(pending, PendingStoppedRead):
        pending = await _prepare_stopped_read(runtime, pending)
        if pending is None:
            return None
    outcome = await runtime.database.write(
        lambda owned: runtime.operations.finish_attempt(pending.finish, pending.key, owned), key=pending.key)
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(f"原读取结果未可靠保存（{outcome.kind.value}）: {outcome.error}")
    return pending


def forget_read_result(runtime, pending):
    ticket = pending.finish.ticket
    del runtime.pending_read_results[(ticket.run_id, ticket.attempt_id)]


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
        return await body()
    from camctl.host_files.tasks import AsyncFileTask, FileTaskId

    row = await read_one(runtime.database, "SELECT target_file_id,source_intermediate_file_id"
        " FROM file_copies WHERE id=?", (copy_id,))
    if row is None or row[0] is None:
        raise ConsistencyError("实际读取拥有者缺少原目标文件")

    async def owned_body(_control):
        return await body()

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
    if continuing and runtime.continuing_read_tickets.get(int(ticket.target_id)) == ticket:
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
                runtime.operations.resume_read)
            if outcome.value.disposition is ReadResumeDisposition.NOT_RUNNING:
                return None, "run_ended"
            runtime.last_recovery_diagnostic = None
            return ticket, None
    def remember(value):
        if value.disposition is BeginDisposition.GRANTED:
            runtime.continuing_read_tickets[copy_id] = value.ticket
    outcome = await save_read_business(runtime, copy_id, "read_intent", intent,
        runtime.operations.begin_attempt, on_completed=remember)
    if outcome.value.disposition is BeginDisposition.GRANTED:
        return outcome.value.ticket, None
    return None, outcome.value.reason


async def recover_unavailable_read(runtime, copy_id: int, binding, now: int) -> bool:
    """不能续传的原读取只按原驱动声明生成正式 UNKNOWN 恢复结果。"""
    for pending in tuple(runtime.pending_read_results.values()):
        ticket = pending.ticket if isinstance(pending, PendingStoppedRead) else pending.finish.ticket
        if int(ticket.target_id) == copy_id:
            prepared = await save_read_result(runtime, pending)
            if prepared is None:
                record_read_diagnostic(runtime, RecoveryBlockedReason.UNCONFIRMED_BOUNDARY, ticket)
                return False
            forget_read_result(runtime, prepared)
    original = await runtime.database.read(lambda owned: running_read_ticket(owned, copy_id))
    if original is None:
        return True
    ticket, intent_id = original
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

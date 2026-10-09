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
    from camctl.persistence.repositories.outputs import OutputsRepository

    for copy_id, held in tuple(runtime.pending_read_ends.items()):
        facts = OutputsRepository().load_copy_state(copy_id, runtime.owned)
        if (facts.committed_bytes != facts.source_size
                or (facts.source_support is not SourceChecksumSupport.UNSUPPORTED
                    and facts.source_sha256 is None)):
            continue
        original = running_read_ticket(runtime.owned, copy_id)
        if original is None or original[0] != held.ticket:
            raise ConsistencyError("已持有的实际读取结束与原未完成尝试不符")
        if held.resume is None:
            raise ConsistencyError("原实际读取结束缺少本地校验续接责任")
        await held.resume(runtime, held)


def canceled_complete_read_result(runtime, ticket, evidence):
    """完整真实结束优先；尚未结束的副本业务按可靠取消收场。"""
    held = runtime.pending_read_ends.get(int(ticket.target_id))
    if held is None or held.ticket != ticket:
        return None
    row = runtime.owned.connection.execute(
        "SELECT a.cancel_requested,r.status FROM operation_runs r JOIN actions a ON a.id=r.action_id"
        " WHERE r.id=? AND r.copy_id=?", (ticket.run_id, int(ticket.target_id))).fetchone()
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


def retry_read_business(runtime):
    for identity, pending in tuple(runtime.pending_read_business.items()):
        receipt = pending.save(pending.request, pending.key, runtime.owned)
        if receipt.kind is not DbOutcomeKind.COMPLETED:
            raise ConsistencyError(f"原读取业务结果未可靠保存（{receipt.kind.value}）: {receipt.error}")
        del runtime.pending_read_business[identity]


def save_read_business(runtime, copy_id, operation, request, save):
    identity = (copy_id, operation)
    pending = runtime.pending_read_business.get(identity)
    if pending is None:
        pending = PendingReadBusiness(save, request, new_operation_key())
        runtime.pending_read_business[identity] = pending
    receipt = pending.save(pending.request, pending.key, runtime.owned)
    if receipt.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(f"原读取业务结果未可靠保存（{receipt.kind.value}）: {receipt.error}")
    del runtime.pending_read_business[identity]
    return receipt


def save_prepared_read_business(runtime, copy_id, operation, pending):
    """完整结束结果预先确定的原业务申请，可靠结果之后才提交。"""
    identity = (copy_id, operation)
    held = runtime.pending_read_business.get(identity)
    if held is not None and held != pending:
        raise ConsistencyError("原读取结束所属申请与已持有的申请不同")
    runtime.pending_read_business[identity] = pending
    return save_read_business(runtime, copy_id, operation, pending.request, pending.save)


def held_binding_read_result(runtime, copy_id, binding_result, business):
    """可靠源实际完成、必要源摘要未取得时分别记录调用成功和绑定失败。"""
    from camctl.devices.bindings import binding_failure_details
    from camctl.outputs.copy import SourceChecksumSupport
    from camctl.persistence.repositories.outputs import OutputsRepository

    held = runtime.pending_read_ends.get(copy_id)
    if held is None:
        return None
    facts = OutputsRepository().load_copy_state(copy_id, runtime.owned)
    if (facts.committed_bytes != facts.source_size or facts.source_support is not SourceChecksumSupport.SUPPORTED
            or facts.source_sha256 is not None):
        return None
    original = running_read_ticket(runtime.owned, copy_id)
    if original is None or original[0] != held.ticket or held.end.stopped is not True or held.end.error is not None:
        raise ConsistencyError("原必要摘要绑定失败缺少可靠原读取结束")
    if held.evidence is None:
        raise ConsistencyError("原实际读取结束缺少原驱动结果契约")
    details = binding_failure_details(binding_result)
    if details is None:
        raise ConsistencyError("必要摘要绑定失败必须具有原绑定异常事实")
    status = runtime.owned.connection.execute("SELECT status FROM operation_runs WHERE id=?", (held.ticket.run_id,)).fetchone()
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


def _prepare_stopped_read(runtime, pending):
    ticket = pending.ticket
    row = runtime.owned.connection.execute(
        "SELECT a.cancel_requested,c.source_size FROM operation_runs r JOIN actions a ON a.id=r.action_id"
        " JOIN file_copies c ON c.id=r.copy_id WHERE r.id=? AND c.id=?", (ticket.run_id, int(ticket.target_id))).fetchone()
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


def save_read_result(runtime, pending):
    """可靠保存原结果；业务收场完成前仍保留原结果与操作键。"""
    if isinstance(pending, PendingStoppedRead):
        pending = _prepare_stopped_read(runtime, pending)
        if pending is None:
            return None
    outcome = runtime.operations.finish_attempt(pending.finish, pending.key, runtime.owned)
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(f"原读取结果未可靠保存（{outcome.kind.value}）: {outcome.error}")
    return pending


def forget_read_result(runtime, pending):
    ticket = pending.finish.ticket
    del runtime.pending_read_results[(ticket.run_id, ticket.attempt_id)]


async def run_owned_read(runtime, copy_id, body):
    """同一文件资格持有到源实际结束及原读取结果保存完成。"""
    executor = getattr(runtime, "file_executor", None)
    if executor is None:
        return await body()
    from camctl.host_files.tasks import AsyncFileTask, FileTaskId

    row = runtime.owned.connection.execute(
        "SELECT target_file_id FROM file_copies WHERE id=?", (copy_id,)).fetchone()
    if row is None or row[0] is None:
        raise ConsistencyError("实际读取拥有者缺少原目标文件")

    async def owned_body(_control):
        return await body()

    return await executor.run_owned_async_file_task(AsyncFileTask(
        FileTaskId(f"read/{new_operation_key()}"), (row[0],), "source_read", "read", owned_body))


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


def acquire_read_attempt(runtime, intent):
    """选择原 ticket 或可靠提交新意图；返回拒绝原因不派发设备。"""
    original = running_read_ticket(runtime.owned, intent.target.copy_id)
    if original is not None:
        ticket, event_id = original
        if not read_host_boundary(runtime, ticket, event_id):
            return None, "recovery_blocked"
        outcome = runtime.operations.resume_read(ReadResumeRequest(
            ticket, intent.config, intent.occurred_at), new_operation_key(), runtime.owned)
        if outcome.kind is not DbOutcomeKind.COMPLETED:
            raise ConsistencyError(f"原读取配置采用未可靠保存: {outcome.error}")
        if outcome.value.disposition is ReadResumeDisposition.NOT_RUNNING:
            return None, "run_ended"
        runtime.last_recovery_diagnostic = None
        return ticket, None
    outcome = runtime.operations.begin_attempt(intent, new_operation_key(), runtime.owned)
    if outcome.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(f"读取意图未可靠保存: {outcome.error}")
    if outcome.value.disposition is BeginDisposition.GRANTED:
        return outcome.value.ticket, None
    return None, outcome.value.reason


def recover_unavailable_read(runtime, copy_id: int, binding, now: int) -> bool:
    """不能续传的原读取只按原驱动声明生成正式 UNKNOWN 恢复结果。"""
    for pending in tuple(runtime.pending_read_results.values()):
        ticket = pending.ticket if isinstance(pending, PendingStoppedRead) else pending.finish.ticket
        if int(ticket.target_id) == copy_id:
            prepared = save_read_result(runtime, pending)
            if prepared is None:
                record_read_diagnostic(runtime, RecoveryBlockedReason.UNCONFIRMED_BOUNDARY, ticket)
                return False
            forget_read_result(runtime, prepared)
    original = running_read_ticket(runtime.owned, copy_id)
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
    recovered = CallOutcome(
        status=AttemptStatus.UNKNOWN, effect=EffectState.UNKNOWN,
        error=ErrorValue("result_not_saved", "recovery"),
        settlement=Settlement(SettlementBasis.ASSUMED,
                              EvidenceValue("adb_foreground_recovery", 1, {})))
    finish = AttemptFinish(ticket, validate_outcome(ticket, recovered, evidence), now)
    pending = hold_read_result(runtime, finish)
    save_read_result(runtime, pending)
    forget_read_result(runtime, pending)
    runtime.last_recovery_diagnostic = None
    return True

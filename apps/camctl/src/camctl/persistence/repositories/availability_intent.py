"""可用性检查意图的原连接独立资格。"""

from dataclasses import replace
from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError
from camctl.persistence.transaction import row_facts
from .availability_create import _qualify, _request
from .availability_target import AvailabilityTargetBlocked


class AvailabilityIntentReads:
    def __init__(self, connection, event, transaction):
        if (event.event_id != transaction.first_event_id or event.event_id != transaction.last_event_id
                or event.transaction_id != transaction.txn_id):
            raise ConsistencyError("检查意图必须是独立完整事务")
        self.connection, self.event = connection, replace(event, change_seq=None)

    def verify(self, event, context, run_id):
        if replace(event, change_seq=None) != self.event or context.transaction.txn_id != event.transaction_id:
            raise ConsistencyError("检查意图没有绑定原内核事件")
        request = _request(event.evidence.get("availability_intent_request"))
        if request.occurred_at != event.occurred_at:
            raise ConsistencyError("检查意图的事实时刻不一致")
        run = row_facts(self.connection, "operation_runs", run_id)
        if (run is None or run["kind"] != int(enum_for("operation_runs.kind").CHECK_DEVICE_AVAILABILITY)
                or run["action_id"] != request.action_id
                or run["status"] not in (int(enum_for("operation_runs.status").PENDING),
                                         int(enum_for("operation_runs.status").ACTIVE))):
            raise ConsistencyError("检查意图没有开放的原责任")
        check = row_facts(self.connection, "device_availability_checks", run["availability_check_id"])
        if check is None or check["action_id"] != run["action_id"]:
            raise ConsistencyError("检查意图缺少原检查与固定机身")
        if (not json_equal(context.state_rows.get("operation_runs", {}).get(run_id), run)
                or not json_equal(context.state_rows.get("device_availability_checks", {}).get(check["id"]), check)):
            raise ConsistencyError("检查意图上下文与原连接事实不同")
        attempts = [row for row in event.rows if row.table == "operation_attempts" and not row.before.exists]
        runs = [row for row in event.rows if row.table == "operation_runs" and row.row_id == run_id and row.before.exists]
        if len(event.rows) != 2 or len(attempts) != 1 or len(runs) != 1:
            raise ConsistencyError("检查意图必须共同保存唯一原流程及新尝试")
        proposal = {**run, **runs[0].after.values}
        attempt = attempts[0].after.values
        if (attempt.get("run_id") != run_id or attempt.get("attempt_no") != run["attempts_used"] + 1
                or proposal.get("attempts_used") != attempt.get("attempt_no")
                or any(not json_equal(attempt.get(name), proposal.get(name)) for name in
                       ("max_attempts_used", "timeout_s_json", "retry_interval_s_json"))
                or any(attempt.get(name) is None for name in
                       ("max_attempts_used", "timeout_s_json", "retry_interval_s_json"))
                or attempt["attempt_no"] > attempt["max_attempts_used"]):
            raise ConsistencyError("检查意图的次数和实际采用配置不一致")
        _qualify(self.connection, request, check["target_boundary_event_id"], existing_check=check)
        if run["retry_wait_required"] != 0:
            raise AvailabilityTargetBlocked("检查重试间隔尚未完成")
        if run["attempts_used"]:
            from .retry_wait_reads import RetryWaitReads
            if not RetryWaitReads(self.connection).completed(run_id, run["attempts_used"]):
                raise AvailabilityTargetBlocked("检查重试缺少上次尝试的已完成间隔")


def recheck_availability_dispatch(connection, ticket, request):
    """意图提交后重新读取触发资格和同机事实，不保存未派发结果。"""
    from contextlib import closing
    from camctl.operations.models import AttemptTicket
    from camctl.scheduling.resources import DispatchRecheck
    from .availability_create import CreateAvailabilityCheck
    from .availability_target import AvailabilityTargetIdentityMismatch, AvailabilityTargetNotRequired

    if not isinstance(ticket, AttemptTicket) or not isinstance(request, CreateAvailabilityCheck):
        raise ValueError("设备检查派发复核需要原票据和当前资格申请")
    run = row_facts(connection, "operation_runs", ticket.run_id)
    if (run is None or run["kind"] != int(enum_for("operation_runs.kind").CHECK_DEVICE_AVAILABILITY)
            or ticket.operation != "availability" or ticket.responsibility_key != run["responsibility_key"]
            or ticket.target_id != str(run["availability_check_id"]) or request.action_id != run["action_id"]):
        raise ConsistencyError("设备检查派发票据与原责任不一致")
    with closing(connection.execute("SELECT id FROM operation_attempts WHERE run_id=? AND attempt_no=?",
                                    (run["id"], ticket.attempt_id))) as cursor:
        found = cursor.fetchone()
    if found is None:
        raise ConsistencyError("设备检查派发缺少原尝试")
    attempt = row_facts(connection, "operation_attempts", found[0])
    if (attempt is None or attempt["run_id"] != run["id"] or attempt["attempt_no"] != run["attempts_used"]):
        raise ConsistencyError("设备检查派发尝试与原累计次数不一致")
    if (attempt["status"] != int(enum_for("operation_attempts.status").RUNNING)
            or attempt["result_event_id"] is not None
            or run["status"] not in (int(enum_for("operation_runs.status").PENDING),
                                     int(enum_for("operation_runs.status").ACTIVE))):
        return DispatchRecheck(False, "attempt_ended")
    check = row_facts(connection, "device_availability_checks", run["availability_check_id"])
    if check is None or check["action_id"] != run["action_id"]:
        raise ConsistencyError("设备检查派发缺少固定检查")
    try:
        _qualify(connection, request, check["target_boundary_event_id"], existing_check=check,
                 exclude_attempt_id=attempt["id"])
    except AvailabilityTargetNotRequired:
        return DispatchRecheck(False, "targets_resolved")
    except AvailabilityTargetIdentityMismatch:
        return DispatchRecheck(False, "identity_mismatch")
    except AvailabilityTargetBlocked:
        return DispatchRecheck(False, "not_eligible")
    return DispatchRecheck(True, None)

"""设备检查重试间隔的可靠完成与原尝试关联。"""

from contextlib import closing
from dataclasses import replace
from enum import Enum

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError, ObjectId, OperationKey, UtcMicros
from camctl.history.events import load_event_registry
from camctl.persistence.transaction import (CommandPlan, commit_operation, event_envelope,
    row_facts, saved_transaction_events, update_change)


class RetryCompletion(Enum):
    COMPLETED = "completed"
    ALREADY_CLEAR = "already_clear"
    TERMINAL = "terminal"


def _verify(connection, run_id, attempt_id):
    run = row_facts(connection, "operation_runs", run_id)
    attempt = row_facts(connection, "operation_attempts", attempt_id)
    if (run is None or attempt is None
            or run["kind"] != int(enum_for("operation_runs.kind").CHECK_DEVICE_AVAILABILITY)
            or attempt["run_id"] != run_id or attempt["attempt_no"] != run["attempts_used"]
            or run["attempts_used"] < 1 or run["retry_wait_required"] != 1
            or run["status"] not in (int(enum_for("operation_runs.status").PENDING),
                                     int(enum_for("operation_runs.status").ACTIVE))
            or attempt["status"] not in (int(enum_for("operation_attempts.status").FAILED),
                                         int(enum_for("operation_attempts.status").UNKNOWN))
            or attempt["result_event_id"] is None or attempt["result_json"] is None):
        raise ConsistencyError("检查间隔缺少原开放流程及最近可靠失败结果")
    with closing(connection.execute(
        "SELECT MAX(attempt_no),COUNT(*) FROM operation_attempts WHERE run_id=?", (run_id,),
    )) as cursor:
        maximum, count = cursor.fetchone()
        if maximum != run["attempts_used"] or count != run["attempts_used"]:
            raise ConsistencyError("检查累计次数与原尝试范围不一致")
    with closing(connection.execute(
        "SELECT 1 FROM operation_attempts WHERE run_id=? AND (status=? OR result_event_id IS NULL) LIMIT 1",
        (run_id, int(enum_for("operation_attempts.status").RUNNING)),
    )) as cursor:
        if cursor.fetchone() is not None:
            raise ConsistencyError("检查调用或原结果尚未收场")
    from camctl.operations.result_format import read_result_document
    try:
        read_result_document(attempt["status"], attempt["effect_state"], attempt["result_json"], attempt["error_json"])
    except (KeyError, TypeError, ValueError) as error:
        raise ConsistencyError("检查上次尝试结果无法解释") from error
    return run, attempt


class AvailabilityRetryReads:
    def __init__(self, connection, event, transaction):
        if (event.event_id != transaction.first_event_id or event.event_id != transaction.last_event_id
                or event.transaction_id != transaction.txn_id):
            raise ConsistencyError("检查间隔完成必须是独立完整事务")
        self.connection, self.event = connection, replace(event, change_seq=None)

    def verify(self, event, context, run_id):
        if (replace(event, change_seq=None) != self.event or len(event.rows) != 1
                or event.rows[0].table != "operation_runs" or event.rows[0].row_id != run_id
                or set(event.evidence) != {"attempt_id"}):
            raise ConsistencyError("检查间隔完成事件不属于原流程")
        attempt_id = ObjectId(event.evidence["attempt_id"])
        run, _ = _verify(self.connection, run_id, attempt_id)
        if not json_equal(context.state_rows.get("operation_runs", {}).get(run_id), run):
            raise ConsistencyError("检查间隔上下文与原连接事实不同")


class CompleteAvailabilityRetryCommand:
    def __init__(self, run_id, attempt_id, occurred_at, key):
        self.run_id, self.attempt_id = ObjectId(run_id), ObjectId(attempt_id)
        self.occurred_at, self.key = UtcMicros(occurred_at), OperationKey(key)

    def plan(self, scope):
        db = scope.connection
        saved = saved_transaction_events(db, self.key)
        event_spec = load_event_registry()["events"]["RETRY_WAIT_CHANGED"]
        reason = event_spec["branches"]["COMPLETE"]["reason"]
        if saved is not None:
            if (len(saved) != 1 or (saved[0]["type"], saved[0]["reason"]) != (event_spec["id"], reason)
                    or saved[0]["occurred_at"] != self.occurred_at
                    or saved[0]["body"]["evidence"] != {"attempt_id": self.attempt_id}
                    or len(saved[0]["body"]["rows"]) != 1
                    or saved[0]["body"]["rows"][0]["table"] != "operation_runs"
                    or saved[0]["body"]["rows"][0]["id"] != self.run_id
                    or saved[0]["body"]["rows"][0]["before"] != {"exists": True, "values": {"retry_wait_required": 1}}
                    or saved[0]["body"]["rows"][0]["after"] != {"exists": True, "values": {"retry_wait_required": 0}}):
                raise ConsistencyError("检查间隔原键与完成申请不一致")
            attempt = row_facts(db, "operation_attempts", self.attempt_id)
            run = row_facts(db, "operation_runs", self.run_id)
            if (attempt is None or run is None or attempt["run_id"] != self.run_id
                    or run["kind"] != int(enum_for("operation_runs.kind").CHECK_DEVICE_AVAILABILITY)
                    or attempt["result_event_id"] is None or attempt["result_event_id"] >= saved[0]["event_id"]):
                raise ConsistencyError("检查间隔原键缺少先前原尝试结果")
            return CommandPlan((), {}, {}, RetryCompletion.COMPLETED, read_only=True)
        run = row_facts(db, "operation_runs", self.run_id)
        if run is None or run["kind"] != int(enum_for("operation_runs.kind").CHECK_DEVICE_AVAILABILITY):
            raise ConsistencyError("检查间隔缺少原流程")
        if run["status"] not in (int(enum_for("operation_runs.status").PENDING),
                                  int(enum_for("operation_runs.status").ACTIVE)):
            return CommandPlan((), {}, {}, RetryCompletion.TERMINAL, read_only=True)
        if run["retry_wait_required"] == 0:
            return CommandPlan((), {}, {}, RetryCompletion.ALREADY_CLEAR, read_only=True)
        _verify(db, self.run_id, self.attempt_id)
        allocation = scope.allocate(1)
        event = event_envelope(allocation.first_event_id, allocation.txn_id, event_spec["id"], reason,
            (update_change("operation_runs", self.run_id, {"retry_wait_required": 1}, {"retry_wait_required": 0}),),
            self.occurred_at, {"attempt_id": self.attempt_id})
        return CommandPlan((event,), {("operation_runs", self.run_id): ("action", run["action_id"])},
                           {"operation_runs": {self.run_id: run}}, RetryCompletion.COMPLETED)


def complete_availability_retry_wait(run_id, attempt_id, occurred_at, key, owned):
    """时间拥有者确认间隔已届满后保存清除事实。"""
    from .operations import _outcome_of
    return _outcome_of(commit_operation(CompleteAvailabilityRetryCommand(run_id, attempt_id, occurred_at, key), key, owned))

"""从已提交的原尝试及完整历史边界恢复清理结果，不调用当前驱动。"""

from dataclasses import dataclass
from collections.abc import Mapping

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal, parse_exact_json
from camctl.contracts.values import ConsistencyError, OperationKey
from camctl.devices.ports import DeviceCallResult
from camctl.devices.evidence import DeviceObservation
from camctl.history.events import event_type_name
from camctl.operations.models import AttemptTicket
from camctl.operations.result_format import read_result_document
from camctl.persistence.transaction import saved_transaction_events


@dataclass(frozen=True)
class SavedCleanupQuery:
    ticket: AttemptTicket
    actual: DeviceCallResult
    key: OperationKey
    occurred_at: int
    after_delete: bool


def saved_query_presence(actual, item_id: int) -> bool | None:
    """解释原成员的 v1 文件在场事实，不借用当前驱动的登记。"""
    present = None
    found = False
    for observation in actual.observations:
        if not isinstance(observation, DeviceObservation):
            raise ConsistencyError("原查询观察不是完整类型化对象")
        if observation.type != "file_presence":
            continue
        if (found or type(observation.version) is not int or observation.version != 1
                or not isinstance(observation.data, Mapping)
                or set(observation.data) != {"cleanup_item_id", "present"}
                or observation.data["cleanup_item_id"] != str(item_id)
                or type(observation.data["present"]) is not bool):
            raise ConsistencyError("已保存文件在场观察不可解释")
        found = True
        present = observation.data["present"]
    return present


def saved_cleanup_query(connection, item_id: int) -> SavedCleanupQuery | None:
    """只交付本成员最新、晚于后续删除的完整查询事实。

    新查询仍运行时不复用更早结果；查询之后存在新的删除意图时，
    原观察不再说明当前文件状态。结果缺失或历史不符明确诊断。
    """
    row = connection.execute(
        "SELECT a.id,a.attempt_no,a.status,a.effect_state,a.result_json,a.error_json,"
        " a.result_event_id,a.intent_event_id,r.id,r.responsibility_key,r.action_id,"
        " r.kind,c.action_id,c.output_id"
        " FROM operation_runs r JOIN operation_attempts a ON a.run_id=r.id"
        " JOIN cleanup_items c ON c.id=r.cleanup_item_id"
        " WHERE c.id=? AND r.kind=? ORDER BY a.attempt_no DESC LIMIT 1",
        (item_id, int(enum_for("operation_runs.kind").CHECK_FILE_EXISTS)),
    ).fetchone()
    if row is None:
        return None
    (attempt_id, attempt_no, status, effect, result_raw, error_raw, result_event,
     intent_event, run_id, responsibility, action_id, kind, member_action, output_id) = row
    if responsibility != f"exists/{item_id}" or action_id != member_action:
        raise ConsistencyError("已保存清理查询的流程归属与成员不符")
    latest_delete = connection.execute(
        "SELECT MAX(a.intent_event_id) FROM operation_attempts a"
        " JOIN operation_runs r ON r.id=a.run_id"
        " JOIN cleanup_items c ON c.id=r.cleanup_item_id"
        " WHERE c.output_id=? AND r.kind=?",
        (output_id, int(enum_for("operation_runs.kind").DELETE_FILE)),
    ).fetchone()[0]
    if latest_delete is not None and latest_delete > intent_event:
        return None
    if status == int(enum_for("operation_attempts.status").RUNNING):
        if result_raw is not None or error_raw is not None or result_event is not None:
            raise ConsistencyError("运行中的清理查询包含结束结果")
        return None
    ticket = AttemptTicket(attempt_no, "query", str(item_id), responsibility, run_id)
    saved = _read_exact_cleanup_attempt(connection, ticket, item_id)
    return SavedCleanupQuery(saved.ticket, saved.actual, saved.key, saved.occurred_at,
                             latest_delete is not None)


@dataclass(frozen=True)
class SavedCleanupAttempt:
    """按原票据取得的可靠结果；不选择最新尝试，也不重新验证驱动。"""

    ticket: AttemptTicket
    actual: DeviceCallResult
    key: OperationKey
    occurred_at: int


def saved_cleanup_attempt(owned, ticket: AttemptTicket, *, database_id: str,
                          item_id: int) -> SavedCleanupAttempt:
    """核实同会话 ALREADY_ENDED 的原结果，供独立恢复消费者复用。"""
    if owned.metadata.instance_id != database_id:
        raise ConsistencyError("原清理票据不属于本次数据库")
    return _read_exact_cleanup_attempt(owned.connection, ticket, item_id)



def saved_cleanup_not_dispatched(connection, ticket: AttemptTicket, item_id: int) -> bool:
    """只用精确票据的完整原保存事实判断未派发，不信任投影片段。"""
    from camctl.operations.models import AttemptStatus, EffectState, SettlementBasis
    saved = _read_exact_cleanup_attempt(connection, ticket, item_id)
    outcome = saved.actual.outcome
    if outcome.settlement.basis is not SettlementBasis.NOT_DISPATCHED:
        return False
    evidence = outcome.settlement.evidence
    if (outcome.status is not AttemptStatus.FAILED or outcome.effect is not EffectState.NO_EFFECT
            or evidence.type != "dispatch_prevented" or type(evidence.version) is not int
            or evidence.version != 1 or evidence.data != {}):
        raise ConsistencyError("原清理未派发结果不符合固定证据契约")
    return True

def _transaction_for_event(connection, event_id):
    row = connection.execute(
        "SELECT t.operation_key FROM history_events e"
        " JOIN history_transactions t ON t.id=e.transaction_id WHERE e.id=?", (event_id,)).fetchone()
    if row is None:
        raise ConsistencyError("原清理尝试缺少完整事务")
    key = OperationKey(row[0])
    events = saved_transaction_events(connection, key)
    if not events:
        raise ConsistencyError("原清理尝试事务缺少事件")
    matches = [event for event in events if event["event_id"] == event_id]
    if len(matches) != 1:
        raise ConsistencyError("原清理尝试事件不属于完整事务")
    return key, matches[0]


def _read_exact_cleanup_attempt(connection, ticket, item_id):
    kinds = {"delete": ("DELETE_FILE", "delete"), "query": ("CHECK_FILE_EXISTS", "exists")}
    if ticket.operation not in kinds or ticket.target_id != str(item_id):
        raise ConsistencyError("原清理票据的操作或目标不对应")
    kind_name, prefix = kinds[ticket.operation]
    if ticket.responsibility_key != f"{prefix}/{item_id}":
        raise ConsistencyError("原清理票据的责任键不对应")
    row = connection.execute(
        "SELECT a.id,a.status,a.effect_state,a.result_json,a.error_json,a.result_event_id,"
        " a.intent_event_id,r.kind,r.responsibility_key,r.action_id,c.action_id,c.output_id,"
        " r.query_purpose,r.activity_id,r.copy_id,r.delivery_id,r.session_key,act.type"
        " FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
        " JOIN cleanup_items c ON c.id=r.cleanup_item_id JOIN actions act ON act.id=r.action_id"
        " WHERE r.id=? AND a.attempt_no=? AND c.id=?",
        (ticket.run_id, ticket.attempt_id, item_id)).fetchone()
    if row is None:
        raise ConsistencyError("原清理票据缺少精确匹配的尝试")
    (attempt_id, status, effect, result_raw, error_raw, result_event, intent_event,
     kind, responsibility, action_id, member_action, output_id, *other) = row
    if (kind != int(enum_for("operation_runs.kind")[kind_name])
            or responsibility != ticket.responsibility_key or action_id != member_action
            or output_id is None or other[:-1] != [None] * 5 or other[-1] != 5):
        raise ConsistencyError("原清理票据的流程、成员或动作归属不对应")
    if status == int(enum_for("operation_attempts.status").RUNNING) or result_raw is None or result_event is None:
        raise ConsistencyError("原清理尝试没有可靠完整结束结果")
    try:
        _intent_key, intent = _transaction_for_event(connection, intent_event)
        if event_type_name(intent["type"]) != "ATTEMPT_STARTED":
            raise ConsistencyError("原清理意图引用不是尝试开始事件")
        attempts = [change for change in intent["body"]["rows"]
                    if change["table"] == "operation_attempts" and change["id"] == attempt_id]
        if (len(attempts) != 1 or attempts[0]["before"]["exists"]
                or not attempts[0]["after"]["exists"]):
            raise ConsistencyError("原清理意图缺少对应尝试的完整创建事实")
        original_attempt = attempts[0]["after"]["values"]
        if (original_attempt.get("run_id") != ticket.run_id
                or original_attempt.get("attempt_no") != ticket.attempt_id
                or original_attempt.get("intent_event_id") != intent_event):
            raise ConsistencyError("原清理票据与原意图创建身份不同")
        first = connection.execute(
            "SELECT intent_event_id FROM operation_attempts WHERE run_id=? AND attempt_no=1",
            (ticket.run_id,)).fetchone()
        if first is None:
            raise ConsistencyError("原清理流程缺少首次意图")
        _run_key, run_created = _transaction_for_event(connection, first[0])
        runs = [change for change in run_created["body"]["rows"]
                if change["table"] == "operation_runs" and change["id"] == ticket.run_id]
        expected_run = {"action_id": action_id, "kind": kind,
                        "responsibility_key": responsibility, "cleanup_item_id": item_id,
                        "query_purpose": None, "activity_id": None, "copy_id": None,
                        "delivery_id": None, "session_key": None}
        if (event_type_name(run_created["type"]) != "ATTEMPT_STARTED" or len(runs) != 1
                or runs[0]["before"]["exists"] or not runs[0]["after"]["exists"]
                or any(not json_equal(runs[0]["after"]["values"].get(name), value)
                       for name, value in expected_run.items())):
            raise ConsistencyError("原清理流程的固定身份与创建历史不同")
        key, result_event_row = _transaction_for_event(connection, result_event)
        if event_type_name(result_event_row["type"]) != "ATTEMPT_RESULT":
            raise ConsistencyError("原清理结果引用不是尝试结束事件")
        result = parse_exact_json(result_raw)
        error = None if error_raw is None else parse_exact_json(error_raw)
        outcome = read_result_document(status, effect, result, error)
        rows = result_event_row["body"]["rows"]
        expected = {"status": status, "effect_state": effect, "result_json": result,
                    "error_json": error, "result_event_id": result_event}
        initial = {name: original_attempt[name] for name in expected}
        changed = {name for name in expected if not json_equal(initial[name], expected[name])}
        if (len(rows) != 1 or rows[0]["table"] != "operation_attempts"
                or rows[0]["id"] != attempt_id or not rows[0]["before"]["exists"]
                or not rows[0]["after"]["exists"]
                or not json_equal(rows[0]["before"]["values"], {name: initial[name] for name in changed})
                or not json_equal(rows[0]["after"]["values"], {name: expected[name] for name in changed})):
            raise ConsistencyError("原清理尝试的结果与完整历史不符")
        if ticket.operation == "query":
            saved_query_presence(outcome, item_id)
        else:
            from camctl.outputs.cleanup_flow import _delete_presence
            _delete_presence(outcome, item_id)
        return SavedCleanupAttempt(ticket, DeviceCallResult.from_outcome(outcome), key,
                                   result_event_row["occurred_at"])
    except (KeyError, TypeError, ValueError) as error:
        raise ConsistencyError("原清理尝试的完整结果不可解释") from error

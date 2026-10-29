"""从已提交的原尝试及完整历史边界恢复清理结果，不调用当前驱动。"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal
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
    running = status == int(enum_for("operation_attempts.status").RUNNING)
    ticket = AttemptTicket(attempt_no, "query", str(item_id), responsibility, run_id)
    saved = _read_exact_cleanup_attempt(connection, ticket, item_id, allow_running=running)
    possible_delete = latest_possible_cleanup_delete(connection, output_id)
    latest_delete = None if possible_delete is None else possible_delete[6]
    if running or (latest_delete is not None and latest_delete > intent_event):
        return None
    return SavedCleanupQuery(saved.ticket, saved.actual, saved.key, saved.occurred_at,
                             latest_delete is not None)


def saved_cleanup_delete(connection, item_id: int) -> SavedCleanupAttempt | None:
    """从本成员最后一次 DELETE 的原完整事务恢复结果。"""
    row = connection.execute(
        "SELECT a.attempt_no,a.status,r.id,r.responsibility_key"
        " FROM operation_runs r JOIN operation_attempts a ON a.run_id=r.id"
        " WHERE r.cleanup_item_id=? AND r.kind=4 ORDER BY a.attempt_no DESC LIMIT 1",
        (item_id,)).fetchone()
    if row is None or row[1] == int(enum_for("operation_attempts.status").RUNNING):
        return None
    return _read_exact_cleanup_attempt(connection,
        AttemptTicket(row[0], "delete", str(item_id), row[3], row[2]), item_id)


@dataclass(frozen=True)
class SavedCleanupAttempt:
    """按原票据取得的可靠结果；不选择最新尝试，也不重新验证驱动。"""

    ticket: AttemptTicket
    actual: DeviceCallResult
    key: OperationKey
    occurred_at: int
    result_event_id: int | None = None


def saved_cleanup_attempt(owned, ticket: AttemptTicket, *, database_id: str,
                          item_id: int) -> SavedCleanupAttempt:
    """核实同会话 ALREADY_ENDED 的原结果，供独立恢复消费者复用。"""
    if owned.metadata.instance_id != database_id:
        raise ConsistencyError("原清理票据不属于本次数据库")
    return _read_exact_cleanup_attempt(owned.connection, ticket, item_id)



def saved_cleanup_not_dispatched(connection, ticket: AttemptTicket, item_id: int) -> bool:
    """只用精确票据的完整原保存事实判断未派发，不信任投影片段。"""
    from camctl.operations.models import AttemptStatus, EffectState, SettlementBasis
    from camctl.devices.evidence import FILE_DELETE_NOT_DISPATCHED, FILE_QUERY_NOT_DISPATCHED
    saved = _read_exact_cleanup_attempt(connection, ticket, item_id)
    outcome = saved.actual.outcome
    if outcome.settlement.basis is not SettlementBasis.NOT_DISPATCHED:
        return False
    evidence = outcome.settlement.evidence
    file_contract = (FILE_DELETE_NOT_DISPATCHED if ticket.operation == "delete"
                     else FILE_QUERY_NOT_DISPATCHED)
    if (outcome.status is not AttemptStatus.FAILED or outcome.effect is not EffectState.NO_EFFECT
            or evidence.type not in ("dispatch_prevented", file_contract.type) or type(evidence.version) is not int
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


def _read_cleanup_attempt_creation(connection, ticket, item_id):
    """共享原票据、创建事务及固定字段核验，不读取当前协作者配置。"""
    kinds = {"delete": ("DELETE_FILE", "delete"), "query": ("CHECK_FILE_EXISTS", "exists")}
    if ticket.operation not in kinds or ticket.target_id != str(item_id):
        raise ConsistencyError("原清理票据的操作或目标不对应")
    kind_name, prefix = kinds[ticket.operation]
    if ticket.responsibility_key != f"{prefix}/{item_id}":
        raise ConsistencyError("原清理票据的责任键不对应")
    row = connection.execute(
        "SELECT a.id,a.intent_event_id,r.kind,r.responsibility_key,r.action_id,c.action_id,c.output_id,"
        " r.query_purpose,r.activity_id,r.copy_id,r.delivery_id,r.session_key,act.type"
        " FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
        " JOIN cleanup_items c ON c.id=r.cleanup_item_id JOIN actions act ON act.id=r.action_id"
        " WHERE r.id=? AND a.attempt_no=? AND c.id=?",
        (ticket.run_id, ticket.attempt_id, item_id)).fetchone()
    if row is None:
        raise ConsistencyError("原清理票据缺少精确匹配的尝试")
    (attempt_id, intent_event, kind, responsibility, action_id, member_action, output_id, *other) = row
    if (kind != int(enum_for("operation_runs.kind")[kind_name])
            or responsibility != ticket.responsibility_key or action_id != member_action
            or output_id is None or other[:-1] != [None] * 5 or other[-1] != 5):
        raise ConsistencyError("原清理票据的流程、成员或动作归属不对应")
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
        from camctl.persistence.transaction import row_facts
        current = row_facts(connection, "operation_attempts", attempt_id)
        mutable = {"status", "effect_state", "result_json", "error_json", "result_event_id"}
        if any(not json_equal(current[name], value) for name, value in original_attempt.items()
               if name not in mutable):
            raise ConsistencyError("原清理尝试的固定字段与创建历史不同")
        return current, original_attempt
    except (KeyError, TypeError, ValueError) as error:
        raise ConsistencyError("原清理尝试的完整创建事实不可解释") from error


def cleanup_attempt_config(owned, ticket, *, database_id, item_id):
    """只从精确原 attempt 及其创建历史恢复不可变配置。"""
    if owned.metadata.instance_id != database_id:
        raise ConsistencyError("原清理配置票据不属于本次数据库")
    from camctl.operations.attempts import AttemptConfig
    try:
        _current, original = _read_cleanup_attempt_creation(owned.connection, ticket, item_id)
        return AttemptConfig(original["max_attempts_used"], original["timeout_s_json"],
                             original["retry_interval_s_json"])
    except (KeyError, TypeError, ValueError) as error:
        raise ConsistencyError(
            f"原清理配置不可解释: item={item_id} run={ticket.run_id} attempt={ticket.attempt_id}") from error


def _read_exact_cleanup_attempt(connection, ticket, item_id, *, allow_running=False):
    current, original_attempt = _read_cleanup_attempt_creation(connection, ticket, item_id)
    attempt_id, status, effect = current["id"], current["status"], current["effect_state"]
    result, error = current["result_json"], current["error_json"]
    result_event, intent_event = current["result_event_id"], current["intent_event_id"]
    try:
        if status == int(enum_for("operation_attempts.status").RUNNING):
            if not allow_running or any(not json_equal(current[name], value)
                    for name, value in original_attempt.items()):
                raise ConsistencyError("原清理尝试没有可靠完整结束结果")
            return ticket, intent_event
        if result is None or result_event is None:
            raise ConsistencyError("原清理尝试没有可靠完整结束结果")
        key, result_event_row = _transaction_for_event(connection, result_event)
        if event_type_name(result_event_row["type"]) != "ATTEMPT_RESULT":
            raise ConsistencyError("原清理结果引用不是尝试结束事件")
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
                                   result_event_row["occurred_at"], result_event)
    except (KeyError, TypeError, ValueError) as error:
        raise ConsistencyError("原清理尝试的完整结果不可解释") from error


def saved_cleanup_absence(connection, item_id):
    """逐票据读取原可靠结果；文件缺席不从效果或迟到返回推导。"""
    from camctl.outputs.cleanup_flow import _delete_presence
    before = None
    while True:
        bound = "" if before is None else " AND a.id<?"
        parameters = (item_id,) if before is None else (item_id,before)
        row = connection.execute(
            "SELECT a.id,a.attempt_no,r.kind,r.id,r.responsibility_key"
            " FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
            " WHERE r.cleanup_item_id=? AND r.kind IN (4,5) AND a.status<>1" + bound +
            " ORDER BY a.id DESC LIMIT 1", parameters).fetchone()
        if row is None:
            return None
        before,attempt,kind,run,responsibility = row
        operation = "delete" if kind == 4 else "query"
        saved = _read_exact_cleanup_attempt(connection,
            AttemptTicket(attempt,operation,str(item_id),responsibility,run),item_id)
        present = (_delete_presence(saved.actual,item_id) if operation == "delete"
                   else saved_query_presence(saved.actual,item_id))
        if present is False:
            return saved


def running_cleanup_attempt(connection, item_id):
    """恢复前核实本成员唯一 RUNNING 的原意图、流程和完整事务。"""
    rows = connection.execute(
        "SELECT a.attempt_no,r.kind,r.id,r.responsibility_key"
        " FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
        " WHERE r.cleanup_item_id=? AND r.kind IN (4,5) AND a.status=1 LIMIT 2",
        (item_id,)).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise ConsistencyError("清理成员存在多个未收场物理尝试")
    attempt, kind, run_id, responsibility = rows[0]
    ticket = AttemptTicket(attempt, "delete" if kind == 4 else "query", str(item_id), responsibility, run_id)
    return _read_exact_cleanup_attempt(connection, ticket, item_id, allow_running=True)


def latest_possible_cleanup_delete(connection, output_id, *, unresolved_only=False, exclude_ticket=None):
    """逐票据核对原完整事实；可靠未派发不形成新的文件阶段。"""
    from camctl.operations.models import SettlementBasis
    before = None
    while True:
        filters = "" if before is None else " AND a.id<?"
        parameters = [output_id] if before is None else [output_id, before]
        if exclude_ticket is not None:
            filters += " AND NOT (r.id=? AND a.attempt_no=?)"
            parameters.extend((exclude_ticket.run_id, exclude_ticket.attempt_id))
        row = connection.execute(
            "SELECT a.id,a.status,a.effect_state,a.result_json,a.error_json,c.id,"
            "a.intent_event_id,a.attempt_no,r.responsibility_key,r.id"
            " FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
            " JOIN cleanup_items c ON c.id=r.cleanup_item_id"
            " WHERE c.output_id=? AND r.kind=4" + filters + " ORDER BY a.id DESC LIMIT 1",
            parameters).fetchone()
        if row is None:
            return None
        ticket = AttemptTicket(row[7], "delete", str(row[5]), row[8], row[9])
        if row[1] == int(enum_for("operation_attempts.status").RUNNING):
            _read_exact_cleanup_attempt(connection, ticket, row[5], allow_running=True)
            return row[:7]
        saved = _read_exact_cleanup_attempt(connection, ticket, row[5])
        if saved.actual.outcome.settlement.basis is SettlementBasis.NOT_DISPATCHED:
            saved_cleanup_not_dispatched(connection, ticket, row[5])
        elif not unresolved_only or row[2] != int(enum_for("operation_attempts.effect_state").CONFIRMED):
            return row[:7]
        before = row[0]


def cleanup_member_delete_state(connection, item_id):
    """有界核对本成员全部 DELETE，返回自身可能派发及未收场事实。"""
    possible = unsettled = False
    after = 0
    while True:
        row = connection.execute(
            "SELECT a.id,a.attempt_no,a.status,r.id,r.responsibility_key"
            " FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
            " WHERE r.cleanup_item_id=? AND r.kind=4 AND a.id>? ORDER BY a.id LIMIT 1",
            (item_id,after)).fetchone()
        if row is None:
            return possible, unsettled
        after, attempt, status, run_id, responsibility = row
        ticket = AttemptTicket(attempt, "delete", str(item_id), responsibility, run_id)
        if status == int(enum_for("operation_attempts.status").RUNNING):
            _read_exact_cleanup_attempt(connection,ticket,item_id,allow_running=True)
            possible = unsettled = True
        elif not saved_cleanup_not_dispatched(connection,ticket,item_id):
            possible = True


def cleanup_cancel_query_available(connection, item_id, *, ticket=None):
    """取消后的唯一机会以原 0→1 事件和原 QUERY 授票历史判定。"""
    from camctl.contracts.enums import load_registry
    member = connection.execute(
        "SELECT c.action_id,a.cancel_requested FROM cleanup_items c"
        " JOIN actions a ON a.id=c.action_id WHERE c.id=?", (item_id,)).fetchone()
    if member is None:
        raise ConsistencyError("取消核实缺少原清理成员")
    if not member[1]:
        return False
    possible, unsettled = cleanup_member_delete_state(connection,item_id)
    if not possible or unsettled:
        return False
    rows = connection.execute(
        "SELECT e.id FROM entity_event_links l JOIN history_events e ON e.id=l.event_id"
        " JOIN json_each(e.body_json,'$.rows') j"
        " WHERE l.entity_type=? AND l.entity_id=?"
        " AND json_extract(j.value,'$.table')='actions'"
        " AND json_extract(j.value,'$.id')=?"
        " AND json_extract(j.value,'$.before.values.cancel_requested')=0"
        " AND json_extract(j.value,'$.after.values.cancel_requested')=1"
        " ORDER BY e.id LIMIT 2",
        (load_registry()["history_objects"]["action"]["id"],member[0],member[0])).fetchall()
    if len(rows) != 1:
        raise ConsistencyError("清理取消缺少唯一可靠生效边界")
    boundary = rows[0][0]
    _key,event = _transaction_for_event(connection,boundary)
    if event_type_name(event["type"]) != "CANCEL_CHANGED" or event["reason"] != 1:
        raise ConsistencyError("清理取消边界不属于原取消生效事务")
    grants = connection.execute(
        "SELECT a.attempt_no,a.status,r.id,r.responsibility_key"
        " FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
        " WHERE r.cleanup_item_id=? AND r.kind=5 AND a.intent_event_id>?"
        " ORDER BY a.intent_event_id LIMIT 2",(item_id,boundary)).fetchall()
    if len(grants) > 1:
        raise ConsistencyError("清理取消核实存在多个已授予机会")
    granted = grants[0] if grants else None
    if granted is None:
        if ticket is None:
            return True
        running = _read_exact_cleanup_attempt(connection,ticket,item_id,allow_running=True)
        return isinstance(running, tuple) and running[0] == ticket and running[1] < boundary
    attempt,status,run_id,responsibility = granted
    original = AttemptTicket(attempt,"query",str(item_id),responsibility,run_id)
    _read_exact_cleanup_attempt(connection,original,item_id,allow_running=True)
    return ticket == original and status == int(enum_for("operation_attempts.status").RUNNING)

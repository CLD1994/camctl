"""从已提交的原尝试及完整历史边界恢复清理查询，不调用当前驱动。"""

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
    if result_raw is None or result_event is None:
        raise ConsistencyError("已结束清理查询缺少完整结果或事件引用")
    transaction = connection.execute(
        "SELECT t.operation_key FROM history_events e"
        " JOIN history_transactions t ON t.id=e.transaction_id WHERE e.id=?",
        (result_event,),
    ).fetchone()
    if transaction is None:
        raise ConsistencyError("已保存清理查询缺少原完整事务")
    try:
        key = OperationKey(transaction[0])
        result = parse_exact_json(result_raw)
        error = None if error_raw is None else parse_exact_json(error_raw)
        outcome = read_result_document(status, effect, result, error)
        events = saved_transaction_events(connection, key)
        if (not events or events[0]["event_id"] != result_event
                or event_type_name(events[0]["type"]) != "ATTEMPT_RESULT"):
            raise ConsistencyError("清理查询引用的事件不是原尝试结果")
        rows = events[0]["body"]["rows"]
        expected = {"status": status, "effect_state": effect, "result_json": result,
                    "error_json": error, "result_event_id": result_event}
        # QUERY 的首次结束前只有意图，五项结果字段具有固定初值。
        # 历史只保存变化列；未变化的 UNKNOWN 效果和空错误仍是原事实。
        original = {"status": int(enum_for("operation_attempts.status").RUNNING),
                    "effect_state": int(enum_for("operation_attempts.effect_state").UNKNOWN),
                    "result_json": None, "error_json": None, "result_event_id": None}
        changed = {name for name in expected if not json_equal(original[name], expected[name])}
        if (len(rows) != 1 or rows[0]["table"] != "operation_attempts"
                or rows[0]["id"] != attempt_id or not rows[0]["before"]["exists"]
                or not rows[0]["after"]["exists"]
                or not json_equal(rows[0]["before"]["values"], {name: original[name] for name in changed})
                or not json_equal(rows[0]["after"]["values"], {name: expected[name] for name in changed})):
            raise ConsistencyError("清理查询结果与原历史事实不符")
        saved_query_presence(outcome, item_id)
        return SavedCleanupQuery(
            AttemptTicket(attempt_no, "query", str(item_id), responsibility, run_id),
            DeviceCallResult.from_outcome(outcome), key, events[0]["occurred_at"],
            latest_delete is not None,
        )
    except (KeyError, TypeError, ValueError) as failure:
        raise ConsistencyError("已保存清理查询的完整结果不可解释") from failure

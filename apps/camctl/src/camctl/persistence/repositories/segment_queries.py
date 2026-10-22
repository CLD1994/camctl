"""显式分段取回的文件 QUERY 结果与来源项结算。"""

from dataclasses import dataclass

from camctl.contracts.json_values import json_equal, parse_exact_json
from camctl.contracts.values import ConsistencyError, OperationKey, ObjectId, UtcMicros
from camctl.contracts.workflow_errors import item_error_id, validate_error_details
from camctl.history.decoding import decode_event_row
from camctl.history.events import load_event_registry
from camctl.history.reads import PlanReadScope
from camctl.history.validators import EventValidationError, register_guard
from camctl.operations.result_format import read_result_document
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.transaction import (
    CommandPlan,
    commit_operation,
    event_envelope,
    row_facts,
    saved_transaction_events,
    update_change,
)
from .segment_selections import _at, is_segmented_source

_EVENTS = load_event_registry()["events"]
_OUTPUT_EVENT = _EVENTS["OUTPUT_REGISTERED"]["id"]
_OUTPUT_REASON = _EVENTS["OUTPUT_REGISTERED"]["branches"]["OBSERVATION"]["reason"]
_READ_EVENT = _EVENTS["READ_PERMISSION_CHANGED"]["id"]
_READ_BRANCHES = _EVENTS["READ_PERMISSION_CHANGED"]["branches"]
_RESOLVE_REASON = _READ_BRANCHES["RESOLVE"]["reason"]
_REJECT_REASON = _READ_BRANCHES["REJECT"]["reason"]
_OUTPUT_UNAVAILABLE = item_error_id("obtain_items", "output_unavailable")
_SOURCE_UNCONFIRMED = item_error_id("obtain_items", "source_file_unconfirmed")
_DEVICE_BINDING = item_error_id("obtain_items", "device_binding_unavailable")


@dataclass(frozen=True)
class ObserveQueriedOutput:
    item_id: int
    result_event_id: int
    present: bool
    occurred_at: int

    def __post_init__(self):
        ObjectId(self.item_id)
        ObjectId(self.result_event_id)
        if type(self.present) is not bool:
            raise TypeError("查询在场结果必须是布尔值")
        UtcMicros(self.occurred_at)


@dataclass(frozen=True)
class SettleQueriedItem:
    item_id: int
    occurred_at: int
    error_code: int | None = None
    error_details: dict | None = None
    result_event_id: int | None = None

    def __post_init__(self):
        ObjectId(self.item_id)
        UtcMicros(self.occurred_at)
        if (self.error_code is None) != (self.error_details is None):
            raise ValueError("来源项失败码和详情必须同时提供")
        if self.result_event_id is not None:
            ObjectId(self.result_event_id)


def _one(connection, sql, parameters=()):
    row = connection.execute(sql, parameters).fetchone()
    return None if row is None else tuple(row)


def _query_presence(connection, item_id, result_event_id):
    """读取并核对原 CHECK_FILE_EXISTS 实际结束和 v2 观察。"""
    item = row_facts(connection, "obtain_items", item_id)
    if item is None:
        raise ConsistencyError("QUERY 来源项不存在")
    found = _one(
        connection,
        "SELECT r.id,r.action_id,r.kind,r.obtain_item_id,r.responsibility_key,"
        " a.type,a.status,a.cancel_requested,a.execution_spec_json"
        " FROM operation_runs r JOIN actions a ON a.id=r.action_id"
        " WHERE r.responsibility_key=?",
        (f"obtain-exists/{item_id}",),
    )
    if found is None:
        raise ConsistencyError("来源项缺少唯一 QUERY v2 流程")
    (
        run_id,
        owner_id,
        kind,
        target,
        responsibility,
        action_type,
        action_status,
        canceled,
        spec,
    ) = found
    dependency = _one(
        connection,
        "SELECT d.action_id,d.depends_on_action_id FROM obtain_source_selections s"
        " JOIN action_dependencies d ON d.id=s.dependency_id WHERE s.id=?",
        (item["selection_id"],),
    )
    source = (
        None if dependency is None else row_facts(connection, "actions", dependency[1])
    )
    if (
        kind != 5
        or target != item_id
        or responsibility != f"obtain-exists/{item_id}"
        or action_type != 4
        or dependency is None
        or dependency[0] != owner_id
        or source is None
        or not is_segmented_source(connection, source["id"])
    ):
        raise ConsistencyError("QUERY v2 责任不属于原分段来源取回")
    attempt = _one(
        connection,
        "SELECT id,attempt_no,status,result_event_id,result_json,error_json,effect_state,intent_event_id"
        " FROM operation_attempts WHERE run_id=? AND result_event_id=?",
        (run_id, result_event_id),
    )
    if attempt is None or attempt[3] is None or attempt[4] is None:
        raise ConsistencyError("QUERY 来源事件没有原完整尝试结果")
    eventrow = _one(
        connection,
        "SELECT id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json"
        " FROM history_events WHERE id=?",
        (result_event_id,),
    )
    event = None if eventrow is None else decode_event_row(eventrow)
    if event is None or event.event_type != _EVENTS["ATTEMPT_RESULT"]["id"]:
        raise ConsistencyError("QUERY 来源不是 ATTEMPT_RESULT 事件")
    intent = decode_event_row(
        _one(
            connection,
            "SELECT id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json"
            " FROM history_events WHERE id=?",
            (attempt[7],),
        )
    )
    created = [
        row
        for row in intent.rows
        if row.table == "operation_attempts"
        and row.row_id == attempt[0]
        and not row.before.exists
        and row.after.exists
    ]
    changed = [
        row
        for row in event.rows
        if row.table == "operation_attempts" and row.row_id == attempt[0]
    ]
    resulting = (
        {}
        if len(created) != 1 or len(changed) != 1
        else {**created[0].after.values, **changed[0].after.values}
    )
    expected = dict(
        status=attempt[2],
        result_event_id=result_event_id,
        result_json=parse_exact_json(attempt[4]),
        error_json=None if attempt[5] is None else parse_exact_json(attempt[5]),
        effect_state=attempt[6],
    )
    if any(
        not json_equal(resulting.get(name), value) for name, value in expected.items()
    ):
        raise ConsistencyError("QUERY 结果事件与原尝试行不同")
    outcome = read_result_document(
        attempt[2],
        attempt[6],
        parse_exact_json(attempt[4]),
        None if attempt[5] is None else parse_exact_json(attempt[5]),
    )
    observations = [
        value
        for value in outcome.observations
        if value.type == "file_presence" and value.version == 2
    ]
    if len(observations) > 1:
        raise ConsistencyError("QUERY v2 结果重复报告在场事实")
    if observations:
        observation = observations[0]
        if observation.data.get("obtain_item_id") != str(item_id):
            raise ConsistencyError("QUERY v2 观察改变原取回项身份")
        return observation.data["present"], run_id, owner_id, outcome, attempt
    return None, run_id, owner_id, outcome, attempt


def _segment_observation_guard(event, context):
    source = event.evidence.get("obtain_check_source")
    if source is None:
        return
    if event.event_type != _OUTPUT_EVENT or event.reason != _OUTPUT_REASON:
        raise EventValidationError("obtain_check_source 只能用于产物观察事件")
    connection = context.current_reads.connection
    present, _, _, _, _ = _query_presence(
        connection, source["item_id"], source["result_event_id"]
    )
    if present is None or len(event.rows) != 1:
        raise EventValidationError("产物状态更新必须由可靠 QUERY v2 在场结果证明")
    row = event.rows[0]
    output = context.state_rows.get("outputs", {}).get(row.row_id)
    item = context.state_rows.get("obtain_items", {}).get(source["item_id"])
    if (
        row.table != "outputs"
        or output is None
        or item is None
        or row.row_id != item["requested_output_id"]
    ):
        raise EventValidationError("QUERY v2 产物观察缺少原产物或取回项")
    expected = 1 if present else 4
    expected_error = (
        None
        if present
        else {
            "code": "output_unavailable",
            "stage": "output_selection",
            "details": {"output_id": str(output["id"]), "availability": "missing"},
        }
    )
    file = context.state_rows.get("device_files", {}).get(output["device_file_id"])
    if (
        output["device_file_id"] is None
        or file is None
        or file["presence_state"] != (2 if present else 3)
        or set(row.after.values) != {"availability", "error_json"}
        or row.after.values.get("availability") != expected
        or not json_equal(row.after.values.get("error_json"), expected_error)
        or row.before.values.get("availability") != 5
    ):
        raise EventValidationError("QUERY v2 产物投影与原文件结果不符")


def _segment_resolution_guard(event, context):
    source = event.evidence.get("obtain_check_source")
    if source is None:
        return
    if event.event_type != _READ_EVENT or event.reason not in (
        _RESOLVE_REASON,
        _REJECT_REASON,
    ):
        raise EventValidationError("obtain_check_source 只能用于显式取回项结算")
    present, _, _, _, _ = _query_presence(
        context.current_reads.connection, source["item_id"], source["result_event_id"]
    )
    if event.reason == _RESOLVE_REASON and present is not True:
        raise EventValidationError("显式取回解析必须由可靠在场结果证明")
    if event.reason == _REJECT_REASON and present is not False:
        raise EventValidationError("缺席拒绝必须由可靠缺席结果证明")
    if len(event.rows) != 1 or event.rows[0].row_id != source["item_id"]:
        raise EventValidationError("QUERY v2 结算必须只更新原取回项")


def register_segment_query_guards():
    register_guard("segment_query_observation", _segment_observation_guard)
    register_guard("segment_query_resolution", _segment_resolution_guard)


def _facts(connection, item_id, saved):
    current = row_facts(connection, "obtain_items", item_id)
    if current is None:
        raise ConsistencyError("原显式取回项不存在")
    selection = row_facts(
        connection, "obtain_source_selections", current["selection_id"]
    )
    dependency = row_facts(
        connection, "action_dependencies", selection["dependency_id"]
    )
    owner_id, source_id = dependency["action_id"], dependency["depends_on_action_id"]
    owner = _at(connection, "actions", owner_id, saved, ("action", owner_id))
    item = _at(connection, "obtain_items", item_id, saved, ("action", owner_id))
    output_id = item["requested_output_id"]
    output = _at(connection, "outputs", output_id, saved, ("output", output_id))
    source = row_facts(connection, "actions", source_id)
    if (
        item["status"] != 1
        or item["basis"] != 5
        or item["output_id"] is not None
        or selection["status"] != 2
        or selection["error_code"] is not None
        or owner["status"] != 2
        or owner["cancel_requested"]
        or output is None
        or output["source_action_id"] != source_id
        or not is_segmented_source(connection, source["id"])
    ):
        raise ConsistencyError("QUERY 结算要求执行中显式未解析项和原分段产物")
    state = {
        "actions": {owner_id: owner, source_id: source},
        "obtain_items": {item_id: item},
        "outputs": {output_id: output},
        "action_dependencies": {dependency["id"]: dependency},
        "obtain_source_selections": {selection["id"]: selection},
        "device_files": {},
    }
    if output["device_file_id"] is not None:
        file_id = output["device_file_id"]
        state["device_files"][file_id] = _at(
            connection, "device_files", file_id, saved, ("device_file", file_id)
        )
    return item, output, owner, state


def _event_matches(saved, event):
    if (
        len(saved) != 1
        or saved[0]["type"] != event.event_type
        or saved[0]["reason"] != event.reason
        or saved[0]["occurred_at"] != event.occurred_at
        or not json_equal(saved[0]["body"]["evidence"], dict(event.evidence))
        or not json_equal(
            saved[0]["body"]["rows"],
            [
                {
                    "table": row.table,
                    "id": row.row_id,
                    "before": {
                        "exists": row.before.exists,
                        "values": dict(row.before.values),
                    },
                    "after": {
                        "exists": row.after.exists,
                        "values": dict(row.after.values),
                    },
                }
                for row in event.rows
            ],
        )
    ):
        raise ConsistencyError("QUERY 业务原键与完整原申请不同")


class _ObserveCommand:
    def __init__(self, request, key):
        self.request, self.key = request, OperationKey(key)

    def plan(self, scope):
        c, r = scope.connection, self.request
        saved = saved_transaction_events(c, self.key)
        transaction = None if saved is None else saved[0]["transaction"]
        present, _, owner_id, _, _ = _query_presence(c, r.item_id, r.result_event_id)
        if present is not r.present:
            raise ConsistencyError("产物观察申请与 QUERY v2 原结果不同")
        item, output, owner, state = _facts(c, r.item_id, transaction)
        output_id = output["id"]
        if (
            output["device_file_id"] is None
            or output["availability"] != 5
            or output["cleanup_status"] not in (1, 6)
        ):
            raise ConsistencyError("只有未知且无清理限制的显式设备产物可应用 QUERY v2")
        file = state["device_files"][output["device_file_id"]]
        if file is None or file["presence_state"] != (2 if present else 3):
            raise ConsistencyError("文件存在性事实尚未先行保存")
        after = {
            "availability": 1 if present else 4,
            "error_json": None
            if present
            else {
                "code": "output_unavailable",
                "stage": "output_selection",
                "details": {"output_id": str(output_id), "availability": "missing"},
            },
        }
        row = update_change(
            "outputs",
            output_id,
            {"availability": 5, "error_json": output["error_json"]},
            after,
        )
        event_id = scope.max_event_id + 1
        evidence = {
            "obtain_check_source": {
                "item_id": r.item_id,
                "result_event_id": r.result_event_id,
            }
        }
        event = event_envelope(
            event_id,
            scope.max_txn_id + 1,
            _OUTPUT_EVENT,
            _OUTPUT_REASON,
            (row,),
            r.occurred_at,
            evidence,
        )
        allocation = None if saved is not None else scope.allocate(1)
        from dataclasses import replace

        if allocation is not None:
            event = replace(
                event,
                event_id=allocation.first_event_id,
                transaction_id=allocation.txn_id,
            )
        owners = {("outputs", output_id): ("output", output_id)}
        if saved is not None:
            _event_matches(saved, event)
            return CommandPlan((), owners, state, True, True)
        return CommandPlan(
            (event,),
            owners,
            state,
            result=True,
            plan_read_scope=PlanReadScope(owner["plan_id"], 128),
        )


class _SettleCommand:
    def __init__(self, request, key):
        self.request, self.key = request, OperationKey(key)

    def plan(self, scope):
        c, r = scope.connection, self.request
        saved = saved_transaction_events(c, self.key)
        transaction = None if saved is None else saved[0]["transaction"]
        item, output, owner, state = _facts(c, r.item_id, transaction)
        if r.error_code is None:
            present, _, _, _, _ = _query_presence(c, r.item_id, r.result_event_id)
            if present is None:
                raise ConsistencyError("有实际 QUERY 的结算必须引用可靠观察")
            if present and output["availability"] != 1:
                raise ConsistencyError("在场解析要求产物已更新为可用")
            if not present and output["availability"] != 4:
                raise ConsistencyError("缺席拒绝要求产物已更新为缺失")
            error_code = None if present else _OUTPUT_UNAVAILABLE
            details = (
                None
                if present
                else {"output_id": str(output["id"]), "availability": "missing"}
            )
        else:
            error_code = r.error_code
            details = dict(r.error_details)
        name = {
            _OUTPUT_UNAVAILABLE: "output_unavailable",
            _SOURCE_UNCONFIRMED: "source_file_unconfirmed",
            _DEVICE_BINDING: "device_binding_unavailable",
        }.get(error_code)
        if error_code is not None and name is None:
            raise ConsistencyError("QUERY 结算错误未登记")
        if name is not None:
            validate_error_details(name, details)
        if error_code == _OUTPUT_UNAVAILABLE and r.result_event_id is None:
            availability = {2: "restricted", 3: "cleaned", 4: "missing"}.get(
                output["availability"]
            )
            if availability is None or not json_equal(
                details, {"output_id": str(output["id"]), "availability": availability}
            ):
                raise ConsistencyError("产物不可用必须保留当前可靠状态")
        row_values = (
            {"output_id": output["id"], "status": 2}
            if error_code is None
            else {
                "output_id": output["id"],
                "status": 4,
                "error_code": error_code,
                "error_details_json": details,
            }
        )
        row = update_change(
            "obtain_items",
            item["id"],
            {key: item[key] for key in row_values},
            row_values,
        )
        evidence = (
            {}
            if r.result_event_id is None
            else {
                "obtain_check_source": {
                    "item_id": r.item_id,
                    "result_event_id": r.result_event_id,
                }
            }
        )
        reason = _RESOLVE_REASON if error_code is None else _REJECT_REASON
        allocation = None if saved is not None else scope.allocate(1)
        event = event_envelope(
            scope.max_event_id + 1 if allocation is None else allocation.first_event_id,
            scope.max_txn_id + 1 if allocation is None else allocation.txn_id,
            _READ_EVENT,
            reason,
            (row,),
            r.occurred_at,
            evidence,
        )
        owners = {("obtain_items", item["id"]): ("action", owner["id"])}
        if saved is not None:
            _event_matches(saved, event)
            return CommandPlan((), owners, state, True, True)
        return CommandPlan(
            (event,),
            owners,
            state,
            result=True,
            plan_read_scope=PlanReadScope(owner["plan_id"], 128),
        )


def observe_queried_output(request, key, owned):
    receipt = commit_operation(_ObserveCommand(request, key), key, owned)
    return DbOutcome(
        DbOutcomeKind(receipt.kind), value=receipt.result, error=receipt.error
    )


def settle_queried_item(request, key, owned):
    receipt = commit_operation(_SettleCommand(request, key), key, owned)
    return DbOutcome(
        DbOutcomeKind(receipt.kind), value=receipt.result, error=receipt.error
    )

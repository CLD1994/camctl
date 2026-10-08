"""操作意图与完整结果事务的唯一仓储。

经 P3 事务内核组织：意图、身份、次数及采用配置在同一事务内核实
后保存，普通尝试意图引用不可省略；结束结果与适用重试等待或流程
结束共同提交，任何检查失败整组回滚。同一操作身份的重送先核实原
事务并复用原结果。正式业务守卫（operation_identity、
query_configuration、attempt_intent、attempt_result、
operation_finish、retry）在本模块注册。
"""

from __future__ import annotations

from collections import ChainMap
from contextlib import closing
from dataclasses import asdict
from typing import Any, Mapping

from camctl.contracts.enums import decode_member, enum_for
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.json_values import is_json_integer, json_equal
from camctl.contracts.values import ConsistencyError, ObjectId, OperationKey
from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.history.validators import EventValidationError, register_guard
from camctl.operations.attempts import (
    AttemptFinish,
    ReadResumeDecision,
    ReadResumeDisposition,
    ReadResumeRequest,
    AttemptIntent,
    AttemptTarget,
    BeginAttemptResult,
    BeginDisposition,
    FinishAttemptResult,
    FinishDisposition,
    OperationKind,
    QueryPurpose,
    RunStatus,
    StaleRunFinish,
    StaleRunFinishResult,
    responsibility_key,
    seconds_from_json,
    ticket_target_id,
    operation_responsibility_key,
)
from camctl.operations.models import (
    AttemptStatus,
    AttemptTicket,
    SettlementBasis,
    ValidatedOutcome,
)
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.runtime import OwnedConnection
from camctl.persistence.row_history import read_row_values_at_boundary
from camctl.persistence.transaction import (
    CommandPlan,
    TransactionError,
    commit_operation,
    event_envelope as _envelope,
    next_row_id as _next_id,
    row_change as _row,
    row_facts,
    saved_transaction_events as _saved_transaction_events,
    SavedEvent,
    update_change as _update,
)

_RUN_KIND = enum_for("operation_runs.kind")
_RUN_STATUS = enum_for("operation_runs.status")
_QUERY_PURPOSE = enum_for("operation_runs.query_purpose")
_ATTEMPT_STATUS = enum_for("operation_attempts.status")
_EFFECT_STATE = enum_for("operation_attempts.effect_state")

#: 结束结果的合法收场依据。
_BASES = frozenset({basis.value for basis in SettlementBasis})

_ATTEMPT_STARTED_EVENT = 11
_ATTEMPT_RESULT_EVENT = 12
_RETRY_WAIT_EVENT = 6
_OPERATION_CONFIGURED_EVENT = 10

_RESULT_REASON = {
    AttemptStatus.SUCCEEDED: 1,
    AttemptStatus.FAILED: 2,
    AttemptStatus.UNKNOWN: 3,
}

#: ATTEMPT_RESULT.RESUME_READ：恢复同一未结束读取尝试的配置。
_RESUME_READ_REASON = 4

_RUN_UPDATE_COLUMNS = (
    "status",
    "attempts_used",
    "max_attempts_used",
    "timeout_s_json",
    "retry_interval_s_json",
    "retry_wait_required",
)

_PURPOSE_FLOW_KIND = {
    QueryPurpose.START_CONFIRMATION: _RUN_KIND.START,
    QueryPurpose.STOP_CONFIRMATION: _RUN_KIND.STOP,
    QueryPurpose.RESIDUAL_STOP_CONFIRMATION: _RUN_KIND.STOP_RESIDUAL,
}

_ACTIVITY_KINDS = (
    OperationKind.START,
    OperationKind.STOP,
    OperationKind.CHECK_CAPTURE_RESULTS,
)


def _fail(message: str) -> None:
    raise EventValidationError(message)


def _load_row(connection, table: str, row_id: int) -> dict | None:
    """读取行事实并把秒数与 JSON 列恢复为精确值。"""
    return row_facts(connection, table, row_id)


def _guard_facts(context, table: str, row_id: int) -> dict[str, Any]:
    facts = dict(context.state_rows.get(table, {}).get(row_id, {}))
    facts.setdefault("id", row_id)
    return facts


def _row_after_facts(context, row: RowChange) -> dict[str, Any]:
    facts = _guard_facts(context, row.table, row.row_id)
    facts.update(row.after.values)
    return facts


def _run_status_name(code: int) -> str:
    return decode_member("operation_runs.status", code).name


def _attempt_status_of(code: int) -> AttemptStatus:
    return AttemptStatus[decode_member("operation_attempts.status", code).name]


def _kind_name(code: int) -> str:
    return decode_member("operation_runs.kind", code).name


def _load_flow_context(
    connection,
    kind: OperationKind,
    action_id: int,
    target: AttemptTarget,
    query_purpose: QueryPurpose | None,
    state: dict,
) -> None:
    """加载目标引用行及守卫所需的关联事实；缺失即拒绝。"""
    if kind is OperationKind.READ_FILE:
        copy = _load_row(connection, "file_copies", target.copy_id or 0)
        if copy is None:
            raise TransactionError(f"文件拷贝不存在: {target.copy_id}")
        state.setdefault("file_copies", {})[copy["id"]] = copy
        if (copy["delivery_id"] is None) == (copy["processing_id"] is None):
            raise ConsistencyError("拷贝必须恰归属交付或录像处理之一")
        if copy["delivery_id"] is not None:
            delivery = _load_row(connection, "deliveries", copy["delivery_id"])
            if delivery is None:
                raise TransactionError(f"交付不存在: {copy['delivery_id']}")
            state.setdefault("deliveries", {})[delivery["id"]] = delivery
        else:
            processing = _load_row(
                connection, "recording_processing", copy["processing_id"]
            )
            if processing is None:
                raise TransactionError(f"录像处理不存在: {copy['processing_id']}")
            state.setdefault("recording_processing", {})[processing["id"]] = processing
        return
    needs_activity = (
        kind in _ACTIVITY_KINDS
        or kind is OperationKind.STOP_RESIDUAL
        or (
            kind is OperationKind.QUERY_ACTIVITY
            and query_purpose is not QueryPurpose.BEFORE_EXECUTION
        )
    )
    if needs_activity:
        assert target.activity_id is not None
        activity = _load_row(connection, "device_activities", target.activity_id)
        if activity is None:
            raise TransactionError(f"设备活动不存在: {target.activity_id}")
        state.setdefault("device_activities", {})[activity["id"]] = activity
    if kind is OperationKind.QUERY_ACTIVITY and query_purpose in _PURPOSE_FLOW_KIND:
        original_kind = _PURPOSE_FLOW_KIND[query_purpose]
        with closing(connection.execute(
            "SELECT id FROM operation_runs WHERE kind = ? AND action_id = ?"
            " AND activity_id = ? LIMIT 2",
            (int(original_kind), action_id, target.activity_id),
        )) as cursor:
            found = cursor.fetchall()
        if len(found) != 1:
            raise ConsistencyError("查询确认必须关联唯一的原流程")
        loaded = _load_row(connection, "operation_runs", int(found[0][0]))
        if loaded is None:
            raise ConsistencyError("查询确认引用的原流程记录不存在")
        state.setdefault("operation_runs", {})[loaded["id"]] = loaded
        return
    if kind in (OperationKind.DELETE_FILE, OperationKind.CHECK_FILE_EXISTS):
        cleanup = _load_row(connection, "cleanup_items", target.cleanup_item_id)
        if cleanup is None:
            raise TransactionError(f"清理项不存在: {target.cleanup_item_id}")
        state.setdefault("cleanup_items", {})[cleanup["id"]] = cleanup


def _fixed_reference(state, table: str, identity: int) -> Mapping[str, Any]:
    """目标必须真实存在且身份一致；不读取或解释可变状态。"""
    try:
        ObjectId(identity)
    except ValueError as error:
        raise ConsistencyError(f"{table} 的关联身份无效") from error
    facts = state.get(table, {}).get(identity)
    # 行图以物理 ID 为键；新建事件的业务列不重复保存派生 ID。
    if facts is None or not json_equal(facts.get("id", identity), identity):
        raise ConsistencyError(f"固定关联缺少 {table}#{identity} 的事实")
    return facts


def _copy_source(copy) -> tuple[str, int]:
    try:
        device, local = copy["source_device_file_id"], copy["source_intermediate_file_id"]
        if (device is None) == (local is None):
            raise ValueError("拷贝必须恰有一种来源")
        return ("device_files", ObjectId(device)) if device is not None else ("intermediate_files", ObjectId(local))
    except (KeyError, ValueError) as error:
        raise ConsistencyError("拷贝缺少有效的唯一源身份") from error


def _load_read_attempt_context(connection, copy, state) -> None:
    """只为新读取意图补齐当前源及原设备绑定，不影响原键恢复。"""
    def required(table, identity):
        try:
            ObjectId(identity)
        except ValueError as error:
            raise ConsistencyError(f"读取源的 {table} 身份无效") from error
        rows = state.setdefault(table, {})
        if identity not in rows:
            facts = _load_row(connection, table, identity)
            if facts is None:
                raise ConsistencyError(f"读取源的关联记录缺失: {table}#{identity}")
            rows[identity] = facts
        return rows[identity]

    table, identity = _copy_source(copy)
    source = required(table, identity)
    if table == "device_files":
        for column in ("observer_action_id", "source_action_id"):
            required("actions", source.get(column))


def _read_attempt_has_slot(state, copy_id: int, copy_round: int) -> bool:
    """当前源、机会和轮次合法时，判断设备拷贝是否已取得机会。"""
    copy = _fixed_reference(state, "file_copies", copy_id)
    table, identity = _copy_source(copy)
    source = _fixed_reference(state, table, identity)
    try:
        slot = copy["slot_device_id"]
        if table == "intermediate_files":
            if slot is not None:
                raise ConsistencyError("主机源拷贝不能持有相机读取机会")
        else:
            observer = _fixed_reference(state, "actions", source["observer_action_id"])
            origin = _fixed_reference(state, "actions", source["source_action_id"])
            binding = (observer["device_id"], observer["driver_id"])
            if (not all(isinstance(value, str) and value for value in binding)
                    or binding != (origin["device_id"], origin["driver_id"])):
                raise ConsistencyError("读取源观察者与可靠来源的原设备绑定不一致")
            if slot is not None and slot != binding[0]:
                raise ConsistencyError("拷贝的读取机会不属于原来源设备")
        current_round = copy["round"]
    except KeyError as error:
        raise ConsistencyError("当前读取源、绑定或拷贝缺少必要字段") from error
    if not is_json_integer(current_round) or current_round < 1:
        raise ConsistencyError("当前拷贝轮次无效")
    if not json_equal(copy_round, current_round):
        raise TransactionError("读取意图的轮次与当前拷贝不符")
    return table == "intermediate_files" or slot is not None


def _verify_run_identity(run: Mapping[str, Any], state) -> None:
    """核对普通流程的固定列及目标归属，供写事件和只读入口共用。"""
    try:
        if not is_json_integer(run["kind"]):
            raise ValueError("流程种类必须是整数编号")
        kind = OperationKind[_RUN_KIND(run["kind"]).name]
        purpose_code = run["query_purpose"]
        if purpose_code is not None and not is_json_integer(purpose_code):
            raise ValueError("查询用途必须是整数编号")
        purpose = QueryPurpose[_QUERY_PURPOSE(purpose_code).name] if purpose_code is not None else None
        target = AttemptTarget(activity_id=run["activity_id"], copy_id=run["copy_id"],
                               cleanup_item_id=run["cleanup_item_id"])
        expected = operation_responsibility_key(kind, run["action_id"], target, purpose)
        delivery_id, session_key = run["delivery_id"], run["session_key"]
    except (KeyError, ValueError) as error:
        raise ConsistencyError("普通流程的固定身份字段无效或缺失") from error
    if run.get("responsibility_key") != expected:
        raise ConsistencyError("流程责任键与固定身份不符")
    if session_key is not None or (kind is not OperationKind.READ_FILE and delivery_id is not None):
        raise ConsistencyError("普通流程的会话或不适用交付引用必须为空")
    action_id = run["action_id"]
    if target.activity_id is not None:
        activity = _fixed_reference(state, "device_activities", target.activity_id)
        try:
            ObjectId(activity["action_id"])
        except (KeyError, ValueError) as error:
            raise ConsistencyError("原活动缺少有效所属动作") from error
        residual = kind is OperationKind.STOP_RESIDUAL or purpose is QueryPurpose.RESIDUAL_STOP_CONFIRMATION
        if not residual and not json_equal(activity["action_id"], action_id):
            raise ConsistencyError("流程与目标活动所属动作不符")
        if purpose in _PURPOSE_FLOW_KIND:
            original_kind = int(_PURPOSE_FLOW_KIND[purpose])
            matches = [(identity, facts) for identity, facts in state.get("operation_runs", {}).items()
                       if facts.get("kind") == original_kind
                       and json_equal(facts.get("action_id"), action_id)
                       and json_equal(facts.get("activity_id"), target.activity_id)]
            if len(matches) != 1:
                raise ConsistencyError("查询确认必须关联唯一的原流程")
            original_id, original = matches[0]
            _fixed_reference(state, "operation_runs", original_id)
            _verify_run_identity(original, state)
    elif target.copy_id is not None:
        copy = _fixed_reference(state, "file_copies", target.copy_id)
        try:
            copy_delivery, copy_processing = copy["delivery_id"], copy["processing_id"]
        except KeyError as error:
            raise ConsistencyError("拷贝缺少完整的交付与处理关联字段") from error
        if (copy_delivery is None) == (copy_processing is None):
            raise ConsistencyError("拷贝必须恰归属交付或录像处理之一")
        if not json_equal(delivery_id, copy_delivery):
            raise ConsistencyError("读取流程与拷贝的交付引用不符")
        parent = (_fixed_reference(state, "deliveries", copy_delivery)
                  if copy_delivery is not None else
                  _fixed_reference(state, "recording_processing", copy_processing))
        if not json_equal(parent.get("action_id"), action_id):
            raise ConsistencyError("读取流程与拷贝所属动作不符")
        matches = [identity for identity, facts in state.get("operation_runs", {}).items()
                   if facts.get("kind") == run["kind"] and json_equal(facts.get("copy_id"), target.copy_id)]
        if len(matches) != 1 or not json_equal(matches[0], run.get("id")):
            raise ConsistencyError("拷贝必须关联其唯一原读取流程")
        _fixed_reference(state, "operation_runs", matches[0])
    elif target.cleanup_item_id is not None:
        cleanup = _fixed_reference(state, "cleanup_items", target.cleanup_item_id)
        if not json_equal(cleanup.get("action_id"), action_id):
            raise ConsistencyError("流程与目标清理项所属动作不符")


def _intent_identity(intent: AttemptIntent) -> dict[str, Any]:
    return {"action_id": intent.action_id, "kind": int(_RUN_KIND[intent.kind.name]),
            "query_purpose": int(_QUERY_PURPOSE[intent.query_purpose.name]) if intent.query_purpose is not None else None,
            "responsibility_key": responsibility_key(intent), "activity_id": intent.target.activity_id,
            "copy_id": intent.target.copy_id, "cleanup_item_id": intent.target.cleanup_item_id,
            "delivery_id": None, "session_key": None}


def _verify_run_intent(run: Mapping[str, Any], intent: AttemptIntent) -> None:
    """输入必须逐列指向原责任；交付归属由实际拷贝父对象核对。"""
    expected = _intent_identity(intent)
    for name in ("action_id", "kind", "query_purpose", "responsibility_key",
                 "activity_id", "copy_id", "cleanup_item_id", "session_key"):
        if name not in run or not json_equal(run[name], expected[name]):
            raise ConsistencyError(f"流程固定字段 {name} 与意图不符")


def _find_responsibility(connection, intent: AttemptIntent) -> int | None:
    """按规范键和实际责任共同定位；错误键不能刷新原责任。"""
    kind = intent.kind
    values = _intent_identity(intent)
    if kind in (OperationKind.START, OperationKind.STOP):
        columns = ("action_id",)
    elif kind is OperationKind.READ_FILE:
        columns = ("copy_id",)
    elif kind in (OperationKind.DELETE_FILE, OperationKind.CHECK_FILE_EXISTS):
        columns = ("cleanup_item_id",)
    elif kind is OperationKind.CHECK_CAPTURE_RESULTS:
        columns = ("activity_id",)
    elif kind is OperationKind.STOP_RESIDUAL:
        columns = ("action_id", "activity_id")
    else:
        columns = ("action_id", "query_purpose", "activity_id")
    predicate = " AND ".join(f"{column} IS ?" for column in columns)
    with closing(connection.execute(
        "SELECT id FROM operation_runs WHERE responsibility_key = ? OR (kind = ? AND "
        + predicate + ") LIMIT 2",
        (values["responsibility_key"], values["kind"], *(values[column] for column in columns)),
    )) as cursor:
        found = cursor.fetchall()
    if len(found) > 1:
        raise ConsistencyError("相同实际责任命中多条流程")
    return int(found[0][0]) if found else None


def _run_owner_ref(
    run_facts: Mapping[str, Any], state: Mapping[str, Mapping[int, Any]]
) -> tuple[str, int]:
    """按登记的归属规格解析流程行的历史对象。"""
    kind = run_facts["kind"]
    if kind == int(_RUN_KIND.READ_FILE):
        copy = state.get("file_copies", {}).get(run_facts["copy_id"])
        if copy is None:
            raise TransactionError("读取流程缺少拷贝事实")
        if copy["delivery_id"] is not None:
            return ("delivery", copy["delivery_id"])
        processing = state.get("recording_processing", {}).get(copy["processing_id"])
        if processing is None:
            raise TransactionError("读取流程缺少录像处理事实")
        return ("action", processing["action_id"])
    if kind in (int(_RUN_KIND.STOP_RESIDUAL), int(_RUN_KIND.EMERGENCY_STOP)):
        activity = state.get("device_activities", {}).get(run_facts["activity_id"])
        if activity is None:
            raise TransactionError("流程缺少设备活动事实")
        return ("action", activity["action_id"])
    return ("action", run_facts["action_id"])


def _event_rows(saved: Mapping[str, Any], table: str) -> list[dict]:
    return [
        row for row in saved["body"].get("rows", []) if row.get("table") == table
    ]


class BeginAttemptCommand:
    """一次普通尝试意图的完整事务命令。"""

    def __init__(self, intent: AttemptIntent, key: OperationKey) -> None:
        self._intent = intent
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        if not isinstance(self._intent, AttemptIntent):
            raise TransactionError("普通意图输入必须是 AttemptIntent")
        connection = scope.connection
        saved = _saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(connection, saved)

        intent = self._intent
        key_str = responsibility_key(intent)
        action = _load_row(connection, "actions", intent.action_id)
        if action is None:
            raise TransactionError(f"动作不存在: {intent.action_id}")
        self._state["actions"] = {intent.action_id: action}
        _load_flow_context(
            connection,
            intent.kind,
            intent.action_id,
            intent.target,
            intent.query_purpose,
            self._state,
        )

        found = _find_responsibility(connection, intent)
        run_facts = (
            _load_row(connection, "operation_runs", found) if found is not None else None
        )
        if found is not None and run_facts is None:
            raise ConsistencyError("责任键引用的流程记录不存在")
        read_has_slot = True
        if run_facts is not None:
            self._state.setdefault("operation_runs", {})[run_facts["id"]] = run_facts
            self._verify_existing_run(run_facts, intent, connection)
            if intent.kind is OperationKind.READ_FILE:
                copy = self._state["file_copies"][intent.target.copy_id]
                _load_read_attempt_context(connection, copy, self._state)
                read_has_slot = _read_attempt_has_slot(self._state, intent.target.copy_id, intent.copy_round)
            if run_facts["status"] not in (
                int(_RUN_STATUS.PENDING),
                int(_RUN_STATUS.ACTIVE),
            ):
                return self._rejected("run_ended")
            if run_facts["attempts_used"] >= intent.config.max_attempts:
                return self._rejected("budget_exhausted")
            if run_facts["attempts_used"] >= 1 and run_facts["retry_wait_required"] != 1:
                raise TransactionError("后续尝试要求先建立重试等待")
            attempt_no = int(run_facts["attempts_used"]) + 1
        else:
            if intent.kind is OperationKind.READ_FILE:
                raise ConsistencyError("已有拷贝缺少其唯一原读取流程")
            _verify_run_identity(_intent_identity(intent), self._state)
            attempt_no = 1

        if not read_has_slot:
            return self._rejected("read_slot_not_held")
        allocation = scope.allocate(1)
        event_id = allocation.first_event_id
        attempt_id = _next_id(connection, "operation_attempts")
        run_id = (
            run_facts["id"] if run_facts is not None else _next_id(connection, "operation_runs")
        )
        owner = self._resolve_owner(run_facts, intent, run_id)

        attempt_row = _row(
            "operation_attempts",
            attempt_id,
            {
                "run_id": run_id,
                "attempt_no": attempt_no,
                "copy_round": intent.copy_round,
                "status": int(_ATTEMPT_STATUS.RUNNING),
                "intent_event_id": event_id,
                "result_event_id": None,
                "max_attempts_used": intent.config.max_attempts,
                "timeout_s_json": intent.config.timeout_s,
                "retry_interval_s_json": intent.config.retry_interval_s,
                "effect_state": int(_EFFECT_STATE.UNKNOWN),
                "result_json": None,
                "error_json": None,
            },
        )
        if run_facts is None:
            run_row = _row(
                "operation_runs",
                run_id,
                {
                    "action_id": intent.action_id,
                    "delivery_id": None,
                    "kind": int(_RUN_KIND[intent.kind.name]),
                    "query_purpose": (
                        int(_QUERY_PURPOSE[intent.query_purpose.name])
                        if intent.query_purpose is not None
                        else None
                    ),
                    "responsibility_key": key_str,
                    "activity_id": intent.target.activity_id,
                    "copy_id": intent.target.copy_id,
                    "cleanup_item_id": intent.target.cleanup_item_id,
                    "session_key": None,
                    "status": int(_RUN_STATUS.ACTIVE),
                    "attempts_used": 1,
                    "max_attempts_used": intent.config.max_attempts,
                    "timeout_s_json": intent.config.timeout_s,
                    "retry_interval_s_json": intent.config.retry_interval_s,
                    "retry_wait_required": 0,
                    "error_json": None,
                },
            )
        else:
            run_row = _update(
                "operation_runs",
                run_id,
                {column: run_facts[column] for column in _RUN_UPDATE_COLUMNS},
                {
                    "status": int(_RUN_STATUS.ACTIVE),
                    "attempts_used": attempt_no,
                    "max_attempts_used": intent.config.max_attempts,
                    "timeout_s_json": intent.config.timeout_s,
                    "retry_interval_s_json": intent.config.retry_interval_s,
                    "retry_wait_required": 0,
                },
            )
        self._owners[("operation_runs", run_id)] = owner
        self._owners[("operation_attempts", attempt_id)] = owner
        event = _envelope(
            event_id,
            allocation.txn_id,
            _ATTEMPT_STARTED_EVENT,
            1,
            (attempt_row, run_row),
            intent.occurred_at,
        )
        ticket = AttemptTicket(
            attempt_id=attempt_no,
            operation=intent.operation,
            target_id=ticket_target_id(intent),
            responsibility_key=key_str,
            run_id=run_id,
        )
        return CommandPlan(
            events=(event,),
            owners=self._owners,
            state_rows=self._state,
            result=BeginAttemptResult(
                disposition=BeginDisposition.GRANTED, ticket=ticket
            ),
        )

    def _rejected(self, reason: str) -> CommandPlan:
        return CommandPlan(
            events=(),
            owners=self._owners,
            state_rows=self._state,
            read_only=True,
            result=BeginAttemptResult(disposition=BeginDisposition.REJECTED, reason=reason),
        )

    def _resolve_owner(self, run_facts, intent, run_id: int) -> tuple[str, int]:
        if run_facts is not None:
            return _run_owner_ref(run_facts, self._state)
        facts = {
            "kind": int(_RUN_KIND[intent.kind.name]),
            "action_id": intent.action_id,
            "activity_id": intent.target.activity_id,
            "copy_id": intent.target.copy_id,
        }
        return _run_owner_ref(facts, self._state)

    def _verify_existing_run(
        self, run_facts: Mapping[str, Any], intent, connection
    ) -> None:
        _verify_run_intent(run_facts, intent)
        _verify_run_identity(run_facts, self._state)
        with closing(connection.execute(
            "SELECT MAX(attempt_no) FROM operation_attempts WHERE run_id = ?",
            (run_facts["id"],),
        )) as cursor:
            max_no = cursor.fetchone()[0]
        if int(run_facts["attempts_used"]) != int(max_no or 0):
            raise TransactionError(
                f"累计次数与最大尝试编号不符: {run_facts['attempts_used']} != {max_no}"
            )

    def _reuse(self, connection, saved: list[dict]) -> CommandPlan:
        started = [event for event in saved if event["type"] == _ATTEMPT_STARTED_EVENT]
        if not started:
            raise TransactionError("操作身份已用于其他阶段，不能作为意图重送")
        attempt_values = None
        for row in _event_rows(started[0], "operation_attempts"):
            if row["after"]["exists"]:
                attempt_values = row["after"]["values"]
        if attempt_values is None:
            raise TransactionError("已保存意图缺少尝试事实")
        intent = self._intent
        run_id = int(attempt_values["run_id"])
        current = _load_row(connection, "operation_runs", run_id)
        if current is None:
            raise ConsistencyError("已保存意图引用的流程记录不存在")
        _verify_run_intent(current, intent)
        self._state["operation_runs"] = {run_id: current}
        _load_flow_context(connection, intent.kind, intent.action_id, intent.target,
                           intent.query_purpose, self._state)
        _verify_run_identity(current, self._state)
        ticket = AttemptTicket(
            attempt_id=int(attempt_values["attempt_no"]),
            operation=intent.operation,
            target_id=ticket_target_id(intent),
            responsibility_key=current["responsibility_key"],
            run_id=run_id,
        )
        return CommandPlan(
            events=(),
            owners=self._owners,
            state_rows=self._state,
            read_only=True,
            result=BeginAttemptResult(
                disposition=BeginDisposition.GRANTED, ticket=ticket
            ),
        )


def _result_json(validated: ValidatedOutcome) -> dict:
    """按统一外层结构编码结束结果；状态、错误及效果由行列保存。"""
    outcome = validated.outcome
    settlement = outcome.settlement
    assert settlement is not None
    document = {
        "format_version": outcome.format_version,
        "settlement": {
            "basis": settlement.basis.value,
            "evidence": {
                "type": settlement.evidence.type,
                "version": settlement.evidence.version,
                "data": dict(settlement.evidence.data),
            },
        },
        "observations": [
            {
                "type": observation.type,
                "version": observation.version,
                "data": dict(observation.data),
            }
            for observation in outcome.observations
        ],
    }
    if outcome.call_info is not None:
        call_info: dict[str, Any] = {}
        if outcome.call_info.local_exit_code is not None:
            call_info["local_exit"] = {"exit_code": outcome.call_info.local_exit_code}
        elif outcome.call_info.local_signal is not None:
            call_info["local_exit"] = {"signal": outcome.call_info.local_signal}
        if outcome.call_info.remote_exit_code is not None:
            call_info["remote_exit_code"] = outcome.call_info.remote_exit_code
        document["call_info"] = call_info
    return document


def _error_json(error) -> dict | None:
    if error is None:
        return None
    return {"code": error.code, "stage": error.stage, "details": dict(error.details)}


def _verify_result_ticket(finish: AttemptFinish, run: Mapping[str, Any], attempt: Mapping[str, Any]) -> None:
    """核对原只读责任与当前票据，适用于首次结果、迟到结果和重送。"""
    ticket = finish.ticket
    try:
        ObjectId(ticket.run_id)
        ObjectId(ticket.attempt_id)
    except ValueError as error:
        raise TransactionError("结果票据的流程与尝试编号无效") from error
    if (not json_equal(run["id"], ticket.run_id)
            or not json_equal(attempt["run_id"], ticket.run_id)
            or not json_equal(attempt["attempt_no"], ticket.attempt_id)
            or run["responsibility_key"] != ticket.responsibility_key):
        raise TransactionError("结果票据与原流程、尝试或责任不符")
    if run["kind"] == int(_RUN_KIND.READ_FILE):
        target = str(run["copy_id"])
    elif run["kind"] in (int(_RUN_KIND.DELETE_FILE), int(_RUN_KIND.CHECK_FILE_EXISTS)):
        target = str(run["cleanup_item_id"])
    elif (run["kind"] == int(_RUN_KIND.QUERY_ACTIVITY)
          and run["query_purpose"] == int(_QUERY_PURPOSE.BEFORE_EXECUTION)):
        target = None
    else:
        target = str(run["activity_id"])
    if ticket.target_id != target:
        raise TransactionError("结果票据的目标与原责任不符")
    if not json_equal(asdict(finish.outcome.ticket), asdict(ticket)):
        raise TransactionError("结果校验时的票据与本次结果票据不符")
    if (finish.outcome.settlement_contract.operation != ticket.operation
            or any(contract.operation != ticket.operation for contract in finish.outcome.observation_contracts)):
        raise TransactionError("结果票据的操作类别与已校验结果不符")
    if type(finish.retry_wait) is not bool:
        raise TransactionError("重试等待输入必须是布尔值")


class FinishAttemptCommand:
    """一次尝试结束结果的完整事务命令。"""

    def __init__(self, finish: AttemptFinish, key: OperationKey) -> None:
        self._finish = finish
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = _saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(scope, saved)

        finish = self._finish
        ticket = finish.ticket
        with closing(connection.execute(
            "SELECT id FROM operation_runs WHERE responsibility_key = ?",
            (ticket.responsibility_key,),
        )) as cursor:
            found = cursor.fetchone()
        if found is None or int(found[0]) != ticket.run_id:
            raise TransactionError(f"票据与流程不符: {ticket.responsibility_key}")
        run_facts = _load_row(connection, "operation_runs", ticket.run_id)
        assert run_facts is not None
        self._state["operation_runs"] = {ticket.run_id: run_facts}
        with closing(connection.execute(
            "SELECT id FROM operation_attempts WHERE run_id = ? AND attempt_no = ?",
            (ticket.run_id, ticket.attempt_id),
        )) as cursor:
            attempt = cursor.fetchone()
        if attempt is None:
            raise TransactionError(
                f"尝试不存在: run {ticket.run_id} #{ticket.attempt_id}"
            )
        attempt_id = int(attempt[0])
        attempt_facts = _load_row(connection, "operation_attempts", attempt_id)
        assert attempt_facts is not None
        _verify_result_ticket(finish, run_facts, attempt_facts)
        self._state["operation_attempts"] = {attempt_id: attempt_facts}
        _load_flow_context(
            connection,
            OperationKind[_kind_name(run_facts["kind"])],
            run_facts["action_id"],
            AttemptTarget(
                activity_id=run_facts.get("activity_id"),
                copy_id=run_facts.get("copy_id"),
                cleanup_item_id=run_facts.get("cleanup_item_id"),
            ),
            (
                QueryPurpose[
                    decode_member(
                        "operation_runs.query_purpose", run_facts["query_purpose"]
                    ).name
                ]
                if run_facts.get("query_purpose") is not None
                else None
            ),
            self._state,
        )

        _verify_run_identity(run_facts, self._state)
        owner = _run_owner_ref(run_facts, self._state)
        self._owners[("operation_runs", ticket.run_id)] = owner
        self._owners[("operation_attempts", attempt_id)] = owner

        if attempt_facts["status"] != int(_ATTEMPT_STATUS.RUNNING):
            # 迟到结果保留原终态，不覆盖已保存事实。
            return CommandPlan(
                events=(),
                owners=self._owners,
                state_rows=self._state,
                read_only=True,
                result=FinishAttemptResult(
                    disposition=FinishDisposition.ALREADY_ENDED,
                    attempt_status=_attempt_status_of(attempt_facts["status"]),
                    run_status=RunStatus[_run_status_name(run_facts["status"])],
                ),
            )

        outcome = finish.outcome.outcome
        # 流程终态与在途尝试分别结算：迟到结果不能重开间隔或改写终态。
        run_active = run_facts["status"] in (int(_RUN_STATUS.PENDING), int(_RUN_STATUS.ACTIVE))
        event_count = 1 + (
            1 if run_active and (finish.retry_wait or finish.run_finish is not None) else 0
        )
        allocation = scope.allocate(event_count)
        result_event = _envelope(
            allocation.first_event_id,
            allocation.txn_id,
            _ATTEMPT_RESULT_EVENT,
            _RESULT_REASON[outcome.status],
            (
                _update(
                    "operation_attempts",
                    attempt_id,
                    {
                        "status": attempt_facts["status"],
                        "result_event_id": attempt_facts["result_event_id"],
                        "effect_state": attempt_facts["effect_state"],
                        "result_json": attempt_facts["result_json"],
                        "error_json": attempt_facts["error_json"],
                    },
                    {
                        "status": int(_ATTEMPT_STATUS[outcome.status.name]),
                        "result_event_id": allocation.first_event_id,
                        "effect_state": int(_EFFECT_STATE[outcome.effect.name]),
                        "result_json": _result_json(finish.outcome),
                        "error_json": _error_json(outcome.error),
                    },
                ),
            ),
            finish.occurred_at,
        )
        events = [result_event]
        run_status_after = RunStatus[_run_status_name(run_facts["status"])]
        if run_active and finish.retry_wait:
            events.append(
                _envelope(
                    allocation.last_event_id,
                    allocation.txn_id,
                    _RETRY_WAIT_EVENT,
                    1,
                    (
                        _update(
                            "operation_runs",
                            ticket.run_id,
                            {"retry_wait_required": run_facts["retry_wait_required"]},
                            {"retry_wait_required": 1},
                        ),
                    ),
                    finish.occurred_at,
                )
            )
        elif run_active and finish.run_finish is not None:
            final = finish.run_finish.status
            events.append(
                _envelope(
                    allocation.last_event_id,
                    allocation.txn_id,
                    _OPERATION_CONFIGURED_EVENT,
                    3,
                    (
                        _update(
                            "operation_runs",
                            ticket.run_id,
                            {
                                "status": run_facts["status"],
                                "retry_wait_required": run_facts["retry_wait_required"],
                                "error_json": run_facts["error_json"],
                            },
                            {
                                "status": int(_RUN_STATUS[final.name]),
                                "retry_wait_required": 0,
                                "error_json": _error_json(finish.run_finish.error),
                            },
                        ),
                    ),
                    finish.occurred_at,
                )
            )
            run_status_after = RunStatus[final.name]
        return CommandPlan(
            events=tuple(events),
            owners=self._owners,
            state_rows=self._state,
            result=FinishAttemptResult(
                disposition=FinishDisposition.SAVED,
                attempt_status=outcome.status,
                run_status=run_status_after,
            ),
        )

    def _reuse(self, scope, saved: list[SavedEvent]) -> CommandPlan:
        if (len(saved) not in (1, 2) or saved[0]["type"] != _ATTEMPT_RESULT_EVENT
                or saved[0]["reason"] not in _RESULT_REASON.values()):
            raise TransactionError("操作身份已用于其他阶段，不能作为结果重送")
        rows = saved[0]["body"]["rows"]
        if (len(rows) != 1 or rows[0]["table"] != "operation_attempts"
                or not rows[0]["before"]["exists"] or not rows[0]["after"]["exists"]):
            raise TransactionError("已保存结果缺少尝试事实")
        finish = self._finish
        connection = scope.connection
        attempt = _load_row(connection, "operation_attempts", rows[0]["id"])
        if attempt is None:
            raise ConsistencyError("原结果引用的尝试记录不存在")
        run = _load_row(connection, "operation_runs", attempt["run_id"])
        if run is None:
            raise ConsistencyError("原尝试所属的流程记录不存在")
        _verify_result_ticket(finish, run, attempt)
        if attempt["result_event_id"] != saved[0]["event_id"]:
            raise ConsistencyError("已结束尝试的结果引用与原事务不符")
        for name, value in rows[0]["after"]["values"].items():
            if name not in attempt or not json_equal(attempt[name], value):
                raise ConsistencyError("已结束尝试的事实与原结果事件不符")
        outcome = finish.outcome.outcome
        requested = {"status": int(_ATTEMPT_STATUS[outcome.status.name]),
                     "effect_state": int(_EFFECT_STATE[outcome.effect.name]),
                     "result_json": _result_json(finish.outcome), "error_json": _error_json(outcome.error)}
        if (any(not json_equal(attempt[name], value) for name, value in requested.items())
                or any(event["occurred_at"] != finish.occurred_at for event in saved)):
            raise TransactionError("重送的结果或事实时刻与原事务不同")
        waiting = len(saved) == 2 and (saved[1]["type"], saved[1]["reason"]) == (_RETRY_WAIT_EVENT, 1)
        ending = len(saved) == 2 and (saved[1]["type"], saved[1]["reason"]) == (_OPERATION_CONFIGURED_EVENT, 3)
        if len(saved) == 2 and not waiting and not ending:
            raise TransactionError("重送的流程处置与原结果事务不同")
        run_columns = frozenset({"status", "retry_wait_required", "error_json"})
        if len(saved) == 2:
            run_rows = saved[1]["body"]["rows"]
            if (len(run_rows) != 1 or run_rows[0]["table"] != "operation_runs"
                    or run_rows[0]["id"] != run["id"] or not run_rows[0]["before"]["exists"]
                    or not run_rows[0]["after"]["exists"]
                    or not run_rows[0]["after"]["values"].keys() <= run_columns):
                raise TransactionError("原流程处置的阶段、行身份或字段不符")
        self._state["operation_runs"] = {run["id"]: run}
        _load_flow_context(connection, OperationKind[_kind_name(run["kind"])], run["action_id"],
            AttemptTarget(activity_id=run["activity_id"], copy_id=run["copy_id"], cleanup_item_id=run["cleanup_item_id"]),
            QueryPurpose[decode_member("operation_runs.query_purpose", run["query_purpose"]).name]
            if run["query_purpose"] is not None else None, self._state)
        _verify_run_identity(run, self._state)
        transaction = saved[0]["transaction"]
        original = read_row_values_at_boundary(connection, owner=_run_owner_ref(run, self._state),
            table="operation_runs", row_id=run["id"], columns=run_columns,
            current_values={name: run[name] for name in run_columns},
            boundary=HistoryBoundary(transaction.txn_id, transaction.last_event_id),
            current_boundary=HistoryBoundary(scope.max_txn_id, scope.max_event_id))
        if len(saved) == 2:
            for name, value in run_rows[0]["after"]["values"].items():
                if not json_equal(original[name], value):
                    raise ConsistencyError("原边界的流程事实与处置事件不符")
        # 单结果事务的原边界已经终态时，调用者请求的流程处置未生效。
        # 从历史边界判定，不能用重送时已经变化的当前流程状态代替。
        terminal_result_only = len(saved) == 1 and original["status"] not in (
            int(_RUN_STATUS.PENDING), int(_RUN_STATUS.ACTIVE))
        if not terminal_result_only and (
                finish.retry_wait != waiting or (finish.run_finish is not None) != ending):
            raise TransactionError("重送的流程处置与原结果事务不同")
        if ending and finish.run_finish is not None:
            if (original["status"] != int(_RUN_STATUS[finish.run_finish.status.name])
                    or not json_equal(original["error_json"], _error_json(finish.run_finish.error))):
                raise TransactionError("重送的流程结束状态或错误与原事务不同")
        return CommandPlan(
            events=(),
            owners=self._owners,
            state_rows=self._state,
            read_only=True,
            result=FinishAttemptResult(
                disposition=FinishDisposition.SAVED,
                attempt_status=_attempt_status_of(attempt["status"]),
                run_status=RunStatus[_run_status_name(original["status"])],
            ),
        )


class _FinishStaleRunsCommand:
    """伴随流程统一收场的完整事务命令。

    拥有方（如清理成员）终态后，责任键下仍待执行或执行中的流程行
    不再有后续尝试，按请求的最终结果逐行保存终态并清除重试等待；
    无匹配行时幂等完成，不产生事件。每个流程行一个结束事件，装
    配与结束守卫约束与尝试结束的流程收场分支一致。
    """

    def __init__(self, request: StaleRunFinish, key: OperationKey) -> None:
        if not isinstance(request, StaleRunFinish):
            raise TypeError("伴随收场申请必须使用 StaleRunFinish")
        self._request = request
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        request = self._request
        saved = _saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(scope, saved)
        marks = ",".join("?" for _ in request.responsibility_keys)
        with closing(connection.execute(
            f"SELECT id FROM operation_runs"
            f" WHERE responsibility_key IN ({marks}) AND status IN (?, ?)",
            (*request.responsibility_keys,
             int(_RUN_STATUS.PENDING), int(_RUN_STATUS.ACTIVE)),
        )) as cursor:
            run_ids = sorted(int(row[0]) for row in cursor.fetchall())
        if not run_ids:
            return CommandPlan(
                events=(), owners=self._owners, state_rows=self._state,
                read_only=True,
                result=StaleRunFinishResult(finished_run_ids=()),
            )
        rows = []
        for run_id in run_ids:
            run_facts = _load_row(connection, "operation_runs", run_id)
            assert run_facts is not None
            self._state.setdefault("operation_runs", {})[run_id] = run_facts
            _load_flow_context(
                connection,
                OperationKind[_kind_name(run_facts["kind"])],
                run_facts["action_id"],
                AttemptTarget(
                    activity_id=run_facts.get("activity_id"),
                    copy_id=run_facts.get("copy_id"),
                    cleanup_item_id=run_facts.get("cleanup_item_id"),
                ),
                (
                    QueryPurpose[
                        decode_member(
                            "operation_runs.query_purpose",
                            run_facts["query_purpose"],
                        ).name
                    ]
                    if run_facts.get("query_purpose") is not None
                    else None
                ),
                self._state,
            )
            _verify_run_identity(run_facts, self._state)
            self._owners[("operation_runs", run_id)] = _run_owner_ref(
                run_facts, self._state)
            rows.append(_update(
                "operation_runs", run_id,
                {
                    "status": run_facts["status"],
                    "retry_wait_required": run_facts["retry_wait_required"],
                    "error_json": run_facts["error_json"],
                },
                {
                    "status": int(_RUN_STATUS[request.status.name]),
                    "retry_wait_required": 0,
                    "error_json": _error_json(request.error),
                },
            ))
        allocation = scope.allocate(len(rows))
        events = tuple(
            _envelope(
                allocation.first_event_id + offset,
                allocation.txn_id,
                _OPERATION_CONFIGURED_EVENT,
                3,
                (row,),
                request.occurred_at,
            )
            for offset, row in enumerate(rows)
        )
        return CommandPlan(
            events=events,
            owners=self._owners,
            state_rows=self._state,
            result=StaleRunFinishResult(finished_run_ids=tuple(run_ids)),
        )

    def _reuse(self, scope, saved: list[SavedEvent]) -> CommandPlan:
        request = self._request
        connection = scope.connection
        run_ids = []
        for event in saved:
            if (event["type"] != _OPERATION_CONFIGURED_EVENT
                    or event["reason"] != 3):
                raise TransactionError(
                    "操作身份已用于其他阶段，不能作为伴随收场重送")
            if event["occurred_at"] != request.occurred_at:
                raise TransactionError("重送的伴随收场时刻与原事务不同")
            rows = event["body"]["rows"]
            if (len(rows) != 1 or rows[0]["table"] != "operation_runs"
                    or not rows[0]["before"]["exists"]
                    or not rows[0]["after"]["exists"]):
                raise TransactionError("伴随收场事件必须是已有流程行的更新")
            expected = {
                "status": int(_RUN_STATUS[request.status.name]),
                "retry_wait_required": 0,
                "error_json": _error_json(request.error),
            }
            after = rows[0]["after"]["values"]
            if set(after) - set(expected):
                raise TransactionError("伴随收场只更新流程终态字段")
            run_id = int(rows[0]["id"])
            run_ids.append(run_id)
            run = _load_row(connection, "operation_runs", run_id)
            if run is None:
                raise ConsistencyError("伴随收场的流程记录不存在")
            self._state.setdefault("operation_runs", {})[run_id] = run
            for name, value in expected.items():
                if name in after and not json_equal(run.get(name), value):
                    raise ConsistencyError("已收场流程的事实与原事件不符")
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=StaleRunFinishResult(finished_run_ids=tuple(run_ids)),
        )


class _ReadResumeCommand:
    """恢复同一未结束读取尝试配置的事务命令。

    重启后沿原在途尝试继续读取时，把本次运行采用的预算与期限写
    入原尝试行；不新增尝试或次数，不改动结果与效果状态。配置与
    原保存值相同时不产生事件。
    """

    _CONFIG_COLUMNS = ("max_attempts_used", "timeout_s_json", "retry_interval_s_json")

    def __init__(self, request: ReadResumeRequest, key: OperationKey) -> None:
        self._request = request
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        saved = _saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(saved)
        ticket = self._request.ticket
        run = self._load_run(connection, ticket)
        attempt_id = self._load_attempt(connection, run, ticket)
        attempt = row_facts(connection, "operation_attempts", attempt_id)
        assert attempt is not None
        self._state["operation_attempts"] = {attempt_id: attempt}
        _verify_result_ticket_config(self._request, run, attempt)
        owner = _run_owner_ref(run, self._state)
        self._owners[("operation_runs", run["id"])] = owner
        self._owners[("operation_attempts", attempt_id)] = owner
        if attempt["status"] != int(_ATTEMPT_STATUS.RUNNING):
            return self._decision(ReadResumeDisposition.NOT_RUNNING)
        desired = {
            "max_attempts_used": self._request.config.max_attempts,
            "timeout_s_json": self._request.config.timeout_s,
            "retry_interval_s_json": self._request.config.retry_interval_s,
        }
        changed = {
            column: value for column, value in desired.items()
            if not json_equal(attempt[column], value)
        }
        if not changed:
            return self._decision(ReadResumeDisposition.UNCHANGED)
        row = _update(
            "operation_attempts", attempt_id,
            {column: attempt[column] for column in changed},
            changed,
        )
        allocation = scope.allocate(1)
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _ATTEMPT_RESULT_EVENT, _RESUME_READ_REASON, (row,),
            self._request.occurred_at, evidence={"attempt_id": attempt_id},
        )
        return CommandPlan(
            events=(event,),
            owners=self._owners,
            state_rows=self._state,
            result=ReadResumeDecision(ReadResumeDisposition.APPLIED),
        )

    def _load_run(self, connection, ticket) -> dict:
        run = _load_row(connection, "operation_runs", ticket.run_id)
        if run is None:
            raise TransactionError(f"恢复配置的流程记录不存在: {ticket.run_id}")
        if run["responsibility_key"] != ticket.responsibility_key:
            raise TransactionError("恢复配置的票据与原流程责任不符")
        self._state["operation_runs"] = {run["id"]: run}
        _load_flow_context(
            connection, OperationKind[_kind_name(run["kind"])], run["action_id"],
            AttemptTarget(
                activity_id=run["activity_id"], copy_id=run["copy_id"],
                cleanup_item_id=run["cleanup_item_id"],
            ),
            QueryPurpose[decode_member(
                "operation_runs.query_purpose", run["query_purpose"]).name]
            if run["query_purpose"] is not None else None,
            self._state,
        )
        _verify_run_identity(run, self._state)
        return run

    def _load_attempt(self, connection, run, ticket) -> int:
        with closing(connection.execute(
            "SELECT id FROM operation_attempts WHERE run_id = ? AND attempt_no = ?",
            (run["id"], ticket.attempt_id),
        )) as cursor:
            found = cursor.fetchone()
        if found is None:
            raise TransactionError(
                f"恢复配置的尝试不存在: run {run['id']} #{ticket.attempt_id}"
            )
        return int(found[0])

    def _decision(self, disposition: ReadResumeDisposition) -> CommandPlan:
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True, result=ReadResumeDecision(disposition),
        )

    def _reuse(self, saved: list[dict]) -> CommandPlan:
        """原键恢复首次恢复配置响应；不按当前状态重新判定。"""
        if len(saved) != 1 or saved[0]["type"] != _ATTEMPT_RESULT_EVENT \
                or saved[0]["reason"] != _RESUME_READ_REASON:
            raise TransactionError("操作身份已用于其他阶段，不能作为恢复配置重送")
        event = saved[0]
        if event["occurred_at"] != self._request.occurred_at:
            raise TransactionError("恢复配置的事实时刻与原事务不同")
        rows = event["body"]["rows"]
        if (len(rows) != 1 or rows[0]["table"] != "operation_attempts"
                or rows[0]["after"]["values"].keys()
                - set(self._CONFIG_COLUMNS)):
            raise TransactionError("原恢复配置的目标尝试或字段与输入不符")
        return CommandPlan(
            events=(), owners={}, state_rows={}, read_only=True,
            result=ReadResumeDecision(ReadResumeDisposition.APPLIED),
        )


class OperationRepository:
    """操作意图与结果事务的 SQLite 仓储。"""

    def begin_attempt(
        self, intent: AttemptIntent, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[BeginAttemptResult]:
        receipt = commit_operation(BeginAttemptCommand(intent, key), key, owned)
        return _outcome_of(receipt)

    def finish_attempt(
        self, finish: AttemptFinish, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[FinishAttemptResult]:
        receipt = commit_operation(FinishAttemptCommand(finish, key), key, owned)
        return _outcome_of(receipt)

    def finish_stale_runs(
        self, request: StaleRunFinish, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[StaleRunFinishResult]:
        receipt = commit_operation(_FinishStaleRunsCommand(request, key), key, owned)
        return _outcome_of(receipt)

    def resume_read(
        self, request: ReadResumeRequest, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[ReadResumeDecision]:
        receipt = commit_operation(_ReadResumeCommand(request, key), key, owned)
        return _outcome_of(receipt)


def _outcome_of(receipt) -> DbOutcome:
    if receipt.kind == "completed":
        return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
    if receipt.kind == "rolled_back":
        return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
    return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)


# -- 正式业务守卫 -----------------------------------------------------


def _operation_identity_guard(event, context) -> None:
    relations = context.association_rows
    if context.transaction_rows is None:
        # 独立校验也必须包含当前创建行；不预取未来事件或改写事件前状态。
        current_rows: dict[str, dict[int, dict[str, Any]]] = {}
        for row in event.rows:
            if row.after.exists:
                facts = _row_after_facts(context, row)
                current_rows.setdefault(row.table, {})[row.row_id] = facts
        relations = ChainMap({
            table: ChainMap(rows, relations.get(table, {}))
            for table, rows in current_rows.items()
        }, relations)
    for row in event.rows:
        if row.table == "operation_runs":
            facts = _row_after_facts(context, row)
        elif row.table == "file_copies" and not row.before.exists:
            matches = [(identity, facts) for identity, facts in relations.get("operation_runs", {}).items()
                       if facts.get("kind") == int(_RUN_KIND.READ_FILE)
                       and json_equal(facts.get("copy_id"), row.row_id)]
            if len(matches) != 1:
                _fail("新拷贝必须关联唯一读取流程")
            identity, saved_facts = matches[0]
            facts = dict(saved_facts)
            facts.setdefault("id", identity)
        else:
            continue
        try:
            _verify_run_identity(facts, relations)
        except ConsistencyError as error:
            raise EventValidationError(str(error)) from error


def _query_configuration_guard(event, context) -> None:
    for row in event.rows:
        if row.table == "operation_runs":
            facts = _row_after_facts(context, row)
            kind = facts.get("kind")
            if kind in (
                int(_RUN_KIND.QUERY_ACTIVITY),
                int(_RUN_KIND.CHECK_CAPTURE_RESULTS),
            ):
                if (
                    facts.get("timeout_s_json") is None
                    or facts.get("retry_interval_s_json") is None
                ):
                    _fail("查询与结果核实流程必须保存完整的超时与间隔配置")
            _check_config_numbers(facts, f"operation_runs#{row.row_id}")
        elif row.table == "operation_attempts":
            _check_config_numbers(
                row.after.values, f"operation_attempts#{row.row_id}"
            )


def _check_config_numbers(values: Mapping[str, Any], where: str) -> None:
    maximum = values.get("max_attempts_used")
    if maximum is not None and (
        isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1
    ):
        _fail(f"{where} 的次数上限必须是正整数: {maximum!r}")
    timeout = values.get("timeout_s_json")
    if timeout is not None:
        number = seconds_from_json(timeout)
        if number is None or number <= 0:
            _fail(f"{where} 的 timeout_s 必须为正秒数: {timeout!r}")
    interval = values.get("retry_interval_s_json")
    if interval is not None:
        number = seconds_from_json(interval)
        if number is None or number < 0:
            _fail(f"{where} 的 retry_interval_s 必须为非负秒数: {interval!r}")


def _attempt_intent_guard(event, context) -> None:
    attempts = [
        row
        for row in event.rows
        if row.table == "operation_attempts" and not row.before.exists
    ]
    if not attempts:
        return
    run_rows = [row for row in event.rows if row.table == "operation_runs"]
    if not run_rows:
        _fail("尝试意图必须与流程事实同事务保存")
    for attempt in attempts:
        after = attempt.after.values
        if after.get("intent_event_id") != event.event_id:
            _fail("普通尝试的意图引用不可省略，且指向本次事件")
        if (
            after.get("result_event_id") is not None
            or after.get("result_json") is not None
            or after.get("error_json") is not None
        ):
            _fail("意图阶段的尝试不得携带结束结果")
        if (
            after.get("status") != int(_ATTEMPT_STATUS.RUNNING)
            or after.get("effect_state") != int(_EFFECT_STATE.UNKNOWN)
        ):
            _fail("意图阶段的尝试必须为运行中且效果未知")
    run_row = run_rows[0]
    run_after = _row_after_facts(context, run_row)
    attempt_no = attempts[0].after.values["attempt_no"]
    if run_after.get("attempts_used") != attempt_no:
        _fail(
            f"累计次数 {run_after.get('attempts_used')!r} 与新尝试编号"
            f" {attempt_no!r} 不一致"
        )
    if run_row.before.exists:
        before_used = run_row.before.values.get("attempts_used")
        if before_used is None or int(before_used) != int(attempt_no) - 1:
            _fail("新尝试编号必须紧接原累计次数")
    elif attempt_no != 1:
        _fail("首次建立的流程必须从第 1 次尝试开始")
    kind = run_after.get("kind")
    copy_round = attempts[0].after.values.get("copy_round")
    if kind == int(_RUN_KIND.READ_FILE):
        if (
            not isinstance(copy_round, int)
            or isinstance(copy_round, bool)
            or copy_round < 1
        ):
            _fail("读取尝试必须保存正的拷贝轮次")
        try:
            held = _read_attempt_has_slot(context.state_rows, run_after.get("copy_id"), copy_round)
        except (ConsistencyError, TransactionError) as error:
            raise EventValidationError(str(error)) from error
        if not held:
            _fail("设备读取意图要求当前拷贝已取得原来源设备的读取机会")
    elif copy_round is not None:
        _fail("只有读取尝试保存拷贝轮次")


def _attempt_result_guard(event, context) -> None:
    for row in event.rows:
        if row.table != "operation_attempts" or not row.before.exists:
            continue
        after = _row_after_facts(context, row)
        if after.get("result_event_id") != event.event_id:
            _fail("结果引用必须指向本次事件")
        status = after.get("status")
        error = after.get("error_json")
        result = after.get("result_json")
        if status == int(_ATTEMPT_STATUS.SUCCEEDED) and error is not None:
            _fail("成功尝试不得携带调用错误")
        if status in (int(_ATTEMPT_STATUS.FAILED), int(_ATTEMPT_STATUS.UNKNOWN)):
            if error is None:
                _fail("失败或未知尝试必须携带调用错误")
        if not isinstance(result, Mapping):
            _fail("结束尝试必须保存统一结构的结果正文")
        assert result is not None
        if result.get("format_version") != 1:
            _fail("结果正文版本不支持")
        settlement = result.get("settlement")
        if not isinstance(settlement, Mapping) or settlement.get("basis") not in _BASES:
            _fail("结果正文缺少合法的收场依据")
        evidence = settlement.get("evidence")
        if (
            not isinstance(evidence, Mapping)
            or not isinstance(evidence.get("type"), str)
            or not evidence.get("type")
            or isinstance(evidence.get("version"), bool)
            or not isinstance(evidence.get("version"), int)
            or evidence.get("version") < 1
        ):
            _fail("收场依据必须是类型化对象")
        observations = result.get("observations")
        if not isinstance(observations, list):
            _fail("结果正文必须保存观察数组")
        effect = after.get("effect_state")
        if settlement["basis"] == "not_dispatched" and effect != int(
            _EFFECT_STATE.NO_EFFECT
        ):
            _fail("未派发依据只支持无效果")
        if effect == int(_EFFECT_STATE.CONFIRMED) and not observations:
            _fail("已确认效果必须有承载观察")
        call_info = result.get("call_info")
        if call_info is not None and not isinstance(call_info, Mapping):
            _fail("调用信息必须是对象")


def _verify_result_ticket_config(request: ReadResumeRequest, run, attempt) -> None:
    """核对恢复配置申请与原流程、尝试的固定身份。"""
    ticket = request.ticket
    if (not json_equal(run["id"], ticket.run_id)
            or not json_equal(attempt["run_id"], ticket.run_id)
            or not json_equal(attempt["attempt_no"], ticket.attempt_id)
            or run["responsibility_key"] != ticket.responsibility_key):
        raise TransactionError("恢复配置票据与原流程、尝试或责任不符")
    if run["kind"] != int(_RUN_KIND.READ_FILE):
        raise TransactionError("恢复配置只适用于读取流程")
    if ticket.target_id != str(run["copy_id"]):
        raise TransactionError("恢复配置票据的目标与原责任不符")


def _read_resume_guard(event, context) -> None:
    """恢复配置事件守卫：只属于读取流程的在途尝试。"""
    if event.event_type != _ATTEMPT_RESULT_EVENT or event.reason != _RESUME_READ_REASON:
        return
    for row in event.rows:
        if row.table != "operation_attempts":
            continue
        attempt = context.state_rows.get("operation_attempts", {}).get(row.row_id)
        if attempt is None:
            _fail("恢复配置缺少当前尝试事实")
        run = context.state_rows.get("operation_runs", {}).get(attempt["run_id"])
        if run is None or run.get("kind") != int(_RUN_KIND.READ_FILE):
            _fail("恢复配置只属于读取流程的尝试")


def _operation_finish_guard(event, context) -> None:
    for row in event.rows:
        if row.table != "operation_runs" or not row.before.exists:
            continue
        if "status" not in row.after.values:
            continue
        after = _row_after_facts(context, row)
        status = after.get("status")
        error = after.get("error_json")
        if status in (int(_RUN_STATUS.FAILED), int(_RUN_STATUS.UNCONFIRMED)):
            if error is None:
                _fail("失败或未确认结束必须携带流程错误")
        elif error is not None:
            _fail("该结束结果不携带流程错误")
        if after.get("retry_wait_required") != 0:
            _fail("结束流程必须清除重试等待责任")


def _retry_guard(event, context) -> None:
    for row in event.rows:
        if row.table != "operation_runs":
            continue
        if row.after.values.get("retry_wait_required") != 1:
            continue
        facts = _row_after_facts(context, row)
        if facts.get("retry_interval_s_json") is None:
            _fail("建立重试等待要求已保存的重试间隔")
        if facts.get("status") not in (
            int(_RUN_STATUS.PENDING),
            int(_RUN_STATUS.ACTIVE),
        ):
            _fail("只有未结束流程保存重试等待责任")


def register_operation_guards() -> None:
    """注册操作事务的正式业务守卫（装配期调用，替换测试替身）。"""
    register_guard("operation_identity", _operation_identity_guard)
    register_guard("query_configuration", _query_configuration_guard)
    register_guard("attempt_intent", _attempt_intent_guard)
    register_guard("attempt_result", _attempt_result_guard)
    register_guard("read_resume", _read_resume_guard)
    register_guard("operation_finish", _operation_finish_guard)
    register_guard("retry", _retry_guard)

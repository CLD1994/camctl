"""操作意图与完整结果事务的唯一仓储。

经 P3 事务内核组织：意图、身份、次数及采用配置在同一事务内核实
后保存，普通尝试意图引用不可省略；结束结果与适用重试等待或流程
结束共同提交，任何检查失败整组回滚。同一操作身份的重送先核实原
事务并复用原结果。正式业务守卫（operation_identity、
query_configuration、attempt_intent、attempt_result、
operation_finish、retry）在本模块注册。
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from camctl.contracts.enums import decode_member, enum_for
from camctl.contracts.values import OperationKey
from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.history.validators import EventValidationError, register_guard
from camctl.operations.attempts import (
    AttemptFinish,
    AttemptTarget,
    BeginAttemptResult,
    BeginDisposition,
    FinishAttemptResult,
    FinishDisposition,
    OperationKind,
    QueryPurpose,
    RunStatus,
    responsibility_key,
    seconds_from_json,
    ticket_target_id,
)
from camctl.operations.models import (
    AttemptStatus,
    AttemptTicket,
    SettlementBasis,
    ValidatedOutcome,
)
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.runtime import OwnedConnection
from camctl.persistence.transaction import (
    CommandPlan,
    TransactionError,
    commit_operation,
    event_envelope as _envelope,
    next_row_id as _next_id,
    row_change as _row,
    row_facts,
    saved_transaction_events as _saved_transaction_events,
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
    values = row_facts(connection, table, row_id)
    if values is None:
        return None
    for column in ("timeout_s_json", "retry_interval_s_json"):
        if column in values:
            values[column] = seconds_from_json(values[column])
    for column in ("error_json", "result_json"):
        if column in values and isinstance(values[column], str):
            values[column] = json.loads(values[column])
    return values


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
        found = connection.execute(
            "SELECT id FROM operation_runs WHERE kind = ? AND action_id = ?"
            " AND activity_id = ?",
            (int(original_kind), action_id, target.activity_id),
        ).fetchall()
        for (run_id,) in found:
            loaded = _load_row(connection, "operation_runs", int(run_id))
            if loaded is not None:
                state.setdefault("operation_runs", {})[loaded["id"]] = loaded
        return
    if kind in (OperationKind.DELETE_FILE, OperationKind.CHECK_FILE_EXISTS):
        exists = connection.execute(
            "SELECT 1 FROM cleanup_items WHERE id = ?", (target.cleanup_item_id,)
        ).fetchone()
        if exists is None:
            raise TransactionError(f"清理项不存在: {target.cleanup_item_id}")


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

    def __init__(self, intent, key: OperationKey) -> None:
        self._intent = intent
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
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

        found = connection.execute(
            "SELECT id FROM operation_runs WHERE responsibility_key = ?", (key_str,)
        ).fetchone()
        run_facts = (
            _load_row(connection, "operation_runs", int(found[0])) if found else None
        )
        if run_facts is not None:
            self._state.setdefault("operation_runs", {})[run_facts["id"]] = run_facts
            self._verify_existing_run(run_facts, intent, connection)
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
            attempt_no = 1

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
        if run_facts["responsibility_key"] != responsibility_key(intent):
            raise TransactionError("责任键与意图不符")
        if run_facts["action_id"] != intent.action_id:
            raise TransactionError("流程动作与意图不符")
        if run_facts["kind"] != int(_RUN_KIND[intent.kind.name]):
            raise TransactionError("流程种类与意图不符")
        expected_purpose = (
            int(_QUERY_PURPOSE[intent.query_purpose.name])
            if intent.query_purpose is not None
            else None
        )
        if run_facts["query_purpose"] != expected_purpose:
            raise TransactionError("查询用途与意图不符")
        max_no = connection.execute(
            "SELECT MAX(attempt_no) FROM operation_attempts WHERE run_id = ?",
            (run_facts["id"],),
        ).fetchone()[0]
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
        if (
            current is None
            or current["responsibility_key"] != responsibility_key(intent)
            or current["action_id"] != intent.action_id
        ):
            raise TransactionError("已保存意图与本次输入的责任不符")
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
            return self._reuse(saved)

        finish = self._finish
        ticket = finish.ticket
        found = connection.execute(
            "SELECT id FROM operation_runs WHERE responsibility_key = ?",
            (ticket.responsibility_key,),
        ).fetchone()
        if found is None or int(found[0]) != ticket.run_id:
            raise TransactionError(f"票据与流程不符: {ticket.responsibility_key}")
        run_facts = _load_row(connection, "operation_runs", ticket.run_id)
        assert run_facts is not None
        self._state["operation_runs"] = {ticket.run_id: run_facts}
        attempt = connection.execute(
            "SELECT id FROM operation_attempts WHERE run_id = ? AND attempt_no = ?",
            (ticket.run_id, ticket.attempt_id),
        ).fetchone()
        if attempt is None:
            raise TransactionError(
                f"尝试不存在: run {ticket.run_id} #{ticket.attempt_id}"
            )
        attempt_id = int(attempt[0])
        attempt_facts = _load_row(connection, "operation_attempts", attempt_id)
        assert attempt_facts is not None
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
        event_count = 1 + (
            1 if finish.retry_wait or finish.run_finish is not None else 0
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
        if finish.retry_wait:
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
        elif finish.run_finish is not None:
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

    def _reuse(self, saved: list[dict]) -> CommandPlan:
        results = [event for event in saved if event["type"] == _ATTEMPT_RESULT_EVENT]
        if not results:
            raise TransactionError("操作身份已用于其他阶段，不能作为结果重送")
        ticket = self._finish.ticket
        attempt_values = None
        for row in _event_rows(results[0], "operation_attempts"):
            if row["after"]["exists"]:
                attempt_values = row["after"]["values"]
        if attempt_values is None:
            raise TransactionError("已保存结果缺少尝试事实")
        if int(attempt_values["run_id"]) != ticket.run_id or int(
            attempt_values["attempt_no"]
        ) != ticket.attempt_id:
            raise TransactionError("已保存结果与本次输入的尝试不符")
        run_status = None
        for event in saved:
            for row in _event_rows(event, "operation_runs"):
                after = row["after"]["values"]
                if "status" in after:
                    run_status = RunStatus[_run_status_name(after["status"])]
        return CommandPlan(
            events=(),
            owners=self._owners,
            state_rows=self._state,
            read_only=True,
            result=FinishAttemptResult(
                disposition=FinishDisposition.SAVED,
                attempt_status=_attempt_status_of(attempt_values["status"]),
                run_status=run_status,
            ),
        )


class OperationRepository:
    """操作意图与结果事务的 SQLite 仓储。"""

    def begin_attempt(
        self, intent, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[BeginAttemptResult]:
        receipt = commit_operation(BeginAttemptCommand(intent, key), key, owned)
        return _outcome_of(receipt)

    def finish_attempt(
        self, finish: AttemptFinish, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[FinishAttemptResult]:
        receipt = commit_operation(FinishAttemptCommand(finish, key), key, owned)
        return _outcome_of(receipt)


def _outcome_of(receipt) -> DbOutcome:
    if receipt.kind == "completed":
        return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
    if receipt.kind == "rolled_back":
        return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
    return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)


# -- 正式业务守卫 -----------------------------------------------------


def _operation_identity_guard(event, context) -> None:
    for row in event.rows:
        if row.table != "operation_runs":
            continue
        facts = _row_after_facts(context, row)
        kind = facts.get("kind")
        purpose = facts.get("query_purpose")
        action_id = facts.get("action_id")
        if kind == int(_RUN_KIND.EMERGENCY_STOP):
            _fail("应急流程不经普通操作事件建立")
        if kind == int(_RUN_KIND.QUERY_ACTIVITY):
            if purpose is None:
                _fail("查询流程必须填写用途")
            if purpose == int(_QUERY_PURPOSE.BEFORE_EXECUTION):
                if facts.get("activity_id") is not None:
                    _fail("执行前检查不得指向具体活动")
                expected = f"query/preflight/{action_id}"
            else:
                activity_id = facts.get("activity_id")
                if activity_id is None:
                    _fail("该查询用途必须指向目标活动")
                purpose_name = decode_member(
                    "operation_runs.query_purpose", purpose
                ).name
                expected = (
                    f"query/{QueryPurpose[purpose_name].value}"
                    f"/{action_id}/{activity_id}"
                )
                _verify_query_original_flow(context, purpose, action_id, activity_id)
        elif kind == int(_RUN_KIND.CHECK_CAPTURE_RESULTS):
            expected = f"results/{facts.get('activity_id')}"
        elif kind == int(_RUN_KIND.STOP_RESIDUAL):
            expected = f"followup/{action_id}/{facts.get('activity_id')}"
        elif kind in (int(_RUN_KIND.START), int(_RUN_KIND.STOP)):
            expected = f"{OperationKind[_kind_name(kind)].value}/{action_id}"
        elif kind == int(_RUN_KIND.READ_FILE):
            expected = f"read/{facts.get('copy_id')}"
        elif kind in (
            int(_RUN_KIND.DELETE_FILE),
            int(_RUN_KIND.CHECK_FILE_EXISTS),
        ):
            expected = f"{OperationKind[_kind_name(kind)].value}/{facts.get('cleanup_item_id')}"
        else:
            _fail(f"未登记的操作种类: {kind!r}")
            continue
        if facts.get("responsibility_key") != expected:
            _fail(
                f"责任键 {facts.get('responsibility_key')!r} 与登记格式 {expected!r} 不符"
            )
        if kind != int(_RUN_KIND.QUERY_ACTIVITY) and purpose is not None:
            _fail("只有查询流程保存查询用途")


def _verify_query_original_flow(
    context, purpose: int, action_id: int, activity_id: int
) -> None:
    purpose_name = decode_member("operation_runs.query_purpose", purpose).name
    if purpose_name == "ACTIVITY_OBSERVATION":
        activity = context.state_rows.get("device_activities", {}).get(activity_id)
        if activity is None or activity.get("action_id") != action_id:
            _fail("活动核实必须指向发起动作自己的活动")
        return
    original_kind = _PURPOSE_FLOW_KIND[QueryPurpose[purpose_name]]
    runs = context.state_rows.get("operation_runs", {})
    for facts in runs.values():
        if (
            facts.get("kind") == int(original_kind)
            and facts.get("action_id") == action_id
            and facts.get("activity_id") == activity_id
        ):
            return
    _fail(f"查询用途 {purpose_name} 缺少对应的原流程事实")


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
    elif copy_round is not None:
        _fail("只有读取尝试保存拷贝轮次")


def _attempt_result_guard(event, context) -> None:
    for row in event.rows:
        if row.table != "operation_attempts" or not row.before.exists:
            continue
        after = row.after.values
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


def _operation_finish_guard(event, context) -> None:
    for row in event.rows:
        if row.table != "operation_runs" or not row.before.exists:
            continue
        after = row.after.values
        if "status" not in after:
            continue
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
    register_guard("operation_finish", _operation_finish_guard)
    register_guard("retry", _retry_guard)

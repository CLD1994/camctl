"""原子启动授予事务：资格核对、共同保存与同键复用。

同一 BEGIN IMMEDIATE 写事务内核对持有者、占用、窗口、预算、取消
及候选顺序，条件成立后把启动流程、尝试意图、次数、参数与设备活
动派发事实作为一组共同保存；提交成功后才允许派发。首次机会记录
缺活动、意图或参数任一项整组拒绝。
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from camctl.contracts.enums import decode_member, enum_for
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.json_values import is_json_integer, json_equal
from camctl.contracts.values import ObjectId, OperationKey, UtcMicros
from camctl.operations.attempts import (
    AttemptConfig,
    AttemptTicket,
    seconds_from_json,
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
    saved_transaction_events,
    update_change as _update,
)
from camctl.scheduling.resources import current_start_holder
from camctl.scheduling.rules import LaunchWindow, WindowPhase, window_phase

_RUN_KIND = enum_for("operation_runs.kind")
_RUN_STATUS = enum_for("operation_runs.status")
_ATTEMPT_STATUS = enum_for("operation_attempts.status")
_DISPATCH_STATE = enum_for("device_activities.dispatch_state")

_ATTEMPT_STARTED_EVENT = 11

_RUN_UPDATE_COLUMNS = (
    "status",
    "attempts_used",
    "max_attempts_used",
    "timeout_s_json",
    "retry_interval_s_json",
    "retry_wait_required",
)

#: 动作类型编号：三种拍摄动作共用设备占用竞争。
_CAMERA_ACTION_TYPES = (1, 2, 3)

#: 动作类型编号：camera_record（启动机会持有者推导范围）。
_CAMERA_RECORD_TYPE = 2

#: 动作状态：RUNNING。
_ACTION_RUNNING = 2


class GrantOutcome(Enum):
    """授予事务的可靠结果分区。"""

    GRANTED = "granted"
    REJECTED = "rejected"


@dataclass(frozen=True)
class GrantRequest:
    """一次启动授予的完整输入。

    动作必须已经进入执行（RUNNING）且未请求取消；窗口与可信时间
    由调用方按 Q1 规则取得。
    """

    device_id: str
    action_id: int
    window: LaunchWindow
    trusted_wall_now: int
    config: AttemptConfig
    occurred_at: int


@dataclass(frozen=True)
class GrantResult:
    """授予结果：授予时携带票据与活动身份，拒绝时携带可靠原因。"""

    outcome: GrantOutcome
    ticket: AttemptTicket | None = None
    activity_id: int | None = None
    reason: str | None = None


class GrantStartCommand:
    """一次录像启动机会授予的完整事务命令。"""

    def __init__(self, request: GrantRequest, key: OperationKey) -> None:
        self._request = request
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        request = self._request
        ObjectId(request.action_id)
        UtcMicros(request.occurred_at)
        if not isinstance(request.config, AttemptConfig):
            raise TransactionError("启动授予要求受约束的尝试配置")
        if not isinstance(request.device_id, str) or not request.device_id:
            raise TransactionError("启动授予要求明确的设备身份")
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(scope, saved)

        action = self._load_action(connection, request.action_id)
        self._state["actions"] = {request.action_id: action}
        if action["device_id"] != request.device_id:
            raise TransactionError("启动授予的设备与动作保存的设备不符")
        if action["type"] not in _CAMERA_ACTION_TYPES:
            raise TransactionError(
                f"启动授予只适用于拍摄动作: {request.action_id}"
            )
        if action["cancel_requested"]:
            return self._rejected("canceled")
        if action["status"] != _ACTION_RUNNING:
            return self._rejected("not_started")

        phase = window_phase(request.window, request.trusted_wall_now)
        if phase is WindowPhase.BEFORE_START:
            return self._rejected("too_early")
        if phase is WindowPhase.AFTER_WINDOW:
            return self._rejected("window_ended")

        holder = current_start_holder(connection, request.device_id)
        if holder is not None and holder.action_id != request.action_id:
            return self._rejected("device_busy")

        if not self._is_first_candidate(connection, request):
            return self._rejected("not_first_candidate")

        with closing(connection.execute(
            "SELECT id FROM device_activities WHERE action_id = ?", (request.action_id,),
        )) as cursor:
            found_activity = cursor.fetchone()
        activity = (None if found_activity is None
                    else row_facts(connection, "device_activities", found_activity[0]))
        if activity is None or activity["action_id"] != request.action_id:
            # 首次机会记录必须包含活动身份；缺活动整组拒绝。
            raise TransactionError(f"设备活动不存在: {request.action_id}")
        self._state["device_activities"] = {activity["id"]: activity}
        if activity["dispatch_state"] not in (
            int(_DISPATCH_STATE.NOT_DISPATCHED),
            int(_DISPATCH_STATE.REJECTED_WITHOUT_EFFECT),
        ):
            raise TransactionError(
                f"设备活动派发状态不允许登记新尝试: {activity['dispatch_state']}"
            )

        key_str = f"start/{request.action_id}"
        with closing(connection.execute(
            "SELECT id FROM operation_runs WHERE responsibility_key = ?", (key_str,)
        )) as cursor:
            found = cursor.fetchone()
        run_facts = None
        if found is not None:
            run_facts = self._load_run(connection, int(found[0]))
            self._verify_start_run(run_facts, activity["id"])
            self._state.setdefault("operation_runs", {})[run_facts["id"]] = run_facts
            if run_facts["status"] not in (
                int(_RUN_STATUS.PENDING),
                int(_RUN_STATUS.ACTIVE),
            ):
                return self._rejected("run_ended")
            if run_facts["attempts_used"] >= request.config.max_attempts:
                return self._rejected("budget_exhausted")
            if run_facts["attempts_used"] >= 1 and run_facts["retry_wait_required"] != 1:
                raise TransactionError("后续尝试要求先建立重试等待")
            attempt_no = int(run_facts["attempts_used"]) + 1
        else:
            attempt_no = 1

        owner = ("action", request.action_id)
        allocation = scope.allocate(1)
        event_id = allocation.first_event_id
        attempt_id = _next_id(connection, "operation_attempts")
        run_id = (
            run_facts["id"]
            if run_facts is not None
            else _next_id(connection, "operation_runs")
        )

        attempt_row = _row(
            "operation_attempts",
            attempt_id,
            {
                "run_id": run_id,
                "attempt_no": attempt_no,
                "copy_round": None,
                "status": int(_ATTEMPT_STATUS.RUNNING),
                "intent_event_id": event_id,
                "result_event_id": None,
                "max_attempts_used": request.config.max_attempts,
                "timeout_s_json": request.config.timeout_s,
                "retry_interval_s_json": request.config.retry_interval_s,
                "effect_state": 1,
                "result_json": None,
                "error_json": None,
            },
        )
        if run_facts is None:
            run_row = _row(
                "operation_runs",
                run_id,
                {
                    **self._start_run_identity(activity["id"]),
                    "status": int(_RUN_STATUS.ACTIVE),
                    "attempts_used": 1,
                    "max_attempts_used": request.config.max_attempts,
                    "timeout_s_json": request.config.timeout_s,
                    "retry_interval_s_json": request.config.retry_interval_s,
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
                    "max_attempts_used": request.config.max_attempts,
                    "timeout_s_json": request.config.timeout_s,
                    "retry_interval_s_json": request.config.retry_interval_s,
                    "retry_wait_required": 0,
                },
            )
        activity_row = _update(
            "device_activities",
            activity["id"],
            {"dispatch_state": activity["dispatch_state"]},
            {"dispatch_state": int(_DISPATCH_STATE.MAY_HAVE_DISPATCHED)},
        )
        self._owners[("operation_runs", run_id)] = owner
        self._owners[("operation_attempts", attempt_id)] = owner
        self._owners[("device_activities", activity["id"])] = owner
        event = _envelope(
            event_id,
            allocation.txn_id,
            _ATTEMPT_STARTED_EVENT,
            1,
            (attempt_row, run_row, activity_row),
            request.occurred_at,
        )
        ticket = AttemptTicket(
            attempt_id=attempt_no,
            operation="control",
            target_id=str(activity["id"]),
            responsibility_key=key_str,
            run_id=run_id,
        )
        return CommandPlan(
            events=(event,),
            owners=self._owners,
            state_rows=self._state,
            result=GrantResult(
                outcome=GrantOutcome.GRANTED,
                ticket=ticket,
                activity_id=activity["id"],
            ),
        )

    def _load_action(self, connection, action_id: int) -> dict[str, Any]:
        action = row_facts(connection, "actions", action_id)
        if action is None:
            raise TransactionError(f"动作不存在: {action_id}")
        return action

    def _load_run(self, connection, run_id: int) -> dict[str, Any]:
        values = row_facts(connection, "operation_runs", run_id)
        if values is None:
            raise TransactionError(f"启动流程不存在: {run_id}")
        for column in ("timeout_s_json", "retry_interval_s_json"):
            values[column] = seconds_from_json(values[column])
        return values

    def _start_run_identity(self, activity_id: int) -> dict[str, Any]:
        action_id = self._request.action_id
        return {"action_id": action_id, "delivery_id": None, "kind": int(_RUN_KIND.START),
                "query_purpose": None, "responsibility_key": f"start/{action_id}",
                "activity_id": activity_id, "copy_id": None, "cleanup_item_id": None, "session_key": None}

    def _verify_start_run(self, run: dict, activity_id: int) -> dict[str, Any]:
        identity = self._start_run_identity(activity_id)
        if any(not json_equal(run[name], value) for name, value in identity.items()):
            raise TransactionError("启动流程的只读身份与动作或活动不符")
        self._verify_start_run_state(run)
        return identity

    @staticmethod
    def _verify_start_run_state(run: dict) -> None:
        status, attempts, waiting = (run[name] for name in
                                     ("status", "attempts_used", "retry_wait_required"))
        if (not all(is_json_integer(value) for value in (status, attempts, waiting))
                or attempts < 0 or waiting not in (0, 1)):
            raise TransactionError("启动流程的状态、次数或等待标志非法")
        try:
            state = _RUN_STATUS(int(status))
        except ValueError as error:
            raise TransactionError("启动流程的状态未登记") from error
        if state is _RUN_STATUS.PENDING:
            valid = attempts == 0 and waiting == 0
        elif state is _RUN_STATUS.ACTIVE:
            valid = attempts > 0
        else:
            valid = waiting == 0 and (attempts > 0 or state not in
                                      (_RUN_STATUS.SUCCEEDED, _RUN_STATUS.UNCONFIRMED))
        if not valid:
            raise TransactionError("启动流程的状态、次数与等待标志组合不符")

    def _is_first_candidate(self, connection, request: GrantRequest) -> bool:
        """同设备存在排序更早的合格候选时不授予本动作。"""
        with closing(connection.execute(
            "SELECT a.id, a.scheduled_at, a.plan_id, a.input_index"
            " FROM actions a"
            " WHERE a.type IN (1, 2, 3) AND a.status = ? AND a.cancel_requested = 0"
            " AND a.device_id = ? AND a.scheduled_at IS NOT NULL",
            (_ACTION_RUNNING, request.device_id),
        )) as cursor:
            rows = cursor.fetchall()
        action = self._state["actions"][request.action_id]
        mine = (
            action["scheduled_at"],
            action["plan_id"],
            action["input_index"],
        )
        for action_id, scheduled_at, plan_id, input_index in rows:
            if int(action_id) == request.action_id:
                continue
            other = (scheduled_at, plan_id, input_index)
            if other < mine:
                # 更早候选也须在窗口内才构成排序阻挡。
                other_end = scheduled_at + (action["max_delay_ms"] or 0) * 1000
                other_window = LaunchWindow(
                    scheduled_at=scheduled_at, window_end=other_end
                )
                if window_phase(other_window, request.trusted_wall_now) in (
                    WindowPhase.IN_WINDOW,
                    WindowPhase.AFTER_WINDOW,
                ):
                    return False
        return True

    def _rejected(self, reason: str) -> CommandPlan:
        return CommandPlan(
            events=(),
            owners=self._owners,
            state_rows=self._state,
            read_only=True,
            result=GrantResult(outcome=GrantOutcome.REJECTED, reason=reason),
        )

    def _reuse(self, scope, saved: list[dict]) -> CommandPlan:
        if (len(saved) != 1 or saved[0]["type"] != _ATTEMPT_STARTED_EVENT
                or saved[0]["reason"] != 1):
            raise TransactionError("操作身份已用于其他阶段，不能作为授予重送")
        event = saved[0]
        request = self._request
        rows = event["body"]["rows"]
        by_table = {row["table"]: row for row in rows}
        if (event["occurred_at"] != request.occurred_at or len(rows) != 3
                or set(by_table) != {"operation_attempts", "operation_runs", "device_activities"}):
            raise TransactionError("原授予的事务组成或事实时刻与输入不符")
        attempt_row = by_table["operation_attempts"]
        run_row = by_table["operation_runs"]
        activity_row = by_table["device_activities"]
        if (attempt_row["before"]["exists"] or not attempt_row["after"]["exists"]
                or not run_row["after"]["exists"] or not activity_row["before"]["exists"]
                or not activity_row["after"]["exists"]):
            raise TransactionError("原授予的尝试、流程或活动不属于本次输入")
        values = attempt_row["after"]["values"]
        if (not json_equal(values["run_id"], run_row["id"])
                or not json_equal(values["intent_event_id"], event["event_id"])
                or values["copy_round"] is not None):
            raise TransactionError("原授予的尝试与流程或意图不符")
        connection = scope.connection
        attempt = row_facts(connection, "operation_attempts", attempt_row["id"])
        run = self._load_run(connection, run_row["id"])
        activity = row_facts(connection, "device_activities", activity_row["id"])
        action = self._load_action(connection, request.action_id)
        fixed = self._verify_start_run(run, activity_row["id"])
        if (attempt is None or activity is None or activity["action_id"] != request.action_id
                or action["device_id"] != request.device_id):
            raise TransactionError("原授予的可靠记录或只读关联与输入不符")
        config = {"max_attempts_used": request.config.max_attempts,
                  "timeout_s_json": request.config.timeout_s,
                  "retry_interval_s_json": request.config.retry_interval_s}
        attempt_columns = ("run_id", "attempt_no", "copy_round", "intent_event_id", *config)
        if (any(not json_equal(attempt[name], values[name]) for name in attempt_columns)
                or any(not json_equal(values[name], value) for name, value in config.items())):
            raise TransactionError("原授予的尝试身份或采用配置与输入不符")
        transaction = event["transaction"]
        after = read_row_values_at_boundary(connection, owner=("action", request.action_id),
            table="operation_runs", row_id=run_row["id"], columns=frozenset(_RUN_UPDATE_COLUMNS),
            current_values=run, boundary=HistoryBoundary(transaction.txn_id, transaction.last_event_id),
            current_boundary=HistoryBoundary(scope.max_txn_id, scope.max_event_id))
        before = {**after, **run_row["before"]["values"]}
        self._verify_start_run_state(after)
        if run_row["before"]["exists"]:
            self._verify_start_run_state(before)
        if (not json_equal(after["status"], int(_RUN_STATUS.ACTIVE))
                or not json_equal(after["attempts_used"], values["attempt_no"])
                or not json_equal(after["retry_wait_required"], 0)
                or any(not json_equal(after[name], values[name]) for name in config)
                or any(name in after and not json_equal(value, after[name])
                       for name, value in run_row["after"]["values"].items())
                or (not run_row["before"]["exists"]
                    and (not json_equal(values["attempt_no"], 1)
                         or any(not json_equal(run_row["after"]["values"][name], value)
                                for name, value in fixed.items())))
                or (run_row["before"]["exists"]
                    and (before["status"] not in (int(_RUN_STATUS.PENDING), int(_RUN_STATUS.ACTIVE))
                         or not json_equal(before["attempts_used"], values["attempt_no"] - 1)
                         or (values["attempt_no"] > 1 and not json_equal(before["retry_wait_required"], 1))))):
            raise TransactionError("原授予的完整流程状态、次数或配置不符")
        ticket = AttemptTicket(
            attempt_id=int(values["attempt_no"]),
            operation="control",
            target_id=str(activity_row["id"]),
            responsibility_key=run["responsibility_key"],
            run_id=run_row["id"],
        )
        return CommandPlan(
            events=(),
            owners=self._owners,
            state_rows=self._state,
            read_only=True,
            result=GrantResult(
                outcome=GrantOutcome.GRANTED,
                ticket=ticket,
                activity_id=activity_row["id"],
            ),
        )


class SchedulingRepository:
    """调度授予事务的 SQLite 仓储。"""

    def grant_start(
        self, request: GrantRequest, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[GrantResult]:
        receipt = commit_operation(GrantStartCommand(request, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

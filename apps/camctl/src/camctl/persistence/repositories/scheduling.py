"""原子启动授予事务：资格核对、共同保存与同键复用。

同一 BEGIN IMMEDIATE 写事务内核对持有者、占用、窗口、预算、取消
及候选顺序，条件成立后把启动流程、尝试意图、次数、参数与设备活
动派发事实作为一组共同保存；提交成功后才允许派发。首次机会记录
缺活动、意图或参数任一项整组拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from camctl.contracts.enums import decode_member, enum_for
from camctl.contracts.values import OperationKey
from camctl.operations.attempts import (
    AttemptConfig,
    AttemptTicket,
    seconds_from_json,
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

#: 动作类型编号：camera_record。
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
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(saved)

        request = self._request
        action = self._load_action(connection, request.action_id)
        self._state["actions"] = {request.action_id: action}
        if action["type"] != _CAMERA_RECORD_TYPE:
            raise TransactionError(
                f"启动授予只适用于录像动作: {request.action_id}"
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

        activity = row_facts(connection, "device_activities", request.action_id)
        if activity is None:
            # 首次机会记录必须包含活动身份；缺活动整组拒绝。
            raise TransactionError(f"设备活动不存在: {request.action_id}")
        self._state["device_activities"] = {request.action_id: activity}
        if activity["dispatch_state"] not in (
            int(_DISPATCH_STATE.NOT_DISPATCHED),
            int(_DISPATCH_STATE.REJECTED_WITHOUT_EFFECT),
        ):
            raise TransactionError(
                f"设备活动派发状态不允许登记新尝试: {activity['dispatch_state']}"
            )

        key_str = f"start/{request.action_id}"
        found = connection.execute(
            "SELECT id FROM operation_runs WHERE responsibility_key = ?", (key_str,)
        ).fetchone()
        run_facts = None
        if found is not None:
            run_facts = self._load_run(connection, int(found[0]))
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
                    "action_id": request.action_id,
                    "delivery_id": None,
                    "kind": int(_RUN_KIND.START),
                    "query_purpose": None,
                    "responsibility_key": key_str,
                    "activity_id": request.action_id,
                    "copy_id": None,
                    "cleanup_item_id": None,
                    "session_key": None,
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
            request.action_id,
            {"dispatch_state": activity["dispatch_state"]},
            {"dispatch_state": int(_DISPATCH_STATE.MAY_HAVE_DISPATCHED)},
        )
        self._owners[("operation_runs", run_id)] = owner
        self._owners[("operation_attempts", attempt_id)] = owner
        self._owners[("device_activities", request.action_id)] = owner
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
            target_id=str(request.action_id),
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
                activity_id=request.action_id,
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

    def _is_first_candidate(self, connection, request: GrantRequest) -> bool:
        """同设备存在排序更早的合格候选时不授予本动作。"""
        rows = connection.execute(
            "SELECT a.id, a.scheduled_at, a.plan_id, a.input_index"
            " FROM actions a"
            " WHERE a.type = ? AND a.status = ? AND a.cancel_requested = 0"
            " AND a.device_id = ? AND a.scheduled_at IS NOT NULL",
            (_CAMERA_RECORD_TYPE, _ACTION_RUNNING, request.device_id),
        ).fetchall()
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

    def _reuse(self, saved: list[dict]) -> CommandPlan:
        started = [event for event in saved if event["type"] == _ATTEMPT_STARTED_EVENT]
        if not started:
            raise TransactionError("操作身份已用于其他阶段，不能作为授予重送")
        attempt_values = None
        for row in started[0]["body"].get("rows", []):
            if row.get("table") == "operation_attempts" and row["after"]["exists"]:
                attempt_values = row["after"]["values"]
        if attempt_values is None:
            raise TransactionError("已保存授予缺少尝试事实")
        request = self._request
        run_id = int(attempt_values["run_id"])
        ticket = AttemptTicket(
            attempt_id=int(attempt_values["attempt_no"]),
            operation="control",
            target_id=str(request.action_id),
            responsibility_key=f"start/{request.action_id}",
            run_id=run_id,
        )
        return CommandPlan(
            events=(),
            owners=self._owners,
            state_rows=self._state,
            read_only=True,
            result=GrantResult(
                outcome=GrantOutcome.GRANTED,
                ticket=ticket,
                activity_id=request.action_id,
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

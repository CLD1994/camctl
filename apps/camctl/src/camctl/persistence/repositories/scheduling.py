"""调度侧开始、窗口观察与原子启动授予事务：资格核对、共同保存与同键复用。

开始事务把取得时间资格的 pending 拍摄动作转入执行并登记设备活动
身份与固定能力（录像同时建立处理责任），一个事务内共同保存。观
察事务在启动窗口内检查到动作时先保存首次观察，再进入设备条件与
调度资格判断；窗口外仍未派发的动作在同一事务保存过期终态、原因
（按持久化观察有无区分错过与耗尽）与计划完成事实。授予事务在同
一 BEGIN IMMEDIATE 写内核对持有者、占用、窗口、预算、取消及候
选顺序，条件成立后把启动流程、尝试意图、次数、参数与设备活动派
发事实作为一组共同保存；提交成功后才允许派发。首次机会记录缺活
动、意图或参数任一项整组拒绝。
"""

from __future__ import annotations

import uuid
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from camctl.contracts.enums import decode_member, enum_for
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.json_values import is_json_integer, json_equal, parse_exact_json
from camctl.contracts.values import ObjectId, OperationKey, UtcMicros
from camctl.capture.models import activity_capabilities
from camctl.history.validators import EventValidationError, register_guard
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
from camctl.scheduling.rules import (
    ExpirationReason,
    LaunchWindow,
    WindowPhase,
    expiration_reason,
    window_phase,
)

_RUN_KIND = enum_for("operation_runs.kind")
_RUN_STATUS = enum_for("operation_runs.status")
_ATTEMPT_STATUS = enum_for("operation_attempts.status")
_DISPATCH_STATE = enum_for("device_activities.dispatch_state")

_ATTEMPT_STARTED_EVENT = 11
_ACTION_STARTED_EVENT = 5
_ACTIVITY_CREATE_EVENT = 13
_WINDOW_OBSERVED_EVENT = 7
_ACTION_FINISHED_EVENT = 8
_PLAN_STATUS_EVENT = 9

#: 拍摄动作类型编号到能力模块使用的字面名称。
_CAMERA_TYPE_NAMES = {1: "camera_take_photo", 2: "camera_record",
                      3: "camera_timelapse"}

#: 动作状态：终态集合。
_ACTION_TERMINAL = (3, 4, 5, 6)

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

#: 动作状态：EXPIRED（过期终态）。
_ACTION_EXPIRED = 5

#: 计划状态：COMPLETED（计划完成）。
_PLAN_COMPLETE = 3

#: 过期原因成员到数据库整数编号；与公共枚举登记一一对应。
_EXPIRATION_CODES = {
    ExpirationReason.WINDOW_MISSED: 1,
    ExpirationReason.WINDOW_EXHAUSTED: 2,
}


def register_window_guard() -> None:
    """注册窗口观察事件的正式守卫（装配期调用）。"""
    register_guard("window", _window_guard)


def _window_guard(event, context) -> None:
    """首次窗口内观察与动作保存的启动窗口共同校验。"""
    for row in event.rows:
        if row.table != "actions":
            continue
        facts = context.state_rows.get("actions", {}).get(row.row_id)
        if facts is None:
            raise EventValidationError(
                f"窗口观察缺少动作事实: actions#{row.row_id}")
        if row.before.values.get("first_window_observed_at") is not None:
            raise EventValidationError("首次观察不能覆盖已有观察时间")
        observed = row.after.values.get("first_window_observed_at")
        if not isinstance(observed, int) or isinstance(observed, bool):
            raise EventValidationError(f"观察时间必须是整数微秒: {observed!r}")
        scheduled = facts.get("scheduled_at")
        max_delay = facts.get("max_delay_ms")
        if (not isinstance(scheduled, int) or isinstance(scheduled, bool)
                or not isinstance(max_delay, int) or isinstance(max_delay, bool)):
            raise EventValidationError(
                f"窗口观察要求动作保存启动窗口: actions#{row.row_id}")
        window = LaunchWindow(
            scheduled_at=scheduled, window_end=scheduled + max_delay * 1000)
        if window_phase(window, observed) is not WindowPhase.IN_WINDOW:
            raise EventValidationError(
                f"观察时间不在启动窗口内: actions#{row.row_id}")


class StartOutcome(Enum):
    """开始事务的可靠结果分区。"""

    STARTED = "started"
    ALREADY = "already"
    REJECTED = "rejected"


@dataclass(frozen=True)
class StartActionRequest:
    """一次动作开始事务的输入。

    动作必须仍为 pending 且未请求取消；可信时间由调用方按 Q1 规
    则取得，事务内按动作自身窗口复核。
    """

    action_id: int
    trusted_wall_now: int
    occurred_at: int


@dataclass(frozen=True)
class StartActionResult:
    """开始结果：开始时携带活动身份，拒绝时携带可靠原因。"""

    outcome: StartOutcome
    activity_id: int | None = None
    reason: str | None = None


class StartActionCommand:
    """拍摄动作取得处理资格的完整事务命令。

    同一事务保存开始事实（ACTION_STARTED：status 1→2、执行标记
    0→1，录像动作同时建立处理责任）与设备活动登记（DEVICE_
    OBSERVED.CREATE：活动身份及固定能力）。动作进入执行当且仅当
    其活动身份已建立，两者不单独生效。
    """

    def __init__(self, request: StartActionRequest, key: OperationKey) -> None:
        self._request = request
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        request = self._request
        ObjectId(request.action_id)
        UtcMicros(request.occurred_at)
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(scope, saved)

        action = row_facts(connection, "actions", request.action_id)
        if action is None:
            raise TransactionError(f"开始事务要求动作存在: {request.action_id}")
        self._state["actions"] = {request.action_id: action}
        activity_id = self._existing_activity_id(connection, request.action_id)
        if action["status"] == 2:
            if action["execution_started"] != 1:
                raise TransactionError(
                    f"已运行动作缺少开始事实: {request.action_id}")
            if activity_id is None:
                raise TransactionError(
                    f"已运行动作缺少设备活动登记: {request.action_id}")
            return self._already(activity_id)
        if action["status"] in _ACTION_TERMINAL:
            return self._rejected("terminal")
        if action["status"] != 1:
            raise TransactionError(
                f"动作状态不允许开始: {request.action_id} {action['status']!r}")
        if action["execution_started"] != 0:
            raise TransactionError(
                f"开始事务要求未开始的动作: {request.action_id}")
        if activity_id is not None:
            raise TransactionError(
                f"设备活动先于动作开始登记: {request.action_id}")
        if action["cancel_requested"]:
            return self._rejected("canceled")
        literal = _CAMERA_TYPE_NAMES.get(action["type"])
        if literal is None:
            raise TransactionError(
                f"开始事务只适用于拍摄动作: {request.action_id}"
                f" type={action['type']!r}")
        if not isinstance(action["device_id"], str) or not action["device_id"]:
            raise TransactionError(f"拍摄动作缺少设备绑定: {request.action_id}")
        if not isinstance(action["driver_id"], str) or not action["driver_id"]:
            raise TransactionError(f"拍摄动作缺少驱动绑定: {request.action_id}")

        window = LaunchWindow(
            scheduled_at=int(action["scheduled_at"]),
            window_end=int(action["scheduled_at"])
            + int(action["max_delay_ms"]) * 1000,
        )
        phase = window_phase(window, request.trusted_wall_now)
        if phase is WindowPhase.BEFORE_START:
            return self._rejected("too_early")
        if phase is WindowPhase.AFTER_WINDOW:
            return self._rejected("window_ended")

        capabilities = activity_capabilities(
            literal, self._decoded_spec(action["execution_spec_json"]))
        owner = ("action", request.action_id)
        allocation = scope.allocate(2)

        action_row = _update(
            "actions", request.action_id,
            {"status": 1, "execution_started": 0},
            {"status": 2, "execution_started": 1})
        self._owners[("actions", request.action_id)] = owner
        started_rows = [action_row]
        if action["type"] == 2:
            processing_id = _next_id(connection, "recording_processing")
            processing = self._processing_row(processing_id)
            started_rows.append(processing)
            self._owners[("recording_processing", processing_id)] = owner
            self._state.setdefault(
                "recording_processing", {})[processing_id] = dict(
                processing.after.values, id=processing_id)
        started = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _ACTION_STARTED_EVENT, 1, tuple(started_rows), request.occurred_at)

        activity_id = _next_id(connection, "device_activities")
        activity_values = {
            "action_id": request.action_id,
            "task_key": uuid.uuid4().hex,
            "task_locator_json": None,
            "state_query_supported": capabilities.state_query_supported,
            "stop_supported": capabilities.stop_supported,
            "safe_repeat_stop": capabilities.safe_repeat_stop,
            "start_return_meaning": capabilities.start_return_meaning,
            "completion_mode": capabilities.completion_mode,
            "ownership_mode": capabilities.ownership_mode,
            "output_scope_json": capabilities.output_scope_json,
            "baseline_state": 1,
            "baseline_first_event_id": None,
            "baseline_last_event_id": None,
            "dispatch_state": int(_DISPATCH_STATE.NOT_DISPATCHED),
            "activity_state": 1,
            "occupancy_state": 1,
            "sent_at": None,
            "started_at": None,
            "result_wait_margin_ms": None,
            "extra_wait_ms_used": None,
            "expected_check_at": None,
            "wait_completed_event_id": None,
            "capture_json": None,
            "control_elapsed_ns": None,
            "completion_basis": None,
            "completion_evidence_json": None,
            "result_set_state": 1,
            "result_check_json": None,
            "last_error_json": None,
        }
        activity_row = _row("device_activities", activity_id, activity_values)
        self._owners[("device_activities", activity_id)] = owner
        self._state["device_activities"] = {
            activity_id: dict(activity_values, id=activity_id)}
        create = _envelope(
            allocation.last_event_id, allocation.txn_id,
            _ACTIVITY_CREATE_EVENT, 1, (activity_row,), request.occurred_at)
        return CommandPlan(
            events=(started, create), owners=self._owners,
            state_rows=self._state,
            result=StartActionResult(
                outcome=StartOutcome.STARTED, activity_id=activity_id))

    def _processing_row(self, processing_id: int):
        """录像处理责任的初始行：各项均未决定，由后续事务推进。"""
        return _row(
            "recording_processing", processing_id,
            {
                "action_id": self._request.action_id,
                "source_device_file_id": None,
                "check_state": 1,
                "check_decision": 1,
                "check_basis_json": None,
                "media_json": {},
                "repair_state": 1,
                "repair_basis_json": None,
                "repair_output_file_id": None,
                "repair_error_json": None,
                "discard_state": 1,
                "discard_error_json": None,
            })

    @staticmethod
    def _existing_activity_id(connection, action_id: int) -> int | None:
        with closing(connection.execute(
            "SELECT id FROM device_activities WHERE action_id = ?", (action_id,),
        )) as cursor:
            found = cursor.fetchone()
        return None if found is None else int(found[0])

    @staticmethod
    def _decoded_spec(raw: Any) -> Mapping[str, Any]:
        spec = parse_exact_json(raw) if isinstance(raw, str) else raw
        if not isinstance(spec, Mapping):
            raise TransactionError("拍摄动作的执行定义不可解释")
        return spec

    def _already(self, activity_id: int) -> CommandPlan:
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=StartActionResult(
                outcome=StartOutcome.ALREADY, activity_id=activity_id))

    def _rejected(self, reason: str) -> CommandPlan:
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=StartActionResult(
                outcome=StartOutcome.REJECTED, reason=reason))

    def _reuse(self, scope, saved: list[dict]) -> CommandPlan:
        """同键重送：核实原事务组成后恢复首次响应。"""
        request = self._request
        if (len(saved) != 2 or saved[0]["type"] != _ACTION_STARTED_EVENT
                or saved[0]["reason"] != 1
                or saved[1]["type"] != _ACTIVITY_CREATE_EVENT
                or saved[1]["reason"] != 1):
            raise TransactionError("操作身份已用于其他事务，不能作为开始重送")
        for event in saved:
            if event["occurred_at"] != request.occurred_at:
                raise TransactionError("开始事务的事实时刻与原事务不同")
        activity_row = saved[1]["body"]["rows"][0]
        action_row = saved[0]["body"]["rows"][0]
        if (activity_row["table"] != "device_activities"
                or activity_row["id"] is None
                or not activity_row["after"]["exists"]
                or action_row["table"] != "actions"
                or action_row["id"] != request.action_id):
            raise TransactionError("原开始事务的组成与输入不符")
        connection = scope.connection
        action = row_facts(connection, "actions", request.action_id)
        activity = row_facts(connection, "device_activities", activity_row["id"])
        if (action is None or activity is None
                or action["status"] != 2 or action["execution_started"] != 1
                or activity["action_id"] != request.action_id):
            raise TransactionError("原开始事务的可靠记录与输入不符")
        return self._already(int(activity_row["id"]))


class ObserveOutcome(Enum):
    """窗口观察事务的可靠结果分区。"""

    OBSERVED = "observed"
    ALREADY = "already"
    REJECTED = "rejected"


@dataclass(frozen=True)
class ObserveWindowRequest:
    """一次窗口观察事务的输入。

    动作必须是合法受理的拍摄动作；可信时间由调用方按 Q1 规则取
    得，事务内按动作自身窗口复核，只有窗口内检查才形成观察。
    """

    action_id: int
    trusted_wall_now: int
    occurred_at: int


@dataclass(frozen=True)
class ObserveWindowResult:
    """观察结果：保存或已有观察时携带观察时间，拒绝时携带原因。"""

    outcome: ObserveOutcome
    observed_at: int | None = None
    reason: str | None = None


class ObserveWindowCommand:
    """保存拍摄动作首次窗口内观察的完整事务命令。

    调度在允许启动窗口内检查到动作时先保存观察事实，再进入设备
    条件与调度资格判断；观察只写一次、不覆盖，窗口外检查不形成
    观察记录。观察不证明已发出设备命令，也不改变动作状态。
    """

    def __init__(self, request: ObserveWindowRequest, key: OperationKey) -> None:
        self._request = request
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        request = self._request
        ObjectId(request.action_id)
        UtcMicros(request.occurred_at)
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(scope, saved)

        action = row_facts(connection, "actions", request.action_id)
        if action is None:
            raise TransactionError(
                f"观察事务要求动作存在: {request.action_id}")
        self._state["actions"] = {request.action_id: action}
        if action["type"] not in _CAMERA_ACTION_TYPES:
            return self._rejected("not_timed")
        if action["status"] in _ACTION_TERMINAL:
            return self._rejected("terminal")
        if action["status"] != 1 and action["status"] != _ACTION_RUNNING:
            raise TransactionError(
                f"动作状态不允许观察: {request.action_id}"
                f" {action['status']!r}")
        if action["first_window_observed_at"] is not None:
            return CommandPlan(
                events=(), owners=self._owners, state_rows=self._state,
                read_only=True,
                result=ObserveWindowResult(
                    outcome=ObserveOutcome.ALREADY,
                    observed_at=int(action["first_window_observed_at"])))

        window = LaunchWindow(
            scheduled_at=int(action["scheduled_at"]),
            window_end=int(action["scheduled_at"])
            + int(action["max_delay_ms"]) * 1000,
        )
        phase = window_phase(window, request.trusted_wall_now)
        if phase is WindowPhase.BEFORE_START:
            return self._rejected("too_early")
        if phase is WindowPhase.AFTER_WINDOW:
            return self._rejected("window_ended")

        allocation = scope.allocate(1)
        self._owners[("actions", request.action_id)] = (
            "action", request.action_id)
        row = _update(
            "actions", request.action_id,
            {"first_window_observed_at": None},
            {"first_window_observed_at": request.trusted_wall_now})
        event = _envelope(
            allocation.first_event_id, allocation.txn_id,
            _WINDOW_OBSERVED_EVENT, 1, (row,), request.occurred_at)
        return CommandPlan(
            events=(event,), owners=self._owners, state_rows=self._state,
            result=ObserveWindowResult(
                outcome=ObserveOutcome.OBSERVED,
                observed_at=request.trusted_wall_now))

    def _rejected(self, reason: str) -> CommandPlan:
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=ObserveWindowResult(
                outcome=ObserveOutcome.REJECTED, reason=reason))

    def _reuse(self, scope, saved: list[dict]) -> CommandPlan:
        """同键重送：核实原事务组成后恢复首次响应。"""
        request = self._request
        if (len(saved) != 1 or saved[0]["type"] != _WINDOW_OBSERVED_EVENT
                or saved[0]["reason"] != 1):
            raise TransactionError("操作身份已用于其他事务，不能作为观察重送")
        if saved[0]["occurred_at"] != request.occurred_at:
            raise TransactionError("观察事务的事实时刻与原事务不同")
        saved_row = saved[0]["body"]["rows"][0]
        if (saved_row["table"] != "actions"
                or saved_row["id"] != request.action_id):
            raise TransactionError("原观察事务的组成与输入不符")
        action = row_facts(scope.connection, "actions", request.action_id)
        if (action is None or action["first_window_observed_at"]
                != saved_row["after"]["values"]["first_window_observed_at"]):
            raise TransactionError("原观察事务的可靠记录与输入不符")
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=ObserveWindowResult(
                outcome=ObserveOutcome.OBSERVED,
                observed_at=int(action["first_window_observed_at"])))


class ExpireOutcome(Enum):
    """未派发动作过期事务的可靠结果分区。"""

    EXPIRED = "expired"
    REJECTED = "rejected"


@dataclass(frozen=True)
class ExpireActionRequest:
    """一次动作过期事务的输入。

    只处理窗口结束仍未派发（未保存开始事实）的动作；已开始的动
    作在窗口结束后的处理由在途启动事实决定，不经本事务。
    """

    action_id: int
    trusted_wall_now: int
    occurred_at: int


@dataclass(frozen=True)
class ExpireActionResult:
    """过期结果：保存时携带原因编号，拒绝时携带可靠原因。"""

    outcome: ExpireOutcome
    expiration_reason: int | None = None
    reason: str | None = None


class ExpireActionCommand:
    """窗口外未派发拍摄动作的过期事务命令。

    同一事务保存过期终态与原因（ACTION_FINISHED.EXPIRE），原因
    按该事务所见的首次窗口内观察记录确定：没有观察为错过窗口，
    已有观察为窗口耗尽。计划内全部动作终态时一并保存计划完成事
    实。取消优先于过期；窗口内不判定过期。
    """

    def __init__(self, request: ExpireActionRequest, key: OperationKey) -> None:
        self._request = request
        self._key = key
        self._owners: dict[tuple[str, int], tuple[str, int]] = {}
        self._state: dict[str, dict[int, dict[str, Any]]] = {}

    def plan(self, scope) -> CommandPlan:
        connection = scope.connection
        request = self._request
        ObjectId(request.action_id)
        UtcMicros(request.occurred_at)
        saved = saved_transaction_events(connection, self._key)
        if saved is not None:
            return self._reuse(scope, saved)

        action = row_facts(connection, "actions", request.action_id)
        if action is None:
            raise TransactionError(
                f"过期事务要求动作存在: {request.action_id}")
        siblings = _sibling_actions(connection, action)
        self._state["actions"] = siblings
        plan = row_facts(connection, "plans", action["plan_id"])
        assert plan is not None
        self._state["plans"] = {plan["id"]: plan}
        if action["type"] not in _CAMERA_ACTION_TYPES:
            return self._rejected("not_timed")
        if action["status"] in _ACTION_TERMINAL:
            return self._rejected("terminal")
        if action["status"] != 1 and action["status"] != _ACTION_RUNNING:
            raise TransactionError(
                f"动作状态不允许过期: {request.action_id}"
                f" {action['status']!r}")
        if action["cancel_requested"]:
            return self._rejected("canceled")
        if action["execution_started"]:
            return self._rejected("in_flight")
        window = LaunchWindow(
            scheduled_at=int(action["scheduled_at"]),
            window_end=int(action["scheduled_at"])
            + int(action["max_delay_ms"]) * 1000,
        )
        if (window_phase(window, request.trusted_wall_now)
                is not WindowPhase.AFTER_WINDOW):
            return self._rejected("window_active")

        reason_code = _EXPIRATION_CODES[expiration_reason(
            action["first_window_observed_at"])]
        plan_complete = all(
            values.get("status") in _ACTION_TERMINAL
            or values["id"] == request.action_id
            for values in siblings.values()
        ) and plan["status"] in (1, 2)
        allocation = scope.allocate(2 if plan_complete else 1)
        self._owners[("actions", request.action_id)] = (
            "action", request.action_id)
        templates = [
            _envelope(
                allocation.first_event_id, allocation.txn_id,
                _ACTION_FINISHED_EVENT, 3,
                (_update(
                    "actions", request.action_id,
                    {"status": action["status"], "expiration_reason": None},
                    {"status": _ACTION_EXPIRED, "expiration_reason": reason_code},
                ),),
                request.occurred_at,
            )
        ]
        if plan_complete:
            self._owners[("plans", plan["id"])] = ("plan", plan["id"])
            templates.append(
                _envelope(
                    allocation.last_event_id, allocation.txn_id,
                    _PLAN_STATUS_EVENT, 2,
                    (_update(
                        "plans", plan["id"],
                        {"status": plan["status"]},
                        {"status": _PLAN_COMPLETE},
                    ),),
                    request.occurred_at,
                )
            )
        return CommandPlan(
            events=tuple(templates), owners=self._owners,
            state_rows=self._state,
            result=ExpireActionResult(
                outcome=ExpireOutcome.EXPIRED,
                expiration_reason=reason_code))

    def _rejected(self, reason: str) -> CommandPlan:
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=ExpireActionResult(
                outcome=ExpireOutcome.REJECTED, reason=reason))

    def _reuse(self, scope, saved: list[dict]) -> CommandPlan:
        """同键重送：核实原事务组成后恢复首次响应。"""
        request = self._request
        if (len(saved) not in (1, 2)
                or saved[0]["type"] != _ACTION_FINISHED_EVENT
                or saved[0]["reason"] != 3):
            raise TransactionError("操作身份已用于其他事务，不能作为过期重送")
        for event in saved:
            if event["occurred_at"] != request.occurred_at:
                raise TransactionError("过期事务的事实时刻与原事务不同")
        saved_row = saved[0]["body"]["rows"][0]
        if (saved_row["table"] != "actions"
                or saved_row["id"] != request.action_id):
            raise TransactionError("原过期事务的组成与输入不符")
        action = row_facts(scope.connection, "actions", request.action_id)
        if action is None or action["status"] != _ACTION_EXPIRED:
            raise TransactionError("原过期事务的可靠记录与输入不符")
        reason_code = saved_row["after"]["values"].get("expiration_reason")
        return CommandPlan(
            events=(), owners=self._owners, state_rows=self._state,
            read_only=True,
            result=ExpireActionResult(
                outcome=ExpireOutcome.EXPIRED,
                expiration_reason=int(reason_code)))


def _sibling_actions(connection, action) -> dict[int, dict[str, Any]]:
    """计划内全部动作的当前事实；计划完成判定按全体状态聚合。"""
    rows: dict[int, dict[str, Any]] = {}
    for row in connection.execute(
        "SELECT id FROM actions WHERE plan_id = ?", (action["plan_id"],)
    ).fetchall():
        facts = row_facts(connection, "actions", int(row[0]))
        if facts is not None:
            rows[int(row[0])] = facts
    return rows


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
    """调度开始、观察、过期与授予事务的 SQLite 仓储。"""

    def start_action(
        self, request: StartActionRequest, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[StartActionResult]:
        receipt = commit_operation(StartActionCommand(request, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def observe_window(
        self, request: ObserveWindowRequest, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[ObserveWindowResult]:
        receipt = commit_operation(
            ObserveWindowCommand(request, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def expire_action(
        self, request: ExpireActionRequest, key: OperationKey,
        owned: OwnedConnection,
    ) -> DbOutcome[ExpireActionResult]:
        receipt = commit_operation(
            ExpireActionCommand(request, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

    def grant_start(
        self, request: GrantRequest, key: OperationKey, owned: OwnedConnection
    ) -> DbOutcome[GrantResult]:
        receipt = commit_operation(GrantStartCommand(request, key), key, owned)
        if receipt.kind == "completed":
            return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=receipt.result)
        if receipt.kind == "rolled_back":
            return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=receipt.error)
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=receipt.error)

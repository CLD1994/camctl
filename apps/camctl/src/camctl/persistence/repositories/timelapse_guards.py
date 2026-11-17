"""业务存储的事件规则适配与登记。"""

from __future__ import annotations

from camctl.capture.wait_rules import validate_natural_wait_confirmation, validate_natural_wait_definition, validate_natural_wait_run, validate_natural_wait_timing
from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import MAX_OBJECT_ID, ConsistencyError
from camctl.history.validators import EventValidationError


_WAIT_COMPLETED_REASON = 3


def _verify_wait_basis(activity, action, state, *, occurred_at, complete, event_id=None):
    """新旧自然定义统一核对真实 START、完整公式和完成真实时刻。"""
    from .capture import _ATTEMPT_RESULT_EVENT, _history_source_event

    try:
        _validate_wait_values(activity)
        spec = validate_natural_wait_definition(activity, action)
        runs = [run for run in state.get("operation_runs", {}).values()
                if run["responsibility_key"] == f"start/{action['id']}"
                and run["action_id"] == action["id"] and run["activity_id"] == activity["id"]]
        run = validate_natural_wait_run(runs)
        attempts = [attempt for attempt in state.get("operation_attempts", {}).values()
                    if attempt["run_id"] == run["id"]]
        attempt = max(attempts, key=lambda value: value["attempt_no"])
        source = _history_source_event(state, attempt["result_event_id"])
        rows = [row for row in source.rows if row.table == "operation_attempts" and row.row_id == attempt["id"]]
        if (attempt["status"] != int(enum_for("operation_attempts.status").SUCCEEDED)
                or attempt["effect_state"] != int(enum_for("operation_attempts.effect_state").CONFIRMED)
                or source.event_type != _ATTEMPT_RESULT_EVENT or len(rows) != 1
                or source.occurred_at != activity["sent_at"]
                or (event_id is not None and source.event_id >= event_id)
                or any(not json_equal(rows[0].after.values.get(name), attempt[name])
                       for name in ("status", "effect_state", "result_json", "error_json"))):
            raise ValueError("自然等待不匹配原可靠发送")
        validate_natural_wait_confirmation(activity, attempt)
        validate_natural_wait_timing(activity, spec, occurred_at=occurred_at, complete=complete)
    except (KeyError, TypeError, ValueError, ConsistencyError) as error:
        raise EventValidationError(str(error)) from error


def _capture_wait_guard(event, context) -> None:
    """三个分支共同验证合法自然任务、原来源、完整公式与完成资格。"""
    for row in event.rows:
        if row.table != "device_activities" or not row.before.exists:
            continue
        _validate_wait_values(row.after.values)
        activity = dict(context.state_rows.get("device_activities", {}).get(row.row_id, {}))
        activity.update(row.after.values)
        action = context.state_rows.get("actions", {}).get(activity.get("action_id"))
        _verify_wait_basis(activity, action, context.state_rows,
            occurred_at=event.occurred_at, complete=event.reason == _WAIT_COMPLETED_REASON,
            event_id=event.event_id)


def _validate_wait_values(values) -> None:
    check = values.get("expected_check_at")
    if check is not None and (isinstance(check, bool) or not isinstance(check, int) or not 0 < check <= MAX_OBJECT_ID):
        raise EventValidationError(f"预计检查时间必须为正微秒: {check!r}")
    for column in ("result_wait_margin_ms", "extra_wait_ms_used"):
        if column in values:
            value = values[column]
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_OBJECT_ID
            ):
                raise EventValidationError(f"{column} 必须是非负毫秒: {value!r}")


def register_timelapse_guards(register) -> None:
    """注册等待安排事件的正式业务守卫（装配期调用）。"""
    register("capture_wait", _capture_wait_guard)

"""业务存储的事件规则适配与登记。"""

from __future__ import annotations

from camctl.history.validators import EventValidationError


def register_window_guard(register) -> None:
    """注册窗口观察事件的正式守卫（装配期调用）。"""
    register("window", _window_guard)


def _window_guard(event, context) -> None:
    """首次窗口内观察与动作保存的启动窗口共同校验。"""
    from camctl.scheduling.rules import WindowObservationError, validate_window_observation

    for row in event.rows:
        if row.table != "actions":
            continue
        facts = context.state_rows.get("actions", {}).get(row.row_id)
        if facts is None:
            raise EventValidationError(
                f"窗口观察缺少动作事实: actions#{row.row_id}")
        try:
            validate_window_observation(action_id=row.row_id,
                previous=row.before.values.get("first_window_observed_at"),
                observed=row.after.values.get("first_window_observed_at"),
                scheduled_at=facts.get("scheduled_at"), max_delay_ms=facts.get("max_delay_ms"))
        except WindowObservationError as error:
            raise EventValidationError(str(error)) from error

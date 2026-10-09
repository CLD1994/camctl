"""启动保留、设备占用推导与派发再检查。

当前启动机会持有者从同一完整状态边界的动作与启动流程共同推导，
不依赖重试间隔、剩余额度或当前墙钟；发现多个满足条件的动作按
一致性错误处理，不按排序挑一个继续。派发再检查在意图可靠提交
后、实际进入驱动调用前执行：窗口耗尽或取消生效时不派发，已提交
次数不退还。
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from typing import Any

from camctl.contracts.values import ConsistencyError
from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal, parse_exact_json
from camctl.scheduling.rules import LaunchWindow, WindowPhase, window_phase

__all__ = [
    "ConsistencyError",
    "DispatchRecheck",
    "StartHolder",
    "current_start_holder",
    "recheck_dispatch",
]


@dataclass(frozen=True)
class StartHolder:
    """当前启动机会持有者的推导事实。"""

    action_id: int
    run_id: int
    attempts_used: int


_HOLDER_QUERY = (
    "SELECT a.id AS action_id, r.id AS run_id, r.attempts_used AS attempts_used"
    " FROM actions a JOIN operation_runs r"
    " ON r.action_id = a.id AND r.kind = 1 AND r.status = 2 AND r.attempts_used > 0"
    " WHERE a.type = 2 AND a.status = 2 AND a.cancel_requested = 0 AND a.device_id = ?"
    " ORDER BY a.id"
)


def current_start_holder(connection: Any, device_id: str) -> StartHolder | None:
    """推导设备的当前启动机会持有者；至多一个，多个为一致性错误。"""
    with closing(connection.execute(_HOLDER_QUERY, (device_id,))) as cursor:
        rows = cursor.fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise ConsistencyError(
            f"设备 {device_id!r} 存在多个启动机会持有者: {[tuple(row) for row in rows]}"
        )
    action_id, run_id, attempts_used = rows[0]
    return StartHolder(
        action_id=int(action_id), run_id=int(run_id), attempts_used=int(attempts_used)
    )


def baseline_start_reason(activity, action) -> str | None:
    """基准拍摄的意图前提；只读取首次固定的执行定义与可靠活动事实。"""
    ownership = enum_for("device_activities.ownership_mode")
    if activity.get("ownership_mode") != int(ownership.BASELINE_COMPARISON):
        return None
    spec = action.get("execution_spec_json")
    scope = activity.get("output_scope_json")
    if isinstance(spec, str):
        spec = parse_exact_json(spec)
    if isinstance(scope, str):
        scope = parse_exact_json(scope)
    if (not isinstance(spec, dict)
            or spec.get("ownership_mode") != int(ownership.BASELINE_COMPARISON)
            or not json_equal(spec.get("output_scope"), scope)):
        raise ConsistencyError("基准活动的归属方式及范围与原执行定义不符")
    if activity.get("occupancy_state") != int(enum_for("device_activities.occupancy_state").HELD):
        return "occupancy_released"
    if activity.get("dispatch_state") not in (1, 4):
        return "already_dispatched"
    if activity.get("baseline_state") != int(enum_for("device_activities.baseline_state").FIXED):
        return "baseline_pending"
    return None


def capture_scope_blocked(connection, device_id, action_id, ownership_mode, output_scope) -> bool:
    """检查原 HELD 活动的物理占用和输出范围；终态不删除范围责任。"""
    def roots(scope):
        if isinstance(scope, str):
            scope = parse_exact_json(scope)
        if not isinstance(scope, dict):
            raise ConsistencyError("拍摄活动的输出范围不是对象")
        return tuple(scope.get("directories", ()))

    requested = roots(output_scope)
    with closing(connection.execute(
        "SELECT d.action_id,d.activity_state,d.dispatch_state,d.ownership_mode,d.output_scope_json"
        " FROM device_activities d JOIN actions a ON a.id=d.action_id"
        " WHERE a.device_id=? AND d.occupancy_state=1 AND d.action_id<>?",
        (device_id, action_id),
    )) as cursor:
        for other_id, state, dispatch, mode, scope in cursor:
            if ownership_mode != 2 and mode != 2:
                # 原任务归属流程由自己的执行前检查核实物理资格。
                continue
            if state != 3 and dispatch not in (1, 4):
                return True
            if mode != 2 and dispatch in (1, 4):
                # TASK_SCOPE 尚未派发时尚无输出生产，也未取得目录基准范围。
                continue
            existing = roots(scope)
            if not requested or not existing:
                return True
            if any(a == b or a.startswith(b + "/") or b.startswith(a + "/")
                   for a in requested for b in existing):
                return True
    return False


@dataclass(frozen=True)
class DispatchRecheck:
    """派发再检查结果；不允许时携带可靠原因。"""

    allowed: bool
    reason: str | None


def recheck_dispatch(
    *,
    window: LaunchWindow,
    trusted_wall_now: int,
    canceled: bool,
) -> DispatchRecheck:
    """意图提交后、进入驱动调用前的最后核对。

    取消生效优先；窗口判断沿用两端包含规则，超窗不派发。本函数
    只判定资格，不替代未派发事实的保存。
    """
    if canceled:
        return DispatchRecheck(allowed=False, reason="canceled")
    if window_phase(window, trusted_wall_now) is WindowPhase.AFTER_WINDOW:
        return DispatchRecheck(allowed=False, reason="window_ended")
    return DispatchRecheck(allowed=True, reason=None)

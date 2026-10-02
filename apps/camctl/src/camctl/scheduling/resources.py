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

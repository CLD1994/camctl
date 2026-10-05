"""部署装配提供的业务流程构造。

每个流程是接在会话推进循环上的异步端口：接收会话上下文，每轮
被驱动一次，自行管理所需连接与协作者。capture_flow 把到期拍
摄工作推进一个事务批次：先开始取得时间资格的 pending 动作并
登记设备活动，再把执行中的到期动作交给能力处理器；处理器内
部按已保存事实幂等推进，重复调度不产生重复副作用。
"""

from __future__ import annotations

from contextlib import closing
from typing import Any, Callable, Iterable

from camctl.capture.dispatch import dispatch_ready, ready_capture_actions
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.scheduling import (
    SchedulingRepository,
    StartActionRequest,
)
from camctl.session.service import StateDbFailure

__all__ = ["capture_flow"]


def _due_pending_actions(connection: Any, now_us: int) -> list[int]:
    """从当前投影取可开始执行的拍摄动作：待执行、未取消且已到时间。"""
    with closing(connection.execute(
        "SELECT id FROM actions WHERE status = 1 AND cancel_requested = 0"
        " AND type IN (1, 2, 3) AND scheduled_at <= ?"
        " ORDER BY plan_id, input_index", (now_us,)
    )) as cursor:
        return [int(row[0]) for row in cursor.fetchall()]


def capture_flow(capture_factory: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """构造推进拍摄工作的调度流程。

    capture_factory 接收本轮流量的数据库连接，返回组装好的
    CaptureRuntime；生产装配提供真实驱动端口，集成测试注入受
    契约约束的替身。
    """

    async def flow(context: Any) -> None:
        owned = context.open_connection()
        try:
            now = context.clock.utc_micros()
            scheduling = SchedulingRepository()
            for action_id in _due_pending_actions(owned.connection, now):
                outcome = scheduling.start_action(
                    StartActionRequest(
                        action_id=action_id,
                        trusted_wall_now=now,
                        occurred_at=now,
                    ),
                    new_operation_key(),
                    owned,
                )
                if outcome.kind is not DbOutcomeKind.COMPLETED:
                    raise StateDbFailure(
                        f"动作开始事务未完成（{outcome.kind.value}）: {outcome.error}")
            descriptors: Iterable = ready_capture_actions(owned.connection, now)
            runtime = capture_factory(owned)
            await dispatch_ready(runtime, descriptors)
        finally:
            owned.connection.close()

    return flow

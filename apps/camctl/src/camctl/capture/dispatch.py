"""调度侧动作推进循环（Q6 接线）。

按推进资格描述符把到期动作路由到对应能力处理器：未知类型不默认
路由，单个动作的推进错误不阻止其余动作；阻塞（未到时间、占用）
动作没有描述符，自然不创建执行协程。本模块不创建驱动或数据库连
接，一切经 CaptureRuntime 端口。
"""

from __future__ import annotations

from typing import Iterable, Protocol

from camctl.capture.handlers import CaptureRuntime, HandlerOutcome, capture_handler
from camctl.scheduling.service import ActionDescriptor

__all__ = ["ReadyActions", "dispatch_ready", "ready_capture_actions"]


class ReadyActions(Protocol):
    """推进资格检查端口：返回当前可推进的动作描述符。"""

    def __call__(self) -> Iterable[ActionDescriptor]: ...


def ready_capture_actions(connection, now_us: int) -> list[ActionDescriptor]:
    """从当前投影取可推进的拍摄动作：执行中、未取消且已到时间。"""
    from camctl.scheduling.service import ActionDescriptor

    from contextlib import closing

    with closing(connection.execute(
        "SELECT id, type FROM actions WHERE status = 2 AND cancel_requested = 0"
        " AND type IN (1, 2, 3) AND scheduled_at <= ?"
        " ORDER BY plan_id, input_index", (now_us,)
    )) as cursor:
        rows = cursor.fetchall()
    mapping = {1: "camera_take_photo", 2: "camera_record", 3: "camera_timelapse"}
    return [
        ActionDescriptor(action_id=int(row[0]), action_type=mapping[int(row[1])],
                         ready=True)
        for row in rows
    ]


async def dispatch_ready(
    runtime: CaptureRuntime, descriptors: Iterable[ActionDescriptor],
) -> list[tuple[int, HandlerOutcome | BaseException]]:
    """把就绪描述符逐一路由到能力处理器并推进一次。

    单个动作的异常原样记录并继续其余动作；处理器内部按已保存事
    实幂等推进，重复调度不产生重复副作用。
    """
    results: list[tuple[int, HandlerOutcome | BaseException]] = []
    for descriptor in descriptors:
        if not descriptor.ready:
            continue
        try:
            handler = capture_handler(descriptor.action_type)
            await handler(descriptor.action_id, runtime)
            results.append((descriptor.action_id, HandlerOutcome("dispatched")))
        except BaseException as error:  # 单动作失败不阻止其余动作。
            results.append((descriptor.action_id, error))
    return results

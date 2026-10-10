"""调度侧动作推进循环（Q6 接线）。

按推进资格描述符把到期动作路由到对应能力处理器：未知类型不默认
路由，单个动作的业务错误不阻止其余动作，状态前提失效时停止本批；阻塞（未到时间、占用）
动作没有描述符，自然不创建执行协程。本模块不创建驱动或数据库连
接，一切经 CaptureRuntime 端口。
"""

from __future__ import annotations

import asyncio
from typing import Iterable, Protocol

from camctl.capture.handlers import CaptureRuntime, HandlerOutcome, capture_handler
from camctl.contracts.values import ConsistencyError
from camctl.devices.bindings import DeviceConfigurationError
from camctl.persistence.models import DatabaseAccessError
from camctl.scheduling.service import ActionDescriptor

__all__ = ["ReadyActions", "dispatch_ready", "ready_capture_actions"]


class ReadyActions(Protocol):
    """推进资格检查端口：返回当前可推进的动作描述符。"""

    def __call__(self) -> Iterable[ActionDescriptor]: ...


def ready_capture_actions(connection, now_us: int, *, device_id: str | None = None) -> list[ActionDescriptor]:
    """从当前投影取可推进的拍摄动作。

    未取消的到期拍摄正常推进；已取消的三类拍摄继续各自本地收场、
    必要停止及产物保存，处理器阻止普通启动。
    """
    from camctl.scheduling.service import ActionDescriptor

    from contextlib import closing

    query = ("SELECT id, type FROM actions WHERE status = 2"
        " AND ((cancel_requested = 0 AND type IN (1, 2, 3)"
        "       AND scheduled_at <= ?)"
        "      OR (cancel_requested = 1 AND type IN (1, 2, 3)))"
        )
    parameters = (now_us,)
    if device_id is not None:
        query += " AND device_id=?"
        parameters += (device_id,)
    with closing(connection.execute(query + " ORDER BY plan_id, input_index", parameters)) as cursor:
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

    单个动作的异常原样记录；状态库或事实一致性前提失效时停止本批，
    其他动作异常继续推进。处理器内部按已保存事实幂等推进，重复
    调度不产生重复副作用。
    """
    results: list[tuple[int, HandlerOutcome | BaseException]] = []
    for descriptor in descriptors:
        if not descriptor.ready:
            continue
        try:
            handler = capture_handler(descriptor.action_type)
            await handler(descriptor.action_id, runtime)
            results.append((descriptor.action_id, HandlerOutcome("dispatched")))
        except BaseException as error:
            results.append((descriptor.action_id, error))
            if isinstance(error, (DatabaseAccessError, ConsistencyError, DeviceConfigurationError, asyncio.CancelledError)):
                break
    return results

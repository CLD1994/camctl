"""按需动作执行器接入。

只为已具备推进步骤的动作创建执行协程：阻塞（未来时间、来源未
完、占用等）动作没有执行器；处理器按动作类型登记，错误类型不
默认路由到录像。资源限制由统一协调端口核验，本模块不创建驱动
或数据库连接。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Protocol

__all__ = [
    "ActionDescriptor",
    "ActionHandler",
    "HandlerRegistry",
    "ReadyCheck",
    "collect_ready",
]


@dataclass(frozen=True)
class ActionDescriptor:
    """调度侧对动作推进资格的可靠观察。"""

    action_id: int
    action_type: str
    ready: bool
    blocked_reason: str | None = None


class ReadyCheck(Protocol):
    """推进资格检查端口：返回描述符集合。"""

    def __call__(self) -> Iterable[ActionDescriptor]: ...


class ActionHandler(Protocol):
    """一个动作类型的推进处理器。"""

    async def __call__(self, action_id: int, context: Any) -> None: ...


def collect_ready(descriptors: Iterable[ActionDescriptor]) -> list[int]:
    """从描述符集合取已具备推进步骤的动作 ID（按需创建执行者）。"""
    return [item.action_id for item in descriptors if item.ready]


class HandlerRegistry:
    """动作类型到处理器的目录；未知类型不默认路由。"""

    def __init__(self) -> None:
        self._handlers: dict[str, ActionHandler] = {}

    def register_handler(self, action_type: str, handler: ActionHandler) -> None:
        self._handlers[action_type] = handler

    def handler_for(self, action_type: str) -> ActionHandler | None:
        return self._handlers.get(action_type)

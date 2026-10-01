"""有界工作发现与候选缓存。

按未完成管理字段分页加载未完成动作、未结束操作流程，并独立发现
已结束流程下尚未结束的尝试；动作终态不遮蔽仍在途的调用责任。
三路使用稳定 ID 游标与有限批量，不遍历全部历史计划。数据库之外
只保留有界候选缓存，缓存不是授予依据。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

__all__ = [
    "BoundedCandidateCache",
    "DiscoveryCursor",
    "WorkPage",
    "discover_work",
]

_ACTION_QUERY = (
    "SELECT id, plan_id, input_index, type, device_id, scheduled_at, status"
    " FROM actions WHERE status IN (1, 2) AND id > ? ORDER BY id LIMIT ?"
)
_RUN_QUERY = (
    "SELECT id, action_id, kind, query_purpose, responsibility_key, status,"
    " attempts_used, retry_wait_required"
    " FROM operation_runs WHERE status IN (1, 2) AND id > ? ORDER BY id LIMIT ?"
)
_ATTEMPT_QUERY = (
    "SELECT a.id, a.run_id, a.attempt_no, a.status,"
    " r.action_id, r.kind, r.responsibility_key"
    " FROM operation_attempts a JOIN operation_runs r ON a.run_id = r.id"
    " WHERE a.status = 1 AND a.id > ? ORDER BY a.id LIMIT ?"
)


@dataclass(frozen=True)
class DiscoveryCursor:
    """三路分页的稳定位置；各路按自身行 ID 独立推进。"""

    action_id: int = 0
    run_id: int = 0
    attempt_id: int = 0


@dataclass(frozen=True)
class WorkPage:
    """一批发现的工作与下一位置。

    exhausted 为真表示三路都到达末尾；错误不返回空批，由连接层
    的读取错误分类表达。
    """

    pending_actions: tuple[Mapping[str, Any], ...]
    open_runs: tuple[Mapping[str, Any], ...]
    running_attempts: tuple[Mapping[str, Any], ...]
    next: DiscoveryCursor
    exhausted: bool


def _fetch(
    connection: Any, sql: str, cursor_value: int, limit: int
) -> list[dict[str, Any]]:
    cursor = connection.execute(sql, (cursor_value, limit))
    names = [description[0] for description in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def discover_work(
    connection: Any,
    cursor: DiscoveryCursor | None = None,
    *,
    limit: int = 100,
) -> WorkPage:
    """从给定位置发现一批未完成工作。

    每路最多 limit 行并按稳定 ID 升序返回；next 携带本批末位 ID，
    任一路未满批即视为该路到达末尾，三路都到末尾才 exhausted。
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"批量上限必须是正整数: {limit!r}")
    position = cursor or DiscoveryCursor()

    actions = _fetch(connection, _ACTION_QUERY, position.action_id, limit)
    runs = _fetch(connection, _RUN_QUERY, position.run_id, limit)
    attempts = _fetch(connection, _ATTEMPT_QUERY, position.attempt_id, limit)
    exhausted = len(actions) < limit and len(runs) < limit and len(attempts) < limit
    return WorkPage(
        pending_actions=tuple(actions),
        open_runs=tuple(runs),
        running_attempts=tuple(attempts),
        next=DiscoveryCursor(
            action_id=actions[-1]["id"] if actions else position.action_id,
            run_id=runs[-1]["id"] if runs else position.run_id,
            attempt_id=attempts[-1]["id"] if attempts else position.attempt_id,
        ),
        exhausted=exhausted,
    )


class BoundedCandidateCache:
    """数据库之外的有界候选缓存：保留最近候选，淘汰最旧。

    缓存只是减少重复读取；父对象事实以数据库为准，新提交不改变
    已缓存内容。
    """

    def __init__(self, capacity: int) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError(f"缓存容量必须是正整数: {capacity!r}")
        self._items: deque = deque(maxlen=capacity)

    def extend(self, candidates: Iterable[Mapping[str, Any]]) -> None:
        self._items.extend(candidates)

    def snapshot(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self._items)

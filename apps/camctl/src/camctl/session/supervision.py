"""实际任务监督与取消接手。

取消等待与实际执行是两个维度：等待者取消不释放实际资源，实际
结果由责任拥有者接手消费且只消费一次。接手动作必须先登记到本
监督器，再解除原等待；调用方的这一顺序是接口契约的一部分。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = [
    "OwnedTask",
    "OwnerToken",
    "ResponsibilityOwner",
    "SupervisionError",
    "SupervisionResult",
    "Supervisor",
    "TaskFailure",
    "TaskRecord",
]


class SupervisionError(ValueError):
    """监督登记或收场违反接口契约。"""


@dataclass(frozen=True)
class OwnedTask:
    """一项等待者已取消、需要接手消费的实际任务。

    pending 是实际结果的等待入口（如数据库提交句柄或设备调用结
    果），其类型由所属业务定义；监督器不解释其内容，只交给登记
    的拥有者接手。
    """

    identity: str
    stage: str
    business: str
    resources: tuple[str, ...]
    pending: Any


class ResponsibilityOwner(Protocol):
    """实际任务的责任拥有者。"""

    async def take_over(self, task: OwnedTask) -> None:
        """接手一项等待者已取消的任务。

        等待实际结束、保存并通知结果后释放实际占用；接手中途抛
        出的异常由监督器记录，实际结果不会重复投递。
        """
        ...


@dataclass(frozen=True)
class OwnerToken:
    """监督器发放给已登记拥有者的不透明凭据。"""

    index: int


@dataclass(frozen=True)
class TaskRecord:
    """一项已收场任务的登记事实。"""

    identity: str
    stage: str
    business: str
    resources: tuple[str, ...]


@dataclass(frozen=True)
class TaskFailure:
    """一次接手失败的记录，保留诊断信息。"""

    record: TaskRecord
    error: str


@dataclass(frozen=True)
class SupervisionResult:
    """一次收场的结果：已由拥有者完成的任务与接手失败的任务。"""

    settled: tuple[TaskRecord, ...]
    failed: tuple[TaskFailure, ...]


class Supervisor:
    """会话内的实际责任监督器。

    只跟踪独立语义责任并保证实际结果恰好消费一次；不解释任务负
    载，也不创建脱离监督的后台任务。
    """

    def __init__(self) -> None:
        self._owners: dict[OwnerToken, ResponsibilityOwner] = {}
        self._next_index = 0
        self._pending: dict[str, tuple[ResponsibilityOwner, OwnedTask]] = {}
        self._drain_task: asyncio.Task[SupervisionResult] | None = None

    def register(self, owner: ResponsibilityOwner) -> OwnerToken:
        self._next_index += 1
        token = OwnerToken(index=self._next_index)
        self._owners[token] = owner
        return token

    def handoff(self, token: OwnerToken, task: OwnedTask) -> None:
        owner = self._owners.get(token)
        if owner is None:
            raise SupervisionError("接手凭据未登记或已失效")
        if task.identity in self._pending:
            raise SupervisionError(f"责任 {task.identity} 已在等待接手，不能重复登记")
        self._pending[task.identity] = (owner, task)

    def outstanding(self) -> tuple[TaskRecord, ...]:
        return tuple(
            TaskRecord(
                identity=task.identity,
                stage=task.stage,
                business=task.business,
                resources=task.resources,
            )
            for _, task in self._pending.values()
        )

    async def drain_required(self) -> SupervisionResult:
        """消费全部已登记责任的实际结果并返回收场记录。

        收场作为内部任务执行：外部取消收场等待不会中止实际消费，
        之后的重试沿同一收场等待并取得同一结果，不重复消费。
        """
        running = self._drain_task
        if running is not None and not running.done():
            return await asyncio.shield(running)
        task = asyncio.get_running_loop().create_task(self._drain_loop())
        self._drain_task = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._drain_task = None

    async def _drain_loop(self) -> SupervisionResult:
        settled: list[TaskRecord] = []
        failed: list[TaskFailure] = []
        while self._pending:
            for identity, (owner, task) in list(self._pending.items()):
                record = TaskRecord(
                    identity=task.identity,
                    stage=task.stage,
                    business=task.business,
                    resources=task.resources,
                )
                del self._pending[identity]
                try:
                    await asyncio.shield(_consume(owner, task))
                except Exception as error:  # 接手异常保留诊断，不吞掉。
                    failed.append(
                        TaskFailure(record=record, error=f"{type(error).__name__}: {error}")
                    )
                else:
                    settled.append(record)
        return SupervisionResult(settled=tuple(settled), failed=tuple(failed))


async def _consume(owner: ResponsibilityOwner, task: OwnedTask) -> None:
    await owner.take_over(task)

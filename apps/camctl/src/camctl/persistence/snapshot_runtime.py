"""正常 run 的快照维护拥有者；提交及新对象发现驱动单批维护。

数据库任务与普通业务共用执行器。维护拥有独立通知版本，不占业务
工作计数；退出时停止新增维护并接收已开始操作的真实结果。
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from functools import partial
from pathlib import Path
from typing import Callable

from camctl.contracts.enums import load_registry
from camctl.contracts.values import OperationKey
from camctl.history.snapshots import (
    MaintenanceContext, MaintenanceOutcome, MaintenanceState,
    SnapshotEnqueueTimeout, SnapshotMaintenanceError, maintain_snapshots,
)
from camctl.logging_runtime.diagnostics import record_error
from camctl.logging_runtime import get_sync_logger
from camctl.persistence.business import BusinessDatabase, settle_required
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.history import SqliteSnapshotStore
from camctl.persistence.runtime import DbConfig, OwnedConnection
from camctl.scheduling.notifications import WakeReason, WorkNotifier

__all__ = ["SnapshotMaintenanceOwner"]

_OBJECTS = load_registry()["history_objects"]
_SNAPSHOT_TYPES = ",".join(
    str(spec["id"]) for spec in _OBJECTS.values() if spec["snapshot"])
_INVALID_PROGRESS = (
    "p.id IS NULL OR p.current_change_count < 1"
    " OR p.snapshot_change_count < 0"
    " OR p.current_change_count < p.snapshot_change_count")
_COMMIT_PROGRESS = f"""
WITH own_transaction AS (
    SELECT id, first_event_id, last_event_id
    FROM history_transactions WHERE operation_key = ?
)
SELECT t.id,
       MAX(CASE WHEN p.pending_changes >= ? THEN 1 ELSE 0 END),
       MAX(CASE WHEN {_INVALID_PROGRESS} THEN 1 ELSE 0 END)
FROM own_transaction AS t
CROSS JOIN entity_event_links AS l INDEXED BY entity_links_event
LEFT JOIN entity_snapshot_progress AS p
  ON p.entity_type = l.entity_type AND p.entity_id = l.entity_id
WHERE l.event_id BETWEEN t.first_event_id AND t.last_event_id
  AND l.entity_type IN ({_SNAPSHOT_TYPES})
GROUP BY t.id
"""
class SnapshotMaintenanceOwner:
    """维护生命周期与业务生命周期分开；积压本身不延长会话。"""

    def __init__(
        self, database: BusinessDatabase, path: Path, *, threshold: int,
        batch_size: int, config: DbConfig | None = None,
        on_disabled: Callable[[str], None] | None = None,
    ) -> None:
        self._state = MaintenanceState()
        self._path = path
        self._database_id = database.metadata.instance_id
        self._store = SqliteSnapshotStore(path, database.executor, config=config)
        self._context = MaintenanceContext(
            threshold, batch_size, self._state, self._store)
        self._notifier = WorkNotifier()
        self._on_disabled = (on_disabled if on_disabled is not None
                             else partial(get_sync_logger(__name__).warning,
                                 extra={"stage": "snapshot.maintenance",
                                        "database_id": self._database_id}))
        self._started = False
        self._task: asyncio.Task | None = None
        self._discovery_task: asyncio.Task | None = None
        self._discovery_ready = False
        self._disabled_reported = False
        self._last_plan_id = 0
        # 只在串行数据库 worker 的确认提交回调内推进。
        self._last_observed_transaction = 0

    async def start(self) -> None:
        """启动时记录外部发现上界，安排一次持久化候选检查。"""
        self.check_completed()
        if self._started or self._state.closing or self._state.disabled:
            return
        self._started = True
        self._task = asyncio.create_task(
            self._run(), name="camctl-snapshot-maintenance")

    def observe_commit(
        self, owned: OwnedConnection, outcome: DbOutcome, key: OperationKey,
    ) -> None:
        """worker 只检查原申请实际事务影响的快照对象，不猜当前头。"""
        if (outcome.kind is not DbOutcomeKind.COMPLETED
                or self._state.closing or self._state.disabled):
            return
        with closing(owned.connection.execute(
                _COMMIT_PROGRESS, (str(key), self._context.threshold))) as cursor:
            row = cursor.fetchone()
        if row is None:
            return
        transaction_id, eligible, invalid = row
        if invalid:
            raise SnapshotMaintenanceError("本次已提交对象缺少有效快照维护进度")
        if transaction_id <= self._last_observed_transaction:
            return
        self._last_observed_transaction = transaction_id
        if eligible:
            self._notifier.mark_changed(WakeReason.NEW_WORK)

    async def discover(self) -> None:
        """在既有新计划检查机会内，只核新上界内的计划及动作。"""
        self.check_completed()
        if (not self._discovery_ready or self._state.closing
                or self._state.disabled):
            return
        self._discovery_task = asyncio.create_task(
            self._store.discover_objects(
                self._last_plan_id, self._context.threshold),
            name="camctl-snapshot-discovery")
        try:
            upper, eligible = await self._discovery_task
        except SnapshotEnqueueTimeout as error:
            # 入队超时已按停用分类交付，不作为后续 check/settle 的数据库故障。
            self._discovery_task = None
            self._disable(error)
            return
        except asyncio.CancelledError:
            if self._state.closing:
                return
            raise
        self._last_plan_id = upper
        if eligible and not self._state.closing and not self._state.disabled:
            self._notifier.mark_changed(WakeReason.NEW_WORK)

    def stop_new_work(self) -> None:
        """立即进入收尾；未开始可撤回，已开始由原任务实际收场。"""
        if self._state.closing:
            return
        self._state.close()
        for task in self._tasks():
            if not task.done():
                task.cancel()

    def check_completed(self) -> None:
        """错误必须交给会话；不把失败任务解释为没有候选。"""
        for task in self._tasks():
            if task.done():
                if task.cancelled() and (self._state.closing or self._state.disabled):
                    continue
                task.result()

    async def settle(self) -> None:
        """停止安排后等待实际任务；重复取消不丢弃迟到错误。"""
        async def collect():
            results = await asyncio.gather(*self._tasks(), return_exceptions=True)
            failures = [result for result in results if isinstance(result, BaseException)
                        and not (isinstance(result, asyncio.CancelledError)
                                 and (self._state.closing or self._state.disabled))]
            if len(failures) == 1:
                raise failures[0]
            if failures:
                raise BaseExceptionGroup("快照维护收场失败", failures)

        await settle_required(collect())

    def _tasks(self):
        return tuple(task for task in (self._task, self._discovery_task) if task is not None)

    def _disable(self, error: SnapshotEnqueueTimeout | None = None) -> None:
        self._state.disable()
        if self._task is not None and self._task is not asyncio.current_task() and not self._task.done():
            self._task.cancel()
        if not self._disabled_reported:
            self._disabled_reported = True
            if error is not None:
                record_error(error, stage="snapshot_enqueue",
                             context={"state_db": self._path, "database_id": self._database_id})
            self._on_disabled("快照操作入队前超时，本次会话停止快照维护")

    async def _run(self) -> None:
        try:
            self._last_plan_id, _ = await self._store.discover_objects(
                None, self._context.threshold)
        except SnapshotEnqueueTimeout as error:
            self._disable(error)
            return
        self._discovery_ready = True
        while not self._state.closing and not self._state.disabled:
            observed = self._notifier.snapshot()
            result = await maintain_snapshots(self._context, max_batches=1)
            if result.outcome is MaintenanceOutcome.DISABLED:
                self._disable(result.exception)
                return
            if result.outcome is MaintenanceOutcome.CLOSING:
                return
            again = (result.outcome is MaintenanceOutcome.YIELDED or any(
                item.remaining_changes >= self._context.threshold
                for item in result.saved))
            del result
            if not again and not self._state.closing:
                # observed 在查询前取得；查询、准备和保存期间的新通知不丢失。
                await self._notifier.wait_changed(observed)

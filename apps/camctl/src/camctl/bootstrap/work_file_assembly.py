"""一次正常运行的有限中间文件维护与实际责任监督。"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from camctl.host_files.tasks import FileTaskExecutor
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.history.decoding import decode_event_row
from camctl.outputs.work_files import (
    CleanupScan, WorkFileContext, WorkFileHistory, WorkFileLimits,
    WorkFileAction, classify_work_file, clean_one_work_file, clean_work_files,
    WorkFileSingleOutcome,
)
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.session.service import StateDbFailure
from camctl.session.supervision import OwnedTask, Supervisor

_RETENTION = enum_for("intermediate_files.retention_state")
_ACTION_STATUS = enum_for("actions.status")
_DELIVERY_STATUS = enum_for("deliveries.status")
_TERMINAL_ACTIONS = frozenset(int(_ACTION_STATUS[name]) for name in (
    "SUCCEEDED", "FAILED", "EXPIRED", "CANCELED"))
_TERMINAL_DELIVERIES = frozenset(int(_DELIVERY_STATUS[name]) for name in (
    "PUBLISHED", "FAILED", "CANCELED", "WITHDRAWN"))


class WorkFileRuntime:
    """保存会话的固定范围与处理记录，后台责任使用共同数据库端口。"""

    def __init__(self, *, staging: Path, limits: WorkFileLimits, pending_media_results=None,
                 download_cleanup=None):
        self.staging = staging
        self.limits = limits
        self.repository = OutputsRepository()
        self.supervisor = Supervisor()
        # 整体维护可以等待文件接手；两类监督不能相互等待同一责任。
        self.file_supervisor = Supervisor()
        self.executor = FileTaskExecutor(self.file_supervisor)
        self.history = WorkFileHistory()
        self.processed: dict = {}
        self.pending_results: dict = {}
        self.pending_withdrawals: dict = {}
        self.pending_media_results = {} if pending_media_results is None else pending_media_results
        self._task: asyncio.Task | None = None
        self._token = self.supervisor.register(self)
        self._stopping = False
        self._discovery_after = 0
        self._first_files: set[int] = set()
        self.download_cleanup = download_cleanup

    async def resume_media_results(self, database) -> None:
        """筛选业务前核实会话已取得的媒体申请，不开始设备或工具操作。"""
        from camctl.capture.media_flow import resume_media_saves

        await self._cleanup_downloads(database)
        try:
            await resume_media_saves(database, self.pending_media_results, self.executor)
        except (ConsistencyError, sqlite3.Error) as error:
            raise StateDbFailure(f"原媒体申请未可靠保存: {error}") from error

    async def _cleanup_downloads(self, database):
        if self.download_cleanup is not None:
            try:
                await self.download_cleanup.run(database)
            except (OSError, ValueError, sqlite3.Error) as error:
                raise StateDbFailure(f"原 READ 下载遗留未可靠回收: {error}") from error

    async def first_cleanup(self, file_ids: tuple[int, ...], *, database, occurred_at: int):
        """采用本次必要文件的首次结果，不使用历史扫描范围或额度。"""
        from camctl.cancellation.ports import SettlementOutcome

        work = WorkFileContext(
            repository=self.repository, database=database, staging=self.staging,
            occurred_at=occurred_at, limits=self.limits, processed=self.processed,
            executor=self.executor, history=self.history, pending_results=self.pending_results)
        failed = False
        for file_id in file_ids:
            facts = await database.read(
                lambda owned: self.repository.load_work_file_state(file_id, owned),
                description="中间文件首次清理状态读取")
            if (file_id in self.executor.unfinished_files()
                    or classify_work_file(facts).action is WorkFileAction.KEEP_IN_USE):
                self._first_files.add(file_id)
                return SettlementOutcome(complete=False)
            try:
                result = await clean_one_work_file(file_id, work)
            except Exception as error:
                raise StateDbFailure(f"取消中间文件的首次结果未可靠保存: {error}") from error
            failed = failed or result.outcome is WorkFileSingleOutcome.FAILED
            self._first_files.discard(file_id)
        return SettlementOutcome(complete=True, failed=failed)

    async def initialize(self, database) -> None:
        """会话锁内固定范围；本方法不开始文件操作。"""
        def read_range(owned):
            after = self.repository.load_cleanup_cursor(owned)
            ceiling = self.repository.max_cleanup_candidate_id(owned)
            with closing(owned.connection.execute("SELECT MAX(id) FROM history_events")) as cursor:
                horizon = int(cursor.fetchone()[0] or 0)
            return after, ceiling, horizon

        after, ceiling, horizon = await database.read(read_range, description="中间文件初始范围读取")
        start = after if after is not None else 0
        self.history.scan = CleanupScan(
            start_after=start, after_id=start, ceiling=ceiling,
            remaining=self.limits.limit_per_run, batch_size=self.limits.batch_size)
        self._discovery_after = horizon
        if self.download_cleanup is not None:
            await self.download_cleanup.initialize(database, horizon=horizon)

    async def flow(self, context: Any) -> None:
        """后台推进已固定的历史责任，立即让出设备调度。"""
        self._stopping = False
        await self._cleanup_downloads(context.database)
        if self._task is not None:
            if not self._task.done():
                return
            # 实际任务完成后消费原监督登记，再接纳下一轮。
            await self.supervisor.drain_required()
            self._raise_task_failure()
        await self._discover(context.database)
        scan = self.history.scan
        if scan is None:
            raise StateDbFailure("中间文件维护尚未固定本次范围")
        if scan.next_query() is None:
            self.history.finished = True
        first_ready = False
        for file_id in tuple(self._first_files):
            if file_id in self.processed:
                self._first_files.discard(file_id)
                continue
            facts = await context.database.read(
                lambda owned: self.repository.load_work_file_state(file_id, owned),
                description="中间文件首次责任状态读取")
            if (file_id not in self.executor.unfinished_files()
                    and classify_work_file(facts).action is not WorkFileAction.KEEP_IN_USE):
                first_ready = True
        if self.history.finished and not first_ready and not self.pending_results:
            return
        self._task = asyncio.create_task(self._maintain(context))
        self.supervisor.handoff(self._token, OwnedTask(
            identity="work-file/history", stage="work_file_cleanup",
            business="本次中间文件维护", resources=("state_db",), pending=self._task))

    async def _maintain(self, context: Any) -> None:
        work = WorkFileContext(
            repository=self.repository, database=context.database, staging=self.staging,
            occurred_at=context.clock.utc_micros(), limits=self.limits,
            processed=self.processed, executor=self.executor, history=self.history,
            pending_results=self.pending_results, stop_requested=lambda: self._stopping)
        await self._discover(context.database)
        for file_id in tuple(sorted(self._first_files)):
            if self._stopping:
                break
            if file_id in self.processed:
                self._first_files.discard(file_id)
                continue
            facts = await context.database.read(
                lambda owned: self.repository.load_work_file_state(file_id, owned),
                description="中间文件首次责任状态读取")
            if (file_id in self.executor.unfinished_files()
                    or classify_work_file(facts).action is WorkFileAction.KEEP_IN_USE):
                continue
            await clean_one_work_file(file_id, work)
            self._first_files.discard(file_id)
        if not self._stopping:
            await clean_work_files(work)

    async def _discover(self, database) -> None:
        """有界读取本次业务变化，只由主协程登记首次责任与继续位置。"""
        def read_ceiling(owned):
            with closing(owned.connection.execute("SELECT MAX(id) FROM history_events")) as cursor:
                return int(cursor.fetchone()[0] or 0)

        ceiling = await database.read(read_ceiling, description="中间文件发现历史上界读取")
        while self._discovery_after < ceiling:
            after_id = self._discovery_after

            def read_page(owned):
                with closing(owned.connection.execute(
                    "SELECT id, transaction_id, event_type, event_version, occurred_at,"
                    " clock_status, change_seq, body_json FROM history_events"
                    " WHERE id > ? AND id <= ? ORDER BY id LIMIT 128",
                    (after_id, ceiling),
                )) as cursor:
                    return cursor.fetchall()

            batch = await database.read(read_page, description="中间文件变化页读取")
            if not batch:
                raise StateDbFailure("本次中间文件责任的可靠历史范围缺失")
            for stored in batch:
                event = decode_event_row(stored)
                for row in event.rows:
                    if not row.after.exists:
                        continue
                    after, before = row.after.values, row.before.values
                    if row.table == "intermediate_files":
                        if (after.get("retention_state") == int(_RETENTION.RELEASABLE)
                                and before.get("retention_state") != int(_RETENTION.RELEASABLE)):
                            self._first_files.add(row.row_id)
                        elif not row.before.exists:
                            await self._add_owned_files(database, "id", row.row_id)
                    elif row.table == "actions" and after.get("status") in _TERMINAL_ACTIONS:
                        if before.get("status") != after["status"]:
                            await self._add_owned_files(database, "owner_action_id", row.row_id)
                    elif row.table == "deliveries" and after.get("status") in _TERMINAL_DELIVERIES:
                        if before.get("status") != after["status"]:
                            await self._add_owned_files(database, "owner_delivery_id", row.row_id)
                self._discovery_after = event.event_id

    async def _add_owned_files(self, database, column: str, identity: int) -> None:
        """沿变更对象关联分批读取文件，在主协程登记首次责任。"""
        after = 0
        while True:
            def read_page(owned):
                with closing(owned.connection.execute(
                    f"SELECT id FROM intermediate_files WHERE {column} = ?"
                    " AND retention_state IN (?, ?) AND id > ? ORDER BY id LIMIT ?",
                    (identity, int(_RETENTION.REQUIRED), int(_RETENTION.RELEASABLE),
                     after, self.limits.batch_size),
                )) as cursor:
                    identities = cursor.fetchall()
                return tuple((int(file_id), self.repository.load_work_file_state(file_id, owned).owner_finished)
                             for (file_id,) in identities)

            batch = await database.read(read_page, description="中间文件归属页读取")
            if not batch:
                break
            for file_id, owner_finished in batch:
                if owner_finished:
                    self._first_files.add(file_id)
                after = file_id

    def _raise_task_failure(self) -> None:
        if self._task is None or not self._task.done():
            return
        try:
            self._task.result()
        except Exception as error:
            raise StateDbFailure(f"中间文件维护未可靠完成: {error}") from error

    def required_settlements(self) -> int:
        return int(self._task is not None and not self._task.done()) + len(self.pending_media_results)

    async def take_over(self, task: OwnedTask) -> None:
        await asyncio.shield(task.pending)

    async def settle(self) -> None:
        """等待已接纳责任；正常关闭重查后仍可推进新业务责任。

        维护收场失败与每份未核实媒体申请的原保存错误都是已知收场
        事实，全部呈现且不互相覆盖；首份媒体原错误保留为异常因果。
        """
        result = await self.supervisor.drain_required()
        files = await self.file_supervisor.drain_required()
        self._raise_task_failure()
        reasons = [failure.error for failure in (*result.failed, *files.failed)]
        reasons.extend(
            f"原媒体申请尚未可靠保存: processing_id={held.command.processing_id},"
            f" stage={held.stage.value}, key={held.key}: {held.save_error}"
            for held in self.pending_media_results.values())
        if not reasons:
            return
        error = StateDbFailure("; ".join(reasons))
        if self.pending_media_results:
            raise error from next(
                iter(self.pending_media_results.values())).save_error
        raise error

    def stop_new_work(self) -> None:
        """致命错误、取消或受限转换停止新增维护，保留实际收场。"""
        self._stopping = True

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
    """保存会话的固定范围与处理记录，后台责任自己关闭独立连接。"""

    def __init__(self, *, staging: Path, limits: WorkFileLimits, pending_media_results=None):
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

    async def resume_media_results(self, owned) -> None:
        """筛选业务前核实会话已取得的媒体申请，不开始设备或工具操作。"""
        from camctl.capture.media_flow import resume_media_saves

        try:
            await resume_media_saves(owned, self.pending_media_results, self.executor)
        except (ConsistencyError, sqlite3.Error) as error:
            raise StateDbFailure(f"原媒体申请未可靠保存: {error}") from error

    async def first_cleanup(self, file_ids: tuple[int, ...], *, owned, occurred_at: int):
        """采用本次必要文件的首次结果，不使用历史扫描范围或额度。"""
        from camctl.cancellation.ports import SettlementOutcome

        work = WorkFileContext(
            repository=self.repository, owned=owned, staging=self.staging,
            occurred_at=occurred_at, limits=self.limits, processed=self.processed,
            executor=self.executor, history=self.history, pending_results=self.pending_results)
        failed = False
        for file_id in file_ids:
            facts = self.repository.load_work_file_state(file_id, owned)
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

    def initialize(self, owned) -> None:
        """会话锁内固定范围；本方法不开始文件操作。"""
        cursor = self.repository.load_cleanup_cursor(owned)
        start = cursor if cursor is not None else 0
        self.history.scan = CleanupScan(
            start_after=start, after_id=start,
            ceiling=self.repository.max_cleanup_candidate_id(owned),
            remaining=self.limits.limit_per_run,
            batch_size=self.limits.batch_size)
        with closing(owned.connection.execute("SELECT MAX(id) FROM history_events")) as cursor:
            self._discovery_after = int(cursor.fetchone()[0] or 0)

    async def flow(self, context: Any) -> None:
        """后台推进已固定的历史责任，立即让出设备调度。"""
        # 正常关闭重查可能发现新工作；继续原会话时恢复新增责任，
        # 历史范围和已经消耗的额度仍由原 history 保存。
        self._stopping = False
        if self._task is not None:
            if not self._task.done():
                return
            # 实际任务完成不等于监督登记已经消费；重用身份之前
            # 必须沿原拥有者完成交付，不能丢弃旧登记。
            await self.supervisor.drain_required()
            self._raise_task_failure()
        owned = context.open_connection()
        try:
            self._discover(owned)
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
                facts = self.repository.load_work_file_state(file_id, owned)
                if (file_id not in self.executor.unfinished_files()
                        and classify_work_file(facts).action is not WorkFileAction.KEEP_IN_USE):
                    first_ready = True
            if self.history.finished and not first_ready and not self.pending_results:
                return
        finally:
            owned.connection.close()
        self._task = asyncio.create_task(self._maintain(context))
        self.supervisor.handoff(self._token, OwnedTask(
            identity="work-file/history", stage="work_file_cleanup",
            business="本次中间文件维护", resources=("state_db",), pending=self._task))

    async def _maintain(self, context: Any) -> None:
        owned = context.open_connection()
        try:
            work = WorkFileContext(
                repository=self.repository, owned=owned, staging=self.staging,
                occurred_at=context.clock.utc_micros(), limits=self.limits,
                processed=self.processed, executor=self.executor, history=self.history,
                pending_results=self.pending_results,
                stop_requested=lambda: self._stopping)
            self._discover(owned)
            for file_id in tuple(sorted(self._first_files)):
                if self._stopping:
                    break
                if file_id in self.processed:
                    self._first_files.discard(file_id)
                    continue
                facts = self.repository.load_work_file_state(file_id, owned)
                # 本次必要责任尚被使用时保留身份，不把一次提前观察
                # 当作已取得的首次清理结果。
                if (file_id in self.executor.unfinished_files()
                        or classify_work_file(facts).action is WorkFileAction.KEEP_IN_USE):
                    continue
                await clean_one_work_file(file_id, work)
                self._first_files.discard(file_id)
            if not self._stopping:
                await clean_work_files(work)
        finally:
            owned.connection.close()

    def _discover(self, owned) -> None:
        """有界读取本次业务变化，只由用途或终态变化产生首次责任。"""
        with closing(owned.connection.execute("SELECT MAX(id) FROM history_events")) as cursor:
            ceiling = int(cursor.fetchone()[0] or 0)
        while self._discovery_after < ceiling:
            with closing(owned.connection.execute(
                "SELECT id, transaction_id, event_type, event_version, occurred_at,"
                " clock_status, change_seq, body_json FROM history_events"
                " WHERE id > ? AND id <= ? ORDER BY id LIMIT 128",
                (self._discovery_after, ceiling),
            )) as cursor:
                batch = cursor.fetchall()
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
                            self._add_owned_files(owned, "id", row.row_id)
                    elif row.table == "actions" and after.get("status") in _TERMINAL_ACTIONS:
                        if before.get("status") != after["status"]:
                            self._add_owned_files(owned, "owner_action_id", row.row_id)
                    elif row.table == "deliveries" and after.get("status") in _TERMINAL_DELIVERIES:
                        if before.get("status") != after["status"]:
                            self._add_owned_files(owned, "owner_delivery_id", row.row_id)
                self._discovery_after = event.event_id

    def _add_owned_files(self, owned, column: str, identity: int) -> None:
        """沿变更对象关联分批读取文件，不以对象末事件推断用途。"""
        after = 0
        while True:
            with closing(owned.connection.execute(
                f"SELECT id FROM intermediate_files WHERE {column} = ?"
                " AND retention_state IN (?, ?) AND id > ? ORDER BY id LIMIT ?",
                (identity, int(_RETENTION.REQUIRED), int(_RETENTION.RELEASABLE),
                 after, self.limits.batch_size),
            )) as cursor:
                batch = cursor.fetchall()
            if not batch:
                break
            for (file_id,) in batch:
                facts = self.repository.load_work_file_state(file_id, owned)
                if facts.owner_finished:
                    self._first_files.add(int(file_id))
                after = int(file_id)

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
        """等待已接纳责任；正常关闭重查后仍可推进新业务责任。"""
        result = await self.supervisor.drain_required()
        files = await self.file_supervisor.drain_required()
        self._raise_task_failure()
        if result.failed or files.failed:
            raise StateDbFailure("; ".join(
                failure.error for failure in (*result.failed, *files.failed)))
        if self.pending_media_results:
            pending = next(iter(self.pending_media_results.values()))
            raise StateDbFailure(
                f"原媒体申请尚未可靠保存: processing_id={pending.command.processing_id},"
                f" stage={pending.stage.value}, key={pending.key}: {pending.save_error}"
            ) from pending.save_error

    def stop_new_work(self) -> None:
        """致命错误、取消或受限转换停止新增维护，保留实际收场。"""
        self._stopping = True

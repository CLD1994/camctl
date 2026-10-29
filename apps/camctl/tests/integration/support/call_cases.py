"""普通调用保存专项的正式工厂、设备输入和实际 SQLite 故障。"""

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest_asyncio

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import AcceptanceContext, CommandMode, accept_input
from camctl.contracts.values import new_operation_key
from camctl.devices.ports import DeviceCallResult
from camctl.host_files.tasks import FileTaskExecutor
from camctl.operations.attempts import AttemptConfig
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.scheduling import SchedulingRepository, ObserveWindowRequest, StartActionRequest
from camctl.persistence.runtime import open_existing, DbOpenMode, DbConfig
from camctl.session.supervision import Supervisor

from .capture import capture_assembly
from .database import create_test_database
from .result_cases import CaseClock


class SavedCallBoundary(Exception):
    """实际调用结果已可靠保存，在后续业务之前观察该边界。"""


class CallPort:
    """按原请求票据生成真实设备结果，脚本与调用记录由测试拥有。"""

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls, self.actual = [], []
        self.returned = None

    def result(self, request):
        self.calls.append(request)
        actual = self.outcome(request) if callable(self.outcome) else self.outcome
        self.actual.append(actual)
        if self.returned is not None:
            self.returned()
        return DeviceCallResult.from_outcome(actual)

    async def control(self, request):
        return self.result(request)

    async def stop(self, request):
        return self.result(request)

    async def query_state(self, request):
        return self.result(request)


class CallStorageFault:
    """在实际设备返回后的 SQLite 授权边界注入故障，不识别SQL文本。"""

    def __init__(self):
        self.target = None
        self.mode = None
        self.armed = False
        self.enabled = True
        self.injected = False
        self.result_keys, self.rolled_back_images = [], []

    def arm(self, target, mode, *, responsibility=None):
        self.target, self.mode, self.armed, self.enabled = target, mode, True, True
        self.calls_before = len(target.calls)
        self.responsibility = responsibility
        self.injected = False
        self.result_keys.clear()
        self.rolled_back_images.clear()

    def connection(self, real):
        owner = self
        path = real.execute("PRAGMA database_list").fetchone()[2]
        class Connection:
            def close(self):
                unfinished = real.in_transaction
                real.close()
                if unfinished and owner.mode == "commit_before":
                    # UNKNOWN worker关闭旧连接时由SQLite实际回滚；在关闭后核对磁盘前态。
                    from contextlib import closing
                    with closing(sqlite3.connect("file:" + path + "?mode=ro", uri=True)) as observer:
                        owner.rolled_back_images.append(tuple(observer.iterdump()))
            def __getattr__(self, name):
                return getattr(real, name)
            def execute(self, statement, parameters=()):
                if owner.target is None or len(owner.target.calls) <= owner.calls_before:
                    return real.execute(statement, parameters)
                if (owner.responsibility is not None
                        and owner.target.calls[-1].ticket.responsibility_key != owner.responsibility):
                    return real.execute(statement, parameters)
                commit, rollback, inserted = False, False, False
                def authorize(operation, table, _column, _database, _source):
                    nonlocal commit, rollback, inserted
                    if operation == sqlite3.SQLITE_INSERT and table == "history_transactions":
                        inserted = True
                    if operation == sqlite3.SQLITE_TRANSACTION:
                        commit |= table == "COMMIT"
                        rollback |= table == "ROLLBACK"
                    fail = owner.enabled and (
                        (owner.armed and owner.mode == "read" and operation == sqlite3.SQLITE_READ and table == "operation_runs")
                        or (owner.mode == "action_read" and operation == sqlite3.SQLITE_READ and table == "actions")
                        or (owner.armed and owner.mode == "projection" and operation == sqlite3.SQLITE_UPDATE and table == "operation_attempts")
                        or (owner.mode == "persistent" and operation == sqlite3.SQLITE_TRANSACTION and table == "COMMIT")
                        or (owner.armed and owner.mode == "commit_before" and operation == sqlite3.SQLITE_TRANSACTION and table == "COMMIT"))
                    if fail:
                        owner.armed = False
                        owner.injected = True
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                real.set_authorizer(authorize)
                try:
                    result = real.execute(statement, parameters)
                    if inserted:
                        row = real.execute("SELECT operation_key FROM history_transactions ORDER BY id DESC LIMIT 1").fetchone()
                        if row is not None:
                            owner.result_keys.append(row[0])
                    if rollback:
                        owner.rolled_back_images.append(tuple(real.iterdump()))
                    if commit and owner.enabled and owner.armed and owner.mode == "commit_after":
                        owner.armed = False
                        owner.injected = True
                        raise sqlite3.OperationalError("原调用结果实际提交后的回执丢失")
                    return result
                except sqlite3.Error:
                    if commit and owner.mode == "commit_before" and not real.in_transaction:
                        owner.rolled_back_images.append(tuple(real.iterdump()))
                    raise
                finally:
                    real.set_authorizer(None)
        return Connection()


class CallCase:
    def __init__(self):
        self.fault = CallStorageFault()
        self.supervisor = Supervisor()
        self.database = None
        self.owned = None
        self.saves = []
        self.boundary = None

    async def world(self, tmp_path, *, evidence, control, stop, query, now):
        from ..capture.result_consumer_fixtures import ResultCatalog
        from ..capture.test_capture_contract import ResultsDouble
        self.path = tmp_path / "state.db"
        create_test_database(self.path)
        self.owned = open_existing(self.path, DbOpenMode.EXISTING_RW, DbConfig())
        def connect():
            real = open_existing(self.path, DbOpenMode.EXISTING_RW, DbConfig())
            return replace(real, connection=self.fault.connection(real.connection))
        self.database = await BusinessDatabase.open(connect)
        self.clock = CaseClock(now, 7_000_000_000)
        self.control, self.stop, self.query = control, stop, query
        self.options = dict(driver=control, evidence=evidence, stopper=stop, query=query,
            results=ResultsDouble({}), staging=Path(self.database.metadata.staging_path),
            wall_us=self.clock.wall_us, monotonic_ns=self.clock.monotonic_ns,
            start_config=AttemptConfig(3, Decimal("0.75"), Decimal("1")),
            stop_config=AttemptConfig(1, Decimal("0.125"), Decimal("1")),
            query_config=AttemptConfig(2, Decimal("0.5"), Decimal("1")),
            residual_config=AttemptConfig(3, Decimal("10"), Decimal("3")),
            file_executor=FileTaskExecutor(self.supervisor))
        self._factory()
        action = await self.accept(tmp_path, occurred_at=now)
        started = SchedulingRepository().start_action(StartActionRequest(action, now, now),
            new_operation_key(), self.owned)
        assert started.kind is DbOutcomeKind.COMPLETED, started.error
        self.action_id, self.activity_id = action, started.value.activity_id
        return await self.view()

    def _factory(self):
        self.factory = capture_assembly(**self.options)

    async def view(self):
        # 仓储故障替身完整调用正式命令；通过装配构造协作者，不搬运runtime。
        from unittest.mock import patch
        case = self
        class Capture(CaptureRepository):
            def finish_start_result(self, finish, observation, key, owned, **options):
                case.saves.append((finish, key))
                result = super().finish_start_result(finish, observation, key, owned, **options)
                return self.after(finish.ticket.operation, result, finish.ticket.responsibility_key)
            def finish_capture_stop(self, finish, key, owned):
                case.saves.append((finish, key))
                return self.after("stop", super().finish_capture_stop(finish, key, owned))
            @staticmethod
            def after(operation, result, responsibility=None):
                if (result.kind is DbOutcomeKind.COMPLETED
                        and case.boundary in (operation, responsibility)
                        and case.boundary is not None):
                    return replace(result, handoff_error=SavedCallBoundary())
                return result
        class Operations(OperationRepository):
            def finish_attempt(self, finish, key, owned):
                case.saves.append((finish, key))
                result = super().finish_attempt(finish, key, owned)
                return Capture.after(finish.ticket.operation, result, finish.ticket.responsibility_key)
        with patch("camctl.bootstrap.capture_assembly.CaptureRepository", Capture), patch(
                "camctl.bootstrap.capture_assembly.OperationRepository", Operations):
            runtime = await self.factory(self.database, "cam-1")
        assert runtime is not None
        return runtime

    async def accept(self, tmp_path, *, occurred_at):
        from ..capture.result_consumer_fixtures import ResultCatalog
        self._requests = getattr(self, "_requests", 40) + 1
        instant = datetime.fromtimestamp(occurred_at // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        result = await accept_input(ParsedInput(str(tmp_path / f"call-{self._requests}.json"), {
            "request_id": str(self._requests), "created_at": instant, "name": "普通调用结果",
            "actions": [{"name": "录像", "type": "camera_record", "device_id": "cam-1",
                "scheduled_at": instant, "params": {"type": "video"},
                "policy": {"max_delay_ms": 60000}}]}),
            AcceptanceContext(CommandMode.RUN, ResultCatalog(), AcceptanceRepository(),
                clock=SimpleNamespace(utc_micros=lambda: occurred_at)), new_operation_key(), self.database)
        assert result.plan_id is not None, result
        return self.owned.connection.execute("SELECT id FROM actions WHERE plan_id=?", (result.plan_id,)).fetchone()[0]

    async def restart(self, *, cold=False):
        await self.drain()
        await self.database.close()
        def connect():
            real = open_existing(self.path, DbOpenMode.EXISTING_RW, DbConfig())
            return replace(real, connection=self.fault.connection(real.connection))
        self.database = await BusinessDatabase.open(connect)
        if cold:
            self._factory()
        return await self.view()

    async def drain(self):
        result = await self.supervisor.drain_required()
        errors = [failure.exception or RuntimeError(failure.error) for failure in result.failed]
        if errors:
            raise BaseExceptionGroup("普通调用专项文件责任收场失败", errors)

    async def close(self):
        errors = []
        try:
            await self.drain()
        except BaseException as error:
            errors.append(error)
        try:
            if self.database is not None:
                await self.database.close()
        except BaseException as error:
            errors.append(error)
        try:
            if self.owned is not None:
                self.owned.connection.close()
        except BaseException as error:
            errors.append(error)
        if errors:
            raise BaseExceptionGroup("普通调用专项实际资源收场失败", errors)


@pytest_asyncio.fixture
async def call_case(capture_resources):
    case = CallCase()
    try:
        yield case
    finally:
        await case.close()

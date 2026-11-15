"""结果页专项的正式尝试入口和 SQLite 故障边界。"""

import sqlite3

from camctl.contracts.values import new_operation_key
from camctl.operations.attempts import AttemptIntent, AttemptTarget, OperationKind
from camctl.operations.models import AttemptTicket
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.operations import OperationRepository


def begin_result_attempt(owned, action_id, config, occurred_at):
    """经正式结果意图事务取得票据，供页面存储专项准备前态。"""
    activity_id, = owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
    saved = OperationRepository().begin_attempt(AttemptIntent(
        operation="result", action_id=action_id, kind=OperationKind.CHECK_CAPTURE_RESULTS,
        target=AttemptTarget(activity_id=activity_id), query_purpose=None,
        config=config, occurred_at=occurred_at), new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    assert saved.value.ticket is not None, saved.value
    return saved.value.ticket


def current_result_ticket(owned, action_id):
    row = owned.connection.execute(
        "SELECT r.id,t.attempt_no,r.activity_id,r.responsibility_key"
        " FROM operation_runs r JOIN operation_attempts t ON t.run_id=r.id"
        " WHERE r.action_id=? AND r.kind=7 ORDER BY t.id DESC LIMIT 1", (action_id,)).fetchone()
    assert row is not None, "正式处理器应建立结果核实意图"
    run_id, attempt_no, activity_id, responsibility = row
    return AttemptTicket(attempt_no, "result", str(activity_id), responsibility, run_id)


def saved_result_pages(owned, action_id):
    return CaptureRepository().read_result_pages(
        current_result_ticket(owned, action_id), None, 128, owned).items


class ResultPageFault:
    """在实际文件发现写入或原 COMMIT 边界注入一次存储故障。"""

    def __init__(self, connection, mode):
        self.connection, self.mode = connection, mode
        self.armed = True
        self.page_started = False

    def execute(self, sql, parameters=()):
        commit_boundary = False

        def authorize(operation, table, _column, _database, _source):
            nonlocal commit_boundary
            if operation == sqlite3.SQLITE_INSERT and table == "device_files":
                self.page_started = True
                if self.armed and self.mode == "projection":
                    self.armed = False
                    return sqlite3.SQLITE_DENY
            if (operation == sqlite3.SQLITE_TRANSACTION and table == "COMMIT"
                    and self.page_started and self.armed):
                commit_boundary = True
                if self.mode == "commit_before":
                    self.armed = False
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.connection.set_authorizer(authorize)
        try:
            cursor = self.connection.execute(sql, parameters)
            if commit_boundary and self.armed and self.mode == "commit_after":
                self.armed = False
                raise sqlite3.OperationalError("结果页实际提交后的回执故障")
            return cursor
        finally:
            self.connection.set_authorizer(None)

    def __getattr__(self, name):
        return getattr(self.connection, name)


from contextvars import ContextVar
from pathlib import Path
from decimal import Decimal
import pytest_asyncio

from camctl.host_files.tasks import FileTaskExecutor
from camctl.session.supervision import Supervisor
from .capture import capture_assembly


_active_case = ContextVar("result_case", default=None)


class CaseClock:
    """装配前注入的受控时钟；读数属于测试输入。"""

    def __init__(self, wall, monotonic):
        self.wall = wall
        self.monotonic = monotonic
        self.wall_error = None

    def wall_us(self):
        if self.wall_error is not None:
            raise AssertionError(self.wall_error)
        return self.wall

    def monotonic_ns(self):
        return self.monotonic


class ResultPort:
    """结果端口替身，脚本变化不需要改写运行时的装配。"""

    def __init__(self, delegate):
        self.delegate = delegate

    async def list_page(self, *args, **kwargs):
        return await self.delegate.list_page(*args, **kwargs)

    async def list_round(self, *args, **kwargs):
        return await self.delegate.list_round(*args, **kwargs)

    async def list_files(self, *args, **kwargs):
        return await self.delegate.list_files(*args, **kwargs)


class ResultCase:
    """持有正式会话工厂及其配置，区分同一保存责任重入和冷恢复。"""

    def __init__(self, session):
        from ..capture.result_consumer_fixtures import _NOW
        self.session = session
        self.factory = None
        self.clock = CaseClock(_NOW, 5_000_000_000)
        self.supervisor = None
        self.diagnostics = []
        self.assembly_options = {}

    async def _runtime(self, owned, *, driver):
        from ..capture.result_consumer_fixtures import RESULT_EVIDENCE, returned
        from ..capture.test_capture_contract import ResultsDouble
        from ..capture.test_baseline_prepare import Directory
        from camctl.devices.directory import DirectoryRead
        from camctl.devices.evidence import RESULT_PAGE_CONTRACT
        from camctl.contracts.pages import Page
        from camctl.operations.attempts import AttemptConfig
        from camctl.devices.ports import StopDriver, DeviceCallResult
        from unittest.mock import create_autospec

        database = await self.session.database(owned)
        self.driver = driver
        self.stopper = create_autospec(StopDriver, instance=True)
        activity_id, = owned.connection.execute("SELECT id FROM device_activities ORDER BY id DESC LIMIT 1").fetchone()
        self.stopper.stop.return_value = DeviceCallResult.from_outcome(returned("stop", "stop_confirmed", activity_id))
        self.results = ResultPort(ResultsDouble({}))
        evidence = (RESULT_EVIDENCE.with_contract(RESULT_PAGE_CONTRACT)
                    if self.result_page_contract else RESULT_EVIDENCE)
        for contract in self.contracts:
            evidence = evidence.with_contract(contract)
        self.evidence = evidence
        self.options = dict(driver=driver, evidence=evidence,
            results=self.results, stopper=self.stopper,
            staging=Path(database.metadata.staging_path),
            wall_us=self.clock.wall_us, monotonic_ns=self.clock.monotonic_ns,
            check_config=AttemptConfig(3, Decimal("1.25"), Decimal("2")),
            directory=Directory([lambda request: DirectoryRead(None, None, Page((), None))]),
            on_recovery_diagnostic=self.diagnostics.append)
        self.options.update(self.assembly_options)
        self._build_factory()
        runtime = await self.factory(database, "cam-1")
        assert runtime is not None
        return runtime

    def _build_factory(self):
        from types import SimpleNamespace
        from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
        from camctl.devices.ports import DriverDeclaration
        self.supervisor = Supervisor()
        options = dict(self.options)
        recovery_operations = set()
        if options.pop("result_recovery", False):
            recovery_operations.add("result")
        if options.pop("control_recovery", False):
            recovery_operations.add("control")
        declaration = DriverDeclaration(control_supported=True, stop_supported=True,
            query_supported=False, result_supported=False, read_supported=False,
            digest_supported=False, delete_supported=False, directory_supported=True,
            adb_foreground_recovery_operations=frozenset(recovery_operations))
        ports = SimpleNamespace(control=self.driver.control, stop=self.stopper.stop,
            read_directory=options["directory"].read_directory)
        options["drivers"] = DriverRegistry(tuple(DriverEntry(identity, ports,
            declaration, self.evidence, DriverStatus.SOFTWARE_CONTRACT_VERIFIED)
            for identity in ("camctl-adb", "other")))
        if options.get("devices") == {}:
            options.pop("check_config", None)
        options["file_executor"] = FileTaskExecutor(self.supervisor)
        self.factory = capture_assembly(**options)

    async def world(self, tmp_path, consumer, *, prepare_start_only=False,
                    assembly_options=None, evidence_contracts=(), result_page_contract=True, **options):
        from ..capture.result_consumer_fixtures import consumer_world, _prepare_through_handler, _NOW
        from camctl.capture.handlers import capture_handler
        from camctl.capture.processing import CheckBasis, CheckDecisionChoice, CheckDecisionSave, CheckReason
        from camctl.persistence.repositories.cancellation import CancellationRepository
        from camctl.cancellation.models import (StartCancelAction, FixCancelTargets, FixedCancelSet,
            FixedTarget, SelectionBasis, CancellationEffect, ApplyCancelTarget, CancelApplyMode)

        self.assembly_options = {} if assembly_options is None else dict(assembly_options)
        self.contracts = evidence_contracts
        self.result_page_contract = result_page_contract
        start = options.pop("start", True)
        elapsed = options.pop("record_control_elapsed_ns", None)
        world = await consumer_world(tmp_path, consumer, start=False,
            runtime_factory=self._runtime, **options)
        owned, runtime, action_id, handler = world
        try:
            if not start:
                return world
            if prepare_start_only or consumer == "photo":
                await _prepare_through_handler(runtime, action_id, handler, "start")
            else:
                await capture_handler(handler)(action_id, runtime)
            if prepare_start_only:
                self.clock.wall = _NOW + 700_000_000
                return world
            if consumer in ("record", "winddown"):
                self.clock.wall = _NOW + 60_000_000
                self.clock.monotonic = 5_000_000_000 + (60_000_000_000 if elapsed is None else elapsed)
                await _prepare_through_handler(runtime, action_id, handler, "conclude")
            if handler == "camera_record":
                processing_id, = owned.connection.execute(
                    "SELECT id FROM recording_processing WHERE action_id=?", (action_id,)).fetchone()
                receipt = CaptureRepository().save_check_decision(CheckDecisionSave(processing_id,
                    CheckDecisionChoice.NOT_NEEDED, CheckBasis(CheckReason.CONTINUOUS_CONTROL_COMPLETE,
                        60000, control_elapsed_ns=60_000_000_000), self.clock.wall), new_operation_key(), owned)
                assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            if consumer == "cancel":
                repository = CancellationRepository()
                cancel_id = action_id + 1
                started = repository.start_cancel_action(StartCancelAction(cancel_id, self.clock.wall), new_operation_key(), owned)
                assert started.kind is DbOutcomeKind.COMPLETED, started.error
                fixed = repository.fix_cancel_targets(FixCancelTargets(cancel_id, FixedCancelSet((FixedTarget(
                    action_id, SelectionBasis.DIRECT, CancellationEffect.NOT_APPLIED),)), self.clock.wall), new_operation_key(), owned)
                assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
                item_id, = fixed.value.item_ids
                applied = repository.apply_cancel_target(ApplyCancelTarget(item_id, CancelApplyMode.WITH_STOP,
                    self.clock.wall), new_operation_key(), owned)
                assert applied.kind is DbOutcomeKind.COMPLETED, applied.error
                self.clock.wall = _NOW + 60_000_000
                self.clock.monotonic = 65_000_000_000
                await _prepare_through_handler(runtime, action_id, handler, "stop")
            if consumer == "timelapse":
                # 新工厂代表前次 START 已保存后的新会话，不继承旧单调等待锚点。
                self.clock.wall = _NOW + 700_000_000
                runtime = await self.reopen(owned, cold=True)
            return owned, runtime, action_id, handler
        except BaseException:
            from ..capture.task_cleanup import close_observer
            close_observer(owned)
            raise

    async def reopen(self, owned, *, cold=False, assembly_options=None):
        # 自有文件执行器必须在旧 BusinessDatabase 关闭之前实际收场。
        await self._drain()
        database = await self.session.restart_database(owned)
        if assembly_options is not None:
            assert cold, "配置变化须重新建立正式会话工厂"
            self.options.update(assembly_options)
        if cold:
            self._build_factory()
        resumed = await self.factory(database, "cam-1")
        assert resumed is not None
        return resumed

    async def _drain(self):
        if self.supervisor is None:
            return
        result = await self.supervisor.drain_required()
        errors = [failure.exception or RuntimeError(failure.error) for failure in result.failed]
        if errors:
            raise BaseExceptionGroup("结果页测试实际文件责任收场失败", errors)

    async def close(self):
        await self._drain()


def result_case_for():
    case = _active_case.get()
    if case is None:
        raise RuntimeError("结果运行时测试须使用 result_case 资源作用域")
    return case


@pytest_asyncio.fixture(autouse=True)
async def result_case(capture_resources):
    case = ResultCase(capture_resources)
    token = _active_case.set(case)
    try:
        try:
            yield case
        finally:
            await case.close()
    finally:
        _active_case.reset(token)

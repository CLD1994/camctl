"""拍摄装配专项的正式工厂、真实数据库和默认流程观察。"""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sqlite3

import pytest_asyncio

from camctl.acceptance.service import CommandMode
from camctl.bootstrap import capture_assembly as assembly, lifecycle
from camctl.contracts.values import ConsistencyError
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.ports import DriverDeclaration, DeviceCallResult
from camctl.devices.evidence import RESULT_PAGE_CONTRACT
from camctl.host_files.tasks import FileTaskExecutor
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.session.outcome import SessionOutcome
from camctl.session.supervision import Supervisor

from .capture import capture_assembly
from .result_cases import CaseClock


class SavedBoundary(Exception):
    """正式仓储已处理原保存申请，专项在后续业务之前观察。"""


class StopPort:
    def __init__(self, outcome):
        self.outcome, self.calls = outcome, []

    async def stop(self, request):
        self.calls.append(request)
        value = self.outcome(request) if callable(self.outcome) else self.outcome
        return DeviceCallResult.from_outcome(value)


class WorkerFault:
    """在实际 SQLite 表权限或事务边界注入，不依赖 SQL 文本格式。"""

    enabled = False
    table = operation = mode = None
    hits = 0

    def connection(self, real):
        owner = self

        class Connection:
            def __getattr__(self, name):
                return getattr(real, name)

            def execute(self, sql, parameters=()):
                commit = False

                def authorize(operation, table, _column, _database, _source):
                    nonlocal commit
                    commit |= operation == sqlite3.SQLITE_TRANSACTION and table == "COMMIT"
                    denied = owner.enabled and (
                        (owner.table == table and owner.operation == operation)
                        or (owner.mode == "commit_before" and operation == sqlite3.SQLITE_TRANSACTION
                            and table == "COMMIT"))
                    if denied:
                        owner.hits += 1
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                real.set_authorizer(authorize)
                try:
                    value = real.execute(sql, parameters)
                    if commit and owner.enabled and owner.mode == "commit_after":
                        owner.hits += 1
                        raise sqlite3.OperationalError("原实际提交后的回执丢失")
                    return value
                finally:
                    real.set_authorizer(None)

        return Connection()


class BootstrapCaptureCase:
    def __init__(self, resources):
        from ..capture.test_capture_contract import _NOW
        self.resources = resources
        self.clock = CaseClock(_NOW, 5_000_000_000)
        self.supervisor = Supervisor()
        self.executors = []
        self.fault = WorkerFault()
        self.saves = []
        self.boundary = None
        self.before_save = None
        self.after_save = None
        self.database = self.deps = self.owned = None

    async def world(self, home, consumer="record", *, control=None, results=None,
                    directory=None, catalog=None, options=None, default=False,
                    device_options=None, max_delay_ms=60000):
        from ..capture.result_consumer_fixtures import (
            CASES, RESULT_EVIDENCE, returned, ResultCatalog)
        from ..capture.test_capture_contract import ResultsDouble
        from ..capture.test_baseline_prepare import Directory
        from camctl.contracts.pages import Page
        from camctl.devices.directory import DirectoryRead
        from ..bootstrap.test_media_assembly import _config

        self.home, self.consumer = Path(home), consumer
        self.results = ResultsDouble({}) if results is None else results
        self.directory = directory or Directory([lambda request: DirectoryRead(None, None, Page((), None))])
        self.catalog = ResultCatalog() if catalog is None else catalog
        self.config = _config(home)
        self.driver_id = self.catalog.driver_id("cam-1")
        self.config = replace(self.config, devices={"cam-1": {
            "kind": "camera", "driver": self.driver_id,
            **({} if device_options is None else device_options)}})
        self.extra = {} if options is None else dict(options)
        self.evidence = RESULT_EVIDENCE.with_contract(RESULT_PAGE_CONTRACT)
        self.default = default

        async def runtime_factory(owned, *, driver):
            self.owned = owned
            self.path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
            self.activity_id = owned.connection.execute("SELECT id FROM device_activities ORDER BY id DESC LIMIT 1").fetchone()[0]
            if control is not None:
                from camctl.devices.ports import DeviceCallResult
                driver.control.return_value = DeviceCallResult.from_outcome(replace(control,
                    observations=tuple(replace(item, data={**item.data, "activity_id": str(self.activity_id)})
                        for item in control.observations)))
            self.driver = driver
            self.stopper = StopPort(lambda request: returned("stop", "stop_confirmed", request.ticket.target_id))
            await self.open_database()
            self.options = dict(driver=driver, stopper=self.stopper, evidence=self.evidence,
                driver_id=self.driver_id, devices=self.config.devices,
                results=self.results, directory=self.directory, staging=Path(self.database.metadata.staging_path),
                wall_us=self.clock.wall_us, monotonic_ns=self.clock.monotonic_ns,
                file_executor=FileTaskExecutor(self.supervisor))
            self.options.update(self.extra)
            if default:
                await self.assemble_default()
            else:
                self.factory = capture_assembly(**self.options)
            return await self.view()

        from camctl.acceptance.input import ParsedInput
        from camctl.acceptance.service import AcceptanceContext, accept_input
        from camctl.contracts.values import new_operation_key
        from camctl.devices.ports import ControlDriver, DeviceCallResult
        from camctl.persistence.initialization import initialize_state
        from camctl.persistence.repositories.acceptance import AcceptanceRepository
        from camctl.persistence.repositories.scheduling import SchedulingRepository, ObserveWindowRequest, StartActionRequest
        from unittest.mock import create_autospec
        self.home.mkdir(parents=True, exist_ok=True)
        initialize_state(self.config, Path(self.config.paths.state_db))
        self.owned = owned = open_existing(Path(self.config.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
        self.path = Path(self.config.paths.state_db)
        await self.open_database()
        self.handler, parameter, _operation, observation = CASES[consumer]
        instant = datetime.fromtimestamp(self.clock.wall // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        accepted = await accept_input(ParsedInput(str(self.home / "capture.json"), {
            "request_id": "1", "created_at": instant, "name": "拍摄装配协作",
            "actions": [{"name": "拍摄", "type": self.handler, "device_id": "cam-1",
                "scheduled_at": instant, "params": {"type": parameter},
                "policy": {"max_delay_ms": max_delay_ms}}]}),
            AcceptanceContext(CommandMode.RUN, self.catalog, AcceptanceRepository(),
                clock=SimpleNamespace(utc_micros=self.clock.wall_us)), new_operation_key(), self.database)
        assert accepted.plan_id is not None, accepted
        self.action_id = owned.connection.execute("SELECT id FROM actions").fetchone()[0]
        scheduling = SchedulingRepository()
        for method, request in ((scheduling.observe_window, ObserveWindowRequest(self.action_id, self.clock.wall, self.clock.wall)),
                                (scheduling.start_action, StartActionRequest(self.action_id, self.clock.wall, self.clock.wall))):
            key = new_operation_key()
            receipt = await self.database.write(lambda current: method(request, key, current), key=key)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        self.activity_id = owned.connection.execute("SELECT id FROM device_activities").fetchone()[0]
        driver = create_autospec(ControlDriver, instance=True)
        driver.control.return_value = DeviceCallResult.from_outcome(returned("operation", observation, self.activity_id))
        # 本方法已经拥有真实 worker，工厂回调复用它。
        return await runtime_factory(owned, driver=driver)

    async def open_database(self):
        if self.database is None:
            self.database = await self.new_database()

    async def new_database(self):
        def connect():
            owned = open_existing(self.path, DbOpenMode.EXISTING_RW, DbConfig())
            return replace(owned, connection=self.fault.connection(owned.connection))
        return await BusinessDatabase.open(connect)

    async def assemble_default(self):
        """观察 execute_command 实际生成的 SessionContext 与三份正式工厂。

        这里隔离会话循环，只核对默认装配；业务步骤随后经真实工厂和
        正式 flow/handler 执行，不将本步骤称为完整会话验收。
        """
        self.factories = []
        self.executors = []
        original = assembly.session_capture_assembly
        ports = SimpleNamespace(control=self.driver.control, stop=self.stopper.stop,
            read_directory=self.directory.read_directory)
        entry = DriverEntry(self.driver_id, ports, DriverDeclaration(
            control_supported=True, stop_supported=True, query_supported=False,
            result_supported=False, read_supported=False, digest_supported=False,
            delete_supported=False, directory_supported=True), self.evidence,
            DriverStatus.SOFTWARE_CONTRACT_VERIFIED)

        def observed(**options):
            options.update(results=self.results, wall_us=self.clock.wall_us,
                monotonic_ns=self.clock.monotonic_ns)
            executor = options.get("file_executor")
            if executor is not None and not any(item is executor for item in self.executors):
                self.executors.append(executor)
            factory = original(**options)
            self.factories.append(factory)
            return factory

        async def session(context, source):
            assert source is None
            self.context = context
            return SessionOutcome(succeeded=True)

        from camctl.devices.drivers import runtime as registration
        with patch.object(registration, "current_registry", return_value=DriverRegistry((entry,))), patch.object(
                assembly, "session_capture_assembly", observed), patch.object(lifecycle, "run_session", session):
            self.deps = lifecycle.build_runtime(CommandMode.RUN, self.config, catalog=self.catalog)
            assert self.deps.startup_error is None, self.deps.startup_error
            result = await lifecycle.execute_command(self.deps, None)
            assert result.succeeded, result
        assert len(self.factories) == 3
        self.factory = self.factories[0]

    async def view(self, factory=None, *, database=None):
        case = self

        def observed(name, method):
            def call(repository, request, *args, **options):
                case.saves.append((name, request, args[0]))
                result = (None if case.before_save is None else
                    case.before_save(name, request, args[0]))
                if result is None:
                    result = method(repository, request, *args, **options)
                if case.after_save is not None:
                    result = case.after_save(name, request, args[0], result)
                if name == case.boundary and result.kind is DbOutcomeKind.COMPLETED:
                    return replace(result, handoff_error=SavedBoundary(name))
                return result
            return call

        capture = type("ObservedCapture", (CaptureRepository,), {name: observed(name, getattr(CaptureRepository, name))
            for name in ("finish_start_result", "finish_capture_stop", "conclude_activity", "append_baseline",
                         "fix_baseline",
                         "save_baseline_read_intent", "save_baseline_read_return", "save_baseline_read_unconfirmed",
                         "save_result_page")})
        operations = type("ObservedOperations", (OperationRepository,), {
            "finish_attempt": observed("finish_attempt", OperationRepository.finish_attempt)})
        with patch.object(assembly, "CaptureRepository", capture), patch.object(assembly, "OperationRepository", operations):
            runtime = await (self.factory if factory is None else factory)(
                self.database if database is None else database, "cam-1")
        assert runtime is not None
        return runtime

    async def factory_view(self, database, device):
        assert device == "cam-1"
        return await self.view(database=database)

    async def common_round(self):
        """由 execute_command 默认装配的取消 flow 消费共同拍摄保存。"""
        await self.context.flows["cancel"](SimpleNamespace(database=self.database,
            clock=SimpleNamespace(utc_micros=self.clock.wall_us)))

    async def cancel(self):
        from camctl.acceptance.input import ParsedInput
        from camctl.acceptance.service import AcceptanceContext, accept_input
        from camctl.persistence.repositories.acceptance import AcceptanceRepository
        from camctl.contracts.values import new_operation_key
        from camctl.bootstrap.flows import cancel_flow
        from ..capture.result_consumer_fixtures import ResultCatalog
        instant = datetime.fromtimestamp(self.clock.wall // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        accepted = await accept_input(ParsedInput(str(self.home / "cancel.json"), {
            "request_id": "2", "created_at": instant, "name": "取消拍摄",
            "actions": [{"name": "取消", "type": "cancel_task", "params": {
                "target": {"action_instance_id": str(self.action_id)}}}]}),
            AcceptanceContext(CommandMode.RUN, ResultCatalog(), AcceptanceRepository(),
                clock=SimpleNamespace(utc_micros=self.clock.wall_us)), new_operation_key(), self.database)
        assert accepted.plan_id is not None, accepted
        await cancel_flow(staging=Path(self.config.paths.staging),
            ready=Path(self.config.paths.ready), processing=Path(self.config.paths.processing))(
            SimpleNamespace(database=self.database, clock=SimpleNamespace(utc_micros=self.clock.wall_us)))

    async def reconnect(self, *, cold=False):
        await self.drain()
        await self.database.close()
        self.database = None
        await self.open_database()
        if cold:
            if self.default:
                lifecycle.close_runtime(self.deps)
                self.deps = None
                await self.assemble_default()
            else:
                self.factory = capture_assembly(**self.options)
        return await self.view()

    async def drain(self):
        errors = []
        for owner in (self.supervisor, *self.executors):
            try:
                result = await owner.drain_required()
                errors.extend(item.exception or RuntimeError(item.error) for item in result.failed)
            except BaseException as error:
                errors.append(error)
        if errors:
            raise BaseExceptionGroup("拍摄专项文件责任收场失败", errors)

    async def close(self):
        errors = []
        try:
            await self.drain()
        except BaseException as error:
            errors.append(error)
        if self.database is not None:
            try:
                await self.database.close()
            except BaseException as error:
                errors.append(error)
        if self.deps is not None:
            try:
                lifecycle.close_runtime(self.deps)
            except BaseException as error:
                errors.append(error)
        if self.owned is not None:
            try:
                self.owned.connection.close()
            except BaseException as error:
                errors.append(error)
        if errors:
            raise BaseExceptionGroup("拍摄专项资源关闭失败", errors)


@pytest_asyncio.fixture
async def bootstrap_case(capture_resources):
    case = BootstrapCaptureCase(capture_resources)
    try:
        yield case
    finally:
        await case.close()


async def run_flow_round(path, *, clock, flows, catalog=None, open_database=None):
    """实际会话消费一轮正式流程，保留锁、worker 和错误分类。"""
    from .session import session_context
    from camctl.session.service import run_session
    from camctl.session.clock import ClockCheckInput

    class RoundObserved(BaseException):
        pass

    async def observed(_context):
        raise RoundObserved()

    options = {} if open_database is None else {"open_database": open_database}
    context = session_context(path, clock=clock, catalog=catalog,
        flows={**flows, "round_observed": observed},
        clock_policy=lambda connection: ClockCheckInput(connection.execute(
            "SELECT trusted_time_lower_bound FROM runtime_state").fetchone()[0], 0, 0, 1), **options)
    try:
        return await run_session(context, None)
    except RoundObserved:
        return None


async def exhausted_result_world(case, home):
    """正式延时 START、冷会话等待和两次 RESULTS 耗尽形成报告原历史。"""
    from decimal import Decimal
    from dataclasses import replace
    from camctl.capture.handlers import capture_handler
    from camctl.capture.results import FileKind
    from camctl.capture.timelapse import CaptureWaitConfig
    from camctl.operations.attempts import AttemptConfig
    from camctl.session.clock import ClockCheckInput, check_clock
    from camctl.persistence.repositories.session import SessionRepository
    from camctl.contracts.values import new_operation_key
    from camctl.contracts.json_values import parse_exact_json
    from ..capture.test_capture_contract import ResultsDouble, _entry

    entry = replace(_entry("auxiliary", size=41, kind=FileKind.OTHER),
        original_name="auxiliary.bin", media_type="application/octet-stream")
    runtime = await case.world(home, "timelapse", results=ResultsDouble({1: (entry,)}, set_finalized=False), options={
        "check_config": AttemptConfig(2, Decimal("1.25"), Decimal(0)),
        "wait_config": lambda _params: CaptureWaitConfig(target_duration_ms=600_000, driver_margin_ms=0)})
    await capture_handler(case.handler)(case.action_id, runtime)
    case.clock.wall += 700_000_000
    runtime = await case.reconnect(cold=True)
    for _ in range(3):
        await capture_handler(case.handler)(case.action_id, runtime)
    assert case.owned.connection.execute("SELECT status FROM actions").fetchone() == (4,)
    status, used, raw = case.owned.connection.execute("SELECT status,attempts_used,error_json FROM operation_runs"
        " WHERE responsibility_key=?", (f"results/{case.activity_id}",)).fetchone()
    assert (status, used) == (6, 2) and case.results.calls == [case.activity_id, case.activity_id]
    clock = SimpleNamespace(utc_micros=lambda: case.clock.wall + 1)
    bound = check_clock(ClockCheckInput(None, 0, 0, 1), clock)
    saved = SessionRepository().update_lower_bound(bound, new_operation_key(), case.owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    return case.owned, runtime, case.action_id, parse_exact_json(raw)

"""默认保存门的共同准备：公开工厂生产申请，真实会话接手原申请。"""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap import lifecycle
from camctl.bootstrap.capture_assembly import execution_wait_config, session_capture_assembly
from camctl.capture.handlers import capture_handler
from camctl.contracts.values import new_operation_key
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.ports import DriverDeclaration
from camctl.operations.models import ErrorValue
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.session import SessionRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.session.clock import ClockCheckInput, check_clock

from .bootstrap_consumers import StorageGate, TransactionFault
from .capture import capture_database
from ..bootstrap.test_closed_result_binding_recovery import _unavailable_device_ports
from ..bootstrap.test_media_assembly import _config
from ..capture.result_consumer_fixtures import ResultCatalog, consumer_world
from ..capture.test_capture_contract import _NOW, ResultsDouble
from ..capture.test_recording_start_runtime import StartDriver, _START_EVIDENCE, _result
from ..capture.test_call_result_save import ReturnedCall, _CALL_EVIDENCE, _outcome


class SaveObserved(BaseException):
    """原保存已交付；专项在下一次业务读取或取得时刻前退出。"""


class DefaultRecoveryWorld:
    def __init__(self, source, resources, monkeypatch, *, binding=False):
        self.source, self.resources, self.monkeypatch = source, resources, monkeypatch
        self.path, self.metadata = source.path, source.metadata
        self.action_id = source.action_id
        self.wall = [source.formed_at]
        self.mono = [99_000_000_000]
        self.config = _config(self.path.parent)
        from camctl.persistence.directory_switch import canonical_to_path
        self.config = replace(self.config, paths=replace(self.config.paths,
            staging=str(canonical_to_path(self.metadata.staging_path)),
            ready=str(canonical_to_path(self.metadata.ready_path)),
            processing=str(canonical_to_path(self.metadata.processing_path))))
        if binding:
            self.config = replace(self.config, devices={})
        self.registry, self.methods = _unavailable_device_ports()
        self.deps = lifecycle.build_runtime(CommandMode.RUN, self.config, catalog=ResultCatalog())
        assert self.deps.startup_error is None, self.deps.startup_error
        self.database = self.runtime = None
        self.active = self.observed = False

    async def producer(self, *, driver=None, query=None, stopper=None, evidence=None, devices=None):
        """只把空集合交给正式工厂，由实际处理器形成完整保存申请。"""
        self.database = await capture_database(self.path)
        if driver is None:
            registry = self.registry
        else:
            ports = SimpleNamespace(control=driver.control)
            if query is not None:
                ports.query_state = query.query_state
            if stopper is not None:
                ports.stop = stopper.stop
            declaration = DriverDeclaration(True, stopper is not None, query is not None,
                False, False, False, False)
            registry = DriverRegistry((DriverEntry("camctl-adb", ports, declaration,
                evidence, DriverStatus.SOFTWARE_CONTRACT_VERIFIED),))
        factory = session_capture_assembly(devices=self.config.devices if devices is None else devices,
            drivers=registry, results=ResultsDouble({}), staging=Path(self.config.paths.staging),
            wait_config=execution_wait_config, wall_us=lambda: self.wall[0],
            monotonic_ns=lambda: self.mono[0],
            pending_capture_completions=self.deps.capture_completions,
            retry_wait_gate=self.deps.capture_retry_gate)
        self.runtime = await factory(self.database, "cam-1")
        assert self.runtime is not None
        return self.runtime

    async def reconnect(self):
        """关闭生产者真实 worker；恢复时由实际会话另开数据库。"""
        await self.resources.restart_database(self.path)
        # restart_database 的新 worker 由资源作用域持有；不作为会话数据库。
        self.wall[0] += 100_000_000
        self.mono[0] += 100_000_000_000
        self.deps.config = replace(self.deps.config,
            devices={"cam-1": {"driver": "alternate-camera"}})
        with self.observer() as owned:
            previous = owned.connection.execute(
                "SELECT trusted_time_lower_bound FROM runtime_state").fetchone()[0]
            clock = SimpleNamespace(utc_micros=lambda: self.wall[0])
            check = check_clock(ClockCheckInput(previous, 0, 0, 0), clock)
            if check.needs_bound_update:
                receipt = SessionRepository().update_lower_bound(check, new_operation_key(), owned)
                assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error

    def observer(self):
        from contextlib import contextmanager

        @contextmanager
        def opened():
            owned = open_existing(self.path, DbOpenMode.EXISTING_RW, DbConfig())
            try:
                assert owned.metadata == self.metadata
                yield owned
            finally:
                owned.connection.close()
        return opened()

    async def replay(self, entrance, check, *, ready, failure=False):
        """flows=None 生成默认入口，正式 SessionContext 选择本专项入口。

        实际 run_session 仍拥有锁、时钟检查、BusinessDatabase 和关闭。
        其他默认入口不提前消费本专项要验证的原申请。
        """
        actual_session = lifecycle.run_session
        actual_read = BusinessDatabase.read
        from camctl.capture import handlers
        actual_resume = handlers.resume_capture_completions
        capture_resumed = False
        restricted = entrance.startswith("restricted")
        selected = {"normal": "scheduling", "residual": "residual",
            "restricted": "winddown", "normal-cancel": "cancel",
            "restricted-cancel": "cancel"}[entrance]

        def boundary():
            if not capture_resumed:
                pytest.fail("原拍摄保存恢复尚未完整交付，不得进入新的业务边界")
            if failure:
                pytest.fail("原申请未可靠核实前不得取得新的业务候选或时刻")
            check()
            self.observed = True
            raise SaveObserved()

        async def read(database, operation, **options):
            # 恢复前置也读取原基准事实；申请已保存后才截停下一次读取。
            if self.active and capture_resumed and ready():
                boundary()
            return await actual_read(database, operation, **options)

        async def resume(*args, **options):
            nonlocal capture_resumed
            result = await actual_resume(*args, **options)
            if self.active:
                capture_resumed = True
            return result

        async def session(context, source):
            original = (context.restricted_flows if restricted else context.flows)[selected]

            async def flow(current):
                self.active = True
                current.clock = SimpleNamespace(utc_micros=boundary)
                try:
                    return await original(current)
                finally:
                    self.active = False
                    current.clock = SimpleNamespace(utc_micros=lambda: self.wall[0])

            context.clock = SimpleNamespace(utc_micros=lambda: self.wall[0])
            context.flows = {} if restricted else {selected: flow}
            context.restricted_flows = {selected: flow} if restricted else {}
            context.clock_policy = lambda connection: ClockCheckInput(
                connection.execute("SELECT trusted_time_lower_bound FROM runtime_state").fetchone()[0],
                self.wall[0] + 1 if restricted else 0, 0, 0)
            return await actual_session(context, source)

        with self.monkeypatch.context() as patch:
            patch.setattr(lifecycle, "run_session", session)
            patch.setattr("camctl.devices.drivers.runtime.current_registry", lambda: self.registry)
            patch.setattr(BusinessDatabase, "read", read)
            patch.setattr(handlers, "resume_capture_completions", resume)
            if failure:
                result = await lifecycle.execute_command(self.deps, None, poll_interval_s=.005)
                assert not self.observed and result.reason == "state_db_error", result
            else:
                try:
                    result = await lifecycle.execute_command(self.deps, None, poll_interval_s=.005)
                except SaveObserved:
                    pass
                else:
                    pytest.fail(f"未到保存门后边界: {result!r}")
                assert self.observed
        for method in self.methods:
            method.assert_not_called()

    def close(self):
        lifecycle.close_runtime(self.deps)


async def record_world(tmp_path, resources, monkeypatch, *, mode=None, consumer="ordinary"):
    """公开受理并授予录像；START/STOP 均由真实 handler 执行。"""
    world = None
    driver = StartDriver(_result(confirmed=False,
        no_effect=mode == "lowered", error=ErrorValue(
            "device_busy" if mode == "lowered" else "transport_timeout",
            "device" if mode == "lowered" else "transport")))
    if mode not in (None, "unknown", "lowered"):
        raise ValueError("仅构造当前正式可达的录像业务场景")
    query = None
    stopper = ReturnedCall(_outcome("stop")) if mode is None else None

    async def runtime_factory(owned, **_options):
        nonlocal world
        identity = owned.connection.execute("SELECT id FROM actions WHERE type=2").fetchone()[0]
        source = SimpleNamespace(path=tmp_path / "state.db", metadata=owned.metadata,
            action_id=identity, formed_at=_NOW)
        world = DefaultRecoveryWorld(source, resources, monkeypatch)
        if mode is None:
            # 成功启动观察由正式调度已分配的活动身份提供。
            from ..capture.result_consumer_fixtures import returned
            driver.outcome = returned("operation", "start_confirmed", 1)
        devices = {"cam-1": {"driver": "camctl-adb",
            "recording": {"max_start_attempts": 3, "start_retry_interval_s": "1",
                          "max_stop_attempts": 1, "stop_retry_interval_s": "1"},
            "query": {"max_attempts": 2, "retry_interval_s": "1"}}}
        return await world.producer(driver=driver, query=query, stopper=stopper,
            evidence=_CALL_EVIDENCE if mode is None else _START_EVIDENCE, devices=devices)

    owned, runtime, action_id, _handler = await consumer_world(tmp_path, "record",
        start=False, runtime_factory=runtime_factory)
    world.owned, world.driver, world.query, world.stopper = owned, driver, query, stopper
    try:
        await capture_handler("camera_record")(action_id, runtime)
        if mode is None:
            world.wall[0] += 60_000_000
            world.mono[0] += 60_000_000_000
            actual = OperationRepository.finish_attempt

            class StopSaved(Exception):
                pass

            def saved(repository, command, key, current):
                receipt = actual(repository, command, key, current)
                if command.ticket.operation != "stop":
                    return receipt
                assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
                return replace(receipt, handoff_error=StopSaved())

            with monkeypatch.context() as patch:
                patch.setattr(OperationRepository, "finish_attempt", saved)
                try:
                    await capture_handler("camera_record")(action_id, runtime)
                except StopSaved:
                    pass
                else:
                    assert len(stopper.calls) == 1, "处理器必须实际执行一次 STOP"
                    pytest.fail("真实 STOP 结果未到保存交付边界")
            world.stop_id = owned.connection.execute(
                "SELECT id FROM operation_runs WHERE responsibility_key='stop/1'").fetchone()[0]
            if consumer == "canceled":
                from ..capture.test_cancel_timelapse_exhaustion_files import _apply_public_cancel
                _apply_public_cancel(owned, action_id, world.wall[0] + 1)
        devices = {"cam-1": {"driver": "camctl-adb",
            "recording": {"max_start_attempts": 1 if mode == "lowered" else 3,
                "max_stop_attempts": 1, "stop_retry_interval_s": "1"},
            "query": {"max_attempts": 2}}}
        # 新配置只通过正式工厂输入，不修改运行对象的执行配置或责任集合。
        await world.producer(driver=driver, query=query, stopper=stopper,
            evidence=_CALL_EVIDENCE if mode is None else _START_EVIDENCE, devices=devices)
        world.wall[0] += 5_000_000
        world.mono[0] += 5_000_000_000
        world.formed_at, world.mode, world.consumer = world.wall[0], mode, consumer
        assert len(driver.calls) == 1
        return world
    except BaseException:
        owned.connection.close()
        world.close()
        raise

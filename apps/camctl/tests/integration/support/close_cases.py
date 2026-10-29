"""START/STOP复合收场通过真实受理、调度和正式会话工厂准备。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

from camctl.bootstrap.flows import cancel_flow
from camctl.capture.handlers import capture_handler
from camctl.contracts.values import new_operation_key
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import ControlRequest, DeviceCallResult, DriverDeclaration
from camctl.operations.attempts import (
    AttemptConfig, AttemptFinish, AttemptIntent, AttemptTarget, BeginDisposition,
    OperationKind, QueryPurpose,
)
from camctl.operations.models import (
    AttemptStatus, CallInfo, CallOutcome, EffectState, ErrorValue,
    EvidenceValue, Settlement, SettlementBasis,
)
from camctl.operations.validation import validate_outcome
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.scheduling import (
    ObserveWindowRequest, SchedulingRepository, StartActionRequest, register_window_guard,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .capture import capture_database, capture_factory, restart_capture_database
from .session import accept_plan, session_config
from ..bootstrap.test_recording_stop import _RecordCatalog
from ..capture.test_capture_contract import _NOW, ResultsDouble


class CloseDriver:
    """控制与停止端口在实际调用时按原票据返回完整结果。"""

    def __init__(self, *, start="confirmed"):
        self.start = start
        self.calls, self.stop_calls, self.query_calls = [], [], []

    async def control(self, request):
        self.calls.append(request)
        confirmed = self.start == "confirmed"
        error = None if confirmed else ErrorValue(
            "device_busy" if self.start == "lowered" else "transport_timeout",
            "device" if self.start == "lowered" else "transport")
        return DeviceCallResult.from_outcome(CallOutcome(
            status=AttemptStatus.SUCCEEDED if confirmed else AttemptStatus.FAILED,
            effect=EffectState.CONFIRMED if confirmed else
                EffectState.NO_EFFECT if self.start == "lowered" else EffectState.UNKNOWN,
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("operation_returned", 1, {})),
            error=error,
            observations=(DeviceObservation("start_confirmed", 1,
                {"activity_id": request.ticket.target_id}),) if confirmed else (),
            call_info=CallInfo(local_exit_code=0)))

    async def stop(self, request):
        self.stop_calls.append(request)
        return DeviceCallResult.from_outcome(CallOutcome(
            status=AttemptStatus.FAILED, effect=EffectState.UNKNOWN,
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("stop_returned", 1, {})),
            error=ErrorValue("transport_timeout", "transport", {"timeout_s": Decimal("0.125")}),
            call_info=CallInfo(local_exit_code=0)))

    async def query_state(self, request):
        self.query_calls.append(request)
        return DeviceCallResult.from_outcome(CallOutcome(
            status=AttemptStatus.FAILED, effect=EffectState.UNKNOWN,
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("query_returned", 1, {})),
            error=ErrorValue("transport_timeout", "transport")))


class CloseCase:
    """工厂拥有原申请；重开实际worker后仍通过同一正式工厂创建runtime。"""

    async def advance(self):
        await capture_handler("camera_record")(self.action_id, self.runtime)

    async def reopen(self, *, recording=None, query=None, changed_binding=False):
        self.database = await restart_capture_database(self.path)
        self.owned.connection.close()
        self.owned = open_existing(self.path, DbOpenMode.EXISTING_RW, DbConfig())
        if recording is not None:
            self.devices["cam-1"]["recording"].update(recording)
        if query is not None:
            self.devices["cam-1"]["query"].update(query)
        if changed_binding:
            self.devices["cam-1"]["driver"] = "replacement-driver"
        self.runtime = await self.factory(self.database, "cam-1")

    async def close(self):
        original, errors = sys.exception(), []
        try:
            await self.database.close()
        except BaseException as error:
            errors.append(error)
        finally:
            try:
                self.owned.connection.close()
            except BaseException as error:
                errors.append(error)
        if errors:
            raise BaseExceptionGroup("复合收场世界的资源关闭失败",
                ([original] if original is not None else []) + errors)


async def _prepared(tmp_path, *, start="confirmed", query=False):
    world = CloseCase()
    world.wall, world.mono = [_NOW], [7_000_000_000]
    world.driver, world.replacement = CloseDriver(start=start), CloseDriver()
    world.results = ResultsDouble({})
    world.stopper = SimpleNamespace(calls=world.driver.stop_calls)
    world.query = SimpleNamespace(calls=world.driver.query_calls) if query else None
    evidence = EvidenceRegistry(tuple(EvidenceContract(name, 1, operation,
        frozenset({"activity_id"}) if name.endswith("confirmed") or name == "activity_status" else frozenset(),
        identity_field="activity_id" if name.endswith("confirmed") or name == "activity_status" else None)
        for name, operation in (("operation_returned", "control"), ("start_confirmed", "control"),
            ("stop_returned", "stop"), ("stop_confirmed", "stop"),
            ("query_returned", "query"), ("activity_status", "query"))))
    world.evidence = evidence
    config = session_config(tmp_path, devices={"cam-1": {
        "kind": "camera", "driver": "camctl-adb",
        "recording": {"max_start_attempts": 3, "start_timeout_s": Decimal("0.75"),
            "start_retry_interval_s": Decimal("1"), "max_stop_attempts": 1,
            "stop_timeout_s": Decimal("0.125"), "stop_retry_interval_s": Decimal("1")},
        "query": {"max_attempts": 2, "timeout_s": Decimal("0.5"), "retry_interval_s": Decimal("1")}}})
    world.config, world.path = config, Path(config.paths.state_db)
    # 正式加载器的初始配置不可变；此Mapping端口表达后续重开时的部署输入。
    world.devices = {identity: {name: dict(value) if isinstance(value, Mapping) else value
        for name, value in declaration.items()} for identity, declaration in config.devices.items()}
    assert initialize_state(config, world.path).outcome is InitOutcome.CREATED
    world.owned = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    world.metadata = world.owned.metadata
    world.database = await capture_database(world.owned)
    catalog = _RecordCatalog()
    catalog.duration_s = Decimal("60")
    instant = datetime.fromtimestamp(_NOW // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    try:
        register_window_guard()
        await accept_plan(world.database, tmp_path, catalog, {
            "request_id": "1", "created_at": instant, "name": "原复合收场",
            "actions": [{"name": "录像", "type": "camera_record", "device_id": "cam-1",
                "scheduled_at": instant, "params": {"type": "video"},
                "policy": {"max_delay_ms": 60000}}]},
            SimpleNamespace(utc_micros=lambda: world.wall[0]))
        world.action_id, = world.owned.connection.execute("SELECT id FROM actions WHERE type=2").fetchone()
        repository = SchedulingRepository()
        for method, request in ((repository.observe_window,
                ObserveWindowRequest(world.action_id, _NOW, _NOW)),
                (repository.start_action, StartActionRequest(world.action_id, _NOW, _NOW))):
            key = new_operation_key()
            receipt = await world.database.write(lambda owned, method=method, request=request, key=key:
                method(request, key, owned), key=key)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        world.activity_id, = world.owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (world.action_id,)).fetchone()

        def entry(identity, driver):
            ports = SimpleNamespace(control=driver.control, stop=driver.stop)
            if query:
                ports.query_state = driver.query_state
            return DriverEntry(identity, ports, DriverDeclaration(control_supported=True, stop_supported=True,
                query_supported=query, result_supported=False, read_supported=False,
                digest_supported=False, delete_supported=False), evidence, DriverStatus.SOFTWARE_CONTRACT_VERIFIED)

        registry = DriverRegistry((entry("camctl-adb", world.driver),
            entry("replacement-driver", world.replacement)))
        world.factory = await capture_factory(world.database, driver=world.driver, evidence=evidence,
            drivers=registry, devices=world.devices, results=world.results, wall_us=lambda: world.wall[0],
            monotonic_ns=lambda: world.mono[0])
        world.runtime = await world.factory(world.database, "cam-1")
        await world.advance()
        assert len(world.driver.calls) == 1
        assert world.owned.connection.execute("SELECT status FROM actions WHERE id=?",
            (world.action_id,)).fetchone() == (2,)
        assert world.owned.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts WHERE status=1 OR result_event_id IS NULL").fetchone() == (0,)
        return world
    except BaseException:
        await world.close()
        raise


async def start_case(tmp_path, mode):
    if mode not in ("unknown", "lowered"):
        raise ValueError(mode)
    world = await _prepared(tmp_path, start="lowered" if mode == "lowered" else "unknown")
    world.mode = mode
    # 首次受理冻结的查询能力是正式事实，登记端口不能改变它。
    assert world.owned.connection.execute(
        "SELECT state_query_supported FROM device_activities WHERE id=?", (world.activity_id,)).fetchone() == (0,)
    if mode == "lowered":
        world.devices["cam-1"]["recording"]["max_start_attempts"] = 1
    world.wall[0] += 5_000_000
    world.mono[0] += 5_000_000_000
    world.formed_at = world.wall[0]
    world.runtime = await world.factory(world.database, "cam-1")
    return world


async def start_query_storage_case(tmp_path):
    """正式尝试仓储登记一次QUERY，供两责任共同保存的存储契约使用。"""
    world = await _prepared(tmp_path, start="unknown", query=True)
    world.mode = "query_storage"
    try:
        assert world.owned.connection.execute(
            "SELECT state_query_supported FROM device_activities WHERE id=?",
            (world.activity_id,)).fetchone() == (0,)
        world.wall[0] += 1_000_000
        world.mono[0] += 1_000_000_000
        intent = AttemptIntent(
            operation="query", action_id=world.action_id, kind=OperationKind.QUERY_ACTIVITY,
            target=AttemptTarget(activity_id=world.activity_id),
            query_purpose=QueryPurpose.START_CONFIRMATION,
            config=AttemptConfig(max_attempts=2, timeout_s=Decimal("0.5"),
                retry_interval_s=Decimal("1")), occurred_at=world.wall[0])
        operations = OperationRepository()
        key = new_operation_key()
        begun = await world.database.write(lambda owned:
            operations.begin_attempt(intent, key, owned), key=key)
        assert begun.kind is DbOutcomeKind.COMPLETED, begun.error
        assert begun.value.disposition is BeginDisposition.GRANTED
        world.query_ticket = begun.value.ticket
        response = await world.driver.query_state(ControlRequest(
            "query", world.driver.calls[0].binding,
            {"activity_id": str(world.activity_id)}, ticket=world.query_ticket,
            timeout_s=intent.config.timeout_s))
        assert response.outcome is not None
        finish = AttemptFinish(world.query_ticket,
            validate_outcome(world.query_ticket, response.outcome, world.evidence),
            world.wall[0], retry_wait=True)
        key = new_operation_key()
        saved = await world.database.write(lambda owned:
            operations.finish_attempt(finish, key, owned), key=key)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        assert len(world.query.calls) == 1
        assert world.owned.connection.execute(
            "SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE id=?",
            (world.query_ticket.run_id,)).fetchone() == (2, 1, 1)
        world.wall[0] += 5_000_000
        world.mono[0] += 5_000_000_000
        world.formed_at = world.wall[0]
        return world
    except BaseException:
        await world.close()
        raise


async def stop_case(tmp_path, consumer):
    if consumer not in ("ordinary", "canceled"):
        raise ValueError(consumer)
    world = await _prepared(tmp_path)
    world.consumer = consumer
    world.wall[0], world.mono[0] = _NOW + 60_000_000, 67_000_000_000
    try:
        await world.advance()
        assert len(world.stopper.calls) == 1
        world.stop_id = world.stopper.calls[0].ticket.run_id
        assert world.owned.connection.execute(
            "SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE id=?",
            (world.stop_id,)).fetchone() == (2, 1, 1)
        assert world.owned.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts WHERE status=1 OR result_event_id IS NULL").fetchone() == (0,)
        if consumer == "canceled":
            instant = datetime.fromtimestamp(_NOW // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            catalog = _RecordCatalog()
            catalog.duration_s = Decimal("60")
            await accept_plan(world.database, tmp_path, catalog, {
                "request_id": "2", "created_at": instant, "name": "取消停止中的录像",
                "actions": [{"name": "取消", "type": "cancel_task",
                    "params": {"target": {"action_instance_id": str(world.action_id)}}}]},
                SimpleNamespace(utc_micros=lambda: world.wall[0] + 1))
            await cancel_flow(ready=Path(world.config.paths.ready), processing=Path(world.config.paths.processing))(
                SimpleNamespace(database=world.database, clock=SimpleNamespace(utc_micros=lambda: world.wall[0] + 1)))
        world.wall[0] += 5_000_000
        world.mono[0] += 5_000_000_000
        world.formed_at = world.wall[0]
        return world
    except BaseException:
        await world.close()
        raise


class CloseFault:
    """只在原公共保存事务的SQLite表列写入或COMMIT边界失败。"""

    def __init__(self, connection, table, phase):
        self.connection, self.table, self.phase = connection, table, phase
        self.targeted = self.failed = False
        self.commit_calls = self.rollback_calls = 0
        self.target_before = tuple(connection.iterdump())
        self.transaction_before = self.target_before

        def authorize(operation, table, column, _database, _trigger):
            if operation == sqlite3.SQLITE_UPDATE and table == self.table and column == "status":
                self.targeted = True
                if phase == "projection":
                    self.failed = True
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, parameters=()):
        if sql == "COMMIT" and self.targeted and self.phase != "projection":
            self.commit_calls += 1
            self.failed = True
            if self.phase == "commit_after":
                self.connection.execute(sql, parameters)
            raise sqlite3.OperationalError("原复合保存COMMIT响应丢失")
        result = self.connection.execute(sql, parameters)
        if sql == "ROLLBACK" and self.failed:
            self.rollback_calls += 1
        return result

    def close(self):
        self.connection.set_authorizer(None)

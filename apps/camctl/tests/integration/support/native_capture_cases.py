"""原生有限任务通过正式受理、会话工厂与设备端口运行。"""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import AcceptanceContext, CommandMode, accept_input
from camctl.bootstrap.capture_assembly import DriverResultListing
from camctl.bootstrap.flows import cancel_flow
from camctl.capture.handlers import capture_handler
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.contracts.values import new_operation_key
from camctl.devices.bindings import DeviceBinding
from camctl.devices.ports import DeviceCallResult, ResultDriver, StopDriver
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue, Settlement, SettlementBasis
from camctl.persistence.repositories.acceptance import AcceptanceRepository

from ..capture.result_consumer_fixtures import RESULT_EVIDENCE, consumer_world
from ..capture.test_capture_contract import _NOW
from ..capture.test_record_file_completion_wait import _REGISTRY
from .capture import capture_factory


async def native_world(tmp_path, catalog):
    wall, mono = [_NOW], [5_000_000_000]
    result = create_autospec(ResultDriver, instance=True)
    stopper = create_autospec(StopDriver, instance=True)
    stopper.stop.return_value = DeviceCallResult.from_outcome(CallOutcome(
        status=AttemptStatus.FAILED, effect=EffectState.UNKNOWN,
        error=ErrorValue('device_error', 'device'),
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue('stop_returned', 1, {}))))
    evidence = _REGISTRY.with_contract(RESULT_EVIDENCE.contract('timelapse_sent', 1))

    async def assemble(owned, *, driver):
        factory = await capture_factory(owned, driver=driver, evidence=evidence,
            results=DriverResultListing(result, DeviceBinding('cam-1', 'camctl-adb'), evidence),
            stopper=stopper, wall_us=lambda: wall[0], monotonic_ns=lambda: mono[0],
            wait_config=lambda action: CaptureWaitConfig(1000, 200, 0, 0),
            check_config=AttemptConfig(1, Decimal('1'), Decimal(0)),
            directory=directory)
        return await factory(owned)

    from camctl.contracts.pages import Page
    from camctl.devices.directory import DirectoryRead
    from ..capture.test_baseline_prepare import Directory
    directory = Directory([lambda request: DirectoryRead(None, None, Page((), None))])
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, 'timelapse', catalog=catalog, start=False, runtime_factory=assemble)
    advance = capture_handler(handler)
    await advance(action_id, runtime)
    return owned, runtime, action_id, advance, wall, mono, result, stopper


async def request_cancel(runtime, action_id, wall, catalog):
    instant = datetime.fromtimestamp(wall // 1_000_000, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    accepted = await accept_input(ParsedInput('cancel-timelapse.json', {
        'request_id': '2', 'created_at': instant, 'name': '取消延时摄影',
        'actions': [{'name': '取消', 'type': 'cancel_task', 'params': {
            'target': {'action_instance_id': str(action_id)}}}]}),
        AcceptanceContext(CommandMode.RUN, catalog, AcceptanceRepository(),
            clock=SimpleNamespace(utc_micros=lambda: wall)), new_operation_key(), runtime.database)
    assert accepted.plan_id is not None, accepted
    metadata = runtime.database.metadata
    await cancel_flow(staging=Path(metadata.staging_path),
        ready=Path(metadata.ready_path), processing=Path(metadata.processing_path),
        start_entries=runtime.start_entries)(SimpleNamespace(database=runtime.database,
            clock=SimpleNamespace(utc_micros=lambda: wall)))


async def native_fault_world(tmp_path, catalog, *, committed=False):
    """业务数据库工作线程在实际 SQLite 提交边界注入故障。"""
    from dataclasses import replace
    from camctl.persistence.business import BusinessDatabase
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    from ..capture.result_consumer_fixtures import returned
    from ..capture.test_result_device_completion import _EVIDENCE, device_pages
    from .call_cases import CallPort, CallStorageFault
    from .database import database_path

    fault = AfterCommitHoldFault() if committed else CallStorageFault()
    wall, mono = [_NOW], [5_000_000_000]
    stopper = CallPort(returned("stop", "stop_confirmed", 1))
    holder = SimpleNamespace()
    result_driver = device_pages(holder, completion_page=None)
    evidence = _REGISTRY.with_contract(RESULT_EVIDENCE.contract("timelapse_sent", 1))
    evidence = evidence.with_contract(_EVIDENCE.contract("timelapse_completed", 1))
    devices = {"cam-1": {"driver": "camctl-adb", "result_check": {
        "max_attempts": 3, "call_timeout_s": Decimal("1.25"), "retry_interval_s": Decimal("2")}}}
    state = {}
    connection_threads = []

    async def assemble(owned, *, driver):
        path = database_path(owned)
        def connect():
            from threading import current_thread
            connection_threads.append(current_thread())
            actual = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
            return replace(actual, connection=fault.connection(actual.connection))
        database = await BusinessDatabase.open(connect)
        from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
        from camctl.devices.ports import DriverDeclaration
        from camctl.contracts.pages import Page
        from camctl.devices.directory import DirectoryRead
        from ..capture.test_baseline_prepare import Directory
        directory = Directory([lambda request: DirectoryRead(None, None, Page((), None))])
        ports = SimpleNamespace(control=driver.control, stop=stopper.stop, read_directory=directory.read_directory)
        declaration = DriverDeclaration(control_supported=True, stop_supported=True,
            query_supported=False, result_supported=False, read_supported=False,
            digest_supported=False, delete_supported=False, directory_supported=True)
        drivers = DriverRegistry(tuple(DriverEntry(identity, ports, declaration, evidence,
            DriverStatus.SOFTWARE_CONTRACT_VERIFIED) for identity in ("camctl-adb", "other")))
        async def build_factory(db):
            return await capture_factory(db, driver=driver, evidence=evidence,
                results=holder.results, stopper=stopper, devices=devices, drivers=drivers,
                wall_us=lambda: wall[0], monotonic_ns=lambda: mono[0], check_config=None,
                directory=directory)
        factory = await build_factory(database)
        state.update(database=database, factory=factory, driver=driver,
            connect=connect, build_factory=build_factory)
        return await factory(database)

    owned, runtime, action_id, handler = await consumer_world(tmp_path, "timelapse",
        independent_activity=True, catalog=catalog, start=False, runtime_factory=assemble)
    return SimpleNamespace(owned=owned, runtime=runtime, action_id=action_id, handler=handler,
        fault=fault, wall=wall, mono=mono, stopper=stopper, devices=devices,
        result_driver=result_driver, connection_threads=connection_threads, **state)


class AfterCommitHoldFault:
    """原提交实际成功后丢失回执，并暂停读取以保留未知状态。"""

    def __init__(self):
        self.target = None
        self.enabled = True
        self.injected = False
        self.held_reads = 0

    def arm(self, target):
        self.target = target
        self.calls_before = len(target.calls)

    def connection(self, real):
        import sqlite3
        owner = self

        class Connection:
            def __getattr__(self, name):
                return getattr(real, name)

            def execute(self, statement, parameters=()):
                if owner.target is None or len(owner.target.calls) <= owner.calls_before:
                    return real.execute(statement, parameters)
                commit = False

                def authorize(operation, table, _column, _database, _source):
                    nonlocal commit
                    if operation == sqlite3.SQLITE_TRANSACTION and table == "COMMIT":
                        commit = True
                    if owner.enabled and owner.injected and operation == sqlite3.SQLITE_READ:
                        owner.held_reads += 1
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                real.set_authorizer(authorize)
                try:
                    cursor = real.execute(statement, parameters)
                    if commit and owner.enabled and not owner.injected:
                        owner.injected = True
                        raise sqlite3.OperationalError("实际提交后回执丢失")
                    return cursor
                finally:
                    real.set_authorizer(None)

        return Connection()

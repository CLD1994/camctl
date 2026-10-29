"""目录等待期间其他设备继续；真实业务数据库在实际调用收场后关闭。"""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from camctl.bootstrap.capture_assembly import execution_wait_config, session_capture_assembly
from camctl.bootstrap.flows import capture_flow
from camctl.contracts.pages import Page
from camctl.devices.directory import DirectoryRead
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import DeviceCallResult, DriverDeclaration
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.repositories.scheduling import register_window_guard
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.session.service import StateDbFailure

from ..acceptance.test_acceptance import _plan_body
from ..acceptance.test_adb_camera_definitions import _catalog, _params
from ..capture.test_capture_contract import ResultsDouble, _PAGE_EVIDENCE
from ..scheduling.test_start_action import _SCHEDULED
from ..support.session import accept_plan, session_config

pytestmark = pytest.mark.asyncio
register_window_guard()


class TrackedConnection:
    """在原连接所属线程记录真实关闭，其他 SQLite 操作完整转发。"""

    def __init__(self, connection):
        self.connection, self.closed = connection, False

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, *args):
        return self.connection.execute(*args)

    @property
    def in_transaction(self):
        return self.connection.in_transaction

    @property
    def total_changes(self):
        return self.connection.total_changes

    def close(self):
        self.connection.close()
        self.closed = True


@pytest.mark.parametrize("fatal", [False, True])
async def test_devices_progress_independently_and_settle_before_connection_close(tmp_path, fatal):
    config = session_config(tmp_path, devices={device: {
        "kind": "camera", "driver": "dji-action6"} for device in ("cam-1", "cam-2")})
    assert initialize_state(config, tmp_path / "state.db").outcome is InitOutcome.CREATED
    entered, other_started, canceled, returned, actual_done = (asyncio.Event() for _ in range(5))
    connections = []

    def open_database_connection():
        current = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
        if fatal:
            # 由真实存储在首次完成基准时拒绝写入，不绑定 SQL 文本或仓储调用图。
            current.connection.execute(
                "CREATE TEMP TRIGGER reject_baseline_fix BEFORE UPDATE OF baseline_state"
                " ON device_activities WHEN NEW.baseline_state=3 AND OLD.baseline_state<>3"
                " BEGIN SELECT RAISE(ABORT, '状态保存不可用'); END")
        tracked = TrackedConnection(current.connection)
        connections.append(tracked)
        return replace(current, connection=tracked)

    class Driver:
        async def read_directory(self, request, *, stop):
            if request.binding.device_id == "cam-1":
                entered.set()
                try:
                    await returned.wait()
                except asyncio.CancelledError:
                    canceled.set()
                    await returned.wait()
                    raise
                finally:
                    actual_done.set()
            return DirectoryRead(None, None, Page((), None))

        async def control(self, request):
            if request.binding.device_id == "cam-2":
                other_started.set()
            return DeviceCallResult((DeviceObservation(
                "start_confirmed", 1, {"activity_id": request.ticket.target_id}),), None)

    driver = Driver()
    entry = DriverEntry("dji-action6", driver, DriverDeclaration(
        control_supported=True, stop_supported=False, query_supported=False,
        result_supported=False, read_supported=False, digest_supported=False,
        delete_supported=False, directory_supported=True), _PAGE_EVIDENCE,
        DriverStatus.SOFTWARE_CONTRACT_VERIFIED)
    factory = session_capture_assembly(
        devices=config.devices, drivers=DriverRegistry((entry,)),
        results=ResultsDouble({}), staging=tmp_path / "staging",
        wait_config=execution_wait_config, wall_us=lambda: _SCHEDULED,
        monotonic_ns=lambda: 0)
    capture = capture_flow(factory)
    database = await BusinessDatabase.open(open_database_connection)
    task = None
    try:
        for request_id, device_id in (("42", "cam-1"), ("43", "cam-2")):
            body = _plan_body(request_id=request_id)
            body["actions"][0].update(type="camera_record", device_id=device_id,
                params=_params("dji-action6", "camera_record"))
            await accept_plan(database, tmp_path, _catalog("dji-action6", device_id=device_id),
                body, SimpleNamespace(utc_micros=lambda: _SCHEDULED))
        context = SimpleNamespace(database=database, clock=SimpleNamespace(utc_micros=lambda: _SCHEDULED))

        async def drive():
            while not (canceled if fatal else other_started).is_set():
                await capture(context)
                await asyncio.sleep(0)

        task = asyncio.create_task(drive())
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for((canceled if fatal else other_started).wait(), 1)
        assert all(not connection.closed for connection in connections)
        assert not actual_done.is_set()
        returned.set()
        if fatal:
            with pytest.raises(StateDbFailure):
                await task
        else:
            await task
        await capture.settle()
        assert actual_done.is_set()
        assert all(not connection.closed for connection in connections)
    finally:
        returned.set()
        try:
            if task is not None:
                try:
                    await task
                except StateDbFailure:
                    if not fatal:
                        raise
            await capture.settle()
        finally:
            await database.close()
    assert connections and all(connection.closed for connection in connections)

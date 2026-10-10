"""一台设备等待目录时其他设备继续；致命错误等实际收场后关闭连接。"""
import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.bootstrap.flows import capture_flow
from camctl.contracts.pages import Page
from camctl.devices.directory import DirectoryRead
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import DeviceCallResult
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.scheduling import register_window_guard
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.session.service import StateDbFailure
from ..acceptance.test_acceptance import _plan_body
from ..acceptance.test_adb_camera_definitions import _catalog, _params
from ..capture.test_baseline_start import _runtime
from ..capture.test_baseline_prepare import Directory
from ..scheduling.test_start_action import _SCHEDULED, _accept_plan, owned

pytestmark = pytest.mark.asyncio
register_window_guard()


class TrackedConnection:
    def __init__(self, connection):
        self.connection, self.closed = connection, False
    def execute(self, *args):
        return self.connection.execute(*args)
    @property
    def in_transaction(self):
        return self.connection.in_transaction
    def close(self):
        self.connection.close()
        self.closed = True


@pytest.mark.parametrize("fatal", [False, True])
async def test_devices_progress_independently_and_settle_before_connection_close(owned, tmp_path, fatal):
    for request_id, device_id in (("42", "cam-1"), ("43", "cam-2")):
        body = _plan_body(request_id=request_id)
        body["actions"][0].update(type="camera_record", device_id=device_id, params=_params("dji-action6", "camera_record"))
        await _accept_plan(owned, tmp_path, body, _catalog("dji-action6", device_id=device_id))
    entered, other_started, canceled, returned, actual_done = (asyncio.Event() for _ in range(5))
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    connections = []
    def open_connection():
        current = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        tracked = TrackedConnection(current.connection)
        connections.append(tracked)
        return replace(current, connection=tracked)
    class HeldDirectory:
        async def read_directory(self, request, *, stop):
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
    class Driver:
        async def control(self, request):
            if request.binding.device_id == "cam-2":
                other_started.set()
            return DeviceCallResult((DeviceObservation("start_confirmed", 1, {"activity_id": request.ticket.target_id}),), None)
    class UnavailableSave(CaptureRepository):
        def fix_baseline(self, request, key, owned):
            return DbOutcome(DbOutcomeKind.ROLLED_BACK, error=OSError("状态保存不可用"))
    def factory(connection, device_id):
        directory = HeldDirectory() if device_id == "cam-1" else Directory([lambda r: DirectoryRead(None, None, Page((), None))])
        runtime = _runtime(connection, directory, Driver())
        if fatal and device_id == "cam-2":
            runtime.capture = UnavailableSave()
        return runtime
    context = SimpleNamespace(open_connection=open_connection, clock=SimpleNamespace(utc_micros=lambda: _SCHEDULED))
    capture = capture_flow(factory)
    async def drive():
        while not (canceled if fatal else other_started).is_set():
            await capture(context)
            await asyncio.sleep(0)
    task = asyncio.create_task(drive())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for((canceled if fatal else other_started).wait(), 1)
        assert any(not connection.closed for connection in connections) and not actual_done.is_set()
        returned.set()
        if fatal:
            with pytest.raises(StateDbFailure):
                await task
            await capture.settle()
        else:
            await task
            await capture.settle()
        assert all(connection.closed for connection in connections) and actual_done.is_set()
    finally:
        returned.set()
        try:
            await task
            await capture.settle()
        except StateDbFailure:
            if not fatal:
                raise

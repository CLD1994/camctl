"""真实工具取消后，拍摄消费者保存原结果且不派发后续命令。"""

import asyncio
import json
import sys
import shlex
from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.capture.handlers import capture_handler
from camctl.contracts.pages import Page
from camctl.contracts.enums import enum_for
from camctl.devices.adb_transport import DeviceCommand, InterpretedFacts
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryRead
from camctl.devices.drivers.adb_cameras.commands import CameraModel, start_for
from camctl.devices.drivers.adb_cameras.contracts import CameraCall, CameraContract
from camctl.devices.drivers.adb_cameras.driver import AdbCameraDriver
from camctl.devices.drivers.adb_cameras.filesystem import AdbFilesystem, ShellFileTools
from camctl.devices.drivers.adb_cameras.transport import shell_argv
from camctl.devices.evidence import DeviceObservation
from camctl.operations.models import EffectState
from camctl.operations.process import execute_tool, spawn_subprocess
from .test_adb_camera_dispatch import EVIDENCE, RETURNED, ASSUMED
from ..capture.test_baseline_start import _prepared, _runtime
from ..capture.test_baseline_prepare import Directory
from ..scheduling.test_start_action import owned

pytestmark = pytest.mark.asyncio


class LocalTransport:
    def __init__(self, marker, held_call):
        self.marker, self.held_call = marker, held_call
        self.calls, self.handles = [], []

    async def run(self, spec, stop):
        self.calls.append(spec)
        held = self.held_call(spec)
        script = "import sys; print('ACTUAL', flush=True)"
        if held:
            script += "; from pathlib import Path; import time; Path(" + repr(str(self.marker)) + ").write_text('started'); time.sleep(30)"
        async def spawn(local):
            handle = await spawn_subprocess(local)
            self.handles.append(handle)
            return handle
        return await execute_tool(replace(spec, argv=(sys.executable, "-c", script)), stop=stop, spawner=spawn)


async def _entered(marker):
    async with asyncio.timeout(5):
        while not marker.exists():
            await asyncio.sleep(.005)


def _assert_settled(transport):
    assert all(handle._process.returncode is not None for handle in transport.handles)
    assert all(handle._reader.done() and handle._stderr_reader.done() for handle in transport.handles)


@pytest.mark.parametrize("action_type", ["camera_record", "camera_timelapse"])
@pytest.mark.parametrize("phase", ["setting", "start"])
async def test_canceled_real_control_saves_actual_response_without_next_dispatch(owned, tmp_path, action_type, phase):
    await _prepared(owned, tmp_path, action_type)
    marker = tmp_path / "started"
    start_argv = shell_argv("serial-1", shlex.join(start_for(CameraModel.ACTION6, action_type)))
    def is_start(spec):
        return spec.argv == start_argv
    transport = LocalTransport(marker, lambda spec: is_start(spec) == (phase == "start"))

    def command(kind):
        class Parser:
            def interpret(self, raw):
                name = "setting_applied" if kind == "setting" else (
                    "start_confirmed" if action_type == "camera_record" else "timelapse_sent")
                observations = (DeviceObservation(name, 1, {"activity_id": "1"}),) if b"ACTUAL" in (raw.output or b"") else ()
                return InterpretedFacts(observations, None, EffectState.CONFIRMED if observations else EffectState.UNKNOWN)
        def factory(request, serial, argv, batch):
            return DeviceCommand("control", argv, request.binding,
                request.timeout_s, Decimal(".1"), EVIDENCE, RETURNED, ASSUMED, Parser(), kind)
        return factory

    contract = CameraContract(CameraModel.ACTION6,
        commands={CameraCall.SETTING: command("setting"), CameraCall.START: command("start")}, evidence=EVIDENCE)
    driver = AdbCameraDriver(contract, {"cam-1": {"driver": CameraModel.ACTION6, "adb": {"serial": "serial-1"}}},
        transport, terminate_grace_s=Decimal(".1"), monotonic_ns=lambda: 0)
    runtime = _runtime(owned, Directory([lambda request: DirectoryRead(None, None, Page((), None))]), driver)
    runtime.evidence = EVIDENCE
    task = asyncio.create_task(capture_handler(action_type)(1, runtime))
    try:
        await _entered(marker)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        _assert_settled(transport)
        row = owned.connection.execute("SELECT result_json,effect_state,error_json FROM operation_attempts").fetchone()
        result = json.loads(row[0])
        assert json.loads(row[2])["code"] == "transport_cancelled"
        assert result["observations"][0]["type"] == ("setting_applied" if phase == "setting" else (
            "start_confirmed" if action_type == "camera_record" else "timelapse_sent"))
        assert result["call_info"]["local_exit"]["signal"] is not None
        effect = enum_for("operation_attempts.effect_state")
        assert row[1] == int(effect.NO_EFFECT if phase == "setting" else effect.CONFIRMED)
        assert len([spec for spec in transport.calls if is_start(spec)]) == int(phase == "start")
        assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE responsibility_key='start/1'").fetchone() == (1,)
        assert owned.connection.execute("SELECT status FROM operation_attempts").fetchone() == (
            int(enum_for("operation_attempts.status").FAILED),)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_canceled_real_baseline_settles_tool_and_saves_error_without_start(owned, tmp_path):
    await _prepared(owned, tmp_path, "camera_record")
    transport = LocalTransport(tmp_path / "started", lambda spec: True)
    filesystem = AdbFilesystem(DeviceBinding("cam-1", CameraModel.ACTION6), "serial-1", transport,
        tools=ShellFileTools(), terminate_grace_s=Decimal(".1"))
    runtime = _runtime(owned, filesystem, None)
    task = asyncio.create_task(capture_handler("camera_record")(1, runtime))
    try:
        await _entered(transport.marker)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        _assert_settled(transport)
        assert owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (0,)
        assert owned.connection.execute("SELECT status FROM actions").fetchone() == (4,)
        actual = json.loads(owned.connection.execute("SELECT last_error_json FROM device_activities").fetchone()[0])
        assert actual["code"] == "adb_directory_failed"
        assert actual["details"]["transport_error"] == "cancelled"
        assert len(transport.calls) == 1
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

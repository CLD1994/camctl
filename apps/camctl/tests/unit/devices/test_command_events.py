"""驱动命令阶段保留逻辑命令、实际预算和已掌握的业务身份。"""

from dataclasses import replace
from decimal import Decimal
import json
from unittest.mock import create_autospec

import pytest

from camctl.logging_runtime.events import event_logging
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogChannel
from camctl.operations.models import AttemptTicket
from camctl.operations.process import LocalExit, RawToolOutcome
from camctl.operations.process import execute_tool
from ..operations.test_process import FakeProcess, _ready
from .test_hasselblad_driver import (
    FakeTransport, _driver as hb_driver, _request as hb_request, host_files,
)
from .test_osmo360ii_capture import _driver, _request, Transport

pytestmark = pytest.mark.asyncio


def _channel():
    return create_autospec(LogChannel, instance=True, spec_set=True)


def _records(channel):
    records = [call.args[0] for call in channel.slog.call_args_list]
    assert all(record.level is LogLevel.INFO for record in records)
    return [json.loads(record.message) for record in records]


async def test_adb_stop_records_actual_command_and_budget():
    request = replace(_request(), operation="stop_recording",
        ticket=AttemptTicket(2, "stop", "7", "stop/7", 9))
    transport = Transport()
    channel = _channel()
    with event_logging(channel):
        result = await _driver(transport).stop(request)
    assert result.error is None
    started, returned = _records(channel)
    assert (started["stage"], returned["stage"]) == ("camera_stop_command", "camera_stop_command")
    assert [record["event"] for record in (started, returned)] == ["started", "returned"]
    assert started["command"] == returned["command"] == transport.commands[0]
    assert returned["timeout_s"] == "10"
    assert returned["run_id"] == 9 and returned["attempt_id"] == 2


async def test_adb_driver_keeps_logical_command_distinct_from_actual_adb_argv():
    class ManagedTransport(Transport):
        async def run(self, spec, stop):
            raw = await super().run(spec, stop)
            process = FakeProcess(raw.output)
            process.stderr = raw.stderr
            process.exit(exit_code=raw.exit.exit_code)
            return await execute_tool(spec, stop=stop, spawner=lambda _: _ready(process))

    transport = ManagedTransport()
    channel = _channel()
    request = _request()
    with event_logging(channel):
        result = await _driver(transport).control(request)
    assert result.error is None
    records = _records(channel)
    tools = [record for record in records if record["stage"] == "external_tool"]
    assert len(tools) == len(transport.commands) * 2
    for index, (command, spec) in enumerate(zip(transport.commands, transport.specs)):
        started, returned = tools[index * 2:index * 2 + 2]
        assert [record["event"] for record in (started, returned)] == ["started", "returned"]
        for record in (started, returned):
            assert record["command"] == command
            assert record["tool_argv"] == list(spec.argv)
            assert record["tool_argv"][:5] == ["adb", "-s", "serial-1", "shell", "-T"]
            assert record["device_id"] == "camera" and record["driver_id"] == "dji-osmo360-ii"
            assert record["activity_id"] == "7" and record["run_id"] == 9 and record["attempt_id"] == 1


async def test_hasselblad_only_capture_command_is_named_camera_start(host_files):
    roots, _, _ = host_files
    transport = FakeTransport()
    channel = _channel()
    request = hb_request()
    request.params["focus"] = {"mode": "MF", "relative_move": 5}
    with event_logging(channel):
        result = await hb_driver(transport, roots).control(request)
    assert result.error is None
    records = _records(channel)
    returned = [record for record in records if record["event"] == "returned"]
    actual_commands = [list(spec.argv[3:]) for spec in transport.calls if spec.argv[3] not in ("list", "info")]
    assert [record["command"] for record in returned] == actual_commands
    assert all(record["stage"] == "camera_setting" for record in returned[:-1])
    assert returned[-1]["stage"] == "camera_start_command"
    assert all(record["timeout_s"] == "180" and record["activity_id"] == "7"
               and record["run_id"] == 9 and record["attempt_id"] == 1 for record in records)
    assert len(records) == len(returned) * 2


async def test_hasselblad_failed_setting_is_returned_without_fabricating_start(host_files):
    roots, _, _ = host_files
    raw = RawToolOutcome(LocalExit(exit_code=255), b"private output", None, None, stderr=b"")
    transport = FakeTransport(outcomes={3: raw})
    channel = _channel()
    with event_logging(channel):
        result = await hb_driver(transport, roots).control(hb_request())
    assert result.error is not None
    records = _records(channel)
    assert [record["event"] for record in records] == ["started", "returned"]
    assert all(record["stage"] == "camera_setting" and "private" not in json.dumps(record) for record in records)


@pytest.mark.parametrize("method,stage", [
    ("recovery_identity", "camera_recovery_identity"),
    ("recovery_reboot", "camera_recovery_reboot"),
])
async def test_recovery_actual_port_records_stage_without_interpreting_call_as_success(method, stage, mocker):
    driver = _driver(Transport())
    request = _request()
    raw = RawToolOutcome(LocalExit(exit_code=7), b"", None, None, stderr=b"")
    mocker.patch.object(driver, "_recovery_raw", autospec=True, return_value=raw)
    channel = _channel()
    with event_logging(channel):
        result = await getattr(driver, method)(request.binding, timeout_s=Decimal("3"))
    records = _records(channel)
    assert [record["event"] for record in records] == ["started", "returned"]
    assert all(record["stage"] == stage and record["device_id"] == "camera"
               and record["timeout_s"] == "3" for record in records)
    if method == "recovery_identity":
        assert result["reason"] == "identity_call_failed"
    else:
        assert result["exit_code"] == 7


async def test_recovery_preparation_records_one_stage_for_real_reapplication(mocker):
    driver = _driver(Transport())
    request = _request()
    raw = RawToolOutcome(LocalExit(exit_code=7), b"", None, None, stderr=b"")
    called = mocker.patch.object(driver, "_recovery_raw", autospec=True, return_value=raw)
    channel = _channel()
    with event_logging(channel):
        result = await driver.recovery_prepare(request.binding, request.params,
            activity_id=7, directories=("/DCIM",), timeout_s=Decimal("3"))
    assert result["ready"] is False
    assert called.call_count == 1
    started, returned = _records(channel)
    assert started["stage"] == returned["stage"] == "camera_recovery_preparation"
    assert returned["activity_id"] == 7 and returned["timeout_s"] == "3"

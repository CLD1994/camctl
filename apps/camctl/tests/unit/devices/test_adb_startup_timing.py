"""区分相机参数设置与实际启动命令的耗时，不执行真实工具。"""

from dataclasses import replace
import json
from unittest.mock import create_autospec

import pytest

from camctl.logging_runtime.performance import performance_logging
from camctl.logging_runtime.service import LogChannel
from .test_osmo360ii_capture import Transport, _driver, _request


def _records(channel):
    return [json.loads(call.args[0].message.removeprefix("性能计时 "))
            for call in channel.slog.call_args_list]


@pytest.mark.asyncio
async def test_setting_and_start_command_have_separate_timings():
    transport = Transport()
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    request = _request("record")
    with performance_logging(channel):
        result = await _driver(transport).control(request)
    assert result.error is None
    records = _records(channel)
    assert len(records) == len(transport.commands)
    assert all(record["stage"] == "camera_setting" for record in records[:-1])
    assert records[-1]["stage"] == "camera_start_command"
    assert all(record["device_id"] == request.binding.device_id
               and record["activity_id"] == request.ticket.target_id
               and record["run_id"] == request.ticket.run_id
               and record["attempt_id"] == request.ticket.attempt_id
               and record["elapsed_ms"] >= 0 for record in records)


@pytest.mark.asyncio
async def test_window_ending_during_settings_does_not_log_a_start_command():
    transport = Transport()
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    request = replace(_request("record"), dispatch_check=lambda:
                      "window_ended" if transport.commands else None)
    with performance_logging(channel):
        result = await _driver(transport).control(request)
    assert result.error["code"] == "window_ended"
    assert len(transport.commands) == 1
    assert [record["stage"] for record in _records(channel)] == ["camera_setting"]

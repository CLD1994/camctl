"""相机参数设置与实际启动命令有各自 INFO 阶段，不执行真实工具。"""

from unittest.mock import AsyncMock

from dataclasses import replace
import json
from unittest.mock import create_autospec

import pytest

from camctl.logging_runtime.performance import performance_logging
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogChannel
from .test_osmo360ii_capture import Transport, _driver, _request


def _records(channel):
    records = [call.args[0] for call in channel.slog.call_args_list]
    assert all(record.level is LogLevel.INFO for record in records)
    return [json.loads(record.message) for record in records]


@pytest.mark.asyncio
async def test_setting_and_start_command_have_separate_stage_pairs():
    transport = Transport()
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    request = _request("record")
    with performance_logging(channel):
        result = await _driver(transport).control(request)
    assert result.error is None
    records = _records(channel)
    assert len(records) == len(transport.commands) * 2
    starts, returns = records[::2], records[1::2]
    assert all(record["event"] == "started" for record in starts)
    assert all(record["event"] == "returned" for record in returns)
    assert all(record["stage"] == "camera_setting" for record in returns[:-1])
    assert returns[-1]["stage"] == "camera_start_command"
    assert [record["command"] for record in starts] == transport.commands
    assert [record["command"] for record in returns] == transport.commands
    assert all(record["device_id"] == request.binding.device_id
               and record["activity_id"] == request.ticket.target_id
               and record["run_id"] == request.ticket.run_id
               and record["attempt_id"] == request.ticket.attempt_id
               and record["timeout_s"] == str(request.timeout_s) for record in records)
    assert all(record["elapsed_ms"] >= 0 for record in returns)


@pytest.mark.asyncio
async def test_window_ending_during_settings_does_not_log_a_start_command():
    transport = Transport()
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    request = replace(_request("record"), dispatch_check=AsyncMock(side_effect=lambda:
                      "window_ended" if transport.commands else None))
    with performance_logging(channel):
        result = await _driver(transport).control(request)
    assert result.error["code"] == "window_ended"
    assert len(transport.commands) == 1
    records = _records(channel)
    assert [record["stage"] for record in records] == ["camera_setting", "camera_setting"]
    assert [record["event"] for record in records] == ["started", "returned"]

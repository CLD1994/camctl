"""真实日志通道、文件处理器和受管工具组合后的命令 INFO。"""

import asyncio
from decimal import Decimal
import json
import os
import sys

import pytest

from camctl.logging_runtime.clh_adapter import FileChannel
from camctl.logging_runtime.events import event_logging, log_context
from camctl.logging_runtime.lifecycle import LogRuntime, close_logging
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.performance import record_performance
from camctl.logging_runtime.service import LogChannel
from camctl.operations.process import ToolSpec, execute_tool

pytestmark = pytest.mark.asyncio


class NeverStop:
    async def requested(self):
        await asyncio.Future()


async def test_info_file_contains_actual_tool_pair_and_filters_debug_timing(tmp_path):
    path = tmp_path / "camctl.log"
    files = FileChannel(path, max_bytes=1 << 20, file_count=2)
    channel = LogChannel(capacity=32, low_watermark=24, high_watermark=28,
                         level=LogLevel.INFO, writer=lambda record: files.write_record(record).appended)
    runtime = LogRuntime(channel, level=LogLevel.INFO, file_closer=files.close)
    channel.start()
    spec = ToolSpec((sys.executable, "-c",
        "import sys;sys.stdout.write('P'*31);sys.stderr.write('E'*37);sys.exit(7)"),
        Decimal("5"), Decimal("1"))
    try:
        with event_logging(channel), log_context(action_id=31, activity_id="7", command=["logical-command", "0"]):
            record_performance("baseline_preparation", 2_280_000)
            result = await execute_tool(spec, stop=NeverStop())
    finally:
        closed = await close_logging(runtime)
    assert closed.listener_stopped and closed.close_error is None
    assert result.exit.exit_code == 7 and result.output == b"P" * 31 and result.stderr == b"E" * 37
    text = path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in text.splitlines()]
    assert len(records) == 2
    assert [record["event"] for record in records] == ["started", "returned"]
    assert records[0]["tool_call_id"] == records[1]["tool_call_id"]
    for record in records:
        assert record["level"] == "INFO" and record["pid"] == os.getpid()
        assert record["logged_at_utc"].endswith("+00:00")
        assert record["stage"] == "external_tool"
        assert record["tool_argv"] == list(spec.argv)
        assert record["command"] == ["logical-command", "0"]
        assert record["activity_id"] == "7" and record["action_id"] == 31
        assert "stdout" not in record and "stderr" not in record
    assert "P" * 31 not in text and "E" * 37 not in text
    assert records[1]["local_exit_code"] == 7 and records[1]["tool_error"] is None

"""真实日志队列和文件通道共同验证级别过滤及统一逐行 JSON。"""

from datetime import datetime, timezone
import json
import logging
import os

import pytest

from camctl.logging_runtime.clh_adapter import FileChannel
from camctl.logging_runtime.diagnostics import (
    diagnostic_scope, error_diagnostics_context, record_error,
)
from camctl.logging_runtime.events import event_logging, log_context, measure_stage
from camctl.logging_runtime.lifecycle import LogRuntime, close_logging
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.performance import record_performance
from camctl.logging_runtime.service import LogChannel, LogRecord
from camctl.logging_runtime.standard_logging import install_standard_logging


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [LogLevel.INFO, LogLevel.DEBUG])
async def test_common_json_preserves_events_messages_and_original_error(tmp_path, level):
    # 组合真实接纳、标准 logging 路由、消费线程和文件写入；队列不进入采样水位。
    path = tmp_path / "camctl.log"
    file_channel = FileChannel(path, max_bytes=1 << 20, file_count=1)
    channel = LogChannel(capacity=64, low_watermark=48, high_watermark=56,
                         level=level,
                         writer=lambda record: file_channel.write_record(record).appended)
    runtime = LogRuntime(channel=channel, level=level, file_closer=file_channel.close)
    root = logging.RootLogger(logging.DEBUG)
    manager = logging.Manager(root)
    root.manager = manager
    fallback = []
    route = install_standard_logging(channel, fallback.append, manager=manager)
    plain_message = '原始文本\n第二行 "quoted"'
    vendor_message = '第三方文本\n完整内容 "quoted"'
    began = datetime.now(timezone.utc)
    channel.start()
    try:
        with event_logging(channel), log_context(action_id=147, device_id="action6"):
            with measure_stage("camera_setting", command=["tool", "setting"], timeout_s="10"):
                record_performance("baseline_preparation", 2_280_000)
        channel.slog(LogRecord(LogLevel.WARNING, plain_message))
        try:
            raise ValueError("original vendor exception")
        except ValueError:
            manager.getLogger("vendor.camera").exception(vendor_message)
        with error_diagnostics_context(channel, fallback.append):
            async with diagnostic_scope():
                record_error(ValueError("original own exception"), stage="device_response",
                             details={"stderr_hex": "70726f626c656dff"})
    finally:
        route.close()
        await close_logging(runtime)
    ended = datetime.now(timezone.utc)

    # 每个物理行都是完整 JSON，文本中的换行不分裂记录。
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for record in records:
        assert record["pid"] == os.getpid()
        timestamp = datetime.fromisoformat(record["logged_at_utc"])
        assert timestamp.utcoffset().total_seconds() == 0
        assert began <= timestamp <= ended
        assert record["stage"]
        assert record["level"] in {"DEBUG", "INFO", "WARNING", "ERROR"}

    events = [record for record in records if record["stage"] == "camera_setting"]
    assert [record["event"] for record in events] == ["started", "returned"]
    assert all(record["level"] == "INFO" and record["action_id"] == 147
               and record["device_id"] == "action6"
               and record["command"] == ["tool", "setting"]
               and record["timeout_s"] == "10" for record in events)
    assert "elapsed_ms" not in events[0] and events[1]["elapsed_ms"] >= 0
    timings = [record for record in records if record["stage"] == "baseline_preparation"]
    if level is LogLevel.DEBUG:
        assert len(timings) == 1
        assert timings[0]["level"] == "DEBUG" and timings[0]["event"] == "timing"
        assert timings[0]["elapsed_ms"] == 2.28
    else:
        assert timings == []

    messages = [record for record in records if record["stage"] == "runtime"]
    assert len(messages) == 2 and messages[0]["message"] == plain_message
    assert messages[0]["level"] == "WARNING"
    assert vendor_message in messages[1]["message"]
    assert "original vendor exception" in messages[1]["message"]
    assert "vendor.camera" in messages[1]["message"]
    assert messages[1]["level"] == "ERROR"
    diagnostics = [record for record in records if record["stage"] == "device_response"]
    assert len(diagnostics) == 1
    assert diagnostics[0]["error"]["message"] == "original own exception"
    assert diagnostics[0]["details"] == {
        "stderr_hex": "70726f626c656dff", "stderr_text": "problem\\xff"}
    assert "message" not in diagnostics[0]
    assert not fallback and not channel.listener_alive
    assert channel.counters.total_dropped() == 0

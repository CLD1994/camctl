"""具体 ADB 文件适配与受管命令的日志身份及缓存边界。"""

import asyncio
from dataclasses import replace
from decimal import Decimal
import json
from unittest.mock import create_autospec

import pytest

from camctl.devices.directory import DirectoryRequest
from camctl.logging_runtime.events import event_logging, log_context
from camctl.logging_runtime.service import LogChannel
from camctl.operations.models import AttemptTicket
from .test_adb_camera_transport import BINDING, Stop, _filesystem, _identity, _source, _POSIX_ONLY

pytestmark = [pytest.mark.asyncio, _POSIX_ONLY]


def _events(channel):
    return [record for call in channel.slog.call_args_list
            if (record := json.loads(call.args[0].message))["stage"] == "external_tool"]


async def test_directory_cache_pages_do_not_claim_new_commands(tmp_path):
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        (tmp_path / name).write_bytes(b"private content")
    fs = _filesystem()
    request = DirectoryRequest(BINDING, (str(tmp_path),), None, 1, Decimal("5"))
    scan = fs.directory_scan(request)
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    pages = []
    with event_logging(channel), log_context(activity_id="7", run_id=9, attempt_id=1):
        try:
            while True:
                result = await scan.read_directory(request, stop=Stop())
                assert result.error is None, result.error
                pages.extend(result.page.items)
                assert len(_events(channel)) == 2
                if result.page.next_cursor is None:
                    break
                request = replace(request, cursor=result.page.next_cursor)
        finally:
            scan.close()
    started, returned = _events(channel)
    assert len(pages) == 3
    assert started["event"] == "started" and returned["event"] == "returned"
    assert started["tool_call_id"] == returned["tool_call_id"]
    assert all(record["device_id"] == BINDING.device_id and record["activity_id"] == "7"
               and record["directory"] == str(tmp_path) for record in (started, returned))


async def test_source_digest_command_keeps_file_operation_and_actual_path(tmp_path):
    path = tmp_path / "source.mp4"
    path.write_bytes(b"source content")
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    with event_logging(channel):
        result = await _filesystem().source_digest(_identity(path), timeout_s=Decimal("5"), stop=Stop())
    assert result.error is None
    started, returned = _events(channel)
    assert all(record["device_id"] == BINDING.device_id and record["file_operation"] == "digest"
               and record["device_path"] == str(path) for record in (started, returned))


async def test_read_stream_command_inherits_copy_and_file_identity_across_thread_wait(tmp_path):
    content = b"original media bytes" * 10_000
    path = tmp_path / "source.mp4"
    path.write_bytes(content)
    fs = _filesystem()
    source = _source(path, len(content))
    ticket = AttemptTicket(2, "read", "9", "copy/9", 18)
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    result = bytearray()
    with event_logging(channel), log_context(action_id=31):
        session = await fs.open_read(source, 0, ticket, idle_timeout_s=Decimal("5"))
        try:
            while True:
                chunk = await asyncio.to_thread(session.read_chunk, 65536)
                assert chunk.error is None
                result.extend(chunk.data or b"")
                if chunk.eof:
                    break
        finally:
            session.request_stop()
            await session.wait_stopped()
    assert result == content
    started, returned = _events(channel)
    assert [record["event"] for record in (started, returned)] == ["started", "returned"]
    assert all(record["copy_id"] == "9" and record["file_id"] == source.file_id
               and record["action_id"] == 31 and record["run_id"] == 18
               and record["attempt_id"] == 2 and record["device_id"] == BINDING.device_id
               for record in (started, returned))
    assert "original media bytes" not in json.dumps((started, returned))

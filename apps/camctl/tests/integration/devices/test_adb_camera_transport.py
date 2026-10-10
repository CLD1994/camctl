"""具体文件适配与真实受管进程、读取会话组合；ADB 通道受约束替换。"""
import asyncio
from dataclasses import replace
from decimal import Decimal
import hashlib
import os
import shlex
import sys
import time

import pytest

from camctl.devices.bindings import DeviceBinding
from camctl.devices.drivers.adb_cameras.commands import CameraModel
from camctl.devices.drivers.adb_cameras.driver import AdbCameraDriver
from camctl.devices.ports import ControlRequest
from camctl.devices.drivers.adb_cameras.filesystem import (
    AdbFilesystem, DirectoryRequest, ShellFileTools,
)
from camctl.devices.drivers.adb_cameras.transport import AdbTransport
from camctl.devices.file_identity import FileIdentity
from camctl.devices.read_session import SourceFile
from camctl.operations.models import AttemptTicket
from camctl.operations.process import LocalExit, RawToolOutcome, ToolSpec
from .adb_camera_fixtures import software_contract

pytestmark = pytest.mark.asyncio
BINDING = DeviceBinding("cam-1", "dji-action6")


class Stop:
    def __init__(self):
        self.event = asyncio.Event()

    async def requested(self):
        await self.event.wait()


class LocalAdbChannel:
    """保留具体适配生成的远端 shell，替换指定 serial 的 ADB 通道。"""
    def __init__(self):
        self.calls = []

    async def run(self, spec, stop):
        return await self._run(spec, stop)

    async def run_stream(self, spec, stop, stdout_sink):
        return await self._run(spec, stop, stdout_sink)

    async def _run(self, spec, stop, stdout_sink=None):
        self.calls.append(spec)
        assert spec.argv[:4] == ("adb", "-s", "serial-1", "exec-out")
        remote = shlex.split(spec.argv[4])
        assert remote[:2] == ["sh", "-c"] and len(remote) == 3
        local = replace(spec, argv=tuple(remote))
        if stdout_sink is not None:
            return await AdbTransport().run_stream(local, stop, stdout_sink)
        return await AdbTransport().run(local, stop)


def _filesystem(transport=None):
    return AdbFilesystem(BINDING, "serial-1", transport or LocalAdbChannel(), tools=ShellFileTools())


def _identity(path):
    return FileIdentity(BINDING, str(path))


async def test_managed_transport_preserves_both_channels_and_process_group():
    script = "import os,sys; print(os.getpgrp()); sys.stderr.write('diagnostic')"
    raw = await AdbTransport().run(ToolSpec((sys.executable, "-c", script), Decimal("5"), Decimal("1")), Stop())
    assert raw.exit == LocalExit(exit_code=0) and raw.error is None
    assert raw.output == f"{os.getpgrp()}\n".encode()
    assert getattr(raw, "stderr", None) == b"diagnostic"


@pytest.mark.parametrize("channel", ["stdout", "stderr"])
async def test_real_large_response_is_explicitly_incomplete(channel):
    script = f"import sys; sys.{channel}.buffer.write(b'x' * (2 << 20))"
    raw = await AdbTransport().run(ToolSpec((sys.executable, "-c", script), Decimal("5"), Decimal("1")), Stop())
    assert raw.exit is not None
    assert raw.error == "output_failed" and "output_limit_exceeded" in raw.output_failure
    assert len(raw.output if channel == "stdout" else raw.stderr) == 1 << 20


async def test_stream_output_error_triggers_stop_before_normal_deadline():
    from camctl.operations.process import execute_tool

    async def reject(data):
        raise OSError("consumer failed")

    script = "import sys,time; sys.stdout.buffer.write(b'data'); sys.stdout.flush(); time.sleep(10)"
    raw = await execute_tool(ToolSpec((sys.executable, "-c", script), Decimal("0.2"), Decimal("0.1")),
                             stop=Stop(), stdout_sink=reject)
    assert raw.error == "output_failed"
    assert raw.exit is not None and "consumer failed" in raw.output_failure


async def test_recursive_paging_preserves_old_new_and_special_paths(tmp_path):
    empty = tmp_path / "a-empty"
    root = tmp_path / "b-files"
    empty.mkdir(); root.mkdir()
    (root / "new-dir").mkdir()
    paths = [root / "old.mp4", root / "a 'quoted'.DNG", root / "b\nnewline.jpg",
             root / "new-dir" / "new.mp4"]
    for path in paths:
        path.write_bytes(b"data")
    fs = _filesystem()
    cursor, collected, pages = None, [], []
    while True:
        result = await fs.read_directory(DirectoryRequest(BINDING, (str(empty), str(root)), cursor, 2, Decimal("5")), stop=Stop())
        assert result.error is None, result.error
        pages.append(result.page)
        collected.extend(entry.path for entry in result.page.items)
        cursor = result.page.next_cursor
        if cursor is None:
            break
    assert pages[0].items == () and pages[0].next_cursor is not None
    assert pages[-1].items and pages[-1].next_cursor is None
    assert collected == sorted(map(str, paths))
    assert all(len(page.items) <= 2 for page in pages)


async def test_directory_tool_failure_is_not_a_reliable_empty_page(tmp_path):
    result = await _filesystem().read_directory(
        DirectoryRequest(BINDING, (str(tmp_path / "missing"),), None, 128, Decimal("5")), stop=Stop())
    assert result.page is None and result.error is not None
    assert result.outcome.exit.exit_code != 0
    assert result.outcome.stderr


async def test_metadata_and_source_digest_come_from_remote_tool(tmp_path):
    path = tmp_path / "a 'quoted'\nfile"
    path.write_bytes(b"remote source content")
    fs = _filesystem()
    metadata = await fs.metadata(_identity(path), timeout_s=Decimal("5"), stop=Stop())
    digest = await fs.source_digest(_identity(path), timeout_s=Decimal("5"), stop=Stop())
    assert metadata.error is digest.error is None
    assert metadata.value == 21
    assert digest.value == hashlib.sha256(b"remote source content").hexdigest()


async def test_concrete_driver_file_ports_preserve_original_identity_and_content(tmp_path):
    content = b"camera-source" * 100000
    path, other = tmp_path / "source ' \n.mp4", tmp_path / "keep.mp4"
    path.write_bytes(content)
    other.write_bytes(b"keep")
    channel = LocalAdbChannel()
    driver = AdbCameraDriver(software_contract(CameraModel.ACTION6), {
        "cam-1": {"driver": CameraModel.ACTION6, "adb": {"serial": "serial-1"}}}, channel,
        terminate_grace_s=Decimal("1"), monotonic_ns=time.monotonic_ns)
    directory = await driver.read_directory(DirectoryRequest(BINDING, (str(tmp_path),), None, 128, Decimal("5")), stop=Stop())
    assert directory.error is None and {entry.path for entry in directory.page.items} == {str(path), str(other)}
    locator = _identity(path).as_json()
    digest = await driver.digest(ControlRequest("digest", BINDING, {"file_id": "9", "size_bytes": len(content), "locator": locator}))
    assert digest.error is None and digest.observations[0].data == {
        "file_id": "9", "sha256": hashlib.sha256(content).hexdigest()}
    session = await driver.open_read(SourceFile("9", locator, len(content)), 65535,
        AttemptTicket(1, "read", "9", "copy/9", 1), idle_timeout_s=Decimal("5"))
    chunks = bytearray()
    while True:
        chunk = await asyncio.to_thread(session.read_chunk, 65536)
        assert chunk.error is None
        chunks.extend(chunk.data or b"")
        if chunk.eof:
            break
    end = await session.wait_stopped()
    assert end.stopped and end.error is None and chunks == content[65535:]
    assert path.read_bytes() == content
    deleted = await driver.delete(ControlRequest("delete_file", BINDING, {"locator": locator},
        AttemptTicket(1, "delete", "12", "delete/12", 2), Decimal("5")))
    assert deleted.error is None and deleted.observations[0].data == {"cleanup_item_id": "12"}
    assert not path.exists() and other.read_bytes() == b"keep"


async def test_missing_source_retains_access_error_without_host_digest(tmp_path):
    fs = _filesystem()
    result = await fs.source_digest(_identity(tmp_path / "missing"), timeout_s=Decimal("5"), stop=Stop())
    assert result.value is None and result.error is not None and result.outcome.stderr


async def test_empty_file_is_present_and_access_failure_remains_unknown(tmp_path):
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    fs = _filesystem()
    present = await fs.exists(_identity(empty), timeout_s=Decimal("5"), stop=Stop())
    unknown = await fs.exists(_identity(tmp_path / "missing"), timeout_s=Decimal("5"), stop=Stop())
    assert present.value is True and present.error is None
    assert unknown.value is None and unknown.error is not None


async def test_explicit_delete_only_removes_requested_file(tmp_path):
    target, other = tmp_path / "quote ' \n target", tmp_path / "other"
    target.write_bytes(b"delete"); other.write_bytes(b"keep")
    result = await _filesystem().delete_file(_identity(target), timeout_s=Decimal("5"), stop=Stop())
    assert result.value is True and result.error is None
    assert not target.exists() and other.read_bytes() == b"keep"


async def test_read_session_preserves_unaligned_offset_and_actual_end(tmp_path):
    content = bytes(range(256)) * 10000
    path = tmp_path / "source ' \n.bin"
    path.write_bytes(content)
    fs = _filesystem()
    source = SourceFile("9", _identity(path).as_json(), len(content))
    ticket = AttemptTicket(1, "read", "9", "copy/9", 1)
    session = await fs.open_read(source, 65535, ticket, idle_timeout_s=Decimal("5"))
    result = bytearray()
    while True:
        chunk = await asyncio.to_thread(session.read_chunk, 65536)
        assert chunk.error is None
        result.extend(chunk.data or b"")
        if chunk.eof:
            break
    assert result == content[65535:]
    end = await session.wait_stopped()
    assert end.stopped and end.error is None and end.bytes_read == len(content) - 65535
    assert path.read_bytes() == content


async def test_read_shorter_than_fixed_source_is_a_failure(tmp_path):
    path = tmp_path / "short"
    path.write_bytes(b"short")
    fs = _filesystem()
    source = SourceFile("9", _identity(path).as_json(), 20)
    session = await fs.open_read(source, 0, AttemptTicket(1, "read", "9", "copy/9", 1), idle_timeout_s=Decimal("5"))
    first = await asyncio.to_thread(session.read_chunk, 20)
    assert first.data == b"short" and not first.eof
    with pytest.raises(OSError):
        await asyncio.to_thread(session.read_chunk, 20)
    end = await session.wait_stopped()
    assert end.error == "failed" and end.bytes_read == 5


async def test_wrong_original_binding_never_invokes_tool(tmp_path):
    channel = LocalAdbChannel()
    identity = FileIdentity(DeviceBinding("cam-2", "dji-action6"), str(tmp_path / "source"))
    with pytest.raises(ValueError):
        await _filesystem(channel).metadata(identity, timeout_s=Decimal("5"), stop=Stop())
    assert channel.calls == []


async def test_stop_request_waits_for_actual_read_call_return(tmp_path):
    class HeldCall:
        def __init__(self):
            self.entered, self.stopped, self.release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def run_stream(self, spec, stop, stdout_sink):
            self.entered.set()
            await stop.requested()
            self.stopped.set()
            await self.release.wait()
            return RawToolOutcome(LocalExit(exit_code=0), b"", "cancelled", Decimal("1"))

    transport = HeldCall()
    fs = _filesystem(transport)
    source = SourceFile("9", _identity(tmp_path / "source").as_json(), 100)
    session = await fs.open_read(source, 0, AttemptTicket(1, "read", "9", "copy/9", 1), idle_timeout_s=Decimal("5"))
    reading = asyncio.create_task(asyncio.to_thread(session.read_chunk, 10))
    try:
        await asyncio.wait_for(transport.entered.wait(), 5)
        session.request_stop()
        await asyncio.wait_for(transport.stopped.wait(), 5)
        assert session.poll_stopped() is None and not reading.done()
        transport.release.set()
        chunk = await asyncio.wait_for(reading, 5)
        end = await asyncio.wait_for(session.wait_stopped(), 5)
        assert chunk.error == end.error == "stopped"
        assert end.bytes_read == 0 and end.stopped
    finally:
        transport.release.set()
        await asyncio.wait_for(asyncio.gather(reading, return_exceptions=True), 5)

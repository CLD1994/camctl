"""下载副本的普通文件校验、有界缓冲与实际线程取消收场。"""

import asyncio
import errno
import hashlib
import os
from pathlib import Path
import sys
import threading

import pytest

from camctl.host_files.downloads import DownloadWorkspace, download_path, recover_download
from camctl.host_files.segments import DEFAULT_CHUNK_SIZE_BYTES
from camctl.operations.models import AttemptTicket

TICKET = AttemptTicket(1, "read", "22", "read/22", 31)


@pytest.fixture
def staging(tmp_path):
    path = tmp_path / "staging"
    path.mkdir()
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["two_files", "directory", "symlink"])
async def test_download_must_be_a_single_regular_file(staging, invalid):
    workspace = DownloadWorkspace(staging, TICKET)
    path = await workspace.prepare()
    payload = b"content"
    if invalid == "two_files":
        (path / "one").write_bytes(payload)
        (path / "two").write_bytes(payload)
    elif invalid == "directory":
        (path / "one").mkdir()
    else:
        outside = staging / "outside"
        outside.write_bytes(payload)
        (path / "one").symlink_to(outside)
    try:
        with pytest.raises((ValueError, OSError)):
            await workspace.open_verified(len(payload), hashlib.sha256(payload).hexdigest(), 0)
    finally:
        await workspace.cleanup()
    assert not path.exists()
    if invalid == "symlink":
        assert outside.read_bytes() == payload


@pytest.mark.asyncio
async def test_stream_caps_read_buffer_and_duplicate_owner_cannot_release_first_claim(staging):
    workspace = DownloadWorkspace(staging, TICKET)
    path = await workspace.prepare()
    duplicate = DownloadWorkspace(staging, TICKET)
    with pytest.raises(ValueError):
        await duplicate.prepare()
    await duplicate.cleanup()
    assert not await recover_download(staging, TICKET)
    payload = b"x" * (DEFAULT_CHUNK_SIZE_BYTES + 11)
    (path / "file").write_bytes(payload)
    stream = await workspace.open_verified(len(payload), hashlib.sha256(payload).hexdigest(), 0)
    try:
        assert len(await asyncio.to_thread(stream.read, len(payload))) == DEFAULT_CHUNK_SIZE_BYTES
        assert await asyncio.to_thread(stream.read, len(payload)) == b"x" * 11
    finally:
        await asyncio.to_thread(stream.close)
    assert not path.exists()


@pytest.mark.asyncio
async def test_cancelled_verification_waits_for_actual_file_worker_then_closes_and_reclaims(staging, monkeypatch):
    workspace = DownloadWorkspace(staging, TICKET)
    path = await workspace.prepare()
    payload = b"complete"
    (path / "file").write_bytes(payload)
    entered, release = threading.Event(), threading.Event()
    original = os.read
    def blocked(fd, limit):
        if not entered.is_set():
            entered.set()
            assert release.wait(3)
        return original(fd, limit)
    monkeypatch.setattr(os, "read", blocked)
    task = asyncio.create_task(workspace.open_verified(len(payload), hashlib.sha256(payload).hexdigest(), 0))
    try:
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert path.exists()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not path.exists()


def _close_test_descriptor(fd, identity):
    """只关闭测试注错后仍指向原文件的描述符。"""
    try:
        current = os.fstat(fd)
    except OSError as error:
        if error.errno != errno.EBADF:
            raise
    else:
        assert (current.st_dev, current.st_ino) == identity
        os.close(fd)


async def _unconfirmed_close_scenario(staging, cancel):
    monkeypatch = pytest.MonkeyPatch()
    workspace = DownloadWorkspace(staging, TICKET)
    path = await workspace.prepare()
    payload = b"complete"
    (path / "file").write_bytes(payload)
    stat = (path / "file").stat()
    identity = (stat.st_dev, stat.st_ino)
    opened = []
    original_close, original_open, original_read = os.close, os.open, os.read
    entered, release = threading.Event(), threading.Event()
    close_error = OSError("close not confirmed")
    def record_open(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        if str(args[0]) == str(path / "file"):
            opened.append(fd)
        return fd
    def fail_close(fd):
        if fd in opened:
            raise close_error
        return original_close(fd)
    def blocked_read(fd, size):
        if cancel and fd in opened and not entered.is_set():
            entered.set()
            assert release.wait(3)
        return original_read(fd, size)
    monkeypatch.setattr(os, "open", record_open)
    monkeypatch.setattr(os, "close", fail_close)
    monkeypatch.setattr(os, "read", blocked_read)
    digest = hashlib.sha256(payload if cancel else b"replaced").hexdigest()
    task = asyncio.create_task(workspace.open_verified(len(payload), digest, 0))
    try:
        if cancel:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0.001)
            task.cancel("cancel during verification")
            release.set()
        with pytest.raises(BaseException) as failure:
            await task
        assert "close not confirmed" in repr(failure.value) + repr(failure.value.__cause__)
        if cancel:
            assert isinstance(failure.value, asyncio.CancelledError)
            assert failure.value.__cause__ is close_error
        else:
            assert isinstance(failure.value, ExceptionGroup)
            assert any(error is close_error for error in failure.value.exceptions)
        with pytest.raises(BaseException):
            await workspace.cleanup()
        assert os.fstat(opened[0]).st_size == len(payload)
        assert path.exists(), "关闭未确认不能删除原目录"
        assert not await recover_download(staging, TICKET), "拥有者不能因关闭抛错被释放"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        monkeypatch.undo()
        for fd in opened:
            _close_test_descriptor(fd, identity)


async def _stream_close_failure_scenario(staging):
    from decimal import Decimal
    from camctl.devices.read_session import ReadSession, SourceFile
    monkeypatch = pytest.MonkeyPatch()
    workspace = DownloadWorkspace(staging, TICKET)
    path = await workspace.prepare()
    payload = b"complete"
    (path / "file").write_bytes(payload)
    stat = (path / "file").stat()
    identity = (stat.st_dev, stat.st_ino)
    opened = []
    original_open, original_close = os.open, os.close

    def record_open(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        if str(args[0]) == str(path / "file"):
            opened.append(fd)
        return fd

    monkeypatch.setattr(os, "open", record_open)
    stop_requested = False
    try:
        stream = await workspace.open_verified(len(payload), hashlib.sha256(payload).hexdigest(), 0)
        assert len(opened) == 1
        fd = opened[0]
        session = ReadSession(SourceFile("fixed", {}, len(payload)), 0, stream, Decimal("10"))
        calls = []
        close_error = OSError("close outcome unknown")

        def fail_close(value):
            if value == fd:
                calls.append(value)
                raise close_error
            return original_close(value)

        monkeypatch.setattr(os, "close", fail_close)
        stop_requested = True
        session.request_stop()
        with pytest.raises(OSError, match="outcome unknown") as first:
            await session.wait_stopped()
        with pytest.raises(OSError, match="outcome unknown") as second:
            session.poll_stopped()
        with pytest.raises(OSError, match="outcome unknown") as third:
            await asyncio.to_thread(stream.close)
        assert first.value is second.value is third.value is close_error
        assert calls == [fd]
        assert os.fstat(fd).st_size == len(payload)
        assert path.exists() and not await recover_download(staging, TICKET)
    finally:
        if stop_requested:
            await asyncio.gather(session.wait_stopped(), return_exceptions=True)
        monkeypatch.undo()
        for fd in opened:
            _close_test_descriptor(fd, identity)


async def _run_close_scenario(staging, scenario):
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(Path(__file__).resolve()), scenario, str(staging),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
    assert process.returncode == 0, (
        f"下载关闭场景 {scenario} 子进程退出 {process.returncode}\n"
        f"stdout: {stdout.decode(errors='replace')}\n"
        f"stderr: {stderr.decode(errors='replace')}")
    path = download_path(staging, TICKET)
    assert path.exists()
    assert await recover_download(staging, TICKET)
    assert not path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_unconfirmed_close_retains_descriptor_directory_and_owner(staging, cancel):
    await _run_close_scenario(staging, "cancel" if cancel else "verification")


@pytest.mark.asyncio
async def test_stream_close_failure_never_confirms_read_stopped_or_releases_owner(staging):
    await _run_close_scenario(staging, "stream")


@pytest.mark.asyncio
async def test_closed_file_with_directory_delete_failure_can_be_recovered(staging, monkeypatch):
    from camctl.host_files import downloads
    workspace = DownloadWorkspace(staging, TICKET)
    path = await workspace.prepare()
    payload = b"complete"
    (path / "file").write_bytes(payload)
    stream = await workspace.open_verified(len(payload), hashlib.sha256(payload).hexdigest(), 0)
    original = downloads.shutil.rmtree
    def fail_remove(_path):
        raise PermissionError("delete denied after file closed")
    monkeypatch.setattr(downloads.shutil, "rmtree", fail_remove)
    with pytest.raises(PermissionError):
        await asyncio.to_thread(stream.close)
    assert workspace.files_closed and path.exists()
    monkeypatch.setattr(downloads.shutil, "rmtree", original)
    assert await recover_download(staging, TICKET)
    assert not path.exists()


if __name__ == "__main__":
    scenario, staging_arg = sys.argv[1:]
    if scenario in ("verification", "cancel"):
        asyncio.run(_unconfirmed_close_scenario(Path(staging_arg), scenario == "cancel"))
    elif scenario == "stream":
        asyncio.run(_stream_close_failure_scenario(Path(staging_arg)))
    else:
        raise ValueError(f"未知下载关闭场景: {scenario}")

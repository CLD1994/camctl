"""下载副本的普通文件校验、有界缓冲与实际线程取消收场。"""

import asyncio
import hashlib
import os
from pathlib import Path
import threading

import pytest

from camctl.host_files.downloads import DownloadWorkspace, recover_download
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


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_unconfirmed_close_retains_descriptor_directory_and_owner(staging, monkeypatch, cancel):
    workspace = DownloadWorkspace(staging, TICKET)
    path = await workspace.prepare()
    payload = b"complete"
    (path / "file").write_bytes(payload)
    opened = []
    original_close, original_open, original_read = os.close, os.open, os.read
    entered, release = threading.Event(), threading.Event()
    def record_open(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        if str(args[0]) == str(path / "file"):
            opened.append(fd)
        return fd
    def fail_close(fd):
        if fd in opened:
            raise OSError("close not confirmed")
        return original_close(fd)
    def blocked_read(fd, size):
        if cancel and not entered.is_set():
            entered.set()
            assert release.wait(3)
        return original_read(fd, size)
    monkeypatch.setattr(os, "open", record_open)
    monkeypatch.setattr(os, "close", fail_close)
    monkeypatch.setattr(os, "read", blocked_read)
    digest = hashlib.sha256(payload if cancel else b"replaced").hexdigest()
    task = asyncio.create_task(workspace.open_verified(len(payload), digest, 0))
    if cancel:
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        task.cancel("cancel during verification")
        release.set()
    try:
        with pytest.raises(BaseException) as failure:
            await task
        assert "close not confirmed" in repr(failure.value) + repr(failure.value.__cause__)
        with pytest.raises(BaseException):
            await workspace.cleanup()
        assert os.fstat(opened[0]).st_size == len(payload)
        assert path.exists(), "关闭未确认不能删除原目录"
        assert not await recover_download(staging, TICKET), "拥有者不能因关闭抛错被释放"
    finally:
        # 注入使 close 没有执行；测试知道其 fd 未复用，并自行结束测试资源。
        original_close(opened[0])
        from camctl.host_files import downloads
        with downloads._ACTIVE_LOCK:
            downloads._ACTIVE.pop(path, None)


@pytest.mark.asyncio
async def test_stream_close_failure_never_confirms_read_stopped_or_releases_owner(staging, monkeypatch):
    from decimal import Decimal
    from camctl.devices.read_session import ReadSession, SourceFile
    workspace = DownloadWorkspace(staging, TICKET)
    path = await workspace.prepare()
    payload = b"complete"
    (path / "file").write_bytes(payload)
    stream = await workspace.open_verified(len(payload), hashlib.sha256(payload).hexdigest(), 0)
    session = ReadSession(SourceFile("fixed", {}, len(payload)), 0, stream, Decimal("10"))
    fd = workspace.fd
    original_close = os.close
    calls = []
    def fail_close(value):
        if value == fd:
            calls.append(value)
            raise OSError("close outcome unknown")
        return original_close(value)
    monkeypatch.setattr(os, "close", fail_close)
    try:
        session.request_stop()
        with pytest.raises(OSError, match="outcome unknown"):
            await session.wait_stopped()
        with pytest.raises(OSError, match="outcome unknown"):
            session.poll_stopped()
        with pytest.raises(OSError, match="outcome unknown"):
            await asyncio.to_thread(stream.close)
        assert calls == [fd]
        assert path.exists() and not await recover_download(staging, TICKET)
    finally:
        original_close(fd)
        from camctl.host_files import downloads
        with downloads._ACTIVE_LOCK:
            downloads._ACTIVE.pop(path, None)


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

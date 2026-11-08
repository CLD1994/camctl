"""媒体工具、默认线程池与共享文件责任的真实组合。"""

import asyncio
import os
import signal
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from camctl.host_files import media
from camctl.host_files.tasks import FileTaskExecutor, FileTaskId, FileTask, FileTaskError
from camctl.session.supervision import Supervisor
from integration.host_files.test_media import _setup, _tool, _REPAIR_BODY

pytestmark = pytest.mark.asyncio


class Owner:
    def __init__(self):
        self.tasks = []

    async def take_over(self, task):
        self.tasks.append(task)


async def test_stop_during_observation_prevents_new_hash_and_sync(tmp_path, monkeypatch):
    input_ref, output_ref, roots, _, output_path = _setup(tmp_path)
    tool = _tool(tmp_path, "observe", _REPAIR_BODY)
    started, release = threading.Event(), threading.Event()
    original_stat, original_open = os.stat, os.open

    def observe(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if path == output_path:
            started.set()
            assert release.wait(5)
        return result

    def open_file(path, *args, **kwargs):
        if path == output_path:
            raise AssertionError("停止后不得打开成品进行摘要或同步")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", observe)
    monkeypatch.setattr(os, "open", open_file)
    executor, identity = FileTaskExecutor(Supervisor()), FileTaskId("observe")
    waiting = asyncio.create_task(media.repair_media(
        input_ref, output_ref, roots, media.RepairRequest(ffmpeg=tool),
        executor=executor, task_id=identity, owner=Owner(),
    ))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        executor.request_stop(identity)
        assert executor.unfinished_files() == (11, 12)
    finally:
        release.set()
    result = await waiting
    assert result.error is None
    assert result.value.size_bytes == 259
    assert result.value.postprocessing_stopped is True
    assert result.value.checksum is None and result.value.synchronization is None
    assert result.value.complete is False


async def test_queued_file_collection_keeps_ownership_after_waiter_cancel(tmp_path, monkeypatch):
    input_ref, output_ref, roots, _, output_path = _setup(tmp_path)
    tool = _tool(tmp_path, "queued", _REPAIR_BODY)
    loop = asyncio.get_running_loop()
    occupied, queued = asyncio.Event(), asyncio.Event()
    release = threading.Event()

    class ObservedPool(ThreadPoolExecutor):
        submissions = 0

        def submit(self, fn, /, *args, **kwargs):
            future = super().submit(fn, *args, **kwargs)
            self.submissions += 1
            if self.submissions == 2:
                queued.set()
            return future

    def occupy():
        loop.call_soon_threadsafe(occupied.set)
        assert release.wait(5)

    original_open = os.open

    def open_file(path, *args, **kwargs):
        if path == output_path:
            raise AssertionError("排队期间停止后不得打开成品进行摘要或同步")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_file)
    supervisor, owner = Supervisor(), Owner()
    executor, identity = FileTaskExecutor(supervisor), FileTaskId("queued")
    with ObservedPool(max_workers=1) as pool:
        loop.set_default_executor(pool)
        blocker = loop.run_in_executor(None, occupy)
        try:
            async with asyncio.timeout(5):
                await occupied.wait()
                waiting = asyncio.create_task(media.repair_media(
                    input_ref, output_ref, roots, media.RepairRequest(ffmpeg=tool),
                    executor=executor, task_id=identity, owner=owner,
                ))
                await queued.wait()
                waiting.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiting
                await supervisor.drain_required()
                handle = owner.tasks[0].pending
                handle.request_stop()
                assert executor.unfinished_files() == (11, 12)
                release.set()
                result = await handle.wait()
                assert result.error is None
                assert result.value.size_bytes == 259
                assert result.value.postprocessing_stopped is True
                assert result.value.checksum is None and result.value.synchronization is None
                assert executor.unfinished_files() == ()
        finally:
            release.set()
            await blocker


@pytest.mark.parametrize("phase", ["hash", "sync"])
@pytest.mark.parametrize("fails", [False, True])
async def test_cancel_during_file_work_keeps_lease_and_result(tmp_path, monkeypatch, phase, fails):
    input_ref, output_ref, roots, _, output_path = _setup(tmp_path)
    tool = _tool(tmp_path, "repair", _REPAIR_BODY)
    started, release = threading.Event(), threading.Event()
    thread_ids = []
    original_open, original_close = os.open, os.close
    original_read, original_fsync = os.read, os.fsync
    target_fds = set()

    def open_file(path, *args, **kwargs):
        fd = original_open(path, *args, **kwargs)
        if path == output_path:
            target_fds.add(fd)
        return fd

    def gate():
        thread_ids.append(threading.get_ident())
        started.set()
        assert release.wait(10), "测试未释放文件调用"
        if fails:
            raise OSError("injected")

    def close_file(fd):
        target_fds.discard(fd)
        return original_close(fd)

    def read_file(fd, size):
        if phase == "hash" and fd in target_fds and not started.is_set():
            gate()
        return original_read(fd, size)

    def sync_file(fd):
        if phase == "sync" and fd in target_fds and not started.is_set():
            gate()
        return original_fsync(fd)

    monkeypatch.setattr(os, "open", open_file)
    monkeypatch.setattr(os, "close", close_file)
    monkeypatch.setattr(os, "read", read_file)
    monkeypatch.setattr(os, "fsync", sync_file)
    supervisor, owner = Supervisor(), Owner()
    executor = FileTaskExecutor(supervisor)
    identity = FileTaskId("repair-12")
    waiting = asyncio.create_task(media.repair_media(
        input_ref, output_ref, roots, media.RepairRequest(ffmpeg=tool),
        executor=executor, task_id=identity, owner=owner,
    ))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        assert thread_ids != [threading.get_ident()]
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert executor.unfinished_files() == (11, 12)
        for file_id in (11, 12):
            with pytest.raises(FileTaskError):
                await executor.run_file_task(
                    FileTask(FileTaskId(f"delete-{file_id}"), file_id, "delete", "cleanup", lambda stop: None),
                    owner,
                )
        await supervisor.drain_required()
        handle = owner.tasks[0].pending
        handle.request_stop()
    finally:
        release.set()
    actual = await handle.wait()
    assert actual.ran is True and actual.error is None
    artifact = actual.value
    assert artifact.exists is True and artifact.size_bytes == 259
    assert executor.unfinished_files() == ()
    if phase == "hash":
        assert artifact.postprocessing_stopped is True
        assert artifact.synchronization is None
        assert artifact.complete is (not fails)
    else:
        assert artifact.complete is True
        assert artifact.file_synced is (not fails)
        assert artifact.synchronization is not None
    if fails:
        assert "injected" in artifact.error


@pytest.mark.parametrize("entry", ["probe", "repair"])
async def test_waiter_cancel_keeps_tool_until_owner_stops_it(tmp_path, entry):
    input_ref, output_ref, roots, _, _ = _setup(tmp_path)
    tool = _tool(tmp_path, "waiting-tool", """
from pathlib import Path
import time
Path(__file__).with_suffix('.ready').write_text('ready')
time.sleep(30)
""")
    supervisor, owner = Supervisor(), Owner()
    executor = FileTaskExecutor(supervisor)
    identity = FileTaskId("media")
    kwargs = dict(executor=executor, task_id=identity, owner=owner)
    operation = (media.probe_media(input_ref, roots, media.ProbeRequest(ffprobe=tool), **kwargs)
                 if entry == "probe" else media.repair_media(
                     input_ref, output_ref, roots, media.RepairRequest(ffmpeg=tool), **kwargs))
    waiting = asyncio.create_task(operation)
    try:
        async with asyncio.timeout(5):
            while not (tmp_path / "waiting-tool.ready").exists():
                await asyncio.sleep(0.01)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert executor.unfinished_files() == ((11,) if entry == "probe" else (11, 12))
        await supervisor.drain_required()
        handle = owner.tasks[0].pending
        handle.request_stop()
        actual = await handle.wait()
        assert actual.ran is True and actual.error is None
        assert "cancelled" in actual.value.error
        assert executor.unfinished_files() == ()
    finally:
        executor.request_stop(identity)
        if not waiting.done():
            await waiting


@pytest.mark.parametrize("entry", ["probe", "repair"])
@pytest.mark.parametrize("output_fails", [False, True])
@pytest.mark.skipif(sys.platform == "win32", reason="需要 POSIX 信号注入")
async def test_signal_errors_keep_media_files_until_real_exit(tmp_path, monkeypatch, entry, output_fails):
    input_ref, output_ref, roots, input_path, _ = _setup(tmp_path)
    tool = _tool(tmp_path, "signal-errors", """
from pathlib import Path
import sys, time
marker = Path(__file__)
if '-i' in sys.argv:
    Path(sys.argv[-1]).write_bytes(b'partial')
marker.with_suffix('.ready').write_text('ready')
print('known-prefix', flush=True)
while not marker.with_suffix('.release').exists():
    time.sleep(0.01)
sys.exit(7)
""")
    killed = asyncio.Event()
    signals = []
    child = None
    original_spawn = asyncio.create_subprocess_exec
    original_kill = os.kill

    async def capture_spawn(*args, **kwargs):
        nonlocal child
        child = await original_spawn(*args, **kwargs)
        return child

    def deny_signal(pid, sig):
        if child is not None and pid == child.pid and sig in (signal.SIGTERM, signal.SIGKILL):
            signals.append(sig)
            if sig == signal.SIGKILL:
                killed.set()
                raise PermissionError("kill denied")
            raise PermissionError("terminate denied")
        return original_kill(pid, sig)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_spawn)
    monkeypatch.setattr(os, "kill", deny_signal)
    if output_fails:
        original_read = asyncio.StreamReader.read
        stdout_read = False

        async def broken_pipe(reader, size):
            nonlocal stdout_read
            if child is not None and reader is child.stdout:
                if stdout_read:
                    raise OSError("pipe read failed")
                chunk = await original_read(reader, size)
                if chunk:
                    stdout_read = True
                return chunk
            return await original_read(reader, size)

        monkeypatch.setattr(asyncio.StreamReader, "read", broken_pipe)
    supervisor, owner = Supervisor(), Owner()
    executor = FileTaskExecutor(supervisor)
    identity = FileTaskId("signal-errors")
    kwargs = dict(executor=executor, task_id=identity, owner=owner)
    operation = (media.probe_media(input_ref, roots, media.ProbeRequest(ffprobe=tool), **kwargs)
                 if entry == "probe" else media.repair_media(
                     input_ref, output_ref, roots, media.RepairRequest(ffmpeg=tool), **kwargs))
    waiting = asyncio.create_task(operation)
    received = None
    try:
        async with asyncio.timeout(15):
            while not (tmp_path / "signal-errors.ready").exists():
                await asyncio.sleep(0.01)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            await supervisor.drain_required()
            handle = owner.tasks[0].pending
            handle.request_stop()
            received = asyncio.create_task(handle.wait())
            await killed.wait()
            assert signals == [signal.SIGTERM, signal.SIGKILL]
            assert child.returncode is None
            await asyncio.sleep(0)
            assert not received.done()
            assert executor.unfinished_files() == ((11,) if entry == "probe" else (11, 12))
            with pytest.raises(FileTaskError):
                await executor.run_file_task(
                    FileTask(FileTaskId("delete-input"), 11, "delete", "cleanup",
                             lambda stop: input_path.unlink()), owner,
                )
            assert input_path.read_bytes() == b"x" * 250
    finally:
        (tmp_path / "signal-errors.release").write_text("release")
        async with asyncio.timeout(5):
            if received is None:
                await asyncio.gather(waiting, return_exceptions=True)
            else:
                actual = await received
    assert child.returncode == 7
    assert input_path.read_bytes() == b"x" * 250
    assert actual.error is None
    assert "exit=7" in actual.value.error
    assert "terminate denied" in actual.value.error and "kill denied" in actual.value.error
    assert "tool_unavailable" not in actual.value.error
    if output_fails:
        assert "pipe read failed" in actual.value.error
    if entry == "repair":
        assert actual.value.exists is True and actual.value.size_bytes == 7
        assert actual.value.complete is False
    assert executor.unfinished_files() == ()

"""媒体工具、默认线程池与共享文件责任的真实组合。"""

import asyncio
import errno
import threading

import pytest

from camctl.host_files import media
from camctl.host_files.io import DirectorySyncStage
from camctl.host_files.tasks import FileTaskExecutor, FileTaskId, FileTask, FileTaskError
from camctl.session.supervision import Supervisor
from integration.host_files.test_media import _setup, _tool, _REPAIR_BODY

pytestmark = pytest.mark.asyncio


class Owner:
    def __init__(self):
        self.tasks = []

    async def take_over(self, task):
        self.tasks.append(task)


@pytest.mark.parametrize("phase", ["hash", "sync"])
@pytest.mark.parametrize("fails", [False, True])
async def test_cancel_during_file_work_keeps_lease_and_result(tmp_path, monkeypatch, phase, fails):
    input_ref, output_ref, roots, _, _ = _setup(tmp_path)
    tool = _tool(tmp_path, "repair", _REPAIR_BODY)
    started, release = threading.Event(), threading.Event()
    thread_ids = []
    original = media._file_digest if phase == "hash" else media._file_sync

    def gated(ref, roots):
        thread_ids.append(threading.get_ident())
        started.set()
        assert release.wait(10), "测试未释放文件调用"
        if fails:
            if phase == "hash":
                from camctl.host_files.io import HashResult
                return HashResult(None, None, "read_failed: injected")
            from camctl.host_files.io import SyncResult
            return SyncResult(False, DirectorySyncStage.NOT_ATTEMPTED, "fsync_failed: injected")
        return original(ref, roots)

    monkeypatch.setattr(media, "_file_digest" if phase == "hash" else "_file_sync", gated)
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

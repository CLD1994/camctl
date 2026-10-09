"""清理通过真实文件执行器调度，并保留实际文件拥有者。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import threading

import pytest

from camctl.host_files.tasks import FileTask, FileTaskExecutor, FileTaskId
from camctl.outputs import work_files
from camctl.outputs.work_files import WorkFileSingleOutcome, clean_one_work_file
from camctl.session.supervision import Supervisor

from .test_work_files import (
    _cancel_delivery, _context, _file_state, _release_ok, _work_path,
    local_read, read_targets, work_env,  # noqa: F401
)


class FileOwner:
    async def take_over(self, task):
        await task.pending.wait()


@pytest.mark.asyncio
async def test_cleanup_observation_and_unlink_execute_outside_event_loop(work_env, monkeypatch):
    owned, roots, qualification = work_env
    _cancel_delivery(owned, qualification.delivery_id)
    _release_ok(owned, qualification.target_file_id)
    main_thread = threading.get_ident()
    actual_threads = []
    observe, remove = work_files._observe_work_file, work_files._remove_work_file

    def record_observe(path):
        actual_threads.append(("stat", threading.get_ident()))
        return observe(path)

    def record_remove(path):
        actual_threads.append(("unlink", threading.get_ident()))
        return remove(path)

    monkeypatch.setattr(work_files, "_observe_work_file", record_observe)
    monkeypatch.setattr(work_files, "_remove_work_file", record_remove)

    result = await clean_one_work_file(qualification.target_file_id, _context(owned, roots))

    assert result.outcome is WorkFileSingleOutcome.DELETED
    assert [stage for stage, _thread in actual_threads] == ["stat", "unlink"]
    assert all(worker != main_thread for _stage, worker in actual_threads)


@pytest.mark.asyncio
async def test_live_media_file_task_keeps_terminal_owner_file_until_actual_end(work_env):
    owned, roots, qualification = work_env
    file_id = qualification.target_file_id
    _cancel_delivery(owned, qualification.delivery_id)
    _release_ok(owned, file_id)
    supervisor = Supervisor()
    executor = FileTaskExecutor(supervisor)
    started, release = threading.Event(), threading.Event()

    def media(_stop):
        started.set()
        release.wait()
        return "actual media ended"

    running = asyncio.create_task(executor.run_file_task(FileTask(
        task_id=FileTaskId("media/terminal-owner"), file_id=file_id,
        stage="media", business="original file", body=media), FileOwner()))
    try:
        await asyncio.to_thread(started.wait)
        context = replace(_context(owned, roots), executor=executor)

        result = await clean_one_work_file(file_id, context)

        assert result.outcome is WorkFileSingleOutcome.KEPT
        assert _work_path(roots, owned, file_id).exists()
        assert _file_state(owned, file_id)[:2] == (2, 2)
        assert executor.unfinished_files() == (file_id,)
    finally:
        release.set()
        await running
        await supervisor.drain_required()


@pytest.mark.asyncio
async def test_cancel_cleanup_waits_actual_unlink_and_saves_result_once(work_env, monkeypatch):
    owned, roots, qualification = work_env
    file_id = qualification.target_file_id
    _cancel_delivery(owned, qualification.delivery_id)
    _release_ok(owned, file_id)
    started, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    remove = work_files._remove_work_file
    calls = []

    def unlink(path):
        calls.append(path)
        if threading.get_ident() == loop_thread:
            raise OSError("不能在事件循环线程等待实际文件调用")
        started.set()
        release.wait()
        remove(path)

    monkeypatch.setattr(work_files, "_remove_work_file", unlink)
    context = _context(owned, roots)
    task = asyncio.create_task(clean_one_work_file(file_id, context))
    try:
        assert await asyncio.to_thread(started.wait, 0.5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "文件还未实际结束，原连接拥有者不能离开"
        assert _work_path(roots, owned, file_id).exists()
        assert _file_state(owned, file_id)[:2] == (2, 3)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert _file_state(owned, file_id)[:2] == (2, 4)
        assert context.processed[file_id] is WorkFileSingleOutcome.DELETED
        assert len(calls) == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)

"""清理通过真实文件执行器调度，并保留实际文件拥有者。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import threading
from pathlib import Path

import pytest

from ..support.database import business_database, database_path

from camctl.host_files.tasks import FileTask, FileTaskExecutor, FileTaskId
from camctl.outputs.work_files import WorkFileSingleOutcome, clean_one_work_file
from camctl.session.supervision import Supervisor

from .test_work_files import (
    _CONTENT, _cancel_public_delivery, _context, _events, _file_state, _release_ok, _work_path,
    public_work_env, public_file_env,
    local_read, read_targets, work_env,  # noqa: F401
)


async def _wait_started(started, running):
    deadline = asyncio.get_running_loop().time() + 5
    while not started.is_set():
        remaining = deadline - asyncio.get_running_loop().time()
        assert remaining > 0, "实际文件操作没有进入目标阶段"
        done, _ = await asyncio.wait({running}, timeout=min(remaining, 0.05))
        if done:
            await running
            raise AssertionError("实际文件任务在进入目标阶段前结束")


async def _wait_finished(running):
    done, _ = await asyncio.wait({running}, timeout=5)
    assert done, "放行后实际责任未在期限内结束"


class FileOwner:
    async def take_over(self, task):
        await task.pending.wait()


@pytest.mark.asyncio
async def test_cleanup_observation_and_unlink_execute_outside_event_loop(public_file_env, monkeypatch):
    owned, roots, qualification = public_file_env
    async with business_database(database_path(owned)) as database:
        _cancel_public_delivery(owned, qualification)
        _release_ok(owned, qualification.target_file_id)
        main_thread = threading.get_ident()
        actual_threads = []
        target = _work_path(roots, owned, qualification.target_file_id)
        observe, remove = Path.stat, Path.unlink

        def record_observe(path, *args, **kwargs):
            if path == target:
                actual_threads.append(("stat", threading.get_ident()))
            return observe(path, *args, **kwargs)

        def record_remove(path, *args, **kwargs):
            if path == target:
                actual_threads.append(("unlink", threading.get_ident()))
            return remove(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", record_observe)
        monkeypatch.setattr(Path, "unlink", record_remove)

        result = await clean_one_work_file(qualification.target_file_id, _context(database, roots))

        assert result.outcome is WorkFileSingleOutcome.DELETED
        assert [stage for stage, _thread in actual_threads] == ["stat", "unlink"]
        assert all(worker != main_thread for _stage, worker in actual_threads)


@pytest.mark.asyncio
async def test_live_media_file_task_keeps_terminal_owner_file_until_actual_end(public_file_env):
    owned, roots, qualification = public_file_env
    async with business_database(database_path(owned)) as database:
        file_id = qualification.target_file_id
        _cancel_public_delivery(owned, qualification)
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
            await _wait_started(started, running)
            context = replace(_context(database, roots), executor=executor)

            result = await clean_one_work_file(file_id, context)

            assert result.outcome is WorkFileSingleOutcome.KEPT
            assert _work_path(roots, owned, file_id).read_bytes() == _CONTENT
            assert _file_state(owned, file_id)[:2] == (2, 2)
            assert executor.unfinished_files() == (file_id,)
        finally:
            release.set()
            await _wait_finished(running)
            await running
            drain = asyncio.create_task(supervisor.drain_required())
            await _wait_finished(drain)
            await drain


@pytest.mark.asyncio
async def test_cancel_cleanup_waits_actual_unlink_and_saves_result_once(public_file_env, monkeypatch):
    owned, roots, qualification = public_file_env
    async with business_database(database_path(owned)) as database:
        file_id = qualification.target_file_id
        _cancel_public_delivery(owned, qualification)
        _release_ok(owned, file_id)
        started, release = threading.Event(), threading.Event()
        loop_thread = threading.get_ident()
        target = _work_path(roots, owned, file_id)
        remove = Path.unlink
        calls = []

        def unlink(path, *args, **kwargs):
            if path == target:
                calls.append(path)
                if threading.get_ident() == loop_thread:
                    raise OSError("不能在事件循环线程等待实际文件调用")
                started.set()
                release.wait()
            return remove(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", unlink)
        context = _context(database, roots)
        task = asyncio.create_task(clean_one_work_file(file_id, context))
        try:
            await _wait_started(started, task)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), "文件还未实际结束，原连接拥有者不能离开"
            assert _work_path(roots, owned, file_id).read_bytes() == _CONTENT
            assert _file_state(owned, file_id)[:2] == (2, 3)
            release.set()
            await _wait_finished(task)
            with pytest.raises(asyncio.CancelledError):
                await task
            assert _file_state(owned, file_id)[:2] == (2, 4)
            assert not target.exists()
            assert _events(owned, 26, 4) == 1
            assert calls == [target]
        finally:
            release.set()
            await _wait_finished(task)
            await asyncio.gather(task, return_exceptions=True)

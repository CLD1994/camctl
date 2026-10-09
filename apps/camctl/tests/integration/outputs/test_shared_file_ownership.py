"""拷贝与发布共用实际文件资格，取消后连接保持到调用结束。"""

import asyncio
from dataclasses import replace
from decimal import Decimal
import threading

import pytest

from camctl.devices.read_session import ReadSession, SourceFile
from camctl.host_files.tasks import FileTaskExecutor
from camctl.outputs import copy, handoff
from camctl.outputs.copy import (
    CompletionContext, CopyContext, SegmentContext, complete_copy,
    copy_next_segment, prepare_copy,
)
from camctl.outputs.handoff import DeliveryContext, DeliveryDirectories, publish_delivery
from camctl.outputs.work_files import WorkFileSingleOutcome, clean_one_work_file
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.session.supervision import Supervisor

from .test_copy_segments import (
    _ByteStream, _CONTENT, _drive,
    local_read, read_targets, segment_env,  # noqa: F401
)
from .test_qualification import _NOW
from .test_work_files import _cancel_delivery, _context, _file_state


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["prepare", "segment", "hash", "move"])
async def test_shared_file_operation_keeps_qualification_until_actual_end(
        segment_env, tmp_path, monkeypatch, stage):
    owned, roots, qualification = segment_env
    # 普通发布只适用于 delivery；内部拷贝由其他三个分区覆盖。
    if stage == "move" and qualification.delivery_id is None:
        pytest.skip("内部副本不执行普通 delivery 发布")
    file_id, copy_id = qualification.target_file_id, qualification.copy_id
    owned.connection.execute("UPDATE device_files SET checksum_support=3 WHERE id=501")
    owned.connection.commit()
    repository = OutputsRepository()
    executor = FileTaskExecutor(Supervisor())
    started, release = threading.Event(), threading.Event()
    actual_threads = []
    loop_thread = threading.get_ident()
    session = None

    def hold(actual):
        def run(*args, **kwargs):
            actual_threads.append(threading.get_ident())
            if threading.get_ident() == loop_thread:
                raise OSError("实际文件操作不能在事件循环中等待")
            started.set()
            release.wait()
            return actual(*args, **kwargs)
        return run

    if stage == "prepare":
        monkeypatch.setattr(copy, "_prepare_target", hold(copy._prepare_target))
        operation = prepare_copy(copy_id, CopyContext(
            repository, owned, roots, _NOW + 20, executor=executor))
    elif stage == "segment":
        await prepare_copy(copy_id, CopyContext(repository, owned, roots, _NOW + 1))
        session = ReadSession(SourceFile("501", {}, len(_CONTENT)), 0,
                              _ByteStream(_CONTENT), Decimal("10"))
        monkeypatch.setattr(copy, "_transfer_segment", hold(copy._transfer_segment))
        operation = copy_next_segment(copy_id, SegmentContext(
            repository, owned, roots, _NOW + 20, len(_CONTENT), session,
            executor=executor))
    elif stage == "hash":
        await _drive(owned, roots, copy_id, segment_size=len(_CONTENT))
        monkeypatch.setattr(copy, "hash_target", hold(copy.hash_target))
        operation = complete_copy(copy_id, CompletionContext(
            repository, owned, roots, _NOW + 20, executor=executor))
    else:
        await _drive(owned, roots, copy_id, segment_size=len(_CONTENT))
        await complete_copy(copy_id, CompletionContext(repository, owned, roots, _NOW + 2))
        directories = DeliveryDirectories(roots.staging, tmp_path / "ready", tmp_path / "processing")
        directories.ready.mkdir()
        directories.processing.mkdir()
        original = handoff.publish_file

        async def move(*args, **kwargs):
            await asyncio.to_thread(hold(lambda: None))
            return await original(*args, **kwargs)

        monkeypatch.setattr(handoff, "publish_file", move)
        operation = publish_delivery(qualification.delivery_id, DeliveryContext(
            repository, owned, directories, _NOW + 20, executor=executor))

    running = asyncio.create_task(operation)
    try:
        assert await asyncio.to_thread(started.wait, 0.5), "实际调用必须已开始"
        assert executor.unfinished_files() == (file_id,)
        if qualification.delivery_id is not None:
            _cancel_delivery(owned, qualification.delivery_id)
        else:
            owned.connection.execute("UPDATE actions SET status=6,cancel_requested=1 WHERE id=11")
            owned.connection.commit()
        before = _file_state(owned, file_id)
        result = await clean_one_work_file(file_id, replace(_context(owned, roots), executor=executor))
        assert result.outcome is WorkFileSingleOutcome.KEPT
        assert _file_state(owned, file_id) == before
        running.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not running.done()
        assert owned.connection.execute("SELECT 1").fetchone() == (1,)
        assert executor.unfinished_files() == (file_id,)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert executor.unfinished_files() == ()
        assert actual_threads and all(value != loop_thread for value in actual_threads)
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)
        if session is not None:
            session.request_stop()
            await session.wait_stopped()

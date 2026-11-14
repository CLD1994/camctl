"""拷贝与发布共用实际文件资格，取消后连接保持到调用结束。"""

import asyncio
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from camctl.devices.read_session import ReadSession, SourceFile
from camctl.host_files.tasks import FileTaskExecutor
from camctl.outputs import copy
from camctl.outputs.copy import (
    CompletionContext, CopyContext, SegmentContext, complete_copy,
    copy_next_segment, prepare_copy,
)
from camctl.outputs.handoff import DeliveryContext, DeliveryPhase, publish_delivery
from camctl.outputs.work_files import WorkFileSingleOutcome, clean_one_work_file
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.session.supervision import Supervisor

from .test_copy_segments import (
    _ByteStream, _CONTENT, _drive,
    local_read, read_targets, segment_env,  # noqa: F401
)
from ..support.database import business_database, database_path
from ..support.public_delivery_cases import prepare_public_delivery
from ..support.outputs import submit_output_document
from ..capture.result_consumer_fixtures import ResultCatalog
from .test_qualification import _NOW
from .test_work_files import _cancel_delivery, _context, _file_state


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["prepare", "segment", "hash"])
async def test_shared_file_operation_keeps_qualification_until_actual_end(
        segment_env, tmp_path, monkeypatch, stage):
    owned, roots, qualification = segment_env
    async with business_database(database_path(owned)) as database:
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
            monkeypatch.setattr(copy, "prepare_target", hold(copy.prepare_target))
            operation = prepare_copy(copy_id, CopyContext(
                repository, database, roots, _NOW + 20, executor=executor))
        elif stage == "segment":
            await prepare_copy(copy_id, CopyContext(repository, database, roots, _NOW + 1))
            session = ReadSession(SourceFile("501", {}, len(_CONTENT)), 0,
                                  _ByteStream(_CONTENT), Decimal("10"))
            monkeypatch.setattr(copy, "transfer_segment", hold(copy.transfer_segment))
            operation = copy_next_segment(copy_id, SegmentContext(
                repository, database, roots, _NOW + 20, len(_CONTENT), session,
                executor=executor))
        elif stage == "hash":
            await _drive(owned, roots, copy_id, segment_size=len(_CONTENT))
            monkeypatch.setattr(copy, "hash_target", hold(copy.hash_target))
            operation = complete_copy(copy_id, CompletionContext(
                repository, database, roots, _NOW + 20, executor=executor))

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
            result = await clean_one_work_file(file_id, replace(_context(database, roots), executor=executor))
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


@pytest.mark.asyncio
async def test_publication_cancel_keeps_file_until_actual_move(tmp_path, monkeypatch):
    from camctl.bootstrap.flows import cancel_flow
    from camctl.bootstrap.work_file_assembly import WorkFileRuntime
    from camctl.outputs.work_files import WorkFileLimits
    from camctl.persistence.models import DbExecutorError
    from camctl.session.locks import acquire_session, lock_file_paths

    class Catalog(ResultCatalog):
        def action_types(self):
            return super().action_types() | {'obtain_action_outputs'}

    owned, roots, directories, qualification = await prepare_public_delivery(
        tmp_path, content=_CONTENT, now=_NOW)
    lock_path, _ = lock_file_paths(database_path(owned))
    lease = acquire_session(lock_path)
    started, release = threading.Event(), threading.Event()
    running = None
    try:
        file_id = qualification.target_file_id
        relative, = owned.connection.execute(
            'SELECT relative_path FROM intermediate_files WHERE id=?', (file_id,)).fetchone()
        filename, = owned.connection.execute(
            'SELECT file_name FROM deliveries WHERE id=?', (qualification.delivery_id,)).fetchone()
        source, target = roots.staging / relative, directories.ready / filename
        assert source.read_bytes() == _CONTENT
        async with business_database(database_path(owned)) as database:
            instant = datetime.fromtimestamp(_NOW // 1_000_000, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            accepted = await submit_output_document(database, catalog=Catalog(), occurred_at=_NOW + 10, body={
                'request_id': '4', 'created_at': instant, 'name': '取消取回',
                'actions': [{'name': '取消', 'type': 'cancel_task',
                             'params': {'target': {'action_instance_id': str(qualification.action_id)}}}]})
            assert accepted.plan_id is not None, accepted
            files = WorkFileRuntime(staging=roots.staging, limits=WorkFileLimits(32, 128))
            executor = files.executor
            name = 'rename' if sys.platform == 'win32' else 'link'
            actual_move = getattr(os, name)
            actual_stat, actual_unlink = os.stat, os.unlink
            moves, premature = [], []

            def observe_stat(path, *args, **kwargs):
                if (started.is_set() and not release.is_set() and
                        isinstance(path, (str, Path)) and Path(path) in (source, target)):
                    premature.append(('stat', Path(path)))
                return actual_stat(path, *args, **kwargs)

            def observe_unlink(path, *args, **kwargs):
                if (started.is_set() and not release.is_set() and
                        isinstance(path, (str, Path)) and Path(path) in (source, target)):
                    premature.append(('unlink', Path(path)))
                return actual_unlink(path, *args, **kwargs)

            def hold_move(src, dst, *args, **kwargs):
                if Path(src) == source and Path(dst) == target:
                    moves.append((Path(src), Path(dst)))
                    started.set()
                    assert release.wait(10), '测试未放行实际移动'
                return actual_move(src, dst, *args, **kwargs)

            monkeypatch.setattr(os, name, hold_move)
            monkeypatch.setattr(os, 'stat', observe_stat)
            monkeypatch.setattr(os, 'unlink', observe_unlink)
            operation = publish_delivery(qualification.delivery_id, DeliveryContext(
                OutputsRepository(), database, directories, _NOW + 20, executor=executor))
            running = asyncio.create_task(operation)
            context = SimpleNamespace(database=database, clock=SimpleNamespace(utc_micros=lambda: _NOW + 30))
            cancel = cancel_flow(staging=directories.staging, ready=directories.ready,
                processing=directories.processing, work_files=files, file_executor=executor)
            try:
                assert await asyncio.to_thread(started.wait, 5), '实际移动必须进入文件接口'
                assert executor.unfinished_files() == (file_id,)
                await asyncio.wait_for(cancel(context), 5)
                assert not running.done() and premature == []
                assert owned.connection.execute('SELECT cancel_requested FROM actions WHERE id=?',
                                                (qualification.action_id,)).fetchone() == (1,)
                assert _file_state(owned, file_id)[1] != 4
                release.set()
                result = await asyncio.wait_for(running, 5)
                assert result.phase is DeliveryPhase.PUBLISHED
                assert moves == [(source, target)] and executor.unfinished_files() == ()
                assert target.read_bytes() == _CONTENT and not source.exists()
                await asyncio.wait_for(cancel(context), 5)
                assert not target.exists()
                assert owned.connection.execute('SELECT withdrawal_state FROM deliveries WHERE id=?',
                                                (qualification.delivery_id,)).fetchone() == (3,)
            finally:
                release.set()
                await asyncio.wait_for(asyncio.gather(running, return_exceptions=True), 10)
                await asyncio.wait_for(executor.drain_required(), 10)
        with pytest.raises(DbExecutorError):
            await database.read(lambda owned: owned.connection.execute('SELECT 1').fetchone())
    finally:
        release.set()
        lease.close()
        owned.connection.close()
    reacquired = acquire_session(lock_path)
    reacquired.close()

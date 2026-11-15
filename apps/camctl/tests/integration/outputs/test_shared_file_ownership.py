"""拷贝与发布共用实际文件资格，取消后连接保持到调用结束。"""

import asyncio
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from camctl.host_files.tasks import FileTaskExecutor
from camctl.host_files.models import BoundDirectories
from camctl.outputs.handoff import DeliveryContext, DeliveryPhase, publish_delivery
from camctl.outputs.work_files import WorkFileSingleOutcome, clean_one_work_file
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.session.supervision import Supervisor

from .test_copy_segments import _CONTENT
from ..support.database import business_database, database_path
from ..support.public_delivery_cases import prepare_public_delivery
from ..support.outputs import submit_output_document
from ..bootstrap.test_output_binding_changes import environment  # noqa: F401
from ..capture.result_consumer_fixtures import ResultCatalog
from .test_qualification import _NOW
from .test_work_files import _context, _file_state
from .test_work_file_execution import _wait_finished, _wait_started


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["delivery", "internal"])
@pytest.mark.parametrize("stage", ["prepare", "segment", "hash"])
async def test_shared_file_operation_keeps_qualification_until_actual_end(
        environment, capture_resources, tmp_path, monkeypatch, purpose, stage):
    """真实拍摄来源上的交付或检查副本在取消后仍由原文件任务持有。"""
    from ..bootstrap.test_output_binding_changes import (
        Catalog, _NOW as now, _read_registry, _save_photos,
    )
    from ..capture.test_action6_segmented_normal import Action6RecordCatalog
    from ..support.bootstrap_read_cases import inspection_case
    from ..support.outputs import obtain_factory, prepare_cancel_target
    from ..support.segment_inspection_cases import inspection_source
    from camctl.outputs.obtain_flow import advance_obtain

    source = world = None
    read_sessions, segment_paths = [], []

    def observe_sessions(driver):
        actual = driver.open_read

        async def open_read(*args, **kwargs):
            session = await actual(*args, **kwargs)
            read_sessions.append(session)
            if stage == "segment":
                actual_read = session.read_chunk

                def read_chunk(limit):
                    assert len(segment_paths) == 1
                    hold(segment_paths[0])
                    return actual_read(limit)

                monkeypatch.setattr(session, "read_chunk", read_chunk)
            return session

        monkeypatch.setattr(driver, "open_read", open_read)

    if purpose == "delivery":
        cfg, owned, context, driver = environment
        await _save_photos(cfg, owned, context, driver)
        source_id, = owned.connection.execute(
            "SELECT id FROM actions WHERE device_id='cam-a'").fetchone()
        accepted = await submit_output_document(context.database, catalog=Catalog(), occurred_at=now, body={
            "request_id": "2", "created_at": "2026-01-15 08:00:00", "name": "取回照片",
            "actions": [{"name": "取回", "type": "obtain_action_outputs",
                "scheduled_at": "2026-01-15 09:00:00",
                "params": {"source": {"action_instance_id": str(source_id)}}}],
        })
        assert accepted.plan_id is not None, accepted
        action_id, = owned.connection.execute(
            "SELECT id FROM actions WHERE plan_id=?", (accepted.plan_id,)).fetchone()
        database = context.database
        observe_sessions(driver)
        roots = BoundDirectories(staging=Path(cfg.paths.staging))
        executor = FileTaskExecutor(Supervisor())
        for directory in (cfg.paths.staging, cfg.paths.ready, cfg.paths.processing):
            Path(directory).mkdir(parents=True, exist_ok=True)
        factory = obtain_factory(
            devices=cfg.devices, drivers=_read_registry(driver), staging=roots.staging,
            ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing),
            occurred_at=lambda: now + 20, monotonic_ns=lambda: 0,
            segment_size=len(_CONTENT), file_executor=executor)
        runtime = factory(database)
        operation = advance_obtain(runtime)
        catalog, target_directory = Catalog(), "deliveries"
    else:
        directory = tmp_path / "inspection"
        directory.mkdir()
        source = await inspection_source(directory, count=1)
        world = await inspection_case(source, capture_resources)
        owned, database, roots, executor = world.owned, world.database, world.roots, world.executor
        observe_sessions(world.driver)
        action_id = world.ref.action_id
        now = world.wall[0]
        from camctl.bootstrap.capture_assembly import execution_wait_config, session_capture_assembly
        from camctl.capture.segment_inspections import inspect_segment
        from camctl.devices.drivers.registry import DriverRegistry
        factory = session_capture_assembly(
            devices=world.config.devices, drivers=DriverRegistry((world.entry,)),
            results=world.results, staging=roots.staging, wait_config=execution_wait_config,
            wall_us=lambda: world.wall[0], monotonic_ns=lambda: 7_000_000_000,
            probe_request=world.probe_request, repair_request=world.repair_request,
            segment_size=len(_CONTENT), file_executor=executor, **world.collections)
        capture = await factory(database, "cam-1")
        operation = inspect_segment(capture.media, world.request)
        catalog, target_directory = Action6RecordCatalog(), "recording-inputs"

    started, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    actual_threads, selected_paths = [], []
    actual_open = os.open

    def hold(path):
        selected_paths.append(path)
        actual_threads.append(threading.get_ident())
        if threading.get_ident() == loop_thread:
            raise OSError("实际文件操作不能在事件循环中等待")
        started.set()
        assert release.wait(10), "测试未放行实际文件调用"

    def matches(path):
        return (isinstance(path, (str, Path)) and Path(path).is_absolute()
                and Path(path).parent == roots.staging / target_directory)

    def open_file(path, flags, *args, **kwargs):
        selected = matches(path)
        if selected and not started.is_set() and (
                (stage == "prepare" and flags & os.O_CREAT)
                or (stage == "hash" and flags & os.O_ACCMODE == os.O_RDONLY)):
            hold(Path(path))
        fd = actual_open(path, flags, *args, **kwargs)
        if selected and stage == "segment" and flags & os.O_ACCMODE == os.O_RDWR:
            segment_paths.append(Path(path))
        return fd

    monkeypatch.setattr(os, "open", open_file)
    running = asyncio.create_task(operation)
    try:
        await _wait_started(started, running)
        assert len(selected_paths) == 1
        if stage in ("segment", "hash"):
            assert len(read_sessions) == 1
        relative = selected_paths[0].relative_to(roots.staging).as_posix()
        file_id, = owned.connection.execute(
            "SELECT id FROM intermediate_files WHERE relative_path=?", (relative,)).fetchone()
        assert executor.unfinished_files() == (file_id,)
        prepare_cancel_target(owned, catalog=catalog, now=now + 30,
                              action_id=action_id, request_id="4")
        assert owned.connection.execute(
            "SELECT cancel_requested FROM actions WHERE id=?", (action_id,)).fetchone() == (1,)
        before = _file_state(owned, file_id)
        result = await clean_one_work_file(file_id, replace(
            _context(database, roots, occurred_at=now + 40), executor=executor))
        assert result.outcome is WorkFileSingleOutcome.KEPT
        assert _file_state(owned, file_id) == before
        running.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not running.done()
        assert await database.read(lambda worker: worker.connection.execute("SELECT 1").fetchone()) == (1,)
        assert executor.unfinished_files() == (file_id,)
        release.set()
        await _wait_finished(running)
        with pytest.raises(asyncio.CancelledError):
            await running
        assert executor.unfinished_files() == ()
        assert actual_threads and all(value != loop_thread for value in actual_threads)
    finally:
        release.set()
        await _wait_finished(running)
        await asyncio.gather(running, return_exceptions=True)
        drain = asyncio.create_task(executor.drain_required())
        await _wait_finished(drain)
        settled = await drain
        assert not settled.failed
        for session in read_sessions:
            session.request_stop()
            read_end = asyncio.create_task(session.wait_stopped())
            await _wait_finished(read_end)
            assert (await read_end).stopped
        if world is not None:
            await world.close()
        if source is not None:
            source[0].connection.close()


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

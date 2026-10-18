"""清理调用独立推进时，真实会话仍可施加新取消。"""

import asyncio
import sqlite3
from pathlib import Path

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.cleanup_assembly import cleanup_flow, session_cleanup_assembly
from camctl.bootstrap.flows import cancel_flow
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.persistence.initialization import initialize_state, InitOutcome
from camctl.devices.ports import DeviceCallResult
from .test_cleanup_host_file_tasks import host_environment

from .test_cleanup_flow import (
    _Catalog, _CleanupDriver, _config, _registry, _submit,
    _photo_plan, _run_photo_session, _cleanup_plan,
)

pytestmark = pytest.mark.asyncio


def _rows(db, sql):
    with sqlite3.connect(db) as connection:
        return connection.execute(sql).fetchall()


async def _until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.parametrize('operation', ['delete', 'query'])
@pytest.mark.parametrize('entry', ['custom', 'direct', 'default'])
async def test_real_session_applies_cancel_before_device_call_returns(tmp_path, monkeypatch, operation, entry):
    cfg = _config(tmp_path)
    db = Path(cfg.paths.state_db)
    assert initialize_state(cfg, db).outcome is InitOutcome.CREATED
    await _submit(tmp_path, cfg, _photo_plan('1'))
    output_id = await _run_photo_session(tmp_path, cfg)
    await _submit(tmp_path, cfg, _cleanup_plan('2', {'output_ids': [str(output_id)]}))
    entered, release = asyncio.Event(), asyncio.Event()
    invocations = []

    class Driver(_CleanupDriver):
        async def delete(self, request):
            invocations.append(('delete', request.ticket))
            if operation == 'delete':
                entered.set()
                await release.wait()
            return await super().delete(request)

        async def query_file(self, request):
            invocations.append(('query', request.ticket))
            if operation == 'query':
                entered.set()
                await release.wait()
            return await super().query_file(request)

    driver = Driver(delete_results=((None, None),), query_results=(('present', None),))
    cleanup = cleanup_flow(session_cleanup_assembly(
        devices=cfg.devices, drivers=_registry(driver), max_delete_attempts=2,
        max_query_attempts=1, staging=Path(cfg.paths.staging)), state_db_errors=True)
    deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
    flows = {
        'cancel': cancel_flow(ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing)),
        'cleanup': cleanup}
    if entry == 'direct':
        from camctl.bootstrap.application import query_work_facts
        from camctl.bootstrap.background_flow import CombinedLocalWork
        from camctl.bootstrap.lifecycle import SystemClock, _clock_policy_factory
        from camctl.persistence.repositories.acceptance import AcceptanceRepository
        from camctl.persistence.repositories.session import SessionRepository
        from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
        from camctl.session.locks import acquire_session, acquire_admission
        from camctl.session.service import SessionContext, SessionPaths, run_session
        context = SessionContext(
            mode=CommandMode.RUN, catalog=_Catalog(), clock=SystemClock(),
            open_connection=lambda: open_existing(db, DbOpenMode.EXISTING_RW, DbConfig()),
            acceptance_repository=AcceptanceRepository(), session_repository=SessionRepository(),
            paths=SessionPaths(deps.session_lock, deps.admission_lock),
            acquire_session=lambda: acquire_session(deps.session_lock),
            acquire_admission=lambda: acquire_admission(deps.admission_lock),
            clock_policy=_clock_policy_factory(cfg), facts_query=query_work_facts,
            flows=flows, local_work=CombinedLocalWork((cleanup,)), poll_interval_s=0.005)
        task = asyncio.create_task(run_session(context, None))
    elif entry == 'default':
        from camctl.bootstrap import cleanup_assembly
        from camctl.devices.drivers import runtime as driver_runtime
        monkeypatch.setattr(driver_runtime, 'current_registry', lambda: _registry(driver))
        captured = []
        original_factory = cleanup_assembly.cleanup_flow

        def record_owner(*args, **kwargs):
            owner = original_factory(*args, **kwargs)
            captured.append(owner)
            return owner

        monkeypatch.setattr(cleanup_assembly, 'cleanup_flow', record_owner)
        task = asyncio.create_task(execute_command(deps, None, poll_interval_s=0.005))
    else:
        task = asyncio.create_task(execute_command(deps, None, flows=flows, poll_interval_s=0.005))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if entry == 'default':
            assert len(captured) == 1
            cleanup = captured[0]
        original = _rows(db, 'SELECT id, run_id, intent_event_id FROM operation_attempts WHERE result_event_id IS NULL')
        assert len(original) == 1
        await _submit(tmp_path, cfg, {'request_id': '3', 'created_at': '2026-01-15 08:00:00',
            'name': 'cancel', 'actions': [{'name': 'cancel', 'type': 'cancel_task',
            'params': {'target': {'request_id': '2'}}}]})
        try:
            await _until(lambda: _rows(db, 'SELECT cancel_requested FROM actions WHERE type=5') == [(1,)])
        except TimeoutError:
            pytest.fail('DELETE/FILE_QUERY 尚未返回时，中央会话必须可靠保存新取消事实')
        assert not release.is_set() and not task.done()
        assert _rows(db, 'SELECT id, run_id, intent_event_id FROM operation_attempts WHERE result_event_id IS NULL') == original
        assert [kind for kind, _ in invocations] == (['delete'] if operation == 'delete' else ['delete', 'query'])
        assert cleanup.required_settlements() > 0
        release.set()
        await _until(lambda: _rows(db, 'SELECT status FROM actions WHERE type=5') == [(6,)])
        assert [kind for kind, _ in invocations] == ['delete', 'query']
        assert _rows(db, 'SELECT COUNT(*) FROM operation_attempts WHERE result_event_id IS NULL') == [(0,)]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        close_runtime(deps)


@pytest.mark.parametrize('operation', ['delete', 'query'])
async def test_real_session_applies_cancel_while_shared_host_file_task_is_running(tmp_path, monkeypatch, operation):
    """真实 unlink/stat 在默认池运行时仍保存取消，原文件资格和结果保持。"""
    import os
    import threading
    from camctl.bootstrap.cleanup_assembly import HostArtifacts
    from camctl.bootstrap.work_file_assembly import WorkFileRuntime
    from camctl.host_files import paths as host_paths
    from camctl.outputs.work_files import WorkFileLimits
    from .test_cleanup_flow import _seed_repaired_artifact

    cfg = _config(tmp_path)
    db = Path(cfg.paths.state_db)
    assert initialize_state(cfg, db).outcome is InitOutcome.CREATED
    await _submit(tmp_path, cfg, _photo_plan('1'))
    output_id = await _run_photo_session(tmp_path, cfg)
    _seed_repaired_artifact(db, cfg, output_id)
    await _submit(tmp_path, cfg, _cleanup_plan('2', {'output_ids': [str(output_id)]}))
    target = Path(cfg.paths.staging) / 'derived/900.jpg'
    entered, release = asyncio.Event(), threading.Event()
    query_enabled = threading.Event()
    loop = asyncio.get_running_loop()
    main_thread = threading.get_ident()
    actual_threads, operations, originals = [], [], []
    real_unlink, real_stat = Path.unlink, os.stat

    def gate():
        actual_threads.append(threading.get_ident())
        loop.call_soon_threadsafe(entered.set)
        release.wait()

    def unlink(path, *args, **kwargs):
        if path == target:
            if operation == 'delete':
                gate()
            else:
                raise PermissionError('query must verify retained file')
        return real_unlink(path, *args, **kwargs)

    def stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if path == target and query_enabled.is_set() and not release.is_set():
            gate()
        return result

    original_run = HostArtifacts._run

    async def track_run(self, request):
        operations.append(request.operation)
        originals.append(request)
        if request.operation == 'query':
            query_enabled.set()
        return await original_run(self, request)

    monkeypatch.setattr(Path, 'unlink', unlink)
    monkeypatch.setattr(host_paths.os, 'stat', stat)
    monkeypatch.setattr(HostArtifacts, '_run', track_run)
    driver = _CleanupDriver()
    work_files = WorkFileRuntime(staging=Path(cfg.paths.staging), limits=WorkFileLimits(batch_size=100, limit_per_run=100))
    cleanup = cleanup_flow(session_cleanup_assembly(
        devices=cfg.devices, drivers=_registry(driver), max_delete_attempts=2,
        max_query_attempts=1, staging=Path(cfg.paths.staging), file_executor=work_files.executor), state_db_errors=True)
    deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
    deps.work_files = work_files
    task = asyncio.create_task(execute_command(deps, None, flows={
        'cancel': cancel_flow(ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing), work_files=work_files),
        'cleanup': cleanup}, poll_interval_s=0.005))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert 900 in work_files.executor.unfinished_files()
        assert actual_threads == [actual_threads[0]] and actual_threads[0] != main_thread
        attempts = _rows(db, 'SELECT id,run_id,intent_event_id FROM operation_attempts WHERE result_event_id IS NULL')
        assert len(attempts) == 1
        await _submit(tmp_path, cfg, {'request_id': '3', 'created_at': '2026-01-15 08:00:00',
            'name': 'cancel', 'actions': [{'name': 'cancel', 'type': 'cancel_task',
            'params': {'target': {'request_id': '2'}}}]})
        await _until(lambda: _rows(db, 'SELECT cancel_requested FROM actions WHERE type=5') == [(1,)])
        assert not release.is_set() and not task.done()
        assert 900 in work_files.executor.unfinished_files()
        assert _rows(db, 'SELECT id,run_id,intent_event_id FROM operation_attempts WHERE result_event_id IS NULL') == attempts
        release.set()
        await _until(lambda: _rows(db, 'SELECT status FROM actions WHERE type=5') == [(6,)])
        assert operations == (['delete'] if operation == 'delete' else ['delete', 'query'])
        assert not driver.delete_calls and not driver.query_calls
        assert _rows(db, 'SELECT COUNT(*) FROM operation_attempts WHERE result_event_id IS NULL') == [(0,)]
        assert work_files.executor.unfinished_files() == ()
        assert target.exists() is (operation == 'query')
    finally:
        release.set()
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        close_runtime(deps)


@pytest.mark.parametrize('operation', ['delete', 'query'])
@pytest.mark.parametrize('control', ['owner_stop', 'parent_cancel'])
async def test_cleanup_stop_retains_started_host_result_and_original_pending(
        host_environment, monkeypatch, operation, control):
    """停止完整 owner 仍等待原线程和首次保存，QUERY消费责任由新owner接手。"""
    import threading
    from camctl.bootstrap.cleanup_assembly import HostArtifacts
    from camctl.persistence.repositories.outputs import OutputsRepository
    from camctl.session.service import StateDbFailure
    from .cleanup_helpers import finish_cleanup_round
    from .test_cleanup_host_file_tasks import _seed_repaired_file, _accept_cleanup, _cleanup_factory
    from .test_output_binding_changes import _save_photos

    cfg, owned, context, driver, work_files = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    _accept_cleanup(owned, output_id)
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    operations, results, saves, opened = [], [], [], []
    actual_execute, original_open = HostArtifacts._execute, context.open_connection
    original_save = OutputsRepository.finish_cleanup_attempt
    from camctl.persistence.repositories.operations import OperationRepository
    original_query_save = OperationRepository.finish_attempt

    def execute(self, request):
        operations.append(request.operation)
        if operation == 'query' and request.operation == 'delete':
            # 实际删除已结束，但调用回执没有可靠观察，须由独立QUERY核实。
            actual_execute(self, request)
            return DeviceCallResult((), {'code': 'file_delete_failed', 'stage': 'delete'})
        actual = actual_execute(self, request)
        if request.operation == operation:
            results.append(actual)
            loop.call_soon_threadsafe(entered.set)
            release.wait()
        return actual

    def finish(repository, request, key, current_owned):
        saves.append((request, key))
        return original_save(repository, request, key, current_owned)

    def finish_query(repository, request, key, current_owned):
        saves.append((request, key))
        return original_query_save(repository, request, key, current_owned)

    monkeypatch.setattr(OperationRepository, 'finish_attempt', finish_query)
    monkeypatch.setattr(HostArtifacts, '_execute', execute)
    monkeypatch.setattr(OutputsRepository, 'finish_cleanup_attempt', finish)
    pending_queries = {}
    original_factory = _cleanup_factory(cfg, driver, work_files, pending_queries=pending_queries)

    def factory(current_owned):
        opened.append(current_owned)
        return original_factory(current_owned)

    factory.pending_collections = original_factory.pending_collections
    owner = cleanup_flow(factory, state_db_errors=True)
    deps = None
    if control == 'parent_cancel':
        # 直接会话上下文显式登记同一个完整owner。
        from camctl.bootstrap.application import query_work_facts
        from camctl.bootstrap.background_flow import CombinedLocalWork
        from camctl.bootstrap.lifecycle import SystemClock, _clock_policy_factory
        from camctl.persistence.repositories.acceptance import AcceptanceRepository
        from camctl.persistence.repositories.session import SessionRepository
        from camctl.session.locks import acquire_session, acquire_admission
        from camctl.session.service import SessionContext, SessionPaths, run_session
        from .test_output_binding_changes import Catalog
        deps = build_runtime(CommandMode.RUN, cfg, catalog=Catalog())
        session = SessionContext(
            mode=CommandMode.RUN, catalog=Catalog(), clock=SystemClock(),
            open_connection=original_open,
            acceptance_repository=AcceptanceRepository(), session_repository=SessionRepository(),
            paths=SessionPaths(deps.session_lock, deps.admission_lock),
            acquire_session=lambda: acquire_session(deps.session_lock),
            acquire_admission=lambda: acquire_admission(deps.admission_lock),
            clock_policy=_clock_policy_factory(cfg), facts_query=query_work_facts,
            flows={'cleanup': owner}, local_work=CombinedLocalWork((owner, work_files)),
            poll_interval_s=0.005)
        task = asyncio.create_task(run_session(session, None))
    else:
        await owner(context)
        task = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert work_files.executor.unfinished_files() == (900,)
        if task is None:
            owner.stop_new_work()
            owner.stop_new_work()
            task = asyncio.create_task(owner.settle())
        else:
            task.cancel('original run cancellation')
            await asyncio.sleep(0)
            task.cancel('repeated run cancellation')
        for _ in range(5):
            await asyncio.sleep(0)
        assert not task.done() and owner.required_settlements() > 0
        assert work_files.executor.unfinished_files() == (900,)
        assert len(opened) == 1
        assert opened[0].connection.execute('SELECT 1').fetchone() == (1,)
        release.set()
        if control == 'parent_cancel':
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert str(caught.value) == 'original run cancellation'
            if operation == 'query':
                assert '原清理保存责任尚未可靠交接' in str(caught.value.__notes__)
        elif operation == 'query':
            with pytest.raises(StateDbFailure):
                await task
        else:
            await task
        assert operations == (['delete'] if operation == 'delete' else ['delete', 'query'])
        assert len(results) == 1
        assert len(saves) == (1 if operation == 'delete' else 2)
        assert work_files.executor.unfinished_files() == ()
        with pytest.raises(sqlite3.ProgrammingError):
            opened[0].connection.execute('SELECT 1')
        if operation == 'query':
            assert len(pending_queries) == 1
            pending = next(iter(pending_queries.values()))
            raw, key, when = pending.actual, pending.key, pending.occurred_at
            from camctl.outputs.cleanup_flow import CleanupQueryPhase
            assert pending.phase is CleanupQueryPhase.SAVED
            before = list(operations)
            new_owner = cleanup_flow(factory, state_db_errors=True)
            await finish_cleanup_round(new_owner, context)
            assert not pending_queries and operations == before
            assert pending.actual is raw and pending.key is key and pending.occurred_at == when
            assert saves[-1][0] is pending.finish and saves[-1][1] is key
        assert owner.required_settlements() == 0
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        if deps is not None:
            close_runtime(deps)


@pytest.mark.parametrize('qualification', ['future', 'source_waiting'])
async def test_short_discovery_round_does_not_wake_another_empty_round(tmp_path, qualification):
    """未来或来源未就绪时，已同步交付的发现轮次不反复唤醒中央等待。"""
    from types import SimpleNamespace
    from camctl.bootstrap.lifecycle import SystemClock
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    from camctl.scheduling.notifications import WorkNotifier
    from .cleanup_helpers import finish_cleanup_round
    from .test_obtain_flow import _future_schedule, _past_schedule

    cfg = _config(tmp_path)
    db = Path(cfg.paths.state_db)
    assert initialize_state(cfg, db).outcome is InitOutcome.CREATED
    plan = _photo_plan('1')
    plan['actions'][0]['scheduled_at'] = _future_schedule(60)
    plan['actions'].append({'name': 'purge', 'type': 'delete_action_outputs',
        'scheduled_at': _future_schedule(60) if qualification == 'future' else _past_schedule(),
        'params': {'source': {'action_name': 'shoot'}}})
    await _submit(tmp_path, cfg, plan)
    wake = WorkNotifier()
    context = SimpleNamespace(wake=wake, clock=SystemClock(), open_connection=lambda:
        open_existing(db, DbOpenMode.EXISTING_RW, DbConfig()))
    driver = _CleanupDriver()
    flow = cleanup_flow(session_cleanup_assembly(
        devices=cfg.devices, drivers=_registry(driver), max_delete_attempts=2,
        max_query_attempts=1, staging=Path(cfg.paths.staging)))
    observed = wake.snapshot()
    for _ in range(3):
        await finish_cleanup_round(flow, context)
        assert flow.required_settlements() == 0
        assert wake.snapshot() == observed
    assert not driver.delete_calls and not driver.query_calls
    assert _rows(db, 'SELECT COUNT(*) FROM operation_attempts') == [(0,)]
    assert _rows(db, "SELECT status FROM actions WHERE name='purge'") == ([(1,)] if qualification == 'future' else [(2,)])


async def test_other_flow_fatal_stops_cleanup_before_emergency_wait_and_preserves_actual(tmp_path):
    """清理D仍在途时其他流程致命失败，先停普通推进再等待应急与实际原结果。"""
    from camctl.bootstrap.application import query_work_facts
    from camctl.bootstrap.background_flow import CombinedLocalWork
    from camctl.bootstrap.lifecycle import SystemClock, _clock_policy_factory
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from camctl.persistence.repositories.session import SessionRepository
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    from camctl.session.locks import acquire_session, acquire_admission
    from camctl.session.service import SessionContext, SessionPaths, StateDbFailure, run_session

    cfg = _config(tmp_path)
    db = Path(cfg.paths.state_db)
    assert initialize_state(cfg, db).outcome is InitOutcome.CREATED
    await _submit(tmp_path, cfg, _photo_plan('1'))
    output_id = await _run_photo_session(tmp_path, cfg)
    await _submit(tmp_path, cfg, _cleanup_plan('2', {'output_ids': [str(output_id)]}))
    entered, release, emergency_entered, emergency_release = (asyncio.Event() for _ in range(4))
    originals = []

    class Driver(_CleanupDriver):
        async def delete(self, request):
            originals.append(request)
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError as interruption:
                # 设备端口跟踪实际结束，并把等待中断交还原调用拥有范围。
                from camctl.operations.owned_calls import current_call_scope
                scope = current_call_scope()
                assert scope is not None
                scope.interrupted = interruption
                await release.wait()
            return await super().delete(request)

    driver = Driver(delete_results=((None, None),))
    factory = session_cleanup_assembly(
        devices=cfg.devices, drivers=_registry(driver), max_delete_attempts=2,
        max_query_attempts=1, staging=Path(cfg.paths.staging))
    cleanup = cleanup_flow(factory, state_db_errors=True)
    deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())

    async def fail(_context):
        await entered.wait()
        raise StateDbFailure('other flow original fatal')

    async def emergency():
        emergency_entered.set()
        await emergency_release.wait()
        return []

    context = SessionContext(
        mode=CommandMode.RUN, catalog=_Catalog(), clock=SystemClock(),
        open_connection=lambda: open_existing(db, DbOpenMode.EXISTING_RW, DbConfig()),
        acceptance_repository=AcceptanceRepository(), session_repository=SessionRepository(),
        paths=SessionPaths(deps.session_lock, deps.admission_lock),
        acquire_session=lambda: acquire_session(deps.session_lock),
        acquire_admission=lambda: acquire_admission(deps.admission_lock),
        clock_policy=_clock_policy_factory(cfg), facts_query=query_work_facts,
        flows={'cleanup': cleanup, 'fatal': fail}, local_work=CombinedLocalWork((cleanup,)),
        emergency_settlement=emergency, poll_interval_s=0.005)
    task = asyncio.create_task(run_session(context, None))
    try:
        await asyncio.wait_for(emergency_entered.wait(), 5)
        assert cleanup.required_settlements() > 0
        assert len(originals) == 1 and not driver.query_calls
        original = _rows(db, 'SELECT id,run_id,intent_event_id FROM operation_attempts WHERE result_event_id IS NULL')
        assert len(original) == 1
        release.set()
        await _until(lambda: _rows(db, 'SELECT COUNT(*) FROM operation_attempts WHERE result_event_id IS NULL') == [(0,)])
        assert len(originals) == 1 and not driver.query_calls
        assert not task.done()
        emergency_release.set()
        outcome = await task
        assert outcome.reason == 'state_db_error' and 'other flow original fatal' in str(outcome.details)
        assert len(originals) == 1 and not driver.query_calls
        assert _rows(db, f'SELECT id,run_id,intent_event_id FROM operation_attempts WHERE id>={original[0][0]}') == original
    finally:
        release.set()
        emergency_release.set()
        await asyncio.gather(task, return_exceptions=True)
        close_runtime(deps)

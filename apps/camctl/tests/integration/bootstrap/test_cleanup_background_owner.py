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
from ..support.recording_consumer_cases import reconciliation_case, recording_cases
from ..support.bootstrap_runtime_cases import (
    original_host_calls, observe_host_calls, direct_session_context, wait_session_event,
    runtime_database_facts, save_runtime_evidence,
)

from .test_cleanup_flow import (
    _Catalog, _CleanupDriver, _config, _registry, _submit,
    _photo_plan, _run_photo_session, _cleanup_plan,
)

pytestmark = pytest.mark.asyncio


def _rows(db, sql):
    with sqlite3.connect(db) as connection:
        return connection.execute(sql).fetchall()


async def _until(predicate, *, task=None):
    async with asyncio.timeout(2):
        while not predicate():
            if task is not None and task.done():
                result = await task
                raise AssertionError(f'目标数据库事实尚未形成，会话已经结束: {result}')
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
    invocations, returns, snapshots = [], [], []

    class Driver(_CleanupDriver):
        async def delete(self, request):
            invocations.append(('delete', request.ticket))
            if operation == 'delete':
                entered.set()
                await release.wait()
            result = await super().delete(request)
            returns.append(('delete', request.ticket, result))
            return result

        async def query_file(self, request):
            invocations.append(('query', request.ticket))
            if operation == 'query':
                entered.set()
                await release.wait()
            result = await super().query_file(request)
            returns.append(('query', request.ticket, result))
            return result

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
        from camctl.bootstrap.lifecycle import SystemClock
        from camctl.persistence.repositories.acceptance import AcceptanceRepository
        from camctl.persistence.repositories.session import SessionRepository
        from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
        from camctl.session.locks import acquire_session, acquire_admission
        from camctl.session.service import SessionContext, SessionPaths, run_session
        context = direct_session_context(cfg, _Catalog(), flows=flows,
            local_work=CombinedLocalWork((cleanup,)), poll_interval_s=0.005)
        task = asyncio.create_task(run_session(context, None))
    elif entry == 'default':
        from camctl.bootstrap import cleanup_assembly
        from ..support.session import run_default_session
        captured = []
        original_factory = cleanup_assembly.cleanup_flow

        def record_owner(*args, **kwargs):
            owner = original_factory(*args, **kwargs)
            captured.append(owner)
            return owner

        monkeypatch.setattr(cleanup_assembly, 'cleanup_flow', record_owner)
        task = asyncio.create_task(run_default_session(deps, None,
            _registry(driver).entry('camctl-adb'), poll_interval_s=0.005))
    else:
        task = asyncio.create_task(execute_command(deps, None, flows=flows, poll_interval_s=0.005))
    try:
        await wait_session_event(entered, task)
        if entry == 'default':
            assert len(captured) == 1
            cleanup = captured[0]
        original = _rows(db, 'SELECT id, run_id, intent_event_id FROM operation_attempts WHERE result_event_id IS NULL')
        assert len(original) == 1
        snapshots.append(runtime_database_facts(db, 'original_device_call_running'))
        await _submit(tmp_path, cfg, {'request_id': '3', 'created_at': '2026-01-15 08:00:00',
            'name': 'cancel', 'actions': [{'name': 'cancel', 'type': 'cancel_task',
            'params': {'target': {'request_id': '2'}}}]})
        try:
            await _until(lambda: _rows(db, 'SELECT cancel_requested FROM actions WHERE type=5') == [(1,)], task=task)
        except TimeoutError:
            pytest.fail('DELETE/FILE_QUERY 尚未返回时，中央会话必须可靠保存新取消事实')
        snapshots.append(runtime_database_facts(db, 'cancel_applied_before_device_return'))
        assert not release.is_set() and not task.done()
        assert _rows(db, 'SELECT id, run_id, intent_event_id FROM operation_attempts WHERE result_event_id IS NULL') == original
        assert [kind for kind, _ in invocations] == (['delete'] if operation == 'delete' else ['delete', 'query'])
        assert cleanup.required_settlements() > 0
        release.set()
        try:
            await _until(lambda: _rows(db, 'SELECT status FROM actions WHERE type=5') == [(6,)], task=task)
        except (TimeoutError, AssertionError) as error:
            if entry == 'default' and operation == 'delete':
                import json
                def task_facts(item):
                    return {'repr': repr(item), 'done': item.done(), 'cancelled': item.cancelled(),
                        'coroutine': repr(item.get_coro()), 'stack': [
                            {'file': frame.f_code.co_filename, 'line': frame.f_lineno,
                             'function': frame.f_code.co_name} for frame in item.get_stack()]}
                log = Path(cfg.paths.log_file)
                evidence = {'failure': repr(error), 'session_task': task_facts(task),
                    'tasks': [task_facts(item) for item in asyncio.all_tasks()],
                    'database': runtime_database_facts(db, 'before_session_cancellation'),
                    'invocations': invocations, 'returns': returns,
                    'required_settlements': cleanup.required_settlements(),
                    'log': log.read_text() if log.exists() else None}
                (tmp_path / 'pre-cancel-evidence.json').write_text(
                    json.dumps(evidence, default=repr, ensure_ascii=False, indent=2))
            raise
        assert [kind for kind, _ in invocations] == ['delete', 'query']
        assert _rows(db, 'SELECT COUNT(*) FROM operation_attempts WHERE result_event_id IS NULL') == [(0,)]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        try:
            outcome = await task
        except asyncio.CancelledError:
            pass
        else:
            if entry == 'default':
                snapshots.append(runtime_database_facts(db, 'session_finished'))
                save_runtime_evidence(tmp_path, outcome, snapshots)
        finally:
            close_runtime(deps)


@pytest.mark.parametrize('operation', ['delete', 'query'])
async def test_real_session_applies_cancel_while_shared_host_file_task_is_running(tmp_path, monkeypatch, operation, capture_resources):
    """真实 unlink/stat 在默认池运行时仍保存取消，原文件资格和结果保持。"""
    import os
    import threading
    from camctl.bootstrap.cleanup_assembly import HostArtifacts
    from camctl.host_files import paths as host_paths
    case = await reconciliation_case(tmp_path, started_at_offset_s=120, repair=True)
    await case.advance()
    cfg, db = case.config, case.path
    output_id, file_id, relative_path = case.owned.connection.execute(
        'SELECT o.id,i.id,i.relative_path FROM outputs o JOIN intermediate_files i'
        ' ON i.id=o.intermediate_file_id WHERE o.kind=2').fetchone()
    target = Path(cfg.paths.staging) / relative_path
    assert target.is_file()
    assert case.stop_calls == ['stop_recording'] and case.tools.calls == ['repair']
    await case.close()
    await capture_resources.close()
    await _submit(tmp_path, cfg, _cleanup_plan('2', {'output_ids': [str(output_id)]}))
    entered, release, finished = asyncio.Event(), threading.Event(), threading.Event()
    query_enabled = threading.Event()
    loop = asyncio.get_running_loop()
    main_thread = threading.get_ident()
    actual_threads, operations, originals = [], [], []
    real_unlink, real_stat = Path.unlink, os.stat

    def gate():
        actual_threads.append(threading.get_ident())
        loop.call_soon_threadsafe(entered.set)
        try:
            release.wait()
        finally:
            finished.set()

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

    original_run = original_host_calls()

    async def track_run(self, request):
        operations.append(request.operation)
        originals.append(request)
        if request.operation == 'query':
            query_enabled.set()
        return await original_run(self, request)

    monkeypatch.setattr(Path, 'unlink', unlink)
    monkeypatch.setattr(host_paths.os, 'stat', stat)
    observe_host_calls(monkeypatch, track_run)
    driver = _CleanupDriver()
    from ..support.session import run_default_session
    deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
    task = asyncio.create_task(run_default_session(deps, None,
        _registry(driver).entry('camctl-adb'), poll_interval_s=0.005))
    try:
        await wait_session_event(entered, task)
        assert originals[-1].file_ref.file_id == file_id
        assert originals[-1].file_ref.relative_path == relative_path
        assert actual_threads == [actual_threads[0]] and actual_threads[0] != main_thread
        attempts = _rows(db, 'SELECT id,run_id,intent_event_id FROM operation_attempts WHERE result_event_id IS NULL')
        assert len(attempts) == 1
        await _submit(tmp_path, cfg, {'request_id': '3', 'created_at': '2026-01-15 08:00:00',
            'name': 'cancel', 'actions': [{'name': 'cancel', 'type': 'cancel_task',
            'params': {'target': {'request_id': '2'}}}]})
        await _until(lambda: _rows(db, 'SELECT cancel_requested FROM actions WHERE type=5') == [(1,)], task=task)
        assert not release.is_set() and not task.done()
        assert _rows(db, 'SELECT id,run_id,intent_event_id FROM operation_attempts WHERE result_event_id IS NULL') == attempts
        release.set()
        await _until(lambda: _rows(db, 'SELECT status FROM actions WHERE type=5') == [(6,)], task=task)
        assert finished.is_set()
        assert operations == (['delete'] if operation == 'delete' else ['delete', 'query'])
        assert not driver.delete_calls and not driver.query_calls
        assert _rows(db, 'SELECT COUNT(*) FROM operation_attempts WHERE result_event_id IS NULL') == [(0,)]
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
    """完整 owner 收场等待原线程和保存，原 QUERY 结果由下一拥有者消费。"""
    import os
    import threading
    from camctl.bootstrap.background_flow import CombinedLocalWork
    from camctl.bootstrap.cleanup_assembly import HostArtifacts
    from camctl.host_files import paths as host_paths
    from camctl.outputs.cleanup_results import saved_cleanup_query
    from camctl.persistence.repositories.outputs import OutputsRepository
    from camctl.persistence.repositories.operations import OperationRepository
    from camctl.session.service import StateDbFailure, run_session
    from .cleanup_helpers import finish_cleanup_round
    from .test_cleanup_host_file_tasks import _seed_repaired_file, _accept_cleanup, _cleanup_factory
    from .test_output_binding_changes import Catalog, _save_photos

    cfg, owned, context, driver, executor, supervisor = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    await _accept_cleanup(owned, output_id)
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    querying = threading.Event()
    operations, results, saves, opened = [], [], [], []
    unlink, stat = Path.unlink, os.stat
    actual_delete, actual_query = HostArtifacts.delete, HostArtifacts.query_file
    original_save = OutputsRepository.finish_cleanup_attempt
    original_query_save = OperationRepository.finish_attempt

    def wait_for_thread():
        loop.call_soon_threadsafe(entered.set)
        release.wait()

    def blocked_unlink(path, *args, **kwargs):
        result = unlink(path, *args, **kwargs)
        if path == target and operation == 'delete':
            wait_for_thread()
        return result

    def blocked_stat(path, *args, **kwargs):
        try:
            return stat(path, *args, **kwargs)
        finally:
            if path == target and querying.is_set():
                querying.clear()
                wait_for_thread()

    async def delete(port, request):
        operations.append(request.operation)
        actual = await actual_delete(port, request)
        if operation == 'query':
            return DeviceCallResult((), {'code': 'file_delete_failed', 'stage': 'delete'})
        results.append(actual)
        return actual

    async def query(port, request):
        operations.append(request.operation)
        querying.set()
        actual = await actual_query(port, request)
        results.append(actual)
        return actual

    def finish(repository, request, key, current_owned):
        saves.append((request, key))
        return original_save(repository, request, key, current_owned)

    def finish_query(repository, request, key, current_owned):
        saves.append((request, key))
        return original_query_save(repository, request, key, current_owned)

    monkeypatch.setattr(Path, 'unlink', blocked_unlink)
    monkeypatch.setattr(host_paths.os, 'stat', blocked_stat)
    monkeypatch.setattr(HostArtifacts, 'delete', delete)
    monkeypatch.setattr(HostArtifacts, 'query_file', query)
    monkeypatch.setattr(OperationRepository, 'finish_attempt', finish_query)
    monkeypatch.setattr(OutputsRepository, 'finish_cleanup_attempt', finish)
    original_factory = _cleanup_factory(cfg, driver, executor)

    async def factory(database):
        opened.append(database)
        return await original_factory(database)

    factory.pending_collections = original_factory.pending_collections
    owner = cleanup_flow(factory, state_db_errors=True)
    if control == 'parent_cancel':
        session = direct_session_context(cfg, Catalog(), flows={'cleanup': owner},
            local_work=CombinedLocalWork((owner,)), poll_interval_s=0.005)
        task = asyncio.create_task(run_session(session, None))
    else:
        await owner(context)
        task = None
    try:
        await wait_session_event(entered, task)
        assert executor.unfinished_files() == (900,)
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
        assert executor.unfinished_files() == (900,)
        assert len(opened) == 1
        assert await opened[0].read(lambda current: current.connection.execute('SELECT 1').fetchone()) == (1,)
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
        assert executor.unfinished_files() == ()
        if control == 'parent_cancel':
            from camctl.persistence.models import DbExecutorError
            with pytest.raises(DbExecutorError):
                await opened[0].read(lambda current: current.connection.execute('SELECT 1').fetchone())
        else:
            assert await opened[0].read(lambda current: current.connection.execute('SELECT 1').fetchone()) == (1,)
        if operation == 'query':
            item_id, = owned.connection.execute('SELECT id FROM cleanup_items').fetchone()
            saved = saved_cleanup_query(owned.connection, item_id)
            assert saved is not None and saved.after_delete
            before = list(operations), list(saves)
            original_row = owned.connection.execute('SELECT * FROM operation_attempts WHERE run_id=? AND attempt_no=?',
                (saved.ticket.run_id, saved.ticket.attempt_id)).fetchone()
            new_owner = cleanup_flow(factory, state_db_errors=True)
            await finish_cleanup_round(new_owner, context)
            assert (operations, saves) == before
            assert owned.connection.execute('SELECT * FROM operation_attempts WHERE run_id=? AND attempt_no=?',
                (saved.ticket.run_id, saved.ticket.attempt_id)).fetchone() == original_row
            assert new_owner.required_settlements() == 0
        assert owner.required_settlements() == 0
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('qualification', ['future', 'source_waiting'])
async def test_short_discovery_round_does_not_wake_another_empty_round(tmp_path, monkeypatch, qualification):
    """真实发现跨返回完成至多通知一次；默认会话保留未就绪的实际资格。"""
    from types import SimpleNamespace
    from camctl.bootstrap.cleanup_assembly import CleanupFlow
    from camctl.bootstrap.lifecycle import SystemClock
    from camctl.persistence.business import BusinessDatabase
    from camctl.persistence.models import DbExecutorError
    from camctl.scheduling.notifications import WorkNotifier
    from ..support.capture import capture_database
    from ..support.session import run_default_session
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
    database = await capture_database(db)
    wake = WorkNotifier()
    context = SimpleNamespace(database=database, wake=wake, clock=SystemClock())
    entered, release = asyncio.Event(), asyncio.Event()
    original_read = BusinessDatabase.read
    held_reads, read_calls = [], []
    armed = True

    async def gated_read(current, callback, **options):
        nonlocal armed
        if current is database:
            read_calls.append(options)
            if armed:
                armed = False
                held_reads.append(options)
                entered.set()
                await release.wait()
        return await original_read(current, callback, **options)

    monkeypatch.setattr(BusinessDatabase, 'read', gated_read)
    driver = _CleanupDriver()
    flow = cleanup_flow(session_cleanup_assembly(
        devices=cfg.devices, drivers=_registry(driver), max_delete_attempts=2,
        max_query_attempts=1, staging=Path(cfg.paths.staging)))
    before_status = _rows(db, "SELECT status FROM actions WHERE name='purge'")
    expected = [(1,)] if qualification == 'future' else [(2,)]
    observed = wake.snapshot()
    try:
        await flow(context)
        await asyncio.wait_for(entered.wait(), 5)
        assert flow.required_settlements() > 0
        assert wake.snapshot() == observed
        for _ in range(2):
            await flow(context)
            assert len(read_calls) == len(held_reads) == 1
            assert wake.snapshot() == observed
        release.set()
        await flow.settle()
        completed = wake.snapshot()
        assert 0 <= completed.version - observed.version <= 1
        if before_status != expected:
            assert completed.version == observed.version + 1
        assert flow.required_settlements() == 0
        for _ in range(3):
            flow.check_completed()
            await flow.settle()
            assert wake.snapshot() == completed
    finally:
        release.set()
        await flow.settle()
    assert _rows(db, "SELECT status FROM actions WHERE name='purge'") == expected
    assert not driver.delete_calls and not driver.query_calls
    assert _rows(db, 'SELECT COUNT(*) FROM operation_attempts') == [(0,)]

    # 完整默认生命周期独立核对具体清理来源，调度次数由真实变化决定。
    called = asyncio.Event()
    owners, databases = [], []
    original_flow = CleanupFlow.__call__

    async def observe(owner, current):
        if owner not in owners:
            owners.append(owner)
            databases.append(current.database)
        called.set()
        return await original_flow(owner, current)

    monkeypatch.setattr(CleanupFlow, '__call__', observe)
    deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
    task = asyncio.create_task(run_default_session(deps, None,
        _registry(driver).entry('camctl-adb'), poll_interval_s=0.005))
    try:
        await wait_session_event(called, task)
        for _ in range(3):
            await asyncio.sleep(0.02)
            assert not task.done()
            assert _rows(db, "SELECT status FROM actions WHERE name='purge'") == expected
            assert _rows(db, 'SELECT COUNT(*) FROM operation_attempts') == [(0,)]
            assert not driver.delete_calls and not driver.query_calls
    finally:
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            close_runtime(deps)
    assert owners and all(owner.required_settlements() == 0 for owner in owners)
    for actual_database in databases:
        with pytest.raises(DbExecutorError):
            await actual_database.read(lambda current: current.connection.execute('SELECT 1').fetchone())
    import json
    (tmp_path / 'discovery-evidence.json').write_text(json.dumps({
        'qualification': qualification, 'before_status': before_status, 'after_status': expected,
        'public_calls_before_read_release': 3, 'held_first_reads': len(held_reads),
        'notification_before_release': observed.version,
        'notification_after_completion': completed.version,
        'repeated_consumptions': 3, 'notification_after_repeat': wake.snapshot().version,
        'default_attempt_count': _rows(db, 'SELECT COUNT(*) FROM operation_attempts'),
        'default_delete_calls': len(driver.delete_calls), 'default_query_calls': len(driver.query_calls),
        'default_owner_settlements': [owner.required_settlements() for owner in owners],
        'default_database_closed': len(databases),
    }, ensure_ascii=False, indent=2))


async def test_synchronous_empty_source_consumption_does_not_notify(tmp_path):
    """公开空来源协议在同次调用内消费，三轮均无迟到通知。"""
    from types import SimpleNamespace
    from camctl.bootstrap.lifecycle import SystemClock
    from camctl.scheduling.notifications import WorkNotifier
    from ..support.capture import capture_database

    cfg = _config(tmp_path)
    db = Path(cfg.paths.state_db)
    assert initialize_state(cfg, db).outcome is InitOutcome.CREATED
    database = await capture_database(db)
    context = SimpleNamespace(database=database, clock=SystemClock(), wake=WorkNotifier())
    sources = []

    async def empty_source(actual_database):
        sources.append(actual_database)
        return None

    flow = cleanup_flow(empty_source)
    observed = context.wake.snapshot()
    for _ in range(3):
        await flow(context)
        assert flow.required_settlements() == 0
        await flow.settle()
        flow.check_completed()
        assert context.wake.snapshot() == observed
    assert sources == [database, database, database]


async def test_other_flow_fatal_stops_cleanup_before_emergency_wait_and_preserves_actual(tmp_path):
    """清理D仍在途时其他流程致命失败，先停普通推进再等待应急与实际原结果。"""
    from camctl.bootstrap.application import query_work_facts
    from camctl.bootstrap.background_flow import CombinedLocalWork
    from camctl.bootstrap.lifecycle import SystemClock
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
    originals, raw_results = [], []
    import sys
    from decimal import Decimal
    from camctl.operations.process import ToolSpec, execute_tool, spawn_subprocess
    ready_file, release_file = tmp_path / 'delete-ready', tmp_path / 'delete-release'
    code = (
        "from pathlib import Path\nimport signal,time,sys\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "assert signal.getsignal(signal.SIGTERM) == signal.SIG_IGN\n"
        "Path(sys.argv[1]).write_text('ready')\n"
        "while not Path(sys.argv[2]).exists(): time.sleep(0.005)\n"
    )
    class NoStop:
        async def requested(self):
            await asyncio.Event().wait()

    async def spawn(spec):
        process = await spawn_subprocess(spec)
        async with asyncio.timeout(5):
            while not ready_file.exists():
                await asyncio.sleep(0.005)
        entered.set()
        return process

    class Driver(_CleanupDriver):
        async def delete(self, request):
            originals.append(request)
            raw_results.append(await execute_tool(ToolSpec(
                (sys.executable, '-c', code, str(ready_file), str(release_file)),
                request.timeout_s, Decimal('5')), stop=NoStop(), spawner=spawn))
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

    context = direct_session_context(cfg, _Catalog(),
        flows={'cleanup': cleanup, 'fatal': fail}, local_work=CombinedLocalWork((cleanup,)),
        emergency_settlement=emergency, poll_interval_s=0.005)
    task = asyncio.create_task(run_session(context, None))
    try:
        await asyncio.wait_for(emergency_entered.wait(), 5)
        assert cleanup.required_settlements() > 0
        assert len(originals) == 1 and not driver.query_calls
        original = _rows(db, 'SELECT id,run_id,intent_event_id FROM operation_attempts WHERE result_event_id IS NULL')
        assert len(original) == 1
        release_file.touch()
        release.set()
        await _until(lambda: _rows(db, 'SELECT COUNT(*) FROM operation_attempts WHERE result_event_id IS NULL') == [(0,)], task=task)
        assert len(originals) == 1 and not driver.query_calls
        assert not task.done()
        emergency_release.set()
        outcome = await task
        assert outcome.reason == 'state_db_error' and 'other flow original fatal' in str(outcome.details)
        assert len(originals) == 1 and not driver.query_calls
        assert len(raw_results) == 1 and raw_results[0].exit.exit_code == 0
        assert _rows(db, f'SELECT id,run_id,intent_event_id FROM operation_attempts WHERE id>={original[0][0]}') == original
    finally:
        release_file.touch()
        release.set()
        emergency_release.set()
        await asyncio.gather(task, return_exceptions=True)
        close_runtime(deps)

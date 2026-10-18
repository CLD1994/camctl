"""主机正式产物清理复用会话共享文件执行器。"""

import asyncio
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.bootstrap.cleanup_assembly import (
    HostArtifacts, _LocalArtifactResultOwner, session_cleanup_assembly,
)
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.cleanup_assembly import cleanup_flow
from camctl.bootstrap.work_file_assembly import WorkFileRuntime
from camctl.contracts.values import ConsistencyError
from camctl.devices.ports import DeviceCallResult
from camctl.host_files import paths as host_paths
from camctl.host_files.models import FilePurpose
from camctl.operations.models import SettlementBasis
from camctl.outputs import cleanup_flow as cleanup_flow_module
from camctl.outputs.cleanup_results import saved_cleanup_delete, saved_cleanup_query
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.repositories.acceptance import register_acceptance_guards
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.cancellation import register_cancellation_guards
from camctl.persistence.repositories.operations import register_operation_guards
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.repositories.scheduling import register_window_guard
from camctl.persistence.repositories.timelapse import register_timelapse_guards
from camctl.host_files.tasks import FileTask, FileTaskId
from camctl.outputs.work_files import WorkFileLimits

from .test_output_binding_changes import (
    Driver, _NOW, _accept, _registry, _save_photos,
)
from .test_output_binding_cancellation import _apply_cleanup_cancel


pytestmark = pytest.mark.asyncio


@pytest.fixture
def host_environment(tmp_path):
    cfg = load_config({
        "paths": {
            "state_db": str(tmp_path / "state.db"),
            "log_file": str(tmp_path / "camctl.log"),
            **{name: str(tmp_path / name) for name in ("staging", "ready", "processing")},
        },
        "devices": {device: {"kind": "camera", "driver": "camctl-adb"}
                    for device in ("cam-a", "cam-b")},
    }, ConfigDefaults())
    assert initialize_state(cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
    register_acceptance_guards()
    register_capture_guards()
    register_cancellation_guards()
    register_operation_guards()
    register_outputs_guards()
    register_window_guard()
    register_timelapse_guards()
    owned = open_existing(Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
    context = SimpleNamespace(
        open_connection=lambda: open_existing(
            Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig()),
        clock=SimpleNamespace(utc_micros=lambda: _NOW),
    )
    driver = Driver(owned)
    work_files = WorkFileRuntime(
        staging=Path(cfg.paths.staging),
        limits=WorkFileLimits(batch_size=100, limit_per_run=100),
    )
    try:
        yield cfg, owned, context, driver, work_files
    finally:
        owned.connection.close()


def _seed_repaired_file(cfg, owned):
    output_id = owned.connection.execute(
        "SELECT o.id FROM outputs o JOIN actions a ON a.id=o.source_action_id"
        " WHERE a.device_id='cam-a'").fetchone()[0]
    connection = sqlite3.connect(cfg.paths.state_db)
    try:
        connection.execute(
            "INSERT INTO intermediate_files (id, owner_action_id, purpose, relative_path,"
            " retention_state, cleanup_state, size_bytes, created_event_id, last_event_id,"
            " change_count) VALUES (900, (SELECT source_action_id FROM outputs WHERE id=?),"
            " 4, 'derived/900.jpg', 3, 1, 16, 1, 1, 1)", (output_id,))
        connection.execute(
            "UPDATE outputs SET kind=2, device_file_id=NULL, intermediate_file_id=900"
            " WHERE id=?", (output_id,))
        connection.commit()
    finally:
        connection.close()
    target = Path(cfg.paths.staging) / "derived" / "900.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"repaired-artifact")
    return output_id, target


def _accept_cleanup(owned, output_id):
    _accept(owned, "2", [{
        "name": "清理主机成品", "type": "delete_action_outputs",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"output_ids": [str(output_id)]},
    }])


def _cleanup_factory(cfg, driver, work_files, **kwargs):
    return session_cleanup_assembly(
        devices=cfg.devices, drivers=_registry(driver),
        max_delete_attempts=cfg.cleanup.max_delete_attempts,
        max_query_attempts=cfg.cleanup.max_query_attempts,
        staging=Path(cfg.paths.staging), file_executor=work_files.executor,
        **kwargs)


class _Owner:
    async def take_over(self, task):
        self.result = await task.pending.wait()


async def _wait_until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0)


def _exception_chain_text(error):
    parts = []
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        parts.append(f"{type(current).__name__}: {current}")
        parts.extend(str(note) for note in getattr(current, "__notes__", ()))
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        elif current.__context__ is not None:
            pending.append(current.__context__)
    return "\n".join(parts)


class _FailingOwner:
    def __init__(self, message):
        self.message = message

    async def take_over(self, _task):
        raise RuntimeError(self.message)


@pytest.mark.parametrize("business_cancel", [False, True])
async def test_queued_host_delete_wait_cancellation_saves_not_dispatched_once(
        host_environment, monkeypatch, business_cancel):
    """排队撤回保存真实 ran=False；等待取消不成为业务取消或重放原票据。"""
    cfg, owned, context, driver, work_files = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    _accept_cleanup(owned, output_id)

    loop = asyncio.get_running_loop()
    previous_pool = loop._default_executor
    pool = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(pool)
    gate = threading.Event()
    started = asyncio.Event()
    owner = _Owner()
    submitted = []
    run_file_task = work_files.executor.run_file_task
    requests = []
    host_run = HostArtifacts._run

    async def capture_request(self, request):
        requests.append(request)
        return await host_run(self, request)

    monkeypatch.setattr(HostArtifacts, "_run", capture_request)

    async def track_host_task(task, task_owner, **kwargs):
        if task.business == "host_artifact_cleanup":
            submitted.append((task, kwargs))
        return await run_file_task(task, task_owner, **kwargs)

    monkeypatch.setattr(work_files.executor, "run_file_task", track_host_task)

    def occupy_pool(_stop_event):
        loop.call_soon_threadsafe(started.set)
        gate.wait()
        return "released"

    blocker = FileTask(
        FileTaskId("r4-test-pool-blocker"), 999_001, "test", "pool_blocker", occupy_pool)
    blocker_task = asyncio.create_task(
        work_files.executor.run_file_task(blocker, owner))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        factory = session_cleanup_assembly(
            devices=cfg.devices, drivers=_registry(driver),
            max_delete_attempts=cfg.cleanup.max_delete_attempts,
            max_query_attempts=cfg.cleanup.max_query_attempts,
            staging=Path(cfg.paths.staging), file_executor=work_files.executor,
        )
        cleanup = asyncio.create_task(cleanup_flow(factory)(context))
        await _wait_until(lambda: 900 in work_files.executor.unfinished_files())
        item_id = owned.connection.execute("SELECT id FROM cleanup_items").fetchone()[0]
        if business_cancel:
            _apply_cleanup_cancel(owned)
        cleanup.cancel()
        # 取消先让执行器在同一锁内撤回排队任务；blocker 放行后才会完成共享 drain。
        await _wait_until(lambda: 900 not in work_files.executor.unfinished_files())
        gate.set()
        with pytest.raises(asyncio.CancelledError) as interrupted:
            await cleanup
        await blocker_task

        observed = context.open_connection()
        try:
            saved = saved_cleanup_delete(observed.connection, item_id)
            item_status = observed.connection.execute(
                "SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
            attempts = observed.connection.execute(
                "SELECT a.id,a.status FROM operation_attempts a JOIN operation_runs r"
                " ON r.id=a.run_id WHERE r.cleanup_item_id=? AND r.kind=4",
                (item_id,)).fetchall()
            config = observed.connection.execute(
                "SELECT a.max_attempts_used,a.timeout_s_json,a.retry_interval_s_json"
                " FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
                " WHERE r.cleanup_item_id=? AND r.kind=4 ORDER BY a.id",
                (item_id,)).fetchall()
        finally:
            observed.connection.close()
        assert saved is not None, (item_status, attempts, repr(interrupted.value.__cause__))
        assert saved.actual.outcome.settlement.basis is SettlementBasis.NOT_DISPATCHED
        assert target.is_file()
        assert item_status == ((6,) if business_cancel else (3,))
        assert len(attempts) == 1
        assert len(submitted) == 1
        task, task_options = submitted[0]
        assert task.file_id == 900
        assert task.task_id.value == (
            f"cleanup:{owned.metadata.instance_id}:delete:{saved.ticket.run_id}:"
            f"{saved.ticket.attempt_id}")
        assert task.stage == "cleanup_delete"
        assert task_options == {"handoff_withdrawn": True}
        request = requests[0]
        assert request.database_id == owned.metadata.instance_id
        assert request.ticket == saved.ticket
        assert request.file_ref.file_id == task.file_id == 900
        assert request.file_ref.purpose is FilePurpose.REPAIR_OUTPUT
        assert request.file_ref.relative_path == "derived/900.jpg"
        assert request.file_ref.root == Path(cfg.paths.staging)
        assert config == [(cfg.cleanup.max_delete_attempts, None, None)]

        if business_cancel:
            assert len(submitted) == 1
            return

        # 新工厂按已保存未派发事实授予新票据，在线程中完成同一成品的删除。
        retry_factory = session_cleanup_assembly(
            devices=cfg.devices, drivers=_registry(driver),
            max_delete_attempts=cfg.cleanup.max_delete_attempts,
            max_query_attempts=cfg.cleanup.max_query_attempts,
            staging=Path(cfg.paths.staging), file_executor=work_files.executor,
        )
        await cleanup_flow(retry_factory)(context)
        observed = context.open_connection()
        try:
            attempts = observed.connection.execute(
                "SELECT a.id,a.status FROM operation_attempts a JOIN operation_runs r"
                " ON r.id=a.run_id WHERE r.cleanup_item_id=? AND r.kind=4 ORDER BY a.id",
                (item_id,)).fetchall()
            item_status = observed.connection.execute(
                "SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
        finally:
            observed.connection.close()
        assert len(attempts) == 2
        assert attempts[0][0] != attempts[1][0]
        assert not target.exists()
        assert item_status == (4,)
        assert len(submitted) == 2
        assert submitted[1][0].file_id == 900
        assert submitted[1][0].task_id != submitted[0][0].task_id
        assert len(requests) == 2
    finally:
        gate.set()
        if not blocker_task.done():
            await asyncio.gather(blocker_task, return_exceptions=True)
        loop._default_executor = previous_pool
        pool.shutdown(wait=True)


async def test_queued_host_query_wait_cancellation_keeps_original_consumer(
        host_environment, monkeypatch):
    """排队 QUERY 撤回沿原 AFTER_DELETE consumer 保存真实 NOT_DISPATCHED。"""
    cfg, owned, context, driver, work_files = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    _accept_cleanup(owned, output_id)

    loop = asyncio.get_running_loop()
    previous_pool = loop._default_executor
    pool = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(pool)
    gate = threading.Event()
    blocker_started = asyncio.Event()
    owner = _Owner()

    def occupy_pool(_stop_event):
        loop.call_soon_threadsafe(blocker_started.set)
        gate.wait()
        return "released"

    blocker = FileTask(
        FileTaskId("r4-test-query-pool-blocker"), 999_002,
        "test", "pool_blocker", occupy_pool)
    blocker_task = asyncio.create_task(
        work_files.executor.run_file_task(blocker, owner))
    original_run = HostArtifacts._run
    operations = []
    submitted = []
    run_file_task = work_files.executor.run_file_task

    async def fail_delete_capture_query(self, request):
        operations.append(request.operation)
        if request.operation == "delete":
            return DeviceCallResult((), {
                "code": "local_io_failed", "stage": "delete"})
        return await original_run(self, request)

    async def track_task(task, task_owner, **kwargs):
        if task.business == "host_artifact_cleanup":
            submitted.append(task)
        return await run_file_task(task, task_owner, **kwargs)

    monkeypatch.setattr(HostArtifacts, "_run", fail_delete_capture_query)
    monkeypatch.setattr(work_files.executor, "run_file_task", track_task)
    active_calls = set()
    pending_queries = {}
    consumed = {}
    factory = _cleanup_factory(
        cfg, driver, work_files, active_calls=active_calls,
        pending_queries=pending_queries, consumed_queries=consumed)
    cleanup = asyncio.create_task(cleanup_flow(factory)(context))
    try:
        await asyncio.wait_for(blocker_started.wait(), timeout=5)
        await _wait_until(lambda: 900 in work_files.executor.unfinished_files())
        item_id = owned.connection.execute("SELECT id FROM cleanup_items").fetchone()[0]
        assert owned.connection.execute(
            "SELECT r.kind,a.status FROM operation_attempts a JOIN operation_runs r"
            " ON r.id=a.run_id WHERE r.cleanup_item_id=? ORDER BY a.id",
            (item_id,)).fetchall()[-1] == (5, 1)
        cleanup.cancel()
        await _wait_until(lambda: 900 not in work_files.executor.unfinished_files())
        with pytest.raises(asyncio.CancelledError):
            await cleanup
    finally:
        gate.set()
        if not blocker_task.done():
            await asyncio.gather(blocker_task, return_exceptions=True)
        loop._default_executor = previous_pool
        pool.shutdown(wait=True)

    observed = context.open_connection()
    try:
        saved_delete = saved_cleanup_delete(observed.connection, item_id)
        saved_query = saved_cleanup_query(observed.connection, item_id)
        attempts = observed.connection.execute(
            "SELECT r.kind,a.status FROM operation_attempts a JOIN operation_runs r"
            " ON r.id=a.run_id WHERE r.cleanup_item_id=? ORDER BY a.id", (item_id,)).fetchall()
        config = observed.connection.execute(
            "SELECT a.max_attempts_used,a.timeout_s_json,a.retry_interval_s_json"
            " FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
            " WHERE r.cleanup_item_id=? AND r.kind=5 ORDER BY a.id", (item_id,)).fetchall()
        item = observed.connection.execute(
            "SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
    finally:
        observed.connection.close()
    assert saved_delete is not None and saved_delete.actual.error is not None
    assert saved_query is not None and saved_query.after_delete
    assert saved_query.actual.outcome.settlement.basis is SettlementBasis.NOT_DISPATCHED
    assert saved_query.actual.observations == ()
    assert attempts[-1] == (5, 3)
    assert config == [(cfg.cleanup.max_query_attempts, None, None)]
    assert item == (3,)
    assert len(pending_queries) == 1
    pending = next(iter(pending_queries.values()))
    assert pending.consumer.value == "after_delete"
    assert pending.phase.value == "saved"
    assert active_calls == set()
    assert target.is_file()
    assert operations == ["delete", "query"]
    assert len(submitted) == 1
    assert submitted[0].stage == "cleanup_query"
    assert submitted[0].file_id == 900
    assert work_files.executor.unfinished_files() == ()

    operations_before_resume = tuple(operations)
    await cleanup_flow(_cleanup_factory(
        cfg, driver, work_files, active_calls=active_calls,
        pending_queries=pending_queries, consumed_queries=consumed))(context)
    assert tuple(operations) == operations_before_resume
    assert not pending_queries
    assert target.is_file()


async def test_started_host_delete_cancellation_saves_observation_and_error(
        host_environment, monkeypatch):
    """已开始 unlink 的取消等待线程与原 ticket 收场；观察和错误一并保存。"""
    cfg, owned, context, driver, work_files = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    _accept_cleanup(owned, output_id)

    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    gate = threading.Event()
    active_calls = set()
    factory = _cleanup_factory(cfg, driver, work_files, active_calls=active_calls)
    original_unlink = Path.unlink
    calls = []
    worker_threads = []

    def unlink_then_fail(path, *args, **kwargs):
        if path == target:
            calls.append(path)
            worker_threads.append(threading.get_ident())
            original_unlink(path, *args, **kwargs)
            loop.call_soon_threadsafe(entered.set)
            gate.wait()
            raise OSError("unlink returned an error after removal")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_then_fail)
    drains = []
    drain_required = work_files.executor.drain_required

    async def tracked_drain():
        drains.append("started")
        return await drain_required()

    monkeypatch.setattr(work_files.executor, "drain_required", tracked_drain)
    cleanup = asyncio.create_task(cleanup_flow(factory)(context))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        item_id = owned.connection.execute("SELECT id FROM cleanup_items").fetchone()[0]
        running = owned.connection.execute(
            "SELECT a.status FROM operation_attempts a JOIN operation_runs r"
            " ON r.id=a.run_id WHERE r.cleanup_item_id=? AND r.kind=4", (item_id,)).fetchone()
        assert running == (1,)
        assert active_calls
        assert 900 in work_files.executor.unfinished_files()
        cleanup.cancel()
        await asyncio.wait_for(_wait_until(lambda: bool(drains)), timeout=5)
        assert active_calls
        assert 900 in work_files.executor.unfinished_files()
        assert not cleanup.done()
        cleanup.cancel()
        assert len(drains) == 1
    finally:
        gate.set()

    with pytest.raises(asyncio.CancelledError):
        await cleanup

    observed = context.open_connection()
    try:
        saved = saved_cleanup_delete(observed.connection, item_id)
        item = observed.connection.execute(
            "SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
        attempts = observed.connection.execute(
            "SELECT a.status FROM operation_attempts a JOIN operation_runs r"
            " ON r.id=a.run_id WHERE r.cleanup_item_id=? AND r.kind=4", (item_id,)).fetchall()
    finally:
        observed.connection.close()
    assert saved is not None
    assert saved.actual.outcome.settlement.basis.value == "observed"
    assert saved.actual.observations[0].type == "file_absent"
    assert saved.actual.error["code"] == "local_io_failed"
    assert item == (4,)
    assert attempts == [(3,)]
    assert calls == [target]
    assert len(worker_threads) == 1 and worker_threads[0] != loop_thread
    assert not target.exists()
    assert not active_calls
    assert 900 not in work_files.executor.unfinished_files()
    assert work_files.file_supervisor.outstanding() == ()


@pytest.mark.parametrize("failure_stage", ["register", "handoff"])
async def test_supervisor_handoff_failure_keeps_original_handle_until_saved(
        host_environment, monkeypatch, failure_stage):
    """监督注册/登记故障不能丢掉已开始任务，也不能长期保留其 handle。"""
    cfg, owned, context, driver, work_files = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    _accept_cleanup(owned, output_id)

    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    gate = threading.Event()
    original_unlink = Path.unlink

    def unlink_and_wait(path, *args, **kwargs):
        if path == target:
            original_unlink(path, *args, **kwargs)
            loop.call_soon_threadsafe(entered.set)
            gate.wait()
        return None if path == target else original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_and_wait)
    supervisor = work_files.file_supervisor
    if failure_stage == "register":
        def fail_register(owner):
            raise RuntimeError("controlled supervisor register failure")

        monkeypatch.setattr(supervisor, "register", fail_register)
    else:
        def fail_handoff(token, task):
            raise RuntimeError("controlled supervisor handoff failure")

        monkeypatch.setattr(supervisor, "handoff", fail_handoff)

    active_calls = set()
    drain_started = asyncio.Event()
    drain = work_files.executor.drain_required

    async def tracked_drain():
        drain_started.set()
        return await drain()

    monkeypatch.setattr(work_files.executor, "drain_required", tracked_drain)
    cleanup = asyncio.create_task(cleanup_flow(_cleanup_factory(
        cfg, driver, work_files, active_calls=active_calls))(context))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        item_id = owned.connection.execute("SELECT id FROM cleanup_items").fetchone()[0]
        assert owned.connection.execute(
            "SELECT a.status FROM operation_attempts a JOIN operation_runs r"
            " ON r.id=a.run_id WHERE r.cleanup_item_id=? AND r.kind=4",
            (item_id,)).fetchone() == (1,)
        assert active_calls
        cleanup.cancel()
        await asyncio.wait_for(drain_started.wait(), timeout=5)
        assert active_calls
        assert not cleanup.done()
    finally:
        gate.set()

    with pytest.raises(asyncio.CancelledError) as canceled:
        await cleanup

    observed = context.open_connection()
    try:
        saved = saved_cleanup_delete(observed.connection, item_id)
        attempt = observed.connection.execute(
            "SELECT a.status FROM operation_attempts a JOIN operation_runs r"
            " ON r.id=a.run_id WHERE r.cleanup_item_id=? AND r.kind=4",
            (item_id,)).fetchone()
    finally:
        observed.connection.close()
    assert saved is not None
    assert saved.actual.observations[0].type == "file_absent"
    assert attempt == (2,)
    assert not target.exists()
    assert not active_calls
    assert work_files.executor.unfinished_files() == ()
    assert supervisor.outstanding() == ()
    assert canceled.value.__cause__ is not None
    assert failure_stage in str(canceled.value.__cause__)
    retained = [owner for owner in supervisor._owners.values()
                if isinstance(owner, _LocalArtifactResultOwner)]
    assert all(owner.handle is None for owner in retained)
    if failure_stage == "handoff":
        assert len(retained) == 1 and retained[0].result is not None


async def test_finished_host_delete_cancellation_before_runner_delivery_keeps_actual(
        host_environment, monkeypatch):
    """FileTask completion 已形成但 runner 尚未交付时，撤回不能改写实际结果。"""
    cfg, owned, context, driver, work_files = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    _accept_cleanup(owned, output_id)

    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    runner_returned = asyncio.Event()
    release_runner = asyncio.Event()
    drain_started = asyncio.Event()
    unlink_calls = []
    worker_threads = []
    original_unlink = Path.unlink

    def track_unlink(path, *args, **kwargs):
        if path == target:
            unlink_calls.append(path)
            worker_threads.append(threading.get_ident())
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", track_unlink)
    runner = work_files.executor._runner

    async def hold_delivery(body):
        result = await runner(body)
        loop.call_soon_threadsafe(runner_returned.set)
        await release_runner.wait()
        return result

    monkeypatch.setattr(work_files.executor, "_runner", hold_delivery)
    drain = work_files.executor.drain_required

    async def track_drain():
        drain_started.set()
        return await drain()

    monkeypatch.setattr(work_files.executor, "drain_required", track_drain)
    active_calls = set()
    cleanup = asyncio.create_task(cleanup_flow(_cleanup_factory(
        cfg, driver, work_files, active_calls=active_calls))(context))
    try:
        await asyncio.wait_for(runner_returned.wait(), timeout=5)
        item_id = owned.connection.execute("SELECT id FROM cleanup_items").fetchone()[0]
        assert not target.exists()
        assert unlink_calls == [target]
        assert active_calls
        assert owned.connection.execute(
            "SELECT a.status FROM operation_attempts a JOIN operation_runs r"
            " ON r.id=a.run_id WHERE r.cleanup_item_id=? AND r.kind=4",
            (item_id,)).fetchone() == (1,)
        cleanup.cancel()
        await asyncio.wait_for(drain_started.wait(), timeout=5)
        assert active_calls
        assert not cleanup.done()
    finally:
        release_runner.set()

    with pytest.raises(asyncio.CancelledError):
        await cleanup

    observed = context.open_connection()
    try:
        saved = saved_cleanup_delete(observed.connection, item_id)
        attempt = observed.connection.execute(
            "SELECT a.status FROM operation_attempts a JOIN operation_runs r"
            " ON r.id=a.run_id WHERE r.cleanup_item_id=? AND r.kind=4",
            (item_id,)).fetchone()
        item = observed.connection.execute(
            "SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
    finally:
        observed.connection.close()
    assert saved is not None
    assert saved.actual.outcome.settlement.basis.value == "observed"
    assert saved.actual.observations[0].type == "file_absent"
    assert attempt == (2,)
    assert item == (4,)
    assert unlink_calls == [target]
    assert len(worker_threads) == 1 and worker_threads[0] != loop_thread
    assert not active_calls
    assert work_files.file_supervisor.outstanding() == ()


async def test_owner_failure_recovers_same_handle_and_saves_actual_before_error(
        host_environment, monkeypatch):
    """supervisor owner 失败不重投，collector 用原 handle 保留 actual 后报告错误。"""
    cfg, owned, context, driver, work_files = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    _accept_cleanup(owned, output_id)

    unlink_calls = []
    original_unlink = Path.unlink

    def count_unlink(path, *args, **kwargs):
        if path == target:
            unlink_calls.append(path)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", count_unlink)
    runner = work_files.executor._runner

    async def fail_after_completion(body):
        await runner(body)
        raise RuntimeError("runner failed after the shared completion formed")

    async def fail_owner(_self, _task):
        raise RuntimeError("supervisor owner failed before reading its handle")

    take_over = _LocalArtifactResultOwner.take_over
    monkeypatch.setattr(work_files.executor, "_runner", fail_after_completion)
    monkeypatch.setattr(_LocalArtifactResultOwner, "take_over", fail_owner)
    pending_deletes = {}
    factory = _cleanup_factory(
        cfg, driver, work_files, pending_deletes=pending_deletes)
    with pytest.raises(ConsistencyError, match="监督交接失败"):
        await cleanup_flow(factory)(context)

    item_id = owned.connection.execute("SELECT id FROM cleanup_items").fetchone()[0]
    saved = saved_cleanup_delete(owned.connection, item_id)
    assert saved is not None
    assert saved.actual.observations[0].type == "file_absent"
    assert len(pending_deletes) == 1
    assert next(iter(pending_deletes.values())).actual.observations == saved.actual.observations
    assert owned.connection.execute(
        "SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone() == (3,)
    assert unlink_calls == [target]
    assert not target.exists()
    assert work_files.executor.unfinished_files() == ()
    assert work_files.file_supervisor.outstanding() == ()

    # 同一 pending 在新 factory 中完成成员交接，不重新提交文件任务。
    monkeypatch.setattr(work_files.executor, "_runner", runner)
    monkeypatch.setattr(_LocalArtifactResultOwner, "take_over", take_over)
    await cleanup_flow(_cleanup_factory(
        cfg, driver, work_files, pending_deletes=pending_deletes))(context)
    assert owned.connection.execute(
        "SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone() == (4,)
    assert unlink_calls == [target]


async def test_canceled_owner_failure_saves_actual_and_chains_drain_error(
        host_environment, monkeypatch):
    """取消时 owner drain 失败仍从原 handle 保存缺席，并把故障挂到取消原因。"""
    cfg, owned, context, driver, work_files = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    _accept_cleanup(owned, output_id)

    loop = asyncio.get_running_loop()
    runner_returned = asyncio.Event()
    release_runner = asyncio.Event()
    owner_failed = asyncio.Event()
    unlink_calls = []
    original_unlink = Path.unlink
    runner = work_files.executor._runner
    take_over = _LocalArtifactResultOwner.take_over
    active_calls = set()

    def count_unlink(path, *args, **kwargs):
        if path == target:
            unlink_calls.append(path)
        return original_unlink(path, *args, **kwargs)

    async def hold_delivery(body):
        result = await runner(body)
        runner_returned.set()
        await release_runner.wait()
        return result

    async def fail_owner(_self, _task):
        owner_failed.set()
        raise RuntimeError("supervisor owner failed during cancellation drain")

    monkeypatch.setattr(Path, "unlink", count_unlink)
    monkeypatch.setattr(work_files.executor, "_runner", hold_delivery)
    monkeypatch.setattr(_LocalArtifactResultOwner, "take_over", fail_owner)
    pending_deletes = {}
    cleanup = asyncio.create_task(cleanup_flow(_cleanup_factory(
        cfg, driver, work_files, active_calls=active_calls,
        pending_deletes=pending_deletes))(context))
    try:
        await asyncio.wait_for(runner_returned.wait(), timeout=5)
        assert not target.exists()
        item_id = owned.connection.execute("SELECT id FROM cleanup_items").fetchone()[0]
        assert active_calls
        assert owned.connection.execute(
            "SELECT a.status FROM operation_attempts a JOIN operation_runs r"
            " ON r.id=a.run_id WHERE r.cleanup_item_id=? AND r.kind=4",
            (item_id,)).fetchone() == (1,)
        cleanup.cancel()
        await asyncio.wait_for(owner_failed.wait(), timeout=5)
        release_runner.set()
        with pytest.raises(asyncio.CancelledError) as canceled:
            await cleanup
    finally:
        release_runner.set()

    observed = context.open_connection()
    try:
        saved = saved_cleanup_delete(observed.connection, item_id)
        item = observed.connection.execute(
            "SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
        attempt = observed.connection.execute(
            "SELECT a.status FROM operation_attempts a JOIN operation_runs r"
            " ON r.id=a.run_id WHERE r.cleanup_item_id=? AND r.kind=4",
            (item_id,)).fetchone()
    finally:
        observed.connection.close()
    assert saved is not None
    assert saved.actual.observations[0].type == "file_absent"
    assert item == (3,)
    assert attempt == (2,)
    assert canceled.value.__cause__ is not None
    assert "supervisor owner failed" in str(canceled.value.__cause__)
    assert unlink_calls == [target]
    assert active_calls == set()
    assert work_files.file_supervisor.outstanding() == ()
    monkeypatch.setattr(_LocalArtifactResultOwner, "take_over", take_over)
    pending = pending_deletes[item_id]
    assert pending.supervision_diagnostic is not None
    assert pending.supervision_diagnostic_delivered
    await cleanup_flow(_cleanup_factory(
        cfg, driver, work_files, active_calls=active_calls,
        pending_deletes=pending_deletes))(context)
    assert owned.connection.execute(
        "SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone() == (4,)
    assert not pending_deletes
    assert unlink_calls == [target]


async def test_host_delete_commit_unknown_recovers_same_save_without_task_replay(
        host_environment, monkeypatch):
    """主机 DELETE 提交后回执 UNKNOWN 时以原保存身份恢复，不重做 unlink。"""
    from camctl.outputs.cleanup_flow import delete_source_file
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.persistence.repositories.outputs import OutputsRepository

    cfg, owned, context, driver, work_files = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    _accept_cleanup(owned, output_id)
    pending_deletes = {}
    pending_intents = {}
    active_calls = set()
    factory = _cleanup_factory(
        cfg, driver, work_files, pending_deletes=pending_deletes,
        pending_intents=pending_intents, active_calls=active_calls)
    runtime = factory(owned)

    original_finish = OutputsRepository.finish_cleanup_attempt
    finish_calls = []

    def unknown_after_commit(repository, finish, key, current_owned):
        receipt = original_finish(repository, finish, key, current_owned)
        finish_calls.append((finish, key, receipt))
        if len(finish_calls) == 1:
            return DbOutcome(
                DbOutcomeKind.UNKNOWN,
                error=sqlite3.OperationalError("commit result not observable"),
            )
        return receipt

    monkeypatch.setattr(
        OutputsRepository, "finish_cleanup_attempt", unknown_after_commit)
    submitted = []
    run_file_task = work_files.executor.run_file_task

    async def track_task(task, owner, **kwargs):
        if task.business == "host_artifact_cleanup":
            submitted.append(task)
        return await run_file_task(task, owner, **kwargs)

    monkeypatch.setattr(work_files.executor, "run_file_task", track_task)
    cleanup = cleanup_flow(factory)
    with pytest.raises(ConsistencyError):
        await cleanup(context)
    item_id = owned.connection.execute(
        "SELECT id FROM cleanup_items WHERE output_id=?", (output_id,)).fetchone()[0]
    original_pending = pending_deletes[item_id]
    original_actual = original_pending.actual
    original_key = original_pending.key
    original_ticket = original_pending.ticket
    original_finish_request = original_pending.finish
    original_occurred_at = original_pending.occurred_at
    assert original_actual.observations[0].type == "file_absent"
    assert not target.exists()
    assert len(submitted) == 1
    assert active_calls == set()

    owned.connection.close()
    await cleanup(context)
    fresh = context.open_connection()
    try:
        saved = saved_cleanup_delete(fresh.connection, item_id)
        assert saved is not None
        assert saved.ticket == original_ticket
        assert saved.key == original_key
        assert saved.actual.observations == original_actual.observations
        assert len(finish_calls) == 2
        assert finish_calls[0][0] == finish_calls[1][0] == original_finish_request
        assert finish_calls[0][1] == finish_calls[1][1] == original_key
        assert finish_calls[0][2].kind is DbOutcomeKind.COMPLETED
        assert finish_calls[1][2].kind is DbOutcomeKind.COMPLETED
        assert original_pending.actual is original_actual
        assert original_pending.occurred_at == original_occurred_at
        assert not pending_deletes and not pending_intents
        assert len(submitted) == 1
        assert not target.exists()
        assert active_calls == set()
        assert work_files.executor.unfinished_files() == ()
    finally:
        fresh.connection.close()


@pytest.mark.parametrize("query_error", [False, True])
async def test_started_host_query_cancellation_keeps_query_consumer_for_reentry(
        host_environment, monkeypatch, query_error):
    """取消期间 QUERY 仍由原 AFTER_DELETE ticket 保存，再入场时不重查文件。"""
    cfg, owned, context, driver, work_files = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    _accept_cleanup(owned, output_id)

    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    gate = threading.Event()
    query_enabled = threading.Event()
    stat_calls = []
    worker_threads = []
    original_stat = os.stat
    from camctl.host_files import paths as host_paths

    def tracked_stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if path == target:
            stat_calls.append(path)
            worker_threads.append(threading.get_ident())
            if query_enabled.is_set() and not entered.is_set():
                loop.call_soon_threadsafe(entered.set)
                gate.wait()
                if query_error:
                    raise PermissionError("query result cannot be observed")
        return result

    monkeypatch.setattr(host_paths.os, "stat", tracked_stat)
    original_unlink = Path.unlink

    def failed_unlink(path, *args, **kwargs):
        if path == target:
            raise PermissionError("keep repaired artifact for verification")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failed_unlink)
    original_run = HostArtifacts._run
    host_operations = []

    async def enable_query_gate(self, request):
        host_operations.append(request.operation)
        if request.operation == "query":
            query_enabled.set()
        return await original_run(self, request)

    monkeypatch.setattr(HostArtifacts, "_run", enable_query_gate)

    active_calls = set()
    pending_queries = {}
    consumed = {}
    factory = _cleanup_factory(
        cfg, driver, work_files, active_calls=active_calls,
        pending_queries=pending_queries, consumed_queries=consumed)
    cleanup = asyncio.create_task(cleanup_flow(factory)(context))
    drain_started = asyncio.Event()
    drain_required = work_files.executor.drain_required

    async def tracked_drain():
        drain_started.set()
        return await drain_required()

    monkeypatch.setattr(work_files.executor, "drain_required", tracked_drain)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        item_id = owned.connection.execute("SELECT id FROM cleanup_items").fetchone()[0]
        running = owned.connection.execute(
            "SELECT r.kind,a.status FROM operation_attempts a JOIN operation_runs r"
            " ON r.id=a.run_id WHERE r.cleanup_item_id=? ORDER BY a.id", (item_id,)).fetchall()
        assert running[-1] == (5, 1)
        assert active_calls
        assert 900 in work_files.executor.unfinished_files()
        stats_when_query_paused = len(stat_calls)
        cleanup.cancel()
        await asyncio.wait_for(drain_started.wait(), timeout=5)
        assert active_calls
        assert 900 in work_files.executor.unfinished_files()
        assert not cleanup.done()
    finally:
        gate.set()

    with pytest.raises(asyncio.CancelledError):
        await cleanup

    observed = context.open_connection()
    try:
        saved = saved_cleanup_query(observed.connection, item_id)
        attempts = observed.connection.execute(
            "SELECT r.kind,a.status FROM operation_attempts a JOIN operation_runs r"
            " ON r.id=a.run_id WHERE r.cleanup_item_id=? ORDER BY a.id", (item_id,)).fetchall()
        item = observed.connection.execute(
            "SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
    finally:
        observed.connection.close()
    assert saved is not None and saved.after_delete
    if query_error:
        assert saved.actual.observations == ()
        assert saved.actual.error["code"] == "local_io_failed"
        assert attempts[-1] == (5, 3)
    else:
        assert saved.actual.observations[0].data == {
            "cleanup_item_id": str(item_id), "present": True}
        assert attempts[-1] == (5, 2)
    assert item == (3,)
    assert len(pending_queries) == 1
    pending = next(iter(pending_queries.values()))
    assert pending.consumer.value == "after_delete"
    assert pending.phase.value == "saved"
    assert active_calls == set()
    assert worker_threads and all(thread != loop_thread for thread in worker_threads)
    assert original_stat(target).st_size == len(b"repaired-artifact")
    assert 900 not in work_files.executor.unfinished_files()
    # 取消前已经开始的 QUERY 在线程里只观察一次；这次结果形成后，
    # 下方新工厂复入不应再提交 QUERY，也不应再次观察目标。
    assert len(stat_calls) == stats_when_query_paused

    # 新工厂先接回已保存 consumer；这一步不得重新 stat 目标文件。
    before_resume_stats = len(stat_calls)
    operations_before_resume = tuple(host_operations)
    await cleanup_flow(_cleanup_factory(
        cfg, driver, work_files, active_calls=active_calls,
        pending_queries=pending_queries, consumed_queries=consumed))(context)
    assert len(stat_calls) == before_resume_stats
    assert tuple(host_operations) == operations_before_resume
    assert not pending_queries


async def test_fresh_host_target_path_error_stops_before_attempt_ticket(
        host_environment, monkeypatch):
    """fresh FileRef 纯路径错误在授票前失败，不生成 attempt 或 worker。"""
    cfg, owned, context, driver, work_files = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    _accept_cleanup(owned, output_id)
    calls = []

    def invalid_target(file_ref, directories):
        calls.append(file_ref)
        raise ValueError("controlled unsafe path")

    monkeypatch.setattr(host_paths, "resolve_file", invalid_target)
    factory = _cleanup_factory(cfg, driver, work_files)
    with pytest.raises(ConsistencyError, match="FileRef 身份或路径不合法"):
        await cleanup_flow(factory)(context)

    assert len(calls) == 1
    assert calls[0].file_id == 900
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM operation_attempts WHERE run_id IN"
        " (SELECT id FROM operation_runs WHERE cleanup_item_id IS NOT NULL)").fetchone() == (0,)
    assert target.is_file()
    assert work_files.executor.unfinished_files() == ()
    assert work_files.file_supervisor.outstanding() == ()


async def test_legal_ticket_target_recheck_records_real_not_dispatched(
        host_environment, monkeypatch):
    """合法原票据获准后目标纯校验失败，沿该票据保存未派发结果。"""
    cfg, owned, context, driver, work_files = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    _accept_cleanup(owned, output_id)
    resolve_file = host_paths.resolve_file
    calls = []

    def invalid_second_check(file_ref, directories):
        calls.append(file_ref)
        if len(calls) == 2:
            raise ValueError("path changed after ticket grant")
        return resolve_file(file_ref, directories)

    monkeypatch.setattr(host_paths, "resolve_file", invalid_second_check)
    submitted = []
    run_file_task = work_files.executor.run_file_task

    async def track_task(task, owner, **kwargs):
        if task.business == "host_artifact_cleanup":
            submitted.append(task)
        return await run_file_task(task, owner, **kwargs)

    monkeypatch.setattr(work_files.executor, "run_file_task", track_task)
    await cleanup_flow(_cleanup_factory(cfg, driver, work_files))(context)

    item_id = owned.connection.execute("SELECT id FROM cleanup_items").fetchone()[0]
    saved = saved_cleanup_delete(owned.connection, item_id)
    assert len(calls) == 2
    assert saved is not None
    assert saved.actual.outcome.settlement.basis is SettlementBasis.NOT_DISPATCHED
    assert saved.actual.observations == ()
    assert saved.actual.error is not None
    assert target.is_file()
    assert submitted == []
    assert work_files.executor.unfinished_files() == ()


async def test_ticket_identity_error_is_not_rewritten_as_not_dispatched(
        host_environment, monkeypatch):
    """原票据关联不成立时保留 RUNNING 意图，不伪造撤回 completion。"""
    cfg, owned, context, driver, work_files = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    _accept_cleanup(owned, output_id)
    calls = []

    def mismatched_ticket(runtime, item_id, ticket, operation):
        calls.append((item_id, ticket, operation))
        raise ConsistencyError("controlled ticket/member mismatch")

    monkeypatch.setattr(cleanup_flow_module, "_local_artifact_request", mismatched_ticket)
    factory = _cleanup_factory(cfg, driver, work_files)
    with pytest.raises(ConsistencyError, match="ticket/member mismatch"):
        await cleanup_flow(factory)(context)

    item_id, ticket, operation = calls[0]
    assert operation == "delete"
    assert ticket.target_id == str(item_id)
    assert owned.connection.execute(
        "SELECT a.status,a.result_event_id FROM operation_attempts a JOIN operation_runs r"
        " ON r.id=a.run_id WHERE r.cleanup_item_id=? AND r.kind=4",
        (item_id,)).fetchone() == (1, None)
    assert saved_cleanup_delete(owned.connection, item_id) is None
    assert target.is_file()
    assert work_files.executor.unfinished_files() == ()


@pytest.mark.parametrize("conflict", ["duplicate_task_id", "same_file_id"])
async def test_host_executor_admission_conflicts_preserve_original_owner(
        host_environment, monkeypatch, conflict):
    """两种 admission 冲突都拒绝第二 owner，原任务继续到实际交付。"""
    cfg, owned, context, driver, work_files = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    _accept_cleanup(owned, output_id)

    loop = asyncio.get_running_loop()
    original_started = asyncio.Event()
    release_original = threading.Event()
    release_adapter = asyncio.Event()
    conflict_seen = asyncio.Event()
    original_calls = []
    original_result = {}
    original_owner = _Owner()
    original_run = HostArtifacts._run

    def original_body(_stop_event):
        original_calls.append(conflict)
        loop.call_soon_threadsafe(original_started.set)
        release_original.wait()
        return "original-owner-completed"

    async def conflict_adapter(self, request):
        task_id = FileTaskId(
            f"cleanup:{request.database_id}:{request.operation}:"
            f"{request.ticket.run_id}:{request.ticket.attempt_id}")
        task = FileTask(
            task_id=(task_id if conflict == "duplicate_task_id"
                     else FileTaskId("r4-existing-same-file")),
            file_id=(901 if conflict == "duplicate_task_id" else request.file_ref.file_id),
            stage="test_original", business="test_original", body=original_body)
        task_waiter = asyncio.create_task(
            work_files.executor.run_file_task(task, original_owner))
        try:
            await asyncio.wait_for(original_started.wait(), timeout=5)
            with pytest.raises(ConsistencyError):
                await original_run(self, request)
            conflict_seen.set()
            await release_adapter.wait()
            release_original.set()
            original_result["result"] = await task_waiter
            raise ConsistencyError("host cleanup admission conflict")
        finally:
            release_original.set()
            if not task_waiter.done():
                await asyncio.gather(task_waiter, return_exceptions=True)

    monkeypatch.setattr(HostArtifacts, "_run", conflict_adapter)
    cleanup = asyncio.create_task(cleanup_flow(_cleanup_factory(
        cfg, driver, work_files))(context))
    try:
        await asyncio.wait_for(conflict_seen.wait(), timeout=5)
        item_id = owned.connection.execute("SELECT id FROM cleanup_items").fetchone()[0]
        row = owned.connection.execute(
            "SELECT a.status,a.result_event_id FROM operation_attempts a JOIN operation_runs r"
            " ON r.id=a.run_id WHERE r.cleanup_item_id=? AND r.kind=4",
            (item_id,)).fetchone()
        assert row == (1, None)
        assert target.is_file()
        assert original_calls == [conflict]
        assert (901 if conflict == "duplicate_task_id" else 900) in (
            work_files.executor.unfinished_files())
        assert not cleanup.done()
        release_adapter.set()
        with pytest.raises(ConsistencyError, match="admission conflict"):
            await cleanup
    finally:
        release_adapter.set()
        release_original.set()
        if not cleanup.done():
            await asyncio.gather(cleanup, return_exceptions=True)

    assert original_result["result"].ran
    assert original_result["result"].value == "original-owner-completed"
    assert original_calls == [conflict]
    assert saved_cleanup_delete(owned.connection, item_id) is None
    assert work_files.executor.unfinished_files() == ()
    assert work_files.file_supervisor.outstanding() == ()


async def test_cleanup_drain_preserves_failure_from_another_file_owner(
        host_environment, monkeypatch):
    """本项收场消费共享 drain 时，不能丢弃另一文件责任的失败。"""
    cfg, owned, context, driver, work_files = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    _accept_cleanup(owned, output_id)

    loop = asyncio.get_running_loop()
    other_started = asyncio.Event()
    release_other = threading.Event()
    other_file_id = 901

    def other_body(_stop_event):
        loop.call_soon_threadsafe(other_started.set)
        release_other.wait()
        return "other owner actual"

    other_task = FileTask(
        FileTaskId("r4-shared-other-owner"), other_file_id,
        "other_file_cleanup", "other_file_business", other_body,
        resources=("database:other-db", "other-responsibility"))
    other_waiter = asyncio.create_task(work_files.executor.run_file_task(
        other_task, _FailingOwner("PROBE unrelated required file owner failure")))
    try:
        await asyncio.wait_for(other_started.wait(), timeout=5)
        other_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await other_waiter
    finally:
        release_other.set()
    await _wait_until(lambda: other_file_id not in work_files.executor.unfinished_files())

    original_runner = work_files.executor._runner

    async def fail_cleanup_runner_after_completion(body):
        await original_runner(body)
        raise RuntimeError("PROBE current cleanup runner fault")

    monkeypatch.setattr(
        work_files.executor, "_runner", fail_cleanup_runner_after_completion)
    pending_deletes = {}
    unlink_calls = []
    original_unlink = Path.unlink

    def count_unlink(path, *args, **kwargs):
        if path == target:
            unlink_calls.append(path)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", count_unlink)
    with pytest.raises(ConsistencyError, match="监督交接失败") as caught:
        await cleanup_flow(_cleanup_factory(
            cfg, driver, work_files, pending_deletes=pending_deletes))(context)

    item_id = owned.connection.execute(
        "SELECT id FROM cleanup_items WHERE output_id=?", (output_id,)).fetchone()[0]
    pending = pending_deletes[item_id]
    diagnostic = getattr(pending, "supervision_diagnostic", None)
    assert diagnostic is not None
    assert any(fact.identity == "r4-shared-other-owner"
               and fact.error_message == "PROBE unrelated required file owner failure"
               and fact.resources == (
                   "file:901", "database:other-db", "other-responsibility")
               for fact in diagnostic.facts)
    assert "PROBE unrelated required file owner failure" in _exception_chain_text(caught.value)
    saved = saved_cleanup_delete(owned.connection, item_id)
    assert saved is not None
    assert saved.actual.observations[0].type == "file_absent"
    assert saved.actual.error is None
    assert unlink_calls == [target]
    assert not target.exists()
    assert work_files.executor.unfinished_files() == ()
    assert work_files.file_supervisor.outstanding() == ()


async def test_runner_failure_survives_cancellation_during_shared_drain(
        host_environment, monkeypatch):
    """runner 先失败、collector drain 后取消时，取消仍保留原 runner 诊断。"""
    import camctl.bootstrap.cleanup_assembly as cleanup_assembly_module

    cfg, owned, context, driver, work_files = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    _accept_cleanup(owned, output_id)

    loop = asyncio.get_running_loop()
    owner_waiting = asyncio.Event()
    release_owner = asyncio.Event()
    collector_cancel_seen = asyncio.Event()
    active_calls = set()
    original_runner = work_files.executor._runner
    original_take_over = _LocalArtifactResultOwner.take_over
    original_collect = cleanup_assembly_module._collect_file_result
    collect_task = None

    async def fail_after_completion(body):
        await original_runner(body)
        raise RuntimeError("PROBE runner failure before later waiter cancellation")

    async def delayed_take_over(self, task):
        owner_waiting.set()
        await release_owner.wait()
        self.result = await task.pending.wait()

    async def observe_collect(*args, **kwargs):
        nonlocal collect_task
        collect_task = asyncio.current_task()
        return await original_collect(*args, **kwargs)

    original_shield = asyncio.shield

    def observed_shield(awaitable):
        shielded = original_shield(awaitable)
        if asyncio.current_task() is not collect_task:
            return shielded

        async def wait_for_shielded():
            try:
                return await shielded
            except asyncio.CancelledError:
                collector_cancel_seen.set()
                raise

        return wait_for_shielded()

    monkeypatch.setattr(work_files.executor, "_runner", fail_after_completion)
    monkeypatch.setattr(_LocalArtifactResultOwner, "take_over", delayed_take_over)
    monkeypatch.setattr(cleanup_assembly_module, "_collect_file_result", observe_collect)
    monkeypatch.setattr(asyncio, "shield", observed_shield)
    pending_deletes = {}
    cleanup = asyncio.create_task(cleanup_flow(_cleanup_factory(
        cfg, driver, work_files, pending_deletes=pending_deletes,
        active_calls=active_calls))(context))
    try:
        await asyncio.wait_for(owner_waiting.wait(), timeout=5)
        cleanup.cancel()
        await asyncio.wait_for(collector_cancel_seen.wait(), timeout=5)
    finally:
        release_owner.set()

    with pytest.raises(asyncio.CancelledError) as canceled:
        await cleanup

    cancellation_chain = _exception_chain_text(canceled.value)
    assert "PROBE runner failure before later waiter cancellation" in cancellation_chain
    item_id = owned.connection.execute(
        "SELECT id FROM cleanup_items WHERE output_id=?", (output_id,)).fetchone()[0]
    pending = pending_deletes[item_id]
    diagnostic = getattr(pending, "supervision_diagnostic", None)
    assert diagnostic is not None
    assert any(fact.error_message ==
               "PROBE runner failure before later waiter cancellation"
               for fact in diagnostic.facts)
    assert getattr(pending, "supervision_diagnostic_delivered", False)
    saved = saved_cleanup_delete(owned.connection, item_id)
    assert saved is not None
    assert saved.actual.observations[0].type == "file_absent"
    assert owned.connection.execute(
        "SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone() == (3,)
    assert active_calls == set()
    assert work_files.executor.unfinished_files() == ()
    assert work_files.file_supervisor.outstanding() == ()
    monkeypatch.setattr(work_files.executor, "_runner", original_runner)
    monkeypatch.setattr(_LocalArtifactResultOwner, "take_over", original_take_over)


async def test_delete_supervision_diagnostic_survives_unknown_finish(
        host_environment, monkeypatch):
    """DELETE commit UNKNOWN 后原监督诊断随同 actual/key/time 等待交付。"""
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.persistence.repositories.outputs import OutputsRepository

    cfg, owned, context, driver, work_files = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    _accept_cleanup(owned, output_id)
    pending_deletes = {}
    original_runner = work_files.executor._runner

    async def fail_delete_runner_after_completion(body):
        await original_runner(body)
        raise RuntimeError("PROBE DELETE runner diagnosis survives UNKNOWN")

    monkeypatch.setattr(
        work_files.executor, "_runner", fail_delete_runner_after_completion)
    original_repository_finish = OutputsRepository.finish_cleanup_attempt
    finish_calls = []

    def unknown_after_delete_commit(repository, finish, key, current_owned):
        receipt = original_repository_finish(repository, finish, key, current_owned)
        finish_calls.append((finish, key, receipt))
        if len(finish_calls) == 1:
            return DbOutcome(
                DbOutcomeKind.UNKNOWN,
                error=sqlite3.OperationalError("PROBE DELETE commit result is unknown"),
            )
        return receipt

    monkeypatch.setattr(
        OutputsRepository, "finish_cleanup_attempt", unknown_after_delete_commit)
    submitted = []
    original_run_file_task = work_files.executor.run_file_task

    async def track_file_task(task, owner, **kwargs):
        if task.business == "host_artifact_cleanup":
            submitted.append(task)
        return await original_run_file_task(task, owner, **kwargs)

    monkeypatch.setattr(work_files.executor, "run_file_task", track_file_task)
    cleanup = cleanup_flow(_cleanup_factory(
        cfg, driver, work_files, pending_deletes=pending_deletes))
    with pytest.raises(ConsistencyError) as first:
        await cleanup(context)

    item_id = owned.connection.execute(
        "SELECT id FROM cleanup_items WHERE output_id=?", (output_id,)).fetchone()[0]
    pending = pending_deletes[item_id]
    original_actual = pending.actual
    original_key = pending.key
    original_ticket = pending.ticket
    original_pending_finish = pending.finish
    original_occurred_at = pending.occurred_at
    diagnostic = getattr(pending, "supervision_diagnostic", None)
    assert diagnostic is not None
    assert not getattr(pending, "supervision_diagnostic_delivered", True)
    first_text = _exception_chain_text(first.value)
    assert "PROBE DELETE commit result is unknown" in first_text
    assert "PROBE DELETE runner diagnosis survives UNKNOWN" in first_text
    assert saved_cleanup_delete(owned.connection, item_id) is not None
    assert original_actual.observations[0].type == "file_absent"
    assert not target.exists()

    monkeypatch.setattr(work_files.executor, "_runner", original_runner)
    owned.connection.close()
    with pytest.raises(ConsistencyError) as recovered:
        await cleanup(context)
    assert "PROBE DELETE runner diagnosis survives UNKNOWN" in _exception_chain_text(
        recovered.value)
    assert pending.supervision_diagnostic_delivered, (
        _exception_chain_text(recovered.value), pending.saved,
        [(call[2].kind.value, str(call[2].error)) for call in finish_calls])
    assert pending.actual is original_actual
    assert pending.key == original_key
    assert pending.ticket == original_ticket
    assert pending.finish is original_pending_finish
    assert pending.occurred_at == original_occurred_at

    await cleanup(context)
    assert not pending_deletes
    assert [task.task_id.value for task in submitted].count(
        f"cleanup:{pending.database_id}:delete:{original_ticket.run_id}:"
        f"{original_ticket.attempt_id}") == 1
    assert len(finish_calls) == 2
    assert finish_calls[0][0] is original_pending_finish
    assert finish_calls[1][0] is original_pending_finish
    assert finish_calls[0][1] is original_key
    assert finish_calls[1][1] is original_key
    assert work_files.executor.unfinished_files() == ()


async def test_query_supervision_diagnostic_survives_unknown_finish(
        host_environment, monkeypatch):
    """QUERY commit UNKNOWN 后不丢 original pending 中的监督诊断。"""
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.persistence.repositories.operations import OperationRepository

    cfg, owned, context, driver, work_files = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    _accept_cleanup(owned, output_id)
    pending_queries = {}
    original_runner = work_files.executor._runner
    original_repository_finish = OperationRepository.finish_attempt
    finish_calls = []

    def unknown_after_query_commit(repository, finish, key, current_owned):
        receipt = original_repository_finish(repository, finish, key, current_owned)
        finish_calls.append((finish, key, receipt))
        if finish.ticket.operation == "query" and len(finish_calls) == 1:
            return DbOutcome(
                DbOutcomeKind.UNKNOWN,
                error=sqlite3.OperationalError("PROBE QUERY commit result is unknown"),
            )
        return receipt

    monkeypatch.setattr(
        OperationRepository, "finish_attempt", unknown_after_query_commit)
    submitted = []
    original_run_file_task = work_files.executor.run_file_task

    async def track_file_task(task, owner, **kwargs):
        if task.business == "host_artifact_cleanup":
            submitted.append(task)
        if task.stage != "cleanup_query":
            return await original_run_file_task(task, owner, **kwargs)

        async def fail_after_completion(body):
            await original_runner(body)
            raise RuntimeError("PROBE QUERY runner diagnosis survives UNKNOWN")

        work_files.executor._runner = fail_after_completion
        try:
            return await original_run_file_task(task, owner, **kwargs)
        finally:
            work_files.executor._runner = original_runner

    monkeypatch.setattr(work_files.executor, "run_file_task", track_file_task)
    original_unlink = Path.unlink

    def keep_target_present(path, *args, **kwargs):
        if path == target:
            raise PermissionError("PROBE DELETE leaves the artifact for QUERY")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", keep_target_present)
    cleanup = cleanup_flow(_cleanup_factory(
        cfg, driver, work_files, pending_queries=pending_queries))
    with pytest.raises(ConsistencyError) as first:
        await cleanup(context)

    assert len(pending_queries) == 1, (
        _exception_chain_text(first.value),
        [(task.stage, task.task_id.value) for task in submitted],
        [call[0].ticket.operation for call in finish_calls])
    pending = next(iter(pending_queries.values()))
    original_actual = pending.actual
    original_key = pending.key
    original_ticket = pending.ticket
    original_pending_finish = pending.finish
    original_occurred_at = pending.occurred_at
    item_id = pending.item_id
    diagnostic = getattr(pending, "supervision_diagnostic", None)
    assert diagnostic is not None
    assert not getattr(pending, "supervision_diagnostic_delivered", True)
    first_text = _exception_chain_text(first.value)
    assert "PROBE QUERY commit result is unknown" in first_text
    assert "PROBE QUERY runner diagnosis survives UNKNOWN" in first_text
    saved = saved_cleanup_query(owned.connection, pending.item_id)
    assert saved is not None
    assert saved.actual.observations == original_actual.observations
    assert saved.actual.observations[0].data == {
        "cleanup_item_id": str(pending.item_id), "present": True}
    assert target.is_file()

    monkeypatch.setattr(work_files.executor, "_runner", original_runner)
    owned.connection.close()
    with pytest.raises(ConsistencyError) as recovered:
        await cleanup(context)
    assert "PROBE QUERY runner diagnosis survives UNKNOWN" in _exception_chain_text(
        recovered.value)
    assert pending.supervision_diagnostic_delivered, (
        _exception_chain_text(recovered.value), pending.phase,
        [(call[2].kind.value, str(call[2].error)) for call in finish_calls])
    assert pending.actual is original_actual
    assert pending.key == original_key
    assert pending.ticket == original_ticket
    assert pending.finish is original_pending_finish
    assert pending.occurred_at == original_occurred_at

    await cleanup(context)
    assert len(finish_calls) >= 2
    assert finish_calls[0][0] is original_pending_finish
    assert finish_calls[1][0] is original_pending_finish
    assert finish_calls[0][1] is original_key
    assert finish_calls[1][1] is original_key
    assert target.is_file()
    first_task_id = (
        f"cleanup:{pending.database_id}:query:{original_ticket.run_id}:"
        f"{original_ticket.attempt_id}")
    assert [task.task_id.value for task in submitted].count(first_task_id) == 1
    assert work_files.executor.unfinished_files() == ()
    assert target.is_file()


async def test_late_owner_callback_does_not_retain_handle_after_fallback(
        host_environment, monkeypatch):
    """fallback 取回结果后，最后一次真实 drain 不得重新长期持有 handle。"""
    cfg, owned, context, driver, work_files = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    _accept_cleanup(owned, output_id)

    original_runner = work_files.executor._runner
    real_drain = work_files.executor.drain_required
    unlink_calls = []
    original_unlink = Path.unlink

    async def fail_after_completion(body):
        await original_runner(body)
        raise RuntimeError("PROBE runner failure triggers fallback")

    async def drain_fails_before_owner_consumption():
        raise RuntimeError("PROBE shared drain fails before owner consumption")

    def count_unlink(path, *args, **kwargs):
        if path == target:
            unlink_calls.append(path)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(work_files.executor, "_runner", fail_after_completion)
    monkeypatch.setattr(
        work_files.executor, "drain_required", drain_fails_before_owner_consumption)
    monkeypatch.setattr(Path, "unlink", count_unlink)
    pending_deletes = {}
    with pytest.raises(ConsistencyError, match="监督交接失败"):
        await cleanup_flow(_cleanup_factory(
            cfg, driver, work_files, pending_deletes=pending_deletes))(context)

    owners = [owner for owner in work_files.file_supervisor._owners.values()
              if isinstance(owner, _LocalArtifactResultOwner)]
    assert len(owners) == 1
    assert owners[0].handle is None
    assert owners[0].result is not None
    assert owners[0].result.ran
    assert unlink_calls == [target]
    assert not target.exists()
    item_id = owned.connection.execute(
        "SELECT id FROM cleanup_items WHERE output_id=?", (output_id,)).fetchone()[0]
    saved = saved_cleanup_delete(owned.connection, item_id)
    assert saved is not None and saved.actual.observations[0].type == "file_absent"

    monkeypatch.setattr(work_files.executor, "drain_required", real_drain)
    await work_files.file_supervisor.drain_required()
    assert owners[0].handle is None
    assert owners[0].result is not None
    assert owners[0].result.ran
    assert unlink_calls == [target]
    assert len(pending_deletes) == 1
    assert work_files.executor.unfinished_files() == ()


@pytest.mark.parametrize("scope_inside", [False, True],
                         ids=["scope-outside", "owned-scope"])
@pytest.mark.parametrize("cancel_waiter", [False, True],
                         ids=["no-cancellation", "waiter-cancelled"])
@pytest.mark.parametrize("runner_failure", [False, True],
                         ids=["no-diagnostic", "runner-diagnostic"])
async def test_host_task_control_and_diagnostic_delivery_matrix(
        host_environment, monkeypatch, scope_inside, cancel_waiter, runner_failure):
    """scope、取消与监督诊断组合都交回原 actual 和各自责任。"""
    import camctl.bootstrap.cleanup_assembly as cleanup_assembly_module
    from camctl.operations.models import AttemptTicket
    from camctl.operations.owned_calls import current_call_scope, owned_tool_call
    from camctl.outputs.cleanup_flow import LocalArtifactRequest, LocalArtifactTaskFailure
    from camctl.host_files.models import FileRef

    cfg, _owned, _context, _driver, work_files = host_environment
    staging = Path(cfg.paths.staging)
    target = staging / "derived" / "900.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"scope-outside-cancel")
    ticket = AttemptTicket(1, "delete", "1", "delete/1", 1)
    request = LocalArtifactRequest(
        "scope-outside-test", "delete", 1, ticket,
        FileRef(900, FilePurpose.REPAIR_OUTPUT, "derived/900.jpg", staging),
    )
    port = HostArtifacts(staging, work_files.executor)
    assert current_call_scope() is None

    loop = asyncio.get_running_loop()
    body_started = asyncio.Event()
    collect_started = asyncio.Event()
    cancel_seen = asyncio.Event()
    repeated_cancel_seen = asyncio.Event()
    release_body = threading.Event()
    actuals = []
    delivered = []
    workers = []
    captured = {}
    submitted = []
    cancellation_errors = []
    original_execute = port._execute

    def blocked_execute(current_request):
        actual = original_execute(current_request)
        actuals.append(actual)
        loop.call_soon_threadsafe(body_started.set)
        release_body.wait()
        return actual

    port._execute = blocked_execute
    if runner_failure:
        async def fail_runner_before_result(body):
            worker = asyncio.create_task(asyncio.to_thread(body))
            workers.append(worker)
            await body_started.wait()
            raise RuntimeError("PROBE scope-outside runner fault")

        monkeypatch.setattr(work_files.executor, "_runner", fail_runner_before_result)

    original_run = work_files.executor.run_file_task

    async def track_file_task(task, owner, **kwargs):
        submitted.append(task)
        return await original_run(task, owner, **kwargs)

    monkeypatch.setattr(work_files.executor, "run_file_task", track_file_task)
    original_device_result = cleanup_assembly_module._device_result

    def capture_device_result(current_request, result):
        actual = original_device_result(current_request, result)
        delivered.append((current_request, actual))
        return actual

    monkeypatch.setattr(
        cleanup_assembly_module, "_device_result", capture_device_result)
    original_collect = cleanup_assembly_module._collect_file_result
    collect_task = None

    def remember_cancellation(error):
        cancellation_errors.append(error)
        if len(cancellation_errors) == 1:
            captured["interruption"] = error
            if runner_failure:
                cause = RuntimeError("PROBE original cancellation cause")
                error.add_note("PROBE original cancellation note")
                error.__cause__ = cause
                error.__suppress_context__ = True
                captured["cancel_cause"] = cause
            cancel_seen.set()
        elif len(cancellation_errors) == 2:
            repeated_cancel_seen.set()

    async def capture_collect(executor, owner, task, interruption, runner_error):
        nonlocal collect_task
        collect_task = asyncio.current_task()
        captured["initial_interruption"] = interruption
        captured["runner_error"] = runner_error
        if interruption is not None:
            remember_cancellation(interruption)
        collect_started.set()
        result = await original_collect(
            executor, owner, task, interruption, runner_error)
        captured["interruption"] = result[1]
        captured["diagnostic"] = result[2]
        return result

    monkeypatch.setattr(
        cleanup_assembly_module, "_collect_file_result", capture_collect)
    original_shield = asyncio.shield

    def observe_collector_cancellation(awaitable):
        shielded = original_shield(awaitable)
        if asyncio.current_task() is not collect_task:
            return shielded

        async def wait_for_shielded():
            try:
                return await shielded
            except asyncio.CancelledError as error:
                remember_cancellation(error)
                raise

        return wait_for_shielded()

    monkeypatch.setattr(asyncio, "shield", observe_collector_cancellation)
    scopes = {}
    if scope_inside:
        @owned_tool_call
        async def invoke_port():
            scopes["current"] = current_call_scope()
            return await port.delete(request)

        call = asyncio.create_task(invoke_port())
    else:
        assert current_call_scope() is None
        call = asyncio.create_task(port.delete(request))
    outcome = None
    try:
        await asyncio.wait_for(body_started.wait(), timeout=5)
        if runner_failure:
            await asyncio.wait_for(collect_started.wait(), timeout=5)
        if cancel_waiter:
            call.cancel("ORIGINAL_SCOPE_OUTSIDE_CANCEL")
            await asyncio.wait_for(cancel_seen.wait(), timeout=5)
            call.cancel("REPEATED_SCOPE_OUTSIDE_CANCEL")
            if not scope_inside:
                await asyncio.wait_for(repeated_cancel_seen.wait(), timeout=5)
            else:
                await asyncio.sleep(0)
        release_body.set()
        try:
            outcome = await call
        except BaseException as error:
            outcome = error
    finally:
        release_body.set()
        if not call.done():
            await asyncio.gather(call, return_exceptions=True)
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    assert len(actuals) == len(delivered) == len(submitted) == 1
    assert delivered[0][0] is request
    assert delivered[0][1] is actuals[0]
    assert request.ticket is ticket
    assert submitted[0].task_id.value == (
        f"cleanup:{request.database_id}:delete:{ticket.run_id}:{ticket.attempt_id}")
    assert submitted[0].file_id == 900
    assert target.exists() is False

    diagnostic = captured.get("diagnostic")
    assert (diagnostic is not None) is runner_failure
    if diagnostic is not None:
        assert any(fact.source == "runner"
                   and fact.error_message == "PROBE scope-outside runner fault"
                   for fact in diagnostic.facts)

    carrier = None
    if cancel_waiter:
        interruption = captured["interruption"]
        assert isinstance(interruption, asyncio.CancelledError)
        if scope_inside:
            assert isinstance(outcome, asyncio.CancelledError)
            assert str(outcome) == "ORIGINAL_SCOPE_OUTSIDE_CANCEL"
            assert scopes["current"].interrupted is interruption
            if diagnostic is not None:
                carrier = outcome.__cause__
        else:
            assert isinstance(outcome, LocalArtifactTaskFailure)
            carrier = outcome
            assert str(interruption) == "ORIGINAL_SCOPE_OUTSIDE_CANCEL"
            assert carrier.__cause__ is interruption
            assert len(cancellation_errors) == 2
        assert cancellation_errors[0] is interruption
        if diagnostic is not None:
            assert interruption.__cause__ is captured["cancel_cause"]
            assert "PROBE original cancellation note" in interruption.__notes__
            assert any(fact.source == "wait_cancellation.cause"
                       and fact.error_message == "PROBE original cancellation cause"
                       for fact in diagnostic.facts)
            assert any(fact.source == "wait_cancellation"
                       and "PROBE original cancellation note" in fact.notes
                       for fact in diagnostic.facts)
    elif diagnostic is not None:
        assert isinstance(outcome, LocalArtifactTaskFailure)
        carrier = outcome
    else:
        assert outcome is actuals[0]

    if carrier is not None:
        assert isinstance(carrier, LocalArtifactTaskFailure)
        assert carrier.actual is actuals[0]
        assert carrier.supervision_diagnostic is diagnostic
        if cancel_waiter and not scope_inside:
            assert carrier.__cause__ is captured["interruption"]
        elif not cancel_waiter:
            assert isinstance(carrier.__cause__, RuntimeError)
    if scope_inside:
        assert scopes["current"] is not None
        assert scopes["current"].interrupted is (
            captured.get("interruption") if cancel_waiter else None)
    assert request.ticket is ticket
    assert work_files.executor.unfinished_files() == ()
    assert work_files.file_supervisor.outstanding() == ()


async def test_scope_outside_cancel_then_drain_failure_keeps_actual_and_control(
        host_environment, monkeypatch):
    """先取消、后共享 drain 故障时，仍交回原 actual 与同一取消。"""
    import camctl.bootstrap.cleanup_assembly as cleanup_assembly_module
    from camctl.operations.models import AttemptTicket
    from camctl.operations.owned_calls import current_call_scope
    from camctl.outputs.cleanup_flow import LocalArtifactRequest, LocalArtifactTaskFailure
    from camctl.host_files.models import FileRef

    cfg, _owned, _context, _driver, work_files = host_environment
    staging = Path(cfg.paths.staging)
    target = staging / "derived" / "900.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"cancel-before-drain-failure")
    ticket = AttemptTicket(1, "delete", "1", "delete/1", 1)
    request = LocalArtifactRequest(
        "scope-outside-drain-test", "delete", 1, ticket,
        FileRef(900, FilePurpose.REPAIR_OUTPUT, "derived/900.jpg", staging),
    )
    port = HostArtifacts(staging, work_files.executor)
    assert current_call_scope() is None

    loop = asyncio.get_running_loop()
    body_started = asyncio.Event()
    collect_started = asyncio.Event()
    drain_started = asyncio.Event()
    release_body = threading.Event()
    release_drain = asyncio.Event()
    actuals = []
    delivered = []
    captured = {}
    original_execute = port._execute

    def blocked_execute(current_request):
        actual = original_execute(current_request)
        actuals.append(actual)
        loop.call_soon_threadsafe(body_started.set)
        release_body.wait()
        return actual

    port._execute = blocked_execute
    original_drain = work_files.executor.drain_required

    async def fail_drain_after_cancellation():
        drain_started.set()
        await release_drain.wait()
        raise RuntimeError("PROBE shared drain failed after cancellation")

    monkeypatch.setattr(
        work_files.executor, "drain_required", fail_drain_after_cancellation)
    original_collect = cleanup_assembly_module._collect_file_result
    collect_task = None

    async def capture_collect(executor, owner, task, interruption, runner_error):
        nonlocal collect_task
        collect_task = asyncio.current_task()
        captured["interruption"] = interruption
        captured["runner_error"] = runner_error
        if interruption is not None:
            cause = RuntimeError("PROBE original cancellation cause")
            interruption.add_note("PROBE original cancellation note")
            interruption.__cause__ = cause
            interruption.__suppress_context__ = True
            captured["cancel_cause"] = cause
        collect_started.set()
        result = await original_collect(
            executor, owner, task, interruption, runner_error)
        captured["actual_interruption"] = result[1]
        captured["diagnostic"] = result[2]
        return result

    monkeypatch.setattr(
        cleanup_assembly_module, "_collect_file_result", capture_collect)
    original_shield = asyncio.shield
    repeated_cancel_seen = asyncio.Event()

    def observe_repeated_cancellation(awaitable):
        shielded = original_shield(awaitable)
        if asyncio.current_task() is not collect_task:
            return shielded

        async def wait_for_shielded():
            try:
                return await shielded
            except asyncio.CancelledError:
                repeated_cancel_seen.set()
                raise

        return wait_for_shielded()

    monkeypatch.setattr(asyncio, "shield", observe_repeated_cancellation)
    original_device_result = cleanup_assembly_module._device_result

    def capture_device_result(current_request, result):
        actual = original_device_result(current_request, result)
        delivered.append((current_request, actual))
        return actual

    monkeypatch.setattr(
        cleanup_assembly_module, "_device_result", capture_device_result)
    call = asyncio.create_task(port.delete(request))
    try:
        await asyncio.wait_for(body_started.wait(), timeout=5)
        call.cancel("ORIGINAL_CANCEL_BEFORE_DRAIN_FAILURE")
        await asyncio.wait_for(collect_started.wait(), timeout=5)
        await asyncio.wait_for(drain_started.wait(), timeout=5)
        call.cancel("REPEATED_CANCEL_BEFORE_DRAIN_FAILURE")
        await asyncio.wait_for(repeated_cancel_seen.wait(), timeout=5)
        release_drain.set()
        await asyncio.sleep(0)
        release_body.set()
        with pytest.raises(LocalArtifactTaskFailure) as caught:
            await call
    finally:
        release_drain.set()
        release_body.set()
        if not call.done():
            await asyncio.gather(call, return_exceptions=True)
        await original_drain()

    carrier = caught.value
    interruption = captured["interruption"]
    diagnostic = captured["diagnostic"]
    assert captured["runner_error"] is None
    assert carrier.__cause__ is interruption
    assert str(interruption) == "ORIGINAL_CANCEL_BEFORE_DRAIN_FAILURE"
    assert interruption.__cause__ is captured["cancel_cause"]
    assert "PROBE original cancellation note" in interruption.__notes__
    assert carrier.actual is actuals[0]
    assert delivered == [(request, actuals[0])]
    assert carrier.supervision_diagnostic is diagnostic
    assert any(fact.source == "shared_drain"
               and fact.error_message == "PROBE shared drain failed after cancellation"
               for fact in diagnostic.facts)
    assert any(fact.source == "wait_cancellation.cause"
               and fact.error_message == "PROBE original cancellation cause"
               for fact in diagnostic.facts)
    assert any(fact.source == "wait_cancellation"
               and "PROBE original cancellation note" in fact.notes
               for fact in diagnostic.facts)
    assert len(actuals) == 1
    assert request.ticket is ticket
    assert target.exists() is False
    assert work_files.executor.unfinished_files() == ()
    assert work_files.file_supervisor.outstanding() == ()


async def test_scope_outside_unavailable_actual_keeps_cancel_and_supervision_failstop(
        host_environment, monkeypatch):
    """无法等待原 handle 时保留取消与监督失败，并且不合成可保存 actual。"""
    import camctl.bootstrap.cleanup_assembly as cleanup_assembly_module
    from camctl.host_files.tasks import FileTaskHandle
    from camctl.operations.models import AttemptTicket
    from camctl.operations.owned_calls import current_call_scope
    from camctl.outputs.cleanup_flow import LocalArtifactRequest
    from camctl.host_files.models import FileRef

    cfg, _owned, _context, _driver, work_files = host_environment
    staging = Path(cfg.paths.staging)
    target = staging / "derived" / "900.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"result-cannot-be-delivered")
    ticket = AttemptTicket(1, "delete", "1", "delete/1", 1)
    request = LocalArtifactRequest(
        "scope-outside-no-actual-test", "delete", 1, ticket,
        FileRef(900, FilePurpose.REPAIR_OUTPUT, "derived/900.jpg", staging),
    )
    port = HostArtifacts(staging, work_files.executor)
    assert current_call_scope() is None

    loop = asyncio.get_running_loop()
    body_started = asyncio.Event()
    collect_started = asyncio.Event()
    owner_wait_failed = asyncio.Event()
    drain_started = asyncio.Event()
    cancel_seen = asyncio.Event()
    repeated_cancel_seen = asyncio.Event()
    release_body = threading.Event()
    release_drain = asyncio.Event()
    actuals = []
    workers = []
    handles = []
    delivered = []
    captured = {}
    task_identity = (
        f"cleanup:{request.database_id}:delete:{ticket.run_id}:{ticket.attempt_id}")
    original_execute = port._execute

    def blocked_execute(current_request):
        actual = original_execute(current_request)
        actuals.append(actual)
        loop.call_soon_threadsafe(body_started.set)
        release_body.wait()
        return actual

    port._execute = blocked_execute

    async def fail_runner_before_completion(body):
        worker = asyncio.create_task(asyncio.to_thread(body))
        workers.append(worker)
        await body_started.wait()
        raise RuntimeError("PROBE runner failure before result delivery")

    monkeypatch.setattr(work_files.executor, "_runner", fail_runner_before_completion)
    original_handle_wait = FileTaskHandle.wait

    async def fail_handle_wait(handle):
        if handle.task_id.value == task_identity:
            handles.append(handle)
            owner_wait_failed.set()
            raise RuntimeError("PROBE original handle cannot deliver result")
        return await original_handle_wait(handle)

    monkeypatch.setattr(FileTaskHandle, "wait", fail_handle_wait)
    original_drain = work_files.executor.drain_required

    async def controlled_drain():
        drain_started.set()
        await release_drain.wait()
        return await original_drain()

    monkeypatch.setattr(work_files.executor, "drain_required", controlled_drain)
    original_collect = cleanup_assembly_module._collect_file_result
    collect_task = None

    async def capture_collect(executor, owner, task, interruption, runner_error):
        nonlocal collect_task
        collect_task = asyncio.current_task()
        captured["initial_interruption"] = interruption
        captured["runner_error"] = runner_error
        collect_started.set()
        result = await original_collect(
            executor, owner, task, interruption, runner_error)
        captured["interruption"] = result[1]
        captured["diagnostic"] = result[2]
        return result

    monkeypatch.setattr(
        cleanup_assembly_module, "_collect_file_result", capture_collect)
    original_shield = asyncio.shield

    def observe_collector_cancellation(awaitable):
        shielded = original_shield(awaitable)
        if asyncio.current_task() is not collect_task:
            return shielded

        async def wait_for_shielded():
            try:
                return await shielded
            except asyncio.CancelledError as error:
                if "interruption" not in captured:
                    captured["interruption"] = error
                    cause = RuntimeError("PROBE original cancellation cause")
                    error.add_note("PROBE original cancellation note")
                    error.__cause__ = cause
                    error.__suppress_context__ = True
                    captured["cancel_cause"] = cause
                    cancel_seen.set()
                else:
                    repeated_cancel_seen.set()
                raise

        return wait_for_shielded()

    monkeypatch.setattr(asyncio, "shield", observe_collector_cancellation)
    original_device_result = cleanup_assembly_module._device_result

    def capture_device_result(current_request, result):
        actual = original_device_result(current_request, result)
        delivered.append((current_request, actual))
        return actual

    monkeypatch.setattr(
        cleanup_assembly_module, "_device_result", capture_device_result)
    call = asyncio.create_task(port.delete(request))
    try:
        await asyncio.wait_for(body_started.wait(), timeout=5)
        await asyncio.wait_for(collect_started.wait(), timeout=5)
        await asyncio.wait_for(drain_started.wait(), timeout=5)
        call.cancel("ORIGINAL_CANCEL_WITHOUT_RESULT")
        await asyncio.wait_for(cancel_seen.wait(), timeout=5)
        call.cancel("REPEATED_CANCEL_WITHOUT_RESULT")
        await asyncio.wait_for(repeated_cancel_seen.wait(), timeout=5)
        release_drain.set()
        await asyncio.wait_for(owner_wait_failed.wait(), timeout=5)
        with pytest.raises(ConsistencyError) as caught:
            await call
    finally:
        release_drain.set()
        release_body.set()
        if not call.done():
            await asyncio.gather(call, return_exceptions=True)
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        if handles:
            await original_handle_wait(handles[0])
        await original_drain()

    interruption = captured["interruption"]
    assert isinstance(interruption, asyncio.CancelledError)
    assert str(interruption) == "ORIGINAL_CANCEL_WITHOUT_RESULT"
    assert interruption.__cause__ is captured["cancel_cause"]
    assert "PROBE original cancellation note" in interruption.__notes__
    assert isinstance(caught.value.__cause__, BaseExceptionGroup)
    failures = caught.value.__cause__.exceptions
    assert len(failures) == 2
    assert failures[0] is interruption
    assert isinstance(failures[1], RuntimeError)
    assert "PROBE runner failure before result delivery" in str(failures[1])
    assert "PROBE original handle cannot deliver result" in str(failures[1])
    assert "PROBE original cancellation cause" in str(failures[1])
    assert getattr(caught.value, "actual", None) is None
    diagnostic = captured["diagnostic"]
    assert diagnostic is not None
    assert str(failures[1]) == diagnostic.render()
    assert any(fact.source == "runner"
               and fact.error_message == "PROBE runner failure before result delivery"
               for fact in diagnostic.facts)
    assert any(fact.source == "supervisor_owner"
               and fact.error_message == "PROBE original handle cannot deliver result"
               for fact in diagnostic.facts)
    assert delivered == []
    assert len(actuals) == 1
    assert request.ticket is ticket
    assert target.exists() is False
    assert work_files.executor.unfinished_files() == ()
    assert work_files.file_supervisor.outstanding() == ()

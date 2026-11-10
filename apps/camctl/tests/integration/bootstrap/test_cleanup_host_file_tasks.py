"""主机正式产物清理复用会话共享文件执行器。"""

import asyncio
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from .cleanup_helpers import finish_cleanup_round, cleanup_failure

from ..support.bootstrap_runtime_cases import (
    HostFileWorld, ThreadRunnerInput, OwnerHandoffProbe, check_unknown_cleanup_supervision, check_host_port_control, original_host_calls, observe_host_calls, observe_host_failures,
    advance_cleanup_once, apply_host_cleanup_cancel,
)
from ..support.capture import capture_database

from camctl.bootstrap.cleanup_assembly import session_cleanup_assembly
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.cleanup_assembly import cleanup_flow
from camctl.contracts.values import ConsistencyError
from camctl.devices.ports import DeviceCallResult
from camctl.host_files import paths as host_paths
from camctl.host_files.models import FilePurpose
from camctl.operations.models import SettlementBasis
from camctl.logging_runtime.diagnostics import error_diagnostics_context, flush_diagnostics
from camctl.outputs.cleanup_results import saved_cleanup_delete, saved_cleanup_query
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.host_files.tasks import FileTask, FileTaskId, FileTaskExecutor
from camctl.session.supervision import Supervisor

from .test_output_binding_changes import (
    Driver, _NOW, _accept, _registry, _save_photos,
)


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def host_environment(tmp_path, monkeypatch):
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
    owned = open_existing(Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
    context = SimpleNamespace(
        open_connection=lambda: open_existing(
            Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig()),
        clock=SimpleNamespace(utc_micros=lambda: _NOW),
    )
    driver = Driver(owned)
    runner = ThreadRunnerInput()
    supervisor = Supervisor()
    executor = FileTaskExecutor(supervisor, thread_runner=runner)
    context.database = await capture_database(owned)
    world = HostFileWorld(cfg, owned, context, driver, executor, supervisor, runner)
    observe_host_failures(monkeypatch, world)
    with error_diagnostics_context(None, world.diagnostics.append):
        try:
            yield world
        finally:
            try:
                result = await supervisor.drain_required()
                assert not result.failed, result.failed
                assert executor.unfinished_files() == ()
                assert supervisor.outstanding() == ()
                await flush_diagnostics()
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


async def _accept_cleanup(owned, output_id):
    await _accept(owned, "2", [{
        "name": "清理主机成品", "type": "delete_action_outputs",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"output_ids": [str(output_id)]},
    }])


def _cleanup_factory(cfg, driver, executor, **kwargs):
    return session_cleanup_assembly(
        devices=cfg.devices, drivers=_registry(driver),
        max_delete_attempts=cfg.cleanup.max_delete_attempts,
        max_query_attempts=cfg.cleanup.max_query_attempts,
        staging=Path(cfg.paths.staging), file_executor=executor,
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
@pytest.mark.asyncio(loop_scope="function")
async def test_registered_host_delete_wait_cancellation_saves_not_dispatched_once(
        host_environment, monkeypatch, business_cancel):
    """已登记且未开始的任务撤回保存真实 ran=False，不重放原票据。"""
    cfg, owned, context, driver, executor, supervisor = host_environment
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    await _accept_cleanup(owned, output_id)

    gate = threading.Event()
    started = asyncio.Event()
    owner = _Owner()
    submitted = []
    run_file_task = executor.run_file_task
    requests = []
    host_run = original_host_calls()

    async def capture_request(self, request):
        requests.append(request)
        return await host_run(self, request)

    observe_host_calls(monkeypatch, capture_request)

    async def track_host_task(task, task_owner, **kwargs):
        if task.business == "host_artifact_cleanup":
            submitted.append((task, kwargs))
        return await run_file_task(task, task_owner, **kwargs)

    monkeypatch.setattr(executor, "run_file_task", track_host_task)

    def occupy_pool(_stop_event):
        loop.call_soon_threadsafe(started.set)
        gate.wait()
        return "released"

    blocker = FileTask(
        FileTaskId("r4-test-pool-blocker"), 999_001, "test", "pool_blocker", occupy_pool)
    blocker_task = asyncio.create_task(
        executor.run_file_task(blocker, owner))
    cleanup = None
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        factory = session_cleanup_assembly(
            devices=cfg.devices, drivers=_registry(driver),
            max_delete_attempts=cfg.cleanup.max_delete_attempts,
            max_query_attempts=cfg.cleanup.max_query_attempts,
            staging=Path(cfg.paths.staging), file_executor=executor,
        )
        cleanup = asyncio.create_task(advance_cleanup_once(factory, context))
        await _wait_until(lambda: 900 in executor.unfinished_files())
        item_id = owned.connection.execute("SELECT id FROM cleanup_items").fetchone()[0]
        if business_cancel:
            await apply_host_cleanup_cancel(cfg, owned, context)
        cleanup.cancel()
        # 取消先撤回已登记且未开始的任务；blocker 放行后才会完成共享 drain。
        await _wait_until(lambda: 900 not in executor.unfinished_files())
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
            staging=Path(cfg.paths.staging), file_executor=executor,
        )
        await finish_cleanup_round(cleanup_flow(retry_factory), context)
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
        if cleanup is not None and not cleanup.done():
            cleanup.cancel()
        gate.set()
        await asyncio.gather(
            *(task for task in (cleanup, blocker_task) if task is not None),
            return_exceptions=True)


@pytest.mark.asyncio(loop_scope="function")
async def test_registered_host_query_wait_cancellation_keeps_original_consumer(
        host_environment, monkeypatch):
    """已登记且未开始的 QUERY 撤回沿原 consumer 保存 NOT_DISPATCHED。"""
    cfg, owned, context, driver, executor, supervisor = host_environment
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    await _accept_cleanup(owned, output_id)

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
        executor.run_file_task(blocker, owner))
    cleanup = None
    try:
        original_run = original_host_calls()
        operations = []
        submitted = []
        run_file_task = executor.run_file_task

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

        observe_host_calls(monkeypatch, fail_delete_capture_query)
        monkeypatch.setattr(executor, "run_file_task", track_task)
        active_calls = set()
        factory = _cleanup_factory(
            cfg, driver, executor, active_calls=active_calls)
        cleanup = asyncio.create_task(advance_cleanup_once(factory, context))
        await asyncio.wait_for(blocker_started.wait(), timeout=5)
        await _wait_until(lambda: 900 in executor.unfinished_files())
        item_id = owned.connection.execute("SELECT id FROM cleanup_items").fetchone()[0]
        assert owned.connection.execute(
            "SELECT r.kind,a.status FROM operation_attempts a JOIN operation_runs r"
            " ON r.id=a.run_id WHERE r.cleanup_item_id=? ORDER BY a.id",
            (item_id,)).fetchall()[-1] == (5, 1)
        cleanup.cancel()
        await _wait_until(lambda: 900 not in executor.unfinished_files())
        with pytest.raises(asyncio.CancelledError):
            await cleanup
    finally:
        if cleanup is not None and not cleanup.done():
            cleanup.cancel()
        gate.set()
        await asyncio.gather(
            *(task for task in (cleanup, blocker_task) if task is not None),
            return_exceptions=True)

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
    next_owner = cleanup_flow(factory)
    assert next_owner.required_settlements() == 1
    original_query = saved_cleanup_query(owned.connection, item_id)
    assert original_query is not None and original_query.after_delete
    assert active_calls == set()
    assert target.is_file()
    assert operations == ["delete", "query"]
    assert len(submitted) == 1
    assert submitted[0].stage == "cleanup_query"
    assert submitted[0].file_id == 900
    assert executor.unfinished_files() == ()

    operations_before_resume = tuple(operations)
    await finish_cleanup_round(next_owner, context)
    assert tuple(operations) == operations_before_resume
    assert next_owner.required_settlements() == 0
    assert saved_cleanup_query(owned.connection, item_id) == original_query
    assert target.is_file()


async def test_started_host_delete_cancellation_saves_observation_and_error(
        host_environment, monkeypatch):
    """已开始 unlink 的取消等待线程与原 ticket 收场；观察和错误一并保存。"""
    cfg, owned, context, driver, executor, supervisor = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    await _accept_cleanup(owned, output_id)

    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    gate = threading.Event()
    active_calls = set()
    factory = _cleanup_factory(cfg, driver, executor, active_calls=active_calls)
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
    drain_required = executor.drain_required

    async def tracked_drain():
        drains.append("started")
        return await drain_required()

    monkeypatch.setattr(executor, "drain_required", tracked_drain)
    cleanup = asyncio.create_task(advance_cleanup_once(factory, context))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        item_id = owned.connection.execute("SELECT id FROM cleanup_items").fetchone()[0]
        running = owned.connection.execute(
            "SELECT a.status FROM operation_attempts a JOIN operation_runs r"
            " ON r.id=a.run_id WHERE r.cleanup_item_id=? AND r.kind=4", (item_id,)).fetchone()
        assert running == (1,)
        assert active_calls
        assert 900 in executor.unfinished_files()
        cleanup.cancel()
        await asyncio.wait_for(_wait_until(lambda: bool(drains)), timeout=5)
        assert active_calls
        assert 900 in executor.unfinished_files()
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
    assert 900 not in executor.unfinished_files()
    assert supervisor.outstanding() == ()


@pytest.mark.parametrize("failure_stage", ["register", "handoff"])
async def test_supervisor_handoff_failure_keeps_original_handle_until_saved(
        host_environment, monkeypatch, failure_stage):
    """监督注册/登记故障不能丢掉已开始任务，也不能长期保留其 handle。"""
    cfg, owned, context, driver, executor, supervisor = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    await _accept_cleanup(owned, output_id)

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
    supervisor = supervisor
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
    drain = executor.drain_required

    async def tracked_drain():
        drain_started.set()
        return await drain()

    monkeypatch.setattr(executor, "drain_required", tracked_drain)
    cleanup = asyncio.create_task(advance_cleanup_once(_cleanup_factory(
        cfg, driver, executor, active_calls=active_calls), context))
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
    assert executor.unfinished_files() == ()
    assert supervisor.outstanding() == ()
    assert canceled.value.__cause__ is not None
    assert failure_stage in str(canceled.value.__cause__)
    identity = FileTaskId(f"cleanup:{owned.metadata.instance_id}:delete:{saved.ticket.run_id}:{saved.ticket.attempt_id}")
    assert executor.lease_of(identity) is None


async def test_finished_host_delete_cancellation_before_runner_delivery_keeps_actual(
        host_environment, monkeypatch):
    """FileTask completion 已形成但 runner 尚未交付时，撤回不能改写实际结果。"""
    host_runner = host_environment.thread_runner
    cfg, owned, context, driver, executor, supervisor = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    await _accept_cleanup(owned, output_id)

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
    runner = host_runner.call

    async def hold_delivery(body):
        result = await runner(body)
        loop.call_soon_threadsafe(runner_returned.set)
        await release_runner.wait()
        return result

    monkeypatch.setattr(host_runner, "call", hold_delivery)
    drain = executor.drain_required

    async def track_drain():
        drain_started.set()
        return await drain()

    monkeypatch.setattr(executor, "drain_required", track_drain)
    active_calls = set()
    cleanup = asyncio.create_task(advance_cleanup_once(_cleanup_factory(
        cfg, driver, executor, active_calls=active_calls), context))
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
    assert supervisor.outstanding() == ()


async def test_owner_failure_recovers_same_handle_and_saves_actual_before_error(
        host_environment, monkeypatch):
    """supervisor owner 失败不重投，collector 用原 handle 保留 actual 后报告错误。"""
    host_runner = host_environment.thread_runner
    cfg, owned, context, driver, executor, supervisor = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    await _accept_cleanup(owned, output_id)

    unlink_calls = []
    original_unlink = Path.unlink

    def count_unlink(path, *args, **kwargs):
        if path == target:
            unlink_calls.append(path)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", count_unlink)
    runner = host_runner.call

    async def fail_after_completion(body):
        await runner(body)
        raise RuntimeError("runner failed after the shared completion formed")

    async def fail_owner(_self, _task):
        raise RuntimeError("supervisor owner failed before reading its handle")

    owner_probe = OwnerHandoffProbe(monkeypatch, supervisor)
    monkeypatch.setattr(host_runner, "call", fail_after_completion)
    owner_probe.callback = fail_owner
    factory = _cleanup_factory(
        cfg, driver, executor)
    with cleanup_failure(ConsistencyError, match="监督交接失败"):
        await finish_cleanup_round(cleanup_flow(factory), context)

    item_id = owned.connection.execute("SELECT id FROM cleanup_items").fetchone()[0]
    saved = saved_cleanup_delete(owned.connection, item_id)
    assert saved is not None
    assert saved.actual.observations[0].type == "file_absent"
    request, failure = host_environment.port_failures[-1]
    assert request.ticket == saved.ticket
    assert failure.actual.observations == saved.actual.observations
    assert owned.connection.execute(
        "SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone() == (3,)
    assert unlink_calls == [target]
    assert not target.exists()
    assert executor.unfinished_files() == ()
    assert supervisor.outstanding() == ()

    # 同一 pending 在新 factory 中完成成员交接，不重新提交文件任务。
    monkeypatch.setattr(host_runner, "call", runner)
    owner_probe.callback = None
    await finish_cleanup_round(cleanup_flow(factory), context)
    assert owned.connection.execute(
        "SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone() == (4,)
    assert unlink_calls == [target]

    assert saved_cleanup_delete(owned.connection, item_id) == saved


async def test_canceled_owner_failure_saves_actual_and_chains_drain_error(
        host_environment, monkeypatch):
    """取消时 owner drain 失败仍从原 handle 保存缺席，并把故障挂到取消原因。"""
    host_runner = host_environment.thread_runner
    cfg, owned, context, driver, executor, supervisor = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    await _accept_cleanup(owned, output_id)

    loop = asyncio.get_running_loop()
    runner_returned = asyncio.Event()
    release_runner = asyncio.Event()
    owner_failed = asyncio.Event()
    unlink_calls = []
    original_unlink = Path.unlink
    runner = host_runner.call
    owner_probe = OwnerHandoffProbe(monkeypatch, supervisor)
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
    monkeypatch.setattr(host_runner, "call", hold_delivery)
    owner_probe.callback = fail_owner
    factory = _cleanup_factory(cfg, driver, executor, active_calls=active_calls)
    cleanup = asyncio.create_task(advance_cleanup_once(factory, context))
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
    assert supervisor.outstanding() == ()
    owner_probe.callback = None
    request, failure = host_environment.port_failures[-1]
    assert request.ticket == saved.ticket
    assert failure.supervision_diagnostic is not None
    await flush_diagnostics()
    assert len([line for line in host_environment.diagnostics
        if 'supervisor owner failed during cancellation drain' in line]) == 1
    resumed = cleanup_flow(factory)
    await finish_cleanup_round(resumed, context)
    assert owned.connection.execute(
        "SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone() == (4,)
    assert resumed.required_settlements() == 0
    assert saved_cleanup_delete(owned.connection, item_id) == saved
    assert unlink_calls == [target]


async def test_host_delete_commit_unknown_recovers_same_save_without_task_replay(
        host_environment, monkeypatch):
    """主机 DELETE 提交后回执 UNKNOWN 时以原保存身份恢复，不重做 unlink。"""
    from camctl.outputs.cleanup_flow import delete_source_file
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.persistence.repositories.outputs import OutputsRepository

    cfg, owned, context, driver, executor, supervisor = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    await _accept_cleanup(owned, output_id)
    active_calls = set()
    factory = _cleanup_factory(
        cfg, driver, executor, active_calls=active_calls)

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
    run_file_task = executor.run_file_task

    async def track_task(task, owner, **kwargs):
        if task.business == "host_artifact_cleanup":
            submitted.append(task)
        return await run_file_task(task, owner, **kwargs)

    monkeypatch.setattr(executor, "run_file_task", track_task)
    cleanup = cleanup_flow(factory)
    with cleanup_failure(ConsistencyError):
        await finish_cleanup_round(cleanup, context)
    item_id = owned.connection.execute(
        "SELECT id FROM cleanup_items WHERE output_id=?", (output_id,)).fetchone()[0]
    original_finish_request, original_key, _ = finish_calls[0]
    original_actual = original_finish_request.outcome.outcome
    original_ticket = original_finish_request.ticket
    original_occurred_at = original_finish_request.occurred_at
    assert original_actual.observations[0].type == "file_absent"
    assert not target.exists()
    assert len(submitted) == 1
    assert active_calls == set()

    owned.connection.close()
    await finish_cleanup_round(cleanup, context)
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
        assert finish_calls[1][0].outcome.outcome is original_actual
        assert finish_calls[1][0].occurred_at == original_occurred_at
        assert cleanup.required_settlements() == 0
        assert len(submitted) == 1
        assert not target.exists()
        assert active_calls == set()
        assert executor.unfinished_files() == ()
    finally:
        fresh.connection.close()


@pytest.mark.parametrize("query_error", [False, True])
async def test_started_host_query_cancellation_keeps_query_consumer_for_reentry(
        host_environment, monkeypatch, query_error):
    """取消期间 QUERY 仍由原 AFTER_DELETE ticket 保存，再入场时不重查文件。"""
    cfg, owned, context, driver, executor, supervisor = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    await _accept_cleanup(owned, output_id)

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
    original_run = original_host_calls()
    host_operations = []

    async def enable_query_gate(self, request):
        host_operations.append(request.operation)
        if request.operation == "query":
            query_enabled.set()
        return await original_run(self, request)

    observe_host_calls(monkeypatch, enable_query_gate)

    active_calls = set()
    factory = _cleanup_factory(
        cfg, driver, executor, active_calls=active_calls)
    cleanup = asyncio.create_task(advance_cleanup_once(factory, context))
    drain_started = asyncio.Event()
    drain_required = executor.drain_required

    async def tracked_drain():
        drain_started.set()
        return await drain_required()

    monkeypatch.setattr(executor, "drain_required", tracked_drain)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        item_id = owned.connection.execute("SELECT id FROM cleanup_items").fetchone()[0]
        running = owned.connection.execute(
            "SELECT r.kind,a.status FROM operation_attempts a JOIN operation_runs r"
            " ON r.id=a.run_id WHERE r.cleanup_item_id=? ORDER BY a.id", (item_id,)).fetchall()
        assert running[-1] == (5, 1)
        assert active_calls
        assert 900 in executor.unfinished_files()
        stats_when_query_paused = len(stat_calls)
        cleanup.cancel()
        await asyncio.wait_for(drain_started.wait(), timeout=5)
        assert active_calls
        assert 900 in executor.unfinished_files()
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
    next_owner = cleanup_flow(factory)
    assert next_owner.required_settlements() == 1
    original_query = saved_cleanup_query(owned.connection, item_id)
    assert original_query is not None and original_query.after_delete
    assert active_calls == set()
    assert worker_threads and all(thread != loop_thread for thread in worker_threads)
    assert original_stat(target).st_size == len(b"repaired-artifact")
    assert 900 not in executor.unfinished_files()
    # 取消前已经开始的 QUERY 在线程里只观察一次；这次结果形成后，
    # 下方新工厂复入不应再提交 QUERY，也不应再次观察目标。
    assert len(stat_calls) == stats_when_query_paused

    # 新工厂先接回已保存 consumer；这一步不得重新 stat 目标文件。
    before_resume_stats = len(stat_calls)
    operations_before_resume = tuple(host_operations)
    await finish_cleanup_round(next_owner, context)
    assert len(stat_calls) == before_resume_stats
    assert tuple(host_operations) == operations_before_resume
    assert next_owner.required_settlements() == 0
    assert saved_cleanup_query(owned.connection, item_id) == original_query


async def test_fresh_host_target_path_error_stops_before_attempt_ticket(
        host_environment, monkeypatch):
    """fresh FileRef 纯路径错误在授票前失败，不生成 attempt 或 worker。"""
    cfg, owned, context, driver, executor, supervisor = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    await _accept_cleanup(owned, output_id)
    calls = []

    def invalid_target(file_ref, directories):
        calls.append(file_ref)
        raise ValueError("controlled unsafe path")

    monkeypatch.setattr(host_paths, "resolve_file", invalid_target)
    factory = _cleanup_factory(cfg, driver, executor)
    with cleanup_failure(ConsistencyError, match="FileRef 身份或路径不合法"):
        await finish_cleanup_round(cleanup_flow(factory), context)

    assert len(calls) == 1
    assert calls[0].file_id == 900
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM operation_attempts WHERE run_id IN"
        " (SELECT id FROM operation_runs WHERE cleanup_item_id IS NOT NULL)").fetchone() == (0,)
    assert target.is_file()
    assert executor.unfinished_files() == ()
    assert supervisor.outstanding() == ()


async def test_legal_ticket_target_recheck_records_real_not_dispatched(
        host_environment, monkeypatch):
    """合法原票据获准后目标纯校验失败，沿该票据保存未派发结果。"""
    cfg, owned, context, driver, executor, supervisor = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    await _accept_cleanup(owned, output_id)
    resolve_file = host_paths.resolve_file
    calls = []

    def invalid_second_check(file_ref, directories):
        calls.append(file_ref)
        if len(calls) == 2:
            raise ValueError("path changed after ticket grant")
        return resolve_file(file_ref, directories)

    monkeypatch.setattr(host_paths, "resolve_file", invalid_second_check)
    submitted = []
    run_file_task = executor.run_file_task

    async def track_task(task, owner, **kwargs):
        if task.business == "host_artifact_cleanup":
            submitted.append(task)
        return await run_file_task(task, owner, **kwargs)

    monkeypatch.setattr(executor, "run_file_task", track_task)
    await finish_cleanup_round(cleanup_flow(_cleanup_factory(cfg, driver, executor)), context)

    item_id = owned.connection.execute("SELECT id FROM cleanup_items").fetchone()[0]
    saved = saved_cleanup_delete(owned.connection, item_id)
    assert len(calls) == 2
    assert saved is not None
    assert saved.actual.outcome.settlement.basis is SettlementBasis.NOT_DISPATCHED
    assert saved.actual.observations == ()
    assert saved.actual.error is not None
    assert target.is_file()
    assert submitted == []
    assert executor.unfinished_files() == ()


async def test_ticket_identity_error_is_not_rewritten_as_not_dispatched(
        host_environment, monkeypatch):
    """公开主机端口拒绝与原成员不符的票据，不伪造未派发结果。"""
    from dataclasses import replace
    from camctl.host_files.models import FileRef
    from camctl.outputs.cleanup_flow import LocalArtifactRequest
    from camctl.persistence.repositories.outputs import OutputsRepository

    cfg, owned, context, driver, executor, supervisor = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    await _accept_cleanup(owned, output_id)
    factory = _cleanup_factory(cfg, driver, executor)
    begin = OutputsRepository.begin_cleanup_attempt
    tickets = []
    class Granted(Exception):
        pass

    def granted(repository, request, key, current):
        receipt = begin(repository, request, key, current)
        ticket = getattr(receipt.value, 'ticket', None)
        if ticket is not None and ticket.operation == 'delete':
            tickets.append(ticket)
            return replace(receipt, handoff_error=Granted())
        return receipt

    with monkeypatch.context() as boundary:
        boundary.setattr(OutputsRepository, 'begin_cleanup_attempt', granted)
        with pytest.raises(Granted):
            await advance_cleanup_once(factory, context)
    ticket, = tickets
    item_id = int(ticket.target_id)
    port = (await factory(context.database)).local_files
    bad_request = LocalArtifactRequest(owned.metadata.instance_id, 'delete', item_id,
        replace(ticket, target_id=str(item_id + 1)),
        FileRef(900, FilePurpose.REPAIR_OUTPUT, 'derived/900.jpg', Path(cfg.paths.staging)))
    with pytest.raises(ConsistencyError, match='原票据'):
        await port.delete(bad_request)
    assert owned.connection.execute(
        'SELECT a.status,a.result_event_id FROM operation_attempts a JOIN operation_runs r '
        'ON r.id=a.run_id WHERE r.cleanup_item_id=? AND r.kind=4',
        (item_id,)).fetchone() == (1, None)
    assert saved_cleanup_delete(owned.connection, item_id) is None
    assert target.is_file()
    assert executor.unfinished_files() == ()


@pytest.mark.parametrize("conflict", ["duplicate_task_id", "same_file_id"])
async def test_host_executor_admission_conflicts_preserve_original_owner(
        host_environment, monkeypatch, conflict):
    """两种 admission 冲突都拒绝第二 owner，原任务继续到实际交付。"""
    cfg, owned, context, driver, executor, supervisor = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    await _accept_cleanup(owned, output_id)

    loop = asyncio.get_running_loop()
    original_started = asyncio.Event()
    release_original = threading.Event()
    release_adapter = asyncio.Event()
    conflict_seen = asyncio.Event()
    original_calls = []
    original_result = {}
    original_owner = _Owner()
    original_run = original_host_calls()

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
            executor.run_file_task(task, original_owner))
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

    observe_host_calls(monkeypatch, conflict_adapter)
    cleanup = asyncio.create_task(advance_cleanup_once(_cleanup_factory(
        cfg, driver, executor), context))
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
            executor.unfinished_files())
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
    assert executor.unfinished_files() == ()
    assert supervisor.outstanding() == ()


async def test_cleanup_drain_preserves_failure_from_another_file_owner(
        host_environment, monkeypatch):
    """本项收场消费共享 drain 时，不能丢弃另一文件责任的失败。"""
    host_runner = host_environment.thread_runner
    cfg, owned, context, driver, executor, supervisor = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    await _accept_cleanup(owned, output_id)

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
    other_waiter = asyncio.create_task(executor.run_file_task(
        other_task, _FailingOwner("PROBE unrelated required file owner failure")))
    try:
        await asyncio.wait_for(other_started.wait(), timeout=5)
        other_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await other_waiter
    finally:
        release_other.set()
    await _wait_until(lambda: other_file_id not in executor.unfinished_files())

    original_runner = host_runner.call

    async def fail_cleanup_runner_after_completion(body):
        await original_runner(body)
        raise RuntimeError("PROBE current cleanup runner fault")

    monkeypatch.setattr(host_runner, "call", fail_cleanup_runner_after_completion)
    unlink_calls = []
    original_unlink = Path.unlink

    def count_unlink(path, *args, **kwargs):
        if path == target:
            unlink_calls.append(path)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", count_unlink)
    with cleanup_failure(ConsistencyError, match="监督交接失败") as caught:
        await finish_cleanup_round(cleanup_flow(_cleanup_factory(
            cfg, driver, executor)), context)

    item_id = owned.connection.execute(
        "SELECT id FROM cleanup_items WHERE output_id=?", (output_id,)).fetchone()[0]
    request, failure = host_environment.port_failures[-1]
    assert request.cleanup_item_id == item_id
    diagnostic = failure.supervision_diagnostic
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
    assert executor.unfinished_files() == ()
    assert supervisor.outstanding() == ()


async def test_runner_failure_survives_cancellation_during_shared_drain(
        host_environment, monkeypatch):
    """runner 先失败、collector drain 后取消时，取消仍保留原 runner 诊断。"""
    host_runner = host_environment.thread_runner
    import camctl.bootstrap.cleanup_assembly as cleanup_assembly_module

    cfg, owned, context, driver, executor, supervisor = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    await _accept_cleanup(owned, output_id)

    loop = asyncio.get_running_loop()
    owner_waiting = asyncio.Event()
    release_owner = asyncio.Event()
    collector_cancel_seen = asyncio.Event()
    active_calls = set()
    original_runner = host_runner.call
    owner_probe = OwnerHandoffProbe(monkeypatch, supervisor)
    collect_task = None

    async def fail_after_completion(body):
        await original_runner(body)
        raise RuntimeError("PROBE runner failure before later waiter cancellation")

    async def delayed_take_over(self, task):
        owner_waiting.set()
        await release_owner.wait()
        await self.take_over(task)

    run_file_task = executor.run_file_task
    async def observe_file_call(task, owner, **options):
        nonlocal collect_task
        try:
            return await run_file_task(task, owner, **options)
        except RuntimeError:
            collect_task = asyncio.current_task()
            raise

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

    monkeypatch.setattr(host_runner, "call", fail_after_completion)
    owner_probe.callback = delayed_take_over
    monkeypatch.setattr(executor, "run_file_task", observe_file_call)
    monkeypatch.setattr(asyncio, "shield", observed_shield)
    cleanup = asyncio.create_task(advance_cleanup_once(_cleanup_factory(
        cfg, driver, executor, active_calls=active_calls), context))
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
    request, failure = host_environment.port_failures[-1]
    assert request.cleanup_item_id == item_id
    diagnostic = failure.supervision_diagnostic
    assert diagnostic is not None
    assert any(fact.error_message ==
               "PROBE runner failure before later waiter cancellation"
               for fact in diagnostic.facts)
    await flush_diagnostics()
    assert len([line for line in host_environment.diagnostics
        if 'PROBE runner failure before later waiter cancellation' in line]) == 1
    saved = saved_cleanup_delete(owned.connection, item_id)
    assert saved is not None
    assert saved.actual.observations[0].type == "file_absent"
    assert owned.connection.execute(
        "SELECT status FROM cleanup_items WHERE id=?", (item_id,)).fetchone() == (3,)
    assert active_calls == set()
    assert executor.unfinished_files() == ()
    assert supervisor.outstanding() == ()
    monkeypatch.setattr(host_runner, "call", original_runner)
    owner_probe.callback = None


async def test_delete_supervision_diagnostic_survives_unknown_finish(host_environment, monkeypatch):
    """DELETE 的原申请、实际提交和监督错误分别交付且不重放。"""
    cfg, owned, context, driver, executor, supervisor = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    await _accept_cleanup(owned, output_id)
    await check_unknown_cleanup_supervision(host_environment, monkeypatch, 'delete',
        _cleanup_factory(cfg, driver, executor))


async def test_query_supervision_diagnostic_survives_unknown_finish(host_environment, monkeypatch):
    """QUERY 的原申请、实际提交和监督错误分别交付且不重放。"""
    cfg, owned, context, driver, executor, supervisor = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    await _accept_cleanup(owned, output_id)
    await check_unknown_cleanup_supervision(host_environment, monkeypatch, 'query',
        _cleanup_factory(cfg, driver, executor))


async def test_late_owner_callback_does_not_retain_handle_after_fallback(
        host_environment, monkeypatch):
    """fallback 取回结果后，最后一次真实 drain 不得重新长期持有 handle。"""
    host_runner = host_environment.thread_runner
    cfg, owned, context, driver, executor, supervisor = host_environment
    await _save_photos(cfg, owned, context, driver)
    output_id, target = _seed_repaired_file(cfg, owned)
    await _accept_cleanup(owned, output_id)

    original_runner = host_runner.call
    owner_probe = OwnerHandoffProbe(monkeypatch, supervisor)
    real_drain = executor.drain_required
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

    monkeypatch.setattr(host_runner, "call", fail_after_completion)
    monkeypatch.setattr(
        executor, "drain_required", drain_fails_before_owner_consumption)
    monkeypatch.setattr(Path, "unlink", count_unlink)
    factory = _cleanup_factory(cfg, driver, executor)
    owner = cleanup_flow(factory)
    with cleanup_failure(ConsistencyError, match="监督交接失败"):
        await finish_cleanup_round(owner, context)

    handle, = owner_probe.handles
    assert handle.lease.released
    completion = await handle.wait()
    assert completion.ran
    assert executor.lease_of(handle.task_id) is None
    assert unlink_calls == [target]
    assert not target.exists()
    item_id = owned.connection.execute(
        "SELECT id FROM cleanup_items WHERE output_id=?", (output_id,)).fetchone()[0]
    saved = saved_cleanup_delete(owned.connection, item_id)
    assert saved is not None and saved.actual.observations[0].type == "file_absent"

    monkeypatch.setattr(executor, "drain_required", real_drain)
    await supervisor.drain_required()
    assert await handle.wait() is completion
    assert handle.lease.released
    assert executor.lease_of(handle.task_id) is None
    assert unlink_calls == [target]
    assert owner.required_settlements() > 0
    assert executor.unfinished_files() == ()


@pytest.mark.parametrize("scope_inside", [False, True],
                         ids=["scope-outside", "owned-scope"])
@pytest.mark.parametrize("cancel_waiter", [False, True],
                         ids=["no-cancellation", "waiter-cancelled"])
@pytest.mark.parametrize("runner_failure", [False, True],
                         ids=["no-diagnostic", "runner-diagnostic"])
async def test_host_task_control_and_diagnostic_delivery_matrix(host_environment, monkeypatch, scope_inside, cancel_waiter, runner_failure):
    """正式文件端口交付原实际结果和完整控制、监督错误图。"""
    cfg, owned, context, driver, executor, supervisor = host_environment
    await check_host_port_control(host_environment, monkeypatch,
        _cleanup_factory(cfg, driver, executor), scope_inside=scope_inside, cancel_waiter=cancel_waiter, runner_failure=runner_failure)


async def test_scope_outside_cancel_then_drain_failure_keeps_actual_and_control(host_environment, monkeypatch):
    """正式文件端口交付原实际结果和完整控制、监督错误图。"""
    cfg, owned, context, driver, executor, supervisor = host_environment
    await check_host_port_control(host_environment, monkeypatch,
        _cleanup_factory(cfg, driver, executor), drain_failure=True,
        cancel_label='ORIGINAL_CANCEL_BEFORE_DRAIN_FAILURE',
        repeat_label='REPEATED_CANCEL_BEFORE_DRAIN_FAILURE')


async def test_scope_outside_unavailable_actual_keeps_cancel_and_supervision_failstop(host_environment, monkeypatch):
    """正式文件端口交付原实际结果和完整控制、监督错误图。"""
    cfg, owned, context, driver, executor, supervisor = host_environment
    await check_host_port_control(host_environment, monkeypatch,
        _cleanup_factory(cfg, driver, executor), runner_failure=True, unavailable_actual=True,
        cancel_label='ORIGINAL_CANCEL_WITHOUT_RESULT', repeat_label='REPEATED_CANCEL_WITHOUT_RESULT',
        runner_message='PROBE runner failure before result delivery')

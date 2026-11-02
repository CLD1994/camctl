"""真实默认取消消费者必须采用 ready 撤回实际结果。"""

from dataclasses import replace
from pathlib import Path

import pytest

from camctl.bootstrap.flows import cancel_flow
from camctl.bootstrap.obtain_assembly import obtain_flow, session_obtain_assembly
from camctl.bootstrap.work_file_assembly import WorkFileRuntime
from camctl.contracts.workflow_errors import item_error_id
from camctl.host_files import handoff
from camctl.outputs.work_files import WorkFileLimits

from .test_output_binding_changes import (
    _NOW, _accept, _registry, _save_photos, environment,  # noqa: F401
)

pytestmark = pytest.mark.asyncio


async def _published(environment, *, direct_session=False):
    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    await _accept(owned, "2", [{"name": "取回已发布", "type": "obtain_action_outputs",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"source": {"plan_instance_id": "1", "group": "files"}}}])
    target = owned.connection.execute("SELECT id FROM actions WHERE type=4").fetchone()[0]
    factory = session_obtain_assembly(devices=cfg.devices, drivers=_registry(driver),
        staging=Path(cfg.paths.staging), ready=Path(cfg.paths.ready),
        processing=Path(cfg.paths.processing), segment_size=4,
        occurred_at=lambda: _NOW, monotonic_ns=lambda: 0)
    await obtain_flow(factory)(context)
    await obtain_flow(factory)(context)
    assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (target,)).fetchone() == (3,)
    assert owned.connection.execute("SELECT status FROM deliveries ORDER BY id").fetchall() == [(5,), (5,)]
    ready = Path(cfg.paths.ready)
    files = {row[0]: (ready / row[0]).read_bytes() for row in owned.connection.execute("SELECT file_name FROM deliveries ORDER BY id")}
    assert len(files) == 2
    runtime = None
    if not direct_session:
        runtime = WorkFileRuntime(staging=Path(cfg.paths.staging), limits=WorkFileLimits(32, 1))
        await runtime.initialize(context.database)
    await _accept(owned, "3", [{"name": "撤回已发布取回", "type": "cancel_task",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"target": {"action_instance_id": str(target)}}}])
    return target, files, runtime


@pytest.mark.parametrize("position", ["ready", "processing", "unlink_failed"])
async def test_cancel_published_delivery_uses_actual_withdrawal_result(environment, monkeypatch, position):
    cfg, owned, context, driver = environment
    target, contents, _ = await _published(environment, direct_session=True)
    ready, processing = Path(cfg.paths.ready), Path(cfg.paths.processing)
    if position == "processing":
        for name in contents:
            (ready / name).rename(processing / name)
    elif position == "unlink_failed":
        original_remove = handoff._remove
        def refused(path):
            if path.name in contents:
                raise OSError("withdraw unlink refused")
            return original_remove(path)
        monkeypatch.setattr(handoff, "_remove", refused)

    from camctl.acceptance.service import CommandMode
    from camctl.bootstrap.lifecycle import build_runtime, close_runtime
    from .test_output_binding_changes import Catalog
    from ..support.session import run_default_session

    session_cfg = replace(cfg, cleanup=replace(cfg.cleanup, work_file_batch_size=32,
        work_file_limit_per_run=1))
    deps = build_runtime(CommandMode.RUN, session_cfg, catalog=Catalog())
    try:
        outcome = await run_default_session(deps, None, _registry(driver).entry("camctl-adb"),
            poll_interval_s=0.001)
    finally:
        close_runtime(deps)
    assert outcome.succeeded, outcome

    assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (target,)).fetchone() == (3,)
    origin_status = owned.connection.execute("SELECT status FROM actions WHERE type=6").fetchone()[0]
    member = owned.connection.execute("SELECT status,error_code FROM cancel_items").fetchone()
    if position == "ready":
        assert all(not (ready / name).exists() for name in contents)
        assert owned.connection.execute("SELECT status,withdrawal_state FROM deliveries ORDER BY id").fetchall() == [(8, 3), (8, 3)]
        assert origin_status == 3 and member == (3, None)
    elif position == "processing":
        assert {name: (processing / name).read_bytes() for name in contents} == contents
        assert owned.connection.execute("SELECT status,withdrawal_state FROM deliveries ORDER BY id").fetchall() == [(5, 4), (5, 4)]
        assert origin_status == 3 and member == (3, None)
    else:
        assert {name: (ready / name).read_bytes() for name in contents} == contents
        assert owned.connection.execute("SELECT status,withdrawal_state FROM deliveries ORDER BY id").fetchall() == [(5, 5), (5, 5)]
        assert origin_status == 4
        assert member == (4, item_error_id("cancel_items", "target_cleanup_failed"))
    assert owned.connection.execute("SELECT cleanup_cursor_file_id FROM runtime_state").fetchone() == (None,)


@pytest.mark.parametrize("phase", ["projection", "commit_before", "commit_after"])
async def test_actual_withdrawal_result_is_held_with_original_key_after_save_failure(environment, monkeypatch, phase):
    from dataclasses import replace
    from camctl.outputs.handoff import WithdrawalChoice
    from camctl.persistence.repositories.outputs import OutputsRepository
    from camctl.session.service import StateDbFailure
    from .test_binding_transactions import _CommitFailure
    from ..operations.test_result_reuse import _FaultConnection

    cfg, owned, context, _driver = environment
    _target, contents, runtime = await _published(environment)
    ready, processing = Path(cfg.paths.ready), Path(cfg.paths.processing)
    original_remove, original_save = handoff._remove, OutputsRepository.advance_withdrawal
    removed, inputs = [], []
    fault_pending = True

    def remove(path):
        removed.append(path.name)
        original_remove(path)

    def save(repository, command, key, operation_owned):
        nonlocal fault_pending
        if command.choice is WithdrawalChoice.WITHDRAWN:
            inputs.append((command, key))
            if fault_pending:
                fault_pending = False
                faulty = (_FaultConnection(operation_owned.connection, "UPDATE cancel_delivery_items")
                          if phase == "projection" else _CommitFailure(operation_owned.connection, phase == "commit_after"))
                return original_save(repository, command, key, replace(operation_owned, connection=faulty))
        return original_save(repository, command, key, operation_owned)

    monkeypatch.setattr(handoff, "_remove", remove)
    monkeypatch.setattr(OutputsRepository, "advance_withdrawal", save)
    with pytest.raises(StateDbFailure):
        await cancel_flow(staging=Path(cfg.paths.staging),
            ready=ready, processing=processing, work_files=runtime)(context)
    assert len(removed) == 1 and not (ready / removed[0]).exists()
    assert owned.connection.execute("SELECT status FROM actions WHERE type=6").fetchone() == (2,)
    # cancel_flow 已关闭原独立连接；下一实际装配以新连接核实原输入，不能再 unlink。
    await cancel_flow(staging=Path(cfg.paths.staging), ready=ready, processing=processing, work_files=runtime)(context)
    assert inputs[0] == inputs[1]
    assert sorted(removed) == sorted(contents)
    assert len(removed) == len(set(removed)) == 2
    assert owned.connection.execute("SELECT status,withdrawal_state FROM deliveries ORDER BY id").fetchall() == [(8,3),(8,3)]
    assert owned.connection.execute("SELECT status FROM actions WHERE type=6").fetchone() == (3,)


async def test_default_session_cancel_waits_for_actual_withdrawal_and_saves_it(environment, monkeypatch):
    import asyncio
    import threading
    from camctl.acceptance.service import CommandMode
    from camctl.bootstrap.lifecycle import build_runtime, close_runtime
    from camctl.host_files.tasks import AsyncFileTask, FileTaskError, FileTaskExecutor, FileTaskId
    from camctl.persistence.business import BusinessDatabase
    from camctl.persistence.executor import DbExecutorError
    from camctl.session.locks import SessionLockConflictError, acquire_session
    from .test_output_binding_changes import Catalog
    from ..support.session import run_default_session

    cfg, owned, context, driver = environment
    _target, contents, _ = await _published(environment, direct_session=True)
    ready = Path(cfg.paths.ready)
    session_cfg = replace(cfg, cleanup=replace(cfg.cleanup, work_file_batch_size=32,
        work_file_limit_per_run=1))
    opened, tasks = [], []
    open_database = BusinessDatabase.open
    actual_run = FileTaskExecutor.run_owned_async_file_task
    async def track_open(cls, *args, **kwargs):
        database = await open_database(*args, **kwargs)
        opened.append(database)
        return database
    async def track_task(executor, file_task):
        if file_task.stage == "delivery_withdrawal":
            tasks.append((executor, file_task))
        return await actual_run(executor, file_task)
    monkeypatch.setattr(BusinessDatabase, "open", classmethod(track_open))
    monkeypatch.setattr(FileTaskExecutor, "run_owned_async_file_task", track_task)
    entered, release = threading.Event(), threading.Event()
    original_remove = handoff._remove
    removed = []

    def remove(path):
        if path.name not in contents:
            return original_remove(path)
        entered.set()
        assert release.wait(10), "actual withdrawal was never released"
        removed.append(path.name)
        original_remove(path)

    monkeypatch.setattr(handoff, "_remove", remove)
    deps = build_runtime(CommandMode.RUN, session_cfg, catalog=Catalog())
    task = asyncio.create_task(run_default_session(
        deps, None, _registry(driver).entry("camctl-adb"), poll_interval_s=0.001))
    try:
        while not entered.is_set() and not task.done():
            await asyncio.sleep(0)
        assert entered.is_set(), "ready withdrawal must execute actual unlink"
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert len(opened) == 1 and len(tasks) == 1
        assert await opened[0].read(lambda worker: worker.connection.execute("SELECT 1").fetchone()) == (1,)
        with pytest.raises(SessionLockConflictError):
            acquire_session(deps.session_lock)
        executor, original_task = tasks[0]
        conflict_entered = []
        async def conflict(_control):
            conflict_entered.append(True)
        with pytest.raises(FileTaskError):
            await actual_run(executor, AsyncFileTask(
                FileTaskId("conflict-check"), original_task.file_ids,
                "conflict_check", "核对文件资格", conflict))
        assert not conflict_entered
        assert owned.connection.execute("SELECT status FROM actions WHERE type=6").fetchone() == (2,)
        assert all((ready / name).read_bytes() == value for name, value in contents.items())
    finally:
        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            close_runtime(deps)
    assert len(removed) == 1
    with pytest.raises(DbExecutorError):
        await opened[0].read(lambda worker: worker.connection.execute("SELECT 1").fetchone())
    lease = acquire_session(deps.session_lock)
    lease.close()
    assert owned.connection.execute(
        "SELECT status,withdrawal_state FROM deliveries WHERE file_name=?", (removed[0],)).fetchone() == (8, 3)
    assert owned.connection.execute("SELECT status FROM actions WHERE type=6").fetchone() == (2,)

    # 新会话接续已保存的文件事实，不重复撤回已处理的交付。
    deps = build_runtime(CommandMode.RUN, session_cfg, catalog=Catalog())
    try:
        result = await run_default_session(
            deps, None, _registry(driver).entry("camctl-adb"), poll_interval_s=0.001)
    finally:
        close_runtime(deps)
    assert result.succeeded, result
    assert len(removed) == 2 and len(set(removed)) == 2
    assert owned.connection.execute("SELECT status FROM actions WHERE type=6").fetchone() == (3,)


@pytest.mark.parametrize("after_missing", ["processing", "unknown"])
async def test_ready_disappears_during_actual_withdrawal_uses_new_reliable_position(environment, monkeypatch, after_missing):
    cfg, owned, context, driver = environment
    _target, contents, _ = await _published(environment, direct_session=True)
    ready, processing = Path(cfg.paths.ready), Path(cfg.paths.processing)
    original_remove = handoff._remove

    def taken(path):
        if path.name not in contents:
            return original_remove(path)
        if after_missing == "processing":
            path.rename(processing / path.name)
        else:
            original_remove(path)
        raise FileNotFoundError("host already took ready file")

    monkeypatch.setattr(handoff, "_remove", taken)
    from camctl.acceptance.service import CommandMode
    from camctl.bootstrap.lifecycle import build_runtime, close_runtime
    from .test_output_binding_changes import Catalog
    from ..support.session import run_default_session

    session_cfg = replace(cfg, cleanup=replace(cfg.cleanup, work_file_batch_size=32,
        work_file_limit_per_run=1))
    deps = build_runtime(CommandMode.RUN, session_cfg, catalog=Catalog())
    try:
        outcome = await run_default_session(deps, None, _registry(driver).entry("camctl-adb"),
            poll_interval_s=0.001)
    finally:
        close_runtime(deps)
    assert outcome.succeeded, outcome
    expected = (5,4) if after_missing == "processing" else (5,6)
    assert owned.connection.execute("SELECT status,withdrawal_state FROM deliveries ORDER BY id").fetchall() == [expected,expected]
    assert owned.connection.execute("SELECT status FROM actions WHERE type=6").fetchone() == ((3,) if after_missing == "processing" else (4,))
    if after_missing == "processing":
        assert {name: (processing / name).read_bytes() for name in contents} == contents


@pytest.mark.parametrize("actual_state", ["failed", "unknown"])
@pytest.mark.parametrize("phase", ["projection", "commit_before", "commit_after"])
async def test_held_actual_withdrawal_failure_remains_cancel_failure_after_save_recovery(environment, monkeypatch, actual_state, phase):
    from dataclasses import replace
    from camctl.outputs.handoff import WithdrawalChoice
    from camctl.persistence.repositories.outputs import OutputsRepository
    from camctl.session.service import StateDbFailure
    from .test_binding_transactions import _CommitFailure
    from ..operations.test_result_reuse import _FaultConnection

    cfg, owned, context, _driver = environment
    target, contents, runtime = await _published(environment)
    ready, processing = Path(cfg.paths.ready), Path(cfg.paths.processing)
    expected_choice = WithdrawalChoice.FAILED if actual_state == "failed" else WithdrawalChoice.UNKNOWN
    original_save, attempted, inputs = OutputsRepository.advance_withdrawal, [], []
    first = True
    fault_delivery = owned.connection.execute("SELECT MAX(id) FROM deliveries").fetchone()[0]
    def remove(path):
        attempted.append(path.name)
        if actual_state == "failed":
            raise OSError("ready unlink refused")
        raise InterruptedError("ready unlink result unknown")
    def save(repository, command, key, operation_owned):
        nonlocal first
        if command.choice is expected_choice and command.delivery_id == fault_delivery:
            inputs.append((command, key))
            if first:
                first = False
                faulty = (_FaultConnection(operation_owned.connection, "UPDATE cancel_delivery_items")
                          if phase == "projection" else _CommitFailure(operation_owned.connection, phase == "commit_after"))
                return original_save(repository, command, key, replace(operation_owned, connection=faulty))
        return original_save(repository, command, key, operation_owned)
    monkeypatch.setattr(handoff, "_remove", remove)
    monkeypatch.setattr(OutputsRepository, "advance_withdrawal", save)
    with pytest.raises(StateDbFailure):
        await cancel_flow(staging=Path(cfg.paths.staging),
            ready=ready, processing=processing, work_files=runtime)(context)
    assert len(attempted) == 2
    assert owned.connection.execute("SELECT status FROM actions WHERE type=6").fetchone() == (2,)
    # 最后一份实际失败结果保存不确定；恢复后没有其余 pending 可掩盖汇总。
    await cancel_flow(staging=Path(cfg.paths.staging), ready=ready, processing=processing, work_files=runtime)(context)
    assert inputs[0] == inputs[1]
    assert sorted(attempted) == sorted(contents) and len(set(attempted)) == 2
    state = 5 if actual_state == "failed" else 6
    assert owned.connection.execute("SELECT status,withdrawal_state FROM deliveries ORDER BY id").fetchall() == [(5,state),(5,state)]
    assert owned.connection.execute("SELECT status,error_code FROM cancel_delivery_items ORDER BY id").fetchall() == [
        (4,item_error_id("cancel_delivery_items", "delivery_withdrawal_failed" if actual_state == "failed" else "delivery_position_unconfirmed"))] * 2
    assert owned.connection.execute("SELECT status FROM actions WHERE type=6").fetchone() == (4,)
    assert owned.connection.execute("SELECT status,cancel_requested FROM actions WHERE id=?", (target,)).fetchone() == (3,0)
    assert {name: (ready/name).read_bytes() for name in contents} == contents
    before = tuple(owned.connection.iterdump())
    await cancel_flow(staging=Path(cfg.paths.staging), ready=ready, processing=processing, work_files=runtime)(context)
    assert tuple(owned.connection.iterdump()) == before and len(attempted) == 2

"""真实取消汇总必须消费本次 staging 首次结果，不挂入其他历史责任。"""

from pathlib import Path

import pytest

from camctl.bootstrap.flows import cancel_flow
from camctl.bootstrap.obtain_assembly import obtain_flow, session_obtain_assembly
from camctl.bootstrap.work_file_assembly import WorkFileRuntime
from camctl.contracts.enums import enum_for
from camctl.contracts.workflow_errors import item_error_id
from camctl.outputs import work_files
from camctl.outputs.work_files import WorkFileLimits
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.session.service import StateDbFailure

from .test_output_binding_changes import _NOW, _accept, _registry, _save_photos, environment  # noqa: F401
from .test_work_file_runtime import _InterruptedReadDriver, _history_cleanup

pytestmark = pytest.mark.asyncio


async def _active_obtain(environment, request_id):
    cfg, owned, context, _driver = environment
    _accept(owned, request_id, [{"name": "取回待取消", "type": "obtain_action_outputs",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"source": {"plan_instance_id": "1", "group": "files"}}}])
    target = owned.connection.execute("SELECT MAX(id) FROM actions WHERE type=4").fetchone()[0]
    factory = session_obtain_assembly(
        devices=cfg.devices, drivers=_registry(_InterruptedReadDriver(owned)),
        staging=Path(cfg.paths.staging), ready=Path(cfg.paths.ready),
        processing=Path(cfg.paths.processing), segment_size=4,
        occurred_at=lambda: _NOW, monotonic_ns=lambda: 0)
    await obtain_flow(factory)(context)
    assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (target,)).fetchone() == (2,)
    assert owned.connection.execute(
        "SELECT committed_bytes FROM file_copies c JOIN deliveries d ON d.id=c.delivery_id"
        " WHERE d.action_id=? ORDER BY c.id", (target,)).fetchall() == [(4,), (4,)]
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
        " JOIN file_copies c ON c.id=r.copy_id JOIN deliveries d ON d.id=c.delivery_id"
        " WHERE d.action_id=? AND a.status=1", (target,)).fetchone() == (0,)
    return target


@pytest.mark.parametrize("result", ["completed", "failed", "unknown"])
async def test_cancel_origin_adopts_only_its_first_work_file_result(environment, monkeypatch, result):
    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    runtime = WorkFileRuntime(staging=Path(cfg.paths.staging), limits=WorkFileLimits(32, 1))
    runtime.initialize(owned)
    target = await _active_obtain(environment, "2")
    _accept(owned, "3", [{"name": "取消取回", "type": "cancel_task",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"target": {"action_instance_id": str(target)}}}])
    origin = owned.connection.execute("SELECT id FROM actions WHERE type=6").fetchone()[0]
    if result == "failed":
        def failed(_path):
            raise OSError("first unlink refused")
        monkeypatch.setattr(work_files, "_remove_work_file", failed)
    elif result == "unknown":
        monkeypatch.setattr(OutputsRepository, "save_cleanup_result", lambda *args: DbOutcome(
            DbOutcomeKind.UNKNOWN, error=RuntimeError("first result commit unknown"), stage="commit"))
    flow = cancel_flow(ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing), work_files=runtime)
    if result == "unknown":
        with pytest.raises(StateDbFailure):
            await flow(context)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (origin,)).fetchone() == (2,)
        assert owned.connection.execute("SELECT status FROM cancel_items WHERE action_id=?", (origin,)).fetchone() == (2,)
        assert len(runtime.pending_results) == 1
    else:
        await flow(context)
        expected_origin = 3 if result == "completed" else 4
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (origin,)).fetchone() == (expected_origin,)
        row = owned.connection.execute("SELECT status,error_code FROM cancel_items WHERE action_id=?", (origin,)).fetchone()
        assert row == ((3, None) if result == "completed" else
            (4, item_error_id("cancel_items", "target_cleanup_failed")))
        states = owned.connection.execute(
            "SELECT f.cleanup_state FROM intermediate_files f JOIN deliveries d ON d.id=f.owner_delivery_id"
            " WHERE d.action_id=? ORDER BY f.id", (target,)).fetchall()
        assert states == ([(4,), (4,)] if result == "completed" else [(5,), (5,)])
    assert runtime.history.scan.remaining == 1
    assert owned.connection.execute("SELECT cleanup_cursor_file_id FROM runtime_state").fetchone() == (None,)


async def test_cancel_first_cleanup_does_not_consume_unrelated_old_history(environment):
    cfg = await _history_cleanup(environment)
    owned, context = environment[1:3]
    old = owned.connection.execute("SELECT id,cleanup_state FROM intermediate_files ORDER BY id").fetchall()
    runtime = WorkFileRuntime(staging=Path(cfg.paths.staging), limits=WorkFileLimits(32, 1))
    runtime.initialize(owned)
    target = await _active_obtain(environment, "4")
    _accept(owned, "5", [{"name": "取消新取回", "type": "cancel_task",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"target": {"action_instance_id": str(target)}}}])

    await cancel_flow(ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing), work_files=runtime)(context)

    assert owned.connection.execute("SELECT id,cleanup_state FROM intermediate_files WHERE id<=? ORDER BY id",
        (old[-1][0],)).fetchall() == old
    assert runtime.history.scan.remaining == 1
    assert owned.connection.execute("SELECT cleanup_cursor_file_id FROM runtime_state").fetchone() == (None,)
    assert owned.connection.execute("SELECT status FROM actions WHERE type=6").fetchone() == (3,)


async def test_cancel_waits_shared_actual_file_owner_before_any_terminal(environment):
    import asyncio
    from camctl.host_files.tasks import AsyncFileTask, FileTaskId

    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    runtime = WorkFileRuntime(staging=Path(cfg.paths.staging), limits=WorkFileLimits(32, 1))
    runtime.initialize(owned)
    target = await _active_obtain(environment, "2")
    file_id = owned.connection.execute(
        "SELECT f.id FROM intermediate_files f JOIN deliveries d ON d.id=f.owner_delivery_id"
        " WHERE d.action_id=? ORDER BY f.id LIMIT 1", (target,)).fetchone()[0]
    _accept(owned, "3", [{"name": "取消在用取回", "type": "cancel_task",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"target": {"action_instance_id": str(target)}}}])
    entered, release = asyncio.Event(), asyncio.Event()

    async def actual(_control):
        entered.set()
        await release.wait()

    owner = asyncio.create_task(runtime.executor.run_owned_async_file_task(AsyncFileTask(
        FileTaskId("original-local-operation"), (file_id,), "hash", "原副本实际使用", actual)))
    try:
        await entered.wait()
        before = tuple(owned.connection.execute(
            "SELECT r.id,r.status,r.attempts_used FROM operation_runs r JOIN file_copies c ON c.id=r.copy_id"
            " JOIN deliveries d ON d.id=c.delivery_id WHERE d.action_id=? ORDER BY r.id", (target,)))
        flow = cancel_flow(ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing), work_files=runtime)
        await flow(context)
        assert owned.connection.execute("SELECT status,cancel_requested FROM actions WHERE id=?", (target,)).fetchone() == (2, 1)
        assert owned.connection.execute("SELECT status FROM cancel_items").fetchone() == (2,)
        assert tuple(owned.connection.execute(
            "SELECT r.id,r.status,r.attempts_used FROM operation_runs r JOIN file_copies c ON c.id=r.copy_id"
            " JOIN deliveries d ON d.id=c.delivery_id WHERE d.action_id=? ORDER BY r.id", (target,))) == before
        assert owned.connection.execute("SELECT cleanup_state FROM intermediate_files WHERE id=?", (file_id,)).fetchone() == (1,)
        release.set()
        await owner
        await flow(context)
        assert owned.connection.execute("SELECT status FROM actions WHERE type=6").fetchone() == (3,)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (target,)).fetchone() == (6,)
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)


async def test_cancel_of_terminal_obtain_keeps_its_old_history_out_of_first_cleanup(environment):
    cfg = await _history_cleanup(environment)
    owned, context = environment[1:3]
    target = owned.connection.execute("SELECT id FROM actions WHERE type=4").fetchone()[0]
    old = tuple(owned.connection.execute("SELECT * FROM intermediate_files ORDER BY id"))
    runtime = WorkFileRuntime(staging=Path(cfg.paths.staging), limits=WorkFileLimits(32, 1))
    runtime.initialize(owned)
    _accept(owned, "3", [{"name": "取消旧终态取回", "type": "cancel_task",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"target": {"action_instance_id": str(target)}}}])

    await cancel_flow(ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing), work_files=runtime)(context)

    assert tuple(owned.connection.execute("SELECT * FROM intermediate_files ORDER BY id")) == old
    assert runtime.processed == {}
    assert runtime.history.scan.remaining == 1
    assert owned.connection.execute("SELECT status FROM actions WHERE type=6").fetchone() == (3,)
    assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (target,)).fetchone() == (4,)

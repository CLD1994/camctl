"""真实取消汇总必须消费本次 staging 首次结果，不挂入其他历史责任。"""

from pathlib import Path

import pytest

from camctl.bootstrap.obtain_assembly import obtain_flow, session_obtain_assembly
from camctl.contracts.workflow_errors import item_error_id
from camctl.outputs import work_files
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository

from .test_output_binding_changes import _NOW, _accept, _registry, _save_photos, environment  # noqa: F401
from .test_work_file_runtime import _InterruptedReadDriver, _history_cleanup

pytestmark = pytest.mark.asyncio


async def _active_obtain(environment, request_id):
    cfg, owned, context, _driver = environment
    await _accept(owned, request_id, [{"name": "取回待取消", "type": "obtain_action_outputs",
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


@pytest.mark.parametrize("result", ["completed", "failed"])
async def test_default_session_cancel_adopts_actual_first_work_file_result(environment, monkeypatch, result):
    from dataclasses import replace
    from camctl.acceptance.service import CommandMode
    from camctl.bootstrap.lifecycle import build_runtime, close_runtime
    from .test_output_binding_changes import Catalog
    from ..support.session import run_default_session

    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    target = await _active_obtain(environment, "2")
    await _accept(owned, "3", [{"name": "取消取回", "type": "cancel_task",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"target": {"action_instance_id": str(target)}}}])
    origin = owned.connection.execute("SELECT id FROM actions WHERE type=6").fetchone()[0]
    paths = {row[0] for row in owned.connection.execute(
        "SELECT f.relative_path FROM intermediate_files f JOIN deliveries d ON d.id=f.owner_delivery_id"
        " WHERE d.action_id=?", (target,))}
    original_remove = work_files._remove_work_file
    removed = []
    def remove(path):
        if str(path.relative_to(cfg.paths.staging)) in paths:
            removed.append(str(path.relative_to(cfg.paths.staging)))
            if result == "failed":
                raise OSError("first unlink refused")
        return original_remove(path)
    monkeypatch.setattr(work_files, "_remove_work_file", remove)
    session_cfg = replace(cfg, cleanup=replace(cfg.cleanup, work_file_batch_size=32,
        work_file_limit_per_run=1))
    deps = build_runtime(CommandMode.RUN, session_cfg, catalog=Catalog())
    try:
        outcome = await run_default_session(deps, None, _registry(driver).entry("camctl-adb"),
            poll_interval_s=0.001)
    finally:
        close_runtime(deps)
    assert outcome.succeeded, outcome
    expected_origin = 3 if result == "completed" else 4
    assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (origin,)).fetchone() == (expected_origin,)
    row = owned.connection.execute("SELECT status,error_code FROM cancel_items WHERE action_id=?", (origin,)).fetchone()
    assert row == ((3, None) if result == "completed" else
        (4, item_error_id("cancel_items", "target_cleanup_failed")))
    states = owned.connection.execute(
        "SELECT f.cleanup_state FROM intermediate_files f JOIN deliveries d ON d.id=f.owner_delivery_id"
        " WHERE d.action_id=? ORDER BY f.id", (target,)).fetchall()
    assert states == ([(4,), (4,)] if result == "completed" else [(5,), (5,)])
    assert len(removed) == 2 and set(removed) == paths
    assert owned.connection.execute("SELECT cleanup_cursor_file_id FROM runtime_state").fetchone() == (None,)


@pytest.mark.parametrize("terminal_old", [False, True])
async def test_default_session_cancel_keeps_first_cleanup_separate_from_old_history(environment, terminal_old):
    from dataclasses import replace
    from camctl.acceptance.service import CommandMode
    from camctl.bootstrap.lifecycle import build_runtime, close_runtime
    from camctl.persistence.repositories.cancellation import CancellationRepository
    from support.repository_ports import repository_port
    from .test_output_binding_changes import Catalog
    from ..support.session import run_default_session

    cfg = await _history_cleanup(environment)
    original_cfg, owned, context, driver = environment
    old = owned.connection.execute("SELECT id,cleanup_state FROM intermediate_files ORDER BY id").fetchall()
    if terminal_old:
        target = owned.connection.execute("SELECT id FROM actions WHERE type=4").fetchone()[0]
        request_id = "3"
        new_files = set()
    else:
        target = await _active_obtain(environment, "4")
        request_id = "5"
        new_files = {row[0] for row in owned.connection.execute(
            "SELECT f.id FROM intermediate_files f JOIN deliveries d ON d.id=f.owner_delivery_id"
            " WHERE d.action_id=?", (target,))}
    await _accept(owned, request_id, [{"name": "取消取回", "type": "cancel_task",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"target": {"action_instance_id": str(target)}}}])
    origin = owned.connection.execute("SELECT id FROM actions WHERE type=6").fetchone()[0]
    snapshots, saves = [], []

    class CancelObserver(CancellationRepository):
        def finish_cancel_action(self, command, key, worker):
            result = super().finish_cancel_action(command, key, worker)
            if result.kind is DbOutcomeKind.COMPLETED:
                snapshots.append((worker.connection.execute(
                    "SELECT id,cleanup_state FROM intermediate_files WHERE id<=? ORDER BY id", (old[-1][0],)).fetchall(),
                    worker.connection.execute("SELECT cleanup_cursor_file_id FROM runtime_state").fetchone()))
            return result

    class OutputObserver(OutputsRepository):
        def save_cleanup_result(self, request, key, worker):
            saves.append((request.file_id, request.advance_cursor))
            return super().save_cleanup_result(request, key, worker)

    session_cfg = replace(original_cfg, cleanup=cfg.cleanup)
    deps = build_runtime(CommandMode.RUN, session_cfg, catalog=Catalog())
    try:
        with repository_port(CancellationRepository, CancelObserver(), ("finish_cancel_action",),
                matches=lambda _name, args, _kwargs: args[0].action_id == origin), \
             repository_port(OutputsRepository, OutputObserver(), ("save_cleanup_result",)):
            outcome = await run_default_session(deps, None, _registry(driver).entry("camctl-adb"),
                poll_interval_s=0.001)
    finally:
        close_runtime(deps)
    assert outcome.succeeded, outcome
    assert snapshots == [(old, (None,))]
    new_saves = [(file_id, advance) for file_id, advance in saves if file_id in new_files]
    assert len(new_saves) == len(new_files)
    assert set(new_saves) == {(file_id, False) for file_id in new_files}
    old_saves = [(file_id, advance) for file_id, advance in saves
        if file_id in {identity for identity, _ in old}]
    assert old_saves == [(old[0][0], True)]
    assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (origin,)).fetchone() == (3,)
    assert owned.connection.execute("SELECT id,cleanup_state FROM intermediate_files WHERE id<=? ORDER BY id",
        (old[-1][0],)).fetchall() == [(old[0][0], 4), old[1]]
    assert owned.connection.execute("SELECT cleanup_cursor_file_id FROM runtime_state").fetchone() == (old[0][0],)


async def test_default_session_cancel_waits_for_active_read_before_file_cleanup(environment, monkeypatch):
    import asyncio
    import threading
    from dataclasses import replace
    from camctl.acceptance.service import CommandMode
    from camctl.bootstrap.lifecycle import build_runtime, close_runtime
    from camctl.devices.read_session import ReadSession
    from .test_output_binding_changes import Catalog, Driver, _CONTENT, _MemoryStream
    from ..support.session import run_default_session

    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    target = await _active_obtain(environment, "2")
    entered, release = threading.Event(), threading.Event()
    closed = []

    class HeldStream(_MemoryStream):
        def read(self, limit):
            entered.set()
            assert release.wait(10), "原读取没有获得释放"
            return super().read(limit)

        def close(self):
            closed.append(True)
            return super().close()

    class HeldDriver(Driver):
        async def open_read(self, source, offset, ticket, *, idle_timeout_s):
            return ReadSession(source, offset, HeldStream(_CONTENT[offset:]), idle_timeout_s)

    held_driver = HeldDriver(owned)
    original_remove = work_files._remove_work_file
    removed = []
    def remove(path):
        removed.append(str(path.relative_to(cfg.paths.staging)))
        return original_remove(path)
    monkeypatch.setattr(work_files, "_remove_work_file", remove)
    session_cfg = replace(cfg, cleanup=replace(cfg.cleanup, work_file_batch_size=32,
        work_file_limit_per_run=1))
    deps = build_runtime(CommandMode.RUN, session_cfg, catalog=Catalog())
    task = asyncio.create_task(run_default_session(
        deps, None, _registry(held_driver).entry("camctl-adb"), poll_interval_s=0.001))
    try:
        async with asyncio.timeout(10):
            while not entered.is_set() and not task.done():
                await asyncio.sleep(0.001)
        assert entered.is_set(), f"会话在原读取开始前结束: {task.result() if task.done() else None}"
        before = tuple(owned.connection.execute(
            "SELECT r.id,r.status,r.attempts_used FROM operation_runs r JOIN file_copies c ON c.id=r.copy_id"
            " JOIN deliveries d ON d.id=c.delivery_id WHERE d.action_id=? ORDER BY r.id", (target,)))
        await _accept(owned, "3", [{"name": "取消在用取回", "type": "cancel_task",
            "scheduled_at": "2026-01-15 09:00:00",
            "params": {"target": {"action_instance_id": str(target)}}}])
        async with asyncio.timeout(10):
            while owned.connection.execute("SELECT cancel_requested FROM actions WHERE id=?", (target,)).fetchone() != (1,):
                if task.done():
                    raise AssertionError(f"取消生效前会话已结束: {task.result()}")
                await asyncio.sleep(0.001)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (target,)).fetchone() == (2,)
        assert owned.connection.execute("SELECT status FROM cancel_items").fetchone() == (2,)
        rows = owned.connection.execute(
            "SELECT f.relative_path,f.cleanup_state FROM intermediate_files f JOIN deliveries d ON d.id=f.owner_delivery_id"
            " WHERE d.action_id=? ORDER BY f.id", (target,)).fetchall()
        assert rows and all(state == 1 and (Path(cfg.paths.staging) / path).is_file() for path, state in rows)
        assert not closed and not task.done() and not removed
        assert tuple(owned.connection.execute(
            "SELECT r.id,r.status,r.attempts_used FROM operation_runs r JOIN file_copies c ON c.id=r.copy_id"
            " JOIN deliveries d ON d.id=c.delivery_id WHERE d.action_id=? ORDER BY r.id", (target,))) == before
        release.set()
        outcome = await task
        assert outcome.succeeded, outcome
        assert closed
        assert sorted(removed) == sorted(path for path, _ in rows)
        assert all(not (Path(cfg.paths.staging) / path).exists() for path, _ in rows)
        assert owned.connection.execute("SELECT status FROM actions WHERE type=6").fetchone() == (3,)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (target,)).fetchone() == (6,)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        close_runtime(deps)


async def test_default_session_cancel_stops_when_first_cleanup_commit_is_unknown(environment, monkeypatch):
    from dataclasses import replace
    from camctl.acceptance.service import CommandMode
    from camctl.bootstrap.lifecycle import build_runtime, close_runtime
    from support.repository_ports import repository_port
    from .test_output_binding_changes import Catalog
    from ..support.bootstrap_consumers import TransactionFault
    from ..support.session import run_default_session

    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    target = await _active_obtain(environment, "2")
    await _accept(owned, "3", [{"name": "取消取回", "type": "cancel_task",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"target": {"action_instance_id": str(target)}}}])
    origin = owned.connection.execute("SELECT id FROM actions WHERE type=6").fetchone()[0]
    files = {row[0]: row[1] for row in owned.connection.execute(
        "SELECT f.id,f.relative_path FROM intermediate_files f JOIN deliveries d ON d.id=f.owner_delivery_id"
        " WHERE d.action_id=?", (target,))}
    original_remove = work_files._remove_work_file
    removed = []
    def remove(path):
        removed.append(str(path.relative_to(cfg.paths.staging)))
        return original_remove(path)
    monkeypatch.setattr(work_files, "_remove_work_file", remove)
    requests, errors = [], []

    class SaveObserver(OutputsRepository):
        def save_cleanup_result(self, request, key, worker):
            requests.append((request, key))
            fault = TransactionFault(worker.connection, after_commit=True)
            result = super().save_cleanup_result(request, key, replace(worker, connection=fault))
            assert fault.commit_calls == 1 and result.kind is DbOutcomeKind.UNKNOWN
            errors.append(result.error)
            return result

    session_cfg = replace(cfg, cleanup=replace(cfg.cleanup, work_file_batch_size=32,
        work_file_limit_per_run=1))
    deps = build_runtime(CommandMode.RUN, session_cfg, catalog=Catalog())
    try:
        with repository_port(OutputsRepository, SaveObserver(), ("save_cleanup_result",),
                matches=lambda _name, args, _kwargs: args[0].file_id in files):
            outcome = await run_default_session(deps, None, _registry(driver).entry("camctl-adb"),
                poll_interval_s=0.001)
    finally:
        close_runtime(deps)
    assert not outcome.succeeded and outcome.reason == "state_db_error"
    assert len(requests) == len(errors) == len(removed) == 1
    request, key = requests[0]
    assert request.file_id in files and not request.advance_cursor and key is not None
    assert removed == [files[request.file_id]]
    assert str(errors[0]) in str(outcome.details)
    assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (origin,)).fetchone() == (2,)
    assert owned.connection.execute("SELECT status FROM cancel_items WHERE action_id=?", (origin,)).fetchone() == (2,)
    assert owned.connection.execute("SELECT cleanup_cursor_file_id FROM runtime_state").fetchone() == (None,)

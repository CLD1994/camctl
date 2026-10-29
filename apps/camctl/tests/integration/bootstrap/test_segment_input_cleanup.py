"""分段检查副本沿正式来源、取回、取消和文件清理接口结束保留责任。"""

import asyncio
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.host_files.tasks import AsyncFileTask, FileTaskId
from camctl.outputs.work_files import WorkFileContext, WorkFileLimits, WorkFileSingleOutcome, WorkFileAction, clean_one_work_file
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository, SettleCanceledObtain
from camctl.contracts.values import new_operation_key

from ..capture.test_input_copy import _CONTENT
from ..support.bootstrap_read_cases import segment_input_case, default_work_session

pytestmark = pytest.mark.asyncio


def _context(world):
    return WorkFileContext(repository=OutputsRepository(), database=world.database,
        staging=Path(world.cfg.paths.staging), occurred_at=world.wall[0], limits=WorkFileLimits(1, 1), executor=world.executor)


async def _clean(world, context=None):
    context = _context(world) if context is None else context
    return [await clean_one_work_file(identity, context) for identity, _relative, _state in world.inputs]


async def _published_world(tmp_path, capture_resources, monkeypatch, values, **options):
    world = await segment_input_case(tmp_path, capture_resources, monkeypatch, values, **options)
    await world.publish()
    return world


@pytest.mark.parametrize("waiter_result", ["succeeded", "failed", "canceled", "canceled_prepared"])
async def test_last_bound_waiter_releases_new_source_input_in_original_run(
        tmp_path, capture_resources, monkeypatch, waiter_result):
    world = await segment_input_case(tmp_path, capture_resources, monkeypatch, (Decimal(60),))
    try:
        if waiter_result == "succeeded":
            await world.database.close()
            result = await default_work_session(world, monkeypatch, limit=1, batch=1, on_initialized=world.publish)
            assert result["ceilings"] == [0]
            assert len(result["cleaned"]) == len(set(result["cleaned"]))
            assert set(world.paths) <= set(result["cleaned"])
            assert all(not path.exists() for path in world.paths.values())
            assert world.owned.connection.execute("SELECT cleanup_cursor_file_id FROM runtime_state").fetchone() == (None,)
        else:
            await world.publish()
            initial = await _clean(world)
            assert all(result.decision.action is WorkFileAction.KEEP_REQUIRED for result in initial)
            assert all(path.exists() for path in world.paths.values())
            if waiter_result == "failed":
                from camctl.devices.read_session import ReadChunk
                monkeypatch.setattr("camctl.host_files.io.LocalSourceReader.read_chunk",
                    lambda _reader, _limit: ReadChunk(data=None, error="local read refused"))
                await world.advance_obtains()
            else:
                if waiter_result == "canceled_prepared":
                    await world.advance_obtains(1)
                    assert world.owned.connection.execute("SELECT COUNT(*) FROM operation_attempts WHERE status=1").fetchone() == (0,)
                for index, (waiter, _selection) in enumerate(world.early):
                    await world.cancel(waiter)
                    if index == 0:
                        assert all(result.decision.action is WorkFileAction.KEEP_REQUIRED for result in await _clean(world))
                        assert all(path.exists() for path in world.paths.values())
            late = None
            if waiter_result == "canceled":
                late, _selection = await world.add_waiter(22, start_only=True)
                assert world.owned.connection.execute("SELECT status FROM actions WHERE id=?", (late,)).fetchone() == (2,)
            finished = await _clean(world)
            assert all(result.outcome is WorkFileSingleOutcome.DELETED for result in finished)
            assert all(not path.exists() for path in world.paths.values())
            if late is not None:
                assert world.owned.connection.execute("SELECT status FROM actions WHERE id=?", (late,)).fetchone() == (2,)
                await world.cancel(late)
        expected = 3 if waiter_result == "succeeded" else 4 if waiter_result == "failed" else 6
        assert [world.owned.connection.execute("SELECT status FROM actions WHERE id=?", (identity,)).fetchone()[0]
            for identity, _selection in world.early] == [expected, expected]
        before = tuple(world.owned.connection.iterdump())
        for request, key in world.cancellations:
            for original_key in (key, new_operation_key()):
                outcome = await world.database.write(lambda owned, request=request, original_key=original_key:
                    OutputsRepository().settle_canceled_obtain(request, original_key, owned), key=original_key)
                assert outcome.kind is DbOutcomeKind.COMPLETED and outcome.value.complete, outcome.error
        assert tuple(world.owned.connection.iterdump()) == before
    finally:
        await world.close()


async def test_completed_copy_without_selected_consumers_cleans_before_waiters_end(tmp_path, capture_resources, monkeypatch):
    world = await _published_world(tmp_path, capture_resources, monkeypatch, (Decimal(60),), mode="preview")
    try:
        assert all(result.outcome is WorkFileSingleOutcome.DELETED for result in await _clean(world))
        assert all(not path.exists() for path in world.paths.values())
        assert [world.owned.connection.execute("SELECT status FROM actions WHERE id=?", (identity,)).fetchone()[0]
            for identity, _selection in world.early] == [2, 2]
    finally:
        await world.close()


async def test_finished_partial_inspection_copy_has_no_bound_consumers(tmp_path, capture_resources, monkeypatch):
    world = await _published_world(tmp_path, capture_resources, monkeypatch, (Decimal(60),), partial=True)
    try:
        committed, size = world.owned.connection.execute(
            "SELECT committed_bytes,source_size FROM file_copies WHERE inspection_id IS NOT NULL").fetchone()
        assert 0 < committed < size
        assert all(result.outcome is WorkFileSingleOutcome.DELETED for result in await _clean(world))
        assert all(not path.exists() for path in world.paths.values())
    finally:
        await world.close()


@pytest.mark.parametrize("failure", ["query", "delete"])
async def test_first_inspection_cleanup_failure_is_saved_without_same_run_retry(
        tmp_path, capture_resources, monkeypatch, failure):
    world = await _published_world(tmp_path, capture_resources, monkeypatch, (Decimal(60),), mode="preview")
    paths = set(world.paths.values())
    calls = []
    actual_query, actual_remove = Path.stat, Path.unlink

    def query(path, *args, **options):
        if Path(path) in paths:
            calls.append(("query", Path(path)))
            if failure == "query":
                raise OSError("stat refused")
        return actual_query(path, *args, **options)

    def remove(path, *args, **options):
        if Path(path) in paths:
            calls.append(("delete", Path(path)))
            raise OSError("unlink refused")
        return actual_remove(path, *args, **options)

    monkeypatch.setattr(Path, "stat", query)
    monkeypatch.setattr(Path, "unlink", remove)
    try:
        context = _context(world)
        before = world.owned.connection.execute("SELECT id,status,error_code,error_details_json FROM actions ORDER BY id").fetchall()
        first = await _clean(world, context)
        again = await _clean(world, context)
        assert all(result.outcome is WorkFileSingleOutcome.FAILED for result in first + again)
        assert calls.count(("query", next(iter(paths)))) == 1
        assert len([name for name, _path in calls if name == "delete"]) == int(failure == "delete")
        assert all(actual_query(path) is not None for path in paths)
        assert world.owned.connection.execute("SELECT retention_state,cleanup_state FROM intermediate_files WHERE owner_action_id=?",
            (world.source_id,)).fetchall() == [(2, 5)]
        assert world.owned.connection.execute("SELECT id,status,error_code,error_details_json FROM actions ORDER BY id").fetchall() == before
    finally:
        await world.close()


async def test_unconfirmed_copy_without_consumers_cleans_while_other_waiters_run(tmp_path, capture_resources, monkeypatch):
    world = await _published_world(tmp_path, capture_resources, monkeypatch,
        (Decimal(18), Decimal(27), RuntimeError("probe unavailable")))
    try:
        assert [row[2] for row in world.inputs] == [2, 2, 4]
        results = await _clean(world)
        assert [result.outcome for result in results] == [WorkFileSingleOutcome.KEPT,
            WorkFileSingleOutcome.KEPT, WorkFileSingleOutcome.DELETED]
        assert [path.exists() for path in world.paths.values()] == [True, True, False]
        assert [world.owned.connection.execute("SELECT status FROM actions WHERE id=?", (identity,)).fetchone()[0]
            for identity, _selection in world.early] == [2, 2]
    finally:
        await world.close()


async def test_new_input_waits_for_actual_source_read_after_publication_is_consumed(tmp_path, capture_resources, monkeypatch):
    world = await _published_world(tmp_path, capture_resources, monkeypatch, (Decimal(60),), mode="preview")
    file_id, path = next(iter(world.paths.items()))
    entered, finish = asyncio.Event(), asyncio.Event()
    context = _context(world)

    async def actual(_control):
        with path.open("rb") as source:
            prefix = source.read(4)
            entered.set()
            await finish.wait()
            return prefix + source.read()

    operation = asyncio.create_task(context.executor.run_owned_async_file_task(AsyncFileTask(
        FileTaskId("original-inspection-source-read"), (file_id,), "read_file", "检查副本实际读取", actual)))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        blocked = await clean_one_work_file(file_id, context)
        assert blocked.decision.action is WorkFileAction.KEEP_IN_USE and path.exists()
        finish.set()
        assert await operation == _CONTENT
        completed = await clean_one_work_file(file_id, _context(world))
        assert completed.outcome is WorkFileSingleOutcome.DELETED and not path.exists()
    finally:
        finish.set()
        await operation
        await world.close()


async def test_late_waiter_terminal_does_not_requeue_old_source_as_first_responsibility(tmp_path, capture_resources, monkeypatch):
    world = await _published_world(tmp_path, capture_resources, monkeypatch, (Decimal(60),))
    try:
        await world.advance_obtains()
        file_id = world.inputs[0][0]
        await world.database.close()

        async def late(database):
            world.database = database
            waiter, _selection = await world.add_waiter(22, start_only=True)
            assert world.owned.connection.execute("SELECT status FROM actions WHERE id=?", (waiter,)).fetchone() == (2,)
            facts = await database.read(lambda owned: OutputsRepository().load_work_file_state(file_id, owned))
            assert facts.owner_finished
            await world.cancel(waiter)

        result = await default_work_session(world, monkeypatch, limit=1, batch=1, on_initialized=late)
        assert result["ceilings"] == [file_id]
        assert result["cleaned"].count(file_id) == 1
        assert world.owned.connection.execute("SELECT cleanup_cursor_file_id FROM runtime_state").fetchone() == (file_id,)
    finally:
        await world.close()


@pytest.mark.parametrize("fault", ["copy", "inspection", "source", "file_owner", "horizon"])
async def test_cancel_reused_copy_rejects_damaged_original_associations(tmp_path, capture_resources, monkeypatch, fault):
    world = await _published_world(tmp_path, capture_resources, monkeypatch, (Decimal(30), Decimal(30), Decimal(30)))
    try:
        await world.advance_obtains(1)
        waiter, selection = world.early[0]
        await world.cancel(waiter, settle=False)
        members = tuple(row[0] for row in world.owned.connection.execute(
            "SELECT id FROM obtain_items WHERE selection_id=? ORDER BY id", (selection,)))
        copies = world.owned.connection.execute(
            "SELECT c.id,c.inspection_id,c.source_device_file_id,c.target_file_id FROM file_copies c WHERE inspection_id IS NOT NULL ORDER BY c.id").fetchall()
        delivery_copy = world.owned.connection.execute(
            "SELECT c.id FROM file_copies c JOIN deliveries d ON d.id=c.delivery_id WHERE d.action_id=? ORDER BY c.id LIMIT 1", (waiter,)).fetchone()[0]
        if fault == "copy":
            world.owned.connection.execute("UPDATE file_copies SET source_intermediate_file_id=? WHERE id=?", (copies[1][3], delivery_copy))
        elif fault == "inspection":
            third = world.owned.connection.execute("SELECT id FROM segment_members WHERE role=1 ORDER BY id DESC LIMIT 1").fetchone()[0]
            world.owned.connection.execute("UPDATE segment_inspections SET member_id=? WHERE id=?", (third, copies[0][1]))
        elif fault == "source":
            world.owned.connection.execute("UPDATE file_copies SET source_device_file_id=? WHERE id=?", (copies[1][2], copies[0][0]))
        elif fault == "file_owner":
            world.owned.connection.execute("UPDATE device_files SET source_action_id=? WHERE id=?", (waiter, copies[0][2]))
        else:
            world.owned.connection.execute("UPDATE actions SET created_event_id=last_event_id WHERE id=?", (waiter,))
        world.owned.connection.commit()
        before = tuple(world.owned.connection.iterdump())
        result = OutputsRepository().settle_canceled_obtain(
            SettleCanceledObtain(waiter, world.wall[0], members), new_operation_key(), world.owned)
        assert result.kind is DbOutcomeKind.ROLLED_BACK, result.error
        assert tuple(world.owned.connection.iterdump()) == before
    finally:
        await world.close()

"""正常 run 的快照拥有者：通知、发现、停用和实际收场。"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import OperationKey
from camctl.history.snapshots import (
    MaintenanceOutcome, MaintenanceResult, SavedProgress, SnapshotRef,
    SnapshotEnqueueTimeout, SnapshotMaintenanceError,
)
from camctl.persistence import snapshot_runtime
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.runtime import OwnedConnection


def _owner(mocker, *, threshold=64):
    database = create_autospec(BusinessDatabase, instance=True)
    database.read.return_value = 12
    store = mocker.patch.object(snapshot_runtime, "SqliteSnapshotStore", autospec=True)
    store.return_value.discover_objects.return_value = (12, False)
    maintain = mocker.patch.object(snapshot_runtime, "maintain_snapshots", autospec=True)
    maintain.return_value = MaintenanceResult(MaintenanceOutcome.IDLE)
    disabled = mocker.Mock()
    owner = snapshot_runtime.SnapshotMaintenanceOwner(
        database, Path("/state.sqlite"), threshold=threshold, batch_size=16,
        on_disabled=disabled)
    return owner, database, store, maintain, disabled


async def _turns():
    for _ in range(6):
        await asyncio.sleep(0)


def _owned(row):
    owned = create_autospec(OwnedConnection, instance=True)
    owned.connection = create_autospec(sqlite3.Connection, instance=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    cursor.fetchone.return_value = row
    owned.connection.execute.return_value = cursor
    return owned


@pytest.mark.asyncio
async def test_start_queries_once_then_waits_without_polling(mocker):
    owner, database, store, maintain, _ = _owner(mocker)
    await owner.start()
    await _turns()
    assert maintain.await_count == 1
    await owner.start()
    await _turns()
    assert maintain.await_count == 1
    database.read.assert_not_awaited()
    store.return_value.discover_objects.assert_awaited_once_with(None, 64)
    assert store.call_args.args[1] is database.executor
    assert maintain.call_args.kwargs == {"max_batches": 1}
    owner.stop_new_work()
    await owner.settle()


@pytest.mark.asyncio
async def test_commit_during_empty_query_is_not_lost_before_wait(mocker):
    owner, _, _, maintain, _ = _owner(mocker)
    owned = _owned((3, 1, 0))

    async def run(context, *, max_batches):
        if maintain.await_count == 1:
            owner.observe_commit(owned, DbOutcome(DbOutcomeKind.COMPLETED), OperationKey("1" * 32))
        return MaintenanceResult(MaintenanceOutcome.IDLE)

    maintain.side_effect = run
    await owner.start()
    await _turns()
    assert maintain.await_count == 2
    owner.stop_new_work()
    await owner.settle()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [
    MaintenanceResult(MaintenanceOutcome.YIELDED),
    MaintenanceResult(MaintenanceOutcome.DRAINED, saved=(
        SavedProgress(SnapshotRef(1, 21), 64),)),
])
async def test_one_batch_or_remaining_eligible_progress_continues(mocker, result):
    owner, _, _, maintain, _ = _owner(mocker)
    maintain.side_effect = [result, MaintenanceResult(MaintenanceOutcome.IDLE)]
    await owner.start()
    await _turns()
    assert maintain.await_count == 2
    owner.stop_new_work()
    await owner.settle()


@pytest.mark.asyncio
async def test_notifications_during_save_are_retained_and_coalesced(mocker):
    owner, _, _, maintain, _ = _owner(mocker)
    owned = _owned((7, 1, 0))

    async def run(context, *, max_batches):
        if maintain.await_count == 1:
            owner.observe_commit(owned, DbOutcome(DbOutcomeKind.COMPLETED), OperationKey("2" * 32))
            owner.observe_commit(owned, DbOutcome(DbOutcomeKind.COMPLETED), OperationKey("2" * 32))
        return MaintenanceResult(MaintenanceOutcome.DRAINED)

    maintain.side_effect = run
    await owner.start()
    await _turns()
    assert maintain.await_count == 2
    owner.stop_new_work()
    await owner.settle()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [DbOutcomeKind.ROLLED_BACK, DbOutcomeKind.UNKNOWN, DbOutcomeKind.NOT_EXECUTED])
async def test_unconfirmed_write_never_queries_or_wakes(mocker, kind):
    owner, _, _, maintain, _ = _owner(mocker)
    await owner.start()
    await _turns()
    owned = _owned((3, 1, 0))
    owner.observe_commit(owned, DbOutcome(kind), OperationKey("3" * 32))
    await _turns()
    owned.connection.execute.assert_not_called()
    assert maintain.await_count == 1
    owner.stop_new_work()
    await owner.settle()


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [None, (3, 0, 0)])
async def test_no_event_or_below_threshold_does_not_wake(mocker, row):
    owner, _, _, maintain, _ = _owner(mocker)
    await owner.start()
    await _turns()
    owned = _owned(row)
    key = OperationKey("4" * 32)
    owner.observe_commit(owned, DbOutcome(DbOutcomeKind.COMPLETED), key)
    await _turns()
    assert maintain.await_count == 1
    sql, parameters = owned.connection.execute.call_args.args
    assert "operation_key = ?" in sql
    assert "entity_links_event" in sql
    assert parameters[0] == str(key)
    assert parameters[-1] == 64
    owner.stop_new_work()
    await owner.settle()


@pytest.mark.asyncio
async def test_missing_affected_progress_propagates_to_write_observer(mocker):
    owner, _, _, _, _ = _owner(mocker)
    owned = _owned((3, 0, 1))
    with pytest.raises(SnapshotMaintenanceError):
        owner.observe_commit(owned, DbOutcome(DbOutcomeKind.COMPLETED), OperationKey("5" * 32))


@pytest.mark.asyncio
async def test_discover_only_new_plans_and_actions_and_covers_threshold_one(mocker):
    owner, database, store, maintain, _ = _owner(mocker, threshold=1)
    await owner.start()
    await _turns()
    store.return_value.discover_objects.return_value = (14, True)
    await owner.discover()
    await _turns()
    assert maintain.await_count == 2
    store.return_value.discover_objects.assert_awaited_with(12, 1)
    database.read.assert_not_awaited()
    owner.stop_new_work()
    await owner.settle()


@pytest.mark.asyncio
async def test_discovery_with_unchanged_upper_does_not_read_object_progress(mocker):
    owner, _, store, maintain, _ = _owner(mocker)
    await owner.start()
    await _turns()
    await owner.discover()
    await _turns()
    assert maintain.await_count == 1
    owner.stop_new_work()
    await owner.settle()


@pytest.mark.asyncio
async def test_timeout_disables_session_and_notifications_cannot_restart_it(mocker):
    owner, database, store, maintain, disabled = _owner(mocker)
    maintain.return_value = MaintenanceResult(MaintenanceOutcome.DISABLED)
    await owner.start()
    await _turns()
    disabled.assert_called_once()
    owned = _owned((3, 1, 0))
    owner.observe_commit(owned, DbOutcome(DbOutcomeKind.COMPLETED), OperationKey("6" * 32))
    await owner.discover()
    await owner.start()
    await _turns()
    assert maintain.await_count == 1
    database.read.assert_not_awaited()
    store.return_value.discover_objects.assert_awaited_once_with(None, 64)
    owned.connection.execute.assert_not_called()
    await owner.settle()


@pytest.mark.asyncio
async def test_stop_waits_for_late_actual_result_and_preserves_failure(mocker):
    owner, _, _, maintain, _ = _owner(mocker)
    entered = asyncio.Event()
    finish = asyncio.Event()
    error = SnapshotMaintenanceError("实际保存失败")

    async def run(context, *, max_batches):
        entered.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            await finish.wait()
            raise error

    maintain.side_effect = run
    await owner.start()
    await entered.wait()
    owner.stop_new_work()
    settling = asyncio.create_task(owner.settle())
    await _turns()
    assert not settling.done()
    finish.set()
    with pytest.raises(SnapshotMaintenanceError) as caught:
        await settling
    assert caught.value is error
    with pytest.raises(SnapshotMaintenanceError) as caught:
        owner.check_completed()
    assert caught.value is error


@pytest.mark.asyncio
async def test_stop_before_start_never_submits_maintenance(mocker):
    owner, _, store, maintain, _ = _owner(mocker)
    owner.stop_new_work()
    await owner.start()
    await owner.discover()
    await owner.settle()
    store.return_value.discover_objects.assert_not_awaited()
    maintain.assert_not_awaited()


@pytest.mark.asyncio
async def test_actual_maintenance_error_is_observable_without_stop(mocker):
    owner, _, _, maintain, _ = _owner(mocker)
    error = SnapshotMaintenanceError("读取失败")
    maintain.side_effect = error
    await owner.start()
    await _turns()
    with pytest.raises(SnapshotMaintenanceError) as caught:
        owner.check_completed()
    assert caught.value is error
    with pytest.raises(SnapshotMaintenanceError):
        await owner.settle()


@pytest.mark.asyncio
async def test_join_cancellation_preserves_late_failure(mocker):
    owner, _, _, maintain, _ = _owner(mocker)
    entered, finish = asyncio.Event(), asyncio.Event()
    error = SnapshotMaintenanceError("已开始保存的实际错误")

    async def run(context, *, max_batches):
        entered.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            await finish.wait()
            raise error

    maintain.side_effect = run
    await owner.start()
    await entered.wait()
    owner.stop_new_work()
    joining = asyncio.create_task(owner.settle())
    await _turns()
    joining.cancel()
    await _turns()
    assert not joining.done()
    finish.set()
    with pytest.raises(SnapshotMaintenanceError) as caught:
        await joining
    assert caught.value is error
    assert isinstance(error.__cause__, asyncio.CancelledError)


@pytest.mark.asyncio
async def test_join_cancellation_is_rethrown_after_actual_success(mocker):
    owner, _, _, maintain, _ = _owner(mocker)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def run(context, *, max_batches):
        entered.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            await finish.wait()
        return MaintenanceResult(MaintenanceOutcome.CLOSING)

    maintain.side_effect = run
    await owner.start()
    await entered.wait()
    owner.stop_new_work()
    joining = asyncio.create_task(owner.settle())
    await _turns()
    joining.cancel()
    await _turns()
    assert not joining.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await joining


@pytest.mark.asyncio
async def test_default_threshold_new_creation_does_not_wake(mocker):
    owner, _, store, maintain, _ = _owner(mocker)
    await owner.start()
    await _turns()
    store.return_value.discover_objects.return_value = (14, False)
    await owner.discover()
    await _turns()
    assert maintain.await_count == 1
    owner.stop_new_work()
    await owner.settle()


@pytest.mark.asyncio
async def test_discovery_failure_does_not_publish_a_new_upper(mocker):
    owner, _, store, _, _ = _owner(mocker)
    await owner.start()
    await _turns()
    error = SnapshotMaintenanceError("外部对象进度读取失败")
    store.return_value.discover_objects.side_effect = error
    with pytest.raises(SnapshotMaintenanceError) as caught:
        await owner.discover()
    assert caught.value is error
    owner.stop_new_work()
    with pytest.raises(SnapshotMaintenanceError):
        await owner.settle()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["startup", "discover"])
async def test_discovery_queue_timeout_disables_without_session_error(mocker, phase):
    owner, _, store, maintain, disabled = _owner(mocker)
    timeout = SnapshotEnqueueTimeout("资格发现入队超时")
    if phase == "startup":
        store.return_value.discover_objects.side_effect = timeout
    await owner.start()
    await _turns()
    if phase == "discover":
        store.return_value.discover_objects.side_effect = timeout
        await owner.discover()
    await _turns()
    disabled.assert_called_once()
    count = store.return_value.discover_objects.await_count
    await owner.discover()
    await owner.start()
    assert store.return_value.discover_objects.await_count == count
    owner.check_completed()
    owner.stop_new_work()
    await owner.settle()


@pytest.mark.asyncio
async def test_stop_during_startup_discovery_waits_for_actual_failure(mocker):
    owner, _, store, maintain, _ = _owner(mocker)
    entered, finished = asyncio.Event(), asyncio.Event()
    error = SnapshotMaintenanceError("启动查询实际错误")

    async def discover(after, threshold):
        entered.set()
        try:
            await finished.wait()
        except asyncio.CancelledError:
            await finished.wait()
            raise error

    store.return_value.discover_objects.side_effect = discover
    await owner.start()
    await entered.wait()
    owner.stop_new_work()
    joining = asyncio.create_task(owner.settle())
    await _turns()
    assert not joining.done()
    finished.set()
    with pytest.raises(SnapshotMaintenanceError) as caught:
        await joining
    assert caught.value is error
    maintain.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_during_external_discovery_waits_for_actual_failure(mocker):
    owner, _, store, _, _ = _owner(mocker)
    await owner.start()
    await _turns()
    entered, finished = asyncio.Event(), asyncio.Event()
    error = SnapshotMaintenanceError("资格查询实际错误")

    async def discover(after, threshold):
        entered.set()
        try:
            await finished.wait()
        except asyncio.CancelledError:
            await finished.wait()
            raise error

    store.return_value.discover_objects.side_effect = discover
    discovery = asyncio.create_task(owner.discover())
    await entered.wait()
    owner.stop_new_work()
    joining = asyncio.create_task(owner.settle())
    await _turns()
    assert not joining.done()
    finished.set()
    for task in (discovery, joining):
        with pytest.raises(SnapshotMaintenanceError) as caught:
            await task
        assert caught.value is error

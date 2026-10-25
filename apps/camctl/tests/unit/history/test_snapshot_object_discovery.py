"""快照资格发现只读新对象，并使用快照任务的排队及错误语义。"""

import sqlite3
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from camctl.history.snapshots import SnapshotEnqueueTimeout, SnapshotMaintenanceError
from camctl.persistence.executor import DbExecutor
from camctl.persistence.models import DbJobKind
from camctl.persistence.repositories.history import SqliteSnapshotStore
from camctl.persistence.runtime import OwnedConnection


def _store(mocker, rows):
    store = SqliteSnapshotStore(Path("/state.sqlite"), create_autospec(DbExecutor, instance=True))
    owned = create_autospec(OwnedConnection, instance=True)
    owned.connection = create_autospec(sqlite3.Connection, instance=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    cursor.fetchone.side_effect = rows
    owned.connection.execute.return_value = cursor
    dispatch = mocker.patch.object(store, "_run", autospec=True)

    async def run(kind, execute, description):
        assert kind is DbJobKind.READ
        return execute(owned)

    dispatch.side_effect = run
    return store, owned, dispatch


@pytest.mark.asyncio
@pytest.mark.parametrize("start, maximum", [(None, 12), (12, 12), (None, None)])
async def test_start_or_unchanged_upper_only_reads_latest_plan(mocker, start, maximum):
    store, owned, _ = _store(mocker, [(maximum,)])
    assert await store.discover_objects(start, 64) == (maximum or 0, False)
    assert owned.connection.execute.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("eligible", [0, 1])
async def test_new_range_keeps_per_object_threshold_and_both_endpoints(mocker, eligible):
    store, owned, _ = _store(mocker, [(14,), (0, eligible)])
    assert await store.discover_objects(12, 1) == (14, bool(eligible))
    sql, parameters = owned.connection.execute.call_args.args
    assert parameters == (12, 14, 12, 14, 1)
    assert "FROM plans WHERE id > ? AND id <= ?" in sql
    assert "FROM actions WHERE plan_id > ? AND plan_id <= ?" in sql


@pytest.mark.asyncio
async def test_missing_new_object_progress_is_an_error(mocker):
    store, _, _ = _store(mocker, [(14,), (1, 0)])
    with pytest.raises(SnapshotMaintenanceError):
        await store.discover_objects(12, 64)


@pytest.mark.asyncio
@pytest.mark.parametrize("start, threshold", [(-1, 64), (True, 64), ("1", 64), (0, 0), (0, True)])
async def test_invalid_input_fails_before_dispatch(mocker, start, threshold):
    store, _, dispatch = _store(mocker, [])
    with pytest.raises(SnapshotMaintenanceError):
        await store.discover_objects(start, threshold)
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_snapshot_enqueue_timeout_is_not_converted_to_read_failure(mocker):
    store, _, dispatch = _store(mocker, [])
    timeout = SnapshotEnqueueTimeout("入队前超时")
    dispatch.side_effect = timeout
    with pytest.raises(SnapshotEnqueueTimeout) as caught:
        await store.discover_objects(12, 64)
    assert caught.value is timeout

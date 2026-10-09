"""真实自动维护的文件事实和继续位置沿原完整 H 恢复。"""

from pathlib import Path

import pytest

from camctl.bootstrap.work_file_assembly import WorkFileRuntime
from camctl.contracts.history_values import INITIAL_BOUNDARY
from camctl.history.decoding import decode_event_row
from camctl.history.events import branch_of, business_columns, event_type_name
from camctl.history.replay import EntityImage, RestoreSeed, restore
from camctl.history.snapshots import SnapshotRef, decode_snapshot, prepare_snapshot
from camctl.history.validators import ValidatedEvent
from camctl.outputs import work_files
from camctl.outputs.work_files import WorkFileLimits
from camctl.persistence.executor import DbExecutor
from camctl.persistence.repositories.history import HistoryRepository, SqliteSnapshotStore
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import row_facts

from .test_output_binding_changes import environment  # noqa: F401
from .test_work_file_runtime import _history_cleanup

pytestmark = pytest.mark.asyncio


def _business(rows):
    return {key: {column: value for column, value in values.items()
        if column in business_columns(key[0])} for key, values in rows.items()}


def _events(owned, ref, boundary):
    """文件独立拥有自身行，运行位置独立属于 runtime_state。"""
    events = []
    for raw in owned.connection.execute(
        "SELECT id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json"
        " FROM history_events WHERE id<=? ORDER BY id", (boundary.last_event_id,)):
        envelope = decode_event_row(raw)
        owners = {}
        for row in envelope.rows:
            if row.table == "intermediate_files":
                owners[(row.table, row.row_id)] = (10, row.row_id)
            elif row.table == "runtime_state":
                owners[(row.table, row.row_id)] = (8, row.row_id)
        references = tuple(dict.fromkeys(owners.values()))
        if ref in references:
            events.append(ValidatedEvent(envelope, event_type_name(envelope.event_type),
                branch_of(envelope.event_type, envelope.reason)[0], references, owners))
    return events


@pytest.mark.parametrize("first_failed", [False, True])
async def test_work_file_results_and_cursor_match_three_history_paths(environment, monkeypatch, first_failed):
    _cfg, owned, context, _driver = environment
    # runtime_state 由显式部署初始化创建；这里在任何业务历史产生前
    # 取得其初始行。文件仍从自己的 CREATE 事实开始回放。
    runtime_initial = _business({("runtime_state", 1): row_facts(owned.connection, "runtime_state", 1)})
    cfg = await _history_cleanup(environment)
    history = HistoryRepository(Path(cfg.paths.state_db))
    identities = tuple(row[0] for row in owned.connection.execute("SELECT id FROM intermediate_files ORDER BY id"))
    assert len(identities) == 2
    refs = tuple(SnapshotRef(10, identity) for identity in identities)
    executor = DbExecutor(lambda: open_existing(history.path, DbOpenMode.EXISTING_RW, DbConfig()),
        capacity=4, enqueue_timeout_seconds=9)
    try:
        store = SqliteSnapshotStore(history.path, executor)
        seeds = await store.load_entity_images(refs)
        for seed in seeds:
            await store.save_snapshots((prepare_snapshot(seed),))
    finally:
        await executor.close()
    if first_failed:
        def failed(_path):
            raise OSError("historical unlink refused")
        monkeypatch.setattr(work_files, "_remove_work_file", failed)
    first = WorkFileRuntime(staging=Path(cfg.paths.staging), limits=WorkFileLimits(32, 1))
    first.initialize(owned)
    await first.flow(context)
    await first.settle()
    boundary = history.current_boundary()
    assert owned.connection.execute("SELECT cleanup_state FROM intermediate_files ORDER BY id").fetchall() == [
        (5 if first_failed else 4,), (2,)]
    assert owned.connection.execute("SELECT cleanup_cursor_file_id FROM runtime_state").fetchone() == (identities[0],)
    expected_files = {identity: _business({("intermediate_files", identity): row_facts(
        owned.connection, "intermediate_files", identity)}) for identity in identities}
    expected_runtime = _business({("runtime_state", 1): row_facts(owned.connection, "runtime_state", 1)})
    action_business = tuple(owned.connection.execute("SELECT id,status,cancel_requested FROM actions ORDER BY id"))
    monkeypatch.undo()
    second = WorkFileRuntime(staging=Path(cfg.paths.staging), limits=WorkFileLimits(32, 2))
    second.initialize(owned)
    await second.flow(context)
    await second.settle()
    assert owned.connection.execute("SELECT cleanup_state FROM intermediate_files ORDER BY id").fetchall() == [(4,), (4,)]
    assert tuple(owned.connection.execute("SELECT id,status,cancel_requested FROM actions ORDER BY id")) == action_business
    for identity, seed, ref in zip(identities, seeds, refs):
        events = _events(owned, (10, identity), boundary)
        initial = restore(RestoreSeed(EntityImage(10, identity, False, {}, 0, 0), INITIAL_BOUNDARY), events, boundary)
        stored = owned.connection.execute(
            "SELECT content FROM entity_snapshots WHERE entity_type=10 AND entity_id=?", (identity,)).fetchone()
        header, rows = decode_snapshot(stored[0], expect_ref=ref)
        snapshot = restore(RestoreSeed(EntityImage(10, identity, True,
            _business({(row.table, row.row_id): row.values for row in rows}),
            header["boundary_event_id"], header["change_count"]), seed.boundary), events, boundary)
        reverse = history.restore_entity("intermediate_file", identity, boundary, event_batch_size=1)
        assert initial.rows == expected_files[identity]
        assert snapshot.rows == expected_files[identity]
        assert _business(reverse) == expected_files[identity]
        count = owned.connection.execute("SELECT COUNT(*) FROM entity_event_links WHERE entity_type=10 AND entity_id=? AND event_id<=?",
            (identity, boundary.last_event_id)).fetchone()[0]
        assert initial.change_count == snapshot.change_count == count
    runtime_events = _events(owned, (8, 1), boundary)
    initial_cursor = restore(RestoreSeed(EntityImage(8, 1, True, runtime_initial, 0, 0), INITIAL_BOUNDARY), runtime_events, boundary)
    assert initial_cursor.rows == expected_runtime
    assert _business(history.restore_entity("runtime_state", 1, boundary, event_batch_size=1)) == expected_runtime

"""对象恢复固定依据，按相关次数选路径，并在读事务外应用事件。"""

import asyncio
import threading
from contextlib import closing
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio

from camctl.capture.files import FileChecksumSave, FilePresenceSave
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.history.snapshots import SnapshotRef, prepare_snapshot
from camctl.persistence.executor import DbExecutor
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.history import SqliteSnapshotStore
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.repositories.transactions import encode_json_value, row_facts

from ..support.history_rows import observe_readonly_rows
from ..persistence.test_motor_transactions import NOW, owned
from .test_object_closure import _history
from .test_operation_ownership import _create_flow, operation_world
from .test_report_scope import _finish_action, _submit
from .test_complete_history import world as complete_world


@pytest_asyncio.fixture
async def file_world(owned, tmp_path):
    await _submit(owned, tmp_path, "1")
    await _finish_action(owned)
    return SimpleNamespace(owned=owned, history=_history(owned), capture=CaptureRepository())


def _change(world):
    current = row_facts(world.owned.connection, "device_files", 1)["presence_state"]
    outcome = world.capture.save_file_presence(
        FilePresenceSave(1, 3 if current == 2 else 2, NOW),
        new_operation_key(), world.owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    return world.history.current_boundary().last_event_id


def _close_if_needed(value):
    close = getattr(value, "close", None)
    if close is not None:
        close()


async def _load_prepared(store, ref):
    seed = (await store.load_entity_images((ref,)))[0]
    prepared = None
    try:
        prepared = prepare_snapshot(seed)
    finally:
        try:
            _close_if_needed(seed)
        except BaseException:
            if prepared is not None:
                _close_if_needed(prepared)
            raise
    return prepared


async def _snapshot(world):
    snapshot_id, _ = await _save_ref_snapshot(world, SnapshotRef(9, 1))
    return snapshot_id


async def _prepared(world, ref):
    executor = DbExecutor(
        lambda: open_existing(world.history.path, DbOpenMode.EXISTING_RW, DbConfig()),
        capacity=4, enqueue_timeout_seconds=9)
    prepared = None
    try:
        store = SqliteSnapshotStore(world.history.path, executor)
        prepared = await _load_prepared(store, ref)
    finally:
        try:
            await executor.close()
        except BaseException:
            if prepared is not None:
                _close_if_needed(prepared)
            raise
    return prepared


@pytest.mark.asyncio
@pytest.mark.parametrize("path,before,after,has_snapshot", [
    ("current", 2, 0, True), ("exact", 0, 3, True),
    ("no_snapshot", 1, 3, False), ("forward", 1, 4, True),
    ("reverse", 4, 1, True), ("tie", 2, 2, True),
])
async def test_cost_path_reads_only_selected_event_range(file_world, monkeypatch,
                                                        path, before, after, has_snapshot):
    world = file_world
    if has_snapshot:
        await _snapshot(world)
    forward_ids = [_change(world) for _ in range(before)]
    boundary = world.history.current_boundary()
    expected = row_facts(world.owned.connection, "device_files", 1)
    reverse_ids = [_change(world) for _ in range(after)]
    event_bodies = dict(world.owned.connection.execute(
        "SELECT id, body_json FROM history_events"))
    known_bodies = set(event_bodies.values())
    observed = []

    def record(cursor, row):
        names = [column[0] for column in cursor.description]
        body_columns = [index for index, name in enumerate(names)
                        if name == "body_json" or row[index] in known_bodies]
        if body_columns:
            assert len(body_columns) == 1 and names[body_columns[0]] == "body_json"
            id_columns = [index for index, name in enumerate(names) if name == "id"]
            assert len(id_columns) == 1
            observed.append((row[id_columns[0]], row[body_columns[0]]))

    opened = observe_readonly_rows(monkeypatch, world.history.path, record)
    actual = world.history.restore_entity("device_file", 1, boundary, event_batch_size=1)
    if path in ("current", "exact"):
        wanted = []
    elif path == "forward":
        wanted = forward_ids
    else:
        wanted = list(reversed(reverse_ids))
    assert actual == {("device_files", 1): expected}
    assert opened
    assert observed == [(event_id, event_bodies[event_id]) for event_id in wanted]


class _Cursor:
    def __init__(self, owner, inner):
        self.owner, self.inner = owner, inner
        self.owner.pending += 1

    def close(self):
        self.inner.close()
        self.owner.pending -= 1

    def __getattr__(self, name):
        return getattr(self.inner, name)


class _Connection:
    def __init__(self, inner):
        self.inner, self.pending = inner, 0

    def execute(self, *args):
        return _Cursor(self, self.inner.execute(*args))

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _observe_connections(repository, monkeypatch):
    opened = []
    original = repository._connect

    def connect():
        tracked = _Connection(original())
        opened.append(tracked)
        return tracked

    monkeypatch.setattr(repository, "_connect", connect)
    return opened


def test_reverse_application_runs_after_query_and_transaction_end(file_world, monkeypatch):
    world = file_world
    boundary = world.history.current_boundary()
    expected = row_facts(world.owned.connection, "device_files", 1)
    for _ in range(3):
        _change(world)
    connections = _observe_connections(world.history, monkeypatch)
    from camctl.history import replay
    from camctl.persistence.repositories import history as module
    applied = []

    def check():
        assert all(connection.pending == 0 for connection in connections)
        for connection in connections:
            try:
                assert connection.in_transaction is False
            except Exception as error:
                # sqlite3 在已关闭连接上查询 in_transaction 报 ProgrammingError。
                import sqlite3
                if not isinstance(error, sqlite3.ProgrammingError):
                    raise
        applied.append(True)

    original_rows = module.reverse_row_values
    original_image = replay.apply_reverse

    def rows(*args, **kwargs):
        check()
        return original_rows(*args, **kwargs)

    def image(*args, **kwargs):
        check()
        return original_image(*args, **kwargs)

    monkeypatch.setattr(module, "reverse_row_values", rows)
    monkeypatch.setattr(replay, "apply_reverse", image)
    assert world.history.restore_entity("device_file", 1, boundary, event_batch_size=1) == {
        ("device_files", 1): expected}
    assert applied
    assert len(connections) >= 4  # 依据读取和三页事件各自结束读视图。


def test_projection_and_range_keep_c_when_commit_occurs_between_pages(file_world, monkeypatch):
    world = file_world
    boundary = world.history.current_boundary()
    expected = row_facts(world.owned.connection, "device_files", 1)
    fixed_events = [_change(world) for _ in range(3)]
    with closing(world.owned.connection.execute("SELECT * FROM device_files WHERE id = 1")) as cursor:
        projection_columns = tuple(column[0] for column in cursor.description)
        projection = cursor.fetchone()
    event_bodies = dict(world.owned.connection.execute(
        "SELECT id, body_json FROM history_events"))
    known_bodies = set(event_bodies.values())
    committed = []
    observed = []
    views = {}
    projections = []

    from camctl.persistence.repositories.history import HistoryRepository
    world_for_write = SimpleNamespace(
        owned=world.owned, capture=world.capture, history=HistoryRepository(world.history.path))

    def record(cursor, row):
        names = tuple(column[0] for column in cursor.description)
        connection = cursor.connection
        if set(names) == set(projection_columns):
            assert all(names.count(name) == 1 for name in projection_columns)
            projected = tuple(row[names.index(name)] for name in projection_columns)
            assert projected == projection and not projections and not committed
            assert connection not in views
            projections.append(projected)
            views[connection] = "projection"
        body_columns = [index for index, name in enumerate(names)
                        if name == "body_json" or row[index] in known_bodies]
        if body_columns:
            assert len(body_columns) == 1 and names[body_columns[0]] == "body_json"
            assert names.count("id") == 1
            event = (row[names.index("id")], row[body_columns[0]])
            if not observed:
                assert len(committed) == 1 and connection not in views
                views[connection] = "first_event"
            else:
                assert len(committed) == 2
            observed.append(event)

    def end_view(connection):
        marker = views.pop(connection, None)
        if marker == "projection":
            assert not committed
            committed.append(_change(world_for_write))
        elif marker == "first_event":
            assert len(committed) == 1 and len(observed) == 1
            committed.append(_change(world_for_write))

    opened = observe_readonly_rows(monkeypatch, world.history.path, record, end_view)
    assert world.history.restore_entity("device_file", 1, boundary, event_batch_size=1) == {
        ("device_files", 1): expected}
    assert opened and projections == [projection] and not views
    assert len(committed) == 2
    assert observed == [(event_id, event_bodies[event_id]) for event_id in reversed(fixed_events)]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["format", "count", "missing_primary", "duplicate", "foreign"])
async def test_selected_snapshot_damage_is_not_a_cache_miss(file_world, fault):
    world = file_world
    snapshot_id = await _snapshot(world)
    boundary = world.history.current_boundary()
    _change(world)
    connection = world.owned.connection
    if fault == "count":
        connection.execute("UPDATE entity_snapshots SET change_count = change_count + 1 WHERE id = ?",
                           (snapshot_id,))
    else:
        raw = connection.execute("SELECT content FROM entity_snapshots WHERE id = ?",
                                 (snapshot_id,)).fetchone()[0]
        lines = raw.decode().splitlines()
        if fault == "format":
            from camctl.contracts.json_values import parse_exact_json
            header = parse_exact_json(lines[0])
            header["format_version"] = 2
            lines[0] = encode_json_value(header)
        elif fault == "missing_primary":
            lines = lines[:1]
        elif fault == "duplicate":
            lines.append(lines[1])
        else:
            other = row_facts(connection, "actions", 1)
            lines.append(encode_json_value({"table": "actions", "row": other}))
        content = ("\n".join(lines) + "\n").encode()
        connection.execute("UPDATE entity_snapshots SET content = ? WHERE id = ?", (content, snapshot_id))
    with pytest.raises(ConsistencyError):
        world.history.restore_entity("device_file", 1, boundary)


@pytest.mark.asyncio
async def test_new_snapshot_after_selection_cannot_replace_fixed_s_and_id(file_world, monkeypatch):
    world = file_world
    original_id = await _snapshot(world)
    original_s = world.history.current_boundary()
    forward_id = _change(world)
    boundary = world.history.current_boundary()
    expected = row_facts(world.owned.connection, "device_files", 1)
    late = await _prepared(world, SnapshotRef(9, 1))
    executor = None
    restore_task = None
    release = threading.Event()
    try:
        assert late.boundary == boundary
        for _ in range(4):
            _change(world)
        original = world.owned.connection.execute(
            "SELECT entity_type, entity_id, boundary_event_id, change_count, format_version, content"
            " FROM entity_snapshots WHERE id = ?", (original_id,)).fetchone()
        assert original is not None and original[:3] == (9, 1, original_s.last_event_id)
        metadata = (original_id, original_s.last_event_id, original[3], original[4])
        forward_body = world.owned.connection.execute(
            "SELECT body_json FROM history_events WHERE id = ?", (forward_id,)).fetchone()[0]
        loop = asyncio.get_running_loop()
        selected_ready = asyncio.Event()
        late_committed = False
        selected_views = set()
        selected = []
        snapshots = []
        events = []
        columns = ("entity_type", "entity_id", "boundary_event_id", "change_count", "format_version", "content")
        metadata_columns = ("id", "boundary_event_id", "change_count", "format_version")

        def record(cursor, row):
            names = tuple(column[0] for column in cursor.description)
            if set(names) == set(metadata_columns):
                assert all(names.count(name) == 1 for name in metadata_columns)
                selected_metadata = tuple(row[names.index(name)] for name in metadata_columns)
                assert selected_metadata == metadata and not selected
                selected.append(selected_metadata)
                selected_views.add(cursor.connection)
            content_columns = [index for index, name in enumerate(names)
                               if name == "content" or row[index] == original[-1]]
            if content_columns:
                assert late_committed
                assert len(content_columns) == 1 and names[content_columns[0]] == "content"
                assert all(names.count(name) == 1 for name in columns)
                snapshots.append(tuple(row[names.index(name)] for name in columns))
            body_columns = [index for index, name in enumerate(names)
                            if name == "body_json" or row[index] == forward_body]
            if body_columns:
                assert len(body_columns) == 1 and names[body_columns[0]] == "body_json"
                assert names.count("id") == 1
                events.append((row[names.index("id")], row[body_columns[0]]))

        def end_view(connection):
            if connection in selected_views:
                selected_views.remove(connection)
                loop.call_soon_threadsafe(selected_ready.set)
                assert release.wait(30), "等待迟到快照保存超时"

        from camctl.persistence.repositories.history import HistoryRepository
        opened = observe_readonly_rows(monkeypatch, world.history.path, record, end_view)
        restore_task = asyncio.create_task(asyncio.to_thread(
            lambda: HistoryRepository(world.history.path).restore_entity(
                "device_file", 1, boundary, event_batch_size=1)))
        ready_task = asyncio.create_task(selected_ready.wait())
        try:
            done, _ = await asyncio.wait((ready_task, restore_task), timeout=30,
                                         return_when=asyncio.FIRST_COMPLETED)
            if restore_task in done:
                await restore_task
                pytest.fail("恢复在选中快照的读视图结束前完成")
            assert ready_task in done, "等待选中快照的读视图结束超时"
        finally:
            if not ready_task.done():
                ready_task.cancel()
            await asyncio.gather(ready_task, return_exceptions=True)
        executor = DbExecutor(
            lambda: open_existing(world.history.path, DbOpenMode.EXISTING_RW, DbConfig()),
            capacity=4, enqueue_timeout_seconds=9)
        store = SqliteSnapshotStore(world.history.path, executor)
        saved = await store.save_snapshots((late,))
        assert len(saved) == 1 and saved[0].ref == SnapshotRef(9, 1)
        latest = world.owned.connection.execute(
            "SELECT id, boundary_event_id FROM entity_snapshots WHERE entity_type = 9 AND entity_id = 1"
            " AND boundary_event_id = ?", (boundary.last_event_id,)).fetchone()
        assert latest is not None and latest[1] == boundary.last_event_id
        assert latest[0] != original_id and boundary.last_event_id > original_s.last_event_id
        late_committed = True
        release.set()
        actual = await asyncio.shield(restore_task)
        assert opened and selected == [metadata] and not selected_views
        assert snapshots == [original]
        assert events == [(forward_id, forward_body)]
        assert actual == {("device_files", 1): expected}
    finally:
        release.set()
        try:
            if restore_task is not None:
                await asyncio.shield(restore_task)
        finally:
            try:
                if executor is not None:
                    await executor.close()
            finally:
                _close_if_needed(late)


@pytest.mark.asyncio
async def test_selected_snapshot_cannot_borrow_missing_owner_chain_from_c(operation_world):
    world = operation_world
    _create_flow(world, "residual_stop")
    boundary = world.history.current_boundary()
    snapshot_id, saved_boundary = await _save_ref_snapshot(world, SnapshotRef(1, 1))
    assert saved_boundary == boundary
    connection = world.owned.connection
    # 此分支的 STOP_RESIDUAL 明确由 activity.action_id 归属动作1，
    # 只留下停止与尝试但去掉活动，归属依据已无法在 S 完整成立。
    original = connection.execute(
        "SELECT content FROM entity_snapshots WHERE id = ?", (snapshot_id,)
    ).fetchone()[0].decode().splitlines()
    records = [parse_exact_json(line) for line in original]
    kept = [records[0], *(record for record in records[1:]
                          if record["table"] != "device_activities")]
    content = ("\n".join(encode_json_value(record) for record in kept) + "\n").encode()
    connection.execute("UPDATE entity_snapshots SET content = ? WHERE id = ?", (content, snapshot_id))
    _create_flow(world, "emergency_stop")
    assert row_facts(connection, "device_activities", 1) is not None
    with pytest.raises(ConsistencyError):
        world.history.restore_entity("action", 1, boundary)


def _rewrite_snapshot_row(world, snapshot_id, table, identity, changes):
    """只改变派生快照正文；当前投影和权威事件保持真实保存的事实。"""
    connection = world.owned.connection
    content = connection.execute(
        "SELECT content FROM entity_snapshots WHERE id = ?", (snapshot_id,)
    ).fetchone()[0]
    records = [parse_exact_json(line) for line in content.decode().splitlines()]
    matching = [record for record in records[1:]
                if record["table"] == table and record["row"]["id"] == identity]
    assert len(matching) == 1
    matching[0]["row"].update(changes)
    connection.execute("UPDATE entity_snapshots SET content = ? WHERE id = ?",
        (("\n".join(encode_json_value(record) for record in records) + "\n").encode(),
         snapshot_id))


async def _file_snapshot_path(world, tmp_path, path):
    snapshot_id = await _snapshot(world)
    if path == "forward":
        # 这条真实事件只改变摘要能力，保留矩阵中的非法业务列。
        assert row_facts(world.owned.connection, "device_files", 1)["checksum_support"] == 1
        saved = world.capture.save_file_checksum(
            FileChecksumSave(1, 3, NOW), new_operation_key(), world.owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    elif path == "empty_delta":
        # 无关受理使 S<H，但该文件在 (S,H] 的相关次数仍为零。
        await _submit(world.owned, tmp_path, "2")
    boundary = world.history.current_boundary()
    for _ in range(4):
        _change(world)
    return snapshot_id, boundary


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["exact", "forward", "empty_delta"])
@pytest.mark.parametrize("changes", [
    pytest.param({"presence_state": 999}, id="unknown_enum"),
    pytest.param({"presence_state": "2"}, id="integer_as_text"),
    pytest.param({"presence_state": True}, id="integer_as_bool"),
    pytest.param({"presence_state": Decimal("2.5")}, id="fractional_integer"),
    pytest.param({"presence_state": None}, id="required_null"),
    pytest.param({"locator_json": []}, id="json_array_in_object"),
    pytest.param({"original_name": 42}, id="text_as_integer"),
    pytest.param({"size_bytes": 2**63}, id="integer_outside_sql_range"),
])
async def test_snapshot_business_values_reject_invalid_state(file_world, tmp_path,
                                                           path, changes):
    world = file_world
    snapshot_id, boundary = await _file_snapshot_path(world, tmp_path, path)
    _rewrite_snapshot_row(world, snapshot_id, "device_files", 1, changes)
    with pytest.raises(ConsistencyError):
        world.history.restore_entity("device_file", 1, boundary, event_batch_size=1)


@pytest.mark.asyncio
async def test_snapshot_construction_matches_independent_file_history_at_s(file_world):
    from .test_complete_history import _naive_replay_image

    world = file_world
    boundary = world.history.current_boundary()
    expected = _naive_replay_image(world.owned.connection, boundary.last_event_id)[("device_files", 1)]
    assert expected["size_bytes"] is not None
    snapshot_id = await _snapshot(world)

    def snapshot_values():
        content, = world.owned.connection.execute(
            "SELECT content FROM entity_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        rows = [parse_exact_json(line) for line in content.decode().splitlines()]
        return next(row["row"] for row in rows if row.get("table") == "device_files")

    original = snapshot_values()
    assert {key: original[key] for key in expected} == expected
    _rewrite_snapshot_row(world, snapshot_id, "device_files", 1, {"size_bytes": None})
    assert {key: snapshot_values()[key] for key in expected} != expected


@pytest.mark.asyncio
async def test_on_demand_photo_unknown_length_survives_snapshot_read(tmp_path):
    from pathlib import Path
    from camctl.capture.handlers import capture_handler
    from camctl.persistence.repositories.history import HistoryRepository
    from ..capture.test_host_photo_flow import _world

    config, owned, _, runtime, _ = await _world(tmp_path, storage="device")
    history = HistoryRepository(Path(config.paths.state_db))
    executor = DbExecutor(lambda: open_existing(history.path, DbOpenMode.EXISTING_RW, DbConfig()),
                          capacity=4, enqueue_timeout_seconds=9)
    try:
        await capture_handler("camera_take_photo")(1, runtime)
        boundary = history.current_boundary()
        ids = [row[0] for row in owned.connection.execute("SELECT id FROM device_files ORDER BY id")]
        assert len(ids) == 2
        store = SqliteSnapshotStore(history.path, executor)
        images = await store.load_entity_images(tuple(SnapshotRef(9, identity) for identity in ids))
        prepared = [prepare_snapshot(image) for image in images]
        await store.save_snapshots(tuple(prepared))
        for identity in ids:
            saved = CaptureRepository().save_file_presence(
                FilePresenceSave(identity, 3, NOW), new_operation_key(), owned)
            assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
            restored = history.restore_entity("device_file", identity, boundary)
            assert restored[("device_files", identity)]["size_bytes"] is None
    finally:
        await executor.close()
        owned.connection.close()


@pytest.mark.asyncio
async def test_snapshot_invalid_s_is_rejected_before_later_event_overwrites_bad_column(file_world):
    world = file_world
    snapshot_id = await _snapshot(world)
    _change(world)  # 后续合法事件更新与非法快照相同的 presence_state 列。
    boundary = world.history.current_boundary()
    for _ in range(4):
        _change(world)
    _rewrite_snapshot_row(world, snapshot_id, "device_files", 1, {"presence_state": 999})
    with pytest.raises(ConsistencyError):
        world.history.restore_entity("device_file", 1, boundary)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["exact", "forward", "empty_delta"])
async def test_snapshot_business_values_keep_legal_null_json_and_precise_integer(
        file_world, tmp_path, path):
    world = file_world
    fraction = Decimal("0.12345678901234567890123456789")
    saved = world.capture.save_file_presence(
        FilePresenceSave(1, 3, NOW, locator={"fraction": fraction}),
        new_operation_key(), world.owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    snapshot_id, boundary = await _file_snapshot_path(world, tmp_path, path)
    raw = world.owned.connection.execute(
        "SELECT content FROM entity_snapshots WHERE id = ?", (snapshot_id,)
    ).fetchone()[0]
    snapshot_file = next(parse_exact_json(line)["row"]
        for line in raw.decode().splitlines()[1:]
        if parse_exact_json(line)["table"] == "device_files")
    exact_size = Decimal(str(snapshot_file["size_bytes"]) + ".0")
    _rewrite_snapshot_row(world, snapshot_id, "device_files", 1, {"size_bytes": exact_size})
    actual = world.history.restore_entity("device_file", 1, boundary, event_batch_size=1)[
        ("device_files", 1)]
    assert actual["size_bytes"].as_tuple() == exact_size.as_tuple()
    assert actual["locator_json"]["fraction"].as_tuple() == fraction.as_tuple()
    assert actual["original_device_file_id"] is None
    assert actual["pairing_evidence_json"] is None


async def _save_ref_snapshot(world, ref):
    executor = DbExecutor(
        lambda: open_existing(world.history.path, DbOpenMode.EXISTING_RW, DbConfig()),
        capacity=4, enqueue_timeout_seconds=9)
    try:
        store = SqliteSnapshotStore(world.history.path, executor)
        prepared = await _load_prepared(store, ref)
        try:
            content = b"".join(prepared.chunks)
            assert len(content) == prepared.length
            saved = await store.save_snapshots((prepared,))
            assert len(saved) == 1 and saved[0].ref == ref
            assert saved[0].remaining_changes == 0
            row = world.owned.connection.execute(
                "SELECT id, content, change_count FROM entity_snapshots"
                " WHERE entity_type = ? AND entity_id = ? AND boundary_event_id = ?",
                (ref.entity_type, ref.entity_id, prepared.boundary.last_event_id),
            ).fetchone()
            assert row is not None and row[1] == content and row[2] == prepared.change_count
            return row[0], prepared.boundary
        finally:
            _close_if_needed(prepared)
    finally:
        await executor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("table,changes", [
    pytest.param("actions", {"execution_started": 0}, id="running_action_not_started"),
    pytest.param("operation_runs", {"status": 3, "retry_wait_required": 1},
                 id="terminal_operation_has_retry_wait"),
])
async def test_snapshot_action_and_operation_reject_cross_column_state(
        operation_world, table, changes):
    world = operation_world
    run_id, owner = _create_flow(world, "residual_stop")
    snapshot_id, boundary = await _save_ref_snapshot(world, SnapshotRef(1, owner))
    _create_flow(world, "emergency_stop")  # H 后真实增加该动作的历史。
    _rewrite_snapshot_row(world, snapshot_id, table, owner if table == "actions" else run_id,
                          changes)
    with pytest.raises(ConsistencyError):
        world.history.restore_entity("action", owner, boundary)


@pytest.mark.asyncio
async def test_snapshot_delivery_rejects_published_state_without_publication_facts(
        complete_world):
    import sqlite3

    from camctl.operations.attempts import (
        AttemptConfig, AttemptIntent, AttemptTarget, BeginDisposition, OperationKind,
    )
    from camctl.outputs.slots import SlotOutcome, SlotRequest
    from camctl.outputs.batches import BatchOutcome, BatchRequest
    from camctl.persistence.repositories.file_batches import FileBatchRepository, register_file_batch_guards
    from camctl.persistence.repositories.operations import OperationRepository
    from camctl.persistence.repositories.outputs import OutputsRepository

    world = SimpleNamespace(owned=complete_world.owned, history=complete_world.repository)
    delivery_id = complete_world.delivery_id
    delivery = row_facts(world.owned.connection, "deliveries", delivery_id)
    assert delivery["publication_intent_event_id"] is None
    assert delivery["published_event_id"] is None
    snapshot_id, boundary = await _save_ref_snapshot(world, SnapshotRef(2, delivery_id))
    copy_id = world.owned.connection.execute(
        "SELECT id FROM file_copies WHERE delivery_id = ?", (delivery_id,)).fetchone()[0]
    copy = row_facts(world.owned.connection, "file_copies", copy_id)
    from .test_file_history import _finish_repair
    _finish_repair(complete_world.capture, world.owned, complete_world.repair_file)
    register_file_batch_guards()
    batch = FileBatchRepository().acquire(
        BatchRequest(delivery["action_id"], "cam-1", NOW), new_operation_key(), world.owned)
    assert batch.kind is DbOutcomeKind.COMPLETED, batch.error
    assert batch.value is BatchOutcome.GRANTED, batch.value
    slot = OutputsRepository().grant_read_slot(
        SlotRequest(copy_id, NOW), new_operation_key(), world.owned)
    assert slot.kind is DbOutcomeKind.COMPLETED, slot.error
    assert slot.value.outcome is SlotOutcome.GRANTED, slot.value
    begun = OperationRepository().begin_attempt(AttemptIntent(
        "read", delivery["action_id"], OperationKind.READ_FILE, AttemptTarget(copy_id=copy_id),
        None, AttemptConfig(3, Decimal("5"), Decimal("1")), NOW, copy_round=copy["round"]),
        new_operation_key(), world.owned)
    assert begun.kind is DbOutcomeKind.COMPLETED, begun.error
    assert begun.value.disposition is BeginDisposition.GRANTED, begun.value
    current_delivery = row_facts(world.owned.connection, "deliveries", delivery_id)
    assert current_delivery["change_count"] > delivery["change_count"]
    current = world.history.current_boundary()
    assert current.last_event_id > boundary.last_event_id
    restored = world.history.restore_entity("delivery", delivery_id, boundary)
    assert restored[("deliveries", delivery_id)] == delivery
    _rewrite_snapshot_row(world, snapshot_id, "deliveries", delivery_id, {"status": 5})
    with pytest.raises(ConsistencyError) as caught:
        world.history.restore_entity("delivery", delivery_id, boundary)
    error = caught.value
    while error is not None and not isinstance(error, sqlite3.IntegrityError):
        error = error.__cause__
    assert error is not None and error.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_CHECK
    assert world.history.current_boundary() == current
    assert row_facts(world.owned.connection, "deliveries", delivery_id) == current_delivery


def _track_validation_connections(monkeypatch, *, authorizer=None):
    """跟踪真实内存连接的生命周期，其他真实状态库连接保持原接口。"""
    import sqlite3
    from camctl.persistence import row_validation as module

    created = []
    real_connect = sqlite3.connect

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def close(self):
            try:
                super().close()
            finally:
                self.closed = True

    def connect(database, *args, **kwargs):
        if database == ":memory:":
            kwargs["factory"] = TrackedConnection
            connection = real_connect(database, *args, **kwargs)
            if authorizer is not None:
                connection.set_authorizer(authorizer)
            created.append(connection)
            return connection
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(module.sqlite3, "connect", connect)
    return created


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [False, True], ids=["success", "invalid_row"])
async def test_snapshot_validation_reuses_schema_and_closes_connection(
        file_world, tmp_path, monkeypatch, invalid):
    import sqlite3

    world = file_world
    snapshot_id, boundary = await _file_snapshot_path(world, tmp_path, "exact")
    if invalid:
        _rewrite_snapshot_row(world, snapshot_id, "device_files", 1, {"presence_state": 999})
    created = _track_validation_connections(monkeypatch)
    if invalid:
        with pytest.raises(ConsistencyError):
            world.history.restore_entity("device_file", 1, boundary)
    else:
        assert world.history.restore_entity("device_file", 1, boundary)
    assert len(created) == 1
    assert created[0].closed
    with pytest.raises(sqlite3.ProgrammingError):
        created[0].execute("SELECT 1")


@pytest.mark.asyncio
async def test_projection_row_validation_rolls_back_and_measures_1000_rows(
        file_world, monkeypatch):
    from time import perf_counter
    import tracemalloc
    from camctl.history.events import load_event_registry
    from camctl.persistence.row_validation import (
        ProjectionRowValidationError, ProjectionRowValidator,
    )

    values = row_facts(file_world.owned.connection, "device_files", 1)
    original = dict(values)
    created = _track_validation_connections(monkeypatch)
    tracemalloc.start()
    started = perf_counter()
    try:
        with ProjectionRowValidator() as validator:
            validator.validate("device_files", values)
            validator.validate("device_files", values)  # 相同主键不积累，不能触发唯一约束。
            with pytest.raises(ProjectionRowValidationError):
                validator.validate("device_files", {**values, "presence_state": 999})
            validator.validate("device_files", values)  # 拒绝非法行之后仍可校验合法行。
            for identity in range(1, 1001):
                if identity == values["id"]:
                    validator.validate("device_files", values)
                else:
                    validator.validate("device_files", {
                        **values, "id": identity, "identity_key": f"validation-{identity}",
                    })
            assert len(created) == 1
            for table in load_event_registry()["tables"]:
                assert created[0].execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        elapsed = perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert values == original
    assert created[0].closed
    print(f"1000 行完整值校验：{elapsed:.6f} 秒，Python 跟踪峰值 {peak} 字节")


def test_projection_row_validation_closes_connection_after_schema_error(monkeypatch):
    import sqlite3
    from camctl.persistence import row_validation as module

    denied = []

    def deny_schema_creation(action, table, _column, _database, _origin):
        if action == sqlite3.SQLITE_CREATE_TABLE:
            denied.append(table)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    created = _track_validation_connections(monkeypatch, authorizer=deny_schema_creation)
    with pytest.raises(module.ProjectionRowValidationError) as caught:
        with module.ProjectionRowValidator():
            pytest.fail("结构创建被拒绝时不应进入校验范围")
    assert denied, "SQLite 没有实际尝试创建校验结构"
    assert isinstance(caught.value.__cause__, sqlite3.DatabaseError)
    assert len(created) == 1
    assert created[0].closed
    with pytest.raises(sqlite3.ProgrammingError):
        created[0].execute("SELECT 1")


@pytest_asyncio.fixture
async def two_item_world(owned, tmp_path):
    """真实单张完成文件登记两个产物，真实取回选择固定两个目标项。"""
    import time
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import CommandMode, PlanDisposition, ProcessInput
    from camctl.capture.dispatch import dispatch_ready, ready_capture_actions
    from camctl.capture.results import FileKind
    from camctl.outputs.sources import SelectionMode, SourceSpec, select_outputs
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from camctl.persistence.repositories.outputs import (
        FixSelection, ObtainStartDisposition, OutputsRepository, ResolveSources, StartObtainAction,
        load_selection_facts,
    )
    from camctl.persistence.repositories.scheduling import SchedulingRepository, StartActionRequest
    from .test_report_scope import _Catalog, _runtime
    from ..capture.test_capture_contract import ResultsDouble, _entry

    class ObtainCatalog(_Catalog):
        def action_types(self):
            return super().action_types() | {"obtain_action_outputs"}

    await _submit(owned, tmp_path, "1")
    now = int(time.time() * 1_000_000)
    started = SchedulingRepository().start_action(
        StartActionRequest(1, now, now), new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    async with _runtime(owned) as runtime:
        runtime.results = ResultsDouble({1: (
            _entry("shot-1", kind=FileKind.PHOTO), _entry("shot-2", kind=FileKind.PHOTO),
        )})
        await dispatch_ready(runtime, ready_capture_actions(owned.connection, now))
    assert row_facts(owned.connection, "actions", 1)["status"] == 3
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM outputs WHERE source_action_id = 1").fetchone()[0] == 2
    body = {
        "request_id": "2", "created_at": "2026-10-08 08:00:00", "name": "取回",
        "actions": [{"name": "取回", "type": "obtain_action_outputs",
                     "scheduled_at": "2026-10-08 09:00:00", "params": {
            "source": {"action_instance_id": "1"}, "purpose": "manual",
        }}],
    }
    admitted = AcceptanceRepository().process_input(
        ProcessInput(ParsedInput("obtain.json", body), ObtainCatalog(), CommandMode.RUN, now),
        new_operation_key(), owned)
    assert admitted.kind is DbOutcomeKind.COMPLETED, admitted.error
    diagnostic = (row_facts(owned.connection, "plan_file_diagnostics", admitted.value.diagnostic_id)
                  if admitted.value.diagnostic_id is not None else None)
    assert admitted.value.plan_disposition is PlanDisposition.REGISTERED, diagnostic
    outputs = OutputsRepository()
    started = outputs.start_obtain_action(StartObtainAction(2, now), new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    assert started.value.disposition is ObtainStartDisposition.SAVED, started.value
    resolved = outputs.resolve_sources(
        ResolveSources(2, SourceSpec(action_instance_id=1), now), new_operation_key(), owned)
    assert resolved.kind is DbOutcomeKind.COMPLETED, resolved.error
    assert resolved.value.fixed and resolved.value.member_count == 1, resolved.value
    assert resolved.value.selection_count == 1, resolved.value
    selection_id = owned.connection.execute(
        "SELECT s.id FROM obtain_source_selections s JOIN action_dependencies d ON d.id=s.dependency_id WHERE d.action_id=2").fetchone()[0]
    from camctl.outputs.sources import ResolutionState, SourceResolution
    resolution = SourceResolution(ResolutionState.FIXED, member_action_ids=(1,), source_plan_id=1)
    selected = select_outputs(resolution, load_selection_facts(owned.connection, 1),
                              SelectionMode.DEFAULT)
    fixed = outputs.fix_selection(FixSelection(selection_id, selected, now),
                                  new_operation_key(), owned)
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    item_ids = [row[0] for row in owned.connection.execute(
        "SELECT id FROM obtain_items WHERE selection_id = ? ORDER BY id", (selection_id,))]
    assert len(item_ids) == 2
    return SimpleNamespace(owned=owned, history=_history(owned), outputs=outputs,
                           item_ids=item_ids, now=now)


def _grant_item(world, item_id):
    from camctl.outputs.qualification import FileCandidate, OperationConfig, QualificationOutcome

    item = row_facts(world.owned.connection, "obtain_items", item_id)
    output = row_facts(world.owned.connection, "outputs", item["output_id"])
    granted = world.outputs.grant_file(FileCandidate(
        action_id=2, item_id=item_id, processing_id=None, output_id=output["id"],
        source_device_file_id=output["device_file_id"], target_extension="part",
        delivery_extension="jpg", delivery_display_name="照片",
        config=OperationConfig(3, Decimal("10"), Decimal("0")), occurred_at=world.now),
        new_operation_key(), world.owned)
    assert granted.kind is DbOutcomeKind.COMPLETED, granted.error
    assert granted.value.outcome is QualificationOutcome.GRANTED, granted.value
    return granted.value.delivery_id


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate", ["output", "delivery"])
async def test_snapshot_rejects_duplicate_mutable_unique_key(two_item_world, duplicate):
    import sqlite3

    world = two_item_world
    first, second = world.item_ids
    if duplicate == "delivery":
        delivery_id = _grant_item(world, first)
    snapshot_id, boundary = await _save_ref_snapshot(world, SnapshotRef(1, 2))
    originals = {identity: row_facts(world.owned.connection, "obtain_items", identity)
                 for identity in world.item_ids}
    changes = ({"output_id": originals[first]["output_id"]} if duplicate == "output" else {
        "status": 3, "source_dependency": 1, "delivery_id": delivery_id,
    })
    _grant_item(world, first if duplicate == "output" else second)
    assert row_facts(world.owned.connection, "actions", 2)["last_event_id"] > boundary.last_event_id
    current = world.history.current_boundary()
    assert current.last_event_id > boundary.last_event_id
    current_items = {identity: row_facts(world.owned.connection, "obtain_items", identity)
                     for identity in world.item_ids}
    restored = world.history.restore_entity("action", 2, boundary)
    assert {identity: restored[("obtain_items", identity)] for identity in world.item_ids} == originals
    _rewrite_snapshot_row(world, snapshot_id, "obtain_items", second, changes)
    with pytest.raises(ConsistencyError) as caught:
        world.history.restore_entity("action", 2, boundary)
    error = caught.value
    while error is not None and not isinstance(error, sqlite3.IntegrityError):
        error = error.__cause__
    assert error is not None and error.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_UNIQUE
    assert world.history.current_boundary() == current
    assert {identity: row_facts(world.owned.connection, "obtain_items", identity)
            for identity in world.item_ids} == current_items


@pytest.mark.asyncio
async def test_snapshot_allows_repeated_null_unique_keys(two_item_world, monkeypatch):
    world = two_item_world
    originals = {identity: row_facts(world.owned.connection, "obtain_items", identity)
                 for identity in world.item_ids}
    assert all(row["delivery_id"] is None for row in originals.values())
    snapshot_id, boundary = await _save_ref_snapshot(world, SnapshotRef(1, 2))
    snapshot = world.owned.connection.execute(
        "SELECT entity_type, entity_id, boundary_event_id, change_count, format_version, content"
        " FROM entity_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
    assert snapshot is not None and snapshot[:3] == (1, 2, boundary.last_event_id)
    _grant_item(world, world.item_ids[0])
    assert row_facts(world.owned.connection, "actions", 2)["last_event_id"] > boundary.last_event_id
    assert world.history.current_boundary().last_event_id > boundary.last_event_id
    selected = []
    columns = ("entity_type", "entity_id", "boundary_event_id", "change_count", "format_version", "content")

    def record(cursor, row):
        names = [column[0] for column in cursor.description]
        content_columns = [index for index, name in enumerate(names)
                           if name == "content" or row[index] == snapshot[-1]]
        if content_columns:
            assert len(content_columns) == 1 and names[content_columns[0]] == "content"
            assert all(names.count(name) == 1 for name in columns)
            selected.append(tuple(row[names.index(name)] for name in columns))

    opened = observe_readonly_rows(monkeypatch, world.history.path, record)
    restored = world.history.restore_entity("action", 2, boundary)
    # (entity_type, entity_id, boundary_event_id) 在快照表中唯一。
    assert opened and selected == [snapshot]
    assert {identity: restored[("obtain_items", identity)] for identity in world.item_ids} == originals
    assert all(restored[("obtain_items", identity)]["delivery_id"] is None
               for identity in world.item_ids)


@pytest.mark.parametrize("statuses,rejected", [
    pytest.param((2, 2), False, id="neither_applies"),
    pytest.param((3, 2), False, id="only_first_applies"),
    pytest.param((2, 3), False, id="only_second_applies"),
    pytest.param((3, 3), True, id="both_apply"),
])
def test_projection_row_validation_partial_unique_predicate(monkeypatch, statuses, rejected):
    """部分索引条件由真实SQLite判断；这里验证校验器自身的行集合边界。"""
    from camctl.persistence.row_validation import ProjectionRowValidationError, ProjectionRowValidator

    connections = _track_validation_connections(monkeypatch)
    with ProjectionRowValidator() as validator:
        assert len(connections) == 1
        connection = connections[0]
        # 完整列名来自真实结构，仅显式指定本状态分区必要的业务值。
        metadata = connection.execute("PRAGMA table_xinfo(cleanup_items)").fetchall()
        empty = {row[1]: None for row in metadata}
        first = {**empty, "id": 1, "action_id": 1, "requested_output_id": 42,
                 "output_id": 42, "status": statuses[0], "restriction_state": 2}
        second = {**first, "id": 2, "action_id": 2, "status": statuses[1]}
        validator.validate("cleanup_items", first)
        if rejected:
            with pytest.raises(ProjectionRowValidationError):
                validator.validate("cleanup_items", second)
            # 第二行曾通过普通复合键，部分键冲突不能留下前一索引。
            corrected = {**second, "requested_output_id": 43, "output_id": 43}
            validator.validate("cleanup_items", corrected)
            validator.validate("cleanup_items", corrected)  # 同物理行、相同键可以重验。
            conflicting = {**first, "id": 3, "action_id": 3}
            with pytest.raises(ProjectionRowValidationError):
                validator.validate("cleanup_items", conflicting)
        else:
            validator.validate("cleanup_items", second)

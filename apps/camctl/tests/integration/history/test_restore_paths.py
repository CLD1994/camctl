"""对象恢复固定依据，按相关次数选路径，并在读事务外应用事件。"""

from contextlib import contextmanager
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
from camctl.persistence.transaction import encode_json_value, row_facts

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


async def _snapshot(world):
    executor = DbExecutor(
        lambda: open_existing(world.history.path, DbOpenMode.EXISTING_RW, DbConfig()),
        capacity=4, enqueue_timeout_seconds=9)
    try:
        store = SqliteSnapshotStore(world.history.path, executor)
        seed = (await store.load_entity_images((SnapshotRef(9, 1),)))[0]
        await store.save_snapshots((prepare_snapshot(seed),))
    finally:
        await executor.close()
    return world.owned.connection.execute(
        "SELECT id FROM entity_snapshots WHERE entity_type = 9 AND entity_id = 1"
        " ORDER BY boundary_event_id DESC LIMIT 1").fetchone()[0]


async def _prepared(world, ref):
    executor = DbExecutor(
        lambda: open_existing(world.history.path, DbOpenMode.EXISTING_RW, DbConfig()),
        capacity=4, enqueue_timeout_seconds=9)
    try:
        store = SqliteSnapshotStore(world.history.path, executor)
        seed = (await store.load_entity_images((ref,)))[0]
        return prepare_snapshot(seed)
    finally:
        await executor.close()


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
    observed = []
    from camctl.persistence.repositories import history as module
    original = module.decode_event_row

    def record(raw):
        observed.append(raw[0])
        return original(raw)

    monkeypatch.setattr(module, "decode_event_row", record)
    actual = world.history.restore_entity("device_file", 1, boundary, event_batch_size=1)
    if path in ("current", "exact"):
        wanted = []
    elif path == "forward":
        wanted = forward_ids
    else:
        wanted = list(reversed(reverse_ids))
    assert actual == {("device_files", 1): expected}
    assert observed == wanted


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
    original_read = world.history._read_connection
    read_count = 0
    committed = []

    # _change 取得边界使用另一个仓储，避免测试的同步点递归。
    from camctl.persistence.repositories.history import HistoryRepository
    world_for_write = SimpleNamespace(
        owned=world.owned, capture=world.capture, history=HistoryRepository(world.history.path))

    @contextmanager
    def fixed_interleave():
        nonlocal read_count
        with original_read() as connection:
            yield connection
        read_count += 1
        if read_count in (1, 2):
            committed.append(_change(world_for_write))

    monkeypatch.setattr(world.history, "_read_connection", fixed_interleave)
    from camctl.persistence.repositories import history as module
    original_decode = module.decode_event_row
    read_ids = []

    def record(raw):
        read_ids.append(raw[0])
        return original_decode(raw)

    monkeypatch.setattr(module, "decode_event_row", record)
    assert world.history.restore_entity("device_file", 1, boundary, event_batch_size=1) == {
        ("device_files", 1): expected}
    assert len(committed) == 2
    assert read_ids == list(reversed(fixed_events))


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
    for _ in range(4):
        _change(world)
    from camctl.persistence.repositories import history as module
    original_read = world.history._read_connection
    read_count = 0
    selected = []
    events = []
    original_decode, original_snapshot = module.decode_event_row, world.history._snapshot_rows

    @contextmanager
    def publish_after_selection():
        nonlocal read_count
        with original_read() as connection:
            yield connection
        read_count += 1
        if read_count == 1:
            connection = world.owned.connection
            connection.execute("BEGIN IMMEDIATE")
            module._save_one_snapshot(connection, late)
            connection.execute("COMMIT")

    def observe_selected(seed):
        selected.append((seed.snapshot.snapshot_id, seed.snapshot.boundary))
        return original_snapshot(seed)

    def decode(raw):
        events.append(raw[0])
        return original_decode(raw)

    monkeypatch.setattr(world.history, "_read_connection", publish_after_selection)
    monkeypatch.setattr(world.history, "_snapshot_rows", observe_selected)
    monkeypatch.setattr(module, "decode_event_row", decode)
    actual = world.history.restore_entity("device_file", 1, boundary, event_batch_size=1)
    latest_id = world.owned.connection.execute(
        "SELECT id FROM entity_snapshots WHERE entity_type = 9 AND entity_id = 1"
        " ORDER BY boundary_event_id DESC LIMIT 1").fetchone()[0]
    assert latest_id != original_id
    assert selected == [(original_id, original_s)]
    assert events == [forward_id]
    assert actual == {("device_files", 1): expected}


@pytest.mark.asyncio
async def test_selected_snapshot_cannot_borrow_missing_owner_chain_from_c(operation_world):
    world = operation_world
    _create_flow(world, "residual_stop")
    boundary = world.history.current_boundary()
    prepared = await _prepared(world, SnapshotRef(1, 1))
    from camctl.contracts.json_values import parse_exact_json
    from camctl.persistence.repositories import history as module
    connection = world.owned.connection
    connection.execute("BEGIN IMMEDIATE")
    module._save_one_snapshot(connection, prepared)
    connection.execute("COMMIT")
    # 此分支的 STOP_RESIDUAL 明确由 activity.action_id 归属动作1，
    # 只留下停止与尝试但去掉活动，归属依据已无法在 S 完整成立。
    original = b"".join(prepared.chunks).decode().splitlines()
    records = [parse_exact_json(line) for line in original]
    kept = [records[0], *(record for record in records[1:]
                          if record["table"] != "device_activities")]
    snapshot_id = connection.execute(
        "SELECT id FROM entity_snapshots WHERE entity_type = 1 AND entity_id = 1").fetchone()[0]
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
    pytest.param({"size_bytes": None}, id="complete_without_length"),
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
    prepared = await _prepared(world, ref)
    from camctl.persistence.repositories import history as module
    connection = world.owned.connection
    connection.execute("BEGIN IMMEDIATE")
    try:
        module._save_one_snapshot(connection, prepared)
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    snapshot_id = connection.execute(
        "SELECT id FROM entity_snapshots WHERE entity_type = ? AND entity_id = ?"
        " AND boundary_event_id = ?", (ref.entity_type, ref.entity_id,
                                       prepared.boundary.last_event_id)).fetchone()[0]
    return snapshot_id, prepared.boundary


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
        complete_world, monkeypatch):
    from camctl.operations.attempts import (
        AttemptConfig, AttemptIntent, AttemptTarget, BeginDisposition, OperationKind,
    )
    from camctl.outputs.slots import SlotOutcome, SlotRequest
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
    assert row_facts(world.owned.connection, "deliveries", delivery_id)["change_count"] > delivery["change_count"]
    _rewrite_snapshot_row(world, snapshot_id, "deliveries", delivery_id, {"status": 5})
    selected = []
    original = world.history._snapshot_rows

    def read_selected(seed):
        selected.append(seed.snapshot.snapshot_id)
        return original(seed)

    monkeypatch.setattr(world.history, "_snapshot_rows", read_selected)
    with pytest.raises(ConsistencyError):
        world.history.restore_entity("delivery", delivery_id, boundary)
    assert selected == [snapshot_id]


def _track_validation_connections(monkeypatch):
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
    from camctl.persistence import row_validation as module

    created = _track_validation_connections(monkeypatch)
    monkeypatch.setattr(module, "_schema_scripts", lambda: ("INVALID SQL",))
    with pytest.raises(module.ProjectionRowValidationError):
        with module.ProjectionRowValidator():
            pytest.fail("无效结构不应进入校验范围")
    assert len(created) == 1
    assert created[0].closed


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
    runtime = _runtime(owned)
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
    assert resolved.value.fixed and resolved.value.member_action_ids == (1,), resolved.value
    assert len(resolved.value.selection_ids) == 1, resolved.value
    selection_id = resolved.value.selection_ids[0]
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
async def test_snapshot_rejects_duplicate_mutable_unique_key(two_item_world, monkeypatch, duplicate):
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
    _rewrite_snapshot_row(world, snapshot_id, "obtain_items", second, changes)
    _grant_item(world, first if duplicate == "output" else second)
    assert row_facts(world.owned.connection, "actions", 2)["last_event_id"] > boundary.last_event_id
    selected = []
    original = world.history._snapshot_rows

    def read_selected(seed):
        selected.append(seed.snapshot.snapshot_id)
        return original(seed)

    monkeypatch.setattr(world.history, "_snapshot_rows", read_selected)
    with pytest.raises(ConsistencyError):
        world.history.restore_entity("action", 2, boundary)
    assert selected == [snapshot_id]


@pytest.mark.asyncio
async def test_snapshot_allows_repeated_null_unique_keys(two_item_world, monkeypatch):
    world = two_item_world
    assert all(row_facts(world.owned.connection, "obtain_items", identity)["delivery_id"] is None
               for identity in world.item_ids)
    snapshot_id, boundary = await _save_ref_snapshot(world, SnapshotRef(1, 2))
    _grant_item(world, world.item_ids[0])
    selected = []
    original = world.history._snapshot_rows

    def read_selected(seed):
        selected.append(seed.snapshot.snapshot_id)
        return original(seed)

    monkeypatch.setattr(world.history, "_snapshot_rows", read_selected)
    restored = world.history.restore_entity("action", 2, boundary)
    assert selected == [snapshot_id]
    assert all(restored[("obtain_items", identity)]["delivery_id"] is None
               for identity in world.item_ids)


@pytest.mark.parametrize("statuses,rejected", [
    pytest.param((2, 2), False, id="neither_applies"),
    pytest.param((3, 2), False, id="only_first_applies"),
    pytest.param((2, 3), False, id="only_second_applies"),
    pytest.param((3, 3), True, id="both_apply"),
])
def test_projection_row_validation_partial_unique_predicate(statuses, rejected):
    """部分索引条件由真实SQLite判断；这里验证校验器自身的行集合边界。"""
    from camctl.persistence.row_validation import ProjectionRowValidationError, ProjectionRowValidator

    with ProjectionRowValidator() as validator:
        # 完整列名来自真实结构，仅显式指定本状态分区必要的业务值。
        metadata = validator._connection.execute("PRAGMA table_xinfo(cleanup_items)").fetchall()
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
            caches = validator._connection.execute(
                "SELECT name FROM sqlite_temp_master WHERE type = 'table'").fetchall()
            assert caches
            for (cache,) in caches:
                assert validator._connection.execute(f"SELECT COUNT(*) FROM {cache}").fetchone()[0] == 2
        else:
            validator.validate("cleanup_items", second)

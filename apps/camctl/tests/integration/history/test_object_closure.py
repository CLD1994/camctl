"""完整 H 和取消对象的唯一自身归属，使用真实生产保存入口。"""

from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.cancellation.models import (
    FixCancelTargets, ResolvedTargets, StartCancelAction, TargetFacts,
)
from camctl.cancellation.targets import prepare_cancel_set
from camctl.contracts.history_values import HistoryBoundary, INITIAL_BOUNDARY
from camctl.contracts.public_projection import ProjectionInput, project_public
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.history.decoding import decode_event_row
from camctl.history.events import branch_of, business_columns, event_type_name
from camctl.history.replay import (
    EntityImage, RestoreSeed, apply_reverse, restore,
)
from camctl.history.snapshots import SnapshotRef, decode_snapshot, prepare_snapshot
from camctl.history.validators import ValidatedEvent
from camctl.history.queries import PlanSubtree
from camctl.motor.models import FinishSendRequest, MotorFinalKind, PrepareSendRequest
from camctl.persistence.executor import DbExecutor
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.cancellation import (
    CancellationRepository, register_cancellation_guards,
)
from camctl.persistence.repositories.history import HistoryRepository, SqliteSnapshotStore
from camctl.persistence.repositories.motor import MotorRepository, register_motor_guards
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import row_facts
from camctl.reporting.generation import _plan_facts

from ..persistence.test_motor_transactions import NOW, _admit, _body, owned

register_cancellation_guards()
register_motor_guards()
register_outputs_guards()


def _history(owned):
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    return HistoryRepository(path)


@pytest.fixture
def admitted(owned):
    _admit(owned)
    body = _body()
    body["request_id"] = "102"
    _admit(owned, body)
    return owned, _history(owned)


@pytest.mark.parametrize("target_kind", ["wrong_transaction", "middle_event"])
@pytest.mark.parametrize("entity", ["plan", "action"])
def test_object_boundary_rejects_incomplete_or_mismatched_h(admitted, target_kind, entity):
    owned, history = admitted
    current = history.current_boundary()
    if target_kind == "wrong_transaction":
        target = HistoryBoundary(1, current.last_event_id)
    else:
        first = owned.connection.execute(
            "SELECT first_event_id FROM history_transactions WHERE id = ?",
            (current.txn_id,),
        ).fetchone()[0]
        assert first < current.last_event_id
        target = HistoryBoundary(current.txn_id, first)
    with pytest.raises(ConsistencyError):
        history.restore_entity(entity, 2, target)


def test_object_boundary_checks_h_even_when_recovery_is_needed(admitted):
    owned, history = admitted
    admitted_boundary = history.current_boundary()
    target = HistoryBoundary(1, admitted_boundary.last_event_id)
    started = MotorRepository().prepare_send(
        PrepareSendRequest(2, NOW, NOW), new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    with pytest.raises(ConsistencyError):
        history.restore_entity("action", 2, target, event_batch_size=1)


def test_object_boundary_rejects_nonexistent_transaction(admitted):
    _, history = admitted
    current = history.current_boundary()
    with pytest.raises(ConsistencyError):
        history.restore_entity(
            "plan", 2, HistoryBoundary(current.txn_id + 1, current.last_event_id))


def test_object_boundary_initial_state_excludes_later_objects(admitted):
    _, history = admitted
    assert history.restore_entity("action", 2, INITIAL_BOUNDARY) == {}


@pytest.mark.parametrize("column", ["last_event_id", "change_count"])
def test_object_h_metadata_comes_from_own_directory(admitted, column):
    owned, history = admitted
    motor = MotorRepository()
    prepared = motor.prepare_send(
        PrepareSendRequest(2, NOW, NOW), new_operation_key(), owned)
    assert prepared.kind is DbOutcomeKind.COMPLETED, prepared.error
    unrelated = _body()
    unrelated["request_id"] = "103"
    _admit(owned, unrelated)
    boundary = history.current_boundary()
    expected = row_facts(owned.connection, "actions", 2)[column]
    directory = owned.connection.execute(
        "SELECT event_id, change_count FROM entity_event_links"
        " WHERE entity_type = 1 AND entity_id = 2 AND event_id <= ?"
        " ORDER BY event_id DESC LIMIT 1", (boundary.last_event_id,)).fetchone()
    assert expected == directory[0 if column == "last_event_id" else 1]
    if column == "last_event_id":
        assert expected < boundary.last_event_id - 1  # H 末位属于无关对象。
    finished = motor.finish_send(
        FinishSendRequest(2, NOW + 1, MotorFinalKind.WRITTEN,
                          prepared.value.permit, written_bytes=78),
        new_operation_key(), owned)
    assert finished.kind is DbOutcomeKind.COMPLETED, finished.error
    assert row_facts(owned.connection, "actions", 2)[column] != expected
    actual = history.restore_entity("action", 2, boundary, event_batch_size=1)
    assert actual[("actions", 2)][column] == expected


@pytest.fixture
def cancellation_world(owned):
    _admit(owned)
    _admit(owned, {
        "request_id": "102", "created_at": "2026-10-08 08:00:00",
        "name": "取消", "actions": [{
            "name": "取消", "type": "cancel_task",
            "params": {"target": {"action_instance_id": "1"}},
        }],
    })
    cancellation = CancellationRepository()
    started = cancellation.start_cancel_action(
        StartCancelAction(2, NOW + 1), new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    history = _history(owned)
    return SimpleNamespace(
        owned=owned, history=history, cancellation=cancellation,
        motor=MotorRepository(), before_members=history.current_boundary(),
    )


def _fix_members(world):
    fixed = prepare_cancel_set(
        2, ResolvedTargets(direct=(TargetFacts(1, False, True),)))
    saved = world.cancellation.fix_cancel_targets(
        FixCancelTargets(2, fixed, NOW + 2), new_operation_key(), world.owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    return world.history.current_boundary()


def _finish_target(world):
    prepared = world.motor.prepare_send(
        PrepareSendRequest(1, NOW + 3, NOW + 3),
        new_operation_key(), world.owned)
    assert prepared.kind is DbOutcomeKind.COMPLETED, prepared.error
    finished = world.motor.finish_send(
        FinishSendRequest(1, NOW + 4, MotorFinalKind.WRITTEN,
                          prepared.value.permit, written_bytes=78),
        new_operation_key(), world.owned)
    assert finished.kind is DbOutcomeKind.COMPLETED, finished.error


def _business_rows(rows):
    return {
        key: {column: value for column, value in values.items()
              if column in business_columns(key[0])}
        for key, values in rows.items()
    }


def _cancel_expected(connection):
    """按唯一归属独立读取：本剧本只有自身主行和其取消项。"""
    expected = {("actions", 2): row_facts(connection, "actions", 2)}
    with closing(connection.execute(
            "SELECT id FROM cancel_items WHERE action_id = 2 ORDER BY id")) as cursor:
        ids = [row[0] for row in cursor.fetchall()]
    for identity in ids:
        expected[("cancel_items", identity)] = row_facts(connection, "cancel_items", identity)
    return _business_rows(expected)


def _own_events(connection, action_id, boundary):
    """按本剧本的固定归属逐行解释，不用首个目录对象替代行归属。

    actions 按自己的 id；motor_notifications 和 cancel_items 按创建后
    不变的 action_id；plans 按自己的 id。此分类来自正式归属表。
    """
    with closing(connection.execute(
            "SELECT id, transaction_id, event_type, event_version, occurred_at,"
            " clock_status, change_seq, body_json FROM history_events"
            " WHERE id <= ? ORDER BY id", (boundary.last_event_id,))) as cursor:
        stored = cursor.fetchall()
    events = []
    for raw in stored:
        envelope = decode_event_row(raw)
        owners = {}
        for change in envelope.rows:
            if change.table == "plans":
                owner = (4, change.row_id)
            elif change.table == "actions":
                owner = (1, change.row_id)
            elif change.table in ("motor_notifications", "cancel_items"):
                row = row_facts(connection, change.table, change.row_id)
                owner = (1, row["action_id"])
            else:
                raise AssertionError(f"本剧本出现未定义归属表: {change.table}")
            owners[(change.table, change.row_id)] = owner
        references = tuple(dict.fromkeys(owners.values()))
        if (1, action_id) not in references:
            continue
        events.append(ValidatedEvent(
            envelope, event_type_name(envelope.event_type),
            branch_of(envelope.event_type, envelope.reason)[0],
            references, owners))
    return events


def test_cancel_own_image_excludes_cross_plan_target(cancellation_world):
    world = cancellation_world
    boundary = _fix_members(world)
    connection = world.owned.connection
    assert row_facts(connection, "actions", 1)["plan_id"] != row_facts(
        connection, "actions", 2)["plan_id"]
    expected = _cancel_expected(connection)
    actual = world.history.restore_entity("action", 2, boundary)
    assert _business_rows(actual) == expected


def test_cancel_target_changes_do_not_change_cancel_h_image(cancellation_world):
    world = cancellation_world
    boundary = _fix_members(world)
    expected = _cancel_expected(world.owned.connection)
    old_head = world.owned.connection.execute(
        "SELECT last_event_id, change_count FROM actions WHERE id = 2").fetchone()
    _finish_target(world)
    assert world.owned.connection.execute(
        "SELECT last_event_id, change_count FROM actions WHERE id = 2").fetchone() == old_head
    actual = world.history.restore_entity("action", 2, boundary, event_batch_size=1)
    assert _business_rows(actual) == expected


def test_cancel_h_before_member_creation_has_no_future_target(cancellation_world):
    world = cancellation_world
    expected = _cancel_expected(world.owned.connection)
    _fix_members(world)
    _finish_target(world)
    actual = world.history.restore_entity(
        "action", 2, world.before_members, event_batch_size=1)
    assert _business_rows(actual) == expected


def test_cancel_report_dependency_uses_same_h_without_selecting_cross_plan_target(cancellation_world):
    world = cancellation_world
    boundary = _fix_members(world)
    _finish_target(world)
    subtree = PlanSubtree(2, {"action": {2: {"output": {}, "delivery": {}}}})
    facts = _plan_facts(world.history, subtree, boundary)
    assert facts["actions"][1]["status"] == 1  # H 时目标仍未开始。
    assert ("motor_notifications" not in facts
            or not facts["motor_notifications"])  # 发送意图在 H 后建立。
    fragment = project_public(ProjectionInput(
        "plan", 2, facts, dict(subtree.selection)))
    assert [action["action_instance_id"] for action in fragment["actions"]] == ["2"]


@pytest.mark.asyncio
async def test_cancel_snapshot_contains_only_own_members(cancellation_world):
    world = cancellation_world
    boundary = _fix_members(world)
    expected = _cancel_expected(world.owned.connection)
    executor = DbExecutor(
        lambda: open_existing(world.history.path, DbOpenMode.EXISTING_RW, DbConfig()),
        capacity=4, enqueue_timeout_seconds=9)
    try:
        store = SqliteSnapshotStore(world.history.path, executor)
        images = await store.load_entity_images((SnapshotRef(1, 2),))
        assert images[0].boundary == boundary
        assert _business_rows({
            (row.table, row.row_id): row.values for row in images[0].rows}) == expected
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_cancel_initial_snapshot_and_reverse_match_unique_ownership(cancellation_world):
    world = cancellation_world
    executor = DbExecutor(
        lambda: open_existing(world.history.path, DbOpenMode.EXISTING_RW, DbConfig()),
        capacity=4, enqueue_timeout_seconds=9)
    try:
        store = SqliteSnapshotStore(world.history.path, executor)
        seed = (await store.load_entity_images((SnapshotRef(1, 2),)))[0]
        prepared = prepare_snapshot(seed)
        await store.save_snapshots((prepared,))
        stored = world.owned.connection.execute(
            "SELECT content FROM entity_snapshots WHERE entity_type = 1 AND entity_id = 2"
        ).fetchone()
        header, snapshot_rows = decode_snapshot(stored[0], expect_ref=SnapshotRef(1, 2))
        boundary = _fix_members(world)
        expected = _cancel_expected(world.owned.connection)
        events = _own_events(world.owned.connection, 2, boundary)
        initial = restore(RestoreSeed(
            EntityImage(1, 2, False, {}, 0, 0), INITIAL_BOUNDARY), events, boundary)
        snapshot = restore(RestoreSeed(EntityImage(
            1, 2, True,
            _business_rows({(row.table, row.row_id): row.values for row in snapshot_rows}),
            header["boundary_event_id"], header["change_count"]),
            seed.boundary), events, boundary)
        _finish_target(world)
        reverse_rows = world.history.restore_entity("action", 2, boundary, event_batch_size=1)
        assert initial.rows == expected
        assert snapshot.rows == expected
        assert _business_rows(reverse_rows) == expected
        latest = world.history.current_boundary()
        current = EntityImage(
            1, 2, True, _cancel_expected(world.owned.connection),
            events[-1].envelope.event_id, initial.change_count)
        for event in reversed(_own_events(world.owned.connection, 2, latest)):
            if event.envelope.event_id > seed.boundary.last_event_id:
                current = apply_reverse(current, event)
        assert current.rows == _business_rows({
            (row.table, row.row_id): row.values for row in snapshot_rows})
    finally:
        await executor.close()

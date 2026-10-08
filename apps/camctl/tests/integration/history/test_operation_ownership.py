"""停止流程沿原活动归属，查询流程沿触发动作归属。"""

from contextlib import closing
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio

from camctl.capture.recovery import EmergencyOutcome, EmergencyRecord
from camctl.contracts.history_values import INITIAL_BOUNDARY
from camctl.contracts.values import new_operation_key
from camctl.history.decoding import decode_event_row
from camctl.history.events import branch_of, event_type_name
from camctl.history.replay import EntityImage, RestoreSeed, restore
from camctl.history.snapshots import SnapshotRef
from camctl.history.validators import ValidatedEvent
from camctl.operations.attempts import (
    AttemptConfig, AttemptIntent, AttemptTarget, BeginDisposition,
    OperationKind, QueryPurpose,
)
from camctl.persistence.executor import DbExecutor
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.history import SqliteSnapshotStore
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.scheduling import SchedulingRepository, StartActionRequest
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import row_facts

from ..persistence.test_motor_transactions import NOW, owned
from .test_object_closure import _business_rows, _history
from .test_report_scope import _submit


@pytest_asyncio.fixture
async def operation_world(owned, tmp_path):
    """两个真实受理动作；活动属于动作 1，后续停止由动作 2 触发。"""
    for request_id in ("1", "2"):
        await _submit(owned, tmp_path, request_id, "2026-10-08 09:00:00")
    started = SchedulingRepository().start_action(
        StartActionRequest(1, NOW, NOW), new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    assert row_facts(owned.connection, "device_activities", 1)["action_id"] == 1
    return SimpleNamespace(
        owned=owned, history=_history(owned), operations=OperationRepository(),
        members={1: {("actions", 1), ("device_activities", 1)},
                 2: {("actions", 2)}},
    )


def _remember_run(world, run_id, owner):
    """所有者由场景契约传入，预期不从生产枚举或目录关联推导。"""
    world.members[owner].add(("operation_runs", run_id))
    with closing(world.owned.connection.execute(
            "SELECT id FROM operation_attempts WHERE run_id = ?", (run_id,))) as cursor:
        attempts = cursor.fetchall()
    assert len(attempts) == 1
    world.members[owner].add(("operation_attempts", attempts[0][0]))


def _begin(world, kind, purpose=None):
    intent = AttemptIntent(
        "query" if purpose else "stop", 2, kind,
        AttemptTarget(activity_id=1), purpose,
        AttemptConfig(3, Decimal("5"), Decimal("1")), NOW + 1)
    begun = world.operations.begin_attempt(intent, new_operation_key(), world.owned)
    assert begun.kind is DbOutcomeKind.COMPLETED, begun.error
    assert begun.value.disposition is BeginDisposition.GRANTED
    return begun.value.ticket.run_id


def _create_flow(world, branch):
    if branch in ("residual_stop", "residual_query"):
        stop_id = _begin(world, OperationKind.STOP_RESIDUAL)
        assert row_facts(world.owned.connection, "operation_runs", stop_id)["action_id"] == 2
        _remember_run(world, stop_id, 1)  # 原活动所属动作拥有停止及其尝试。
        if branch == "residual_stop":
            return stop_id, 1
        query_id = _begin(world, OperationKind.QUERY_ACTIVITY,
                          QueryPurpose.RESIDUAL_STOP_CONFIRMATION)
        _remember_run(world, query_id, 2)  # 查询预算属于触发动作。
        return query_id, 2
    assert branch == "emergency_stop"
    saved = CaptureRepository().save_emergency(
        session_key="a" * 32, action_id=1, activity_id=1,
        record=EmergencyRecord(EmergencyOutcome.UNCONFIRMED, 1, 3),
        attempts=({
            "status": 4, "effect_state": 2,
            "result_json": {"format_version": 1, "settlement": {
                "basis": "observed", "evidence": {
                    "type": "stop_returned", "version": 1, "data": {}}},
                "observations": []},
            "error_json": {"code": "timeout", "stage": "transport"},
        },),
        occurred_at=NOW + 1, key=new_operation_key(), owned=world.owned,
        timeout_s=Decimal("10"), retry_interval_s=Decimal("1"))
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    _remember_run(world, saved.value.run_id, 1)
    return saved.value.run_id, 1


def _expected(world, action_id):
    return _business_rows({
        key: row_facts(world.owned.connection, *key)
        for key in world.members[action_id]
    })


def _events(world, action_id, boundary):
    """按场景中明确保存的唯一成员身份逐行赋予所有者。"""
    owners_by_key = {
        key: (1, owner)
        for owner, members in world.members.items() for key in members
    }
    with closing(world.owned.connection.execute(
            "SELECT id, transaction_id, event_type, event_version, occurred_at,"
            " clock_status, change_seq, body_json FROM history_events"
            " WHERE id <= ? ORDER BY id", (boundary.last_event_id,))) as cursor:
        stored = cursor.fetchall()
    events = []
    for raw in stored:
        envelope = decode_event_row(raw)
        owners = {}
        for change in envelope.rows:
            key = (change.table, change.row_id)
            if change.table == "plans":
                owners[key] = (4, change.row_id)
            else:
                owners[key] = owners_by_key[key]
        references = tuple(dict.fromkeys(owners.values()))
        if (1, action_id) in references:
            events.append(ValidatedEvent(
                envelope, event_type_name(envelope.event_type),
                branch_of(envelope.event_type, envelope.reason)[0],
                references, owners))
    return events


@pytest.mark.parametrize("branch", ["residual_stop", "emergency_stop", "residual_query"])
@pytest.mark.parametrize("side", ["owner", "other"])
def test_run_members_follow_declared_owner(operation_world, branch, side):
    world = operation_world
    run_id, owner = _create_flow(world, branch)
    action_id = owner if side == "owner" else 3 - owner
    boundary = world.history.current_boundary()
    actual = world.history.restore_entity("action", action_id, boundary)
    assert (("operation_runs", run_id) in actual) == (side == "owner")
    assert _business_rows(actual) == _expected(world, action_id)


@pytest.mark.parametrize("branch", ["residual_stop", "emergency_stop", "residual_query"])
def test_run_restore_before_creation_has_no_later_members(operation_world, branch):
    world = operation_world
    boundary = world.history.current_boundary()
    expected = {action: _expected(world, action) for action in (1, 2)}
    _create_flow(world, branch)
    for action in (1, 2):
        actual = world.history.restore_entity("action", action, boundary, event_batch_size=1)
        assert _business_rows(actual) == expected[action]


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["residual_stop", "emergency_stop", "residual_query"])
async def test_run_snapshot_and_three_restore_paths_match_ownership(operation_world, branch):
    world = operation_world
    executor = DbExecutor(
        lambda: open_existing(world.history.path, DbOpenMode.EXISTING_RW, DbConfig()),
        capacity=4, enqueue_timeout_seconds=9)
    try:
        store = SqliteSnapshotStore(world.history.path, executor)
        seeds = await store.load_entity_images((SnapshotRef(1, 1), SnapshotRef(1, 2)))
        _create_flow(world, branch)
        boundary = world.history.current_boundary()
        images = await store.load_entity_images((SnapshotRef(1, 1), SnapshotRef(1, 2)))
        for action_id, seed, image in zip((1, 2), seeds, images):
            expected = _expected(world, action_id)
            events = _events(world, action_id, boundary)
            initial = restore(RestoreSeed(
                EntityImage(1, action_id, False, {}, 0, 0), INITIAL_BOUNDARY),
                events, boundary)
            snapshot = restore(RestoreSeed(EntityImage(
                1, action_id, True,
                _business_rows({(row.table, row.row_id): row.values for row in seed.rows}),
                next(row.values["last_event_id"] for row in seed.rows
                     if row.table == "actions"), seed.change_count), seed.boundary),
                events, boundary)
            actual = world.history.restore_entity("action", action_id, boundary, event_batch_size=1)
            assert initial.rows == expected
            assert snapshot.rows == expected
            assert _business_rows(actual) == expected
            assert _business_rows({(row.table, row.row_id): row.values for row in image.rows}) == expected
    finally:
        await executor.close()

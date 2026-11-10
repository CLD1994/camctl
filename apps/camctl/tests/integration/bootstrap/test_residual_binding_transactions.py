"""原残留绑定失败的完整事务、原键核实及故障恢复。"""

from dataclasses import replace

import pytest
import pytest_asyncio

from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import (
    CaptureRepository, FinishCapture, FinishResidualBindingFailure,
    OutputCatalogFacts, RecordingFailure,
)
from camctl.persistence.repositories.scheduling import SchedulingRepository, StartActionRequest
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.repositories.transactions import TransactionError

from ..support.bootstrap_residual_recovery import residual_case
from ..support.bootstrap_recovery import StorageGate, TransactionFault


@pytest_asyncio.fixture
async def environment(tmp_path, monkeypatch, capture_resources):
    async with residual_case(tmp_path, capture_resources, monkeypatch, cancel_trigger=True) as case:
        yield case


async def _command(case, orphan):
    await case.advance()
    keys = (f"followup/{case.trigger}/{case.activity}", f"query/residual/{case.trigger}/{case.activity}")
    assert case.owned.connection.execute(
        "SELECT COUNT(*) FROM operation_runs WHERE responsibility_key IN (?,?) AND status=2", keys,
    ).fetchone() == (2,)
    trigger = None
    if not orphan:
        trigger = await case.accept(4, case.photo("新驱动触发"), driver_id="alternate-camera")
        started = SchedulingRepository().start_action(
            StartActionRequest(trigger, case.wall[0], case.wall[0]), new_operation_key(), case.owned)
        assert started.kind is DbOutcomeKind.COMPLETED, started.error
    command = FinishResidualBindingFailure(trigger, case.activity, case.wall[0],
        RecordingFailure("device_binding_unavailable", {"device_id": "cam-1",
            "expected_driver_id": "camctl-adb", "actual_driver_id": "alternate-camera", "reason": "mismatch"}), keys)
    return command, case.old_action, case.trigger


@pytest.mark.asyncio
@pytest.mark.parametrize("orphan", [False, True])
@pytest.mark.parametrize("phase", ["projection", "commit_before", "commit_after"])
async def test_residual_binding_failure_recovers_full_responsibility_group(environment, orphan, phase):
    owned, home = environment.owned, environment.home
    command, old_action, original_trigger = await _command(environment, orphan)
    repository = CaptureRepository()
    key = new_operation_key()
    before = tuple(owned.connection.iterdump())
    attempts = owned.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall()
    activity = owned.connection.execute(
        "SELECT * FROM device_activities WHERE id = ?", (command.activity_id,)).fetchone()
    if phase == "projection":
        gate = StorageGate(table="operation_runs", column="status")
        failed = gate.submit(repository.finish_residual_binding_failure, command, key, owned)
        assert gate.hits >= 1
    else:
        fault = TransactionFault(owned.connection, after_commit=phase == "commit_after")
        failed = repository.finish_residual_binding_failure(command, key, replace(owned, connection=fault))
        assert fault.commit_calls == 1

    assert failed.kind is (DbOutcomeKind.ROLLED_BACK if phase == "projection" else DbOutcomeKind.UNKNOWN)
    if phase == "projection":
        assert tuple(owned.connection.iterdump()) == before
    owned.connection.close()
    reopened = open_existing(home / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
    try:
        if phase == "commit_before":
            assert tuple(reopened.connection.iterdump()) == before
        recovered = repository.finish_residual_binding_failure(command, key, reopened)
        assert recovered.kind is DbOutcomeKind.COMPLETED, recovered.error
        assert reopened.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall() == attempts
        assert reopened.connection.execute(
            "SELECT * FROM device_activities WHERE id = ?", (command.activity_id,)).fetchone() == activity
        assert reopened.connection.execute(
            "SELECT responsibility_key, status, attempts_used FROM operation_runs"
            " WHERE responsibility_key IN (?, ?) ORDER BY id", command.responsibility_keys,
        ).fetchall() == [(command.responsibility_keys[0], 4, 1), (command.responsibility_keys[1], 4, 1)]
        assert reopened.connection.execute(
            "SELECT status FROM actions WHERE id = ?", (old_action,)).fetchone() == (4,)
        assert reopened.connection.execute(
            "SELECT status FROM actions WHERE id = ?", (original_trigger,)).fetchone() == (6,)
        if command.action_id is not None:
            assert reopened.connection.execute(
                "SELECT status, execution_started, driver_id, error_code FROM actions WHERE id = ?",
                (command.action_id,),
            ).fetchone() == (4, 1, "alternate-camera", 18)
        saved = tuple(reopened.connection.iterdump())
        repeated = repository.finish_residual_binding_failure(command, key, reopened)
        assert repeated.kind is DbOutcomeKind.COMPLETED, repeated.error
        assert tuple(reopened.connection.iterdump()) == saved
        transaction = reopened.connection.execute(
            "SELECT id, first_event_id, last_event_id FROM history_transactions WHERE operation_key = ?",
            (str(key),),
        ).fetchone()
        events = reopened.connection.execute(
            "SELECT id, event_type FROM history_events WHERE transaction_id = ? ORDER BY id",
            (transaction[0],),
        ).fetchall()
        assert [row[0] for row in events] == list(range(transaction[1], transaction[2] + 1))
        assert [row[1] for row in events] == ([10, 10] if orphan else [8, 9, 10, 10])
        assert reopened.connection.execute(
            "SELECT entity_type, entity_id FROM entity_event_links WHERE event_id = ?", (events[-2][0],),
        ).fetchall() == [(1, old_action)]
        assert reopened.connection.execute(
            "SELECT entity_type, entity_id FROM entity_event_links WHERE event_id = ?", (events[-1][0],),
        ).fetchall() == [(1, original_trigger)]
    finally:
        reopened.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["time", "action_branch", "reason", "responsibilities"])
async def test_residual_binding_original_key_rejects_changed_input(environment, changed):
    owned = environment.owned
    command, _, _ = await _command(environment, False)
    repository = CaptureRepository()
    key = new_operation_key()
    saved = repository.finish_residual_binding_failure(command, key, owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    if changed == "time":
        changed_command = replace(command, occurred_at=environment.wall[0] + 1)
    elif changed == "action_branch":
        changed_command = replace(command, action_id=None)
    elif changed == "reason":
        changed_command = replace(command, failure=RecordingFailure("device_binding_unavailable", {
            "device_id": "cam-1", "expected_driver_id": "camctl-adb", "reason": "missing"}))
    else:
        changed_command = replace(command, responsibility_keys=command.responsibility_keys[:1])
    before = tuple(owned.connection.iterdump())

    rejected = repository.finish_residual_binding_failure(changed_command, key, owned)

    assert rejected.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(rejected.error, TransactionError), rejected.error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.asyncio
async def test_residual_binding_original_key_rejects_action_only_group(environment):
    owned = environment.owned
    command, _, _ = await _command(environment, False)
    repository = CaptureRepository()
    key = new_operation_key()
    partial = repository.finish_capture(FinishCapture(
        command.action_id, (), OutputCatalogFacts(command.action_id, True), environment.wall[0],
        RecordingFailure("device_activity_unresolved", {
            "activity_id": str(command.activity_id), "device_id": "cam-1"})), key, owned)
    assert partial.kind is DbOutcomeKind.COMPLETED, partial.error
    before = tuple(owned.connection.iterdump())

    rejected = repository.finish_residual_binding_failure(command, key, owned)

    assert rejected.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(rejected.error, TransactionError), rejected.error
    assert tuple(owned.connection.iterdump()) == before

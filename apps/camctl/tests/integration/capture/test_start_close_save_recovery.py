"""正式C1持有START完整收场，未知或回滚后优先核实原申请。"""

from dataclasses import replace
import json

import pytest

from camctl.capture.media import RecordingFailure
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.contracts.workflow_errors import registered_error
from camctl.operations.attempts import RunOutcome, StaleRunFinish
from camctl.operations.models import ErrorValue
from camctl.outputs.catalog import OutputCatalogFacts
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository, FinishCapture
from camctl.persistence.repositories.transactions import saved_transaction_events

from ..support.close_cases import CloseFault, start_case, start_query_storage_case

pytestmark = pytest.mark.asyncio


async def start_close_world(tmp_path, mode):
    """返回真实受理产生的动态action/activity身份与正式工厂。"""
    return await start_case(tmp_path, mode)


def _attempts(owned):
    return owned.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall()


def _history(owned):
    return owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()


def _assert_complete_group(world, owned, finish, key):
    events = saved_transaction_events(owned.connection, key)
    assert events is not None and {event["occurred_at"] for event in events} == {world.formed_at}
    assert len({event["transaction"].txn_id for event in events}) == 1
    changes = {(row["table"], row["id"]): row["after"]["values"]
        for event in events for row in event["body"]["rows"]}
    expected_start = 4 if world.mode == "lowered" else 6
    for responsibility in finish.responsibility_keys:
        run = owned.connection.execute(
            "SELECT id,status,retry_wait_required,error_json FROM operation_runs WHERE responsibility_key=?",
            (responsibility,)).fetchone()
        if run is None:
            assert world.mode == "unknown"
            assert responsibility == f"query/start/{world.action_id}/{world.activity_id}"
            assert owned.connection.execute("SELECT state_query_supported FROM device_activities WHERE id=?",
                (world.activity_id,)).fetchone() == (0,)
            continue
        assert run[1:3] == (expected_start, 0)
        assert changes[("operation_runs", run[0])]["status"] == expected_start
        assert json.loads(run[3]) == {"code": finish.error.code, "stage": finish.error.stage,
            "details": dict(finish.error.details)}
    assert changes[("actions", world.action_id)]["status"] == 4
    assert changes[("plans", 1)]["status"] == 3
    assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (world.action_id,)).fetchone() == (4,)
    assert owned.connection.execute("SELECT status FROM plans WHERE id=1").fetchone() == (3,)
    occupancy = 2 if world.mode == "lowered" else 1
    assert owned.connection.execute("SELECT occupancy_state FROM device_activities WHERE id=?",
        (world.activity_id,)).fetchone() == (occupancy,)
    if world.mode == "lowered":
        assert changes[("device_activities", world.activity_id)]["occupancy_state"] == 2
    assert owned.connection.execute("SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
        (str(key),)).fetchone() == (1,)
    return events


async def _assert_save_recovery(tmp_path, monkeypatch, mode, phase):
    world = await start_close_world(tmp_path, mode)
    actual_save = CaptureRepository.close_start
    inputs, receipts, faults = [], [], []
    before_attempts, before_history = _attempts(world.owned), _history(world.owned)
    before_dump = tuple(world.owned.connection.iterdump())
    query_calls = 0 if world.query is None else len(world.query.calls)

    def save(repository, finish, action_finish, key, current):
        inputs.append((finish, action_finish, key))
        if len(inputs) == 1:
            fault = CloseFault(current.connection, "actions", phase)
            faults.append(fault)
            try:
                receipt = actual_save(repository, finish, action_finish, key, replace(current, connection=fault))
            finally:
                fault.close()
        else:
            receipt = actual_save(repository, finish, action_finish, key, current)
        receipts.append(receipt)
        return receipt

    monkeypatch.setattr(CaptureRepository, "close_start", save)
    try:
        with pytest.raises(ConsistencyError):
            await world.advance()
        assert len(inputs) == len(receipts) == len(faults) == 1
        finish, action_finish, key = inputs[0]
        assert finish.occurred_at == action_finish.occurred_at == world.formed_at
        expected_keys = (f"start/{world.action_id}",) if mode == "lowered" else (
            f"start/{world.action_id}", f"query/start/{world.action_id}/{world.activity_id}")
        assert finish.responsibility_keys == expected_keys
        if phase == "projection":
            assert faults[0].targeted and faults[0].failed and faults[0].rollback_calls == 1
            assert receipts[0].kind is DbOutcomeKind.ROLLED_BACK
            assert tuple(world.owned.connection.iterdump()) == before_dump
        else:
            assert faults[0].commit_calls == 1 and receipts[0].kind is DbOutcomeKind.UNKNOWN
        await world.reopen(recording={"max_start_attempts": 9}, query={"max_attempts": 9})
        assert world.owned.metadata == world.metadata
        first_events = saved_transaction_events(world.owned.connection, key)
        after = phase == "commit_after"
        assert (first_events is not None) is after
        assert world.owned.connection.execute("SELECT status FROM actions WHERE id=?",
            (world.action_id,)).fetchone() == (4 if after else 2,)
        if not after:
            assert tuple(world.owned.connection.iterdump()) == before_dump
        world.wall[0] += 100_000_000
        world.mono[0] += 100_000_000_000
        await world.advance()
        assert inputs == [(finish, action_finish, key), (finish, action_finish, key)]
        assert inputs[1][0] is finish and inputs[1][1] is action_finish
        assert receipts[1].kind is DbOutcomeKind.COMPLETED and receipts[1].value is None
        events = _assert_complete_group(world, world.owned, finish, key)
        if first_events is not None:
            assert events == first_events
        assert _attempts(world.owned) == before_attempts and _history(world.owned)[:len(before_history)] == before_history
        assert len(world.driver.calls) == 1 and world.stopper.calls == [] and world.results.calls == []
        assert (0 if world.query is None else len(world.query.calls)) == query_calls == 0
        assert world.replacement.calls == world.replacement.stop_calls == world.replacement.query_calls == []
        final_dump = tuple(world.owned.connection.iterdump())
        await world.advance()
        assert tuple(world.owned.connection.iterdump()) == final_dump and len(inputs) == 2
    finally:
        await world.close()


@pytest.mark.parametrize("mode", ["unknown", "lowered"])
@pytest.mark.parametrize("after_commit", [False, True], ids=["commit-before", "commit-after"])
async def test_unknown_start_close_keeps_full_request_until_original_key_is_confirmed(
        tmp_path, monkeypatch, mode, after_commit):
    await _assert_save_recovery(tmp_path, monkeypatch, mode,
        "commit_after" if after_commit else "commit_before")


@pytest.mark.parametrize("mode", ["unknown", "lowered"])
async def test_rolled_back_start_close_preserves_whole_request_and_original_time(
        tmp_path, monkeypatch, mode):
    await _assert_save_recovery(tmp_path, monkeypatch, mode, "projection")


@pytest.mark.parametrize("phase", ["commit_before", "commit_after", "projection"])
async def test_start_query_close_storage_preserves_both_responsibilities_and_original_key(
        tmp_path, phase):
    """公开存储入口共同结束已存在的START与QUERY，并按原键核实整组。"""
    world = await start_query_storage_case(tmp_path)
    repository = CaptureRepository()
    failure = RecordingFailure("capture_result_unconfirmed", {
        "activity_id": str(world.activity_id), "reason": "start_unknown"})
    finish = StaleRunFinish(
        (f"start/{world.action_id}", f"query/start/{world.action_id}/{world.activity_id}"),
        RunOutcome.UNCONFIRMED, world.formed_at,
        ErrorValue(failure.code, registered_error(failure.code)["stage"], failure.details))
    action_finish = FinishCapture(world.action_id, (),
        OutputCatalogFacts(world.action_id, True), world.formed_at, failure=failure)
    key = new_operation_key()
    before_attempts, before_history = _attempts(world.owned), _history(world.owned)
    before_dump = tuple(world.owned.connection.iterdump())
    faults = []

    def save(current):
        fault = CloseFault(current.connection, "actions", phase)
        faults.append(fault)
        try:
            receipt = repository.close_start(finish, action_finish, key,
                replace(current, connection=fault))
        finally:
            fault.close()
        return receipt

    try:
        receipt = await world.database.write(save, key=key)
        (fault,) = faults
        assert fault.targeted and fault.failed
        if phase == "projection":
            assert receipt.kind is DbOutcomeKind.ROLLED_BACK and fault.rollback_calls == 1
        else:
            assert receipt.kind is DbOutcomeKind.UNKNOWN and fault.commit_calls == 1
        if phase != "commit_after":
            assert tuple(world.owned.connection.iterdump()) == before_dump
        await world.reopen(recording={"max_start_attempts": 9}, query={"max_attempts": 9})
        first_events = saved_transaction_events(world.owned.connection, key)
        assert (first_events is not None) is (phase == "commit_after")
        world.wall[0] += 100_000_000
        world.mono[0] += 100_000_000_000
        recovered = await world.database.write(lambda current:
            repository.close_start(finish, action_finish, key, current), key=key)
        assert recovered.kind is DbOutcomeKind.COMPLETED, recovered.error
        events = _assert_complete_group(world, world.owned, finish, key)
        if first_events is not None:
            assert events == first_events
        assert _attempts(world.owned) == before_attempts
        assert _history(world.owned)[:len(before_history)] == before_history
        assert len(world.driver.calls) == len(world.query.calls) == 1
        assert world.stopper.calls == world.results.calls == []
        assert world.replacement.calls == world.replacement.stop_calls == world.replacement.query_calls == []
        completed = tuple(world.owned.connection.iterdump())
        repeated = await world.database.write(lambda current:
            repository.close_start(finish, action_finish, key, current), key=key)
        assert repeated.kind is DbOutcomeKind.COMPLETED, repeated.error
        assert tuple(world.owned.connection.iterdump()) == completed
    finally:
        await world.close()

"""默认五入口从真实会话数据库核实原 START 完整收场申请。"""

from dataclasses import replace

import pytest

from camctl.capture.handlers import capture_handler
from camctl.contracts.values import ConsistencyError
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.transactions import saved_transaction_events

from ..support.bootstrap_recovery import record_world, StorageGate, TransactionFault

pytestmark = pytest.mark.asyncio
_MODES = ("unknown", "lowered")
_ENTRANCES = ("normal", "residual", "restricted", "normal-cancel", "restricted-cancel")


def _rows(owned, table):
    return owned.connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()


def _assert_complete_group(world, owned, finish, key):
    events = saved_transaction_events(owned.connection, key)
    assert events and {event["occurred_at"] for event in events} == {world.formed_at}
    assert len({event["transaction"].txn_id for event in events}) == 1
    changes = {(row["table"], row["id"]): row["after"]["values"]
        for event in events for row in event["body"]["rows"]}
    assert changes[("actions", world.action_id)]["status"] == 4
    assert owned.connection.execute(
        "SELECT status FROM actions WHERE id=?", (world.action_id,)).fetchone() == (4,)
    for responsibility in finish.responsibility_keys:
        run = owned.connection.execute(
            "SELECT id,status,retry_wait_required FROM operation_runs WHERE responsibility_key=?", (responsibility,)).fetchone()
        if run is None:
            assert world.mode == "unknown" and responsibility.startswith("query/start/")
            continue
        expected = 4 if world.mode == "lowered" else 6
        assert run[1:] == (expected, 0)
        assert changes[("operation_runs", run[0])]["status"] == expected
    assert changes[("plans", 1)]["status"] == 3
    assert owned.connection.execute("SELECT status FROM plans WHERE id=1").fetchone() == (3,)
    occupancy = 2 if world.mode == "lowered" else 1
    assert owned.connection.execute("SELECT occupancy_state FROM device_activities WHERE action_id=?",
        (world.action_id,)).fetchone() == (occupancy,)
    if world.mode == "lowered":
        assert changes[("device_activities", world.action_id)]["occupancy_state"] == 2
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(key),)).fetchone() == (1,)
    return events


async def _exercise(tmp_path, monkeypatch, capture_resources, mode, entrance, *,
                    after_commit=True, confirmation_unknown=None):
    world = await record_world(tmp_path, capture_resources, monkeypatch, mode=mode)
    original = CaptureRepository.close_start
    inputs, receipts, faults = [], [], []
    gate = None if confirmation_unknown is None else StorageGate(
        original_key=True, unknown=confirmation_unknown)

    def save(repository, finish, action_finish, key, owned):
        inputs.append((finish, action_finish, key))
        if len(inputs) == 1:
            fault = TransactionFault(owned.connection, after_commit=after_commit)
            faults.append(fault)
            result = original(repository, finish, action_finish, key, replace(owned, connection=fault))
        elif gate is not None:
            result = gate.submit(lambda _request, operation_key, current:
                original(repository, finish, action_finish, operation_key, current), None, key, owned)
        else:
            result = original(repository, finish, action_finish, key, owned)
        receipts.append(result)
        return result

    monkeypatch.setattr(CaptureRepository, "close_start", save)
    try:
        before_attempts = _rows(world.owned, "operation_attempts")
        before_history = _rows(world.owned, "history_events")
        before_calls = (len(world.driver.calls), 0 if world.query is None else len(world.query.calls))
        with pytest.raises(ConsistencyError):
            await capture_handler("camera_record")(world.action_id, world.runtime)
        assert len(inputs) == len(receipts) == len(faults) == 1
        assert receipts[0].kind is DbOutcomeKind.UNKNOWN and faults[0].commit_calls == 1
        finish, action_finish, key = inputs[0]
        assert finish.occurred_at == action_finish.occurred_at == world.formed_at
        assert action_finish.action_id == world.action_id
        world.owned.connection.close()
        await world.reconnect()
        with world.observer() as owned:
            first_events = saved_transaction_events(owned.connection, key)
            assert (first_events is not None) is after_commit
            before = tuple(owned.connection.iterdump())

            def check():
                assert inputs == [(finish, action_finish, key)] * 2
                assert inputs[1][0] is finish and inputs[1][1] is action_finish
                assert receipts[1].kind is DbOutcomeKind.COMPLETED, receipts[1].error
                assert receipts[1].value is None
                events = _assert_complete_group(world, owned, finish, key)
                if first_events is not None:
                    assert events == first_events
                assert _rows(owned, "operation_attempts") == before_attempts
                assert _rows(owned, "history_events")[:len(before_history)] == before_history

            await world.replay(entrance, check, ready=lambda: len(receipts) == 2 and
                receipts[-1].kind is DbOutcomeKind.COMPLETED, failure=gate is not None)
            if gate is not None:
                assert inputs == [(finish, action_finish, key)] * 2
                assert inputs[1][0] is finish and inputs[1][1] is action_finish
                assert gate.hits == 1
                assert receipts[1].kind is (
                    DbOutcomeKind.UNKNOWN if confirmation_unknown else DbOutcomeKind.ROLLED_BACK)
                assert tuple(owned.connection.iterdump()) == before
            else:
                check()
            assert (len(world.driver.calls), 0 if world.query is None else len(world.query.calls)) == before_calls
    finally:
        world.close()


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("after_commit", [False, True], ids=["commit-before", "commit-after"])
@pytest.mark.parametrize("entrance", _ENTRANCES)
async def test_default_entry_confirms_original_start_close_before_candidates(
        tmp_path, monkeypatch, capture_resources, mode, after_commit, entrance):
    await _exercise(tmp_path, monkeypatch, capture_resources, mode, entrance, after_commit=after_commit)


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("confirmation_unknown", [False, True], ids=["rolled-back", "unknown"])
@pytest.mark.parametrize("entrance", ["normal", "restricted-cancel"])
async def test_default_entry_keeps_start_close_when_original_key_is_unavailable(
        tmp_path, monkeypatch, capture_resources, mode, confirmation_unknown, entrance):
    await _exercise(tmp_path, monkeypatch, capture_resources, mode, entrance,
        confirmation_unknown=confirmation_unknown)

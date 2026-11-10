"""正式STOP耗尽的两个保存成员在失败后仍按原申请与键恢复。"""

from dataclasses import replace
from decimal import Decimal
import json

import pytest

from camctl.bootstrap import capture_assembly
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.contracts.workflow_errors import registered_error
from camctl.operations.attempts import RunOutcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.transactions import saved_transaction_events

from ..support.close_cases import CloseFault, stop_case

pytestmark = pytest.mark.asyncio


async def stop_close_world(tmp_path, consumer):
    """实际START和STOP由正式C1保存，下一次推进形成耗尽收场。"""
    world = await stop_case(tmp_path, consumer)
    ticket = world.stopper.calls[0].ticket
    status, effect, result_json, error_json, occurred_at = world.owned.connection.execute(
        "SELECT t.status,t.effect_state,t.result_json,t.error_json,e.occurred_at FROM operation_attempts t"
        " JOIN history_events e ON e.id=t.result_event_id WHERE t.run_id=? AND t.attempt_no=?",
        (ticket.run_id, ticket.attempt_id)).fetchone()
    assert (status, effect) == (3, int(enum_for("operation_attempts.effect_state").UNKNOWN))
    assert occurred_at == world.formed_at - 5_000_000
    result = json.loads(result_json)
    assert result["settlement"] == {"basis": "observed", "evidence": {
        "type": "stop_returned", "version": 1, "data": {}}}
    assert result["observations"] == [] and result["call_info"] == {"local_exit": {"exit_code": 0}}
    assert json.loads(error_json) == {"code": "transport_timeout", "stage": "transport", "details": {"timeout_s": 0.125}}
    return world


class StopCloseFault(CloseFault):
    """公开保存调用对应的真实表列或COMMIT故障，保留供默认消费者使用。"""

    def __init__(self, connection, world, stage, phase):
        self.identity, self.status = (world.stop_id, 6) if stage == "stop" else (world.action_id, 4)
        super().__init__(connection, "operation_runs" if stage == "stop" else "actions", phase)


def _attempts(owned):
    return owned.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall()


def _activity(owned, action_id=None):
    if action_id is None:
        action_id, = owned.connection.execute("SELECT id FROM actions WHERE type=2").fetchone()
    return owned.connection.execute(
        "SELECT activity_state,occupancy_state,started_at FROM device_activities WHERE action_id=?",
        (action_id,)).fetchone()


def _assert_original_group(owned, key, occurred_at, table, identity, status):
    events = saved_transaction_events(owned.connection, key)
    assert events is not None and {event["occurred_at"] for event in events} == {occurred_at}
    assert len({event["transaction"].txn_id for event in events}) == 1
    changes = {(row["table"], row["id"]): row["after"]["values"]
        for event in events for row in event["body"]["rows"]}
    assert changes[(table, identity)]["status"] == status
    assert owned.connection.execute("SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
        (str(key),)).fetchone() == (1,)
    return events


async def _assert_save_recovery(tmp_path, monkeypatch, consumer, stage, phase):
    world = await stop_close_world(tmp_path, consumer)
    stop_save, action_save = OperationRepository.finish_stale_runs, CaptureRepository.finish_capture
    stop_inputs, stop_receipts, action_inputs, action_receipts, order, faults = [], [], [], [], [], []
    before_attempts, before_activity = _attempts(world.owned), _activity(world.owned, world.action_id)
    before_history = world.owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()

    def submit(method, request, key, current, operation):
        if stage == operation and not faults:
            fault = StopCloseFault(current.connection, world, stage, phase)
            faults.append(fault)
            try:
                return method(request, key, replace(current, connection=fault))
            finally:
                fault.close()
        return method(request, key, current)

    def save_stop(repository, finish, key, current):
        relevant = finish.responsibility_keys == (f"stop/{world.action_id}",)
        if relevant:
            stop_inputs.append((finish, key))
            order.append("stop-save")
            receipt = submit(lambda *args: stop_save(repository, *args), finish, key, current, "stop")
            stop_receipts.append(receipt)
            return receipt
        return stop_save(repository, finish, key, current)

    def save_action(repository, request, key, current):
        relevant = request.action_id == world.action_id and request.failure is not None and request.failure.code == "recording_stop_failed"
        if relevant:
            action_inputs.append((request, key))
            order.append("action-save")
            receipt = submit(lambda *args: action_save(repository, *args), request, key, current, "action")
            action_receipts.append(receipt)
            return receipt
        return action_save(repository, request, key, current)

    monkeypatch.setattr(OperationRepository, "finish_stale_runs", save_stop)
    monkeypatch.setattr(CaptureRepository, "finish_capture", save_action)
    try:
        with pytest.raises(ConsistencyError):
            await world.advance()
        assert len(faults) == 1 and faults[0].targeted and faults[0].failed
        fault = faults[0]
        receipt = stop_receipts[0] if stage == "stop" else action_receipts[0]
        if phase == "projection":
            assert receipt.kind is DbOutcomeKind.ROLLED_BACK and fault.rollback_calls == 1
            assert tuple(world.owned.connection.iterdump()) == fault.target_before
        else:
            assert receipt.kind is DbOutcomeKind.UNKNOWN and fault.commit_calls == 1
        finish, stop_key = stop_inputs[0]
        assert finish.responsibility_keys == (f"stop/{world.action_id}",)
        assert finish.status is RunOutcome.UNCONFIRMED and finish.occurred_at == world.formed_at
        expected_details = {"activity_id": str(world.activity_id), "operation_run_id": str(world.stop_id)}
        assert finish.error.code == "recording_stop_failed" and dict(finish.error.details) == expected_details
        if consumer == "canceled":
            assert action_inputs == []
        elif stage == "action":
            original_action, original_action_key = action_inputs[0]
        await world.reopen(recording={"max_stop_attempts": 9, "stop_timeout_s": Decimal("8"),
            "stop_retry_interval_s": Decimal("30")}, query={"max_attempts": 9}, changed_binding=True)
        assert world.owned.metadata == world.metadata
        first_stop_events = saved_transaction_events(world.owned.connection, stop_key)
        after = phase == "commit_after"
        assert (first_stop_events is not None) is (stage == "action" or after)
        first_action_events = (saved_transaction_events(world.owned.connection, original_action_key)
            if stage == "action" else None)
        assert (first_action_events is not None) is (stage == "action" and after)
        assert world.owned.connection.execute("SELECT status FROM actions WHERE id=?",
            (world.action_id,)).fetchone() == (4 if stage == "action" and after else 2,)
        if not after:
            assert tuple(world.owned.connection.iterdump()) == fault.target_before
        real_binding = capture_assembly.check_binding

        def binding(saved, current):
            order.append("binding-check")
            return real_binding(saved, current)

        monkeypatch.setattr(capture_assembly, "check_binding", binding)
        world.wall[0] += 100_000_000
        world.mono[0] += 100_000_000_000
        order.clear()
        await world.advance()
        assert all(saved is finish and key == stop_key for saved, key in stop_inputs)
        assert len(stop_inputs) == 2 and stop_receipts[-1].kind is DbOutcomeKind.COMPLETED
        if "binding-check" in order:
            assert order.index("stop-save") < order.index("binding-check")
        stop_events = _assert_original_group(world.owned, stop_key, world.formed_at,
            "operation_runs", world.stop_id, 6)
        if first_stop_events is not None:
            assert stop_events == first_stop_events
        assert world.owned.connection.execute("SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE id=?",
            (world.stop_id,)).fetchone() == (6, 1, 0)
        if consumer == "ordinary":
            assert len(action_inputs) == (2 if stage == "action" else 1)
            action_finish, action_key = action_inputs[0]
            assert action_key != stop_key and action_receipts[-1].kind is DbOutcomeKind.COMPLETED
            assert all(saved is action_finish and key == action_key for saved, key in action_inputs)
            if stage == "action":
                assert action_finish is original_action and action_key == original_action_key
            assert action_finish.action_id == world.action_id and action_finish.drafts == ()
            assert action_finish.occurred_at == world.formed_at
            assert action_finish.failure.code == "recording_stop_failed" and dict(action_finish.failure.details) == expected_details
            action_events = _assert_original_group(world.owned, action_key, world.formed_at,
                "actions", world.action_id, 4)
            assert stop_events[0]["transaction"].txn_id != action_events[0]["transaction"].txn_id
            if first_action_events is not None:
                assert action_events == first_action_events
            assert world.owned.connection.execute("SELECT status,error_code FROM actions WHERE id=?",
                (world.action_id,)).fetchone() == (4, registered_error("recording_stop_failed")["action_error_id"])
        else:
            assert action_inputs == []
            assert world.owned.connection.execute("SELECT status,cancel_requested,error_code FROM actions WHERE id=?",
                (world.action_id,)).fetchone() == (6, 1, None)
        assert _attempts(world.owned) == before_attempts and _activity(world.owned, world.action_id) == before_activity
        assert world.owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
        assert world.owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()[:len(before_history)] == before_history
        assert len(world.driver.calls) == len(world.stopper.calls) == 1 and world.results.calls == []
        assert world.driver.query_calls == []
        assert world.replacement.calls == world.replacement.stop_calls == world.replacement.query_calls == []
        completed = tuple(world.owned.connection.iterdump())
        counts = len(stop_inputs), len(action_inputs)
        await world.advance()
        assert tuple(world.owned.connection.iterdump()) == completed
        assert (len(stop_inputs), len(action_inputs)) == counts
    finally:
        await world.close()


@pytest.mark.parametrize("consumer,stage", [
    ("ordinary", "stop"), ("ordinary", "action"), ("canceled", "stop"),
])
@pytest.mark.parametrize("after_commit", [False, True], ids=["commit-before", "commit-after"])
async def test_unknown_stop_close_keeps_full_request_before_current_binding_or_terminal_return(
        tmp_path, monkeypatch, consumer, stage, after_commit):
    await _assert_save_recovery(tmp_path, monkeypatch, consumer, stage,
        "commit_after" if after_commit else "commit_before")


@pytest.mark.parametrize("consumer,stage", [
    ("ordinary", "stop"), ("ordinary", "action"), ("canceled", "stop"),
])
async def test_rolled_back_stop_close_preserves_both_original_members_and_times(
        tmp_path, monkeypatch, consumer, stage):
    await _assert_save_recovery(tmp_path, monkeypatch, consumer, stage, "projection")

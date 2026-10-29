"""默认五入口先核原 STOP 及动作收尾，再取得新的业务资格。"""

from dataclasses import replace

import pytest

from camctl.capture.handlers import capture_handler
from camctl.contracts.values import ConsistencyError
from camctl.contracts.workflow_errors import registered_error
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.transaction import saved_transaction_events

from ..support.bootstrap_recovery import record_world, StorageGate, TransactionFault
from .test_start_close_default_recovery import _rows

pytestmark = pytest.mark.asyncio
_MODES = (("ordinary", "stop"), ("ordinary", "action"), ("canceled", "stop"))
_ENTRANCES = ("normal", "residual", "restricted", "normal-cancel", "restricted-cancel")


def _activity(owned, identity):
    return owned.connection.execute("SELECT activity_state,occupancy_state,started_at"
        " FROM device_activities WHERE action_id=?", (identity,)).fetchone()


def _group(owned, command, key, table, identity, status):
    events = saved_transaction_events(owned.connection, key)
    assert events and {event["occurred_at"] for event in events} == {command.occurred_at}
    assert len({event["transaction"].txn_id for event in events}) == 1
    changes = {(row["table"], row["id"]): row["after"]["values"]
        for event in events for row in event["body"]["rows"]}
    assert changes[(table, identity)]["status"] == status
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(key),)).fetchone() == (1,)
    return events


async def _exercise(tmp_path, monkeypatch, capture_resources, consumer, stage, entrance, *,
                    after_commit=True, confirmation_unknown=None):
    world = await record_world(tmp_path, capture_resources, monkeypatch, consumer=consumer)
    saves = {name: [] for name in ("stop", "action", "stop_receipts", "action_receipts")}
    actual_stop, actual_action = OperationRepository.finish_stale_runs, CaptureRepository.finish_capture
    faults = []
    gate = None if confirmation_unknown is None else StorageGate(
        original_key=True, unknown=confirmation_unknown)

    def submit(name, method, request, key, owned):
        saves[name].append((request, key))
        if name == stage and len(saves[name]) == 1:
            fault = TransactionFault(owned.connection, after_commit=after_commit)
            faults.append(fault)
            receipt = method(request, key, replace(owned, connection=fault))
        elif name == stage and gate is not None:
            receipt = gate.submit(method, request, key, owned)
        else:
            receipt = method(request, key, owned)
        saves[name + "_receipts"].append(receipt)
        return receipt

    def stop(repository, request, key, owned):
        if request.responsibility_keys != (f"stop/{world.action_id}",):
            return actual_stop(repository, request, key, owned)
        return submit("stop", lambda *args: actual_stop(repository, *args), request, key, owned)

    def action(repository, request, key, owned):
        if request.action_id != world.action_id or request.failure is None or request.failure.code != "recording_stop_failed":
            return actual_action(repository, request, key, owned)
        return submit("action", lambda *args: actual_action(repository, *args), request, key, owned)

    monkeypatch.setattr(OperationRepository, "finish_stale_runs", stop)
    monkeypatch.setattr(CaptureRepository, "finish_capture", action)
    try:
        before_attempts = _rows(world.owned, "operation_attempts")
        before_history = _rows(world.owned, "history_events")
        before_activity = _activity(world.owned, world.action_id)
        with pytest.raises(ConsistencyError):
            await capture_handler("camera_record")(world.action_id, world.runtime)
        assert len(faults) == 1 and faults[0].commit_calls == 1
        assert saves[stage + "_receipts"][0].kind is DbOutcomeKind.UNKNOWN
        finish, key = saves["stop"][0]
        assert finish.occurred_at == world.formed_at
        assert finish.error.code == "recording_stop_failed"
        assert dict(finish.error.details) == {"activity_id": str(world.action_id),
            "operation_run_id": str(world.stop_id)}
        if consumer == "canceled":
            assert saves["action"] == []
        world.owned.connection.close()
        await world.reconnect()
        with world.observer() as owned:
            first_events = saved_transaction_events(owned.connection, key)
            assert (first_events is not None) is (stage == "action" or after_commit)
            first_action = None if not saves["action"] else saved_transaction_events(
                owned.connection, saves["action"][0][1])
            assert (first_action is not None) is (stage == "action" and after_commit)
            before = tuple(owned.connection.iterdump())

            def frozen(*, action_reached=True):
                assert saves["stop"] == [(finish, key)] * 2
                assert saves["stop"][1][0] is finish
                if consumer == "canceled" or not action_reached:
                    assert saves["action"] == []
                else:
                    command, action_key = saves["action"][0]
                    assert command.occurred_at == world.formed_at and command.drafts == ()
                    assert command.failure.code == "recording_stop_failed"
                    assert dict(command.failure.details) == dict(finish.error.details)
                    assert action_key != key
                    assert saves["action"] == [(command, action_key)] * (2 if stage == "action" else 1)
                    assert all(value is command for value, _key in saves["action"])

            def check():
                frozen()
                assert saves["stop_receipts"][1].kind is DbOutcomeKind.COMPLETED
                events = _group(owned, finish, key, "operation_runs", world.stop_id, 6)
                if first_events is not None:
                    assert events == first_events
                assert owned.connection.execute("SELECT status,attempts_used,retry_wait_required"
                    " FROM operation_runs WHERE id=?", (world.stop_id,)).fetchone() == (6, 1, 0)
                if consumer == "ordinary":
                    command, action_key = saves["action"][0]
                    assert saves["action_receipts"][-1].kind is DbOutcomeKind.COMPLETED
                    events = _group(owned, command, action_key, "actions", world.action_id, 4)
                    if first_action is not None:
                        assert events == first_action
                    assert owned.connection.execute("SELECT status,error_code FROM actions WHERE id=?",
                        (world.action_id,)).fetchone() == (4, registered_error("recording_stop_failed")["action_error_id"])
                else:
                    assert owned.connection.execute("SELECT status,cancel_requested,error_code FROM actions WHERE id=?",
                        (world.action_id,)).fetchone() == (2, 1, None)
                assert _rows(owned, "operation_attempts") == before_attempts
                assert _activity(owned, world.action_id) == before_activity
                assert _rows(owned, "history_events")[:len(before_history)] == before_history
                assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)

            await world.replay(entrance, check, ready=lambda: len(saves["stop_receipts"]) == 2
                and saves["stop_receipts"][-1].kind is DbOutcomeKind.COMPLETED
                and (consumer == "canceled" or bool(saves["action_receipts"])
                     and saves["action_receipts"][-1].kind is DbOutcomeKind.COMPLETED),
                failure=gate is not None)
            if gate is not None:
                frozen(action_reached=stage == "action")
                assert gate.hits == 1
                assert saves[stage + "_receipts"][1].kind is (
                    DbOutcomeKind.UNKNOWN if confirmation_unknown else DbOutcomeKind.ROLLED_BACK)
                assert tuple(owned.connection.iterdump()) == before
            else:
                check()
                if consumer == "canceled":
                    # 保存门只交付原 STOP，随后通过正式工厂和处理器推进取消终态。
                    runtime = await world.producer(devices={})
                    await capture_handler("camera_record")(world.action_id, runtime)
                    assert owned.connection.execute("SELECT status,cancel_requested,error_code FROM actions WHERE id=?",
                        (world.action_id,)).fetchone() == (6, 1, None)
                    assert saves["action"] == []
                    assert _rows(owned, "operation_attempts") == before_attempts
            assert len(world.driver.calls) == len(world.stopper.calls) == 1
    finally:
        world.close()


@pytest.mark.parametrize("consumer,stage", _MODES)
@pytest.mark.parametrize("after_commit", [False, True], ids=["commit-before", "commit-after"])
@pytest.mark.parametrize("entrance", _ENTRANCES)
async def test_default_entry_confirms_original_stop_close_before_candidates(
        tmp_path, monkeypatch, capture_resources, consumer, stage, after_commit, entrance):
    await _exercise(tmp_path, monkeypatch, capture_resources, consumer, stage, entrance, after_commit=after_commit)


@pytest.mark.parametrize("consumer,stage", _MODES)
@pytest.mark.parametrize("confirmation_unknown", [False, True], ids=["rolled-back", "unknown"])
@pytest.mark.parametrize("entrance", ["normal", "restricted-cancel"])
async def test_default_entry_keeps_stop_close_when_original_key_is_unavailable(
        tmp_path, monkeypatch, capture_resources, consumer, stage, confirmation_unknown, entrance):
    await _exercise(tmp_path, monkeypatch, capture_resources, consumer, stage, entrance,
        confirmation_unknown=confirmation_unknown)

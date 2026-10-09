"""STOP 耗尽收尾的原完整申请在独立保存事务失败后仍由会话持有。"""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from camctl.capture.handlers import _stop_call, capture_handler
from camctl.contracts.values import ConsistencyError
from camctl.contracts.workflow_errors import registered_error
from camctl.devices.bindings import check_binding
from camctl.operations.attempts import RunOutcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import saved_transaction_events

from .test_call_result_save import (
    ReturnedCall, _assert_actual_saved, _outcome, _started,
)
from .test_cancel_timelapse_exhaustion_files import _apply_public_cancel
from .test_capture_contract import _NOW
from .test_recording_start_runtime import _world


pytestmark = pytest.mark.asyncio


async def stop_close_world(tmp_path, consumer):
    """实际 START 和尾次 STOP 均已保存，下一次推进才形成耗尽收尾。"""
    if consumer not in ("ordinary", "canceled"):
        raise ValueError(f"未知停止收尾消费者: {consumer}")
    owned = _world(tmp_path)
    try:
        runtime = await _started(owned)
        wall, mono = [_NOW + 60_000_000], [67_000_000_000]
        runtime.wall_us = lambda: wall[0]
        runtime.monotonic_ns = lambda: mono[0]
        stopper = ReturnedCall(_outcome("stop"))
        runtime.stopper = stopper
        step = await _stop_call(runtime, runtime.action(12))
        assert step.phase == "stop_failed"
        call, = stopper.calls
        _assert_actual_saved(owned, call.ticket, assumed=False, confirmed=False,
                             occurred_at=wall[0])
        assert owned.connection.execute(
            "SELECT status,attempts_used,retry_wait_required FROM operation_runs"
            " WHERE responsibility_key='stop/12'"
        ).fetchone() == (2, 1, 1)
        if consumer == "canceled":
            _apply_public_cancel(owned, 12, wall[0] + 1)
        assert runtime.action(12)["status"] == 2
        assert runtime.pending_start_results == runtime.pending_capture_completions == {}
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts WHERE status=1 OR result_event_id IS NULL"
        ).fetchone() == (0,)
        wall[0] += 5_000_000
        mono[0] += 5_000_000_000
        return SimpleNamespace(
            owned=owned, runtime=runtime, stopper=stopper, wall=wall, mono=mono,
            path=Path(tmp_path) / "state.db", metadata=owned.metadata,
            action_id=12, stop_id=call.ticket.run_id, formed_at=wall[0], consumer=consumer)
    except BaseException:
        owned.connection.close()
        raise


class StopCloseFault:
    """只在原 STOP 或普通动作的真实终态投影及其 COMMIT 边界报错。"""

    def __init__(self, connection, world, stage, phase):
        self.connection = connection
        self.table, self.identity, self.status = (
            ("operation_runs", world.stop_id, 6) if stage == "stop"
            else ("actions", world.action_id, 4))
        self.phase = phase
        self.targeted = self.failed = False
        self.commit_calls = self.rollback_calls = 0
        self.transaction_before = self.target_before = None

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, parameters=()):
        if sql == "BEGIN IMMEDIATE":
            self.transaction_before = tuple(self.connection.iterdump())
        if sql == "COMMIT" and self.targeted and not self.failed:
            self.commit_calls += 1
            self.failed = True
            if self.phase == "commit_after":
                self.connection.execute(sql, parameters)
            raise sqlite3.OperationalError("停止收尾的原事务提交不能可靠核实")
        result = self.connection.execute(sql, parameters)
        if sql == "ROLLBACK" and self.failed:
            self.rollback_calls += 1
        if (not self.targeted and sql.startswith(f"UPDATE {self.table} SET")
                and parameters[-1] == self.identity
                and self.connection.execute(
                    f"SELECT status FROM {self.table} WHERE id=?", (self.identity,)
                ).fetchone() == (self.status,)):
            self.targeted = True
            self.target_before = self.transaction_before
            if self.phase == "projection":
                self.failed = True
                raise sqlite3.OperationalError("停止收尾的原终态投影保存失败")
        return result


def _attempts(owned):
    return owned.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall()


def _activity(owned):
    return owned.connection.execute(
        "SELECT activity_state,occupancy_state,started_at"
        " FROM device_activities WHERE action_id=12").fetchone()


def _assert_original_group(owned, key, occurred_at, table, identity, status):
    events = saved_transaction_events(owned.connection, key)
    assert events is not None
    assert {event["occurred_at"] for event in events} == {occurred_at}
    assert len({event["transaction"].txn_id for event in events}) == 1
    changes = {(row["table"], row["id"]): row["after"]["values"]
               for event in events for row in event["body"]["rows"]}
    assert changes[(table, identity)]["status"] == status
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(key),)
    ).fetchone() == (1,)
    return events


async def _assert_save_recovery(tmp_path, monkeypatch, consumer, stage, phase):
    world = await stop_close_world(tmp_path, consumer)
    owned, runtime = world.owned, world.runtime
    stop_save, action_save = OperationRepository.finish_stale_runs, CaptureRepository.finish_capture
    stop_inputs, stop_receipts, action_inputs, action_receipts, order = [], [], [], [], []
    before_attempts, before_activity = _attempts(owned), _activity(owned)
    before_history = owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()
    fault = StopCloseFault(owned.connection, world, stage, phase)

    def save_stop(repository, finish, key, current):
        relevant = finish.responsibility_keys == ("stop/12",)
        if relevant:
            stop_inputs.append((finish, key))
            order.append("stop-save")
        receipt = stop_save(repository, finish, key, current)
        if relevant:
            stop_receipts.append(receipt)
        return receipt

    def save_action(repository, request, key, current):
        relevant = (request.action_id == 12 and request.failure is not None
                    and request.failure.code == "recording_stop_failed")
        if relevant:
            action_inputs.append((request, key))
            order.append("action-save")
        receipt = action_save(repository, request, key, current)
        if relevant:
            action_receipts.append(receipt)
        return receipt

    monkeypatch.setattr(OperationRepository, "finish_stale_runs", save_stop)
    monkeypatch.setattr(CaptureRepository, "finish_capture", save_action)
    runtime.owned = replace(owned, connection=fault)
    try:
        # 删除第一次提交前的外层持有，会丢失 STOP 或尚未提交的动作成员。
        with pytest.raises((ConsistencyError, AssertionError)):
            await capture_handler("camera_record")(12, runtime)
        assert fault.targeted and fault.failed and fault.target_before is not None
        receipt = stop_receipts[0] if stage == "stop" else action_receipts[0]
        if phase == "projection":
            assert receipt.kind is DbOutcomeKind.ROLLED_BACK
            assert fault.rollback_calls == 1
            assert tuple(owned.connection.iterdump()) == fault.target_before
        else:
            assert receipt.kind is DbOutcomeKind.UNKNOWN and fault.commit_calls == 1
        held = runtime.pending_capture_completions
        pending = held.get(12)
        assert pending is not None, "STOP 收尾保存未核实后必须持有原完整外层申请"
        assert hasattr(pending.request, "finish"), "普通动作成员保存失败后也必须保留原 STOP 完整申请"
        request = pending.request
        finish, stop_key = stop_inputs[0]
        assert request.action_id == 12 and pending.key == stop_key
        assert request.finish is finish
        assert finish.responsibility_keys == ("stop/12",)
        assert finish.status is RunOutcome.UNCONFIRMED
        assert finish.occurred_at == world.formed_at
        assert finish.error.code == "recording_stop_failed"
        expected_details = {"activity_id": "12", "operation_run_id": str(world.stop_id)}
        assert dict(finish.error.details) == expected_details
        action_finish, action_key = request.action_finish, request.action_key
        if consumer == "canceled":
            assert action_finish is action_key is None and action_inputs == []
        else:
            assert action_finish.action_id == 12 and action_finish.drafts == ()
            assert action_finish.occurred_at == world.formed_at
            assert action_finish.failure.code == "recording_stop_failed"
            assert dict(action_finish.failure.details) == expected_details
            assert action_key is not None and action_key != stop_key
            assert action_inputs == ([(action_finish, action_key)] if stage == "action" else [])
        anchors = dict(runtime.retry_gate.anchors)

        # 关闭旧连接排除迟到提交，用新的可靠连接核实两个独立原键。
        owned.connection.close()
        owned = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
        assert owned.metadata == world.metadata
        first_stop_events = saved_transaction_events(owned.connection, stop_key)
        first_action_events = (None if action_key is None else
            saved_transaction_events(owned.connection, action_key))
        after = phase == "commit_after"
        assert (first_stop_events is not None) is (stage == "action" or after)
        assert (first_action_events is not None) is (stage == "action" and after)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=12").fetchone() == (
            4 if stage == "action" and after else 2,)
        if not after:
            assert tuple(owned.connection.iterdump()) == fault.target_before
        runtime.owned = owned
        runtime.stop_config = replace(runtime.stop_config, max_attempts=9,
            timeout_s=Decimal("8"), retry_interval_s=Decimal("30"))
        runtime.query_config = replace(runtime.query_config, max_attempts=9)

        def changed_binding(binding):
            order.append("binding-check")
            return check_binding(binding, SimpleNamespace(
                devices={"cam-1": {"driver": "replacement-driver"}}))

        runtime.binding_check = changed_binding
        world.wall[0] += 100_000_000
        world.mono[0] += 100_000_000_000
        order.clear()
        await capture_handler("camera_record")(12, runtime)

        assert all(saved is finish and key == stop_key for saved, key in stop_inputs)
        if stage == "stop":
            assert len(stop_inputs) == 2 and stop_receipts[-1].kind is DbOutcomeKind.COMPLETED
        if "binding-check" in order:
            assert order.index("stop-save") < order.index("binding-check")
        assert runtime.pending_capture_completions is held and held == {}
        assert runtime.retry_gate.anchors == {
            responsibility: anchor for responsibility, anchor in anchors.items()
            if responsibility != "stop/12"}
        stop_events = _assert_original_group(
            owned, stop_key, world.formed_at, "operation_runs", world.stop_id, 6)
        if first_stop_events is not None:
            assert stop_events == first_stop_events
        assert owned.connection.execute(
            "SELECT status,attempts_used,retry_wait_required FROM operation_runs"
            " WHERE id=?", (world.stop_id,)).fetchone() == (6, 1, 0)
        if consumer == "ordinary":
            assert len(action_inputs) == (2 if stage == "action" else 1)
            assert all(saved is action_finish and key == action_key for saved, key in action_inputs)
            assert action_receipts[-1].kind is DbOutcomeKind.COMPLETED
            action_events = _assert_original_group(
                owned, action_key, world.formed_at, "actions", 12, 4)
            assert stop_events[0]["transaction"].txn_id != action_events[0]["transaction"].txn_id
            if first_action_events is not None:
                assert action_events == first_action_events
            assert owned.connection.execute("SELECT status,error_code FROM actions WHERE id=12").fetchone() == (
                4, registered_error("recording_stop_failed")["action_error_id"])
        else:
            assert action_inputs == []
            assert owned.connection.execute(
                "SELECT status,cancel_requested,error_code FROM actions WHERE id=12").fetchone() == (6, 1, None)
        assert _attempts(owned) == before_attempts
        assert _activity(owned) == before_activity
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
        assert owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()[:len(before_history)] == before_history
        assert len(runtime.driver.calls) == len(world.stopper.calls) == 1
        completed = tuple(owned.connection.iterdump())
        input_counts = (len(stop_inputs), len(action_inputs))
        await capture_handler("camera_record")(12, runtime)
        assert tuple(owned.connection.iterdump()) == completed
        assert (len(stop_inputs), len(action_inputs)) == input_counts
    finally:
        owned.connection.close()


@pytest.mark.parametrize("consumer,stage", [
    ("ordinary", "stop"), ("ordinary", "action"), ("canceled", "stop"),
])
@pytest.mark.parametrize("after_commit", [False, True], ids=["commit-before", "commit-after"])
async def test_unknown_stop_close_keeps_full_request_before_current_binding_or_terminal_return(
        tmp_path, monkeypatch, consumer, stage, after_commit):
    await _assert_save_recovery(
        tmp_path, monkeypatch, consumer, stage, "commit_after" if after_commit else "commit_before")


@pytest.mark.parametrize("consumer,stage", [
    ("ordinary", "stop"), ("ordinary", "action"), ("canceled", "stop"),
])
async def test_rolled_back_stop_close_preserves_both_original_members_and_times(
        tmp_path, monkeypatch, consumer, stage):
    await _assert_save_recovery(tmp_path, monkeypatch, consumer, stage, "projection")

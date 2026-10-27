"""START 原结果保存责任、复合事务和已结束动作的旧调用恢复。"""

from dataclasses import replace
from contextlib import closing
from decimal import Decimal
from types import SimpleNamespace
import sqlite3

import pytest

from camctl.capture.handlers import capture_handler
from camctl.capture.models import ActivityObservationSave
from camctl.capture.recovery import RecoveryBoundary
from camctl.capture.residual import residual_flow
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.operations.attempts import (
    AttemptConfig, AttemptFinish, AttemptIntent, AttemptTarget, OperationKind,
    QueryPurpose, RunFinish, RunOutcome, StaleRunFinish,
)
from camctl.operations.models import AttemptStatus, EffectState, ErrorValue
from camctl.operations.validation import validate_outcome
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import FinishCanceledCapture
from camctl.persistence.repositories.scheduling import GrantRequest, SchedulingRepository
from camctl.scheduling.rules import LaunchWindow

from .test_cancel_late_start import _cancel
from .test_capture_contract import _NOW
from .test_recording_start_runtime import (
    _START_EVIDENCE, StartDriver, configured, _result, _start, _world,
)

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("kind", [DbOutcomeKind.ROLLED_BACK, DbOutcomeKind.UNKNOWN])
async def test_start_grant_state_failure_stops_before_device_call(tmp_path, kind, configured):
    owned = _world(tmp_path)
    try:
        class UnavailableGrant(SchedulingRepository):
            def grant_start(self, *args, **kwargs):
                return DbOutcome(kind=kind, error=ConsistencyError("授予事务不可靠"))

        driver = StartDriver(_result(confirmed=True))
        runtime = await configured(owned, driver)
        runtime.scheduling = UnavailableGrant()
        with pytest.raises(ConsistencyError):
            await capture_handler("camera_record")(12, runtime)
        assert driver.calls == []
        assert owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (0,)
    finally:
        owned.connection.close()


class ResultWriteFault:
    """真实 SQLite 写边界一次故障；保留实际提交前后分区。"""

    def __init__(self, real, driver, mode, state=None):
        self.real, self.driver, self.mode = real, driver, mode
        self.state = state if state is not None else {
            "armed": True, "enabled": True, "result_keys": [], "rolled_back_images": []}

    @property
    def armed(self):
        return self.state["armed"]

    @armed.setter
    def armed(self, value):
        self.state["armed"] = value

    @property
    def result_keys(self):
        return self.state["result_keys"]

    @property
    def rolled_back_images(self):
        return self.state["rolled_back_images"]

    def __getattr__(self, name):
        return getattr(self.real, name)

    def close(self):
        path = self.real.execute("PRAGMA database_list").fetchone()[2]
        record = self.mode == "commit_before" and not self.armed
        self.real.close()
        if record:
            with closing(sqlite3.connect(path)) as connection:
                self.rolled_back_images.append(tuple(connection.iterdump()))

    def execute(self, statement, params=()):
        result_phase = bool(self.driver.calls)
        if result_phase and statement.startswith("INSERT INTO history_transactions"):
            self.result_keys.extend(value for value in params
                                    if isinstance(value, str) and len(value) == 32)
        fail = result_phase and self.armed and (
            (self.mode in ("commit_before", "commit_after") and statement == "COMMIT")
            or (self.mode == "projection" and statement.startswith("UPDATE device_activities SET")))
        if fail:
            self.armed = False
            if self.mode == "commit_after":
                self.real.execute(statement, params)
            raise sqlite3.OperationalError(f"确定注入的 {self.mode} 故障")
        result = self.real.execute(statement, params)
        if result_phase and statement == "ROLLBACK":
            self.rolled_back_images.append(tuple(self.real.iterdump()))
        return result


async def test_unresolved_result_save_keeps_original_outcome_and_identity_for_same_session(tmp_path, configured, monkeypatch):
    owned = _world(tmp_path)
    try:
        actual = _result(confirmed=True)
        driver = StartDriver(actual)

        class PersistentFault(ResultWriteFault):
            def execute(self, statement, params=()):
                if self.state["enabled"] and driver.calls and statement == "COMMIT":
                    raise sqlite3.OperationalError("结果提交持续不可核实")
                return super().execute(statement, params)

        proxies = []
        state = {"armed": True, "enabled": True, "result_keys": [], "rolled_back_images": []}
        def wrap(connection):
            proxy = PersistentFault(connection.connection, driver, "commit_before", state)
            proxies.append(proxy)
            return replace(connection, connection=proxy)
        runtime = await configured(owned, driver, connection_wrap=wrap)
        requests = []
        save = runtime.capture.finish_start_result
        def observe(finish, observation, key, connection, **kwargs):
            requests.append((finish, observation, key, kwargs))
            return save(finish, observation, key, connection, **kwargs)
        monkeypatch.setattr(runtime.capture, "finish_start_result", observe)
        deadlines = []
        runtime.on_recording_deadline = lambda action_id, deadline: deadlines.append((action_id, deadline))
        with pytest.raises(ConsistencyError):
            await capture_handler("camera_record")(12, runtime)
        assert deadlines == []
        assert len(requests) == 2 and requests[0] == requests[1]
        finish, observation, key, _ = requests[0]
        assert finish.outcome.outcome == actual
        assert finish.ticket.responsibility_key == "start/12"
        assert observation.started_at == _NOW
        assert len(driver.calls) == 1
        original_anchor = 7_000_000_000
        state["enabled"] = False
        state["armed"] = False
        runtime.wall_us = lambda: _NOW + 5_000_000
        runtime.monotonic_ns = lambda: original_anchor + 1_000_000_000
        await capture_handler("camera_record")(12, runtime)
        assert len(requests) == 3 and requests[2] == requests[0]
        assert len(driver.calls) == 1
        assert owned.connection.execute(
            "SELECT started_at FROM device_activities WHERE action_id=12").fetchone() == (_NOW,)
        assert (await runtime.recording_state.recording_state(12)).stop_target_ns == (
            original_anchor + 60_000_000_000)
        assert deadlines[0] == (12, original_anchor + 60_000_000_000)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("mode", ["projection", "commit_before", "commit_after"])
async def test_actual_start_result_is_verified_and_resaved_with_same_key_without_resend(tmp_path, mode, configured):
    owned = _world(tmp_path)
    try:
        before_result = []
        driver = StartDriver(_result(confirmed=True), returned=lambda: before_result.append(
            tuple(owned.connection.iterdump())))
        proxies = []
        state = {"armed": True, "enabled": True, "result_keys": [], "rolled_back_images": []}
        def wrap(connection):
            proxy = ResultWriteFault(connection.connection, driver, mode, state)
            proxies.append(proxy)
            return replace(connection, connection=proxy)
        runtime = await configured(owned, driver, connection_wrap=wrap)
        proxy = proxies[0]
        await capture_handler("camera_record")(12, runtime)
        assert len(driver.calls) == 1
        assert _start(owned)[1:] == (3, 1, 0)
        assert owned.connection.execute(
            "SELECT status, effect_state FROM operation_attempts").fetchone() == (2, 3)
        assert owned.connection.execute(
            "SELECT started_at FROM device_activities WHERE action_id = 12").fetchone() == (_NOW,)
        assert len(set(proxy.result_keys)) == 1
        assert owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (1,)
        if mode in ("projection", "commit_before"):
            assert proxy.rolled_back_images == before_result
    finally:
        owned.connection.close()


async def test_dispatch_prevented_result_and_expiration_share_transaction(tmp_path, configured):
    owned = _world(tmp_path)
    try:
        wall = [_NOW]
        driver = StartDriver(_result(confirmed=True))
        runtime = await configured(owned, driver, wall=wall)

        class ExpiredAfterGrant(SchedulingRepository):
            def grant_start(self, *args, **kwargs):
                result = super().grant_start(*args, **kwargs)
                wall[0] += 60_000_001
                return result

        runtime.scheduling = ExpiredAfterGrant()
        await capture_handler("camera_record")(12, runtime)
        attempt_txn = owned.connection.execute(
            "SELECT e.transaction_id FROM operation_attempts a"
            " JOIN history_events e ON e.id = a.result_event_id").fetchone()[0]
        expired_txn = owned.connection.execute(
            "SELECT transaction_id FROM history_events WHERE event_type = 8 AND"
            " json_extract(body_json, '$.reason') = 3").fetchone()[0]
        assert attempt_txn == expired_txn
        assert driver.calls == []
    finally:
        owned.connection.close()


def _grant(owned):
    request = GrantRequest(device_id="cam-1", action_id=12,
        window=LaunchWindow(_NOW, _NOW + 60_000_000), trusted_wall_now=_NOW,
        config=AttemptConfig(3, Decimal("0.75"), Decimal("1")), occurred_at=_NOW)
    receipt = SchedulingRepository().grant_start(request, new_operation_key(), owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    assert receipt.value.ticket is not None, receipt.value.reason
    return receipt.value.ticket


def _joint_confirm(owned):
    ticket = _grant(owned)
    finish = AttemptFinish(ticket=ticket,
        outcome=validate_outcome(ticket, _result(confirmed=True), _START_EVIDENCE),
        occurred_at=_NOW, run_finish=RunFinish(RunOutcome.SUCCEEDED))
    observe = ActivityObservationSave(12, _NOW, sent_at=_NOW, started_at=_NOW,
                                      dispatch_state=3, activity_state=2)
    return finish, observe


@pytest.mark.parametrize("changed", ["result_time", "observation_time", "observation", "run_result"])
async def test_joint_start_same_key_rejects_changed_original_input(tmp_path, changed, configured):
    owned = _world(tmp_path)
    try:
        runtime = await configured(owned, StartDriver(_result(confirmed=True)))
        finish, observation = _joint_confirm(owned)
        key = new_operation_key()
        first = runtime.capture.finish_start_result(finish, observation, key, owned)
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        before = tuple(owned.connection.iterdump())
        if changed == "result_time":
            finish = replace(finish, occurred_at=_NOW + 1)
        elif changed == "observation_time":
            observation = replace(observation, occurred_at=_NOW + 1)
        elif changed == "observation":
            observation = replace(observation, started_at=_NOW + 1)
        else:
            finish = replace(finish, run_finish=RunFinish(
                RunOutcome.FAILED, ErrorValue("device_start_failed", "execution")))
        again = runtime.capture.finish_start_result(finish, observation, key, owned)
        assert again.kind is DbOutcomeKind.ROLLED_BACK
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


async def test_joint_query_same_key_rejects_changed_original_start_segment(tmp_path, configured):
    owned = _world(tmp_path)
    try:
        runtime = await configured(owned, StartDriver(_result()))
        ticket = _grant(owned)
        result = runtime.operations.finish_attempt(AttemptFinish(ticket,
            validate_outcome(ticket, _result(error=ErrorValue("transport_timeout", "transport")),
                             _START_EVIDENCE), _NOW), new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        receipt = runtime.operations.begin_attempt(AttemptIntent(
            operation="query", action_id=12, kind=OperationKind.QUERY_ACTIVITY,
            target=AttemptTarget(activity_id=12), query_purpose=QueryPurpose.START_CONFIRMATION,
            config=AttemptConfig(2, Decimal("0.5"), Decimal("1")), occurred_at=_NOW),
            new_operation_key(), owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        from .test_recording_start_runtime import StateQuery

        response = await StateQuery(active=True).query_state(None)
        finish = AttemptFinish(receipt.value.ticket,
            validate_outcome(receipt.value.ticket, response.outcome, _START_EVIDENCE),
            _NOW, run_finish=RunFinish(RunOutcome.SUCCEEDED))
        observation = ActivityObservationSave(12, _NOW, started_at=_NOW,
                                              dispatch_state=3, activity_state=2)
        original = StaleRunFinish(("start/12",), RunOutcome.SUCCEEDED, _NOW)
        key = new_operation_key()
        saved = runtime.capture.finish_start_result(
            finish, observation, key, owned, start_finish=original)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        before = tuple(owned.connection.iterdump())
        changed = replace(original, status=RunOutcome.UNCONFIRMED,
                          error=ErrorValue("result_unconfirmed", "device"))
        again = runtime.capture.finish_start_result(
            finish, observation, key, owned, start_finish=changed)
        assert again.kind is DbOutcomeKind.ROLLED_BACK
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("boundary,old_h", [
    (RecoveryBoundary.HOST_LOCAL_SETTLED, True),
    (RecoveryBoundary.UNCONFIRMED, True),
    (RecoveryBoundary.HOST_LOCAL_SETTLED, False),
])
async def test_residual_consumer_finds_unfinished_old_start_under_terminal_action(tmp_path, boundary, old_h, configured):
    owned = _world(tmp_path)
    try:
        driver = StartDriver(_result(confirmed=True))
        original = await configured(owned, driver)
        _grant(owned)
        horizon = owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0]
        _cancel(owned, 12)
        ended = original.capture.finish_canceled_capture(
            FinishCanceledCapture(12, _NOW), new_operation_key(), owned)
        assert ended.kind is DbOutcomeKind.COMPLETED, ended.error
        recovery = EvidenceRegistry((EvidenceContract(
            "adb_foreground_recovery", 1, "control", frozenset()),))

        runtime = await configured(owned, driver)
        runtime.recovery_boundary = boundary
        runtime.recovery_max_event_id = horizon if old_h else 1
        runtime.recovery_evidence_for = lambda saved, operation: (
            recovery if saved.driver_id == "camctl-adb" and operation == "control" else None)

        async def factory(database, device_id):
            return runtime

        context = SimpleNamespace(
            database=runtime.database,
            clock=SimpleNamespace(utc_micros=lambda: _NOW))
        await residual_flow(factory)(context)
        assert driver.calls == []
        assert _start(owned)[1:] == (5, 1, 0)
        assert owned.connection.execute("SELECT status FROM actions WHERE id = 12").fetchone() == (6,)
        attempt = owned.connection.execute(
            "SELECT status, effect_state, result_json FROM operation_attempts").fetchone()
        if boundary is RecoveryBoundary.HOST_LOCAL_SETTLED and old_h:
            assert attempt[:2] == (4, 1)
            import json

            result = json.loads(attempt[2])
            assert result["settlement"] == {"basis": "assumed", "evidence": {
                "type": "adb_foreground_recovery", "version": 1, "data": {}}}
            assert result["observations"] == [] and "call_info" not in result
        else:
            assert attempt == (1, 1, None)
    finally:
        owned.connection.close()

"""录像真实处理入口的启动结果、重试资格和确认锚点。

真实授予、结果和活动仓储组合，设备替身只返回正式 CallOutcome。
预期直接来自 START 流程、调用结果及录像计时契约。
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest

from camctl.capture.handlers import capture_handler
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import (
    AttemptStatus, CallInfo, CallOutcome, EffectState, ErrorValue, EvidenceValue,
    Settlement, SettlementBasis,
)
from camctl.persistence.repositories.scheduling import SchedulingRepository

from .test_capture_contract import _NOW, _RECORD, _environment, _runtime

pytestmark = pytest.mark.asyncio
_ANCHOR = 7_000_000_000

_START_EVIDENCE = EvidenceRegistry((
    EvidenceContract("operation_returned", 1, "control", frozenset()),
    EvidenceContract("start_confirmed", 1, "control", frozenset({"activity_id"}),
                     identity_field="activity_id"),
    EvidenceContract("dispatch_prevented", 1, "control", frozenset()),
    EvidenceContract("adb_foreground_assumption", 1, "control", frozenset({"terminate_grace_s"})),
    EvidenceContract("query_returned", 1, "query", frozenset()),
    EvidenceContract("activity_status", 1, "query", frozenset({"activity_id"})),
))


def _world(tmp_path):
    owned = _environment(tmp_path, _RECORD)
    # 录像定义和公共原输入均采用正式结构；长窗口让重试测试只改变
    # 所要验证的间隔与预算条件。
    params = {"type": "ordinary", "duration_s": 60}
    owned.connection.execute(
        "UPDATE actions SET input_fields_json = ?, effective_params_json = ?,"
        " max_delay_ms = 60000 WHERE id = 12",
        (json.dumps({"params": params, "policy": {"max_delay_ms": 60000}}),
         json.dumps(params)))
    return owned


def _result(*, confirmed=False, no_effect=False, assumed=False, error=None):
    observations = (DeviceObservation("start_confirmed", 1, {"activity_id": "12"}),) \
        if confirmed else ()
    return CallOutcome(
        status=AttemptStatus.FAILED if error is not None else AttemptStatus.SUCCEEDED,
        error=error,
        effect=EffectState.CONFIRMED if confirmed else (
            EffectState.NO_EFFECT if no_effect else EffectState.UNKNOWN),
        settlement=Settlement(
            SettlementBasis.ASSUMED if assumed else SettlementBasis.OBSERVED,
            EvidenceValue("adb_foreground_assumption" if assumed else "operation_returned",
                          1, {"terminate_grace_s": Decimal("0.25")} if assumed else {})),
        observations=observations,
        call_info=CallInfo(local_signal=9) if assumed else CallInfo(local_exit_code=0),
    )


class StartDriver:
    def __init__(self, outcome, *, returned=None):
        self.outcome = outcome
        self.returned = returned
        self.calls = []

    async def control(self, request):
        self.calls.append(request)
        if self.returned is not None:
            self.returned()
        return DeviceCallResult.from_outcome(self.outcome)


class StateQuery:
    def __init__(self, *, active=False):
        self.active = active
        self.calls = []

    async def query_state(self, request):
        self.calls.append(request)
        return DeviceCallResult.from_outcome(CallOutcome(
            status=AttemptStatus.SUCCEEDED if self.active else AttemptStatus.FAILED,
            error=None if self.active else ErrorValue("transport_timeout", "transport"),
            effect=EffectState.CONFIRMED if self.active else EffectState.UNKNOWN,
            settlement=Settlement(SettlementBasis.OBSERVED,
                                  EvidenceValue("query_returned", 1, {})),
            observations=(DeviceObservation("activity_status", 1, {"activity_id": "12"}),)
                         if self.active else (),
        ))


def _configured(owned, driver, *, wall=None, mono=None, maximum=3):
    wall = [_NOW] if wall is None else wall
    mono = [_ANCHOR] if mono is None else mono
    runtime = _runtime(owned, driver=driver)
    runtime.evidence = _START_EVIDENCE
    runtime.wall_us = lambda: wall[0]
    runtime.monotonic_ns = lambda: mono[0]
    runtime.start_config = AttemptConfig(maximum, Decimal("0.75"), Decimal("1"))
    runtime.query_config = AttemptConfig(2, Decimal("0.5"), Decimal("1"))
    return runtime


def _start(owned):
    return owned.connection.execute(
        "SELECT id, status, attempts_used, retry_wait_required FROM operation_runs"
        " WHERE responsibility_key = 'start/12'").fetchone()


async def test_confirmed_with_error_keeps_start_success_and_actual_confirmation(tmp_path):
    owned = _world(tmp_path)
    try:
        outcome = _result(confirmed=True, error=ErrorValue(
            "transport_timeout", "transport", {"response_received": True}))
        driver = StartDriver(outcome)
        runtime = _configured(owned, driver)
        await capture_handler("camera_record")(12, runtime)
        assert _start(owned)[1:] == (3, 1, 0)
        assert owned.connection.execute(
            "SELECT activity_state, started_at FROM device_activities WHERE action_id = 12"
        ).fetchone() == (2, _NOW)
        attempt = owned.connection.execute(
            "SELECT status, effect_state, error_json FROM operation_attempts"
        ).fetchone()
        assert attempt[:2] == (3, 3)
        assert json.loads(attempt[2]) == {
            "code": "transport_timeout", "stage": "transport",
            "details": {"response_received": True}}
        state = runtime.recording_state.recording_state(12)
        assert state.started_confirmed and state.stop_target_ns == _ANCHOR + 60_000_000_000
        await capture_handler("camera_record")(12, runtime)
        assert len(driver.calls) == 1
    finally:
        owned.connection.close()


async def test_reliable_permanent_start_rejection_finishes_without_using_remaining_attempts(tmp_path):
    owned = _world(tmp_path)
    try:
        driver = StartDriver(_result(no_effect=True, error=ErrorValue(
            "device_start_failed", "execution", {"operation_run_id": "1"})))
        runtime = _configured(owned, driver)
        await capture_handler("camera_record")(12, runtime)
        assert _start(owned)[1:] == (4, 1, 0)
        assert owned.connection.execute(
            "SELECT status, error_code FROM actions WHERE id = 12").fetchone() == (4, 11)
        assert owned.connection.execute(
            "SELECT status, effect_state FROM operation_attempts").fetchone() == (3, 2)
        await capture_handler("camera_record")(12, runtime)
        assert len(driver.calls) == 1
    finally:
        owned.connection.close()


async def test_confirmation_anchor_precedes_database_commit_delay(tmp_path):
    owned = _world(tmp_path)
    try:
        wall, mono, returned = [_NOW], [_ANCHOR], [False]
        driver = StartDriver(_result(confirmed=True), returned=lambda: returned.__setitem__(0, True))
        runtime = _configured(owned, driver, wall=wall, mono=mono)

        def delay_commits(statement):
            if returned[0] and statement == "COMMIT":
                wall[0] += 5_000_000
                mono[0] += 5_000_000_000

        owned.connection.set_trace_callback(delay_commits)
        await capture_handler("camera_record")(12, runtime)
        assert owned.connection.execute(
            "SELECT started_at FROM device_activities WHERE action_id = 12"
        ).fetchone() == (_NOW,)
        assert runtime.recording_state.recording_state(12).stop_target_ns == \
            _ANCHOR + 60_000_000_000
    finally:
        owned.connection.set_trace_callback(None)
        owned.connection.close()


async def test_attempt_result_and_start_observation_share_complete_transaction(tmp_path):
    owned = _world(tmp_path)
    try:
        runtime = _configured(owned, StartDriver(_result(confirmed=True)))
        await capture_handler("camera_record")(12, runtime)
        attempt_txn = owned.connection.execute(
            "SELECT e.transaction_id FROM operation_attempts a"
            " JOIN history_events e ON e.id = a.result_event_id").fetchone()[0]
        observation_txn = owned.connection.execute(
            "SELECT e.transaction_id FROM history_events e,"
            " json_each(e.body_json, '$.rows') r"
            " WHERE json_extract(r.value, '$.table') = 'device_activities'"
            " AND json_extract(r.value, '$.after.values.started_at') IS NOT NULL"
        ).fetchone()[0]
        assert attempt_txn == observation_txn
    finally:
        owned.connection.close()


async def test_start_passes_original_ticket_and_current_timeout_to_driver(tmp_path):
    owned = _world(tmp_path)
    try:
        driver = StartDriver(_result(confirmed=True))
        await capture_handler("camera_record")(12, _configured(owned, driver))
        request, = driver.calls
        assert request.ticket is not None
        assert request.ticket.responsibility_key == "start/12"
        assert request.timeout_s == Decimal("0.75")
    finally:
        owned.connection.close()


async def test_assumed_result_preserves_original_settlement_and_call_info(tmp_path):
    owned = _world(tmp_path)
    try:
        driver = StartDriver(_result(assumed=True, error=ErrorValue(
            "transport_timeout", "transport", {"timeout_s": Decimal("0.75")})))
        await capture_handler("camera_record")(12, _configured(owned, driver))
        result_json, error_json = owned.connection.execute(
            "SELECT result_json, error_json FROM operation_attempts").fetchone()
        saved = json.loads(result_json)
        assert saved["settlement"] == {
            "basis": "assumed", "evidence": {"type": "adb_foreground_assumption",
            "version": 1, "data": {"terminate_grace_s": 0.25}}}
        assert saved["call_info"] == {"local_exit": {"signal": 9}}
        assert json.loads(error_json) == {"code": "transport_timeout", "stage": "transport",
                                  "details": {"timeout_s": 0.75}}
        assert _start(owned)[1:] == (2, 1, 0)
    finally:
        owned.connection.close()


async def test_no_effect_retries_same_run_and_activity_after_current_interval(tmp_path):
    owned = _world(tmp_path)
    try:
        wall, mono = [_NOW], [_ANCHOR]
        driver = StartDriver(_result(no_effect=True, error=ErrorValue("device_rejected", "device")))
        runtime = _configured(owned, driver, wall=wall, mono=mono)
        handler = capture_handler("camera_record")
        await handler(12, runtime)
        run_id = _start(owned)[0]
        assert _start(owned)[1:] == (2, 1, 1)
        assert owned.connection.execute(
            "SELECT dispatch_state, activity_state, occupancy_state FROM device_activities"
            " WHERE action_id = 12").fetchone() == (4, 1, 1)
        await handler(12, runtime)
        assert len(driver.calls) == 1
        for count in (2, 3):
            mono[0] += 1_000_000_000
            wall[0] += 1_000_000
            await handler(12, runtime)
            assert _start(owned)[0] == run_id
            assert _start(owned)[2] == count
        assert len(driver.calls) == 3
        assert _start(owned)[1:] == (4, 3, 0)
        assert owned.connection.execute("SELECT status FROM actions WHERE id = 12").fetchone() == (4,)
        assert owned.connection.execute("SELECT COUNT(*) FROM device_activities").fetchone() == (1,)
        before = tuple(owned.connection.iterdump())
        await handler(12, runtime)
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("new_limit,final_count", [(5, 5), (1, 2)])
async def test_new_session_limit_preserves_original_attempts(tmp_path, new_limit, final_count):
    owned = _world(tmp_path)
    try:
        wall, mono = [_NOW], [_ANCHOR]
        driver = StartDriver(_result(no_effect=True, error=ErrorValue("device_rejected", "device")))
        first = _configured(owned, driver, wall=wall, mono=mono, maximum=3)
        handler = capture_handler("camera_record")
        await handler(12, first)
        mono[0] += 1_000_000_000
        wall[0] += 1_000_000
        await handler(12, first)
        original = owned.connection.execute(
            "SELECT id, attempt_no, result_json FROM operation_attempts ORDER BY id").fetchall()
        assert len(original) == 2
        second = _configured(owned, driver, wall=wall, mono=mono, maximum=new_limit)
        await handler(12, second)
        for _ in range(4):
            mono[0] += 1_000_000_000
            wall[0] += 1_000_000
            await handler(12, second)
        assert _start(owned)[1:] == (4, final_count, 0)
        assert len(driver.calls) == final_count
        assert owned.connection.execute(
            "SELECT id, attempt_no, result_json FROM operation_attempts ORDER BY id LIMIT 2"
        ).fetchall() == original
    finally:
        owned.connection.close()


async def test_window_end_after_intent_prevents_dispatch_and_keeps_consumed_attempt(tmp_path):
    owned = _world(tmp_path)
    try:
        wall = [_NOW]
        driver = StartDriver(_result(confirmed=True))
        runtime = _configured(owned, driver, wall=wall)

        class EndWindowAfterGrant(SchedulingRepository):
            def grant_start(self, *args, **kwargs):
                receipt = super().grant_start(*args, **kwargs)
                wall[0] += 60_000_001
                return receipt

        runtime.scheduling = EndWindowAfterGrant()
        await capture_handler("camera_record")(12, runtime)
        assert driver.calls == []
        assert _start(owned)[1:] == (7, 1, 0)
        assert owned.connection.execute("SELECT status FROM actions WHERE id = 12").fetchone() == (5,)
        effect, result_json = owned.connection.execute(
            "SELECT effect_state, result_json FROM operation_attempts").fetchone()
        saved = json.loads(result_json)
        assert effect == 2
        assert saved["settlement"]["basis"] == "not_dispatched"
    finally:
        owned.connection.close()


async def test_unknown_start_uses_start_confirmation_before_any_resend(tmp_path):
    owned = _world(tmp_path)
    try:
        driver = StartDriver(_result(error=ErrorValue("transport_timeout", "transport")))
        runtime = _configured(owned, driver)
        query = StateQuery(active=True)
        runtime.state_query = query
        handler = capture_handler("camera_record")
        await handler(12, runtime)
        await handler(12, runtime)
        assert len(driver.calls) == 1
        assert len(query.calls) == 1
        assert owned.connection.execute(
            "SELECT query_purpose, attempts_used, status FROM operation_runs"
            " WHERE responsibility_key = 'query/start/12/12'").fetchone() == (2, 1, 3)
        assert _start(owned)[1:] == (3, 1, 0)
        assert owned.connection.execute(
            "SELECT activity_state, started_at FROM device_activities WHERE action_id = 12"
        ).fetchone() == (2, _NOW)
        assert query.calls[0].ticket.responsibility_key == "query/start/12/12"
        assert query.calls[0].timeout_s == Decimal("0.5")
    finally:
        owned.connection.close()


async def test_start_confirmation_exhaustion_keeps_unknown_occupancy_and_original_start(tmp_path):
    owned = _world(tmp_path)
    try:
        wall, mono = [_NOW], [_ANCHOR]
        driver = StartDriver(_result(error=ErrorValue("transport_timeout", "transport")))
        query = StateQuery()
        runtime = _configured(owned, driver, wall=wall, mono=mono)
        runtime.state_query = query
        handler = capture_handler("camera_record")
        await handler(12, runtime)
        for _ in range(4):
            await handler(12, runtime)
            wall[0] += 1_000_000
            mono[0] += 1_000_000_000
        assert len(driver.calls) == 1
        assert len(query.calls) == 2
        assert _start(owned)[1:] == (6, 1, 0)
        assert owned.connection.execute(
            "SELECT status FROM operation_runs WHERE responsibility_key = 'query/start/12/12'"
        ).fetchone() == (6,)
        assert owned.connection.execute(
            "SELECT status, error_code FROM actions WHERE id = 12").fetchone() == (4, 12)
        assert owned.connection.execute(
            "SELECT activity_state, occupancy_state, started_at FROM device_activities"
            " WHERE action_id = 12").fetchone() == (1, 1, None)
    finally:
        owned.connection.close()


async def test_original_start_stays_in_flight_without_second_dispatch_or_early_terminal(tmp_path):
    owned = _world(tmp_path)
    entered, released = asyncio.Event(), asyncio.Event()
    pending = None
    try:
        wall = [_NOW]

        class PendingDriver(StartDriver):
            async def control(self, request):
                self.calls.append(request)
                entered.set()
                await released.wait()
                return DeviceCallResult.from_outcome(self.outcome)

        driver = PendingDriver(_result(confirmed=True))
        runtime = _configured(owned, driver, wall=wall)
        handler = capture_handler("camera_record")
        pending = asyncio.create_task(handler(12, runtime))
        await entered.wait()
        wall[0] += 60_000_001
        await handler(12, runtime)
        assert len(driver.calls) == 1
        assert _start(owned)[1:] == (2, 1, 0)
        assert owned.connection.execute("SELECT status FROM operation_attempts").fetchone() == (1,)
        assert owned.connection.execute("SELECT status FROM actions WHERE id = 12").fetchone() == (2,)
        released.set()
        await pending
        assert _start(owned)[1:] == (3, 1, 0)
    finally:
        released.set()
        if pending is not None:
            await pending
        owned.connection.close()

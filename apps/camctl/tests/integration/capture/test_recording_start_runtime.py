"""录像真实处理入口的启动结果、重试资格和确认锚点。

真实授予、结果和活动仓储组合，设备替身只返回正式 CallOutcome。
预期直接来自 START 流程、调用结果及录像计时契约。
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from unittest.mock import create_autospec

import pytest

from camctl.capture.handlers import capture_handler
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult
from camctl.operations.attempts import AttemptConfig
from camctl.logging_runtime.events import event_logging
from camctl.logging_runtime.service import LogChannel
from camctl.operations.models import (
    AttemptStatus, CallInfo, CallOutcome, EffectState, ErrorValue, EvidenceValue,
    Settlement, SettlementBasis,
)
from camctl.persistence.repositories.scheduling import SchedulingRepository

from .test_capture_contract import _NOW

from .task_cleanup import close_observer, settle_capture_task
from ..support.task_wait import wait_for_signal
from .recording_start_fixtures import start_case

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


def _result(*, confirmed=False, no_effect=False, assumed=False, error=None, activity_id="12"):
    observations = (DeviceObservation("start_confirmed", 1, {"activity_id": activity_id}),) \
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
    def __init__(self, *, active=False, activity_id="12"):
        self.active = active
        self.activity_id = activity_id
        self.calls = []

    async def query_state(self, request):
        self.calls.append(request)
        return DeviceCallResult.from_outcome(CallOutcome(
            status=AttemptStatus.SUCCEEDED if self.active else AttemptStatus.FAILED,
            error=None if self.active else ErrorValue("transport_timeout", "transport"),
            effect=EffectState.CONFIRMED if self.active else EffectState.UNKNOWN,
            settlement=Settlement(SettlementBasis.OBSERVED,
                                  EvidenceValue("query_returned", 1, {})),
            observations=(DeviceObservation("activity_status", 1, {"activity_id": self.activity_id}),)
                         if self.active else (),
        ))


def _start(owned, action_id=12):
    return owned.connection.execute(
        "SELECT id, status, attempts_used, retry_wait_required FROM operation_runs"
        " WHERE responsibility_key = ?", (f"start/{action_id}",)).fetchone()


async def test_confirmed_with_error_keeps_start_success_and_actual_confirmation(tmp_path, start_case):
    owned = await start_case.world(tmp_path)
    try:
        outcome = _result(activity_id="1", confirmed=True, error=ErrorValue(
            "transport_timeout", "transport", {"response_received": True}))
        driver = StartDriver(outcome)
        runtime = (await start_case.runtime(owned, driver))
        await capture_handler("camera_record")(1, runtime)
        assert _start(owned, start_case.aid)[1:] == (3, 1, 0)
        assert owned.connection.execute(
            "SELECT activity_state, started_at FROM device_activities WHERE action_id = 1"
        ).fetchone() == (2, _NOW)
        attempt = owned.connection.execute(
            "SELECT status, effect_state, error_json FROM operation_attempts"
        ).fetchone()
        assert attempt[:2] == (3, 3)
        assert json.loads(attempt[2]) == {
            "code": "transport_timeout", "stage": "transport",
            "details": {"response_received": True}}
        await start_case.assert_stop_deadline(runtime, _ANCHOR + 60_000_000_000)
        await capture_handler("camera_record")(1, runtime)
        assert len(driver.calls) == 1
    finally:
        owned.connection.close()


async def test_actual_start_info_preserves_anchor_and_wait_does_not_repeat(tmp_path, start_case):
    owned = await start_case.world(tmp_path)
    try:
        mono = [_ANCHOR]
        driver = StartDriver(_result(activity_id="1", confirmed=True))
        runtime = await start_case.runtime(owned, driver, mono=mono)
        channel = create_autospec(LogChannel, instance=True, spec_set=True)

        def consume(record):
            payload = json.loads(record.message)
            if payload["stage"] == "capture.start" and payload["event"] == "returned":
                mono[0] += 5_000_000_000

        channel.slog.side_effect = consume
        with event_logging(channel):
            await capture_handler("camera_record")(start_case.aid, runtime)
            await capture_handler("camera_record")(start_case.aid, runtime)
        payloads = [json.loads(call.args[0].message) for call in channel.slog.call_args_list]
        starts = [payload for payload in payloads if payload["stage"] == "capture.start"]
        assert [payload["event"] for payload in starts] == ["started", "returned"]
        assert all((payload["level"], payload["action_id"], payload["activity_id"],
                    payload["run_id"], payload["attempt_id"]) ==
                   ("INFO", start_case.aid, str(start_case.aid), _start(owned, start_case.aid)[0], 1)
                   for payload in starts)
        assert len(driver.calls) == 1
        await start_case.assert_stop_deadline(runtime, _ANCHOR + 60_000_000_000)
    finally:
        owned.connection.close()


async def test_reliable_permanent_start_rejection_finishes_without_using_remaining_attempts(tmp_path, start_case):
    owned = await start_case.world(tmp_path)
    try:
        driver = StartDriver(_result(activity_id="1", no_effect=True, error=ErrorValue(
            "device_start_failed", "execution", {"operation_run_id": "1"})))
        runtime = (await start_case.runtime(owned, driver))
        await capture_handler("camera_record")(1, runtime)
        assert _start(owned, start_case.aid)[1:] == (4, 1, 0)
        assert owned.connection.execute(
            "SELECT status, error_code FROM actions WHERE id = 1").fetchone() == (4, 11)
        assert owned.connection.execute(
            "SELECT status, effect_state FROM operation_attempts").fetchone() == (3, 2)
        await capture_handler("camera_record")(1, runtime)
        assert len(driver.calls) == 1
    finally:
        owned.connection.close()


async def test_confirmation_anchor_precedes_database_commit_delay(tmp_path, start_case):
    owned = await start_case.world(tmp_path)
    try:
        wall, mono, returned = [_NOW], [_ANCHOR], [False]
        driver = StartDriver(_result(activity_id="1", confirmed=True), returned=lambda: returned.__setitem__(0, True))
        runtime = (await start_case.runtime(owned, driver, wall=wall, mono=mono))

        def delay_commits(statement):
            if returned[0] and statement == "COMMIT":
                returned[0] = False
                wall[0] += 5_000_000
                mono[0] += 5_000_000_000

        await runtime.database.read(lambda current: current.connection.set_trace_callback(delay_commits))
        await capture_handler("camera_record")(1, runtime)
        assert mono[0] > _ANCHOR
        assert owned.connection.execute(
            "SELECT started_at FROM device_activities WHERE action_id = 1"
        ).fetchone() == (_NOW,)
        await runtime.database.read(lambda current: current.connection.set_trace_callback(None))
        await start_case.assert_stop_deadline(runtime, _ANCHOR + 60_000_000_000)
    finally:
        await runtime.database.read(lambda current: current.connection.set_trace_callback(None))
        owned.connection.close()


async def test_attempt_result_and_start_observation_share_complete_transaction(tmp_path, start_case):
    owned = await start_case.world(tmp_path)
    try:
        runtime = (await start_case.runtime(owned, StartDriver(_result(activity_id="1", confirmed=True))))
        await capture_handler("camera_record")(1, runtime)
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


async def test_start_passes_original_ticket_and_current_timeout_to_driver(tmp_path, start_case):
    owned = await start_case.world(tmp_path)
    try:
        driver = StartDriver(_result(activity_id="1", confirmed=True))
        await capture_handler("camera_record")(1, (await start_case.runtime(owned, driver)))
        request, = driver.calls
        assert request.ticket is not None
        assert request.ticket.responsibility_key == "start/1"
        assert request.timeout_s == Decimal("0.75")
    finally:
        owned.connection.close()


async def test_two_runs_deliver_their_own_current_timeout_to_start_calls(tmp_path, start_case):
    """两次运行各自的精确超时值分别到达实际调用并保存本次依据。

    旧尝试的期限与结果不被改写；跨运行超时变化与跨运行限额变化
    同规则（见运行配置计划 RC3）。
    """
    owned = await start_case.world(tmp_path)
    try:
        wall, mono = [_NOW], [_ANCHOR]
        driver = StartDriver(_result(activity_id="1", no_effect=True, error=ErrorValue("device_rejected", "device")))
        first = (await start_case.runtime(owned, driver, wall=wall, mono=mono))
        handler = capture_handler("camera_record")
        await handler(1, first)
        assert [call.timeout_s for call in driver.calls] == [Decimal("0.75")]
        original = owned.connection.execute(
            "SELECT id, attempt_no, result_json, timeout_s_json FROM operation_attempts"
            " ORDER BY id").fetchall()
        assert Decimal(original[0][3]) == Decimal("0.75")
        second = (await start_case.reopen(owned, driver, wall=wall, mono=mono, timeout=Decimal("2.5")))
        # 跨会话恢复的等待按本次间隔从本会话首次观察重新计时。
        await handler(1, second)
        assert len(driver.calls) == 1
        mono[0] += 1_000_000_000
        wall[0] += 1_000_000
        await handler(1, second)

        assert [call.timeout_s for call in driver.calls] == [Decimal("0.75"), Decimal("2.5")]
        resumed = owned.connection.execute(
            "SELECT id, attempt_no, result_json, timeout_s_json FROM operation_attempts"
            " ORDER BY id").fetchall()
        assert resumed[0] == original[0]
        assert Decimal(resumed[1][3]) == Decimal("2.5")
    finally:
        owned.connection.close()


async def test_assumed_result_preserves_original_settlement_and_call_info(tmp_path, start_case):
    owned = await start_case.world(tmp_path)
    try:
        driver = StartDriver(_result(activity_id="1", assumed=True, error=ErrorValue(
            "transport_timeout", "transport", {"timeout_s": Decimal("0.75")})))
        await capture_handler("camera_record")(1, (await start_case.runtime(owned, driver)))
        result_json, error_json = owned.connection.execute(
            "SELECT result_json, error_json FROM operation_attempts").fetchone()
        saved = json.loads(result_json)
        assert saved["settlement"] == {
            "basis": "assumed", "evidence": {"type": "adb_foreground_assumption",
            "version": 1, "data": {"terminate_grace_s": 0.25}}}
        assert saved["call_info"] == {"local_exit": {"signal": 9}}
        assert json.loads(error_json) == {"code": "transport_timeout", "stage": "transport",
                                  "details": {"timeout_s": 0.75}}
        assert _start(owned, start_case.aid)[1:] == (2, 1, 0)
    finally:
        owned.connection.close()


async def test_no_effect_retries_same_run_and_activity_after_current_interval(tmp_path, start_case):
    owned = await start_case.world(tmp_path)
    try:
        wall, mono = [_NOW], [_ANCHOR]
        driver = StartDriver(_result(activity_id="1", no_effect=True, error=ErrorValue("device_rejected", "device")))
        runtime = (await start_case.runtime(owned, driver, wall=wall, mono=mono))
        handler = capture_handler("camera_record")
        await handler(1, runtime)
        run_id = _start(owned, start_case.aid)[0]
        assert _start(owned, start_case.aid)[1:] == (2, 1, 1)
        assert owned.connection.execute(
            "SELECT dispatch_state, activity_state, occupancy_state FROM device_activities"
            " WHERE action_id = 1").fetchone() == (4, 1, 1)
        await handler(1, runtime)
        assert len(driver.calls) == 1
        for count in (2, 3):
            mono[0] += 1_000_000_000
            wall[0] += 1_000_000
            await handler(1, runtime)
            assert _start(owned, start_case.aid)[0] == run_id
            assert _start(owned, start_case.aid)[2] == count
        assert len(driver.calls) == 3
        assert _start(owned, start_case.aid)[1:] == (4, 3, 0)
        assert owned.connection.execute("SELECT status FROM actions WHERE id = 1").fetchone() == (4,)
        assert owned.connection.execute("SELECT COUNT(*) FROM device_activities").fetchone() == (1,)
        before = tuple(owned.connection.iterdump())
        await handler(1, runtime)
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("new_limit,final_count", [(5, 5), (1, 2)])
async def test_new_session_limit_preserves_original_attempts(tmp_path, start_case, new_limit, final_count):
    owned = await start_case.world(tmp_path)
    try:
        wall, mono = [_NOW], [_ANCHOR]
        driver = StartDriver(_result(activity_id="1", no_effect=True, error=ErrorValue("device_rejected", "device")))
        first = (await start_case.runtime(owned, driver, wall=wall, mono=mono, maximum=3))
        handler = capture_handler("camera_record")
        await handler(1, first)
        mono[0] += 1_000_000_000
        wall[0] += 1_000_000
        await handler(1, first)
        original = owned.connection.execute(
            "SELECT id, attempt_no, result_json FROM operation_attempts ORDER BY id").fetchall()
        assert len(original) == 2
        second = (await start_case.reopen(owned, driver, wall=wall, mono=mono, maximum=new_limit))
        await handler(1, second)
        for _ in range(4):
            mono[0] += 1_000_000_000
            wall[0] += 1_000_000
            await handler(1, second)
        assert _start(owned, start_case.aid)[1:] == (4, final_count, 0)
        assert len(driver.calls) == final_count
        assert owned.connection.execute(
            "SELECT id, attempt_no, result_json FROM operation_attempts ORDER BY id LIMIT 2"
        ).fetchall() == original
    finally:
        owned.connection.close()


async def test_window_end_after_intent_prevents_dispatch_and_keeps_consumed_attempt(tmp_path, start_case, monkeypatch):
    owned = await start_case.world(tmp_path)
    try:
        wall = [_NOW]
        driver = StartDriver(_result(activity_id="1", confirmed=True))
        runtime = (await start_case.runtime(owned, driver, wall=wall))

        grant = SchedulingRepository.grant_start
        def end_window(repository, *args, **kwargs):
            receipt = grant(repository, *args, **kwargs)
            wall[0] += 60_000_001
            return receipt
        monkeypatch.setattr(SchedulingRepository, "grant_start", end_window)
        await capture_handler("camera_record")(1, runtime)
        assert driver.calls == []
        assert _start(owned, start_case.aid)[1:] == (7, 1, 0)
        assert owned.connection.execute("SELECT status FROM actions WHERE id = 1").fetchone() == (5,)
        effect, result_json = owned.connection.execute(
            "SELECT effect_state, result_json FROM operation_attempts").fetchone()
        saved = json.loads(result_json)
        assert effect == 2
        assert saved["settlement"]["basis"] == "not_dispatched"
    finally:
        owned.connection.close()


async def test_original_start_stays_in_flight_without_second_dispatch_or_early_terminal(tmp_path, start_case):
    owned = await start_case.world(tmp_path)
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

        driver = PendingDriver(_result(activity_id="1", confirmed=True))
        runtime = (await start_case.runtime(owned, driver, wall=wall))
        handler = capture_handler("camera_record")
        pending = asyncio.create_task(handler(1, runtime))
        await wait_for_signal(entered, pending)
        wall[0] += 60_000_001
        await handler(1, runtime)
        assert len(driver.calls) == 1
        assert _start(owned, start_case.aid)[1:] == (2, 1, 0)
        assert owned.connection.execute("SELECT status FROM operation_attempts").fetchone() == (1,)
        assert owned.connection.execute("SELECT status FROM actions WHERE id = 1").fetchone() == (2,)
        released.set()
        await asyncio.wait_for(asyncio.shield(pending), 30)
        assert _start(owned, start_case.aid)[1:] == (3, 1, 0)
    finally:
        released.set()
        try:
            await settle_capture_task(pending)
        finally:
            close_observer(owned)


def _sent_result(activity_id="12"):
    from dataclasses import replace
    return replace(_result(confirmed=True, activity_id=activity_id), observations=(
        DeviceObservation("recording_sent", 1, {"activity_id": activity_id}),))


@pytest.mark.parametrize('model,parameter_type,deadline_ns', [
    ('dji-osmo360-ii', 'osmo360ii_record', 17_000_000_000),
    ('dji-action6', 'action6_record', 18_000_000_000),
])
async def test_builtin_record_stops_at_its_frozen_deadline_without_claiming_actual_start(
        tmp_path, start_case, model, parameter_type, deadline_ns):
    from camctl.capture.models import build_capture_spec
    from camctl.devices.drivers.adb_cameras.registration import builtin_camera_contracts

    contract = next(item for item in builtin_camera_contracts() if item.driver_id == model)
    spec = build_capture_spec('camera_record',
        contract.task_factories[parameter_type]({'duration_s': 10}))
    owned = await start_case.world(tmp_path, task_factory=lambda params:
        contract.task_factories[parameter_type]({'duration_s': 10}))
    try:
        wall, mono = [_NOW], [_ANCHOR]
        driver = StartDriver(_sent_result("1"))
        runtime = (await start_case.runtime(owned, driver, wall=wall, mono=mono))

        class Stop:
            def __init__(self):
                self.calls = []

            async def stop(self, request):
                self.calls.append(request)
                return DeviceCallResult((), {'code': 'transport_timeout'})

        start_case.stopper.delegate = Stop()
        handler = capture_handler('camera_record')
        await handler(1, runtime)
        assert start_case.stopper.calls == []
        mono[0] = deadline_ns - 1
        await handler(1, runtime)
        assert start_case.stopper.calls == []
        mono[0] = deadline_ns
        await handler(1, runtime)
        assert len(start_case.stopper.calls) == 1
        assert start_case.stopper.times == [deadline_ns]
        assert len(driver.calls) == 1
        assert owned.connection.execute(
            'SELECT sent_at,started_at FROM device_activities WHERE action_id=1'
        ).fetchone() == (_NOW, None)
        assert json.loads(owned.connection.execute(
            'SELECT execution_spec_json FROM actions WHERE id=1').fetchone()[0]) == spec
    finally:
        owned.connection.close()


async def test_sent_ack_has_no_actual_started_time_and_waits_eleven_seconds(tmp_path, start_case):
    owned = await start_case.world(tmp_path, duration_ms=10000, startup=1000, sent=True)
    try:
        wall, mono = [_NOW], [_ANCHOR]
        driver = StartDriver(_sent_result("1"))
        runtime = (await start_case.runtime(owned, driver, wall=wall, mono=mono))
        await capture_handler("camera_record")(1, runtime)
        assert owned.connection.execute("SELECT sent_at, started_at, occupancy_state FROM device_activities WHERE action_id=1").fetchone() == (_NOW, None, 1)
        assert driver.calls[0].start_return_meaning == 1
        mono[0] += 10000000000
        await capture_handler("camera_record")(1, runtime)
        assert start_case.stopper.calls == []
        mono[0] += 1000000000
        await capture_handler("camera_record")(1, runtime)
        assert len(start_case.stopper.calls) == 1
        assert start_case.stopper.times == [18000000000]
    finally:
        owned.connection.close()


async def test_legacy_unstarted_action_passes_original_started_contract_to_current_driver(tmp_path, start_case):
    owned = await start_case.world(tmp_path)
    try:
        driver = StartDriver(_result(confirmed=True, activity_id="1"))
        await capture_handler("camera_record")(1, (await start_case.runtime(owned, driver)))
        assert driver.calls[0].start_return_meaning == 2
        assert owned.connection.execute("SELECT started_at FROM device_activities WHERE action_id=1").fetchone() == (_NOW,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('margin_ms', [0, 1000])
async def test_sent_unknown_result_does_not_create_confirmation_anchor(tmp_path, start_case, margin_ms):
    owned = await start_case.world(tmp_path, duration_ms=10000, startup=margin_ms, sent=True)
    try:
        driver = StartDriver(_result(activity_id="1"))
        runtime = (await start_case.runtime(owned, driver))
        await capture_handler("camera_record")(1, runtime)
        assert owned.connection.execute("SELECT sent_at, started_at, occupancy_state FROM device_activities WHERE action_id=1").fetchone() == (None, None, 1)
        start_case.mono[0] += 60_000_000_000
        await capture_handler("camera_record")(1, runtime)
        assert start_case.stopper.calls == []
        assert len(driver.calls) == 1
    finally:
        owned.connection.close()


@pytest.mark.parametrize('margin_ms', [0, 1000])
async def test_cancel_after_sent_stops_immediately_with_original_budget(tmp_path, start_case, margin_ms):
    owned = await start_case.world(tmp_path, duration_ms=10000, startup=margin_ms, sent=True)
    try:
        driver = StartDriver(_sent_result("1"))
        runtime = (await start_case.runtime(owned, driver))
        class Stop:
            calls = []
            async def stop(self, request):
                assert start_case.mono[0] == _ANCHOR
                self.calls.append(request)
                return DeviceCallResult((DeviceObservation("stop_confirmed", 1, {"activity_id": "1"}),), None)
        start_case.stopper.delegate = Stop()
        await capture_handler("camera_record")(1, runtime)
        from .test_natural_wait_time_eligibility import _request_cancel
        await _request_cancel(runtime, start_case.aid, start_case.wall)
        await capture_handler("camera_record")(1, runtime)
        assert len(start_case.stopper.calls) == 1
        assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE responsibility_key='stop/1'").fetchone() == (1,)
        assert len(driver.calls) == 1
    finally:
        owned.connection.close()


@pytest.mark.parametrize('margin_ms,deadline_ns', [(0, 17_000_000_000), (1000, 18_000_000_000)])
async def test_sent_response_anchor_precedes_commit_delay(tmp_path, start_case, margin_ms, deadline_ns):
    owned = await start_case.world(tmp_path, duration_ms=10000, startup=margin_ms, sent=True)
    try:
        wall, mono, returned = [_NOW], [_ANCHOR], [False]
        driver = StartDriver(_sent_result("1"), returned=lambda: returned.__setitem__(0, True))
        runtime = (await start_case.runtime(owned, driver, wall=wall, mono=mono))
        def delay_commits(statement):
            if returned[0] and statement == "COMMIT":
                returned[0] = False
                wall[0] += 5000000
                mono[0] += 5000000000
        await runtime.database.read(lambda current: current.connection.set_trace_callback(delay_commits))
        await capture_handler("camera_record")(1, runtime)
        assert mono[0] > _ANCHOR
        assert owned.connection.execute("SELECT sent_at, started_at FROM device_activities WHERE action_id=1").fetchone() == (_NOW, None)
        await runtime.database.read(lambda current: current.connection.set_trace_callback(None))
        await start_case.assert_stop_deadline(runtime, deadline_ns)
    finally:
        await runtime.database.read(lambda current: current.connection.set_trace_callback(None))
        owned.connection.close()


@pytest.mark.parametrize('margin_ms,before_stop_us', [(0, 9_000_000), (1000, 10_000_000)])
async def test_sent_restart_uses_original_confirmation_and_only_remaining_wait(tmp_path, start_case, margin_ms, before_stop_us):
    owned = await start_case.world(tmp_path, duration_ms=10000, startup=margin_ms, sent=True)
    try:
        first = (await start_case.runtime(owned, StartDriver(_sent_result("1"))))
        await capture_handler("camera_record")(1, first)
        wall, mono = [_NOW + before_stop_us], [1]
        second_driver = StartDriver(_sent_result("1"))
        second = (await start_case.reopen(owned, second_driver, wall=wall, mono=mono))
        class Stop:
            calls = []
            async def stop(self, request):
                self.calls.append(request)
                return DeviceCallResult((), {"code": "transport_timeout"})
        start_case.stopper.delegate = Stop()
        await capture_handler("camera_record")(1, second)
        assert start_case.stopper.calls == []
        wall[0] += 1000000
        await capture_handler("camera_record")(1, second)
        assert len(start_case.stopper.calls) == 1
        assert second_driver.calls == []
        assert owned.connection.execute("SELECT sent_at, started_at FROM device_activities WHERE action_id=1").fetchone() == (_NOW, None)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('boundary', ['total_duration', 'utc_deadline', 'monotonic_deadline'])
@pytest.mark.parametrize('legacy', [False, True])
async def test_unrepresentable_recording_timing_is_rejected_before_any_start(tmp_path, start_case, boundary, legacy):
    from camctl.contracts.values import MAX_OBJECT_ID
    initial_utc = 253370764800000000 if boundary == 'utc_deadline' else _NOW
    limit_utc = (MAX_OBJECT_ID // 1000000) * 1000000
    owned = await start_case.world(tmp_path,
        duration_ms=MAX_OBJECT_ID if boundary == 'total_duration' else 10000,
        sent=not legacy, startup=None if legacy else 1000, now_us=initial_utc,
        max_delay_ms=(limit_utc - initial_utc) // 1000 if boundary == 'utc_deadline' else 60000)
    driver = StartDriver(_sent_result("1"))
    wall = [limit_utc if boundary == 'utc_deadline' else _NOW]
    mono = [MAX_OBJECT_ID - 1000 if boundary == 'monotonic_deadline' else _ANCHOR]
    runtime = await start_case.runtime(owned, driver, wall=wall, mono=mono)
    try:
        with pytest.raises(ValueError, match='精确整数范围'):
            await capture_handler('camera_record')(1, runtime)
        assert driver.calls == []
        assert owned.connection.execute("SELECT COUNT(*) FROM operation_runs WHERE responsibility_key='start/1'").fetchone() == (0,)
        assert owned.connection.execute('SELECT sent_at,started_at FROM device_activities WHERE action_id=1').fetchone() == (None, None)
    finally:
        owned.connection.close()

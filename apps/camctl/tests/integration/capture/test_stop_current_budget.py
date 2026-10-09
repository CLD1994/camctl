"""普通 STOP 采用本次配置，同时保留原次数、实际结果和终态。

真实处理器、仓储和 SQLite 形成 START 与 STOP 事实，设备替身只在
停止端口返回正式 CallOutcome。配置替换仅提供本次运行的输入分区；
本文件不证明配置文件装载或 CLI 的两次实际 run。对账及受限入口
使用新的真实状态装载器，不接续既往会话的单调钟锚点。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode
from camctl.capture.handlers import (
    SessionRecordingState, _stop_call, advance_winddown, capture_handler,
)
from camctl.capture.media import MediaPolicy, MediaTools
from camctl.capture.media_flow import DriverReadSessions, MediaFlow
from camctl.contracts.values import new_operation_key
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import DeviceCallResult
from camctl.host_files.models import BoundDirectories
from camctl.operations.models import AttemptStatus
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.repositories.scheduling import (
    ObserveWindowRequest, SchedulingRepository, StartActionRequest,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .result_consumer_fixtures import RESULT_EVIDENCE, ResultCatalog, returned
from .test_call_result_save import ReturnedCall, _outcome, _started
from .test_capture_contract import ResultsDouble, _NOW, _entry
from .test_recording_start_runtime import StartDriver, _ANCHOR, _configured, _world
from ..bootstrap.test_media_assembly import _config

register_outputs_guards()

pytestmark = pytest.mark.asyncio


async def _ready(owned, maximum):
    """先真实确认启动，再让已保存的 60 秒录像到达正常停止时点。"""
    runtime = await _started(owned)
    runtime.stop_config = replace(runtime.stop_config, max_attempts=maximum)
    wall, mono = [_NOW + 60_000_000], [_ANCHOR + 60_000_000_000]
    runtime.wall_us = lambda: wall[0]
    runtime.monotonic_ns = lambda: mono[0]
    return runtime, wall, mono


def _stop_run(owned):
    row = owned.connection.execute(
        "SELECT id, status, attempts_used, retry_wait_required, error_json"
        " FROM operation_runs WHERE responsibility_key = 'stop/12'"
    ).fetchone()
    assert row is not None
    return row


def _stop_attempts(owned):
    return owned.connection.execute(
        "SELECT a.id, a.attempt_no, a.max_attempts_used, a.status, a.effect_state,"
        " a.result_event_id, a.result_json, a.error_json"
        " FROM operation_attempts a JOIN operation_runs r ON r.id = a.run_id"
        " WHERE r.responsibility_key = 'stop/12' ORDER BY a.attempt_no"
    ).fetchall()


def _activity(owned):
    return owned.connection.execute(
        "SELECT activity_state, occupancy_state, started_at"
        " FROM device_activities WHERE action_id = 12"
    ).fetchone()


def _assert_exhausted(owned, run_id, attempts_used):
    run = _stop_run(owned)
    assert run[:4] == (run_id, 6, attempts_used, 0)
    details = {"activity_id": "12", "operation_run_id": str(run_id)}
    assert json.loads(run[4]) == {
        "code": "recording_stop_failed", "stage": "device_stop", "details": details,
    }
    action = owned.connection.execute(
        "SELECT status, error_code, error_details_json FROM actions WHERE id = 12"
    ).fetchone()
    assert action[:2] == (4, 14)
    assert json.loads(action[2]) == details
    # 停止未确认不能把可靠的执行中事实或占用解释为已经释放。
    assert _activity(owned) == (2, 1, _NOW)


async def _no_retry_sleep(_seconds):
    pytest.fail("本次 STOP 预算已经耗尽，不应等待并循环追加尝试")


async def _advance(runtime, entry):
    if entry == "restricted-winddown":
        await advance_winddown(
            12, runtime, {}, wait_cap_s=Decimal("0"), sleep=_no_retry_sleep,
        )
    else:
        await capture_handler("camera_record")(12, runtime)


@pytest.mark.parametrize("entry", [
    "normal-anchor", "reconcile-no-anchor", "restricted-winddown",
])
async def test_lower_current_limit_closes_unconfirmed_stop_without_new_attempt(tmp_path, entry):
    owned = _world(tmp_path)
    try:
        runtime, wall, mono = await _ready(owned, 3)
        stopper = ReturnedCall(_outcome("stop"))
        runtime.stopper = stopper
        await capture_handler("camera_record")(12, runtime)
        run_id = _stop_run(owned)[0]
        assert _stop_run(owned)[1:4] == (2, 1, 1)
        original = _stop_attempts(owned)
        assert len(original) == 1 and original[0][3:5] == (3, 1)
        assert original[0][5] is not None
        assert _activity(owned) == (2, 1, _NOW)

        runtime.stop_config = replace(runtime.stop_config, max_attempts=1)
        if entry != "normal-anchor":
            runtime.recording_state = SessionRecordingState(runtime)
        # 旧等待已到，普通入口可直接到达新意图的预算判定。
        wall[0] += 1_000_000
        mono[0] += 1_000_000_000
        await _advance(runtime, entry)

        assert len(stopper.calls) == 1
        assert _stop_attempts(owned) == original
        _assert_exhausted(owned, run_id, 1)
    finally:
        owned.connection.close()


async def test_higher_current_limit_retries_unfinished_stop_after_actual_return_and_wait(tmp_path):
    owned = _world(tmp_path)
    try:
        runtime, wall, mono = await _ready(owned, 1)
        stopper = ReturnedCall(_outcome("stop"))
        runtime.stopper = stopper
        handler = capture_handler("camera_record")
        await handler(12, runtime)
        run_id = _stop_run(owned)[0]
        original = _stop_attempts(owned)
        assert _stop_run(owned)[1:4] == (2, 1, 1)
        assert len(original) == 1 and original[0][5] is not None

        runtime.stop_config = replace(runtime.stop_config, max_attempts=3)
        await handler(12, runtime)
        assert len(stopper.calls) == 1
        assert _stop_run(owned)[:4] == (run_id, 2, 1, 1)
        assert runtime.action(12)["status"] == 2

        wall[0] += 1_000_000
        mono[0] += 1_000_000_000
        await handler(12, runtime)
        assert len(stopper.calls) == 2
        assert _stop_run(owned)[:4] == (run_id, 2, 2, 1)
        assert _stop_attempts(owned)[:1] == original
        assert [request.ticket.attempt_id for request in stopper.calls] == [1, 2]
        assert {request.ticket.run_id for request in stopper.calls} == {run_id}
        assert _activity(owned) == (2, 1, _NOW)
    finally:
        owned.connection.close()


async def test_higher_current_limit_allows_only_remaining_winddown_attempts(tmp_path):
    owned = _world(tmp_path)
    try:
        runtime, wall, mono = await _ready(owned, 1)
        stopper = ReturnedCall(_outcome("stop"))
        runtime.stopper = stopper
        await capture_handler("camera_record")(12, runtime)
        run_id = _stop_run(owned)[0]
        original = _stop_attempts(owned)
        assert _stop_run(owned)[1:4] == (2, 1, 1)
        runtime.stop_config = replace(runtime.stop_config, max_attempts=3)
        runtime.recording_state = SessionRecordingState(runtime)
        sleeps = []

        async def retry_sleep(seconds):
            sleeps.append(seconds)
            assert len(sleeps) <= 2, "剩余两次 STOP 不应继续等待追加尝试"
            assert seconds == 1
            wall[0] += 1_000_000
            mono[0] += 1_000_000_000

        await advance_winddown(
            12, runtime, {}, wait_cap_s=Decimal("0"), sleep=retry_sleep,
        )

        assert len(stopper.calls) == 3
        assert _stop_attempts(owned)[:1] == original
        assert [request.ticket.attempt_id for request in stopper.calls] == [1, 2, 3]
        assert {request.ticket.run_id for request in stopper.calls} == {run_id}
        _assert_exhausted(owned, run_id, 3)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("confirmed", [False, True])
async def test_lower_limit_waits_for_in_flight_stop_actual_result(tmp_path, confirmed):
    owned = _world(tmp_path)
    entered, released = asyncio.Event(), asyncio.Event()
    pending = None
    try:
        runtime, wall, mono = await _ready(owned, 3)
        actual = _outcome("stop", confirmed=confirmed)
        if confirmed:
            actual = replace(actual, status=AttemptStatus.SUCCEEDED, error=None)

        class PendingStop(ReturnedCall):
            async def stop(self, request):
                self.calls.append(request)
                entered.set()
                await released.wait()
                return DeviceCallResult.from_outcome(self.outcome)

        stopper = PendingStop(actual)
        runtime.stopper = stopper
        pending = asyncio.create_task(_stop_call(runtime, runtime.action(12)))
        await entered.wait()
        run_id = _stop_run(owned)[0]
        runtime.stop_config = replace(runtime.stop_config, max_attempts=1)
        await capture_handler("camera_record")(12, runtime)
        assert len(stopper.calls) == 1
        assert _stop_run(owned)[:4] == (run_id, 2, 1, 0)
        assert _stop_attempts(owned)[0][3:7] == (1, 1, None, None)
        assert runtime.action(12)["status"] == 2
        assert _activity(owned) == (2, 1, _NOW)

        released.set()
        await pending
        attempt, = _stop_attempts(owned)
        assert attempt[3:5] == ((2, 3) if confirmed else (3, 1))
        assert attempt[5] is not None
        saved = json.loads(attempt[6])
        assert saved["observations"] == ([{
            "type": "stop_confirmed", "version": 1, "data": {"activity_id": "12"},
        }] if confirmed else [])
        assert saved["call_info"] == {"local_exit": {"exit_code": 0}}
        assert len(stopper.calls) == 1
        if confirmed:
            assert _stop_run(owned)[:4] == (run_id, 3, 1, 0)
        else:
            wall[0] += 1_000_000
            mono[0] += 1_000_000_000
            await capture_handler("camera_record")(12, runtime)
            _assert_exhausted(owned, run_id, 1)
    finally:
        released.set()
        if pending is not None:
            await pending
        owned.connection.close()


@pytest.mark.parametrize("confirmed", [False, True])
async def test_higher_limit_preserves_original_stop_terminal_result(tmp_path, confirmed):
    owned = _world(tmp_path)
    try:
        runtime, _wall, _mono = await _ready(owned, 1)
        actual = _outcome("stop", confirmed=confirmed)
        if confirmed:
            actual = replace(actual, status=AttemptStatus.SUCCEEDED, error=None)
        stopper = ReturnedCall(actual)
        runtime.stopper = stopper
        handler = capture_handler("camera_record")
        await handler(12, runtime)
        if not confirmed:
            await handler(12, runtime)
        original_run = _stop_run(owned)
        original_attempts = _stop_attempts(owned)
        original_action = runtime.action(12)
        original_activity = _activity(owned)
        assert original_run[1] == (3 if confirmed else 6)

        runtime.stop_config = replace(runtime.stop_config, max_attempts=3)
        await handler(12, runtime)
        await handler(12, runtime)

        assert len(stopper.calls) == 1
        assert _stop_run(owned) == original_run
        assert _stop_attempts(owned) == original_attempts
        assert runtime.action(12) == original_action
        assert _activity(owned) == original_activity
    finally:
        owned.connection.close()


async def _recording_consumer(tmp_path):
    """受理和调度自然建立未决定处理行，再通过真实入口确认 START。"""
    config = _config(tmp_path, repair_margin_s="10")
    path = Path(config.paths.state_db)
    assert initialize_state(config, path).outcome is InitOutcome.CREATED
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    instant = datetime.fromtimestamp(_NOW // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    try:
        accepted = AcceptanceRepository().process_input(ProcessInput(ParsedInput("record.json", {
            "request_id": "1", "created_at": instant, "name": "停止确认消费", "actions": [{
                "name": "录像", "type": "camera_record", "device_id": "cam-1",
                "scheduled_at": instant, "params": {"type": "video"},
                "policy": {"max_delay_ms": 5000},
            }],
        }), ResultCatalog(), CommandMode.RUN, _NOW), new_operation_key(), owned)
        assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
        action_id, = owned.connection.execute(
            "SELECT id FROM actions"
        ).fetchone()
        scheduling = SchedulingRepository()
        for save, request in (
            (scheduling.observe_window, ObserveWindowRequest(action_id, _NOW, _NOW)),
            (scheduling.start_action, StartActionRequest(action_id, _NOW, _NOW)),
        ):
            receipt = save(request, new_operation_key(), owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        activity_id, = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (action_id,),
        ).fetchone()
        runtime = _configured(owned, StartDriver(returned("operation", "start_confirmed", activity_id)))
        runtime.evidence = RESULT_EVIDENCE
        runtime.stop_config = replace(
            runtime.stop_config, timeout_s=Decimal("0.125"), retry_interval_s=Decimal("1"),
        )
        await capture_handler("camera_record")(action_id, runtime)
        assert owned.connection.execute(
            "SELECT check_decision,repair_state,source_device_file_id"
            " FROM recording_processing WHERE action_id=?", (action_id,),
        ).fetchone() == (1, 1, None)
        runtime.results = ResultsDouble({action_id: (_entry("original", size=41),)})
        return owned, runtime, action_id, activity_id
    except BaseException:
        owned.connection.close()
        raise


def _confirmed_stop_error(activity_id):
    return replace(_outcome("stop", confirmed=True), observations=(
        DeviceObservation("stop_confirmed", 1, {"activity_id": str(activity_id)}),
    ))


def _saved_consumer_stop(owned, action_id):
    row = owned.connection.execute(
        "SELECT r.status,r.attempts_used,a.status,a.effect_state,e.occurred_at,"
        " a.result_json,a.error_json FROM operation_runs r"
        " JOIN operation_attempts a ON a.run_id=r.id"
        " JOIN history_events e ON e.id=a.result_event_id"
        " WHERE r.responsibility_key=?", (f"stop/{action_id}",),
    ).fetchone()
    assert row is not None
    return row


def _assert_confirmed_stop_error(owned, action_id, activity_id, occurred_at):
    row = _saved_consumer_stop(owned, action_id)
    assert row[:5] == (3, 1, 3, 3, occurred_at)
    assert json.loads(row[6]) == {
        "code": "transport_timeout", "stage": "transport", "details": {"timeout_s": 0.125},
    }
    assert json.loads(row[5])["observations"] == [{
        "type": "stop_confirmed", "version": 1, "data": {"activity_id": str(activity_id)},
    }]


@pytest.mark.parametrize("elapsed_s", [60, 75])
async def test_recovered_control_uses_failed_confirmed_stop_original_time(tmp_path, elapsed_s):
    owned, runtime, action_id, activity_id = await _recording_consumer(tmp_path)
    try:
        returned_at = _NOW + elapsed_s * 1_000_000
        runtime.wall_us = lambda: returned_at
        runtime.monotonic_ns = lambda: _ANCHOR + elapsed_s * 1_000_000_000
        stopper = ReturnedCall(_confirmed_stop_error(activity_id))
        runtime.stopper = stopper
        await _stop_call(runtime, runtime.action(action_id))
        original = _saved_consumer_stop(owned, action_id)
        _assert_confirmed_stop_error(owned, action_id, activity_id, returned_at)

        runtime.recording_state = SessionRecordingState(runtime)
        runtime.wall_us = lambda: _NOW + 200_000_000
        runtime.monotonic_ns = lambda: 1_000_000_000
        # 媒体端口未装配；多录决定保存后等待后续具备媒体能力的会话。
        runtime.media = None
        await capture_handler("camera_record")(action_id, runtime)

        decision, check_basis, repair, repair_basis = owned.connection.execute(
            "SELECT check_decision,check_basis_json,repair_state,repair_basis_json"
            " FROM recording_processing WHERE action_id=?", (action_id,),
        ).fetchone()
        assert decision == 2
        basis = json.loads(check_basis)
        assert basis["target_duration_ms"] == 60000
        if elapsed_s == 60:
            assert basis["reason"] == 1
            assert repair == 1 and repair_basis is None
        else:
            assert basis == {"reason": 3, "target_duration_ms": 60000,
                             "control_elapsed_ns": 75_000_000_000}
            assert repair == 3
            assert json.loads(repair_basis) == {
                "reason": 2, "target_duration_ms": 60000, "threshold_s": 70,
                "actual_duration_s": 75, "control_elapsed_ns": 75_000_000_000,
            }
        assert len(stopper.calls) == 1
        assert _saved_consumer_stop(owned, action_id) == original
        _assert_confirmed_stop_error(owned, action_id, activity_id, returned_at)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("confirmation_saved_before_entry", [False, True])
async def test_winddown_saves_progress_once_after_failed_confirmed_stop(
        tmp_path, confirmation_saved_before_entry):
    owned, runtime, action_id, activity_id = await _recording_consumer(tmp_path)
    try:
        returned_at = _NOW + 60_000_000
        runtime.wall_us = lambda: returned_at
        runtime.monotonic_ns = lambda: _ANCHOR + 60_000_000_000
        stopper = ReturnedCall(_confirmed_stop_error(activity_id))
        runtime.stopper = stopper
        if confirmation_saved_before_entry:
            await _stop_call(runtime, runtime.action(action_id))
        runtime.recording_state = SessionRecordingState(runtime)
        sessions = create_autospec(DriverReadSessions, instance=True)
        tools = create_autospec(MediaTools, instance=True)
        sessions.open_session.side_effect = AssertionError("受限会话不能读取媒体输入")
        tools.probe.side_effect = AssertionError("受限会话不能检查媒体")
        tools.repair.side_effect = AssertionError("受限会话不能修复媒体")
        runtime.media = MediaFlow(
            owned=owned, roots=BoundDirectories(staging=tmp_path), sessions=sessions,
            tools=tools, policy=MediaPolicy(repair_margin_s=Decimal("10")),
            occurred_at=runtime.wall_us, digest_supported=False,
        )

        step = await advance_winddown(
            action_id, runtime, {}, wait_cap_s=Decimal("0"), sleep=_no_retry_sleep,
        )

        assert step.phase == "progress_saved"
        row = owned.connection.execute(
            "SELECT p.check_decision,p.check_state,p.check_basis_json,p.repair_state,"
            " p.source_device_file_id,f.completion_state,f.source_action_id"
            " FROM recording_processing p LEFT JOIN device_files f ON f.id=p.source_device_file_id"
            " WHERE p.action_id=?", (action_id,),
        ).fetchone()
        assert row[:2] == (3, 1)
        assert json.loads(row[2]) == {"reason": 2, "target_duration_ms": 60000}
        assert row[3] == 1 and row[4] is not None
        assert row[5:] == (3, action_id)
        assert runtime.action(action_id)["status"] == 2
        assert owned.connection.execute(
            "SELECT activity_state,occupancy_state FROM device_activities WHERE id=?", (activity_id,),
        ).fetchone() == (3, 2)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
        assert owned.connection.execute("SELECT COUNT(*) FROM file_copies").fetchone() == (0,)
        assert len(stopper.calls) == 1
        _assert_confirmed_stop_error(owned, action_id, activity_id, returned_at)
        sessions.open_session.assert_not_awaited()
        tools.probe.assert_not_awaited()
        tools.repair.assert_not_awaited()
    finally:
        owned.connection.close()


async def test_actual_stop_confirmation_survives_the_call_error(tmp_path):
    owned = _world(tmp_path)
    try:
        runtime, _wall, _mono = await _ready(owned, 1)
        stopper = ReturnedCall(_outcome("stop", confirmed=True))
        runtime.stopper = stopper
        await _stop_call(runtime, runtime.action(12))
        attempt, = _stop_attempts(owned)
        assert attempt[3:5] == (3, 3)
        assert json.loads(attempt[7])["code"] == "transport_timeout"
        assert _stop_run(owned)[1] == 3
        # 实际停止确认与调用错误分别保存，错误不抹去已取得的确认。
        state = runtime.recording_state.recording_state(12)
        assert state.stop_confirmed and state.file_complete_guaranteed
    finally:
        owned.connection.close()

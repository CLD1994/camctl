"""普通 STOP 采用本次配置，同时保留原次数、实际结果和终态。

真实处理器、仓储和 SQLite 形成 START 与 STOP 事实，设备替身只在
停止端口返回正式 CallOutcome。配置替换仅提供本次运行的输入分区；
本文件不证明配置文件装载或 CLI 的两次实际 run。对账及受限入口
使用新的真实状态装载器，不接续既往会话的单调钟锚点。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
import json
from unittest.mock import create_autospec

import pytest

from camctl.capture.handlers import advance_winddown, capture_handler
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import DeviceCallResult
from camctl.operations.models import AttemptStatus
from camctl.persistence.models import DbOutcomeKind

from .test_call_result_save import ReturnedCall, _outcome
from .test_capture_contract import _NOW, _entry
from .test_recording_start_runtime import StartDriver, _ANCHOR, _result
from .recording_start_fixtures import start_case


pytestmark = pytest.mark.asyncio


async def _ready(tmp_path, case, maximum, *, max_delay_ms=60000):
    """当前配置属于会话工厂的显式输入；正常轮次保留同一工厂锚点。"""
    owned = await case.world(tmp_path, max_delay_ms=max_delay_ms)
    devices = {'cam-1': {'driver': 'camctl-adb', 'recording': {
        'max_start_attempts': 3, 'start_timeout_s': Decimal('.75'), 'start_retry_interval_s': Decimal('1'),
        'max_stop_attempts': maximum, 'stop_timeout_s': Decimal('.125'), 'stop_retry_interval_s': Decimal('1'),
        'repair_margin_s': Decimal('10')},
        'query': {'max_attempts': 2, 'timeout_s': Decimal('.5'), 'retry_interval_s': Decimal('1')}}}
    case.budget_devices = devices
    driver = StartDriver(_result(confirmed=True, activity_id='1'))
    runtime = await case.runtime(owned, driver, assembly_options={
        'devices': devices, 'start_config': None, 'stop_config': None, 'query_config': None})
    await capture_handler('camera_record')(case.aid, runtime)
    case.wall[0] = _NOW + 60_000_000
    case.mono[0] = _ANCHOR + 60_000_000_000
    return owned, runtime, case.wall, case.mono


async def _with_limit(case, owned, maximum, *, cold=False):
    # 模块装配输入变化；不宣称CLI运行中会重载配置。
    case.budget_devices['cam-1']['recording']['max_stop_attempts'] = maximum
    if cold:
        return await case.reopen(owned, case.options['driver'], wall=case.wall, mono=case.mono,
            assembly_options=dict(case.options))
    return await case.factory(owned)


async def _save_actual_stop(runtime, action_id):
    """真实C1保存STOP后交回仓储回执，后续本地业务尚未运行。"""
    from camctl.persistence.repositories.capture import CaptureRepository
    from camctl.persistence.repositories.operations import OperationRepository
    original = CaptureRepository.finish_capture_stop
    original_attempt = OperationRepository.finish_attempt
    class StopSaved(Exception):
        pass
    def saved(repository, *args, **options):
        receipt = original(repository, *args, **options)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        return replace(receipt, handoff_error=StopSaved())
    def attempt_saved(repository, request, key, current, **options):
        receipt = original_attempt(repository, request, key, current, **options)
        if request.ticket.operation == 'stop':
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            return replace(receipt, handoff_error=StopSaved())
        return receipt
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(CaptureRepository, 'finish_capture_stop', saved)
        patch.setattr(OperationRepository, 'finish_attempt', attempt_saved)
        with pytest.raises(StopSaved):
            await capture_handler('camera_record')(action_id, runtime)


def _stop_run(owned):
    row = owned.connection.execute(
        "SELECT id, status, attempts_used, retry_wait_required, error_json"
        " FROM operation_runs WHERE responsibility_key = 'stop/1'"
    ).fetchone()
    assert row is not None
    return row


def _stop_attempts(owned):
    return owned.connection.execute(
        "SELECT a.id, a.attempt_no, a.max_attempts_used, a.status, a.effect_state,"
        " a.result_event_id, a.result_json, a.error_json"
        " FROM operation_attempts a JOIN operation_runs r ON r.id = a.run_id"
        " WHERE r.responsibility_key = 'stop/1' ORDER BY a.attempt_no"
    ).fetchall()


def _activity(owned):
    return owned.connection.execute(
        "SELECT activity_state, occupancy_state, started_at"
        " FROM device_activities WHERE action_id = 1"
    ).fetchone()


def _assert_exhausted(owned, run_id, attempts_used):
    run = _stop_run(owned)
    assert run[:4] == (run_id, 6, attempts_used, 0)
    details = {"activity_id": "1", "operation_run_id": str(run_id)}
    assert json.loads(run[4]) == {
        "code": "recording_stop_failed", "stage": "device_stop", "details": details,
    }
    action = owned.connection.execute(
        "SELECT status, error_code, error_details_json FROM actions WHERE id = 1"
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
            1, runtime, {}, wait_cap_s=Decimal("0"), sleep=_no_retry_sleep,
        )
    else:
        await capture_handler("camera_record")(1, runtime)


@pytest.mark.parametrize("entry", [
    "normal-anchor", "reconcile-no-anchor", "restricted-winddown",
])
async def test_lower_current_limit_closes_unconfirmed_stop_without_new_attempt(tmp_path, start_case, entry):
    owned, runtime, wall, mono = await _ready(tmp_path, start_case, 3)
    try:
        stopper = ReturnedCall(_outcome("stop"))
        start_case.stopper.delegate = stopper
        await capture_handler("camera_record")(1, runtime)
        run_id = _stop_run(owned)[0]
        assert _stop_run(owned)[1:4] == (2, 1, 1)
        original = _stop_attempts(owned)
        assert len(original) == 1 and original[0][3:5] == (3, 1)
        assert original[0][5] is not None
        assert _activity(owned) == (2, 1, _NOW)

        runtime = await _with_limit(start_case, owned, 1)
        if entry != "normal-anchor":
            runtime = await _with_limit(start_case, owned, 1, cold=True)
        # 旧等待已到，普通入口可直接到达新意图的预算判定。
        wall[0] += 1_000_000
        mono[0] += 1_000_000_000
        await _advance(runtime, entry)

        assert len(stopper.calls) == 1
        assert _stop_attempts(owned) == original
        _assert_exhausted(owned, run_id, 1)
    finally:
        owned.connection.close()


async def test_higher_current_limit_retries_unfinished_stop_after_actual_return_and_wait(tmp_path, start_case):
    owned, runtime, wall, mono = await _ready(tmp_path, start_case, 1)
    try:
        stopper = ReturnedCall(_outcome("stop"))
        start_case.stopper.delegate = stopper
        handler = capture_handler("camera_record")
        await handler(1, runtime)
        run_id = _stop_run(owned)[0]
        original = _stop_attempts(owned)
        assert _stop_run(owned)[1:4] == (2, 1, 1)
        assert len(original) == 1 and original[0][5] is not None

        runtime = await _with_limit(start_case, owned, 3)
        await handler(1, runtime)
        assert len(stopper.calls) == 1
        assert _stop_run(owned)[:4] == (run_id, 2, 1, 1)
        assert (await runtime.action(1))["status"] == 2

        wall[0] += 1_000_000
        mono[0] += 1_000_000_000
        await handler(1, runtime)
        assert len(stopper.calls) == 2
        assert _stop_run(owned)[:4] == (run_id, 2, 2, 1)
        assert _stop_attempts(owned)[:1] == original
        assert [request.ticket.attempt_id for request in stopper.calls] == [1, 2]
        assert {request.ticket.run_id for request in stopper.calls} == {run_id}
        assert _activity(owned) == (2, 1, _NOW)
    finally:
        owned.connection.close()


async def test_higher_current_limit_allows_only_remaining_winddown_attempts(tmp_path, start_case):
    owned, runtime, wall, mono = await _ready(tmp_path, start_case, 1)
    try:
        stopper = ReturnedCall(_outcome("stop"))
        start_case.stopper.delegate = stopper
        await capture_handler("camera_record")(1, runtime)
        run_id = _stop_run(owned)[0]
        original = _stop_attempts(owned)
        assert _stop_run(owned)[1:4] == (2, 1, 1)
        runtime = await _with_limit(start_case, owned, 3)
        runtime = await _with_limit(start_case, owned, 3, cold=True)
        sleeps = []

        async def retry_sleep(seconds):
            sleeps.append(seconds)
            assert len(sleeps) <= 2, "剩余两次 STOP 不应继续等待追加尝试"
            assert seconds == 1
            wall[0] += 1_000_000
            mono[0] += 1_000_000_000

        await advance_winddown(
            1, runtime, {}, wait_cap_s=Decimal("0"), sleep=retry_sleep,
        )

        assert len(stopper.calls) == 3
        assert _stop_attempts(owned)[:1] == original
        assert [request.ticket.attempt_id for request in stopper.calls] == [1, 2, 3]
        assert {request.ticket.run_id for request in stopper.calls} == {run_id}
        _assert_exhausted(owned, run_id, 3)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("confirmed", [False, True])
async def test_lower_limit_waits_for_in_flight_stop_actual_result(tmp_path, start_case, confirmed):
    entered, released = asyncio.Event(), asyncio.Event()
    pending = None
    owned, runtime, wall, mono = await _ready(tmp_path, start_case, 3)
    try:
        actual = _confirmed_stop_error(1) if confirmed else _outcome("stop")
        if confirmed:
            actual = replace(actual, status=AttemptStatus.SUCCEEDED, error=None)

        class PendingStop(ReturnedCall):
            async def stop(self, request):
                self.calls.append(request)
                entered.set()
                await released.wait()
                return DeviceCallResult.from_outcome(self.outcome)

        stopper = PendingStop(actual)
        start_case.stopper.delegate = stopper
        pending = asyncio.create_task(_save_actual_stop(runtime, start_case.aid))
        await entered.wait()
        run_id = _stop_run(owned)[0]
        runtime = await _with_limit(start_case, owned, 1)
        await capture_handler("camera_record")(1, runtime)
        assert len(stopper.calls) == 1
        assert _stop_run(owned)[:4] == (run_id, 2, 1, 0)
        assert _stop_attempts(owned)[0][3:7] == (1, 1, None, None)
        assert (await runtime.action(1))["status"] == 2
        assert _activity(owned) == (2, 1, _NOW)

        released.set()
        await pending
        attempt, = _stop_attempts(owned)
        assert attempt[3:5] == ((2, 3) if confirmed else (3, 1))
        assert attempt[5] is not None
        saved = json.loads(attempt[6])
        assert saved["observations"] == ([{
            "type": "stop_confirmed", "version": 1, "data": {"activity_id": "1"},
        }] if confirmed else [])
        assert saved["call_info"] == {"local_exit": {"exit_code": 0}}
        assert len(stopper.calls) == 1
        if confirmed:
            assert _stop_run(owned)[:4] == (run_id, 3, 1, 0)
        else:
            wall[0] += 1_000_000
            mono[0] += 1_000_000_000
            await capture_handler("camera_record")(1, runtime)
            _assert_exhausted(owned, run_id, 1)
    finally:
        released.set()
        if pending is not None:
            await pending
        owned.connection.close()


@pytest.mark.parametrize("confirmed", [False, True])
async def test_higher_limit_preserves_original_stop_terminal_result(tmp_path, start_case, confirmed):
    owned, runtime, _wall, _mono = await _ready(tmp_path, start_case, 1)
    try:
        actual = _confirmed_stop_error(1) if confirmed else _outcome("stop")
        if confirmed:
            actual = replace(actual, status=AttemptStatus.SUCCEEDED, error=None)
        stopper = ReturnedCall(actual)
        start_case.stopper.delegate = stopper
        handler = capture_handler("camera_record")
        await handler(1, runtime)
        if not confirmed:
            await handler(1, runtime)
        original_run = _stop_run(owned)
        original_attempts = _stop_attempts(owned)
        original_action = (await runtime.action(1))
        original_activity = _activity(owned)
        assert original_run[1] == (3 if confirmed else 6)

        runtime = await _with_limit(start_case, owned, 3)
        await handler(1, runtime)
        await handler(1, runtime)

        assert len(stopper.calls) == 1
        assert _stop_run(owned) == original_run
        assert _stop_attempts(owned) == original_attempts
        assert (await runtime.action(1)) == original_action
        assert _activity(owned) == original_activity
    finally:
        owned.connection.close()


async def _recording_consumer(tmp_path, case):
    owned, runtime, wall, mono = await _ready(tmp_path, case, 3, max_delay_ms=5000)
    action_id = case.aid
    activity_id, = owned.connection.execute('SELECT id FROM device_activities').fetchone()
    assert owned.connection.execute(
        'SELECT check_decision,repair_state,source_device_file_id FROM recording_processing WHERE action_id=?',
        (action_id,)).fetchone() == (1, 1, None)
    case.options['results'].files_by_action[activity_id] = (_entry('original', size=41),)
    return owned, runtime, action_id, activity_id


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
async def test_recovered_control_uses_failed_confirmed_stop_original_time(tmp_path, start_case, elapsed_s):
    owned, runtime, action_id, activity_id = await _recording_consumer(tmp_path, start_case)
    try:
        returned_at = _NOW + elapsed_s * 1_000_000
        start_case.wall[0] = returned_at
        start_case.mono[0] = _ANCHOR + elapsed_s * 1_000_000_000
        stopper = ReturnedCall(_confirmed_stop_error(activity_id))
        start_case.stopper.delegate = stopper
        await _save_actual_stop(runtime, action_id)
        original = _saved_consumer_stop(owned, action_id)
        _assert_confirmed_stop_error(owned, action_id, activity_id, returned_at)

        start_case.wall[0] = _NOW + 200_000_000
        start_case.mono[0] = 1_000_000_000
        runtime = await _with_limit(start_case, owned, 3, cold=True)
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
                "control_elapsed_ns": 75_000_000_000,
            }
            assert "actual_duration_s" not in json.loads(repair_basis)
        assert len(stopper.calls) == 1
        assert _saved_consumer_stop(owned, action_id) == original
        _assert_confirmed_stop_error(owned, action_id, activity_id, returned_at)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("confirmation_saved_before_entry", [False, True])
async def test_winddown_saves_progress_once_after_failed_confirmed_stop(
        tmp_path, start_case, monkeypatch, confirmation_saved_before_entry):
    owned, runtime, action_id, activity_id = await _recording_consumer(tmp_path, start_case)
    try:
        returned_at = _NOW + 60_000_000
        start_case.wall[0] = returned_at
        start_case.mono[0] = _ANCHOR + 60_000_000_000
        stopper = ReturnedCall(_confirmed_stop_error(activity_id))
        start_case.stopper.delegate = stopper
        if confirmation_saved_before_entry:
            await _save_actual_stop(runtime, action_id)
        sessions, tools = _declare_media(start_case, monkeypatch)
        runtime = await _with_limit(start_case, owned, 3, cold=True)

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
        assert (await runtime.action(action_id))["status"] == 2
        assert owned.connection.execute(
            "SELECT activity_state,occupancy_state FROM device_activities WHERE id=?", (activity_id,),
        ).fetchone() == (3, 2)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
        assert owned.connection.execute("SELECT COUNT(*) FROM file_copies").fetchone() == (0,)
        assert len(stopper.calls) == 1
        _assert_confirmed_stop_error(owned, action_id, activity_id, returned_at)
        sessions.open_read.assert_not_awaited()
        tools.probe.assert_not_awaited()
        tools.repair.assert_not_awaited()
    finally:
        owned.connection.close()


async def test_actual_stop_confirmation_survives_the_call_error(tmp_path, start_case):
    owned, runtime, _wall, _mono = await _ready(tmp_path, start_case, 1)
    try:
        stopper = ReturnedCall(_confirmed_stop_error(1))
        start_case.stopper.delegate = stopper
        await _save_actual_stop(runtime, start_case.aid)
        attempt, = _stop_attempts(owned)
        assert attempt[3:5] == (3, 3)
        assert json.loads(attempt[7])["code"] == "transport_timeout"
        assert _stop_run(owned)[1] == 3
        # 实际停止确认与调用错误分别保存，错误不抹去已取得的确认。
        assert owned.connection.execute("SELECT activity_state FROM device_activities WHERE action_id=1").fetchone() == (3,)
    finally:
        owned.connection.close()


def _declare_media(case, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from camctl.bootstrap.capture_assembly import HostMediaTools
    from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
    from camctl.devices.ports import DriverDeclaration, ReadDriver
    sessions = create_autospec(ReadDriver, instance=True)
    sessions.open_read.side_effect = AssertionError('受限会话不能读取媒体输入')
    tools = SimpleNamespace(probe=AsyncMock(side_effect=AssertionError('受限会话不能检查媒体')),
        repair=AsyncMock(side_effect=AssertionError('受限会话不能修复媒体')))
    monkeypatch.setattr(HostMediaTools, 'probe', tools.probe)
    monkeypatch.setattr(HostMediaTools, 'repair', tools.repair)
    driver = case.options['driver']
    ports = SimpleNamespace(control=driver.control, stop=case.stopper.stop, open_read=sessions.open_read)
    declaration = DriverDeclaration(control_supported=True, stop_supported=True, query_supported=False,
        result_supported=False, read_supported=True, digest_supported=False, delete_supported=False)
    case.options['drivers'] = DriverRegistry((DriverEntry('camctl-adb', ports, declaration,
        case.options['evidence'], DriverStatus.SOFTWARE_CONTRACT_VERIFIED),))
    case.options['media_enabled'] = True
    return sessions, tools

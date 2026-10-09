"""基准活动与启动意图的真实仓储门禁。"""
from decimal import Decimal
from dataclasses import replace
import asyncio

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.operations.attempts import AttemptConfig, AttemptIntent, AttemptTarget, OperationKind
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository, register_operation_guards
from camctl.persistence.repositories.scheduling import GrantOutcome, GrantRequest, SchedulingRepository
from camctl.persistence.repositories.scheduling import StartOutcome
from camctl.scheduling.rules import LaunchWindow
from camctl.capture.media import RecordingFailure
from camctl.operations.models import ErrorValue
from camctl.outputs.catalog import OutputCatalogFacts
from camctl.persistence.repositories.capture import CaptureRepository, FinishCapture
from camctl.persistence.repositories.capture import FinishCanceledCapture
from ..acceptance.test_acceptance import _plan_body
from ..acceptance.test_adb_camera_definitions import _catalog, _params
from ..scheduling.test_start_action import _SCHEDULED, _accept_plan, _start, owned
from .test_baseline_history import _fix
from .test_baseline_prepare import Directory
from camctl.capture.handlers import CaptureRuntime, capture_handler
from camctl.devices.directory import DirectoryRead
from camctl.contracts.pages import Page
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import DeviceCallResult
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.persistence.repositories.timelapse import TimelapseRepository
from .test_capture_contract import _EVIDENCE

pytestmark = pytest.mark.asyncio
register_operation_guards()


async def _prepared(owned, tmp_path, action_type):
    body = _plan_body()
    body["actions"][0].update(type=action_type, params=_params("dji-action6", action_type))
    await _accept_plan(owned, tmp_path, body, _catalog("dji-action6"))
    result = _start(owned, 1, now=_SCHEDULED)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    return result.value.activity_id


def _grant(owned):
    return SchedulingRepository().grant_start(GrantRequest(
        "cam-1", 1, LaunchWindow(_SCHEDULED, _SCHEDULED + 30_000_000),
        _SCHEDULED, AttemptConfig(3, Decimal("10")), _SCHEDULED), new_operation_key(), owned)


@pytest.mark.parametrize("action_type", ["camera_record", "camera_timelapse"])
async def test_collecting_baseline_cannot_consume_start(owned, tmp_path, action_type):
    await _prepared(owned, tmp_path, action_type)
    result = _grant(owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is GrantOutcome.REJECTED
    assert result.value.reason == "baseline_pending"
    assert owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (0,)


async def test_generic_start_intent_cannot_bypass_baseline_guard(owned, tmp_path):
    activity_id = await _prepared(owned, tmp_path, "camera_timelapse")
    result = OperationRepository().begin_attempt(AttemptIntent(
        action_id=1, kind=OperationKind.START, target=AttemptTarget(activity_id=activity_id),
        operation="control", query_purpose=None,
        config=AttemptConfig(1, Decimal("10")), occurred_at=_SCHEDULED), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (0,)


@pytest.mark.parametrize("action_type", ["camera_record", "camera_timelapse"])
async def test_fixed_empty_baseline_allows_original_start_intent(owned, tmp_path, action_type):
    activity_id = await _prepared(owned, tmp_path, action_type)
    assert _fix(owned, activity_id).kind is DbOutcomeKind.COMPLETED
    result = _grant(owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is GrantOutcome.GRANTED
    assert result.value.ticket.target_id == str(activity_id)
    assert owned.connection.execute("SELECT dispatch_state FROM device_activities").fetchone() == (2,)
    repeated = _grant(owned)
    assert repeated.kind is DbOutcomeKind.COMPLETED, repeated.error
    assert repeated.value.outcome is GrantOutcome.REJECTED and repeated.value.reason == "already_dispatched"
    assert owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (1,)


@pytest.mark.parametrize("action_type", ["camera_record", "camera_timelapse"])
async def test_baseline_failure_atomically_preserves_error_and_releases_unstarted_activity(owned, tmp_path, action_type):
    activity_id = await _prepared(owned, tmp_path, action_type)
    error = ErrorValue("adb_directory_failed", "device_file_access", {"reason": "actual_failure"})
    request = FinishCapture(1, (), OutputCatalogFacts(1, True), _SCHEDULED,
        RecordingFailure("capture_failed", {"activity_id": str(activity_id), "reason": "baseline_read_failed"}),
        baseline_error=error)
    key = new_operation_key()
    repository = CaptureRepository()
    for _ in range(2):
        result = repository.finish_capture(request, key, owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert owned.connection.execute("SELECT status,error_code FROM actions").fetchone() == (4, 13)
    row = owned.connection.execute("SELECT activity_state,occupancy_state,baseline_state,last_error_json FROM device_activities").fetchone()
    assert row[:3] == (1, 2, 2)
    assert __import__("json").loads(row[3]) == {"code": error.code, "stage": error.stage, "details": dict(error.details)}
    assert owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (0,)
    changed = replace(request, baseline_error=ErrorValue(error.code, error.stage, {"reason": "changed"}))
    assert repository.finish_capture(changed, key, owned).kind is DbOutcomeKind.ROLLED_BACK


def _runtime(owned, directory, driver):
    return CaptureRuntime(owned, SchedulingRepository(), OperationRepository(), CaptureRepository(),
        TimelapseRepository(), driver, None, _EVIDENCE, lambda: _SCHEDULED, lambda: 0,
        lambda a: LaunchWindow(a["scheduled_at"], a["scheduled_at"] + a["max_delay_ms"] * 1000),
        lambda a: CaptureWaitConfig(10_000, 0, 0), baseline_directory=directory)


@pytest.mark.parametrize("action_type", ["camera_record", "camera_timelapse"])
async def test_fixed_baseline_and_intent_precede_dispatch(owned, tmp_path, action_type):
    await _prepared(owned, tmp_path, action_type)
    class Driver:
        calls = 0
        async def control(self, request):
            self.calls += 1
            assert owned.connection.execute("SELECT baseline_state,dispatch_state FROM device_activities").fetchone() == (3, 2)
            assert request.ticket is not None and request.ticket.target_id == "1"
            assert owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (1,)
            name = "start_confirmed" if action_type == "camera_record" else "timelapse_sent"
            return DeviceCallResult((DeviceObservation(name, 1, {"activity_id": "1"}),), None)
    driver = Driver()
    runtime = _runtime(owned, Directory([lambda r: DirectoryRead(None, None, Page((), None))]), driver)
    await capture_handler(action_type)(1, runtime)
    assert driver.calls == 1


@pytest.mark.parametrize("action_type", ["camera_record", "camera_timelapse"])
async def test_baseline_read_failure_consumes_no_start(owned, tmp_path, action_type):
    await _prepared(owned, tmp_path, action_type)
    error = ErrorValue("adb_directory_failed", "device_file_access", {"reason": "actual_error"})
    class Driver:
        async def control(self, request):
            pytest.fail("基准读取失败不能派发启动")
    runtime = _runtime(owned, Directory([lambda r: DirectoryRead(None, error, None)]), Driver())
    await capture_handler(action_type)(1, runtime)
    assert owned.connection.execute("SELECT status,error_code FROM actions").fetchone() == (4, 13)
    assert owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (0,)
    assert owned.connection.execute("SELECT occupancy_state,activity_state FROM device_activities").fetchone() == (2, 1)


@pytest.mark.parametrize("action_type", ["camera_record", "camera_timelapse"])
async def test_cancel_flag_before_actual_error_preserves_error_and_canceled_result(owned, tmp_path, action_type):
    await _prepared(owned, tmp_path, action_type)
    error = ErrorValue("adb_directory_failed", "device_file_access", {"reason": "actual_after_cancel"})
    def actual_return(request):
        owned.connection.execute("UPDATE actions SET cancel_requested=1 WHERE id=1")
        owned.connection.commit()
        return DirectoryRead(None, error, None)
    runtime = _runtime(owned, Directory([actual_return]), None)
    await capture_handler(action_type)(1, runtime)
    assert owned.connection.execute("SELECT status,error_code FROM actions").fetchone() == (6, None)
    import json
    saved = owned.connection.execute("SELECT last_error_json FROM device_activities").fetchone()[0]
    assert json.loads(saved) == {"code": error.code, "stage": error.stage, "details": dict(error.details)}
    assert owned.connection.execute("SELECT occupancy_state,activity_state FROM device_activities").fetchone() == (2, 1)
    assert owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (0,)


@pytest.mark.parametrize("device_id,expected", [("cam-1", StartOutcome.REJECTED), ("cam-2", StartOutcome.STARTED)])
async def test_only_scope_qualified_candidate_creates_preparation(owned, tmp_path, device_id, expected):
    await _prepared(owned, tmp_path, "camera_timelapse")
    owned.connection.execute("UPDATE actions SET cancel_requested=1 WHERE id=1")
    owned.connection.commit()
    canceled = CaptureRepository().finish_canceled_capture(FinishCanceledCapture(1, _SCHEDULED, unstarted=True), new_operation_key(), owned)
    assert canceled.kind is DbOutcomeKind.COMPLETED, canceled.error
    body = _plan_body(request_id="43")
    body["actions"][0].update(type="camera_record", device_id=device_id, params=_params("dji-action6", "camera_record"))
    await _accept_plan(owned, tmp_path, body, _catalog("dji-action6", device_id=device_id))
    result = _start(owned, 2, now=_SCHEDULED)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is expected
    if expected is StartOutcome.REJECTED:
        assert result.value.reason == "device_busy"
        assert owned.connection.execute("SELECT status FROM actions WHERE id=2").fetchone() == (1,)
        assert owned.connection.execute("SELECT COUNT(*) FROM device_activities WHERE action_id=2").fetchone() == (0,)


async def test_later_pending_candidate_cannot_take_baseline_scope_first(owned, tmp_path):
    for request_id in ("42", "43"):
        body = _plan_body(request_id=request_id)
        body["actions"][0].update(type="camera_timelapse", params=_params("dji-action6", "camera_timelapse"))
        await _accept_plan(owned, tmp_path, body, _catalog("dji-action6"))
    later = _start(owned, 2, now=_SCHEDULED)
    assert later.kind is DbOutcomeKind.COMPLETED, later.error
    assert later.value.outcome is StartOutcome.REJECTED and later.value.reason == "not_first_candidate"
    assert owned.connection.execute("SELECT COUNT(*) FROM device_activities").fetchone() == (0,)
    assert _start(owned, 1, now=_SCHEDULED).value.outcome is StartOutcome.STARTED


async def test_later_task_scope_trigger_does_not_block_original_baseline_start(owned, tmp_path):
    activity_id = await _prepared(owned, tmp_path, "camera_timelapse")
    body = _plan_body(request_id="43")
    body["actions"][0].update(type="camera_record", params=_params("dji-action6", "camera_record"))
    await _accept_plan(owned, tmp_path, body, _catalog("dji-action6", baseline=False))
    later = _start(owned, 2, now=_SCHEDULED)
    assert later.kind is DbOutcomeKind.COMPLETED, later.error
    assert later.value.outcome is StartOutcome.STARTED
    assert _fix(owned, activity_id).kind is DbOutcomeKind.COMPLETED
    assert _grant(owned).value.outcome is GrantOutcome.GRANTED


async def test_pending_baseline_waits_for_older_running_unstarted_candidate(owned, tmp_path):
    for request_id, baseline in (("42", False), ("43", True)):
        body = _plan_body(request_id=request_id)
        body["actions"][0].update(type="camera_timelapse", params=_params("dji-action6", "camera_timelapse"))
        await _accept_plan(owned, tmp_path, body, _catalog("dji-action6", baseline=baseline))
    assert _start(owned, 1, now=_SCHEDULED).value.outcome is StartOutcome.STARTED
    later = _start(owned, 2, now=_SCHEDULED)
    assert later.kind is DbOutcomeKind.COMPLETED, later.error
    assert later.value.outcome is StartOutcome.REJECTED and later.value.reason == "not_first_candidate"


@pytest.mark.parametrize("action_type", ["camera_record", "camera_timelapse"])
@pytest.mark.parametrize("read_failed", [False, True])
@pytest.mark.parametrize("release_unknown", [False, True])
async def test_cancellation_waits_for_actual_preparation_return_before_releasing(owned, tmp_path, action_type, read_failed, release_unknown):
    await _prepared(owned, tmp_path, action_type)
    entered, returned = asyncio.Event(), asyncio.Event()
    error = ErrorValue("adb_directory_failed", "device_file_access", {"reason": "original_after_cancel"})
    class HeldDirectory:
        async def read_directory(self, request, *, stop):
            entered.set()
            await returned.wait()
            return DirectoryRead(None, error, None) if read_failed else DirectoryRead(None, None, Page((), None))
    class Driver:
        async def control(self, request):
            pytest.fail("取消后的准备不能派发 START")
    release_calls = []
    class Repository(CaptureRepository):
        def release_occupancy(self, request, key, owned):
            release_calls.append((request, key))
            result = super().release_occupancy(request, key, owned)
            assert result.kind is DbOutcomeKind.COMPLETED, result.error
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError("原释放回执未知")) if release_unknown and len(release_calls) == 1 else result
    runtime = _runtime(owned, HeldDirectory(), Driver())
    runtime.capture = Repository()
    handler = capture_handler(action_type)
    original = asyncio.create_task(handler(1, runtime))
    try:
        await entered.wait()
        owned.connection.execute("UPDATE actions SET cancel_requested=1 WHERE id=1")
        owned.connection.commit()
        await handler(1, runtime)
        assert owned.connection.execute("SELECT status FROM actions").fetchone() == (6,)
        assert owned.connection.execute("SELECT occupancy_state FROM device_activities").fetchone() == (1,)
        assert runtime.pending_baselines[1].in_call
        returned.set()
        if release_unknown:
            with pytest.raises(ConsistencyError):
                await original
            runtime.wall_us = lambda: pytest.fail("原释放恢复不得更换事实时刻")
            runtime.resume_baselines(1)
            assert len(release_calls) == 2 and release_calls[0] == release_calls[1]
            if read_failed:
                request, key = release_calls[0]
                changed = replace(request, preparation_error=ErrorValue(error.code, error.stage, {"reason": "changed"}))
                assert CaptureRepository().release_occupancy(changed, key, owned).kind is DbOutcomeKind.ROLLED_BACK
        else:
            await original
        assert owned.connection.execute("SELECT occupancy_state,activity_state,baseline_state FROM device_activities").fetchone() == (2, 1, 2)
        if read_failed:
            import json
            saved = owned.connection.execute("SELECT last_error_json FROM device_activities").fetchone()[0]
            assert json.loads(saved) == {"code": error.code, "stage": error.stage, "details": dict(error.details)}
        assert runtime.pending_baselines == {}
    finally:
        returned.set()
        if not original.done():
            await original


@pytest.mark.parametrize("action_type", ["camera_record", "camera_timelapse"])
async def test_unknown_failure_resumes_original_request_before_new_binding_and_clock(owned, tmp_path, action_type):
    await _prepared(owned, tmp_path, action_type)
    calls = []
    class Repository(CaptureRepository):
        def finish_capture(self, request, key, owned):
            calls.append((request, key))
            result = super().finish_capture(request, key, owned)
            assert result.kind is DbOutcomeKind.COMPLETED, result.error
            return DbOutcome(
                DbOutcomeKind.UNKNOWN, error=OSError("提交回执未取得")) if len(calls) == 1 else result
    error = ErrorValue("adb_directory_failed", "device_file_access", {"reason": "original"})
    runtime = _runtime(owned, Directory([lambda r: DirectoryRead(None, error, None)]), None)
    runtime.capture = Repository()
    with pytest.raises(ConsistencyError):
        await capture_handler(action_type)(1, runtime)
    runtime.wall_us = lambda: _SCHEDULED + 100_000_000
    runtime.binding_check = lambda b: pytest.fail("原失败保存先于新绑定资格")
    await capture_handler(action_type)(1, runtime)
    assert calls[0] == calls[1] and len(runtime.baseline_directory.calls) == 1
    assert runtime.pending_capture_completions == {} and runtime.pending_baselines == {}


@pytest.mark.parametrize("action_type", ["camera_record", "camera_timelapse"])
async def test_preparation_window_expiry_retains_original_unknown_request(owned, tmp_path, action_type):
    await _prepared(owned, tmp_path, action_type)
    now, calls = [_SCHEDULED], []
    class Scheduling(SchedulingRepository):
        def expire_action(self, request, key, owned):
            calls.append((request, key))
            result = super().expire_action(request, key, owned)
            assert result.kind is DbOutcomeKind.COMPLETED, result.error
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError("原过期回执未知")) if len(calls) == 1 else result
    def after_window(request):
        now[0] = _SCHEDULED + 31_000_000
        return DirectoryRead(None, None, Page((), None))
    runtime = _runtime(owned, Directory([after_window]), None)
    runtime.wall_us = lambda: now[0]
    runtime.scheduling = Scheduling()
    with pytest.raises(ConsistencyError):
        await capture_handler(action_type)(1, runtime)
    now[0] += 1_000_000
    runtime.binding_check = lambda b: pytest.fail("原过期申请先于新绑定")
    await capture_handler(action_type)(1, runtime)
    assert len(calls) == 2 and calls[0] == calls[1]
    assert runtime.pending_capture_completions == {}
    assert owned.connection.execute("SELECT status FROM actions").fetchone() == (5,)
    assert owned.connection.execute("SELECT occupancy_state,activity_state FROM device_activities").fetchone() == (2, 1)

"""首次拍摄控制重新核对残留，尚未派发的等待遵守启动窗口。"""
import sqlite3

import pytest
from decimal import Decimal

from camctl.capture.handlers import CaptureRuntime, DeviceControlPort, _control_call, capture_handler
from camctl.capture.residual import pass_residual_gate
from camctl.capture.baseline import BaselinePreparation, PreparationPhase
from camctl.contracts.enums import enum_for
from camctl.operations.attempts import AttemptConfig
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.scheduling import (
    ExpireActionResult, ExpireOutcome, SchedulingRepository,
)
from camctl.scheduling.rules import LaunchWindow

pytestmark = pytest.mark.asyncio


def _runtime(mocker, *, now=100, action_type="camera_record"):
    runtime = mocker.create_autospec(CaptureRuntime, instance=True)
    runtime.pending_read_results = {}
    runtime.pending_read_business = {}
    runtime.pending_read_ends = {}
    runtime.pending_baselines = {}
    runtime.prepare_baseline.return_value = BaselinePreparation(PreparationPhase.READY)
    runtime.wall_us = mocker.Mock(return_value=now)
    runtime.monotonic_ns = mocker.Mock(return_value=100_000)
    runtime.window_of = mocker.Mock(return_value=LaunchWindow(100, 1100))
    runtime.driver = mocker.create_autospec(DeviceControlPort, instance=True)
    runtime.scheduling = mocker.create_autospec(SchedulingRepository, instance=True)
    runtime.scheduling.expire_action.return_value = DbOutcome(
        DbOutcomeKind.COMPLETED,
        value=ExpireActionResult(ExpireOutcome.EXPIRED, expiration_reason=2))
    runtime.owned = mocker.Mock()
    runtime.owned.connection = mocker.create_autospec(sqlite3.Connection, instance=True)
    cursor = mocker.create_autospec(sqlite3.Cursor, instance=True)
    cursor.fetchone.return_value = None
    cursor.fetchall.return_value = []
    runtime.owned.connection.execute.return_value = cursor
    runtime.binding_check = None
    runtime.timelapse_deadlines = {}
    runtime.start_config = AttemptConfig(3, Decimal("10"), Decimal("3"))
    runtime.last_attempt.return_value = None
    runtime.grant.return_value = (None, "device_busy")
    runtime.action.return_value = {
        "id": 2, "device_id": "cam-1", "status": 2, "cancel_requested": 0,
        "scheduled_at": 100, "max_delay_ms": 1,
        "type": int(enum_for("actions.type")[action_type.upper()]),
        "effective_params_json": {"type": "ordinary", "duration_s": 60},
        "execution_spec_json": {"target_duration_ms": 60000},
    }
    return runtime


@pytest.mark.parametrize("action_type", [
    "camera_take_photo", "camera_record", "camera_timelapse",
])
async def test_first_control_waits_without_grant_or_device_call(mocker, action_type):
    runtime = _runtime(mocker, action_type=action_type)
    mocker.patch("camctl.capture.residual.pass_residual_gate", return_value=False)
    await capture_handler(action_type)(2, runtime)
    runtime.grant.assert_not_called()
    runtime.driver.control.assert_not_awaited()


async def test_gate_failure_preserves_error_and_does_not_grant(mocker):
    runtime = _runtime(mocker)
    mocker.patch("camctl.capture.residual.pass_residual_gate",
                 side_effect=RuntimeError("state unavailable"))
    with pytest.raises(RuntimeError):
        await _control_call(runtime, runtime.action(2), "take_photo", "photo_taken")
    runtime.grant.assert_not_called()
    runtime.driver.control.assert_not_awaited()


async def test_passing_first_gate_uses_existing_grant_conditions(mocker):
    runtime = _runtime(mocker)
    gate = mocker.patch("camctl.capture.residual.pass_residual_gate", return_value=True)
    result = await _control_call(runtime, runtime.action(2), "take_photo", "photo_taken")
    assert result.phase == "not_granted"
    assert result.detail == "device_busy"
    gate.assert_awaited_once()
    runtime.driver.control.assert_not_awaited()


@pytest.mark.parametrize("saved_attempt", [(1, 1, 100), (2, 3, 100), (3, 2, 100), (4, 1, 100)])
async def test_saved_start_attempt_does_not_repeat_first_gate(mocker, saved_attempt):
    runtime = _runtime(mocker)
    runtime.last_attempt.return_value = saved_attempt
    gate = mocker.patch("camctl.capture.residual.pass_residual_gate", return_value=False)
    result = await _control_call(runtime, runtime.action(2), "start_recording", "start_confirmed")
    assert result.phase == "not_granted"
    gate.assert_not_awaited()
    runtime.driver.control.assert_not_awaited()


@pytest.mark.parametrize("status,canceled", [(2, 1), (3, 0), (4, 0), (5, 0), (6, 0)])
async def test_inactive_trigger_does_not_check_or_stop_residual(mocker, status, canceled):
    runtime = _runtime(mocker)
    action = {**runtime.action(2), "status": status, "cancel_requested": canceled}
    candidates = mocker.patch("camctl.capture.residual.residual_candidates", return_value=())
    assert await pass_residual_gate(runtime, action) is False
    candidates.assert_not_called()
    runtime.scheduling.expire_action.assert_not_called()


async def test_trigger_before_window_waits_without_query(mocker):
    runtime = _runtime(mocker, now=99)
    candidates = mocker.patch("camctl.capture.residual.residual_candidates", return_value=())
    assert await pass_residual_gate(runtime, runtime.action(2)) is False
    candidates.assert_not_called()
    runtime.scheduling.expire_action.assert_not_called()


async def test_unstarted_running_trigger_expires_after_window(mocker):
    runtime = _runtime(mocker, now=1101)
    candidates = mocker.patch("camctl.capture.residual.residual_candidates", return_value=())
    assert await pass_residual_gate(runtime, runtime.action(2)) is False
    request = runtime.scheduling.expire_action.call_args.args[0]
    assert request.action_id == 2
    assert request.trusted_wall_now == 1101
    candidates.assert_not_called()


async def test_expiration_error_is_not_a_successful_gate(mocker):
    runtime = _runtime(mocker, now=1101)
    runtime.scheduling.expire_action.return_value = DbOutcome(
        DbOutcomeKind.UNKNOWN, error=RuntimeError("commit unknown"))
    mocker.patch("camctl.capture.residual.residual_candidates", return_value=())
    with pytest.raises(RuntimeError):
        await pass_residual_gate(runtime, runtime.action(2))


@pytest.mark.parametrize("status,now", [(1, 100), (2, 100), (2, 1100)])
async def test_valid_trigger_keeps_existing_gate_conditions(mocker, status, now):
    runtime = _runtime(mocker, now=now)
    action = {**runtime.action(2), "status": status}
    mocker.patch("camctl.capture.residual.residual_candidates", return_value=())
    assert await pass_residual_gate(runtime, action) is True
    runtime.scheduling.expire_action.assert_not_called()


@pytest.mark.parametrize("action_type", [
    "camera_take_photo", "camera_record", "camera_timelapse",
])
async def test_unstarted_cancel_finishes_locally_without_device_control(mocker, action_type):
    from camctl.persistence.repositories.capture import CaptureRepository, CaptureResult

    runtime = _runtime(mocker, action_type=action_type)
    runtime.action.return_value["cancel_requested"] = 1
    runtime.capture = mocker.create_autospec(CaptureRepository, instance=True)
    runtime.capture.finish_canceled_capture.return_value = DbOutcome(
        DbOutcomeKind.COMPLETED, CaptureResult(action_status=6, plan_status=1, output_ids=()))
    runtime.capture.canceled_recording_results.return_value = ()
    runtime.pending_capture_completions = {}
    runtime.retry_gate = None
    runtime.resume_capture_completions.side_effect = lambda identity: CaptureRuntime.resume_capture_completions(
        runtime, identity)
    runtime.save_capture_completion.side_effect = lambda command: CaptureRuntime.save_capture_completion(
        runtime, command)
    await capture_handler(action_type)(2, runtime)
    runtime.capture.finish_canceled_capture.assert_called_once()
    command = runtime.capture.finish_canceled_capture.call_args.args[0]
    assert command.action_id == 2
    assert command.unstarted is True
    runtime.grant.assert_not_called()
    runtime.driver.control.assert_not_awaited()

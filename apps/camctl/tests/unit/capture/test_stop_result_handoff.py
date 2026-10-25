"""STOP 的固定启动锚点先读取，真实返回随后立即进入原结果责任。"""

import asyncio
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.capture.handlers import CaptureRuntime, DeviceStopPort, SessionRecordingState, _stop_call
from camctl.capture.recording import RecordingState
from camctl.contracts.enums import enum_for
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import DeviceCallResult
from camctl.devices.tasks import CaptureResultPolicy
from camctl.operations.attempts import BeginAttemptResult, BeginDisposition
from camctl.operations.models import AttemptTicket
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.scheduling import SchedulingRepository
from camctl.persistence.repositories.timelapse import TimelapseRepository
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from support.capture_database import capture_database

pytestmark = pytest.mark.asyncio


def _runtime(mocker):
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    connection.execute.return_value.fetchone.return_value = (71,)
    owned = OwnedConnection(connection,
        DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing"))
    operations = create_autospec(OperationRepository, instance=True, spec_set=True)
    ticket = AttemptTicket(1, "stop", "71", "stop/21", 9)
    operations.begin_attempt.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        BeginAttemptResult(BeginDisposition.GRANTED, ticket=ticket))
    port = create_autospec(SessionRecordingState, instance=True, spec_set=True)
    port.recording_state.return_value = RecordingState(False, True, True,
        61_000_000_000, 61_000_000_000, False, False, 0, 3, False)
    stopper = create_autospec(DeviceStopPort, instance=True, spec_set=True)
    stopper.stop.return_value = DeviceCallResult(error=None, observations=(
        DeviceObservation("stop_confirmed", 1, {"activity_id": "71"}),))
    runtime = CaptureRuntime(capture_database(owned),
        create_autospec(SchedulingRepository, instance=True), operations,
        create_autospec(CaptureRepository, instance=True),
        create_autospec(TimelapseRepository, instance=True), None, None, None,
        lambda: 1000, lambda: 65_000_000_000, None, None,
        recording_state=port, stopper=stopper)
    mocker.patch.object(runtime, "retry_wait_remaining", autospec=True, return_value=None)
    action = {"id": 21, "type": int(enum_for("actions.type").CAMERA_RECORD),
        "device_id": "cam-1", "driver_id": "dji-action6", "effective_params_json": {},
        "execution_spec_json": {"result_policy": CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1.value,
                                "target_duration_ms": 60_000}}
    return runtime, action, ticket, port


async def test_anchor_read_failure_prevents_dispatch_before_any_actual_stop_result(mocker):
    runtime, action, _, port = _runtime(mocker)
    failure = sqlite3.OperationalError("锚点读取失败")
    port.recording_state.side_effect = failure

    with pytest.raises(sqlite3.OperationalError) as raised:
        await _stop_call(runtime, action)

    assert raised.value is failure
    runtime.operations.begin_attempt.assert_not_called()
    runtime.stopper.stop.assert_not_awaited()
    assert not runtime.pending_start_results


async def test_cancellation_while_reading_anchor_does_not_issue_stop(mocker):
    runtime, action, _, port = _runtime(mocker)
    entered = asyncio.Event()
    async def reading(action_id):
        entered.set()
        await asyncio.Event().wait()
    port.recording_state.side_effect = reading
    task = asyncio.create_task(_stop_call(runtime, action))
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    runtime.operations.begin_attempt.assert_not_called()
    runtime.stopper.stop.assert_not_awaited()
    assert not runtime.pending_start_results


async def test_actual_stop_is_held_with_original_time_before_saving_or_further_reads(mocker):
    runtime, action, ticket, port = _runtime(mocker)
    async def stop(request):
        port.recording_state.assert_awaited_once_with(21)
        port.recording_state.side_effect = sqlite3.OperationalError("真实返回后禁止重读")
        return DeviceCallResult(error=None, observations=(
            DeviceObservation("stop_confirmed", 1, {"activity_id": "71"}),))
    runtime.stopper.stop.side_effect = stop
    async def save(identity, pending):
        assert runtime.pending_start_results[identity] is pending
        assert pending.finish.ticket == ticket
        assert pending.finish.occurred_at == 1000 and pending.returned_ns == 65_000_000_000
        assert pending.capture_stop and pending.host_timer_stop.control_elapsed_ns == 64_000_000_000
    saving = mocker.patch.object(runtime, "_save_call_result", autospec=True, side_effect=save)

    result = await _stop_call(runtime, action)

    assert result.phase == "confirmed"
    saving.assert_awaited_once()
    assert (ticket.run_id, ticket.attempt_id) in runtime.pending_start_results

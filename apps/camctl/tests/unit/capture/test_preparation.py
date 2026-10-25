"""设置准备的实际返回、可靠保存和当前会话凭据分别成立。"""

import asyncio
import sqlite3
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import pytest

pytestmark = pytest.mark.asyncio
from support.capture_database import capture_database
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection

from camctl.capture.baseline import BaselinePreparation, PreparationPhase
from camctl.capture.handlers import CaptureRuntime
from camctl.capture.preparation import (
    RuntimePreparationCache, advance_preparation, prepared_by, resume_preparation_saves,
    _recover_old_preparation, settle_preparation,
    preparation_dispatch_reason,
)
from camctl.contracts.values import ConsistencyError
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import CapturePreparationDriver, ControlDriver, DeviceCallResult
from camctl.operations.attempts import (
    AttemptConfig, BeginAttemptResult, BeginDisposition, FinishAttemptResult, FinishDisposition,
)
from camctl.operations.models import (
    AttemptTicket, CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis,
)
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.scheduling import SchedulingRepository
from camctl.persistence.repositories.capture import CaptureRepository


def _world(mocker, *, recover=True):
    runtime = create_autospec(CaptureRuntime, instance=True)
    runtime.owned = OwnedConnection(create_autospec(sqlite3.Connection, instance=True),
        DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing"))
    runtime.database = capture_database(runtime.owned)
    runtime.operations = create_autospec(OperationRepository, instance=True)
    runtime.scheduling = create_autospec(SchedulingRepository, instance=True)
    runtime.driver = create_autospec(ControlDriver, instance=True)
    runtime.capture = create_autospec(CaptureRepository, instance=True)
    runtime.preparation_driver = create_autospec(CapturePreparationDriver, instance=True)
    runtime.preparation_driver.prepared_capture_matches.return_value = True
    runtime.preparation_cache = RuntimePreparationCache()
    runtime.pending_baselines = {}
    runtime.pending_segment_writes = {}
    runtime.owned.connection.in_transaction = False
    runtime.recovery_max_event_id = 99
    runtime.session_key = "a" * 32
    runtime.prepare_lead_s = Decimal(10)
    runtime.start_config = AttemptConfig(3, Decimal("0.5"), Decimal(3))
    runtime.wall_us = create_autospec(lambda: 0, return_value=0)
    runtime.last_attempt.return_value = None
    action = {"id": 1, "type": 2, "device_id": "cam-1", "driver_id": "driver-1",
              "scheduled_at": 10_000_000, "max_delay_ms": 1000, "status": 1,
              "execution_started": 0, "cancel_requested": 0,
              "effective_params_json": {"duration_s": 1}, "execution_spec_json": {}}
    runtime.action.return_value = action
    runtime.evidence = EvidenceRegistry((
        EvidenceContract("returned", 1, "control", frozenset()),
        EvidenceContract("capture_preparation_completed", 1, "control", frozenset({"activity_id"}),
                         identity_field="activity_id"),
        EvidenceContract("adb_foreground_recovery", 1, "control", frozenset()),
        EvidenceContract("dispatch_prevented", 1, "control", frozenset()),
    ))
    ticket = AttemptTicket(1, "control", "7", f"prepare/7/{runtime.session_key}", 11)
    runtime.operations.begin_attempt.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        BeginAttemptResult(BeginDisposition.GRANTED, ticket))
    runtime.operations.finish_attempt.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        FinishAttemptResult(FinishDisposition.SAVED))
    outcome = CallOutcome(effect=EffectState.CONFIRMED,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("returned", 1)),
        observations=(DeviceObservation("capture_preparation_completed", 1, {"activity_id": "7"}),))
    runtime.preparation_driver.prepare_capture.return_value = DeviceCallResult.from_outcome(outcome)
    runtime.prepare_baseline.return_value = BaselinePreparation(PreparationPhase.READY)
    if recover:
        mocker.patch("camctl.capture.preparation._recover_old_preparation", autospec=True, return_value=True)
    mocker.patch("camctl.capture.preparation._activity", autospec=True,
                 return_value={"id": 7, "occupancy_state": 1, "dispatch_state": 1})
    return runtime, action, ticket


def _photo_world(mocker):
    runtime, action, ticket = _world(mocker)
    runtime.capture_preparation_supported = True
    action.update(type=1, driver_id="hasselblad_cfv100c",
        effective_params_json={"type": "hasselblad_photo", "format": "raw"},
        execution_spec_json={"output_storage": "host", "start_call_timeout_s": Decimal(180)})
    return runtime, action, ticket


@pytest.mark.asyncio
async def test_photo_prepares_settings_with_control_budget_before_start_without_creating_start(mocker):
    from camctl.capture.handlers import _prepared_start_ticket

    runtime, action, ticket = _photo_world(mocker)
    assert (await advance_preparation(runtime, 1)).phase is PreparationPhase.READY
    request = runtime.preparation_driver.prepare_capture.call_args.args[0]
    assert request.operation == "take_photo"
    assert request.timeout_s == Decimal("0.5")
    assert request.start_entry is None
    assert _prepared_start_ticket(runtime, action) == ticket
    assert action["status"] == 1 and action["execution_started"] == 0
    runtime.scheduling.start_action.assert_not_called()
    runtime.grant.assert_not_called()
    runtime.driver.control.assert_not_called()


@pytest.mark.asyncio
async def test_photo_setting_failure_ends_preparation_without_marking_execution_started(mocker):
    from camctl.operations.models import AttemptStatus, ErrorValue
    from camctl.persistence.repositories.capture import ActivityReleaseResult, ReleaseOutcome

    runtime, action, _ = _photo_world(mocker)
    runtime.preparation_driver.prepare_capture.return_value = DeviceCallResult.from_outcome(CallOutcome(
        status=AttemptStatus.FAILED, effect=EffectState.NO_EFFECT,
        error=ErrorValue("setting_failed", "device"),
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("returned", 1))))

    def fail(request, key, owned):
        action["status"] = 4
        return DbOutcome(DbOutcomeKind.COMPLETED)

    runtime.scheduling.fail_preparation.side_effect = fail
    runtime.capture.release_occupancy.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        ActivityReleaseResult(ReleaseOutcome.RELEASED))
    assert (await advance_preparation(runtime, 1)).phase is PreparationPhase.FAILED
    assert action["status"] == 4 and action["execution_started"] == 0
    assert runtime.scheduling.fail_preparation.call_args.args[0].reason == "device_failed"
    runtime.prepare_baseline.assert_not_called()
    runtime.grant.assert_not_called()
    runtime.driver.control.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["settings", "baseline"])
@pytest.mark.parametrize("status", [1, 2], ids=["pending", "running"])
@pytest.mark.parametrize("interruption", ["canceled", "window_ended", "terminal"])
async def test_returned_preparation_failure_obeys_current_action_before_business_failure(
    mocker, phase, status, interruption,
):
    from camctl.operations.models import AttemptStatus, ErrorValue
    from camctl.persistence.repositories.capture import ActivityReleaseResult, ReleaseOutcome

    runtime, action, _ = (_photo_world(mocker) if phase == "settings" else _world(mocker))
    action.update(status=status, execution_started=status - 1)
    actual_returned = False
    setting_error = ErrorValue("setting_failed", "device")
    baseline_error = ErrorValue("directory_read_failed", "device")
    failed_settings = DeviceCallResult.from_outcome(CallOutcome(
        status=AttemptStatus.FAILED, effect=EffectState.NO_EFFECT, error=setting_error,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("returned", 1))))
    failed_baseline = BaselinePreparation(PreparationPhase.FAILED, error=baseline_error)

    def interrupt():
        nonlocal actual_returned
        if interruption == "canceled":
            action["cancel_requested"] = 1
            if status == 1:
                action["status"] = 6
        elif interruption == "window_ended":
            runtime.wall_us.return_value = 11_000_001
        else:
            action["status"] = 3
        actual_returned = True

    async def return_settings(request):
        interrupt()
        return failed_settings

    async def return_baseline(action_id):
        interrupt()
        return failed_baseline

    if phase == "settings":
        runtime.preparation_driver.prepare_capture.side_effect = return_settings
    else:
        runtime.prepare_baseline.side_effect = return_baseline

    def cancel(current_runtime, action_id, *, unstarted):
        assert current_runtime is runtime and action_id == 1 and unstarted
        assert actual_returned
        assert runtime.operations.finish_attempt.call_count == 1
        action["status"] = 6

    canceled = mocker.patch("camctl.capture.handlers._finish_canceled_capture", autospec=True,
        side_effect=cancel)

    def expire(request, key, owned):
        action["status"] = 5
        return DbOutcome(DbOutcomeKind.COMPLETED)

    runtime.scheduling.expire_action.side_effect = expire
    runtime.scheduling.fail_preparation.return_value = DbOutcome(DbOutcomeKind.COMPLETED)

    def release(request, key, owned):
        assert actual_returned
        assert runtime.operations.finish_attempt.call_count == 1
        return DbOutcome(DbOutcomeKind.COMPLETED, ActivityReleaseResult(ReleaseOutcome.RELEASED))

    runtime.capture.release_occupancy.side_effect = release

    result = await advance_preparation(runtime, 1)

    assert result.phase is PreparationPhase.ABORTED
    assert action["status"] == {"canceled": 6, "window_ended": 5, "terminal": 3}[interruption]
    assert action["execution_started"] == status - 1
    runtime.operations.finish_attempt.assert_called_once()
    saved = runtime.operations.finish_attempt.call_args.args[0].outcome.outcome
    if phase == "settings":
        assert saved.error is setting_error and saved.status is AttemptStatus.FAILED
        runtime.prepare_baseline.assert_not_called()
    else:
        assert failed_baseline.error is baseline_error
        runtime.prepare_baseline.assert_awaited_once()
    assert canceled.call_count == int(interruption == "canceled" and status == 2)
    assert runtime.scheduling.expire_action.call_count == int(interruption == "window_ended")
    runtime.capture.release_occupancy.assert_called_once()
    runtime.scheduling.fail_preparation.assert_not_called()
    runtime.save_capture_completion.assert_not_called()
    runtime.scheduling.start_action.assert_not_called()
    runtime.grant.assert_not_called()
    runtime.driver.control.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [PreparationPhase.PENDING, PreparationPhase.WAITING])
async def test_unsettled_baseline_save_precedes_late_window_expiration(mocker, phase):
    runtime, _, _ = _world(mocker)
    original = BaselinePreparation(phase, database_error=RuntimeError("save_unconfirmed"))

    async def unsettled_baseline(action_id):
        runtime.wall_us.return_value = 11_000_001
        return original

    runtime.prepare_baseline.side_effect = unsettled_baseline

    assert await advance_preparation(runtime, 1) is original

    runtime.scheduling.expire_action.assert_not_called()
    runtime.capture.release_occupancy.assert_not_called()


async def test_running_cancellation_preserves_original_baseline_error_until_release(mocker):
    from camctl.capture import baseline, preparation
    from camctl.capture.baseline import PendingBaseline
    from camctl.devices.bindings import DeviceBinding
    from camctl.operations.models import ErrorValue
    from camctl.persistence.repositories.capture import (
        ActivityReleaseResult, FinishCanceledCapture, ReleaseOutcome,
    )
    from camctl.persistence.repositories.capture_facts import StartFacts

    runtime, action, _ = _world(mocker)
    action.update(status=2, execution_started=1, cancel_requested=1)
    error = ErrorValue("directory_read_failed", "device")
    pending = PendingBaseline(7, DeviceBinding("cam-1", "driver-1"), ("/DCIM",), error=error)
    runtime.pending_baselines[1] = pending
    activity = {"id": 7, "occupancy_state": 1, "dispatch_state": 1, "ownership_mode": 2}
    preparation._activity.return_value = activity
    mocker.patch.object(baseline, "row_facts", autospec=True, return_value=action)
    mocker.patch("camctl.persistence.repositories.capture_facts.load_start_facts", autospec=True,
        return_value=StartFacts(activity, None, 0, True, 1))
    runtime.capture.preparation_calls_resolved.return_value = True
    async def resume_baselines(action_id):
        return await baseline.resume_baseline_settlement(action_id, runtime=runtime)

    runtime.resume_baselines.side_effect = resume_baselines

    def finish(request):
        assert isinstance(request, FinishCanceledCapture) and not request.preparation_resolved
        action["status"] = 6

    def release(request, key, owned):
        assert request.preparation_error is error
        activity["occupancy_state"] = 2
        return DbOutcome(DbOutcomeKind.COMPLETED, ActivityReleaseResult(ReleaseOutcome.RELEASED))

    runtime.save_capture_completion.side_effect = finish
    runtime.capture.release_occupancy.side_effect = release

    assert await settle_preparation(runtime, 1)

    assert action["status"] == 6 and runtime.pending_baselines == {}
    runtime.capture.release_occupancy.assert_called_once()


@pytest.mark.asyncio
async def test_new_photo_session_repeats_only_settings_under_original_activity_without_starting(mocker):
    runtime, action, _ = _photo_world(mocker)
    await advance_preparation(runtime, 1)
    runtime.session_key = "b" * 32
    ticket = AttemptTicket(1, "control", "7", f"prepare/7/{runtime.session_key}", 12)
    runtime.operations.begin_attempt.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        BeginAttemptResult(BeginDisposition.GRANTED, ticket))

    assert (await advance_preparation(runtime, 1)).phase is PreparationPhase.READY

    assert runtime.preparation_driver.prepare_capture.await_count == 2
    assert prepared_by(runtime, action) == ticket
    assert runtime.operations.begin_attempt.call_args.args[0].target.activity_id == 7
    assert action["status"] == 1 and action["execution_started"] == 0
    runtime.scheduling.start_action.assert_not_called()
    runtime.driver.control.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("start_facts", [
    (1, 3, False), (3, 2, True), (2, 1, False),
])
async def test_photo_existing_start_always_keeps_original_start_chain(mocker, start_facts):
    from camctl.persistence.repositories.capture_facts import StartFacts

    runtime, action, _ = _photo_world(mocker)
    action.update(status=2, execution_started=1)
    status, effect, not_started = start_facts
    runtime.last_attempt.return_value = (status, effect, 0)
    mocker.patch("camctl.persistence.repositories.capture_facts.load_start_facts", autospec=True,
        return_value=StartFacts(None, {"status": 2}, 1, not_started, 4))
    assert await preparation_dispatch_reason(runtime, 1) == "already_dispatched"
    assert (await advance_preparation(runtime, 1)).phase is PreparationPhase.READY
    runtime.preparation_driver.prepare_capture.assert_not_called()
    runtime.prepare_baseline.assert_not_called()
    runtime.operations.begin_attempt.assert_not_called()


@pytest.mark.asyncio
async def test_preparation_ends_before_scheduled_time_without_starting_action(mocker):
    runtime, action, ticket = _world(mocker)
    result = await advance_preparation(runtime, 1)
    assert result.phase is PreparationPhase.READY
    assert action["status"] == 1 and action["execution_started"] == 0
    assert runtime.preparation_cache.owners[1].task.done()
    assert prepared_by(runtime, action) == ticket
    request = runtime.operations.begin_attempt.call_args.args[0]
    assert request.config == AttemptConfig(1, runtime.start_config.timeout_s)
    runtime.scheduling.start_action.assert_not_called()
    runtime.driver.control.assert_not_called()
    await advance_preparation(runtime, 1)
    runtime.preparation_driver.prepare_capture.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("world", [_world, _photo_world], ids=["recording", "photo"])
async def test_unknown_result_save_keeps_original_request_and_never_repeats_settings(mocker, world):
    runtime, action, ticket = world(mocker)
    runtime.operations.finish_attempt.return_value = DbOutcome(DbOutcomeKind.UNKNOWN, error=RuntimeError("unknown"))
    with pytest.raises(ConsistencyError, match="原完整申请"):
        await advance_preparation(runtime, 1)
    owner = runtime.preparation_cache.owners[1]
    original, key = owner.finish, owner.result_key
    assert prepared_by(runtime, action) is None
    runtime.operations.finish_attempt.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        FinishAttemptResult(FinishDisposition.SAVED))
    await resume_preparation_saves(runtime.database, runtime.preparation_cache, operations=runtime.operations)
    assert runtime.operations.finish_attempt.call_args.args[:2] == (original, key)
    assert prepared_by(runtime, action) == ticket
    await advance_preparation(runtime, 1)
    runtime.preparation_driver.prepare_capture.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("save_fails", [False, True])
@pytest.mark.parametrize("world", [_world, _photo_world], ids=["recording", "photo"])
async def test_second_cancellation_waits_for_actual_result_save_before_connection_close(mocker, save_fails, world):
    runtime, _, _ = world(mocker)
    entered, interrupted, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    closed = asyncio.Event()
    result = runtime.preparation_driver.prepare_capture.return_value

    async def call(request):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            interrupted.set()
            await release.wait()
        return result

    runtime.preparation_driver.prepare_capture.side_effect = call
    if save_fails:
        runtime.operations.finish_attempt.return_value = DbOutcome(DbOutcomeKind.UNKNOWN, error=RuntimeError("unknown"))

    async def advance_and_close():
        try:
            await advance_preparation(runtime, 1)
        finally:
            await runtime.database.close()
            closed.set()

    task = asyncio.create_task(advance_and_close())
    await entered.wait()
    task.cancel()
    await interrupted.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert not closed.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError) as failure:
        await task
    assert runtime.preparation_cache.owners[1].saved is not save_fails
    if save_fails:
        assert isinstance(failure.value.__cause__, ConsistencyError)
    runtime.operations.finish_attempt.assert_called_once()
    assert closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("world", [_world, _photo_world], ids=["recording", "photo"])
async def test_saved_preparation_without_current_driver_match_is_never_start_proof(mocker, world):
    runtime, action, _ = world(mocker)
    await advance_preparation(runtime, 1)
    runtime.preparation_driver.prepared_capture_matches.return_value = False
    assert prepared_by(runtime, action) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("world", [_world, _photo_world], ids=["recording", "photo"])
async def test_local_proof_check_receives_original_activity_ticket(mocker, world):
    runtime, action, ticket = world(mocker)
    runtime.preparation_driver.prepared_capture_matches.side_effect = (
        lambda request: request.ticket == ticket and request.prepared_by == ticket)
    assert (await advance_preparation(runtime, 1)).phase is PreparationPhase.READY
    assert prepared_by(runtime, action) == ticket
    runtime.preparation_driver.prepared_capture_matches.return_value = True
    runtime.session_key = "b" * 32
    assert prepared_by(runtime, action) is None


async def test_cold_preparation_recovers_original_responsibility_without_settings_or_success(mocker):
    from camctl.operations.attempts import RunOutcome

    runtime, action, _ = _world(mocker, recover=False)
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    cursor.fetchall.return_value = [(8, "prepare/7/" + "b" * 32, 7, 1, 1)]
    runtime.owned.connection.execute.return_value = cursor
    runtime._attempt_recovery_evidence.return_value = runtime.evidence
    assert await _recover_old_preparation(runtime, 1)
    owner = runtime.preparation_cache.owners[1]
    assert owner.saved and not owner.successful
    assert owner.finish.run_finish.status is RunOutcome.UNCONFIRMED
    assert owner.finish.recovery_max_event_id == 99
    assert prepared_by(runtime, action) is None
    runtime.preparation_driver.prepare_capture.assert_not_called()


@pytest.mark.asyncio
async def test_existing_start_with_unknown_effect_stays_on_original_chain(mocker):
    from camctl.persistence.repositories.capture_facts import StartFacts

    runtime, action, _ = _world(mocker)
    runtime.last_attempt.return_value = (1, 3, 0)
    mocker.patch("camctl.persistence.repositories.capture_facts.load_start_facts", autospec=True,
                 return_value=StartFacts(None, None, 1, False, None))
    assert (await advance_preparation(runtime, 1)).phase is PreparationPhase.READY
    runtime.preparation_driver.prepare_capture.assert_not_called()
    runtime.prepare_baseline.assert_not_called()
    runtime.operations.begin_attempt.assert_not_called()


@pytest.mark.asyncio
async def test_reliable_no_effect_start_can_prepare_without_resetting_execution(mocker):
    from camctl.persistence.repositories.capture_facts import StartFacts

    runtime, action, _ = _world(mocker)
    action.update(status=2, execution_started=1)
    runtime.last_attempt.return_value = (3, 2, 0)
    mocker.patch("camctl.persistence.repositories.capture_facts.load_start_facts", autospec=True,
                 return_value=StartFacts(None, None, 1, True, 4))
    assert (await advance_preparation(runtime, 1)).phase is PreparationPhase.READY
    assert action["status"] == 2 and action["execution_started"] == 1
    runtime.preparation_driver.prepare_capture.assert_awaited_once()
    runtime.scheduling.start_action.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("world", [_world, _photo_world], ids=["recording", "photo"])
async def test_terminal_preparation_releases_only_after_actual_settings_finish(mocker, world):
    from camctl.persistence.repositories.capture import ActivityReleaseResult, ReleaseOutcome

    runtime, action, _ = world(mocker)
    entered, release = asyncio.Event(), asyncio.Event()
    actual = runtime.preparation_driver.prepare_capture.return_value

    async def call(request):
        entered.set()
        await release.wait()
        return actual

    runtime.preparation_driver.prepare_capture.side_effect = call
    runtime.capture.release_occupancy.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        ActivityReleaseResult(ReleaseOutcome.RELEASED))
    task = asyncio.create_task(advance_preparation(runtime, 1))
    await entered.wait()
    action.update(status=6, cancel_requested=1)
    assert not await settle_preparation(runtime, 1)
    runtime.capture.release_occupancy.assert_not_called()
    release.set()
    assert (await task).phase is PreparationPhase.ABORTED
    runtime.prepare_baseline.assert_not_called()
    runtime.capture.release_occupancy.assert_called_once()
    runtime.driver.control.assert_not_called()


@pytest.mark.asyncio
async def test_aborted_baseline_without_changed_qualification_stops_with_diagnostic(mocker):
    runtime, _, _ = _world(mocker)
    runtime.prepare_baseline.return_value = BaselinePreparation(PreparationPhase.ABORTED)
    with pytest.raises(ConsistencyError, match="没有取消、终态或窗口结束依据"):
        await advance_preparation(runtime, 1)
    runtime.prepare_baseline.assert_awaited_once()
    runtime.preparation_driver.prepare_capture.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("world", [_world, _photo_world], ids=["recording", "photo"])
async def test_cancel_before_actual_task_enters_saves_reliable_not_dispatched(mocker, world):
    from camctl.capture.preparation import _wait_preparation_task

    runtime, _, _ = world(mocker)

    async def cancel_before_entry(task):
        task.cancel()
        await _wait_preparation_task(task)

    mocker.patch("camctl.capture.preparation._wait_preparation_task", autospec=True, side_effect=cancel_before_entry)
    with pytest.raises(asyncio.CancelledError):
        await advance_preparation(runtime, 1)
    owner = runtime.preparation_cache.owners[1]
    assert owner.saved and not owner.entered
    assert owner.finish.outcome.outcome.settlement.basis is SettlementBasis.NOT_DISPATCHED
    runtime.preparation_driver.prepare_capture.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("has_start", [False, True])
@pytest.mark.parametrize("policy", ["action6_video_segments_v1", "osmo360ii_record_segments_v1"])
async def test_running_preparation_failure_publishes_real_no_result_conclusion(mocker, has_start, policy):
    from camctl.capture.segment_models import SegmentSourceKind, PublicationKind, ResultCheckEndReason
    from camctl.capture.segment_storage import SegmentResultRef, ResultSetCreate
    from camctl.contracts.enums import enum_for
    from camctl.contracts.history_values import HistoryBoundary
    from camctl.operations.attempts import OperationKind
    from camctl.operations.models import AttemptStatus, ErrorValue
    from camctl.persistence.repositories.capture import ActivityReleaseResult, ReleaseOutcome
    from camctl.persistence.repositories.capture_facts import StartFacts
    from camctl.persistence.repositories.segments import SegmentRepository

    runtime, action, _ = _world(mocker)
    action.update(status=2, execution_started=1, execution_spec_json={"result_policy": policy})
    if has_start:
        runtime.last_attempt.return_value = (3, 2, 0)
        mocker.patch("camctl.persistence.repositories.capture_facts.load_start_facts", autospec=True,
            return_value=StartFacts(None, {"status": 2}, 1, True, 4))
    runtime.preparation_driver.prepare_capture.return_value = DeviceCallResult.from_outcome(CallOutcome(
        status=AttemptStatus.FAILED, effect=EffectState.NO_EFFECT, error=ErrorValue("setting_failed", "device"),
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("returned", 1))))
    runtime.operations.finish_stale_runs.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    runtime.capture.release_occupancy.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        ActivityReleaseResult(ReleaseOutcome.RELEASED))
    ref = SegmentResultRef(1, 3, 1)
    repository = create_autospec(SegmentRepository, instance=True)
    mocker.patch("camctl.capture.segment_completion.SegmentRepository", autospec=True, return_value=repository)
    repository.read_prepared_result_ref.return_value = None
    repository.prepare_result_set.return_value = ResultSetCreate(1, 7, None, SegmentSourceKind.NO_RESULTS,
        HistoryBoundary(1, 9), None, PublicationKind.RESULT, None, None, 0)
    repository.create_result_set.return_value = DbOutcome(DbOutcomeKind.COMPLETED, ref)
    repository.read_result_set.return_value = {"activity_id": 7,
        "state": int(enum_for("segment_sets.state").MEMBERS_FIXED)}
    repository.conclude_result_check.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    repository.decide_result.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    cursor.fetchone.side_effect = [None, (9,)]
    runtime.owned.connection.execute.return_value = cursor

    def finish(request, key, owned):
        action["status"] = 4
        return DbOutcome(DbOutcomeKind.COMPLETED)

    runtime.capture.finish_segment_recording.side_effect = finish
    assert (await advance_preparation(runtime, 1)).phase is PreparationPhase.FAILED
    conclusion = repository.conclude_result_check.call_args.args[0]
    assert conclusion.reason is ResultCheckEndReason.NO_RESULT_WORK
    assert conclusion.anchor_event_id == 9
    published = runtime.capture.finish_segment_recording.call_args.args[0]
    assert published.failure.code == "capture_failed"
    assert published.failure.details == {"activity_id": "7", "reason": "device_failed"}
    assert action["status"] == 4 and action["execution_started"] == 1
    assert runtime.operations.finish_stale_runs.call_count == (1 if has_start else 0)
    assert runtime.operations.begin_attempt.call_args.args[0].kind is OperationKind.PREPARE_CAPTURE
    runtime.scheduling.start_action.assert_not_called()


@pytest.mark.asyncio
async def test_cold_saved_preparation_conclusion_finishes_before_new_settings(mocker):
    from camctl.capture.media import RecordingFailure
    from camctl.capture.segment_storage import SegmentResultRef
    from camctl.contracts.enums import enum_for
    from camctl.persistence.repositories.capture import ActivityReleaseResult, ReleaseOutcome
    from camctl.persistence.repositories.segments import SegmentRepository

    runtime, action, _ = _world(mocker)
    action.update(status=2, execution_started=1, execution_spec_json={"result_policy": "action6_video_segments_v1"})
    ref = SegmentResultRef(1, 3, 1)
    row = {"id": 3, "action_id": 1, "activity_id": 7, "run_id": None,
        "source_txn_id": 1, "source_event_id": 9, "member_count": 0,
        "source_kind": int(enum_for("segment_sets.source_kind").NO_RESULTS),
        "state": int(enum_for("segment_sets.state").MEMBERS_FIXED)}
    repository = create_autospec(SegmentRepository, instance=True)
    mocker.patch("camctl.capture.segment_completion.SegmentRepository", autospec=True, return_value=repository)
    repository.read_prepared_result_ref.return_value = ref
    repository.read_result_set.return_value = row
    repository.decide_result.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    cursor.fetchone.side_effect = [(9,), (11,), (1,)]
    runtime.owned.connection.execute.return_value = cursor
    verify = mocker.patch("camctl.persistence.repositories.segment_check_conclusions.no_result_preparation_failure",
        autospec=True, return_value=RecordingFailure("capture_failed", {"activity_id": "7", "reason": "device_failed"}))

    def finish(request, key, owned):
        action["status"] = 4
        return DbOutcome(DbOutcomeKind.COMPLETED)

    runtime.capture.finish_segment_recording.side_effect = finish
    runtime.capture.release_occupancy.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        ActivityReleaseResult(ReleaseOutcome.RELEASED))
    assert (await advance_preparation(runtime, 1)).phase is PreparationPhase.ABORTED
    verify.assert_called_once_with(runtime.owned.connection, row, 9)
    runtime.preparation_driver.prepare_capture.assert_not_called()
    runtime.operations.begin_attempt.assert_not_called()
    runtime.driver.control.assert_not_called()
    repository.conclude_result_check.assert_not_called()
    assert runtime.capture.finish_segment_recording.call_args.args[0].ref == ref

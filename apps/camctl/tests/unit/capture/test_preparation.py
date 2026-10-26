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
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import CapturePreparationDriver, ControlDriver, DeviceCallResult
from camctl.operations.attempts import (
    AttemptConfig, BeginAttemptResult, BeginDisposition, FinishAttemptResult, FinishDisposition,
)
from camctl.operations.models import (
    AttemptTicket, CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis,
)
from camctl.persistence.models import DbOutcome, DbOutcomeKind, _attach_db_outcome
from camctl.persistence.business import BusinessDatabase, SharedSave
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
    activity_reader = mocker.patch("camctl.capture.preparation._activity", autospec=True,
        return_value={"id": 7, "occupancy_state": 1, "dispatch_state": 1})
    mocker.patch("camctl.capture.preparation.row_facts", autospec=True,
        side_effect=lambda connection, table, identity: action if table == "actions" else activity_reader.return_value)

    def execute(sql, parameters=()):
        if sql.startswith("SELECT id FROM device_activities WHERE action_id="):
            cursor = create_autospec(sqlite3.Cursor, instance=True)
            cursor.fetchone.return_value = (7,)
            return cursor
        if sql.startswith("SELECT a.status,a.effect_state,e.occurred_at FROM operation_attempts"):
            cursor = create_autospec(sqlite3.Cursor, instance=True)
            cursor.fetchone.return_value = runtime.last_attempt.return_value
            return cursor
        return runtime.owned.connection.execute.return_value

    runtime.owned.connection.execute.side_effect = execute
    return runtime, action, ticket


def _photo_world(mocker):
    runtime, action, ticket = _world(mocker)
    runtime.capture_preparation_supported = True
    action.update(type=1, driver_id="hasselblad_cfv100c",
        effective_params_json={"type": "hasselblad_photo", "format": "raw"},
        execution_spec_json={"output_storage": "host", "start_call_timeout_s": Decimal(180)})
    return runtime, action, ticket


async def test_existing_ready_preparation_has_no_new_completion_notice(mocker):
    runtime, _, _ = _world(mocker)
    runtime.prepare_baseline.return_value = BaselinePreparation(
        PreparationPhase.READY, wakeup_required=False)

    first = await advance_preparation(runtime, 1)
    second = await advance_preparation(runtime, 1)

    assert first.wakeup_required is True
    assert second.phase is PreparationPhase.READY
    assert second.wakeup_required is False
    runtime.preparation_driver.prepare_capture.assert_awaited_once()


async def test_first_session_setup_reserves_extra_lead_and_freezes_total_budget(mocker):
    from camctl.capture import preparation
    from camctl.persistence.repositories.scheduling import ReservePreparationOutcome, ReservePreparationResult

    runtime, action, _ = _world(mocker)
    runtime.start_config = AttemptConfig(3, Decimal("10"), Decimal("3"))
    runtime.session_setup_budget = mocker.Mock(return_value=Decimal("30"))
    runtime.wall_us.return_value = action["scheduled_at"] - 40_000_000
    preparation._activity.side_effect = (None, {"id": 7, "occupancy_state": 1, "dispatch_state": 1})
    runtime.scheduling.reserve_preparation.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        ReservePreparationResult(ReservePreparationOutcome.RESERVED, 7))

    assert (await advance_preparation(runtime, 1)).phase is PreparationPhase.READY

    reserve = runtime.scheduling.reserve_preparation.call_args.args[0]
    intent = runtime.operations.begin_attempt.call_args.args[0]
    request = runtime.preparation_driver.prepare_capture.call_args.args[0]
    assert reserve.prepare_lead_s == Decimal("40")
    assert intent.config.timeout_s == request.timeout_s == Decimal("40")
    assert request.session_setup_timeout_s == Decimal("30")
    runtime.session_setup_budget.return_value = Decimal("0")
    assert runtime.preparation_cache.owners[1].intent.config.timeout_s == Decimal("40")


@pytest.mark.parametrize("configured_lead", [Decimal("0"), Decimal("10")])
async def test_setup_cache_and_disabled_advance_keep_their_ordinary_time_boundaries(mocker, configured_lead):
    runtime, action, _ = _world(mocker)
    runtime.prepare_lead_s = configured_lead
    runtime.session_setup_budget = mocker.Mock(return_value=Decimal("0"))
    runtime.wall_us.return_value = action["scheduled_at"] - 30_000_000

    assert (await advance_preparation(runtime, 1)).phase is PreparationPhase.WAITING
    runtime.preparation_driver.prepare_capture.assert_not_awaited()

    runtime.session_setup_budget.return_value = Decimal("30")
    if configured_lead == 0:
        assert (await advance_preparation(runtime, 1)).phase is PreparationPhase.WAITING
        runtime.preparation_driver.prepare_capture.assert_not_awaited()


@pytest.mark.parametrize("action_type", [2, 3], ids=["recording", "timelapse"])
async def test_ready_preparation_stays_ready_after_action_starts_with_scope_closure(mocker, action_type):
    runtime, action, ticket = _world(mocker)
    action["type"] = action_type
    runtime.scope_closure_supported = True
    runtime.prepare_baseline.return_value = BaselinePreparation(
        PreparationPhase.READY, wakeup_required=False)
    original_execute = runtime.owned.connection.execute.side_effect

    def execute(sql, parameters=()):
        if sql.startswith("SELECT d.id,d.action_id"):
            cursor = create_autospec(sqlite3.Cursor, instance=True)
            cursor.fetchone.return_value = None
            return cursor
        return original_execute(sql, parameters)

    runtime.owned.connection.execute.side_effect = execute
    assert (await advance_preparation(runtime, 1)).phase is PreparationPhase.READY
    assert prepared_by(runtime, action) == ticket

    action.update(status=2, execution_started=1)
    runtime.wall_us.return_value = action["scheduled_at"]

    assert (await advance_preparation(runtime, 1)).phase is PreparationPhase.READY
    assert prepared_by(runtime, action) == ticket
    runtime.preparation_driver.prepare_capture.assert_awaited_once()


async def test_device_busy_reservation_does_not_report_completion_progress(mocker):
    from camctl.persistence.repositories.scheduling import ReservePreparationOutcome, ReservePreparationResult
    from camctl.capture import preparation

    runtime, _, _ = _world(mocker)
    preparation._activity.return_value = None
    runtime.scheduling.reserve_preparation.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        ReservePreparationResult(ReservePreparationOutcome.REJECTED, reason="device_busy"))

    for _ in range(3):
        result = await advance_preparation(runtime, 1)
        assert result.phase is PreparationPhase.WAITING
        assert result.wakeup_required is False
    runtime.preparation_driver.prepare_capture.assert_not_awaited()


async def test_actual_preparation_then_waiting_keeps_completion_progress(mocker):
    runtime, _, _ = _world(mocker)
    runtime.prepare_baseline.return_value = BaselinePreparation(
        PreparationPhase.WAITING, wakeup_required=False)

    result = await advance_preparation(runtime, 1)

    assert result.phase is PreparationPhase.WAITING
    assert result.wakeup_required is True
    runtime.preparation_driver.prepare_capture.assert_awaited_once()


async def test_recover_preparation_keeps_real_baseline_resume_progress_once(mocker):
    from camctl.capture.baseline import PendingBaseline
    from camctl.capture.preparation import recover_preparation
    from camctl.devices.bindings import DeviceBinding

    runtime, action, _ = _world(mocker)
    action["status"] = 3
    pending = PendingBaseline(7, DeviceBinding("cam-1", "driver-1"), ("/DCIM",), completed=True)
    runtime.pending_baselines[1] = pending
    from camctl.capture import preparation
    preparation._activity.return_value = {"id": 7, "occupancy_state": 2, "dispatch_state": 1}
    async def resume(identity):
        return await CaptureRuntime.resume_baselines(runtime, identity)

    runtime.resume_baselines.side_effect = resume
    notices = []
    assert await recover_preparation(runtime, 1, on_progress=lambda: notices.append(True))
    assert notices == [True]
    assert pending.closed
    assert await recover_preparation(runtime, 1, on_progress=lambda: notices.append(True))
    assert notices == [True]


async def test_terminal_preparation_discards_delivered_progress_after_one_result(mocker):
    from camctl.capture.preparation import discard_terminal_preparation

    runtime, action, _ = _world(mocker)
    action["status"] = 3
    runtime.preparation_cache.changed_actions.add(1)
    assert await discard_terminal_preparation(runtime, 1) is True
    assert runtime.preparation_cache.changed_actions == set()
    assert await discard_terminal_preparation(runtime, 1) is False


def _held_save(runtime, phase):
    """在真实数据库端口的等待边界暂停；不运行执行器或访问数据库。"""
    original = runtime.database
    database = create_autospec(BusinessDatabase, instance=True)
    database.read.side_effect = original.read
    entered, release = asyncio.Event(), asyncio.Event()
    keys = []

    async def write(callback, *, key, description="业务写入"):
        cache = runtime.preparation_cache
        owner = cache.owners.get(1)
        target = ((phase == "intent" and owner is not None and key == owner.intent_key)
                  or (phase == "result" and owner is not None and key == owner.result_key)
                  or (phase == "reservation" and 1 in cache.reservations
                      and key == cache.reservations[1][1])
                  or (phase == "ending" and 1 in cache.endings and key == cache.endings[1][2]))
        if target:
            keys.append(key)
            entered.set()
            await release.wait()
        return callback(runtime.owned)

    database.write.side_effect = write
    runtime.database = database
    return entered, release, keys


async def _resume_while_held(runtime):
    task = asyncio.create_task(resume_preparation_saves(runtime.database,
        runtime.preparation_cache, operations=runtime.operations,
        scheduling=runtime.scheduling, capture=runtime.capture))
    # 两个入口只等待受控事件；让可运行任务抵达同一保存等待点。
    for _ in range(10):
        await asyncio.sleep(0)
    return task


@pytest.mark.parametrize("phase", ["intent", "result"])
async def test_device_preparation_and_recovery_join_one_original_save(mocker, phase):
    from camctl.capture.handlers import _prepared_start_ticket

    runtime, action, ticket = _world(mocker)
    runtime.capture_preparation_supported = True
    entered, release, keys = _held_save(runtime, phase)
    device = asyncio.create_task(advance_preparation(runtime, 1))
    await entered.wait()
    recovered = await _resume_while_held(runtime)
    owner = runtime.preparation_cache.owners[1]
    assert not owner.saved
    runtime.prepare_baseline.assert_not_awaited()
    release.set()
    result, _ = await asyncio.gather(device, recovered)

    assert len(keys) == 1
    assert result.phase is PreparationPhase.READY
    assert owner.saved and owner.task.done()
    assert _prepared_start_ticket(runtime, action) == ticket
    runtime.prepare_baseline.assert_awaited_once_with(1)
    runtime.preparation_driver.prepare_capture.assert_awaited_once()
    runtime.operations.begin_attempt.assert_called_once()
    runtime.operations.finish_attempt.assert_called_once()


@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_canceled_recovery_waiter_preserves_shared_actual_result_save(mocker, cancel_count):
    runtime, _, _ = _world(mocker)
    entered, release, keys = _held_save(runtime, "result")
    device = asyncio.create_task(advance_preparation(runtime, 1))
    await entered.wait()
    recovered = await _resume_while_held(runtime)
    for _ in range(cancel_count):
        recovered.cancel()
        await asyncio.sleep(0)
    assert not recovered.done()
    assert not runtime.preparation_cache.owners[1].saved
    release.set()

    assert (await device).phase is PreparationPhase.READY
    with pytest.raises(asyncio.CancelledError):
        await recovered
    assert runtime.preparation_cache.owners[1].saved
    assert len(keys) == 1
    runtime.preparation_driver.prepare_capture.assert_awaited_once()


@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_cancel_during_intent_save_closes_unentered_preparation_before_release(mocker, cancel_count):
    from camctl.persistence.repositories.capture import ActivityReleaseResult, ReleaseOutcome

    runtime, action, _ = _world(mocker)
    entered, release, keys = _held_save(runtime, "intent")
    device = asyncio.create_task(advance_preparation(runtime, 1))
    await entered.wait()
    for _ in range(cancel_count):
        device.cancel()
        await asyncio.sleep(0)
    assert not device.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await device
    owner = runtime.preparation_cache.owners[1]

    assert owner.saved and not runtime.preparation_cache.pending(1)
    assert owner.finish.outcome.outcome.settlement.basis is SettlementBasis.NOT_DISPATCHED
    assert len(keys) == 1
    runtime.preparation_driver.prepare_capture.assert_not_awaited()
    runtime.operations.finish_attempt.assert_called_once()
    action.update(status=6, cancel_requested=1)
    runtime.capture.release_occupancy.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        ActivityReleaseResult(ReleaseOutcome.RELEASED))
    assert await settle_preparation(runtime, 1)
    runtime.capture.release_occupancy.assert_called_once()


async def test_canceled_unknown_intent_closes_only_after_original_ticket_is_confirmed(mocker):
    from camctl.capture.preparation import recover_preparation
    from camctl.persistence.repositories.capture import ActivityReleaseResult, ReleaseOutcome

    runtime, action, ticket = _world(mocker)
    runtime.operations.begin_attempt.return_value = DbOutcome(DbOutcomeKind.UNKNOWN,
        error=RuntimeError("意图结果未知"))
    entered, release, keys = _held_save(runtime, "intent")
    device = asyncio.create_task(advance_preparation(runtime, 1))
    await entered.wait()
    device.cancel("原准备等待取消")
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(ConsistencyError) as failed:
        await device
    assert isinstance(failed.value.__cause__, asyncio.CancelledError)
    owner = runtime.preparation_cache.owners[1]
    assert owner.ticket is None and owner.finish is None
    runtime.operations.finish_attempt.assert_not_called()

    runtime.wall_us.return_value = 50_000_000
    action.update(status=6, cancel_requested=1)
    runtime.operations.begin_attempt.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        BeginAttemptResult(BeginDisposition.GRANTED, ticket))
    runtime.capture.release_occupancy.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        ActivityReleaseResult(ReleaseOutcome.RELEASED))
    assert await recover_preparation(runtime, 1)

    assert owner.saved and owner.finish.ticket == ticket
    assert owner.returned_at == 0
    assert owner.finish.outcome.outcome.settlement.basis is SettlementBasis.NOT_DISPATCHED
    assert owner.finish.outcome.outcome.error.code == "canceled"
    assert keys == [owner.intent_key, owner.intent_key]
    assert runtime.operations.finish_attempt.call_args.args[1] == owner.result_key
    runtime.preparation_driver.prepare_capture.assert_not_awaited()
    runtime.capture.release_occupancy.assert_called_once()


async def test_unknown_intent_without_waiter_cancel_does_not_invent_cancel_result(mocker):
    runtime, _, _ = _world(mocker)
    runtime.operations.begin_attempt.return_value = DbOutcome(DbOutcomeKind.UNKNOWN,
        error=RuntimeError("意图结果未知"))
    with pytest.raises(ConsistencyError):
        await advance_preparation(runtime, 1)
    owner = runtime.preparation_cache.owners[1]
    assert owner.ticket is None and owner.actual is None and owner.finish is None
    runtime.operations.finish_attempt.assert_not_called()
    runtime.preparation_driver.prepare_capture.assert_not_awaited()


@pytest.mark.parametrize("kind", [DbOutcomeKind.UNKNOWN, DbOutcomeKind.ROLLED_BACK])
async def test_concurrent_failed_save_keeps_original_request_for_later_recovery(mocker, kind):
    runtime, action, ticket = _world(mocker)
    runtime.operations.finish_attempt.return_value = DbOutcome(kind, error=RuntimeError("未确认"))
    entered, release, keys = _held_save(runtime, "result")
    device = asyncio.create_task(advance_preparation(runtime, 1))
    await entered.wait()
    recovered = await _resume_while_held(runtime)
    owner = runtime.preparation_cache.owners[1]
    original, key = owner.finish, owner.result_key
    release.set()
    results = await asyncio.gather(device, recovered, return_exceptions=True)

    assert all(isinstance(result, ConsistencyError) for result in results)
    assert len(keys) == 1 and not owner.saved
    assert prepared_by(runtime, action) is None
    runtime.operations.finish_attempt.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        FinishAttemptResult(FinishDisposition.SAVED))
    await resume_preparation_saves(runtime.database, runtime.preparation_cache,
        operations=runtime.operations)
    assert runtime.operations.finish_attempt.call_args.args[:2] == (original, key)
    assert prepared_by(runtime, action) == ticket
    assert (await advance_preparation(runtime, 1)).phase is PreparationPhase.READY
    runtime.preparation_driver.prepare_capture.assert_awaited_once()


async def test_reservation_and_recovery_join_one_original_request(mocker):
    from camctl.persistence.repositories.scheduling import ReservePreparationOutcome, ReservePreparationResult

    runtime, _, _ = _world(mocker)
    from camctl.capture import preparation
    activity = preparation._activity
    activity.return_value = None

    def reserve(request, key, owned):
        activity.return_value = {"id": 7, "occupancy_state": 1, "dispatch_state": 1}
        return DbOutcome(DbOutcomeKind.COMPLETED,
            ReservePreparationResult(ReservePreparationOutcome.RESERVED, 7))

    runtime.scheduling.reserve_preparation.side_effect = reserve
    entered, release, keys = _held_save(runtime, "reservation")
    device = asyncio.create_task(advance_preparation(runtime, 1))
    await entered.wait()
    recovered = await _resume_while_held(runtime)
    release.set()
    result, _ = await asyncio.gather(device, recovered)

    assert len(keys) == 1 and not runtime.preparation_cache.reservations
    assert result.phase is PreparationPhase.READY
    runtime.scheduling.reserve_preparation.assert_called_once()


@pytest.mark.parametrize("kind", ["release", "fail_preparation", "expire_action"])
async def test_ending_and_recovery_join_one_original_request(mocker, kind):
    from camctl.capture.preparation import _end
    from camctl.persistence.repositories.capture import ActivityReleaseResult, ReleaseOutcome

    runtime, _, _ = _world(mocker)
    runtime.capture.release_occupancy.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        ActivityReleaseResult(ReleaseOutcome.RELEASED))
    runtime.scheduling.fail_preparation.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    runtime.scheduling.expire_action.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    entered, release, keys = _held_save(runtime, "ending")
    ending = asyncio.create_task(_end(runtime, 1, kind, object()))
    await entered.wait()
    recovered = await _resume_while_held(runtime)
    release.set()
    await asyncio.gather(ending, recovered)

    assert len(keys) == 1 and not runtime.preparation_cache.endings


def _cached_request(runtime, phase):
    from camctl.persistence.repositories.capture import ActivityReleaseResult, ReleaseOutcome

    request, key = object(), new_operation_key()
    if phase == "reservation":
        held = runtime.preparation_cache.reservations
        held[1] = original = request, key
        method = runtime.scheduling.reserve_preparation
        completed = DbOutcome(DbOutcomeKind.COMPLETED)
    else:
        held = runtime.preparation_cache.endings
        held[1] = original = "release", request, key
        method = runtime.capture.release_occupancy
        completed = DbOutcome(DbOutcomeKind.COMPLETED, ActivityReleaseResult(ReleaseOutcome.RELEASED))
    method.return_value = completed
    return held, original, method, completed


@pytest.mark.parametrize("phase", ["reservation", "ending"])
@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_canceled_cached_request_waiter_applies_original_before_cancel(mocker, phase, cancel_count):
    runtime, _, _ = _world(mocker)
    held, _, method, _ = _cached_request(runtime, phase)
    entered, release, keys = _held_save(runtime, phase)
    original = asyncio.create_task(resume_preparation_saves(runtime.database,
        runtime.preparation_cache, scheduling=runtime.scheduling, capture=runtime.capture))
    await entered.wait()
    recovered = await _resume_while_held(runtime)
    for _ in range(cancel_count):
        recovered.cancel()
        await asyncio.sleep(0)
    assert not recovered.done() and 1 in held
    release.set()
    await original
    with pytest.raises(asyncio.CancelledError):
        await recovered
    assert not held and not runtime.preparation_cache.request_saves
    assert len(keys) == 1
    method.assert_called_once()


@pytest.mark.parametrize("phase", ["reservation", "ending"])
@pytest.mark.parametrize("kind", [DbOutcomeKind.UNKNOWN, DbOutcomeKind.ROLLED_BACK, DbOutcomeKind.NOT_EXECUTED])
async def test_cached_request_failure_recovers_same_input_and_key(mocker, phase, kind):
    runtime, _, _ = _world(mocker)
    held, original, method, completed = _cached_request(runtime, phase)
    method.return_value = DbOutcome(kind, error=RuntimeError("未可靠保存"))
    entered, release, keys = _held_save(runtime, phase)
    pending = asyncio.create_task(resume_preparation_saves(runtime.database,
        runtime.preparation_cache, scheduling=runtime.scheduling, capture=runtime.capture))
    await entered.wait()
    recovered = await _resume_while_held(runtime)
    release.set()
    results = await asyncio.gather(pending, recovered, return_exceptions=True)
    assert all(isinstance(result, ConsistencyError) for result in results)
    assert held[1] is original and len(keys) == 1

    method.return_value = completed
    await resume_preparation_saves(runtime.database, runtime.preparation_cache,
        scheduling=runtime.scheduling, capture=runtime.capture)
    assert not held and not runtime.preparation_cache.request_saves
    assert keys == [original[-1], original[-1]]
    assert method.call_args.args[:2] == (original[-2], original[-1])


@pytest.mark.parametrize("phase", ["reservation", "ending"])
async def test_cached_request_notification_failure_applies_once_before_delivery(mocker, phase):
    runtime, _, _ = _world(mocker)
    held, _, method, completed = _cached_request(runtime, phase)
    failure = RuntimeError("提交后的通知错误")
    _attach_db_outcome(failure, completed)
    method.side_effect = failure
    entered, release, keys = _held_save(runtime, phase)
    pending = asyncio.create_task(resume_preparation_saves(runtime.database,
        runtime.preparation_cache, scheduling=runtime.scheduling, capture=runtime.capture))
    await entered.wait()
    recovered = await _resume_while_held(runtime)
    release.set()
    assert await asyncio.gather(pending, recovered, return_exceptions=True) == [failure, failure]
    assert not held and not runtime.preparation_cache.request_saves
    await resume_preparation_saves(runtime.database, runtime.preparation_cache,
        scheduling=runtime.scheduling, capture=runtime.capture)
    assert len(keys) == 1
    method.assert_called_once()


async def test_completed_cached_request_releases_its_save_when_next_phase_already_exists(mocker):
    runtime, _, _ = _world(mocker)
    held, original, _, _ = _cached_request(runtime, "ending")
    replacement = "release", object(), new_operation_key()
    saving = create_autospec(SharedSave, instance=True)

    async def finish_then_begin_next(save_and_apply):
        value = await save_and_apply()
        held[1] = replacement
        return value

    saving.run.side_effect = finish_then_begin_next
    runtime.preparation_cache.request_saves[original[-1]] = saving
    await resume_preparation_saves(runtime.database, runtime.preparation_cache,
        scheduling=runtime.scheduling, capture=runtime.capture)

    assert held[1] is replacement
    assert original[-1] not in runtime.preparation_cache.request_saves


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

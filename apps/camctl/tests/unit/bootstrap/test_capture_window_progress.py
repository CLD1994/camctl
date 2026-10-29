"""设备长任务不占有后续动作的窗口观察责任。"""

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.bootstrap.flows import CaptureFlow
from camctl.capture.handlers import CaptureRuntime, HandlerOutcome
from camctl.contracts.clock import ClockPort
from camctl.contracts.pages import Page
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.scheduling import (
    ExpireActionResult, ExpireOutcome, ObserveOutcome, ObserveWindowResult,
    SchedulingRepository, StartActionResult, StartOutcome,
)
from camctl.persistence.runtime import (
    CURRENT_FORMAT_VERSION, DatabaseMetadata, OwnedConnection,
)
from camctl.outputs.read_discovery import ReadDiscoveryOwner
from camctl.scheduling.notifications import WakeReason, WorkNotifier
from support.business_database import inline_business_database

pytestmark = pytest.mark.asyncio


@dataclass
class _Action:
    identity: int
    scheduled_at: int
    max_delay_ms: int
    status: int = 1
    first_observed_at: int | None = None
    expiration_reason: int | None = None
    execution_started: int = 0
    cancel_requested: int = 0


class _CaptureState:
    """通过真实查询和仓储端口交付受控的内存状态。"""

    def __init__(self, mocker, actions):
        self.actions = {action.identity: action for action in actions}
        self.now = 0
        self.clock = create_autospec(ClockPort, instance=True, spec_set=True)
        self.clock.utc_micros.side_effect = lambda: self.now
        self.connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
        self.connection.execute.side_effect = self._execute
        self.owned = OwnedConnection(self.connection, DatabaseMetadata(
            "camctl", "a" * 32, CURRENT_FORMAT_VERSION,
            "/staging", "/ready", "/processing"))
        self.database = inline_business_database(self.owned)
        self.context = SimpleNamespace(
            database=self.database, clock=self.clock, wake=None)
        self.discovery = create_autospec(ReadDiscoveryOwner(100), spec_set=True)
        self.discovery.batch_size = 100
        self.discovery.pending = False
        self.discovery.captures.side_effect = self._discover
        self.repository = create_autospec(SchedulingRepository, instance=True, spec_set=True)
        self.repository.observe_window.side_effect = self._observe
        self.repository.expire_action.side_effect = self._expire
        self.repository.start_action.side_effect = self._start
        mocker.patch("camctl.bootstrap.flows.SchedulingRepository", return_value=self.repository)
        self.runtime = create_autospec(CaptureRuntime, instance=True)
        self.runtime.binding_check = None

        async def capture_factory(database, device_id):
            return self.runtime
        self.factory = create_autospec(capture_factory, return_value=self.runtime)
        self.flow = CaptureFlow(self.factory)
        self.flow._discovery = self.discovery

    async def _discover(self, database, now, preparation_leads=None):
        assert database is self.database
        return Page(tuple((action.identity, "cam-1", action.scheduled_at, 1, action.identity)
                          for action in self.actions.values()
                          if action.status in (1, 2) and action.scheduled_at <= now), None)

    def _execute(self, query, parameters):
        cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
        now = parameters[0]
        identities = parameters[2:-1] if "device_id=?" in query else parameters[1:-1]
        selected = [action for action in self.actions.values()
                    if action.identity in identities and action.scheduled_at <= now]
        if query.startswith("SELECT id, device_id, scheduled_at, max_delay_ms"):
            rows = [(action.identity, "cam-1", action.scheduled_at, action.max_delay_ms,
                     action.first_observed_at)
                    for action in selected if action.status == 1 and action.cancel_requested == 0]
            if "first_window_observed_at" not in query:
                rows = [row[:4] for row in rows]
        else:
            assert query.startswith("SELECT id, type FROM actions WHERE status = 2")
            rows = [(action.identity, 2) for action in selected if action.status == 2]
        cursor.fetchall.return_value = rows
        return cursor

    def _observe(self, request, key, owned):
        action = self.actions[request.action_id]
        assert action.scheduled_at <= request.trusted_wall_now <= action.scheduled_at + action.max_delay_ms * 1000
        if action.first_observed_at is None:
            action.first_observed_at = request.trusted_wall_now
        return DbOutcome(DbOutcomeKind.COMPLETED,
                         ObserveWindowResult(ObserveOutcome.OBSERVED, action.first_observed_at))

    def _expire(self, request, key, owned):
        action = self.actions[request.action_id]
        assert request.trusted_wall_now > action.scheduled_at + action.max_delay_ms * 1000
        action.status = 5
        action.expiration_reason = 2 if action.first_observed_at is not None else 1
        return DbOutcome(DbOutcomeKind.COMPLETED,
                         ExpireActionResult(ExpireOutcome.EXPIRED, action.expiration_reason))

    def _start(self, request, key, owned):
        action = self.actions[request.action_id]
        if request.trusted_wall_now > action.scheduled_at + action.max_delay_ms * 1000:
            result = StartActionResult(StartOutcome.REJECTED, reason="window_ended")
        else:
            assert action.first_observed_at is not None
            action.status = 2
            action.execution_started = 1
            result = StartActionResult(StartOutcome.STARTED, action.identity)
        return DbOutcome(DbOutcomeKind.COMPLETED, result)


@pytest.mark.parametrize("observed,now,observations,expirations", [
    (None, 999, 0, 0),
    (None, 1000, 1, 0),
    (None, 2000, 1, 0),
    (1000, 1000, 0, 0),
    (1000, 1500, 0, 0),
    (1000, 2000, 0, 0),
    (None, 2001, 0, 1),
    (1000, 2001, 0, 1),
])
async def test_window_changes_only_write_at_first_observation_or_after_inclusive_end(
        mocker, observed, now, observations, expirations):
    action = _Action(1, 1000, 1, first_observed_at=observed)
    state = _CaptureState(mocker, (action,))
    state.now = now

    await state.flow._observe_windows(state.context, state.database, (1,))

    assert state.repository.observe_window.call_count == observations
    assert state.repository.expire_action.call_count == expirations
    assert action.first_observed_at == (now if observations else observed)
    if expirations:
        assert action.expiration_reason == (1 if observed is None else 2)


@pytest.mark.parametrize("rounds", [145, 414])
async def test_repeated_window_checks_save_one_observation(mocker, rounds):
    action = _Action(1, 1000, 1000)
    state = _CaptureState(mocker, (action,))
    state.now = 1000

    for _ in range(rounds):
        await state.flow._observe_windows(state.context, state.database, (1,))

    assert state.repository.observe_window.call_count == 1
    state.repository.expire_action.assert_not_called()
    assert action.first_observed_at == 1000


async def test_expiration_records_action_window_and_actual_reason_once(mocker):
    from camctl.logging_runtime.events import event_logging

    action = _Action(101, 1000, 1)
    state = _CaptureState(mocker, (action,))
    state.now = 2001
    records = []
    channel = SimpleNamespace(slog=lambda record: records.append(json.loads(record.rendered_message())))
    with event_logging(channel):
        await state.flow._observe_windows(state.context, state.database, (101,))
        await state.flow._observe_windows(state.context, state.database, (101,))
    assert len(records) == 1
    assert {key: records[0][key] for key in (
        'stage', 'event', 'level', 'action_id', 'scheduled_at_us', 'max_delay_ms',
        'observed_at_us', 'expiration_reason')} == {
        'stage': 'scheduling.window', 'event': 'expired', 'level': 'INFO',
        'action_id': 101, 'scheduled_at_us': 1000, 'max_delay_ms': 1,
        'observed_at_us': 2001, 'expiration_reason': 1}


@pytest.mark.parametrize("status,cancel_requested", [
    (1, 1), (2, 0), (3, 0), (4, 0), (5, 0), (6, 1),
])
async def test_window_scan_does_not_write_for_cancelled_or_nonpending_actions(
        mocker, status, cancel_requested):
    state = _CaptureState(mocker, (
        _Action(1, 1000, 1, status=status, cancel_requested=cancel_requested),))
    state.now = 1500

    await state.flow._observe_windows(state.context, state.database, (1,))

    state.repository.observe_window.assert_not_called()
    state.repository.expire_action.assert_not_called()


async def test_saved_observation_does_not_hide_new_candidate_or_actual_task_completion(mocker):
    first = _Action(1, 0, 1000, status=2)
    state = _CaptureState(mocker, (first,))
    state.context.wake = WorkNotifier()
    entered, release = asyncio.Event(), asyncio.Event()

    async def finish_actual_work(runtime, descriptors):
        entered.set()
        await release.wait()
        first.status = 3
        return [(first.identity, HandlerOutcome("terminal"))]

    mocker.patch("camctl.bootstrap.flows.dispatch_ready", autospec=True,
                 side_effect=finish_actual_work)
    try:
        await state.flow(state.context)
        await asyncio.wait_for(entered.wait(), timeout=1)
        later = _Action(2, 1000, 1000)
        state.actions[2] = later
        state.now = 1000
        await state.flow(state.context)
        assert later.first_observed_at == 1000
        await state.flow(state.context)
        assert state.repository.observe_window.call_count == 1

        token = state.context.wake.snapshot()
        release.set()
        notice = await asyncio.wait_for(state.context.wake.wait_changed(token), timeout=1)
        assert WakeReason.ACTIVITY_FINISHED in notice.reasons
        mocker.patch("camctl.capture.residual.pass_residual_gate", autospec=True, return_value=True)
        await state.flow(state.context)
        await state.flow.settle()
        assert later.status == 2
        assert state.repository.observe_window.call_count == 1
    finally:
        release.set()
        await state.flow.settle()


@pytest.mark.parametrize("wakeup_required", [False, True])
async def test_preparation_completion_uses_explicit_recheck_fact(mocker, wakeup_required):
    from camctl.capture.baseline import BaselinePreparation, PreparationPhase

    state = _CaptureState(mocker, (_Action(1, 0, 1000, first_observed_at=0),))
    state.context.wake = WorkNotifier()
    state.runtime.capture_preparation_supported = True
    release = asyncio.Event()

    async def prepare(runtime, action_id):
        await release.wait()
        return BaselinePreparation(PreparationPhase.WAITING, wakeup_required=wakeup_required)

    mocker.patch("camctl.capture.preparation.advance_preparation", autospec=True, side_effect=prepare)
    mocker.patch("camctl.capture.preparation.discard_terminal_preparation", autospec=True, return_value=False)
    mocker.patch("camctl.capture.residual._original_binding_failure", autospec=True, return_value=None)
    mocker.patch("camctl.capture.residual.pass_residual_gate", autospec=True, return_value=True)
    mocker.patch("camctl.bootstrap.flows.dispatch_ready", autospec=True, return_value=[])
    try:
        for _ in range(6):
            release.clear()
            await state.flow(state.context)
            release.set()
            await state.flow.settle()
        assert state.context.wake.snapshot().version == (6 if wakeup_required else 0)
        state.repository.start_action.assert_not_called()
        state.repository.observe_window.assert_not_called()
    finally:
        release.set()
        await state.flow.settle()


async def test_capture_failure_notifies_before_the_retained_error_is_consumed(mocker):
    state = _CaptureState(mocker, (_Action(1, 0, 1000, first_observed_at=0),))
    state.context.wake = WorkNotifier()
    entered, release = asyncio.Event(), asyncio.Event()
    error = RuntimeError("设备流程实际错误")

    async def fail(database, device_id):
        entered.set()
        await release.wait()
        raise error

    state.factory.side_effect = fail
    await state.flow(state.context)
    await entered.wait()
    token = state.context.wake.snapshot()
    release.set()
    task = state.flow._devices["cam-1"].unconsumed_task
    await asyncio.shield(task)
    await asyncio.sleep(0)
    assert state.context.wake.snapshot().version > token.version
    assert state.flow._devices["cam-1"].unconsumed_task is task
    with pytest.raises(RuntimeError) as caught:
        await state.flow.settle()
    assert caught.value is error


async def test_capture_existing_fixed_baseline_before_schedule_does_not_self_wake(mocker):
    from camctl.capture.baseline import prepare_baseline
    from camctl.capture.baseline_models import BaselineRef, BaselineState
    from camctl.persistence.repositories.capture import CaptureRepository

    action = _Action(1, 10_000_000, 1000)
    state = _CaptureState(mocker, (action,))
    state.context.wake = WorkNotifier()
    state.runtime.capture_preparation_supported = True
    state.runtime.pending_baselines = {}
    state.runtime.database = state.database
    state.runtime.capture = create_autospec(CaptureRepository, instance=True, spec_set=True)
    state.flow._preparation_leads = {"cam-1": Decimal(10)}
    state.discovery.captures.side_effect = None
    state.discovery.captures.return_value = Page(((1, "cam-1", action.scheduled_at, 1, 1),), None)
    mocker.patch("camctl.outputs.read_discovery.next_preparation_at", autospec=True, return_value=None)
    mocker.patch("camctl.capture.residual._original_binding_failure", autospec=True, return_value=None)
    mocker.patch("camctl.capture.residual.pass_residual_gate", autospec=True, return_value=True)
    mocker.patch("camctl.bootstrap.flows.dispatch_ready", autospec=True, return_value=[])
    mocker.patch("camctl.capture.baseline.row_facts", autospec=True,
        side_effect=lambda connection, table, identity: (
            {"id": 1} if table == "actions" else {"id": 7, "action_id": 1, "ownership_mode": 2}))
    state.runtime.capture.baseline_ref.return_value = BaselineRef(7, BaselineState.FIXED, 11, 12, 2, 136)

    async def prepare(runtime, action_id):
        return await prepare_baseline(action_id, 7, runtime=runtime)

    mocker.patch("camctl.capture.preparation.advance_preparation", autospec=True, side_effect=prepare)
    for _ in range(6):
        await state.flow(state.context)
        await state.flow.settle()
    assert state.context.wake.snapshot().version == 0
    state.repository.start_action.assert_not_called()
    state.runtime.capture.fix_baseline.assert_not_called()
    state.context.wake.mark_changed(WakeReason.NEW_WORK)
    state.now = action.scheduled_at
    await state.flow(state.context)
    await state.flow.settle()
    assert action.status == 2
    state.repository.start_action.assert_called_once()


async def test_capture_dispatch_waiting_does_not_self_wake_for_present_candidates(mocker):
    state = _CaptureState(mocker, (_Action(1, 0, 1000, status=2),))
    state.context.wake = WorkNotifier()
    mocker.patch("camctl.bootstrap.flows.dispatch_ready", autospec=True,
                 return_value=[(1, HandlerOutcome("waiting", wakeup_required=False))])
    for _ in range(6):
        await state.flow(state.context)
        await state.flow.settle()
    assert state.context.wake.snapshot().version == 0


async def test_busy_device_observes_and_expires_pending_action_before_original_work_ends(mocker):
    first = _Action(1, 0, 1, status=2)
    later = _Action(2, 1000, 1)
    state = _CaptureState(mocker, (first, later))
    entered, release = asyncio.Event(), asyncio.Event()

    async def long_result_read(runtime, descriptors):
        assert [item.action_id for item in descriptors] == [1]
        entered.set()
        await release.wait()
        first.status = 3
        return []

    dispatch = mocker.patch("camctl.bootstrap.flows.dispatch_ready", autospec=True,
                           side_effect=long_result_read)
    try:
        await state.flow(state.context)
        await asyncio.wait_for(entered.wait(), timeout=1)
        state.now = 1500
        await state.flow(state.context)
        assert later.first_observed_at == 1500
        assert later.status == 1
        assert state.flow.owns_device("cam-1")
        state.repository.start_action.assert_not_called()

        state.now = 2500
        await state.flow(state.context)
        assert later.status == 5
        assert later.expiration_reason == 2  # window_exhausted
        assert state.flow.owns_device("cam-1")
        state.repository.start_action.assert_not_called()

        release.set()
        await state.flow.settle()
        await state.flow(state.context)
        assert later.first_observed_at == 1500
        dispatch.assert_awaited_once()
    finally:
        release.set()
        await state.flow.settle()


async def test_later_discovery_page_on_same_device_uses_current_candidates(mocker):
    first, second = _Action(1, 0, 1000), _Action(2, 0, 1000)
    state = _CaptureState(mocker, (first, second))
    state.discovery.captures.side_effect = [
        Page(((1, "cam-1", 0, 1, 1),), None),
        Page(((2, "cam-1", 0, 1, 2),), None),
    ]
    mocker.patch("camctl.capture.residual.pass_residual_gate", autospec=True, return_value=True)
    mocker.patch("camctl.bootstrap.flows.dispatch_ready", autospec=True, return_value=[])
    await state.flow(state.context)
    await state.flow.settle()
    await state.flow(state.context)
    await state.flow.settle()
    assert first.status == 2 and second.status == 2
    assert [call.args[0].action_id for call in state.repository.start_action.call_args_list] == [1, 2]


@pytest.mark.parametrize("lead", [Decimal(0), Decimal(10)])
async def test_preparation_task_returns_pending_and_start_waits_until_scheduled_time(mocker, lead):
    from camctl.capture.baseline import BaselinePreparation, PreparationPhase

    action = _Action(1, 10_000_000, 1000)
    state = _CaptureState(mocker, (action,))
    state.runtime.capture_preparation_supported = True
    state.flow._preparation_leads = {"cam-1": lead}

    async def discover(database, now, leads):
        assert database is state.database
        return Page(tuple(
            (item.identity, "cam-1", item.scheduled_at, 1, item.identity)
            for item in state.actions.values()
            if item.scheduled_at <= now + int(lead * 1_000_000)), None)
    state.discovery.captures.side_effect = discover
    mocker.patch("camctl.outputs.read_discovery.next_preparation_at", autospec=True, return_value=None)
    prepare = mocker.patch("camctl.capture.preparation.advance_preparation", autospec=True,
        return_value=BaselinePreparation(PreparationPhase.READY))
    mocker.patch("camctl.capture.residual.pass_residual_gate", autospec=True, return_value=True)
    mocker.patch("camctl.bootstrap.flows.dispatch_ready", autospec=True, return_value=[])
    await state.flow(state.context)
    await state.flow.settle()
    assert action.status == 1 and action.execution_started == 0
    assert state.flow.required_settlements() == 0
    assert prepare.await_count == (1 if lead else 0)
    state.repository.start_action.assert_not_called()
    state.now = action.scheduled_at
    await state.flow(state.context)
    await state.flow.settle()
    assert action.status == 2 and action.execution_started == 1
    state.repository.start_action.assert_called_once()


async def test_due_preparation_wait_still_advances_original_residual_gate(mocker):
    from camctl.capture.baseline import BaselinePreparation, PreparationPhase

    state = _CaptureState(mocker, (_Action(1, 0, 1000),))
    state.runtime.capture_preparation_supported = True
    prepare = mocker.patch("camctl.capture.preparation.advance_preparation", autospec=True,
        side_effect=[BaselinePreparation(PreparationPhase.WAITING), BaselinePreparation(PreparationPhase.READY)])
    gate = mocker.patch("camctl.capture.residual.pass_residual_gate", autospec=True, return_value=True)
    mocker.patch("camctl.bootstrap.flows.dispatch_ready", autospec=True, return_value=[])
    await state.flow(state.context)
    await state.flow.settle()
    gate.assert_awaited_once()
    assert prepare.await_count == 2
    state.repository.start_action.assert_called_once()


async def test_due_binding_failure_reaches_original_local_failure_path_without_preparing(mocker):
    from camctl.devices.bindings import BindingResult, BindingStatus, DeviceBinding

    state = _CaptureState(mocker, (_Action(1, 0, 1000),))
    state.runtime.capture_preparation_supported = True
    state.runtime.action.return_value = {"id": 1, "device_id": "cam-1", "driver_id": "driver-1", "status": 1}
    state.runtime.binding_check = lambda saved: BindingResult(BindingStatus.DRIVER_MISMATCH,
        DeviceBinding("cam-1", "driver-1"), "driver-2")
    prepare = mocker.patch("camctl.capture.preparation.advance_preparation", autospec=True)
    mocker.patch("camctl.capture.residual.pass_residual_gate", autospec=True, return_value=True)
    mocker.patch("camctl.bootstrap.flows.dispatch_ready", autospec=True, return_value=[])
    await state.flow(state.context)
    await state.flow.settle()
    prepare.assert_not_called()
    state.repository.start_action.assert_called_once()


async def test_actual_start_rechecks_current_time_after_device_gate_wait(mocker):
    action = _Action(1, 1000, 1)
    state = _CaptureState(mocker, (action,))
    state.now = 1500
    entered, release = asyncio.Event(), asyncio.Event()

    async def gate(runtime, trigger, *, on_progress=None):
        entered.set()
        await release.wait()
        return True

    mocker.patch("camctl.capture.residual.pass_residual_gate", autospec=True, side_effect=gate)
    dispatch = mocker.patch("camctl.bootstrap.flows.dispatch_ready", autospec=True, return_value=[])
    try:
        await state.flow(state.context)
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert action.first_observed_at == 1500
        state.now = 2500
        release.set()
        await state.flow.settle()
        assert state.repository.start_action.call_args.args[0].trusted_wall_now == 2500
        assert action.status == 1
        assert list(dispatch.call_args.args[1]) == []
        await state.flow(state.context)
        await state.flow.settle()
        assert action.status == 5 and action.expiration_reason == 2
    finally:
        release.set()
        await state.flow.settle()

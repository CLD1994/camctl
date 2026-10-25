"""设备长任务不占有后续动作的窗口观察责任。"""

import asyncio
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest

from camctl.bootstrap.flows import CaptureFlow
from camctl.capture.handlers import CaptureRuntime
from camctl.contracts.clock import ClockPort
from camctl.contracts.pages import Page
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.scheduling import (
    ExpireActionResult, ExpireOutcome, ObserveOutcome, ObserveWindowResult,
    SchedulingRepository, StartActionResult, StartOutcome,
)
from camctl.persistence.runtime import OwnedConnection
from camctl.outputs.read_discovery import ReadDiscoveryOwner

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


class _CaptureState:
    """通过真实查询和仓储端口交付受控的内存状态。"""

    def __init__(self, mocker, actions):
        self.actions = {action.identity: action for action in actions}
        self.now = 0
        self.clock = create_autospec(ClockPort, instance=True, spec_set=True)
        self.clock.utc_micros.side_effect = lambda: self.now
        self.connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
        self.connection.execute.side_effect = self._execute
        self.owned = OwnedConnection(self.connection, None)
        self.context = SimpleNamespace(
            open_connection=Mock(return_value=self.owned), clock=self.clock, wake=None)
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
        self.factory = Mock(return_value=self.runtime)
        self.flow = CaptureFlow(self.factory)
        self.flow._discovery = self.discovery

    def _discover(self, connection, now):
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
            rows = [(action.identity, "cam-1", action.scheduled_at, action.max_delay_ms)
                    for action in selected if action.status == 1]
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
    state.discovery.captures.side_effect = lambda connection, now, leads: Page(tuple(
        (item.identity, "cam-1", item.scheduled_at, 1, item.identity)
        for item in state.actions.values() if item.scheduled_at <= now + int(lead * 1_000_000)), None)
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

    async def gate(runtime, trigger):
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

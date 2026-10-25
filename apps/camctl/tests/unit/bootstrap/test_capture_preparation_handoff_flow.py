"""组合证明准备结果保存、流程恢复、启动派发与同设备收场。"""

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest

from camctl.bootstrap.flows import CaptureFlow
from camctl.capture.baseline import BaselinePreparation, PreparationPhase
from camctl.capture.handlers import CaptureRuntime
from camctl.capture.preparation import RuntimePreparationCache, resume_preparation_saves
from camctl.contracts.clock import ClockPort
from camctl.contracts.pages import Page
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import CapturePreparationDriver, ControlDriver, DeviceCallResult
from camctl.operations.attempts import (
    AttemptConfig, BeginAttemptResult, BeginDisposition, FinishAttemptResult,
    FinishDisposition,
)
from camctl.operations.models import (
    AttemptStatus, AttemptTicket, CallOutcome, EffectState, EvidenceValue,
    Settlement, SettlementBasis,
)
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import (
    ActivityReleaseResult, CaptureRepository, ReleaseOutcome,
)
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.scheduling import (
    ExpireActionResult, ExpireOutcome, ObserveOutcome, ObserveWindowResult,
    ReservePreparationOutcome, ReservePreparationResult, SchedulingRepository,
    StartActionResult, StartOutcome,
)
from camctl.persistence.runtime import DatabaseMetadata
from camctl.scheduling.rules import LaunchWindow


pytestmark = pytest.mark.asyncio


class _Cursor:
    def __init__(self, rows=(), description=()):
        self._rows = tuple(rows)
        self.description = tuple(description)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        return None


class _Connection:
    def __init__(self, world):
        self.world = world
        self.in_transaction = False
        self.total_changes = 0

    def execute(self, sql, parameters=()):
        world = self.world
        if sql.startswith("SELECT * FROM actions WHERE id = ?"):
            row = world.actions.get(parameters[0])
            return self._row(row, "actions")
        if sql.startswith("SELECT * FROM device_activities WHERE id = ?"):
            row = world.activities.get(parameters[0])
            return self._row(row, "device_activities")
        if sql.startswith("SELECT id FROM device_activities WHERE action_id=?"):
            activity = world.activity_for(parameters[0])
            return _Cursor(() if activity is None else ((activity["id"],),))
        if sql.startswith("SELECT r.id,r.responsibility_key,r.activity_id,t.attempt_no,t.status"):
            rows = tuple(
                (ticket.run_id, ticket.responsibility_key, int(ticket.target_id),
                 ticket.attempt_id, 2)
                for action_id, ticket in world.active_preparations.items()
                if action_id == parameters[0]
            )
            return _Cursor(rows)
        if sql.startswith("SELECT a.status,a.effect_state,e.occurred_at FROM operation_attempts"):
            return _Cursor(())
        if sql.startswith("SELECT id, device_id, scheduled_at, max_delay_ms, first_window_observed_at FROM actions"):
            now = parameters[0]
            if "device_id=?" in sql:
                device_id, identities = parameters[1], parameters[2:-1]
            else:
                device_id, identities = None, parameters[1:-1]
            rows = tuple(
                (action["id"], action["device_id"], action["scheduled_at"], action["max_delay_ms"],
                 action["first_window_observed_at"])
                for action in world.actions.values()
                if action["id"] in identities
                and (device_id is None or action["device_id"] == device_id)
                and action["status"] == 1 and not action["cancel_requested"]
                and action["type"] in (1, 2, 3) and action["scheduled_at"] <= now
            )
            return _Cursor(rows)
        if sql.startswith("SELECT id, type FROM actions WHERE status = 2"):
            now = parameters[0]
            if "device_id=?" in sql:
                device_id, identities = parameters[1], parameters[2:-1]
            else:
                device_id, identities = None, parameters[1:-1]
            rows = tuple(
                (action["id"], action["type"])
                for action in world.actions.values()
                if action["id"] in identities
                and (device_id is None or action["device_id"] == device_id)
                and action["status"] == 2 and action["type"] in (1, 2, 3)
                and ((not action["cancel_requested"] and action["scheduled_at"] <= now)
                     or action["cancel_requested"])
            )
            return _Cursor(rows)
        raise AssertionError(f"未建模的 fake DB 查询: {sql}")

    @staticmethod
    def _row(row, table):
        if row is None:
            return _Cursor()
        columns = tuple(row)
        values = tuple(
            json.dumps(row[column]) if column in {"effective_params_json", "execution_spec_json", "output_scope_json"}
            else row[column]
            for column in columns
        )
        return _Cursor((values,), tuple((column,) for column in columns))


class _Database:
    def __init__(self, world):
        self.world = world
        self.metadata = DatabaseMetadata(
            "camctl", "a" * 32, 5, "/staging", "/ready", "/processing")
        self.owned = SimpleNamespace(connection=_Connection(world), metadata=self.metadata)
        self.result_save_entered = asyncio.Event()
        self.release_result_save = asyncio.Event()
        self.hold_result_save_for: int | None = None
        self.result_save_writes = 0

    async def read(self, callback, *, description="业务读取"):
        return callback(self.owned)

    async def write(self, callback, *, key, description="业务写入"):
        owner = self.world.preparation_cache.owners.get(self.hold_result_save_for)
        if owner is not None and key == owner.result_key:
            self.result_save_writes += 1
            self.result_save_entered.set()
            await self.release_result_save.wait()
        return callback(self.owned)


class _World:
    def __init__(self, mocker, scheduled_at=10_000_000):
        self.now = 0
        self.actions = {1: self._action(1, scheduled_at)}
        self.activities = {}
        self.active_preparations = {}
        self.finish_saves = []
        self.releases_after_finish = []
        self.baseline_ready = set()
        self._next_activity_id = 70
        self.preparation_cache = RuntimePreparationCache()
        self.database = _Database(self)

        self.operations = create_autospec(OperationRepository, instance=True, spec_set=True)
        self.scheduling = create_autospec(SchedulingRepository, instance=True, spec_set=True)
        self.capture = create_autospec(CaptureRepository, instance=True, spec_set=True)
        self.operations.begin_attempt.side_effect = self._begin_attempt
        self.operations.finish_attempt.side_effect = self._finish_attempt
        self.scheduling.reserve_preparation.side_effect = self._reserve_preparation
        self.scheduling.observe_window.side_effect = self._observe_window
        self.scheduling.expire_action.side_effect = self._expire_action
        self.scheduling.start_action.side_effect = self._start_action
        self.capture.release_occupancy.side_effect = self._release_occupancy

        self.runtime = create_autospec(CaptureRuntime, instance=True)
        self.runtime.database = self.database
        self.runtime.operations = self.operations
        self.runtime.scheduling = self.scheduling
        self.runtime.capture = self.capture
        self.runtime.preparation_cache = self.preparation_cache
        self.runtime.capture_preparation_supported = True
        self.runtime.session_key = "a" * 32
        self.runtime.prepare_lead_s = Decimal(10)
        self.runtime.start_config = AttemptConfig(3, Decimal("0.5"), Decimal(3))
        self.runtime.pending_baselines = {}
        self.runtime.pending_result_closes = {}
        self.runtime.pending_capture_completions = {}
        self.runtime.pending_file_observations = {}
        self.runtime.pending_start_results = {}
        self.runtime.pending_segment_writes = {}
        self.runtime.pending_read_business = {}
        self.runtime.pending_read_results = {}
        self.runtime.pending_read_ends = {}
        self.runtime.continuing_read_tickets = {}
        self.runtime.results = object()
        self.runtime.binding_check = None
        self.runtime.wall_us = Mock(side_effect=lambda: self.now)
        self.runtime.monotonic_ns = Mock(side_effect=lambda: self.now * 1000)
        self.runtime.window_of = lambda action: LaunchWindow(
            action["scheduled_at"], action["scheduled_at"] + action["max_delay_ms"] * 1000)
        self.runtime.action.side_effect = lambda action_id: self.actions[action_id]
        self.runtime.last_attempt.return_value = None
        self.runtime.prepare_baseline.side_effect = self._prepare_baseline
        self.runtime.capture_canceled.return_value = False
        self.runtime.grant.side_effect = self._grant
        self.runtime.hold_call_result = Mock()
        self.runtime.save_held_result.return_value = None
        self.runtime.driver = create_autospec(ControlDriver, instance=True, spec_set=True)
        self.runtime.preparation_driver = create_autospec(
            CapturePreparationDriver, instance=True, spec_set=True)
        self.preparation_ticket: AttemptTicket | None = None
        self.preparation_returned = False
        self.start_requests = []
        self.runtime.preparation_driver.prepare_capture.side_effect = self._prepare_capture
        self.runtime.preparation_driver.prepared_capture_matches.side_effect = self._prepared_matches
        self.runtime.driver.control.side_effect = self._control
        self.runtime.evidence = EvidenceRegistry((
            EvidenceContract("returned", 1, "control", frozenset()),
            EvidenceContract("capture_preparation_completed", 1, "control",
                             frozenset({"activity_id"}), identity_field="activity_id"),
            EvidenceContract("start_confirmed", 1, "control", frozenset()),
        ))
        mocker.patch("camctl.bootstrap.flows.SchedulingRepository", return_value=self.scheduling)
        mocker.patch("camctl.outputs.read_discovery.next_preparation_at", autospec=True, return_value=None)
        mocker.patch("camctl.capture.residual.pass_residual_gate", autospec=True, return_value=True)

        async def factory(database, device_id):
            assert database is self.database
            assert device_id == "camera-1"
            return self.runtime

        factory.preparation_leads = {"camera-1": Decimal(10)}
        self.flow = CaptureFlow(factory)
        self.flow._discovery = _Discovery(self)
        self.clock = create_autospec(ClockPort, instance=True, spec_set=True)
        self.clock.utc_micros.side_effect = lambda: self.now
        self.context = SimpleNamespace(database=self.database, clock=self.clock, wake=None)

    @staticmethod
    def _action(action_id, scheduled_at):
        return {
            "id": action_id,
            "type": 2,
            "device_id": "camera-1",
            "driver_id": "fake-camera",
            "scheduled_at": scheduled_at,
            "max_delay_ms": 1000,
            "status": 1,
            "cancel_requested": 0,
            "first_window_observed_at": None,
            "execution_started": 0,
            "effective_params_json": {"duration_s": 1},
            "execution_spec_json": {"target_duration_ms": 1000},
        }

    def activity_for(self, action_id):
        return next((activity for activity in self.activities.values()
                     if activity["action_id"] == action_id), None)

    def _begin_attempt(self, intent, key, owned):
        activity = self.activity_for(intent.action_id)
        assert activity is not None and activity["occupancy_state"] == 1
        ticket = AttemptTicket(
            1, "control", str(activity["id"]),
            f"prepare/{activity['id']}/{self.runtime.session_key}", 100 + intent.action_id)
        self.active_preparations[intent.action_id] = ticket
        return DbOutcome(DbOutcomeKind.COMPLETED,
                         BeginAttemptResult(BeginDisposition.GRANTED, ticket))

    def _finish_attempt(self, finish, key, owned):
        self.finish_saves.append(finish)
        action_id = next(action_id for action_id, ticket in self.active_preparations.items()
                         if ticket.run_id == finish.ticket.run_id)
        assert self.preparation_cache.owners[action_id].result_key == key
        return DbOutcome(DbOutcomeKind.COMPLETED,
                         FinishAttemptResult(FinishDisposition.SAVED))

    def _reserve_preparation(self, request, key, owned):
        assert not any(activity["device_id"] == self.actions[request.action_id]["device_id"]
                       and activity["occupancy_state"] == 1
                       for activity in self.activities.values())
        self._next_activity_id += 1
        activity = {
            "id": self._next_activity_id,
            "action_id": request.action_id,
            "device_id": self.actions[request.action_id]["device_id"],
            "ownership_mode": 1,
            "occupancy_state": 1,
            "dispatch_state": 1,
            "output_scope_json": {"directories": ["/camera"]},
        }
        self.activities[activity["id"]] = activity
        return DbOutcome(DbOutcomeKind.COMPLETED,
                         ReservePreparationResult(ReservePreparationOutcome.RESERVED, activity["id"]))

    def _observe_window(self, request, key, owned):
        action = self.actions[request.action_id]
        if action["first_window_observed_at"] is None:
            action["first_window_observed_at"] = request.trusted_wall_now
        return DbOutcome(DbOutcomeKind.COMPLETED, ObserveWindowResult(
            ObserveOutcome.OBSERVED, action["first_window_observed_at"]))

    def _expire_action(self, request, key, owned):
        action = self.actions[request.action_id]
        if action["status"] == 1:
            action["status"] = 5
            action["expiration_reason"] = (
                2 if action["first_window_observed_at"] is not None else 1)
        return DbOutcome(DbOutcomeKind.COMPLETED, ExpireActionResult(
            ExpireOutcome.EXPIRED, action["expiration_reason"]))

    def _start_action(self, request, key, owned):
        action = self.actions[request.action_id]
        if request.trusted_wall_now <= action["scheduled_at"] + action["max_delay_ms"] * 1000:
            action["status"] = 2
            action["execution_started"] = 1
            activity = self.activity_for(request.action_id)
            return DbOutcome(DbOutcomeKind.COMPLETED,
                             StartActionResult(StartOutcome.STARTED, activity["id"]))
        return DbOutcome(DbOutcomeKind.COMPLETED,
                         StartActionResult(StartOutcome.REJECTED, reason="window_ended"))

    def _release_occupancy(self, request, key, owned):
        assert self.preparation_returned
        assert self.finish_saves
        assert self.preparation_cache.owners[request.action_id].saved
        self.releases_after_finish.append(len(self.finish_saves))
        activity = self.activity_for(request.action_id)
        if activity is not None:
            activity["occupancy_state"] = 0
        return DbOutcome(DbOutcomeKind.COMPLETED,
                         ActivityReleaseResult(ReleaseOutcome.RELEASED))

    async def _prepare_baseline(self, action_id):
        activity = self.activity_for(action_id)
        assert activity is not None and activity["occupancy_state"] == 1
        self.baseline_ready.add(action_id)
        return BaselinePreparation(PreparationPhase.READY)

    def _grant(self, action):
        activity = self.activity_for(action["id"])
        assert activity is not None and activity["occupancy_state"] == 1
        return AttemptTicket(1, "control", str(activity["id"]), f"start/{action['id']}",
                             200 + action["id"]), None

    async def _prepare_capture(self, request):
        self.preparation_ticket = request.ticket
        self.preparation_returned = True
        outcome = CallOutcome(
            status=AttemptStatus.SUCCEEDED,
            effect=EffectState.CONFIRMED,
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("returned", 1)),
            observations=(DeviceObservation(
                "capture_preparation_completed", 1,
                {"activity_id": request.ticket.target_id}),),
        )
        return DeviceCallResult.from_outcome(outcome)

    def _prepared_matches(self, request):
        return (self.preparation_ticket is not None
                and request.prepared_by == self.preparation_ticket
                and request.ticket == self.preparation_ticket
                and request.operation == "start_recording")

    async def _control(self, request):
        assert request.operation == "start_recording"
        assert request.dispatch_check is not None
        assert await request.dispatch_check() is None
        self.start_requests.append(request)
        outcome = CallOutcome(
            status=AttemptStatus.SUCCEEDED,
            effect=EffectState.CONFIRMED,
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("start_confirmed", 1)),
            observations=(DeviceObservation("start_confirmed", 1, {}),),
        )
        return DeviceCallResult.from_outcome(outcome)


class _Discovery:
    batch_size = 100
    pending = False

    def __init__(self, world):
        self.world = world

    async def begin_round(self, database, now):
        assert database is self.world.database

    async def captures(self, database, now, preparation_leads=None):
        assert database is self.world.database
        leads = preparation_leads or {}
        items = tuple(
            (action["id"], action["device_id"], action["scheduled_at"], 1, action["id"])
            for action in self.world.actions.values()
            if action["status"] in (1, 2)
            and action["scheduled_at"] - int(leads.get(action["device_id"], 0) * 1_000_000) <= now
        )
        return Page(items, None)


async def _resume(world):
    return await resume_preparation_saves(
        world.database, world.preparation_cache, operations=world.operations,
        scheduling=world.scheduling, capture=world.capture)


async def _wait_for_result_save(world):
    try:
        await asyncio.wait_for(world.database.result_save_entered.wait(), timeout=1)
    except asyncio.TimeoutError:
        await world.flow.settle()
        raise


async def test_flow_waits_for_original_preparation_result_save_then_dispatches_start(mocker):
    world = _World(mocker)
    world.database.hold_result_save_for = 1

    await world.flow(world.context)
    await _wait_for_result_save(world)
    owner = world.preparation_cache.owners[1]
    assert world.preparation_returned and owner.actual is not None and owner.finish is not None

    recovery = asyncio.create_task(_resume(world))
    for _ in range(6):
        await asyncio.sleep(0)
    assert not recovery.done()
    assert not world.finish_saves

    world.database.release_result_save.set()
    await recovery
    await world.flow.settle()

    assert world.finish_saves == [owner.finish]
    assert world.database.result_save_writes == 1
    assert owner.saved and owner.successful
    assert world.baseline_ready == {1}
    assert world.actions[1]["status"] == 1
    assert world.actions[1]["execution_started"] == 0
    assert world.scheduling.start_action.call_count == 0
    assert not world.start_requests

    world.now = world.actions[1]["scheduled_at"]
    await world.flow(world.context)
    await world.flow.settle()

    assert world.actions[1]["status"] == 2
    assert world.actions[1]["execution_started"] == 1
    assert world.scheduling.start_action.call_count == 1
    assert len(world.start_requests) == 1
    assert world.start_requests[0].prepared_by == world.preparation_ticket


async def test_expired_preparation_releases_device_only_after_call_and_save_then_next_action_qualifies(mocker):
    world = _World(mocker)
    world.actions[2] = world._action(2, 20_000_000)
    world.database.hold_result_save_for = 1

    await world.flow(world.context)
    await _wait_for_result_save(world)
    assert world.preparation_returned and world.activities[71]["occupancy_state"] == 1

    world.now = world.actions[1]["scheduled_at"] + world.actions[1]["max_delay_ms"] * 1000 + 1
    await world.flow(world.context)
    assert world.actions[1]["status"] == 5
    assert world.activities[71]["occupancy_state"] == 1
    assert not world.releases_after_finish

    recovery = asyncio.create_task(_resume(world))
    for _ in range(6):
        await asyncio.sleep(0)
    assert not recovery.done()
    world.database.release_result_save.set()
    await recovery
    await world.flow.settle()

    assert world.finish_saves and world.database.result_save_writes == 1
    assert world.releases_after_finish == [1]
    assert world.activities[71]["occupancy_state"] == 0
    assert world.actions[1]["execution_started"] == 0
    assert not world.start_requests

    await world.flow(world.context)
    await world.flow.settle()

    next_activity = world.activity_for(2)
    assert next_activity is not None and next_activity["occupancy_state"] == 1
    assert world.scheduling.reserve_preparation.call_args.args[0].action_id == 2
    assert world.actions[2]["status"] == 1
    assert world.baseline_ready == {2}

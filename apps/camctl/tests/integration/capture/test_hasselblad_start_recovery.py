"""真实相对调焦后原 START 的恢复保留未知与原结果，不重放。"""
import asyncio
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from camctl.capture.handlers import capture_handler
from camctl.devices.drivers.hasselblad.driver import HasselbladDriver
from camctl.devices.drivers.registry import DriverRegistry
from camctl.host_files.models import BoundDirectories
from camctl.operations.models import AttemptTicket
from camctl.operations.attempts import AttemptConfig
from camctl.operations.process import RawToolOutcome, LocalExit
from camctl.operations.recovery import RecoveryBoundary
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.scheduling import GrantRequest, GrantOutcome, SchedulingRepository
from camctl.scheduling.rules import LaunchWindow
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.contracts.enums import enum_for
from camctl.contracts.workflow_errors import registered_error
from camctl.contracts.json_values import parse_exact_json
from ..support.baseline_cases import baseline_case, baseline_case_for
from .test_host_photo_flow import _world, _entry, _ManagedCaptureTransport

pytestmark = pytest.mark.asyncio


class FocusTransport(_ManagedCaptureTransport):
    def __init__(self):
        super().__init__()
        self.interrupt_focus = False
        self.focused = asyncio.Event()

    async def run(self, spec, stop):
        if spec.argv[3] in ("focus", "autofocus"):
            self.calls.append(spec)
            raw = RawToolOutcome(LocalExit(exit_code=0), b"", None, None, stderr=b"")
            self.outcomes.append(raw)
            if self.interrupt_focus:
                self.focused.set()
                await asyncio.Future()
            return raw
        return await super().run(spec, stop)


async def focus_world(tmp_path, *, transport=None, repository=None):
    transport = FocusTransport() if transport is None else transport
    from contextlib import nullcontext
    collaborator = (nullcontext() if repository is None else patch(
        "camctl.bootstrap.capture_assembly.CaptureRepository", return_value=repository))
    with collaborator:
        cfg, owned, driver, runtime, clock = await _world(tmp_path,
            photo_focus={"mode": "MF", "relative_move": 5},
            driver_factory=lambda cfg: HasselbladDriver(cfg.devices,
                BoundDirectories(Path(cfg.paths.staging)), transport=transport, monotonic_ns=lambda: 1))
    return transport, cfg, owned, driver, runtime, clock


async def resume(cfg, owned, driver, clock, *, boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
                 horizon=True, repository=None):
    maximum = owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0]
    case = baseline_case_for()
    await case.drain()
    database = await case.session.restart_database(owned)
    driver.begin_session()
    return await case.runtime(database, directory=None, driver=driver,
        devices=cfg.devices, drivers=DriverRegistry((_entry(driver),)),
        evidence=_entry(driver).evidence, results=None, staging=Path(cfg.paths.staging),
        wait_config=lambda _: None, wall_us=lambda: clock[0], monotonic_ns=lambda: 1,
        recovery_boundary=boundary,
        recovery_max_event_id=lambda: maximum if horizon is True else horizon() if callable(horizon) else horizon,
        capture_repository=repository)


def _ticket(owned):
    run_id, attempt, activity, responsibility = owned.connection.execute(
        "SELECT r.id,t.attempt_no,r.activity_id,r.responsibility_key FROM operation_runs r"
        " JOIN operation_attempts t ON t.run_id=r.id WHERE r.responsibility_key='start/1'"
        " ORDER BY t.id DESC LIMIT 1").fetchone()
    return AttemptTicket(attempt, "control", str(activity), responsibility, run_id)


def _grant(owned, clock):
    from camctl.persistence.repositories.host_capture import HostCaptureRepository
    prepared = HostCaptureRepository().prepare_host_photo(1, clock[0], new_operation_key(), owned)
    assert prepared.kind is DbOutcomeKind.COMPLETED, prepared.error
    row = owned.connection.execute("SELECT scheduled_at,max_delay_ms FROM actions WHERE id=1").fetchone()
    receipt = SchedulingRepository().grant_start(GrantRequest("cam-1", 1,
        LaunchWindow(row[0], row[0] + row[1] * 1000), clock[0],
        AttemptConfig(3, Decimal("10")), clock[0]), new_operation_key(), owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    assert receipt.value.outcome is GrantOutcome.GRANTED
    return receipt.value.ticket


async def interrupt_after_focus(transport, runtime):
    """让真实调焦调用进入后中断，保留原 START 的未确认状态。"""
    transport.interrupt_focus = True
    task = asyncio.create_task(capture_handler("camera_take_photo")(1, runtime))
    entered = asyncio.create_task(transport.focused.wait())
    try:
        done, _ = await asyncio.wait((task, entered), return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
            pytest.fail("实际相对调焦必须先发生")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await entered
    finally:
        if not entered.done():
            entered.cancel()
        await asyncio.gather(task, entered, return_exceptions=True)
    transport.interrupt_focus = False


@pytest.mark.parametrize("boundary", [RecoveryBoundary.HOST_LOCAL_SETTLED, RecoveryBoundary.HOST_POWER_CYCLE])
@pytest.mark.parametrize("stage", ["after_focus", "saved_return", "complete_unknown"])
async def test_photo_restart_after_relative_focus_never_replays_start(tmp_path, stage, boundary):
    class Interrupt(CaptureRepository):
        def finish_start_result(self, *args, **kwargs):
            receipt = super().finish_start_result(*args, **kwargs)
            if stage == "saved_return":
                assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
                return replace(receipt, handoff_error=OSError("原结果已保存，归档前中断"))
            return receipt
        def finish_capture(self, request, key, owned):
            receipt = super().finish_capture(request, key, owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError("完成提交回执未知"))
    transport, cfg, owned, driver, runtime, clock = await focus_world(tmp_path, repository=Interrupt())
    task = None
    try:
        if stage == "after_focus":
            transport.interrupt_focus = True
            task = asyncio.create_task(capture_handler("camera_take_photo")(1, runtime))
            entered = asyncio.create_task(transport.focused.wait())
            done, _ = await asyncio.wait((task, entered), return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                await task
                pytest.fail("实际相对调焦必须先发生")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await entered
        else:
            with pytest.raises((OSError, ConsistencyError)):
                await capture_handler("camera_take_photo")(1, runtime)
        assert sum(call.argv[3] == "focus" for call in transport.calls) == 1
        original_calls = tuple(transport.calls)
        ticket = _ticket(owned)
        intent = owned.connection.execute("SELECT intent_event_id FROM operation_attempts WHERE run_id=?", (ticket.run_id,)).fetchone()[0]
        recovered = await resume(cfg, owned, driver, clock, boundary=boundary)
        await capture_handler("camera_take_photo")(1, recovered)
        await capture_handler("camera_take_photo")(1, recovered)
        assert tuple(transport.calls) == original_calls and _ticket(owned) == ticket
        status, effect, result, event = owned.connection.execute(
            "SELECT status,effect_state,result_json,intent_event_id FROM operation_attempts WHERE run_id=?",
            (ticket.run_id,)).fetchone()
        result = parse_exact_json(result)
        assert event == intent
        action = await recovered.action(1)
        if stage == "after_focus":
            assert status == int(enum_for("operation_attempts.status").UNKNOWN)
            assert effect == int(enum_for("operation_attempts.effect_state").UNKNOWN)
            assert result["settlement"] == {"basis": "assumed", "evidence": {
                "type": "local_control_recovery", "version": 1, "data": {}}}
            assert result["observations"] == [] and "diagnostics" not in result and "call_info" not in result
            assert action["status"] == 4
            assert action["error_code"] == registered_error("capture_result_unconfirmed")["action_error_id"]
            assert owned.connection.execute("SELECT started_at,activity_state,occupancy_state FROM device_activities").fetchone() == (None, 1, 1)
        else:
            assert [item["type"] for item in result["observations"]] == ["photo_taken", "hasselblad_start_entered"]
            assert action["status"] == 3
            assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (2,)
    finally:
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        owned.connection.close()


@pytest.mark.parametrize("interruption", ["granted", "after_focus"])
@pytest.mark.parametrize("condition,reason", [("boundary", "UNCONFIRMED_BOUNDARY"),
    ("horizon", "MISSING_HORIZON"), ("outside", "INTENT_OUTSIDE_HORIZON")])
async def test_missing_control_result_requires_original_reliable_recovery_boundary(tmp_path, condition, reason, interruption):
    transport, cfg, owned, driver, runtime, clock = await focus_world(tmp_path)
    try:
        if interruption == "after_focus":
            await interrupt_after_focus(transport, runtime)
            ticket = _ticket(owned)
        else:
            ticket = _grant(owned, clock)
        original_calls = tuple(transport.calls)
        recovery = await resume(cfg, owned, driver, clock,
            boundary=RecoveryBoundary.UNCONFIRMED if condition == "boundary" else RecoveryBoundary.HOST_LOCAL_SETTLED,
            horizon=None if condition == "horizon" else (0 if condition == "outside" else True))
        original = tuple(owned.connection.iterdump())
        await capture_handler("camera_take_photo")(1, recovery)
        assert baseline_case_for().diagnostics[-1].reason.name == reason
        assert tuple(owned.connection.iterdump()) == original and _ticket(owned) == ticket
        assert tuple(transport.calls) == original_calls
    finally:
        owned.connection.close()


@pytest.mark.parametrize("held_step", ["focus", "capture"])
async def test_recovery_does_not_end_control_still_executing_in_same_session(tmp_path, held_step):
    class HeldFocus(FocusTransport):
        def __init__(self):
            super().__init__()
            self.entered, self.release = asyncio.Event(), asyncio.Event()
        async def run(self, spec, stop):
            if spec.argv[3] == held_step:
                self.entered.set()
                await self.release.wait()
            return await super().run(spec, stop)
    transport = HeldFocus()
    cfg, owned, driver, _, clock = await _world(tmp_path, driver_factory=lambda cfg: HasselbladDriver(
        cfg.devices, BoundDirectories(Path(cfg.paths.staging)), transport=transport, monotonic_ns=lambda: 1),
        photo_focus={"mode": "MF", "relative_move": 5} if held_step == "focus" else None)
    maximum = [None]
    runtime = await resume(cfg, owned, driver, clock, boundary=RecoveryBoundary.HOST_POWER_CYCLE,
        horizon=lambda: maximum[0])
    task = asyncio.create_task(capture_handler("camera_take_photo")(1, runtime))
    try:
        await asyncio.wait_for(transport.entered.wait(), 1)
        maximum[0] = owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0]
        await capture_handler("camera_take_photo")(1, await baseline_case_for().view())
        assert baseline_case_for().diagnostics[-1].reason.name == "ACTIVE_LOCAL_CONTROL"
        assert owned.connection.execute("SELECT status,result_json FROM operation_attempts WHERE run_id=?", (_ticket(owned).run_id,)).fetchone() == (1, None)
        transport.release.set()
        await task
    finally:
        transport.release.set()
        await asyncio.gather(task, return_exceptions=True)
        owned.connection.close()


@pytest.mark.parametrize("interruption", ["granted", "after_focus"])
async def test_unknown_control_recovery_retains_original_request_key(tmp_path, interruption):
    class Unknown(CaptureRepository):
        def __init__(self):
            self.calls = []
        def finish_start_result(self, finish, observation, key, owned, **kwargs):
            self.calls.append((finish, observation, key, kwargs))
            saved = super().finish_start_result(finish, observation, key, owned, **kwargs)
            assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError("原恢复结果提交回执未知"))
    transport, cfg, owned, driver, runtime, clock = await focus_world(tmp_path)
    try:
        if interruption == "after_focus":
            await interrupt_after_focus(transport, runtime)
            ticket = _ticket(owned)
        else:
            ticket = _grant(owned, clock)
        original_calls = tuple(transport.calls)
        repository = Unknown()
        recovery = await resume(cfg, owned, driver, clock, repository=repository)
        with pytest.raises(ConsistencyError):
            await capture_handler("camera_take_photo")(1, recovery)
        assert len(repository.calls) == 2 and repository.calls[0] == repository.calls[1]
        key = repository.calls[0][2]
        baseline_case_for().repositories.clear()
        await capture_handler("camera_take_photo")(1, await baseline_case_for().view())
        assert owned.connection.execute("SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (key,)).fetchone() == (1,)
        assert _ticket(owned) == ticket and tuple(transport.calls) == original_calls
        assert owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (1,)
    finally:
        owned.connection.close()

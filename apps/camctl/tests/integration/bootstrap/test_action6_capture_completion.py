"""Action6 正式合同、真实仓储与会话装配的有限结束及恢复组合。"""
import asyncio
import json
import shlex

import pytest

from ..support.bootstrap_driver_cases import (close_driver_world, request_driver_cancel,
    interrupt_start_grant, interrupt_stop_grant, recover_through_capture)
from camctl.capture.handlers import capture_handler
from camctl.devices.bindings import DeviceBinding
from camctl.operations.process import LocalExit, RawToolOutcome
from camctl.operations.recovery import RecoveryBoundary
from .test_osmo_shared_recovery import _world, _capture, _reopen, _assert_unknown, Channel
from camctl.devices.catalog import build_catalog
from camctl.devices import definitions_runtime
from ..support.bootstrap_driver_cases import driver_owned as owned

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("isolated_driver_registries")]
BINDING = DeviceBinding("cam-1", "dji-action6")


class Action6Channel(Channel):
    result_path = "/mnt/media_rw/emulated/DCIM/a.MP4"

    def __init__(self, stop_payload="00"):
        super().__init__()
        self.stop_payload = stop_payload

    async def run(self, spec, stop):
        result = await super().run(spec, stop)
        script = shlex.split(spec.argv[5])[2]
        if shlex.split(script)[-3:] == ["-c", "01", "00"]:
            return RawToolOutcome(LocalExit(exit_code=0),
                f"Resp message, len = 1, data:\n {self.stop_payload}\n".encode(), None, None, stderr=b"")
        return result


async def _action6_world(owned, tmp_path, monkeypatch, stop_payload="00"):
    world = await _world(owned, tmp_path, finite=True, binding=BINDING)
    world.channel = Action6Channel(stop_payload)
    world.entry.driver.transport = world.channel
    waits = []

    async def elapsed(seconds):
        waits.append(seconds)
        world.mono[0] += int(seconds * 1_000_000_000)
    monkeypatch.setattr(asyncio, "sleep", elapsed)
    return world, waits


def _calls(world, code, payload):
    return [call for call in world.channel.calls
        if shlex.split(shlex.split(call.argv[5])[2])[-3:] == ["-c", code, payload]]


def _advance_clock(world, seconds):
    world.wall[0] += seconds * 1_000_000
    world.mono[0] += seconds * 1_000_000_000


async def test_action6_natural_completion_restores_original_target_and_never_stops(owned, tmp_path, monkeypatch):
    world, waits = await _action6_world(owned, tmp_path, monkeypatch)
    runtime = await _capture(world)
    await capture_handler(world.kind)(1, runtime)
    saved = owned.connection.execute("SELECT effective_params_json,execution_spec_json FROM actions").fetchone()
    assert len(_calls(world, "01", "01")) == 1
    horizon = await _reopen(world)
    world.channel.forbidden = False
    try:
        resumed = await _capture(world, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=lambda: horizon)
        _advance_clock(world, 1829)
        await capture_handler(world.kind)(1, resumed)
        assert world.owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
        _advance_clock(world, 1)
        await capture_handler(world.kind)(1, resumed)
        assert (await resumed.action(1))["status"] == 3
        assert waits == []
        assert _calls(world, "01", "00") == []
        assert len(_calls(world, "01", "01")) == 1
        assert world.owned.connection.execute("SELECT effective_params_json,execution_spec_json FROM actions").fetchone() == saved
        assert world.owned.connection.execute("SELECT sent_at,started_at,capture_json FROM device_activities").fetchone()[1:] == (None, '{"status":"completed"}')
        assert world.owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (1,)
    finally:
        await close_driver_world(world)


@pytest.mark.parametrize("stop_payload", ["00", "e3", "df"])
async def test_action6_finite_cancel_keeps_exact_stop_result_and_original_natural_anchor(
        owned, tmp_path, monkeypatch, stop_payload):
    world, waits = await _action6_world(owned, tmp_path, monkeypatch, stop_payload)
    runtime = await _capture(world)
    await capture_handler(world.kind)(1, runtime)
    await request_driver_cancel(world.owned, 1, world.wall[0] + 1,
        build_catalog(world.cfg, definitions_runtime.current_driver_definitions()))
    await capture_handler(world.kind)(1, runtime)
    stop_before = owned.connection.execute("SELECT t.result_json,t.error_json FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id WHERE r.kind=2").fetchone()
    assert len(_calls(world, "01", "00")) == 1
    confirmed = stop_payload in ("00", "df")
    if not confirmed:
        assert (await runtime.action(1))["status"] == 2
        assert waits == []
        _advance_clock(world, 1829)
        await capture_handler(world.kind)(1, runtime)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
        _advance_clock(world, 1)
        await capture_handler(world.kind)(1, runtime)
    assert (await runtime.action(1))["status"] == 6
    spec, = owned.connection.execute("SELECT execution_spec_json FROM actions WHERE id=1").fetchone()
    assert waits == ([json.loads(spec)["file_completion_wait_ms"] / 1000] if confirmed else [])
    result, error = stop_before
    document = json.loads(result)
    assert [item["type"] for item in document["observations"]] == (["stop_confirmed"] if confirmed else [])
    assert (error is None) is confirmed
    if confirmed:
        assert document["observations"][0]["data"]["response_payload"] == stop_payload
    await capture_handler(world.kind)(1, runtime)
    assert owned.connection.execute("SELECT t.result_json,t.error_json FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id WHERE r.kind=2").fetchone() == stop_before
    assert len(_calls(world, "01", "01")) == len(_calls(world, "01", "00")) == 1
    assert owned.connection.execute("SELECT activity_state,occupancy_state,capture_json FROM device_activities").fetchone() == (3, 2, None)


@pytest.mark.parametrize("operation", ["control", "stop"])
async def test_action6_original_foreground_recovery_does_not_resend_or_invent_sent(
        owned, tmp_path, monkeypatch, operation):
    world, waits = await _action6_world(owned, tmp_path, monkeypatch, "e3")
    runtime = await _capture(world)
    if operation == 'control':
        ticket = await interrupt_start_grant(runtime, 1, world.kind, monkeypatch)
    else:
        await capture_handler(world.kind)(1, runtime)
    await request_driver_cancel(world.owned, 1, world.wall[0] + 2,
        build_catalog(world.cfg, definitions_runtime.current_driver_definitions()))
    if operation == 'stop':
        ticket = await interrupt_stop_grant(runtime, 1, world.kind, monkeypatch)
    horizon = await _reopen(world)
    try:
        resumed = await _capture(world, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=lambda: horizon)
        await recover_through_capture(world, resumed, ticket, monkeypatch)
        _assert_unknown(world, ticket)
        world.channel.forbidden = False
        await capture_handler(world.kind)(1, resumed)
        if operation == "stop":
            assert (await resumed.action(1))["status"] == 2
            _advance_clock(world, 1830)
            await capture_handler(world.kind)(1, resumed)
        await capture_handler(world.kind)(1, resumed)
        assert (await resumed.action(1))["status"] == 6
        sent, anchor = world.owned.connection.execute("SELECT sent_at,wait_completed_event_id FROM device_activities").fetchone()
        assert (sent is None) is (operation == "control")
        assert (anchor is None) is (operation == "control")
        assert len(_calls(world, "01", "01")) == int(operation == "stop")
        assert len(_calls(world, "01", "00")) == int(operation == "control")
        assert waits == []
    finally:
        await close_driver_world(world)

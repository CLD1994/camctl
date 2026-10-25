"""派发资格在单个数据库工作中读取，返回后核对实际窗口。"""

import asyncio
from copy import deepcopy
from decimal import Decimal
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.capture import preparation, segment_baseline
from camctl.capture.baseline import PendingBaseline
from camctl.capture.baseline_models import BaselineRef, BaselineState
from camctl.capture.handlers import CaptureRuntime
from camctl.contracts.enums import enum_for
from camctl.devices.bindings import DeviceBinding
from camctl.operations.attempts import AttemptConfig
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.capture_facts import StartFacts
from camctl.persistence.runtime import OwnedConnection


NOW = 10_000_000
DEADLINE = 11_000_000


@pytest.fixture
def world(mocker):
    runtime = create_autospec(CaptureRuntime, instance=True)
    runtime.database = create_autospec(BusinessDatabase, instance=True, spec_set=True)
    owned = create_autospec(OwnedConnection, instance=True)
    owned.connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    runtime.capture = create_autospec(CaptureRepository, instance=True, spec_set=True)
    runtime.start_config = AttemptConfig(3, Decimal(1), Decimal(1))
    runtime.pending_baselines = {}
    runtime.baseline_timeout_s = Decimal(1)
    runtime.baseline_directory = None
    clock = [NOW]
    runtime.wall_us = create_autospec(lambda: 0, side_effect=lambda: clock[0])
    action = {
        "id": 1, "type": int(enum_for("actions.type").CAMERA_RECORD),
        "device_id": "cam-1", "driver_id": "dji-action6",
        "scheduled_at": NOW, "max_delay_ms": 1000,
        "status": 1, "execution_started": 0, "cancel_requested": 0,
        "effective_params_json": {}, "execution_spec_json": {},
    }
    activity = {
        "id": 7, "action_id": 1, "occupancy_state": 1, "dispatch_state": 1,
        "ownership_mode": 2, "output_scope_json": {"directories": ["/DCIM"]},
    }
    state = {"action": action, "activity": activity, "attempt": None,
             "action_observed": None, "before_execute": None, "activity_observed": None}

    def row(connection, table, identity):
        assert connection is owned.connection
        value = state["action"] if table == "actions" else state["activity"]
        if table == "actions" and state["action_observed"] is not None:
            state["action_observed"].set()
        if table == "device_activities" and state["activity_observed"] is not None:
            state["activity_observed"]()
        return deepcopy(value)

    mocker.patch("camctl.capture.preparation.row_facts", autospec=True, side_effect=row)
    mocker.patch("camctl.capture.handlers.row_facts", autospec=True, side_effect=row)
    mocker.patch("camctl.persistence.transaction.row_facts", autospec=True, side_effect=row)

    def execute(sql, parameters=()):
        cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
        if "FROM device_activities WHERE action_id" in sql:
            result = None if state["activity"] is None else (7,)
        elif "operation_attempts" in sql:
            result = state["attempt"]
        else:
            raise AssertionError(f"未登记的资格查询: {sql}")
        cursor.fetchone.return_value = result
        return cursor

    owned.connection.execute.side_effect = execute

    async def read(callback, *, description="业务读取"):
        if state["before_execute"] is not None:
            await state["before_execute"]()
        # 每次数据库等待都允许其他本地业务推进；一个callback内部不让出。
        await asyncio.sleep(0)
        return callback(owned)

    runtime.database.read.side_effect = read

    async def action_read(identity):
        return await CaptureRuntime.action(runtime, identity)

    async def last_attempt(responsibility):
        return await CaptureRuntime.last_attempt(runtime, responsibility)

    runtime.action.side_effect = action_read
    runtime.last_attempt.side_effect = last_attempt
    facts = mocker.patch("camctl.persistence.repositories.capture_facts.load_start_facts",
        autospec=True, return_value=StartFacts(activity, None, 0, True, 1))
    return runtime, state, clock, facts


class _CallbackCaptured(Exception):
    pass


async def _baseline_check(mocker, runtime):
    """从真实准备入口取得交给目录调用拥有者的最后资格回调。"""
    runtime.pending_baselines[1] = PendingBaseline(
        7, DeviceBinding("cam-1", "dji-action6"), ("/DCIM",))
    runtime.baseline_directory = object()
    runtime.capture.baseline_ref.return_value = BaselineRef(
        7, BaselineState.COLLECTING, None, None, 0, 0)
    mocker.patch("camctl.capture.baseline.resume_baseline_save", autospec=True, return_value=None)
    callbacks = []

    async def capture(owner, *, directory, capture, database, wall_us, can_read, baseline=None):
        callbacks.append(can_read)
        raise _CallbackCaptured

    mocker.patch("camctl.capture.segment_baseline.advance_baseline_read",
        autospec=True, side_effect=capture)
    with pytest.raises(_CallbackCaptured):
        await segment_baseline.prepare_segment_baseline(1, 7, runtime)
    assert len(callbacks) == 1
    runtime.database.read.reset_mock()
    runtime.action.reset_mock()
    return callbacks[0]


async def _check(mocker, runtime, entry):
    if entry == "settings":
        async def check():
            return await preparation.preparation_dispatch_reason(runtime, 1)
        return check
    return await _baseline_check(mocker, runtime)


def _interruption(state, clock, kind):
    if kind == "canceled":
        state["action"]["cancel_requested"] = 1
    elif kind == "terminal":
        state["action"]["status"] = 4
    elif kind == "window_ended":
        clock[0] = DEADLINE + 1
    else:
        raise AssertionError(kind)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["settings", "baseline"])
@pytest.mark.parametrize("kind", ["canceled", "terminal", "window_ended"])
async def test_change_accepted_while_queued_prevents_new_call(mocker, world, entry, kind):
    runtime, state, clock, _ = world
    check = await _check(mocker, runtime, entry)
    queued, release = asyncio.Event(), asyncio.Event()

    async def before_execute():
        queued.set()
        await release.wait()

    state["before_execute"] = before_execute
    task = asyncio.create_task(check())
    await queued.wait()
    _interruption(state, clock, kind)
    release.set()
    result = await task
    assert result == ({"canceled": "canceled", "terminal": "terminal",
                       "window_ended": "window_ended"}[kind] if entry == "settings" else False)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["settings", "baseline"])
@pytest.mark.parametrize("kind", ["canceled", "terminal", "window_ended"])
async def test_fact_reads_never_yield_between_action_and_dispatch(mocker, world, entry, kind):
    runtime, state, clock, _ = world
    check = await _check(mocker, runtime, entry)
    observed = state["action_observed"] = asyncio.Event()
    forbidden_dispatch = []

    async def change():
        await observed.wait()
        _interruption(state, clock, kind)

    changing = asyncio.create_task(change())
    result = await check()
    allowed = result is None if entry == "settings" else result
    if allowed and (state["action"]["cancel_requested"]
            or state["action"]["status"] in (3, 4, 5, 6) or clock[0] > DEADLINE):
        forbidden_dispatch.append(kind)
    await changing
    assert forbidden_dispatch == []
    # 一份派发资格必须来自一个有限工作，不能让业务写入夹在各行之间。
    assert runtime.database.read.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["settings", "baseline"])
async def test_window_uses_time_after_all_database_facts_return(mocker, world, entry):
    runtime, state, clock, _ = world
    check = await _check(mocker, runtime, entry)
    state["activity_observed"] = lambda: clock.__setitem__(0, DEADLINE + 1)
    assert await check() == ("window_ended" if entry == "settings" else False)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["settings", "baseline"])
async def test_deadline_itself_remains_eligible(mocker, world, entry):
    runtime, state, clock, _ = world
    check = await _check(mocker, runtime, entry)
    clock[0] = DEADLINE
    assert await check() == (None if entry == "settings" else True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,want", [
    ("released", "occupancy_released"), ("missing", "occupancy_released"),
    ("photo_started", "already_dispatched"), ("active_start", "already_dispatched"),
    ("terminal_start", "already_dispatched"), ("budget_used", "already_dispatched"),
    ("no_effect_retry", None),
])
async def test_settings_preserves_original_occupancy_and_start_rules(world, kind, want):
    runtime, state, _, facts = world
    if kind == "released":
        state["activity"]["occupancy_state"] = 2
    elif kind == "missing":
        state["activity"] = None
    else:
        state["attempt"] = (2, 1, NOW)
        if kind == "photo_started":
            state["action"]["type"] = int(enum_for("actions.type").CAMERA_TAKE_PHOTO)
        elif kind == "active_start":
            facts.return_value = StartFacts(state["activity"], None, 1, False, 2)
        elif kind == "terminal_start":
            facts.return_value = StartFacts(state["activity"], {"status": 3}, 1, True, 1)
        elif kind == "budget_used":
            facts.return_value = StartFacts(state["activity"], {"status": 2}, 3, True, 1)
        else:
            facts.return_value = StartFacts(state["activity"], {"status": 2}, 1, True, 1)
    assert await preparation.preparation_dispatch_reason(runtime, 1) == want


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("action_id", 2), ("occupancy_state", 2), ("dispatch_state", 2),
])
async def test_baseline_keeps_original_activity_qualification(mocker, world, field, value):
    runtime, state, _, _ = world
    check = await _check(mocker, runtime, "baseline")
    state["activity"][field] = value
    assert await check() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["settings", "baseline"])
async def test_missing_preparation_action_keeps_lookup_error(mocker, world, entry):
    runtime, state, _, _ = world
    check = await _check(mocker, runtime, entry)
    state["action"] = None
    with pytest.raises(LookupError):
        await check()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["settings", "baseline"])
async def test_database_read_failure_is_not_dispatch_permission(mocker, world, entry):
    runtime, _, _, _ = world
    check = await _check(mocker, runtime, entry)
    failure = sqlite3.DatabaseError("读取不可靠")
    runtime.database.read.side_effect = failure
    with pytest.raises(sqlite3.DatabaseError) as raised:
        await check()
    assert raised.value is failure


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["settings", "baseline"])
async def test_preparation_can_run_before_scheduled_start(mocker, world, entry):
    runtime, _, clock, _ = world
    check = await _check(mocker, runtime, entry)
    clock[0] = NOW - 1
    assert await check() == (None if entry == "settings" else True)


@pytest.mark.asyncio
async def test_running_segment_baseline_is_still_eligible(mocker, world):
    runtime, state, _, _ = world
    check = await _check(mocker, runtime, "baseline")
    state["action"].update(status=2, execution_started=1)
    assert await check() is True

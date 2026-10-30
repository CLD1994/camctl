"""取消生效后迟到启动结果的有限停止与重入。"""

from decimal import Decimal
import asyncio
import json

import pytest

from camctl.cancellation.models import ApplyCancelTarget, CancelApplyMode
from camctl.capture.handlers import capture_handler
from camctl.contracts.values import new_operation_key
from camctl.devices.ports import DeviceCallResult
from camctl.operations.models import (
    AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue,
    Settlement, SettlementBasis,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.cancellation import CancellationRepository, register_cancellation_guards

from .test_capture_contract import _NOW

register_cancellation_guards()
pytestmark = pytest.mark.asyncio


def _cancel(owned, action_id, *, origin_id=50, mode=CancelApplyMode.WITH_STOP, occurred_at=_NOW):
    from camctl.persistence.transaction import row_facts

    origin = row_facts(owned.connection, "actions", action_id)
    origin.update(id=origin_id, name=f"cancel-{origin_id}", input_index=origin_id, type=6, device_id=None,
                  driver_id=None, effective_params_json=None, max_delay_ms=None,
                  execution_spec_json="{}", input_fields_json="{}", target_selection_state=2)
    owned.connection.execute(
        f"INSERT INTO actions ({','.join(origin)}) VALUES ({','.join('?' for _ in origin)})",
        tuple(origin.values()))
    owned.connection.execute(
        "INSERT INTO cancel_items (id, action_id, target_action_id, selection_basis,"
        " status, cancellation_effect) VALUES (?, ?, ?, 1, 1, 1)", (origin_id + 41, origin_id, action_id))
    result = CancellationRepository().apply_cancel_target(
        ApplyCancelTarget(origin_id + 41, mode, occurred_at), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error


@pytest.mark.parametrize('entry', ['camera_record', 'camera_timelapse', 'winddown'])
@pytest.mark.parametrize('late_error', [False, True])
@pytest.mark.parametrize('stop_success', [False, True, 'confirmed_error'])
async def test_cancel_late_unknown_reuses_stop_and_finishes(
        tmp_path, capture_resources, entry, late_error, stop_success):
    import asyncio
    import json
    from camctl.bootstrap.flows import winddown_flow
    from types import SimpleNamespace
    from ..support.capture_cancellation_cases import cancellation_world

    world = await cancellation_world(tmp_path, capture_resources, finite=False,
        handler='camera_timelapse' if entry == 'camera_timelapse' else 'camera_record',
        start=False, canceled=False, stop_attempts=1,
        stop_timeout_s=Decimal('10'), stop_retry_s=Decimal('1'))
    entered, release = asyncio.Event(), asyncio.Event()
    async def control(request):
        entered.set()
        await release.wait()
        return DeviceCallResult.from_outcome(CallOutcome(
            status=AttemptStatus.FAILED if late_error else AttemptStatus.UNKNOWN,
            error=ErrorValue('transport_timeout', 'transport'), effect=EffectState.UNKNOWN,
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue('operation_returned', 1, {})),
            observations=()))
    async def stop(request):
        from camctl.devices.evidence import DeviceObservation
        if stop_success:
            return DeviceCallResult((DeviceObservation('stop_confirmed', 1,
                {'activity_id': request.ticket.target_id}),),
                {'code': 'transport_timeout', 'message': 'timeout'} if stop_success == 'confirmed_error' else None)
        return DeviceCallResult((), {'code': 'transport_timeout', 'message': 'timeout'})
    world.ports.control_script, world.ports.stop_script = control, stop
    task = asyncio.create_task(world.advance())
    async def advance():
        if entry == 'winddown':
            await winddown_flow(capture_factory=world.factory, wait_cap_s=Decimal('1'))(
                SimpleNamespace(database=world.database, clock=SimpleNamespace(utc_micros=lambda: world.wall[0])))
        else:
            await world.advance()
    try:
        await asyncio.wait_for(entered.wait(), 5)
        origins = [await world.cancel(), await world.cancel()]
        await advance()
        assert world.ports.stops == []
        assert world.owned.connection.execute('SELECT status FROM actions WHERE id=?', (world.action_id,)).fetchone() == (2,)
        release.set()
        await task
        actual = world.owned.connection.execute(
            'SELECT a.status,a.effect_state,a.result_json,a.error_json,e.occurred_at FROM operation_attempts a'
            ' JOIN operation_runs r ON r.id=a.run_id JOIN history_events e ON e.id=a.result_event_id WHERE r.kind=1').fetchone()
        assert actual[:2] == (3 if late_error else 4, 1)
        assert json.loads(actual[2])['observations'] == []
        assert json.loads(actual[3]) == {'code': 'transport_timeout', 'stage': 'transport', 'details': {}}
        assert actual[4] == world.wall[0]
        await world.reopen()
        await advance()
        if stop_success == 'confirmed_error':
            await advance()
        if not stop_success:
            await advance()
            assert len(world.ports.stops) == 1
            world.mono[0] += 2_000_000_000
            await advance()
        assert world.owned.connection.execute('SELECT status,cancel_requested,execution_started FROM actions WHERE id=?', (world.action_id,)).fetchone() == (6, 1, 1)
        assert world.owned.connection.execute('SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE responsibility_key=?', (f'start/{world.action_id}',)).fetchone() == (5, 1, 0)
        assert world.owned.connection.execute('SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE responsibility_key=?', (f'stop/{world.action_id}',)).fetchone() == (3 if stop_success else 6, 1, 0)
        assert world.owned.connection.execute('SELECT activity_state,occupancy_state,started_at,sent_at FROM device_activities WHERE action_id=?', (world.action_id,)).fetchone() == (3 if stop_success else 1, 2 if stop_success else 1, None, None)
        assert len(world.ports.controls) == len(world.ports.stops) == 1
        assert world.owned.connection.execute(
            'SELECT a.status,a.effect_state,a.result_json,a.error_json,e.occurred_at FROM operation_attempts a'
            ' JOIN operation_runs r ON r.id=a.run_id JOIN history_events e ON e.id=a.result_event_id WHERE r.kind=1').fetchone() == actual
        stopped = world.owned.connection.execute(
            'SELECT a.status,a.effect_state,a.error_json FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.kind=2').fetchone()
        assert stopped[:2] == (2 if stop_success is True else 3, 3 if stop_success else 1)
        assert (stopped[2] is None) is (stop_success is True)
        if stopped[2] is not None:
            assert json.loads(stopped[2])['code'] == 'transport_timeout'
        saved = tuple(world.owned.connection.iterdump())
        await advance()
        assert tuple(world.owned.connection.iterdump()) == saved
        await world.settle_cancel()
        assert [world.owned.connection.execute('SELECT status FROM actions WHERE id=?', (origin,)).fetchone()[0]
            for origin in origins] == [3 if stop_success else 4] * 2
        saved = tuple(world.owned.connection.iterdump())
        await world.settle_cancel()
        assert tuple(world.owned.connection.iterdump()) == saved
    finally:
        release.set()
        try:
            if not task.done():
                await task
        finally:
            await world.close()


@pytest.mark.parametrize("handler", ["camera_record", "camera_timelapse"])
async def test_independent_sent_column_cannot_replace_reliable_start_result(tmp_path, capture_resources, handler):
    from camctl.capture.models import ActivityObservationSave
    from camctl.devices.capture_completion import validate_control_confirmation
    from camctl.persistence.repositories.capture import CaptureRepository
    from ..support.capture_cancellation_cases import cancellation_world

    world = await cancellation_world(tmp_path, capture_resources, handler=handler,
        finite=False, start=False, canceled=False, stop_attempts=1)
    entered, release = asyncio.Event(), asyncio.Event()
    async def control(request):
        entered.set()
        await release.wait()
        return DeviceCallResult.from_outcome(CallOutcome(status=AttemptStatus.UNKNOWN,
            effect=EffectState.UNKNOWN, error=ErrorValue("transport_timeout", "transport"),
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("operation_returned", 1, {})), observations=()))
    world.ports.control_script = control
    task = asyncio.create_task(world.advance())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        key = new_operation_key()
        request = ActivityObservationSave(world.action_id, world.wall[0], sent_at=world.wall[0], dispatch_state=3)
        receipt = await world.database.write(lambda owned: CaptureRepository().save_activity_observation(request, key, owned), key=key)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        await world.cancel()
        release.set()
        await task
        rows = world.owned.connection.execute(
            "SELECT a.status,a.result_json,a.error_json,a.intent_event_id,a.result_event_id FROM operation_attempts a"
            " JOIN operation_runs r ON r.id=a.run_id WHERE r.kind=1").fetchall()
        assert len(rows) == 1 and rows[0][0] == 4
        assert json.loads(rows[0][1])["observations"] == []
        confirmation = {"status": rows[0][0], "effect_state": 1, "result": rows[0][1], "error": rows[0][2]}
        with pytest.raises(ValueError):
            validate_control_confirmation(confirmation,
                "timelapse_sent" if handler == "camera_timelapse" else "start_confirmed", str(world.activity_id))
        wait_receipt = None
        if handler == "camera_timelapse":
            from camctl.capture.timelapse import WaitPlan, WaitKind
            from camctl.persistence.repositories.timelapse import TimelapseRepository, ScheduleWait
            wait_key = new_operation_key()
            wait = ScheduleWait(world.action_id,
                WaitPlan(WaitKind.WAIT_THEN_CHECK, check_at_utc=world.wall[0] + 600_000_000), 0, 0, world.wall[0])
            saved = tuple(world.owned.connection.iterdump())
            wait_receipt = await world.database.write(
                lambda owned: TimelapseRepository().schedule_wait(wait, wait_key, owned), key=wait_key)
            assert wait_receipt.kind is DbOutcomeKind.ROLLED_BACK, wait_receipt.error
            assert "自然等待不匹配原可靠发送" in str(wait_receipt.error)
            assert tuple(world.owned.connection.iterdump()) == saved
        assert world.owned.connection.execute("SELECT sent_at FROM device_activities WHERE action_id=?", (world.action_id,)).fetchone() == (world.wall[0],)
        events = world.owned.connection.execute("SELECT id,event_type,occurred_at,body_json FROM history_events ORDER BY id").fetchall()
        (tmp_path / "sent-input-facts.json").write_text(json.dumps({"handler": handler,
            "observation_receipt": receipt.kind.value, "start_rows": rows, "events": events,
            "wait_rejection": None if wait_receipt is None else str(wait_receipt.error)}, ensure_ascii=False, indent=2))
    finally:
        release.set()
        try:
            if not task.done():
                await task
        finally:
            await world.close()

"""有限自然任务的真实完成时间、来源页与原预算。"""
from dataclasses import replace
from decimal import Decimal
from unittest.mock import create_autospec

import pytest

from camctl.capture.handlers import _advance_timelapse_natural_wait, capture_handler
from camctl.capture.models import WaitCompletedSave
from camctl.capture.timelapse import CaptureWaitConfig, WaitKind, WaitPlan
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.ports import DeviceCallResult, StopDriver
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue, Settlement, SettlementBasis
from camctl.history.events import load_event_registry
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.timelapse import ScheduleWait

from .result_consumer_fixtures import consumer_world
from .test_capture_contract import _NOW
from .test_cancel_timelapse_exhaustion_files import _apply_public_cancel
from .test_native_completion_context import NativeWaitCatalog, _osv
from .test_record_file_completion_wait import _result_port
from .test_result_page_history import _actual

pytestmark = pytest.mark.asyncio


class SmallNativeCatalog(NativeWaitCatalog):
    def __init__(self, startup):
        self.startup = startup

    def parameter_definition(self, device_id, action_type, parameter_type):
        definition = super().parameter_definition(device_id, action_type, parameter_type)
        if definition is not None and action_type == 'camera_timelapse':
            factory = definition.task_factory
            return replace(definition, task_factory=lambda params: replace(factory(params),
                target_duration_s=Decimal('1'), result_wait_margin_s=Decimal('.2'),
                startup_margin_ms=self.startup or None,
                cancel_completion_policy='finite_device_end'))
        return definition


async def _world(tmp_path, startup, canceled=False, *, catalog=None):
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, 'timelapse', catalog=catalog or SmallNativeCatalog(startup), start=False)
    wall, mono = [_NOW], [5000000000]
    runtime.wall_us = lambda: wall[0]
    runtime.monotonic_ns = lambda: mono[0]
    runtime.wait_config = lambda action: CaptureWaitConfig(1000, 200, 0, startup)
    runtime.check_config = AttemptConfig(3, Decimal('1'), Decimal(0))
    async def actual(request, batch):
        assert request.params['completion_context']['kind'] == 'timelapse_natural'
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()], finalized=True))
    advance = capture_handler(handler)
    await advance(action_id, runtime)
    assert owned.connection.execute('SELECT sent_at FROM device_activities WHERE action_id=?', (action_id,)).fetchone() == (_NOW,)
    result = _result_port(runtime, actual)
    stopper = create_autospec(StopDriver, instance=True)
    if canceled:
        _apply_public_cancel(owned, action_id, _NOW+100000)
        stopper.stop.return_value = DeviceCallResult.from_outcome(CallOutcome(
            status=AttemptStatus.FAILED, effect=EffectState.UNKNOWN,
            error=ErrorValue('device_error', 'device'),
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue('stop_returned', 1, {}))))
        runtime.stopper = stopper
        await advance(action_id, runtime)
    due = _NOW+(startup+1200)*1000
    return owned, runtime, action_id, advance, wall, mono, result, stopper, due


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled', [False, True])
async def test_elapsed_monotonic_time_does_not_save_completion_before_real_utc(tmp_path, startup, canceled):
    owned, runtime, action_id, advance, wall, mono, result, stopper, due = await _world(tmp_path, startup, canceled)
    try:
        mono[0] += (startup+1200)*1000000
        wall[0] = _NOW-1000000
        before = owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()
        for _ in range(3):
            await advance(action_id, runtime)
        assert owned.connection.execute('SELECT wait_completed_event_id,activity_state,occupancy_state FROM device_activities WHERE action_id=?', (action_id,)).fetchone() == (None, 1, 1)
        assert owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone() == before
        result.list_results.assert_not_awaited()
        assert stopper.stop.await_count == int(canceled)
        assert owned.connection.execute("SELECT COUNT(*) FROM operation_runs WHERE responsibility_key LIKE 'results/%'").fetchone() == (0,)
        wall[0] = due
        await advance(action_id, runtime)
        assert runtime.action(action_id)['status'] == (6 if canceled else 3)
        result.list_results.assert_awaited_once()
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled', [False, True])
async def test_completed_wait_defers_new_reads_after_wall_regression(tmp_path, startup, canceled):
    owned, runtime, action_id, advance, wall, mono, result, stopper, due = await _world(tmp_path, startup, canceled)
    try:
        mono[0] += (startup+1200)*1000000
        wall[0] = due+100
        event = _advance_timelapse_natural_wait(runtime, runtime.action(action_id))
        assert event is not None
        wall[0] = due-1000000
        for _ in range(2):
            await advance(action_id, runtime)
        result.list_results.assert_not_awaited()
        assert owned.connection.execute('SELECT wait_completed_event_id FROM device_activities WHERE action_id=?', (action_id,)).fetchone() == (event,)
        wall[0] = due+100
        await advance(action_id, runtime)
        result.list_results.assert_awaited_once()
        assert runtime.action(action_id)['status'] == (6 if canceled else 3)
        assert stopper.stop.await_count == int(canceled)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled', [False, True])
@pytest.mark.parametrize('partial', [False, True])
async def test_in_flight_regressed_page_remains_successful_but_cannot_complete_files(tmp_path, startup, canceled, partial):
    owned, runtime, action_id, advance, wall, mono, result, stopper, due = await _world(tmp_path, startup, canceled)
    from .test_result_page_history import _CURSOR
    calls = []
    async def actual(request, batch):
        calls.append(request.ticket)
        if len(calls) == 1:
            wall[0] = due-1000
        assert 'cursor' not in request.params
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()],
            next_cursor=_CURSOR if partial and len(calls) == 1 else None,
            finalized=not (partial and len(calls) == 1)))
    result.list_results.side_effect = actual
    try:
        wall[0] = due
        mono[0] += (startup+1200)*1000000
        await advance(action_id, runtime)
        assert runtime.action(action_id)['status'] == 2
        page_event = load_event_registry()['events']['RESULT_PAGE']
        first = owned.connection.execute(
            "SELECT id,occurred_at,body_json FROM history_events WHERE event_type=?"
            " AND json_extract(body_json,'$.reason')=?",
            (page_event['id'], page_event['branches']['APPEND']['reason']),
        ).fetchall()
        assert len(first) == 1 and first[0][1] == due-1000
        assert owned.connection.execute('SELECT COUNT(*) FROM device_files WHERE completion_state=3').fetchone() == (0,)
        assert owned.connection.execute('SELECT status FROM operation_attempts WHERE run_id=? AND attempt_no=1', (calls[0].run_id,)).fetchone() == (2,)
        await advance(action_id, runtime)
        assert len(calls) == 1
        wall[0] = due+1000
        await advance(action_id, runtime)
        assert len(calls) == 2 and calls[0].run_id == calls[1].run_id and calls[1].attempt_id == 2
        assert owned.connection.execute('SELECT id,occurred_at,body_json FROM history_events WHERE id=?', (first[0][0],)).fetchall() == first
        assert runtime.action(action_id)['status'] == (6 if canceled else 3)
        assert stopper.stop.await_count == int(canceled)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('branch', ['schedule', 'reconfigure', 'complete'])
async def test_each_wait_event_rejects_wrong_formula_and_early_real_completion(tmp_path, startup, branch):
    owned, runtime, action_id, advance, wall, mono, result, stopper, due = await _world(tmp_path, startup)
    try:
        # Real START/definition remain reliable; corrupt only the proposed branch input.
        if branch == 'schedule':
            # Exercise the independent formula guard before the first arrangement via a new activity state view.
            owned.connection.execute('UPDATE device_activities SET expected_check_at=NULL,result_wait_margin_ms=NULL,extra_wait_ms_used=NULL WHERE action_id=?', (action_id,))
            save = runtime.timelapse.schedule_wait
        elif branch == 'reconfigure': save = runtime.timelapse.reconfigure_wait
        else: save = runtime.timelapse.complete_wait
        before = tuple(owned.connection.iterdump())
        request = (WaitCompletedSave(action_id, due-1) if branch == 'complete' else
            ScheduleWait(action_id, WaitPlan(WaitKind.WAIT_THEN_CHECK, due+1), 200, 0, _NOW))
        receipt = save(request, new_operation_key(), runtime.owned)
        assert receipt.kind is DbOutcomeKind.ROLLED_BACK
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled', [False, True])
@pytest.mark.parametrize('failure', ['rollback', 'unknown_before', 'unknown_after'])
@pytest.mark.parametrize('initial_extra,latest_extra', [(0, 2000), (2000, 0), (0, 0)])
async def test_completion_save_resolution_keeps_original_time_across_regression_and_extra_changes(
        tmp_path, startup, canceled, failure, initial_extra, latest_extra):
    from camctl.persistence.models import DbOutcome
    owned, runtime, action_id, advance, wall, mono, result, stopper, due = await _world(tmp_path, startup, canceled)
    save = runtime.timelapse.complete_wait
    requests = []
    def fail(command, key, connection):
        requests.append((command, key))
        if failure == 'unknown_after':
            assert save(command, key, connection).kind is DbOutcomeKind.COMPLETED
        return DbOutcome(DbOutcomeKind.ROLLED_BACK if failure == 'rollback' else DbOutcomeKind.UNKNOWN)
    runtime.timelapse.complete_wait = fail
    runtime.wait_config = lambda action: CaptureWaitConfig(1000, 200, initial_extra, startup)
    try:
        wall[0] = due+initial_extra*1000+100
        mono[0] += (startup+1200+initial_extra)*1000000
        original_time = wall[0]
        with pytest.raises(ConsistencyError):
            _advance_timelapse_natural_wait(runtime, runtime.action(action_id))
        runtime.timelapse.complete_wait = save
        wall[0] = _NOW-1000
        runtime.wait_config = lambda action: CaptureWaitConfig(1000, 200, latest_extra, startup)
        await advance(action_id, runtime)
        result.list_results.assert_not_awaited()
        row = owned.connection.execute('SELECT expected_check_at,extra_wait_ms_used,wait_completed_event_id FROM device_activities WHERE action_id=?', (action_id,)).fetchone()
        if failure == 'unknown_after' or (failure == 'unknown_before' and initial_extra == latest_extra):
            assert row[:2] == (due+initial_extra*1000, initial_extra) and row[2] is not None
            assert owned.connection.execute('SELECT occurred_at FROM history_events WHERE id=?', (row[2],)).fetchone() == (original_time,)
        else:
            assert row == (due+latest_extra*1000, latest_extra, None)
        wall[0] = due+max(initial_extra, latest_extra)*1000+200
        mono[0] = 5000000000+(startup+1200+max(initial_extra, latest_extra))*1000000
        await advance(action_id, runtime)
        result.list_results.assert_awaited_once()
        assert runtime.action(action_id)['status'] == (6 if canceled else 3)
        assert stopper.stop.await_count == int(canceled)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('committed', [False, True])
async def test_qualified_original_page_unknown_is_reused_without_new_read_during_regression(tmp_path, startup, committed):
    from camctl.persistence.models import DbOutcome
    owned, runtime, action_id, advance, wall, mono, result, stopper, due = await _world(tmp_path, startup)
    save = runtime.capture.save_result_page
    calls = []
    def uncertain(command, key, connection):
        calls.append((command, key))
        if len(calls) == 1:
            if committed:
                assert save(command, key, connection).kind is DbOutcomeKind.COMPLETED
            return DbOutcome(DbOutcomeKind.UNKNOWN)
        return save(command, key, connection)
    runtime.capture.save_result_page = uncertain
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        with pytest.raises(ConsistencyError):
            await advance(action_id, runtime)
        result.list_results.assert_awaited_once()
        wall[0] = due-1000
        await advance(action_id, runtime)
        assert calls[0] == calls[1]
        result.list_results.assert_awaited_once()
        assert runtime.action(action_id)['status'] == 2
        wall[0] = due+100
        await advance(action_id, runtime)
        result.list_results.assert_awaited_once()
        assert runtime.action(action_id)['status'] == 3
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled', [False, True])
async def test_invalid_actual_page_exhausts_original_budget_without_fabricating_end(tmp_path, startup, canceled):
    from pathlib import Path
    from camctl.persistence.repositories.history import HistoryRepository
    owned, runtime, action_id, advance, wall, mono, result, stopper, due = await _world(tmp_path, startup, canceled)
    runtime.check_config = AttemptConfig(1, Decimal('1'), Decimal(0))
    async def actual(request, batch):
        wall[0] = due-1
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()], finalized=True))
    result.list_results.side_effect = actual
    try:
        wall[0] = due
        mono[0] += (startup+1200)*1000000
        await advance(action_id, runtime)
        wall[0] = due+100
        await advance(action_id, runtime)
        assert runtime.action(action_id)['status'] == (6 if canceled else 4)
        assert owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE action_id=?', (action_id,)).fetchone() == (1, 1)
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone() == (0,)
        assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE responsibility_key LIKE 'results/%'").fetchone() == (1,)
        result.list_results.assert_awaited_once()
        assert stopper.stop.await_count == int(canceled)
        history = HistoryRepository(Path(owned.connection.execute('PRAGMA database_list').fetchone()[2]))
        restored = history.restore_entity('action', action_id, history.current_boundary())
        assert restored[('actions', action_id)]['status'] == (6 if canceled else 4)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled', [False, True])
@pytest.mark.parametrize('recovered', [False, True])
async def test_unqualified_wait_returns_to_positive_host_poll_without_consuming_budget(
        tmp_path, monkeypatch, startup, canceled, recovered):
    from types import SimpleNamespace
    from camctl.session.service import _wait_for_more_work
    owned, runtime, action_id, advance, wall, mono, result, stopper, due = await _world(tmp_path, startup, canceled)
    delays = []
    async def sleep(delay):
        delays.append(delay)
    monkeypatch.setattr('camctl.session.service.asyncio.sleep', sleep)
    try:
        if recovered:
            runtime = replace(runtime, natural_wait_states={}, timelapse_deadlines={})
        mono[0] += (startup+1200)*1000000
        wall[0] = _NOW-1000000
        before = owned.connection.execute('SELECT COUNT(*) FROM operation_attempts').fetchone()
        context = SimpleNamespace(poll_interval_s=.5, wake=None, flows={}, wakeup_margin_s=0,
            clock=SimpleNamespace(utc_micros=lambda: wall[0]))
        for _ in range(3):
            await advance(action_id, runtime)
            await _wait_for_more_work(context, owned)
        assert delays == [.5, .5, .5]
        assert owned.connection.execute('SELECT COUNT(*) FROM operation_attempts').fetchone() == before
        result.list_results.assert_not_awaited()
        assert stopper.stop.await_count == int(canceled)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
async def test_saved_page_registration_waits_for_current_source_qualification(tmp_path, startup):
    from camctl.capture.handlers import _listing_round, _register_listing
    owned, runtime, action_id, advance, wall, mono, result, stopper, due = await _world(tmp_path, startup)
    try:
        wall[0] = due
        mono[0] += (startup+1200)*1000000
        _advance_timelapse_natural_wait(runtime, runtime.action(action_id))
        wall[0] = due+100
        listing = await _listing_round(runtime, action_id)
        before = tuple(owned.connection.iterdump())
        wall[0] = due+99
        with pytest.raises(Exception, match='原来源时刻'):
            _register_listing(runtime, action_id, listing)
        assert tuple(owned.connection.iterdump()) == before
        wall[0] = due+100
        await advance(action_id, runtime)
        result.list_results.assert_awaited_once()
        assert runtime.action(action_id)['status'] == 3
    finally:
        owned.connection.close()

"""有限自然任务的真实完成时间、来源页与原预算。"""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.capture.handlers import capture_handler
from camctl.bootstrap.capture_assembly import DriverResultListing
from camctl.bootstrap.flows import cancel_flow
from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import AcceptanceContext, CommandMode, accept_input
from camctl.capture.models import WaitCompletedSave
from camctl.capture.timelapse import CaptureWaitConfig, WaitKind, WaitPlan
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.ports import DeviceCallResult, ResultDriver, StopDriver
from camctl.devices.bindings import DeviceBinding
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue, Settlement, SettlementBasis
from camctl.history.events import load_event_registry
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.timelapse import ScheduleWait, TimelapseRepository
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.acceptance import AcceptanceRepository

from .result_consumer_fixtures import RESULT_EVIDENCE, consumer_world, _prepare_through_handler
from ..support.capture import capture_factory
from .test_capture_contract import _NOW
from .test_native_completion_context import NativeWaitCatalog, _osv
from .test_record_file_completion_wait import _REGISTRY
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


async def _progress_cancel(runtime, wall):
    metadata = runtime.database.metadata
    await cancel_flow(ready=Path(metadata.ready_path), processing=Path(metadata.processing_path),
        start_entries=runtime.start_entries)(SimpleNamespace(database=runtime.database,
            clock=SimpleNamespace(utc_micros=lambda: wall[0])))


async def _request_cancel(runtime, action_id, wall):
    instant = datetime.fromtimestamp(wall[0] // 1_000_000, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    accepted = await accept_input(ParsedInput('cancel-timelapse.json', {
        'request_id': '2', 'created_at': instant, 'name': '取消延时摄影',
        'actions': [{'name': '取消', 'type': 'cancel_task', 'params': {
            'target': {'action_instance_id': str(action_id)}}}]}),
        AcceptanceContext(CommandMode.RUN, SmallNativeCatalog(0), AcceptanceRepository(),
            clock=SimpleNamespace(utc_micros=lambda: wall[0])),
        new_operation_key(), runtime.database)
    assert accepted.plan_id is not None, accepted
    await _progress_cancel(runtime, wall)


async def _world(tmp_path, startup, canceled=False, *, catalog=None, check_config=None,
                 extra=None, state=None, start_only=False, stopper=None):
    state = {} if state is None else state
    wall, mono = [_NOW], [5000000000]
    extra = {'extra_wait_ms': 0} if extra is None else extra
    check_config = check_config or AttemptConfig(3, Decimal('1'), Decimal(0))
    result = create_autospec(ResultDriver, instance=True)
    if stopper is None:
        stopper = create_autospec(StopDriver, instance=True)
        stopper.stop.return_value = DeviceCallResult.from_outcome(CallOutcome(
            status=AttemptStatus.FAILED, effect=EffectState.UNKNOWN,
            error=ErrorValue('device_error', 'device'),
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue('stop_returned', 1, {}))))
    evidence = _REGISTRY.with_contract(RESULT_EVIDENCE.contract('timelapse_sent', 1))
    options = dict(evidence=evidence, results=DriverResultListing(
        result, DeviceBinding('cam-1', 'camctl-adb'), evidence), stopper=stopper,
        wall_us=lambda: wall[0], monotonic_ns=lambda: mono[0],
        wait_config=lambda action: CaptureWaitConfig(1000, 200, 0, startup),
        devices={'cam-1': {'driver': 'camctl-adb', 'capture': extra,
            'result_check': {'max_attempts': check_config.max_attempts,
                'call_timeout_s': check_config.timeout_s,
                'retry_interval_s': check_config.retry_interval_s}}})

    async def assemble(owned, *, driver):
        options['driver'] = driver
        state.update(options=options, factory=await capture_factory(owned, **options),
            wall=wall, mono=mono, extra=extra)
        return await state['factory'](owned)

    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, 'timelapse', catalog=catalog or SmallNativeCatalog(startup),
        start=False, runtime_factory=assemble)
    async def actual(request, batch):
        assert request.params['completion_context']['kind'] == 'timelapse_natural'
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()], finalized=True))
    advance = capture_handler(handler)
    result.list_results.side_effect = actual
    if start_only:
        await _prepare_through_handler(runtime, action_id, handler, 'start')
    else:
        await advance(action_id, runtime)
    assert owned.connection.execute('SELECT sent_at FROM device_activities WHERE action_id=?', (action_id,)).fetchone() == (_NOW,)
    if canceled:
        wall[0] = _NOW + 100000
        await _request_cancel(runtime, action_id, wall)
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
        assert (await runtime.action(action_id))['status'] == (6 if canceled else 3)
        result.list_results.assert_awaited_once()
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled', [False, True])
async def test_completed_wait_defers_new_reads_after_wall_regression(tmp_path, monkeypatch, startup, canceled):
    owned, runtime, action_id, advance, wall, mono, result, stopper, due = await _world(tmp_path, startup, canceled)

    class WaitSaved(Exception):
        pass

    save = TimelapseRepository.complete_wait
    def boundary(repository, request, key, current):
        receipt = save(repository, request, key, current)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        return replace(receipt, handoff_error=WaitSaved())
    try:
        mono[0] += (startup+1200)*1000000
        wall[0] = due+100
        with monkeypatch.context() as fault:
            fault.setattr(TimelapseRepository, 'complete_wait', boundary)
            with pytest.raises(WaitSaved):
                await advance(action_id, runtime)
        event, = owned.connection.execute('SELECT wait_completed_event_id FROM device_activities WHERE action_id=?', (action_id,)).fetchone()
        assert event is not None
        wall[0] = due-1000000
        for _ in range(2):
            await advance(action_id, runtime)
        result.list_results.assert_not_awaited()
        assert owned.connection.execute('SELECT wait_completed_event_id FROM device_activities WHERE action_id=?', (action_id,)).fetchone() == (event,)
        wall[0] = due+100
        await advance(action_id, runtime)
        result.list_results.assert_awaited_once()
        assert (await runtime.action(action_id))['status'] == (6 if canceled else 3)
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
        assert (await runtime.action(action_id))['status'] == 2
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
        assert (await runtime.action(action_id))['status'] == (6 if canceled else 3)
        assert stopper.stop.await_count == int(canceled)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('branch', ['schedule', 'reconfigure', 'complete'])
async def test_each_wait_event_rejects_wrong_formula_and_early_real_completion(tmp_path, startup, branch):
    owned, runtime, action_id, advance, wall, mono, result, stopper, due = await _world(
        tmp_path, startup, start_only=branch == 'schedule')
    try:
        # START 和固定定义来自实际处理器；只向存储入口提交错误公式。
        if branch == 'schedule':
            save = TimelapseRepository().schedule_wait
        elif branch == 'reconfigure': save = TimelapseRepository().reconfigure_wait
        else: save = TimelapseRepository().complete_wait
        before = tuple(owned.connection.iterdump())
        request = (WaitCompletedSave(action_id, due-1) if branch == 'complete' else
            ScheduleWait(action_id, WaitPlan(WaitKind.WAIT_THEN_CHECK, due+1), 200, 0, _NOW))
        receipt = save(request, new_operation_key(), owned)
        assert receipt.kind is DbOutcomeKind.ROLLED_BACK
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled', [False, True])
@pytest.mark.parametrize('failure', ['rollback', 'unknown_before', 'unknown_after'])
@pytest.mark.parametrize('initial_extra,latest_extra', [(0, 2000), (2000, 0), (0, 0)])
async def test_completion_save_resolution_keeps_original_time_across_regression_and_extra_changes(
        tmp_path, monkeypatch, startup, canceled, failure, initial_extra, latest_extra):
    from camctl.persistence.models import DbOutcome
    extra = {'extra_wait_ms': 0}
    owned, runtime, action_id, advance, wall, mono, result, stopper, due = await _world(
        tmp_path, startup, canceled, extra=extra)
    save = TimelapseRepository.complete_wait
    requests = []
    def fail(repository, command, key, connection):
        requests.append((command, key))
        if failure == 'unknown_after':
            assert save(repository, command, key, connection).kind is DbOutcomeKind.COMPLETED
        return DbOutcome(DbOutcomeKind.ROLLED_BACK if failure == 'rollback' else DbOutcomeKind.UNKNOWN)
    extra['extra_wait_ms'] = initial_extra
    try:
        wall[0] = due+initial_extra*1000+100
        mono[0] += (startup+1200+initial_extra)*1000000
        original_time = wall[0]
        with monkeypatch.context() as fault:
            fault.setattr(TimelapseRepository, 'complete_wait', fail)
            with pytest.raises(ConsistencyError):
                await advance(action_id, runtime)
        wall[0] = _NOW-1000
        extra['extra_wait_ms'] = latest_extra
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
        assert (await runtime.action(action_id))['status'] == (6 if canceled else 3)
        assert stopper.stop.await_count == int(canceled)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('committed', [False, True])
async def test_qualified_original_page_unknown_is_reused_without_new_read_during_regression(
        tmp_path, capture_resources, monkeypatch, startup, committed):
    from camctl.persistence.models import DbOutcome
    state = {}
    owned, runtime, action_id, advance, wall, mono, result, stopper, due = await _world(
        tmp_path, startup, state=state)
    save = CaptureRepository.save_result_page
    calls = []
    def uncertain(repository, command, key, connection):
        calls.append((command, key))
        if len(calls) == 1:
            if committed:
                assert save(repository, command, key, connection).kind is DbOutcomeKind.COMPLETED
            return DbOutcome(DbOutcomeKind.UNKNOWN)
        return save(repository, command, key, connection)
    monkeypatch.setattr(CaptureRepository, 'save_result_page', uncertain)
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        with pytest.raises(ConsistencyError):
            await advance(action_id, runtime)
        result.list_results.assert_awaited_once()
        await capture_resources.restart_database(owned)
        runtime = await state['factory'](owned)
        wall[0] = due-1000
        await advance(action_id, runtime)
        assert calls[0] == calls[1]
        result.list_results.assert_awaited_once()
        assert (await runtime.action(action_id))['status'] == 2
        wall[0] = due+100
        await advance(action_id, runtime)
        result.list_results.assert_awaited_once()
        assert (await runtime.action(action_id))['status'] == 3
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled', [False, True])
async def test_invalid_actual_page_exhausts_original_budget_without_fabricating_end(tmp_path, startup, canceled):
    from pathlib import Path
    from camctl.persistence.repositories.history import HistoryRepository
    owned, runtime, action_id, advance, wall, mono, result, stopper, due = await _world(
        tmp_path, startup, canceled, check_config=AttemptConfig(1, Decimal('1'), Decimal(0)))
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
        assert (await runtime.action(action_id))['status'] == (6 if canceled else 4)
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
        tmp_path, capture_resources, monkeypatch, startup, canceled, recovered):
    import asyncio
    from camctl.session.service import run_session
    from ..support.session import session_context
    state = {}
    owned, runtime, action_id, advance, wall, mono, result, stopper, due = await _world(
        tmp_path, startup, canceled, state=state)
    delays = []
    real_sleep = asyncio.sleep
    async def sleep(delay):
        if delay:
            delays.append(delay)
            if len(delays) == 3:
                raise asyncio.CancelledError()
        await real_sleep(0)
    monkeypatch.setattr('camctl.session.service.asyncio.sleep', sleep)
    try:
        mono[0] += (startup+1200)*1000000
        wall[0] = _NOW-1000000
        before = owned.connection.execute('SELECT COUNT(*) FROM operation_attempts').fetchone()
        await capture_resources.close()
        factory = None if recovered else state['factory']
        async def capture(context):
            nonlocal factory
            if factory is None:
                factory = await capture_factory(context.database, **state['options'])
            current = await factory(context.database)
            await advance(action_id, current)
        context = session_context(Path(tmp_path) / 'state.db',
            clock=SimpleNamespace(utc_micros=lambda: wall[0]),
            flows={'capture': capture}, poll_interval_s=.5, wakeup_margin_s=0)
        with pytest.raises(asyncio.CancelledError):
            await run_session(context, None)
        assert delays == [.5, .5, .5]
        assert owned.connection.execute('SELECT COUNT(*) FROM operation_attempts').fetchone() == before
        result.list_results.assert_not_awaited()
        assert stopper.stop.await_count == int(canceled)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
async def test_saved_page_registration_waits_for_current_source_qualification(tmp_path, monkeypatch, startup):
    from .result_consumer_fixtures import complete_natural_wait
    owned, runtime, action_id, advance, wall, mono, result, stopper, due = await _world(tmp_path, startup)

    class PageSaved(Exception):
        pass

    save = CaptureRepository.save_result_page
    def boundary(repository, request, key, current):
        receipt = save(repository, request, key, current)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        return replace(receipt, handoff_error=PageSaved())
    try:
        wall[0] = due
        mono[0] += (startup+1200)*1000000
        await complete_natural_wait(owned, action_id, wall[0])
        wall[0] = due+100
        with monkeypatch.context() as fault:
            fault.setattr(CaptureRepository, 'save_result_page', boundary)
            with pytest.raises(PageSaved):
                await advance(action_id, runtime)
        before = tuple(owned.connection.iterdump())
        wall[0] = due+99
        await advance(action_id, runtime)
        assert tuple(owned.connection.iterdump()) == before
        wall[0] = due+100
        await advance(action_id, runtime)
        result.list_results.assert_awaited_once()
        assert (await runtime.action(action_id))['status'] == 3
    finally:
        owned.connection.close()

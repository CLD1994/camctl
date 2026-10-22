"""自然等待分别核对逐页文件、当前集合和跨轮终态保留。"""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.capture.handlers import _control_call, _natural_file_completion_context
from camctl.capture.models import WaitCompletedSave
from camctl.capture.timelapse import WaitKind, WaitPlan
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.ports import DeviceCallResult
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import ErrorValue
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.history import HistoryRepository
from camctl.persistence.repositories.timelapse import ScheduleWait

from .result_consumer_fixtures import consumer_world, returned
from .test_capture_contract import _NOW
from .test_native_completion_context import _osv
from .test_finite_cancel import _finish_cancel
from .test_natural_wait_time_eligibility import SmallNativeCatalog, _world
from .test_result_page_history import _actual, _CURSOR

pytestmark = pytest.mark.asyncio


def _other():
    return {**_osv(), 'identity': '/DCIM/b.OSV', 'locator': {'path': '/DCIM/b.OSV'}}


def _pages(owned):
    from camctl.history.events import load_event_registry

    contract = load_event_registry()["events"]["RESULT_PAGE"]
    return owned.connection.execute(
        "SELECT id,occurred_at,body_json FROM history_events WHERE event_type=? AND json_extract(body_json,'$.reason')=? ORDER BY id",
        (contract["id"], contract["branches"]["APPEND"]["reason"]),
    ).fetchall()


def _retained(owned, action_id):
    return owned.connection.execute('SELECT o.id,o.device_file_id,f.completion_evidence_json FROM outputs o JOIN device_files f ON f.id=o.device_file_id WHERE o.source_action_id=? ORDER BY o.id', (action_id,)).fetchall()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled', [False, True])
@pytest.mark.parametrize('mixed', [False, True])
@pytest.mark.parametrize('budget', [1, 2])
async def test_qualified_files_survive_bad_latest_page_without_completing_collection(tmp_path, startup, canceled, mixed, budget):
    owned, runtime, aid, advance, wall, mono, result, stopper, due = await _world(tmp_path, startup, canceled)
    runtime.check_config = AttemptConfig(budget if mixed else budget+1, Decimal('1'), Decimal(0))
    calls = []
    async def actual(request, batch):
        calls.append(request.ticket)
        if len(calls) == 1:
            return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()],
                next_cursor=_CURSOR if mixed else None,
                error=None if mixed else ErrorValue('device_error', 'device')))
        wall[0] = due-1
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_other()],
            cursor=_CURSOR if mixed and len(calls) == 2 else None, finalized=True))
    result.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        wall[0] = due+100
        await advance(aid, runtime)
        original_pages = _pages(owned)
        assert len(original_pages) == 2
        assert owned.connection.execute("SELECT completion_state FROM device_files WHERE json_extract(identity_key,'$[2]')='/DCIM/a.OSV'").fetchone() == (3,)
        original_file = owned.connection.execute("SELECT id,completion_evidence_json FROM device_files WHERE json_extract(identity_key,'$[2]')='/DCIM/a.OSV'").fetchone()
        assert owned.connection.execute("SELECT completion_state FROM device_files WHERE json_extract(identity_key,'$[2]')='/DCIM/b.OSV'").fetchone() == (1,)
        assert runtime.action(aid)['status'] == 2
        # 校时恢复只允许沿剩余预算的新尝试，不能重新解释坏页。
        for _ in range(budget+2):
            wall[0] = due+100
            await advance(aid, runtime)
        retained = _retained(owned, aid)
        assert len(retained) == 1 and retained[0][1:] == original_file
        assert _pages(owned)[:2] == original_pages
        assert runtime.action(aid)['status'] == (6 if canceled else 4)
        if canceled: await _finish_cancel(owned, wall)
        assert owned.connection.execute('SELECT activity_state,occupancy_state,result_set_state FROM device_activities WHERE action_id=?', (aid,)).fetchone() == (1, 1, 4)
        assert len(calls) == (budget+1)
        assert stopper.stop.await_count == int(canceled)
        before = retained
        await advance(aid, runtime)
        assert _retained(owned, aid) == before
        history = HistoryRepository(Path(owned.connection.execute('PRAGMA database_list').fetchone()[2]))
        restored = history.restore_entity('action', aid, history.current_boundary())
        assert restored[('actions', aid)]['status'] == (6 if canceled else 4)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled', [False, True])
@pytest.mark.parametrize('latest', ['empty', 'missing'])
async def test_known_complete_file_cannot_fill_current_empty_or_missing_collection(tmp_path, startup, canceled, latest):
    owned, runtime, aid, advance, wall, mono, result, stopper, due = await _world(tmp_path, startup, canceled)
    runtime.check_config = AttemptConfig(2, Decimal('1'), Decimal(0))
    calls = []
    async def actual(request, batch):
        calls.append(request.ticket)
        return DeviceCallResult.from_outcome(_actual(request.ticket,
            entries=[_osv()] if len(calls) == 1 else ([] if latest == 'empty' else [{**_other(), 'complete': False, 'size_bytes': None}]),
            error=ErrorValue('device_error', 'device') if len(calls) == 1 else None,
            finalized=len(calls) > 1))
    result.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        await advance(aid, runtime)
        if latest == 'missing':
            assert runtime.action(aid)['status'] == 2
            await advance(aid, runtime)
        assert runtime.action(aid)['status'] == (6 if canceled else 4)
        if canceled: await _finish_cancel(owned, wall)
        assert len(_retained(owned, aid)) == 1
        activity = owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE action_id=?', (aid,)).fetchone()
        assert activity == (1, 1)
        assert len(calls) == 2
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('observation', ['timelapse_sent', 'photo_taken', 'start_confirmed', None])
async def test_wait_repository_uses_same_sent_semantics_as_completion_context(tmp_path, startup, observation):
    owned, runtime, aid, handler = await consumer_world(tmp_path, 'timelapse', catalog=SmallNativeCatalog(startup), start=False)
    activity_id = owned.connection.execute('SELECT id FROM device_activities WHERE action_id=?', (aid,)).fetchone()[0]
    actual = returned('operation', observation or 'timelapse_sent', activity_id)
    if observation is None:
        actual = replace(actual, observations=())
    runtime.driver.control.return_value = DeviceCallResult.from_outcome(actual)
    try:
        await _control_call(runtime, runtime.action(aid), 'start_timelapse', 'timelapse_sent')
        due = _NOW+(startup+1200)*1000
        commands = [
            (runtime.timelapse.schedule_wait, ScheduleWait(aid, WaitPlan(WaitKind.WAIT_THEN_CHECK, due), 200, 0, _NOW)),
            (runtime.timelapse.reconfigure_wait, ScheduleWait(aid, WaitPlan(WaitKind.WAIT_THEN_CHECK, due+1000), 200, 1, _NOW)),
            (runtime.timelapse.complete_wait, WaitCompletedSave(aid, due+1000))]
        for save, command in commands:
            before = tuple(owned.connection.iterdump())
            key = new_operation_key()
            receipt = save(command, key, owned)
            assert receipt.kind is (DbOutcomeKind.COMPLETED if observation == 'timelapse_sent' else DbOutcomeKind.ROLLED_BACK)
            if observation != 'timelapse_sent':
                assert tuple(owned.connection.iterdump()) == before
            else:
                assert save(command, key, owned).kind is DbOutcomeKind.COMPLETED
        if observation == 'timelapse_sent':
            assert _natural_file_completion_context(runtime, aid) is not None
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('branch', ['schedule', 'reconfigure', 'complete'])
@pytest.mark.parametrize('change', ['none', 'photo_taken', 'start_confirmed', 'wrong_activity', 'no_observation', 'failed', 'unknown_effect'])
async def test_wait_history_guard_checks_shared_reliable_sent_contract(tmp_path, startup, branch, change):
    import json
    from camctl.history.events import load_event_registry
    from camctl.history.validators import EventContext, EventValidationError, validate_event
    from camctl.persistence.transaction import TransactionRange, event_envelope, row_facts, update_change
    from camctl.persistence.repositories.capture import _history_source_row
    owned, runtime, aid, advance, wall, mono, result, stopper, due = await _world(tmp_path, startup)
    try:
        state = {table: {row[0]: row_facts(owned.connection, table, row[0])
            for row in owned.connection.execute(f'SELECT id FROM {table}')}
            for table in ('actions', 'device_activities', 'operation_runs', 'operation_attempts')}
        state['history_events'] = {row[0]: _history_source_row(owned.connection, row[0])
            for row in owned.connection.execute('SELECT id FROM history_events')}
        run = next(value for value in state['operation_runs'].values() if value['responsibility_key'] == f'start/{aid}')
        attempt = next(value for value in state['operation_attempts'].values() if value['run_id'] == run['id'])
        document = attempt['result_json']
        if change in ('photo_taken', 'start_confirmed'):
            document['observations'][0]['type'] = change
        elif change == 'wrong_activity': document['observations'][0]['data']['activity_id'] = '999'
        elif change == 'no_observation': document['observations'] = []
        elif change == 'failed':
            attempt['status'] = 3
            attempt['error_json'] = {'code': 'device_error', 'stage': 'device', 'details': {}}
        elif change == 'unknown_effect':
            from camctl.contracts.enums import enum_for
            attempt['effect_state'] = int(enum_for('operation_attempts.effect_state').UNKNOWN)
        # 原事件与行仍忠实一致；此处仅检查观察语义，不能靠不一致侥幸拒绝。
        source = state['history_events'][attempt['result_event_id']]
        body = json.loads(source['body_json'])
        original_row = next(row for row in body['rows'] if row['table'] == 'operation_attempts' and row['id'] == attempt['id'])
        for field in ('status', 'effect_state', 'result_json', 'error_json'):
            if field in original_row['after']['values'] or (field == 'error_json' and change == 'failed'):
                original_row['before']['values'].setdefault(field, None)
                original_row['after']['values'][field] = attempt[field]
        source['body_json'] = json.dumps(body)
        activity = state['device_activities'][aid]
        event_id = owned.connection.execute('SELECT MAX(id)+1 FROM history_events').fetchone()[0]
        transaction = owned.connection.execute('SELECT MAX(id)+1 FROM history_transactions').fetchone()[0]
        if branch == 'schedule':
            before = {'result_wait_margin_ms': None, 'extra_wait_ms_used': None, 'expected_check_at': None}
            activity.update(before)
            after = {'result_wait_margin_ms': 200, 'extra_wait_ms_used': 0, 'expected_check_at': due}
            reason, instant = 1, _NOW
        elif branch == 'reconfigure':
            before = {'extra_wait_ms_used': 0, 'expected_check_at': due}
            after = {'extra_wait_ms_used': 1, 'expected_check_at': due+1000}
            reason, instant = 2, _NOW
        else:
            before, after = {'wait_completed_event_id': None}, {'wait_completed_event_id': event_id}
            reason, instant = 3, due+100
        event = event_envelope(event_id, transaction, load_event_registry()['events']['CAPTURE_WAIT_CHANGED']['id'],
            reason, (update_change('device_activities', aid, before, after),), instant)
        context = EventContext(TransactionRange(transaction, event_id, event_id), {('device_activities', aid): ('action', aid)}, state)
        if change == 'none':
            assert validate_event(event, context).envelope == event
        else:
            with pytest.raises(EventValidationError): validate_event(event, context)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled', [False, True])
@pytest.mark.parametrize('original_qualified', [False, True])
async def test_optional_preview_never_uses_bad_original_page_for_pairing(tmp_path, startup, canceled, original_qualified):
    owned, runtime, aid, advance, wall, mono, result, stopper, due = await _world(tmp_path, startup, canceled)
    runtime.check_config = AttemptConfig(1, Decimal('1'), Decimal(0))
    calls = []
    preview = {**_osv(), 'identity': '/DCIM/a.LRF', 'locator': {'path': '/DCIM/a.LRF'},
        'format_id': 'lrf', 'paired_identity': '/DCIM/a.OSV'}
    async def actual(request, batch):
        calls.append(request.ticket)
        if len(calls) == 1:
            return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv() if original_qualified else preview], next_cursor=_CURSOR))
        wall[0] = due-1
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[preview if original_qualified else _osv()], cursor=_CURSOR, finalized=True))
    result.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        wall[0] = due+100
        await advance(aid, runtime)
        await advance(aid, runtime)
        retained = _retained(owned, aid)
        assert len(retained) == int(original_qualified)
        assert owned.connection.execute('SELECT COUNT(*) FROM device_files WHERE completion_state=3').fetchone() == (int(original_qualified),)
        assert runtime.action(aid)['status'] == (6 if canceled else 4)
        if canceled: await _finish_cancel(owned, wall)
        assert owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE action_id=?', (aid,)).fetchone() == (1, 1)
        assert len(calls) == 2
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled', [False, True])
@pytest.mark.parametrize('committed', [False, True])
async def test_qualified_file_unknown_keeps_original_source_and_key_across_runtime_restart(tmp_path, startup, canceled, committed):
    from camctl.persistence.models import DbOutcome
    owned, runtime, aid, advance, wall, mono, result, stopper, due = await _world(tmp_path, startup, canceled)
    runtime.check_config = AttemptConfig(1, Decimal('1'), Decimal(0))
    calls = []
    async def actual(request, batch):
        calls.append(request.ticket)
        if len(calls) == 1:
            return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()], next_cursor=_CURSOR))
        wall[0] = due-1
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_other()], cursor=_CURSOR, finalized=True))
    result.list_results.side_effect = actual
    save = runtime.capture.save_file_completion
    requests = []
    def uncertain(command, key, connection):
        requests.append((command, key))
        if len(requests) <= 2:
            if committed and len(requests) == 1:
                assert save(command, key, connection).kind is DbOutcomeKind.COMPLETED
            return DbOutcome(DbOutcomeKind.UNKNOWN)
        return save(command, key, connection)
    runtime.capture.save_file_completion = uncertain
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        wall[0] = due+100
        with pytest.raises(ConsistencyError): await advance(aid, runtime)
        assert len(calls) == 2 and len(requests) == 2
        pages = _pages(owned)
        # 新运行时接管未决原文件申请；时间映射为新会话，申请仍保留原键。
        runtime = replace(runtime, natural_wait_states={}, timelapse_deadlines={})
        wall[0] = due-1
        await advance(aid, runtime)
        assert requests[0] == requests[1] == requests[2]
        assert len(calls) == 2 and _pages(owned) == pages
        wall[0] = due+100
        for _ in range(3): await advance(aid, runtime)
        assert len(_retained(owned, aid)) == 1
        assert runtime.action(aid)['status'] == (6 if canceled else 4)
        if canceled: await _finish_cancel(owned, wall)
        assert len(calls) == 2 and stopper.stop.await_count == int(canceled)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled', [False, True])
async def test_bad_nonfinal_page_stops_remaining_pages_but_retains_good_previous_page(tmp_path, startup, canceled):
    owned, runtime, aid, advance, wall, mono, result, stopper, due = await _world(tmp_path, startup, canceled)
    runtime.check_config = AttemptConfig(1, Decimal('1'), Decimal(0))
    calls = []
    async def actual(request, batch):
        calls.append(request.ticket)
        assert len(calls) <= 2, '不合格页之后不得继续物理目录分页'
        if len(calls) == 1:
            return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()], next_cursor=_CURSOR))
        wall[0] = due-1
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_other()], cursor=_CURSOR,
            next_cursor=replace(_CURSOR, after_path='/DCIM/b.mp4')))
    result.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        pages = _pages(owned)
        wall[0] = due+100
        for _ in range(3): await advance(aid, runtime)
        assert len(_retained(owned, aid)) == 1
        assert _pages(owned) == pages and len(calls) == 2
        assert runtime.action(aid)['status'] == (6 if canceled else 4)
        if canceled: await _finish_cancel(owned, wall)
        assert owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE action_id=?', (aid,)).fetchone() == (1, 1)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('branch', ['schedule', 'reconfigure', 'complete'])
@pytest.mark.parametrize('observation', ['photo_taken', 'start_confirmed'])
async def test_legacy_saved_wait_key_does_not_bypass_reliable_sent_validation(tmp_path, monkeypatch, startup, branch, observation):
    import camctl.persistence.repositories.timelapse as repository
    owned, runtime, aid, handler = await consumer_world(tmp_path, 'timelapse', catalog=SmallNativeCatalog(startup), start=False)
    actual = returned('operation', observation or 'timelapse_sent', aid)
    if observation is None: actual = replace(actual, observations=())
    runtime.driver.control.return_value = DeviceCallResult.from_outcome(actual)
    try:
        await _control_call(runtime, runtime.action(aid), 'start_timelapse', 'timelapse_sent')
        due = _NOW+(startup+1200)*1000
        original_guard = repository._verify_wait_basis
        # 仅在夹具建立时模拟升级前已保存的等待；原真实 START 及事件不改写。
        monkeypatch.setattr(repository, '_verify_wait_basis', lambda *args, **kwargs: None)
        initial = ScheduleWait(aid, WaitPlan(WaitKind.WAIT_THEN_CHECK, due), 200, 0, _NOW)
        initial_key = new_operation_key()
        assert runtime.timelapse.schedule_wait(initial, initial_key, owned).kind is DbOutcomeKind.COMPLETED
        if branch == 'schedule': save, command, key = runtime.timelapse.schedule_wait, initial, initial_key
        elif branch == 'reconfigure':
            save = runtime.timelapse.reconfigure_wait
            command, key = ScheduleWait(aid, WaitPlan(WaitKind.WAIT_THEN_CHECK, due+1000), 200, 1, _NOW), new_operation_key()
            assert save(command, key, owned).kind is DbOutcomeKind.COMPLETED
        else:
            save = runtime.timelapse.complete_wait
            command, key = WaitCompletedSave(aid, due), new_operation_key()
            assert save(command, key, owned).kind is DbOutcomeKind.COMPLETED
        monkeypatch.setattr(repository, '_verify_wait_basis', original_guard)
        before = tuple(owned.connection.iterdump())
        assert save(command, key, owned).kind is DbOutcomeKind.ROLLED_BACK
        assert tuple(owned.connection.iterdump()) == before
        verify = runtime.timelapse.verify_wait(command, key, owned, reconfigure=branch == 'reconfigure')
        assert verify.kind is DbOutcomeKind.ROLLED_BACK
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled', [False, True])
async def test_legacy_v1_closed_bad_result_retains_original_complete_file(tmp_path, startup, canceled):
    from camctl.devices.evidence import DeviceObservation
    from .result_consumer_fixtures import RESULT_EVIDENCE
    from .test_result_consumer_saves import _result_port
    class LegacyCatalog(SmallNativeCatalog):
        def parameter_definition(self, device_id, action_type, parameter_type):
            definition = super().parameter_definition(device_id, action_type, parameter_type)
            if definition is not None and action_type == 'camera_timelapse':
                factory = definition.task_factory
                return replace(definition, task_factory=lambda params: replace(factory(params), file_completion_wait_s=None))
            return definition
    owned, runtime, aid, advance, wall, mono, old_driver, stopper, due = await _world(
        tmp_path, startup, canceled, catalog=LegacyCatalog(startup))
    runtime.check_config = AttemptConfig(2, Decimal('1'), Decimal(0))
    runtime.evidence = RESULT_EVIDENCE
    driver = _result_port(runtime, returned("results", "photo_taken", aid))
    calls = []
    async def actual(request, batch):
        calls.append(request.ticket)
        if len(calls) == 2: wall[0] = due-1
        original = _actual(request.ticket, entries=[_osv() if len(calls) == 1 else _other()],
            error=ErrorValue('device_error', 'device') if len(calls) == 1 else None)
        observation = DeviceObservation('result_files_listed', 1,
            {'activity_id': request.ticket.target_id, 'entries': original.observations[0].data['entries']})
        return DeviceCallResult.from_outcome(replace(original, observations=(observation,)))
    driver.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        original_file = owned.connection.execute('SELECT id,completion_evidence_json FROM device_files WHERE completion_state=3').fetchone()
        assert original_file is not None
        await advance(aid, runtime)
        raw = owned.connection.execute('SELECT result_json FROM operation_attempts WHERE run_id=? ORDER BY attempt_no', (calls[0].run_id,)).fetchall()
        runtime = replace(runtime, natural_wait_states={}, timelapse_deadlines={}, pending_start_results={}, pending_result_scans={})
        wall[0] = due+100
        await advance(aid, runtime)
        retained = _retained(owned, aid)
        assert len(retained) == 1 and retained[0][1:] == original_file
        assert runtime.action(aid)['status'] == (6 if canceled else 4)
        if canceled: await _finish_cancel(owned, wall)
        assert owned.connection.execute('SELECT result_json FROM operation_attempts WHERE run_id=? ORDER BY attempt_no', (calls[0].run_id,)).fetchall() == raw
        assert owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE action_id=?', (aid,)).fetchone() == (1, 1)
        assert len(calls) == 2 and stopper.stop.await_count == int(canceled)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled', [False, True])
async def test_previously_held_good_page_after_bad_page_keeps_independent_file_fact(tmp_path, startup, canceled):
    from camctl.capture.handlers import _advance_timelapse_natural_wait, _begin_check_round
    from camctl.capture.result_pages import ResultPageSave
    from camctl.capture.result_scans import SavedResultEntries
    from camctl.operations.validation import validate_outcome
    owned, runtime, aid, advance, wall, mono, result, stopper, due = await _world(tmp_path, startup, canceled)
    runtime.check_config = AttemptConfig(1, Decimal('1'), Decimal(0))
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        assert _advance_timelapse_natural_wait(runtime, runtime.action(aid)) is not None
        ticket = _begin_check_round(runtime, aid).ticket
        first = _actual(ticket, entries=[_other()], next_cursor=_CURSOR)
        second = _actual(ticket, entries=[_osv()], cursor=_CURSOR, finalized=True)
        for number, actual, cursor, instant in ((1, first, None, due-1), (2, second, _CURSOR, due+100)):
            receipt = runtime.capture.save_result_page(ResultPageSave(ticket, number, cursor,
                validate_outcome(ticket, actual, runtime.evidence), instant), new_operation_key(), owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        runtime.hold_call_result(ticket, second, occurred_at=due+100, returned_ns=mono[0],
            result_listing=SavedResultEntries(runtime.capture, runtime.owned, receipt.value))
        raw = _pages(owned)
        await advance(aid, runtime)
        await advance(aid, runtime)
        assert len(_retained(owned, aid)) == 1
        assert runtime.action(aid)['status'] == (6 if canceled else 4)
        if canceled: await _finish_cancel(owned, wall)
        assert _pages(owned) == raw
        assert owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE action_id=?', (aid,)).fetchone() == (1, 1)
        result.list_results.assert_not_awaited()
    finally:
        owned.connection.close()


async def test_retained_sources_keep_bounded_pages_and_original_completion_metadata(tmp_path):
    owned, runtime, aid, advance, wall, mono, result, stopper, due = await _world(tmp_path, 1000)
    runtime.check_config = AttemptConfig(2, Decimal('1'), Decimal(0))
    calls, reads = [], []
    read = runtime.capture.read_result_sources
    def bounded(ticket, cursor, batch, connection):
        reads.append((ticket, cursor, batch))
        assert batch == 32
        return read(ticket, cursor, batch, connection)
    runtime.capture.read_result_sources = bounded
    async def actual(request, batch):
        calls.append(request.ticket)
        if len(calls) == 1:
            return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv(), _other()], error=ErrorValue('device_error', 'device')))
        wall[0] = due-1
        # 同一原片的坏新元数据不能覆盖可靠完成来源的名称和类型。
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[{**_osv(), 'original_name': 'bad-new-name.OSV'}], finalized=True))
    result.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += 2200*1000000
        await advance(aid, runtime)
        await advance(aid, runtime)
        wall[0] = due+100
        await advance(aid, runtime)
        assert runtime.action(aid)['status'] == 4 and len(_retained(owned, aid)) == 2
        first = read(calls[0], None, 1, owned)
        second = read(calls[0], first.next_cursor, 1, owned)
        assert first.next_cursor is not None and second.next_cursor is None
        assert len(first.items) == len(second.items) == 1
        assert first.items[0][0].identity != second.items[0][0].identity
        assert first.items[0][0].original_name is None
        assert reads and len(calls) == 2
    finally:
        owned.connection.close()

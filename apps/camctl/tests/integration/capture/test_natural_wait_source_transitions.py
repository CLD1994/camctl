"""RESULTS沿原意图选择来源；后来的取消与STOP不改写原页。"""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from camctl.capture.handlers import _advance_timelapse_natural_wait, _listing_round, _register_listing, _stop_call
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.ports import DeviceCallResult, StopDriver
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue, Settlement, SettlementBasis
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .result_consumer_fixtures import returned
from .test_cancel_timelapse_exhaustion_files import _apply_public_cancel
from .test_native_completion_context import _osv
from .test_natural_wait_time_eligibility import _public_world, _world
from .test_natural_wait_retention import _finish_public_cancel, _pages, _retained, _other
from .test_result_page_history import _actual, _CURSOR
from .test_record_file_completion_wait import _completed_result

pytestmark = pytest.mark.asyncio


def _stopper(runtime, aid, outcome):
    driver = create_autospec(StopDriver, instance=True)
    actual = (returned('stop', 'stop_confirmed', aid) if outcome == 'confirmed' else CallOutcome(
        status=AttemptStatus.FAILED if outcome == 'failed' else AttemptStatus.UNKNOWN,
        effect=EffectState.UNKNOWN, error=ErrorValue('device_error', 'device'),
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue('stop_returned', 1, {}))))
    driver.stop.return_value = DeviceCallResult.from_outcome(actual)
    runtime.stopper = driver
    return driver


async def _finish_cancel(owned, wall, confirmed):
    from camctl.cancellation.models import FinishCancelAction
    from camctl.cancellation.service import ApplyCancel, CancellationRuntime, apply_cancel
    from camctl.cancellation.settlement import TargetSettlement
    from camctl.persistence.repositories.cancellation import CancellationRepository
    repo = CancellationRepository()
    origin, = owned.connection.execute('SELECT id FROM actions WHERE type=6').fetchone()
    items = tuple(row[0] for row in owned.connection.execute('SELECT id FROM cancel_items WHERE action_id=?', (origin,)))
    settlement = TargetSettlement(owned, None, repo, lambda _: '', lambda: wall[0])
    await apply_cancel(ApplyCancel(origin, items), CancellationRuntime(owned, repo, settlement, lambda: wall[0]))
    receipt = repo.finish_cancel_action(FinishCancelAction(origin, wall[0]), new_operation_key(), owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    assert owned.connection.execute('SELECT status FROM actions WHERE id=?', (origin,)).fetchone() == (3 if confirmed else 4,)


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('stop_outcome', ['confirmed', 'failed', 'unknown'])
@pytest.mark.parametrize('bad', [False, True])
async def test_public_cancel_during_natural_read_keeps_original_basis_and_page_time(tmp_path, startup, stop_outcome, bad):
    owned, runtime, aid, advance, wall, mono, result, unused, due = await _public_world(tmp_path, startup)
    stop = _stopper(runtime, aid, stop_outcome)
    runtime.check_config = AttemptConfig(1, Decimal('1'), Decimal(0))
    contexts = []
    async def actual(request, batch):
        contexts.append(request.params['completion_context'])
        _apply_public_cancel(owned, aid, wall[0])
        if bad: wall[0] = due-1
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()], finalized=True))
    result.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        raw = _pages(owned)
        wall[0] = due+100
        for _ in range(3): await advance(aid, runtime)
        assert owned.connection.execute('SELECT status FROM actions WHERE id=?', (aid,)).fetchone() == (6,)
        from camctl.contracts.json_values import parse_exact_json
        capture = owned.connection.execute('SELECT capture_json FROM device_activities WHERE action_id=?', (aid,)).fetchone()[0]
        assert capture is None or parse_exact_json(capture)['status'] != 'completed'
        retained = _retained(owned, aid)
        assert len(retained) == int(not bad)
        if not bad:
            from camctl.contracts.json_values import parse_exact_json
            proof = parse_exact_json(retained[0][2])
            assert proof['basis'] == 2
            assert proof['wait_completed_event_id'] == contexts[0]['wait_completed_event_id']
            assert proof['start_result_event_id'] == contexts[0]['start_result_event_id']
            assert 'stop_result_event_id' not in proof
        assert _pages(owned) == raw and len(contexts) == 1
        assert raw[0][1] == (due-1 if bad else due+100)
        assert stop.stop.await_count == result.list_results.await_count == 1
        state = owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE action_id=?', (aid,)).fetchone()
        assert state == ((1, 1) if bad and stop_outcome != 'confirmed' else (3, 2))
        await _finish_public_cancel(owned, runtime, wall, stop_outcome == 'confirmed' and not bad)
    finally:
        await runtime.database.close()
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('bad_last', [False, True])
async def test_natural_pages_cross_stop_with_original_context_cursor_and_budget(tmp_path, startup, bad_last):
    owned, runtime, aid, advance, wall, mono, result, unused, due = await _public_world(tmp_path, startup)
    stop = _stopper(runtime, aid, 'confirmed')
    runtime.check_config = AttemptConfig(1, Decimal('1'), Decimal(0))
    contexts = []
    async def actual(request, batch):
        contexts.append(request.params['completion_context'])
        if len(contexts) == 1:
            assert stop.stop.await_count == 0
            _apply_public_cancel(owned, aid, wall[0])
            return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()], next_cursor=_CURSOR))
        assert stop.stop.await_count == 1
        assert request.params['cursor'] == _CURSOR.as_json()
        if bad_last: wall[0] = due-1
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_other()], cursor=_CURSOR, finalized=True))
    result.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        wall[0] = due+100
        for _ in range(3): await advance(aid, runtime)
        assert owned.connection.execute('SELECT status FROM actions WHERE id=?', (aid,)).fetchone() == (6,)
        assert len(contexts) == 2 and contexts[0] == contexts[1]
        assert len(_retained(owned, aid)) == (1 if bad_last else 2)
        assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE responsibility_key=?", (f'results/{aid}',)).fetchone() == (1,)
        from camctl.contracts.json_values import parse_exact_json
        capture = owned.connection.execute('SELECT capture_json FROM device_activities WHERE action_id=?', (aid,)).fetchone()[0]
        assert capture is None or parse_exact_json(capture)['status'] != 'completed'
        await _finish_public_cancel(owned, runtime, wall, not bad_last)
    finally:
        await runtime.database.close()
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('valid_wait', [False, True])
async def test_real_stop_source_keeps_independent_wait_even_when_natural_wait_exists(tmp_path, startup, valid_wait):
    owned, runtime, aid, advance, wall, mono, result, unused, due = await _world(tmp_path, startup)
    stop = _stopper(runtime, aid, 'confirmed')
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        assert _advance_timelapse_natural_wait(runtime, runtime.action(aid)) is not None
        _apply_public_cancel(owned, aid, wall[0])
        await _stop_call(runtime, runtime.action(aid), 'stop_timelapse')
        async def actual(request, batch):
            context = request.params['completion_context']
            assert context['kind'] == 'timelapse_stop'
            assert 'startup_margin_ms' not in context
            wall[0] = due-1
            if not valid_wait:
                return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()], finalized=True))
            completion = {'method': 'stop_return_and_wait', 'version': 1,
                'activity_id': context['activity_id'], 'stop_result_event_id': context['stop_result_event_id'],
                'required_wait_ms': 30000, 'observed_wait_ns': 30000000000, 'completed': True}
            return _completed_result(request.ticket, context, entries=[_osv()], finalized=True, completion=completion)
        result.list_results.side_effect = actual
        if valid_wait:
            await advance(aid, runtime)
            assert runtime.action(aid)['status'] == 6 and len(_retained(owned, aid)) == 1
            from camctl.contracts.json_values import parse_exact_json
            proof = parse_exact_json(_retained(owned, aid)[0][2])
            assert proof['basis'] == 3 and 'wait_completed_event_id' not in proof
        else:
            with pytest.raises(ConsistencyError): await advance(aid, runtime)
            assert owned.connection.execute('SELECT COUNT(*) FROM device_files WHERE completion_state=3').fetchone() == (0,)
        assert result.list_results.await_count == stop.stop.await_count == 1
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('save_kind', ['page', 'file'])
@pytest.mark.parametrize('committed', [False, True])
@pytest.mark.parametrize('stop_in_call', [False, True])
async def test_unknown_original_save_crosses_stop_and_connection_without_reread(
        tmp_path, startup, save_kind, committed, stop_in_call):
    from camctl.persistence.models import DbOutcome
    owned, runtime, aid, advance, wall, mono, result, unused, due = await _world(tmp_path, startup)
    stop = _stopper(runtime, aid, 'confirmed')
    runtime.check_config = AttemptConfig(1, Decimal('1'), Decimal(0))
    contexts = []
    async def actual(request, batch):
        contexts.append(request.params['completion_context'])
        _apply_public_cancel(owned, aid, wall[0])
        if stop_in_call:
            await _stop_call(runtime, runtime.action(aid), 'stop_timelapse')
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()], finalized=True))
    result.list_results.side_effect = actual
    name = 'save_result_page' if save_kind == 'page' else 'save_file_completion'
    save = getattr(runtime.capture, name)
    requests = []
    unknown_count = 1 if save_kind == 'page' else 2
    def uncertain(command, key, connection):
        requests.append((command, key))
        if len(requests) <= unknown_count:
            if committed and len(requests) == 1:
                assert save(command, key, connection).kind is DbOutcomeKind.COMPLETED
            return DbOutcome(DbOutcomeKind.UNKNOWN)
        return save(command, key, connection)
    setattr(runtime.capture, name, uncertain)
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        with pytest.raises(ConsistencyError): await advance(aid, runtime)
        assert len(requests) == unknown_count
        if stop.stop.await_count == 0:
            await _stop_call(runtime, runtime.action(aid), 'stop_timelapse')
        path = Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        runtime = replace(runtime, owned=owned, natural_wait_states={}, timelapse_deadlines={})
        wall[0] = due-1
        await advance(aid, runtime)
        assert all(item == requests[0] for item in requests)
        wall[0] = due+100
        for _ in range(3): await advance(aid, runtime)
        assert runtime.action(aid)['status'] == 6
        assert len(_retained(owned, aid)) == 1
        from camctl.contracts.json_values import parse_exact_json
        proof = parse_exact_json(_retained(owned, aid)[0][2])
        assert proof['basis'] == 2
        assert proof['wait_completed_event_id'] == contexts[0]['wait_completed_event_id']
        assert 'stop_result_event_id' not in proof
        assert result.list_results.await_count == stop.stop.await_count == 1
        assert len(_pages(owned)) == 1 and _pages(owned)[0][1] == due+100
        await _finish_cancel(owned, wall, True)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('stop_outcome', ['confirmed', 'failed', 'unknown'])
async def test_reliable_natural_page_recovers_original_source_without_process_cache(tmp_path, startup, stop_outcome):
    from .test_result_scan_recovery import _resumed
    owned, runtime, aid, advance, wall, mono, result, unused, due = await _world(tmp_path, startup)
    stop = _stopper(runtime, aid, stop_outcome)
    runtime.check_config = AttemptConfig(1, Decimal('1'), Decimal(0))
    async def actual(request, batch):
        _apply_public_cancel(owned, aid, wall[0])
        await _stop_call(runtime, runtime.action(aid), 'stop_timelapse')
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()], finalized=True))
    result.list_results.side_effect = actual
    def interrupted(*args, **kwargs):
        raise ConsistencyError('原页可靠保存后模拟进程结束')
    runtime.hold_call_result = interrupted
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        with pytest.raises(ConsistencyError, match='模拟进程结束'): await advance(aid, runtime)
        raw = _pages(owned)
        assert len(raw) == 1 and not _retained(owned, aid)
        pending, = runtime.pending_result_scans.values()
        original_source = runtime.capture.read_result_source(pending.ticket, owned)
        horizon = owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()[0]
        path = Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        resumed = _resumed(owned, runtime, horizon)
        resumed.results, resumed.stopper = runtime.results, stop
        resumed.wait_config = runtime.wait_config
        assert not resumed.pending_result_scans and not resumed.pending_start_results
        assert resumed.capture.read_result_source(pending.ticket, owned) == original_source
        for _ in range(3): await advance(aid, resumed)
        assert resumed.action(aid)['status'] == 6
        assert len(_retained(owned, aid)) == 1 and _pages(owned) == raw
        from camctl.contracts.json_values import parse_exact_json
        proof = parse_exact_json(_retained(owned, aid)[0][2])
        assert proof['basis'] == 2 and 'stop_result_event_id' not in proof
        assert result.list_results.await_count == stop.stop.await_count == 1
        # 可靠末页保留了完整实际返回，恢复仍沿原SUCCEEDED，而非补做设备调用。
        assert owned.connection.execute('SELECT status,error_json FROM operation_attempts WHERE run_id=?', (pending.ticket.run_id,)).fetchone() == (2, None)
        await _finish_cancel(owned, wall, stop_outcome == 'confirmed')
    finally:
        owned.connection.close()


def _legacy_catalog(startup):
    from .test_natural_wait_time_eligibility import SmallNativeCatalog
    class LegacyCatalog(SmallNativeCatalog):
        def parameter_definition(self, device_id, action_type, parameter_type):
            definition = super().parameter_definition(device_id, action_type, parameter_type)
            if definition is not None and action_type == 'camera_timelapse':
                factory = definition.task_factory
                return replace(definition, task_factory=lambda params: replace(factory(params), file_completion_wait_s=None))
            return definition
    return LegacyCatalog(startup)


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('version', [1, 2])
async def test_old_direct_file_before_wait_is_retained_without_upgrading_original_source(tmp_path, startup, version):
    from camctl.capture.handlers import _original_ticket
    from camctl.devices.evidence import DeviceObservation
    from .result_consumer_fixtures import RESULT_EVIDENCE
    from .test_result_consumer_saves import _result_port
    owned, runtime, aid, advance, wall, mono, result, unused, due = await _world(
        tmp_path, startup, catalog=_legacy_catalog(startup))
    stop = _stopper(runtime, aid, 'failed')
    runtime.check_config = AttemptConfig(1, Decimal('1'), Decimal(0))
    if version == 1:
        runtime.evidence = RESULT_EVIDENCE
        result = _result_port(runtime, returned('results', 'photo_taken', aid))
    async def actual(request, batch):
        assert not request.params.get('completion_context')
        _apply_public_cancel(owned, aid, wall[0])
        actual = _actual(request.ticket, entries=[_osv()], finalized=True)
        if version == 1:
            actual = replace(actual, observations=(DeviceObservation('result_files_listed', 1,
                {'activity_id': request.ticket.target_id, 'entries': actual.observations[0].data['entries']}),))
        return DeviceCallResult.from_outcome(actual)
    result.list_results.side_effect = actual
    try:
        wall[0] += 100
        listing = await _listing_round(runtime, aid)
        _register_listing(runtime, aid, listing)
        ticket = _original_ticket(runtime, f'results/{aid}', 'result')
        file_before = owned.connection.execute('SELECT id,completion_evidence_json FROM device_files WHERE completion_state=3').fetchone()
        assert file_before is not None
        await _stop_call(runtime, runtime.action(aid), 'stop_timelapse')
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        assert _advance_timelapse_natural_wait(runtime, runtime.action(aid)) is not None
        original = runtime.capture.read_result_source(ticket, owned)
        assert original.kind.value == 'direct' and original.context is None
        assert original.natural_lower_bound is None
        for _ in range(3): await advance(aid, runtime)
        assert runtime.action(aid)['status'] == 6
        assert len(_retained(owned, aid)) == 1
        assert _retained(owned, aid)[0][1:] == file_before
        assert result.list_results.await_count == stop.stop.await_count == 1
        assert owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE action_id=?', (aid,)).fetchone() == (1, 1)
        await _finish_cancel(owned, wall, False)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('version', [1, 2])
@pytest.mark.parametrize('stop_outcome', ['confirmed', 'failed', 'unknown'])
async def test_old_natural_source_keeps_original_direct_guarantee_after_stop(tmp_path, startup, version, stop_outcome):
    from camctl.devices.evidence import DeviceObservation
    from .result_consumer_fixtures import RESULT_EVIDENCE
    from .test_result_consumer_saves import _result_port
    owned, runtime, aid, advance, wall, mono, result, unused, due = await _world(
        tmp_path, startup, catalog=_legacy_catalog(startup))
    stop = _stopper(runtime, aid, stop_outcome)
    runtime.check_config = AttemptConfig(1, Decimal('1'), Decimal(0))
    if version == 1:
        runtime.evidence = RESULT_EVIDENCE
        result = _result_port(runtime, returned('results', 'photo_taken', aid))
    async def actual(request, batch):
        assert not request.params.get('completion_context')
        _apply_public_cancel(owned, aid, wall[0])
        await _stop_call(runtime, runtime.action(aid), 'stop_timelapse')
        actual = _actual(request.ticket, entries=[_osv()], finalized=True)
        if version == 1:
            actual = replace(actual, observations=(DeviceObservation('result_files_listed', 1,
                {'activity_id': request.ticket.target_id, 'entries': actual.observations[0].data['entries']}),))
        return DeviceCallResult.from_outcome(actual)
    result.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        for _ in range(3): await advance(aid, runtime)
        assert runtime.action(aid)['status'] == 6
        assert len(_retained(owned, aid)) == 1
        from camctl.contracts.json_values import parse_exact_json
        proof = parse_exact_json(_retained(owned, aid)[0][2])
        assert proof['basis'] == 1 and 'stop_result_event_id' not in proof
        assert result.list_results.await_count == stop.stop.await_count == 1
        await _finish_cancel(owned, wall, stop_outcome == 'confirmed')
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('stop_outcome', ['confirmed', 'failed', 'unknown'])
async def test_intent_before_wait_cannot_gain_natural_source_from_later_wait_or_stop(tmp_path, startup, stop_outcome):
    from camctl.capture.handlers import _begin_check_round
    from camctl.capture.result_scans import PendingResultScan
    from camctl.persistence.transaction import row_facts
    owned, runtime, aid, advance, wall, mono, result, unused, due = await _world(tmp_path, startup)
    stop = _stopper(runtime, aid, stop_outcome)
    runtime.check_config = AttemptConfig(1, Decimal('1'), Decimal(0))
    async def actual(request, batch):
        assert not request.params.get('completion_context')
        _apply_public_cancel(owned, aid, wall[0])
        await _stop_call(runtime, runtime.action(aid), 'stop_timelapse')
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()], finalized=True))
    result.list_results.side_effect = actual
    try:
        ticket = _begin_check_round(runtime, aid).ticket
        activity = row_facts(owned.connection, 'device_activities', aid)
        runtime.pending_result_scans[(ticket.run_id, ticket.attempt_id)] = PendingResultScan(
            ticket, activity['output_scope_json'], runtime.check_config.timeout_s)
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        assert _advance_timelapse_natural_wait(runtime, runtime.action(aid)) is not None
        for _ in range(3): await advance(aid, runtime)
        assert runtime.capture.read_result_source(ticket, owned).kind.value == 'unconfirmed'
        assert runtime.action(aid)['status'] == 6 and not _retained(owned, aid)
        assert owned.connection.execute('SELECT COUNT(*) FROM device_files WHERE completion_state=3').fetchone() == (0,)
        assert owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE action_id=?', (aid,)).fetchone() == ((3, 2) if stop_outcome == 'confirmed' else (1, 1))
        assert result.list_results.await_count == stop.stop.await_count == 1
        await _finish_cancel(owned, wall, False)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('first_good', [False, True])
async def test_later_independent_stop_round_does_not_reclassify_prior_natural_page(tmp_path, startup, first_good):
    owned, runtime, aid, advance, wall, mono, result, unused, due = await _world(tmp_path, startup)
    stop = _stopper(runtime, aid, 'confirmed')
    runtime.check_config = AttemptConfig(2, Decimal('1'), Decimal(0))
    contexts = []
    async def actual(request, batch):
        context = request.params['completion_context']
        contexts.append(context)
        if len(contexts) == 1:
            assert context['kind'] == 'timelapse_natural'
            _apply_public_cancel(owned, aid, wall[0])
            if not first_good: wall[0] = due-1
            return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()],
                error=ErrorValue('device_error', 'device')))
        assert context['kind'] == 'timelapse_stop' and context['prior_completion'] is None
        completion = {'method': 'stop_return_and_wait', 'version': 1,
            'activity_id': context['activity_id'], 'stop_result_event_id': context['stop_result_event_id'],
            'required_wait_ms': 30000, 'observed_wait_ns': 30000000000, 'completed': True}
        return _completed_result(request.ticket, context, entries=[_osv()], finalized=True, completion=completion)
    result.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        prior = _pages(owned)
        wall[0] = due+100
        for _ in range(3): await advance(aid, runtime)
        assert runtime.action(aid)['status'] == 6 and len(_retained(owned, aid)) == 1
        assert _pages(owned)[:1] == prior[:1]
        from camctl.contracts.json_values import parse_exact_json
        proof = parse_exact_json(_retained(owned, aid)[0][2])
        assert proof['basis'] == (2 if first_good else 3)
        assert proof.get('wait_completed_event_id') == (contexts[0]['wait_completed_event_id'] if first_good else None)
        assert result.list_results.await_count == 2 and stop.stop.await_count == 1
        await _finish_cancel(owned, wall, True)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('latest', ['empty', 'missing_identity', 'incomplete', 'size_unknown', 'size_mismatch'])
@pytest.mark.parametrize('canceled', [False, True])
async def test_stop_source_retains_known_files_but_does_not_fill_current_collection(tmp_path, startup, latest, canceled):
    owned, runtime, aid, advance, wall, mono, result, unused, due = await _world(tmp_path, startup)
    stop = _stopper(runtime, aid, 'confirmed')
    runtime.check_config = AttemptConfig(2, Decimal('1'), Decimal(0))
    calls = []
    later = {'empty': [], 'missing_identity': [{**_other(), 'complete': False, 'size_bytes': None}],
        'incomplete': [{**_osv(), 'complete': False, 'size_bytes': None}],
        'size_unknown': [{**_osv(), 'size_bytes': None}],
        'size_mismatch': [{**_osv(), 'size_bytes': 51}]}[latest]
    async def actual(request, batch):
        context = request.params['completion_context']
        assert context['kind'] == 'timelapse_stop'
        calls.append(request.ticket)
        completion = {'method': 'stop_return_and_wait', 'version': 1,
            'activity_id': context['activity_id'], 'stop_result_event_id': context['stop_result_event_id'],
            'required_wait_ms': 30000, 'observed_wait_ns': 30000000000, 'completed': True}
        return _completed_result(request.ticket, context,
            entries=[_osv()] if len(calls) == 1 else later,
            finalized=len(calls) == 2, error=ErrorValue('device_error', 'device') if len(calls) == 1 else None,
            completion=completion)
    result.list_results.side_effect = actual
    try:
        from camctl.capture.handlers import _finish_listing_result, _product_requirements, _listing_finalized
        from camctl.capture.results import CaptureFileSet, FileKind, assess_capture_files
        if canceled: _apply_public_cancel(owned, aid, wall[0])
        await _stop_call(runtime, runtime.action(aid), 'stop_timelapse')
        first = await _listing_round(runtime, aid)
        _register_listing(runtime, aid, first)
        original = owned.connection.execute('SELECT id,size_bytes,completion_evidence_json FROM device_files WHERE completion_state=3').fetchone()
        assert original is not None
        _finish_listing_result(runtime, first, retry_wait=True)
        second = await _listing_round(runtime, aid)
        registered = _register_listing(runtime, aid, second)
        assessment = assess_capture_files(CaptureFileSet((file for file, _ in registered), _listing_finalized(second)),
            _product_requirements(runtime.action(aid), FileKind.VIDEO))
        assert not assessment.is_complete
        assert owned.connection.execute('SELECT id,size_bytes,completion_evidence_json FROM device_files WHERE completion_state=3').fetchone() == original
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        for _ in range(4): await advance(aid, runtime)
        assert runtime.action(aid)['status'] == (6 if canceled else 4)
        assert len(_retained(owned, aid)) == 1
        assert result.list_results.await_count == 2 and stop.stop.await_count == 1
        if canceled: await _finish_cancel(owned, wall, True)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('page_time', ['good', 'before_wait', 'after_end'])
async def test_independent_finite_end_guard_checks_every_original_page_time(tmp_path, startup, page_time):
    from camctl.contracts.history_values import TransactionRange
    from camctl.history.validators import EventContext, EventValidationError
    from camctl.persistence.event_reads import SqlEventReads
    from camctl.persistence.transaction import event_envelope, update_change
    from camctl.capture.handlers import _original_ticket
    import camctl.persistence.repositories.finite_cancel as finite
    owned, runtime, aid, advance, wall, mono, result, unused, due = await _world(tmp_path, startup, True)
    async def actual(request, batch):
        cursor = request.params.get('cursor')
        return DeviceCallResult.from_outcome(_actual(request.ticket,
            entries=[_osv()] if cursor is None else [],
            cursor=None if cursor is None else _CURSOR,
            next_cursor=_CURSOR if cursor is None else None, finalized=cursor is not None))
    result.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        ticket = _original_ticket(runtime, f'results/{aid}', 'result')
        page = runtime.capture.read_last_result_page(ticket, owned)
        request = finite.FiniteCancelEndSave(aid, page.ref, wall[0])
        state, action, activity = finite._load(owned, request)
        txn_id, event_id = owned.connection.execute('SELECT MAX(id)+1,last_event_id+1 FROM history_transactions').fetchone()
        event = event_envelope(event_id, txn_id, finite._TYPE, finite._REASON,
            (update_change('device_activities', aid, {'activity_state': 1}, {'activity_state': 3}),),
            wall[0], evidence={'cancel_completion': finite._evidence(state, request)})
        owned.connection.execute('BEGIN')
        reader = SqlEventReads(owned.connection, finite._read_scope(request))
        class OriginalPages:
            scope = reader.scope
            required = reader.required
            def result_pages(self, ticket, cursor, batch):
                actual = reader.result_pages(ticket, cursor, batch)
                return replace(actual, items=tuple(replace(saved, occurred_at=due-1 if page_time == 'before_wait' else due+200)
                    if page_time != 'good' and saved.ref.page_no == 1 else saved for saved in actual.items))
        context = EventContext(TransactionRange(txn_id, event_id, event_id),
            {('device_activities', aid): ('action', aid)}, state, current_reads=OriginalPages())
        if page_time != 'good':
            with pytest.raises(EventValidationError): finite._guard(event, context)
        else:
            finite._guard(event, context)
        owned.connection.execute('ROLLBACK')
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('completed', [False, True])
async def test_declared_stop_wait_on_original_natural_page_is_not_silently_skipped(tmp_path, startup, completed):
    from camctl.capture.handlers import _finish_listing_result
    owned, runtime, aid, advance, wall, mono, result, unused, due = await _world(tmp_path, startup)
    stop = _stopper(runtime, aid, 'confirmed')
    runtime.check_config = AttemptConfig(2, Decimal('1'), Decimal(0))
    async def actual(request, batch):
        assert request.params['completion_context']['kind'] == 'timelapse_natural'
        _apply_public_cancel(owned, aid, wall[0])
        await _stop_call(runtime, runtime.action(aid), 'stop_timelapse')
        event, = owned.connection.execute("SELECT t.result_event_id FROM operation_runs r JOIN operation_attempts t ON t.run_id=r.id WHERE r.responsibility_key=?", (f'stop/{aid}',)).fetchone()
        completion = {'method': 'stop_return_and_wait', 'version': 1,
            'activity_id': request.ticket.target_id, 'stop_result_event_id': event,
            'required_wait_ms': 30000, 'observed_wait_ns': 30000000000 if completed else 0,
            'completed': completed}
        return _completed_result(request.ticket, None, completion=completion,
            error=ErrorValue('device_error', 'device'))
    result.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        assert _advance_timelapse_natural_wait(runtime, runtime.action(aid)) is not None
        first = await _listing_round(runtime, aid)
        _finish_listing_result(runtime, first, retry_wait=True)
        raw = _pages(owned)
        with pytest.raises(ConsistencyError): await _listing_round(runtime, aid)
        assert _pages(owned) == raw and not _retained(owned, aid)
        assert result.list_results.await_count == stop.stop.await_count == 1
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled', [False, True])
async def test_old_v2_retained_metadata_comes_from_original_complete_source(tmp_path, startup, canceled):
    owned, runtime, aid, advance, wall, mono, result, stopper, due = await _world(
        tmp_path, startup, canceled, catalog=_legacy_catalog(startup))
    runtime.check_config = AttemptConfig(2, Decimal('1'), Decimal(0))
    calls = []
    async def actual(request, batch):
        calls.append(request.ticket)
        if len(calls) == 1:
            return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()],
                error=ErrorValue('device_error', 'device')))
        wall[0] = due-1
        return DeviceCallResult.from_outcome(_actual(request.ticket,
            entries=[{**_osv(), 'original_name': 'bad-new-name.OSV'}], finalized=True))
    result.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        original = owned.connection.execute('SELECT id,completion_evidence_json FROM device_files WHERE completion_state=3').fetchone()
        await advance(aid, runtime)
        raw = _pages(owned)
        wall[0] = due+100
        for _ in range(3): await advance(aid, runtime)
        assert runtime.action(aid)['status'] == (6 if canceled else 4)
        assert len(_retained(owned, aid)) == 1 and _retained(owned, aid)[0][1:] == original
        saved = runtime.capture.read_result_sources(calls[0], None, 1, owned)
        assert saved.items[0][0].original_name is None
        assert _pages(owned) == raw and result.list_results.await_count == 2
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled, save_mode', [(False, 'reliable'), (True, 'reliable'), (True, 'before_commit'), (True, 'after_commit')])
async def test_legal_unanchored_start_consumes_finite_query_without_completing_activity(tmp_path, startup, canceled, save_mode):
    from camctl.capture.handlers import capture_handler
    from camctl.capture.recovery import RecoveryBoundary
    from camctl.capture.timelapse import CaptureWaitConfig
    from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
    from camctl.persistence.models import DbOutcome
    from .result_consumer_fixtures import consumer_world
    from .test_capture_contract import _NOW
    from .test_natural_wait_time_eligibility import SmallNativeCatalog
    from .test_record_file_completion_wait import _result_port
    owned, runtime, aid, handler = await consumer_world(tmp_path, 'timelapse',
        catalog=SmallNativeCatalog(startup), start=False)
    wall = [_NOW]
    runtime.wall_us = lambda: wall[0]
    runtime.wait_config = lambda action: CaptureWaitConfig(1000, 200, 0, startup)
    runtime.grant(runtime.action(aid))
    if canceled: _apply_public_cancel(owned, aid, _NOW)
    recovery = EvidenceRegistry((EvidenceContract('adb_foreground_recovery', 1, 'control', frozenset()),))
    runtime.recovery_boundary = RecoveryBoundary.HOST_LOCAL_SETTLED
    runtime.recovery_max_event_id = owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()[0]
    runtime.recovery_evidence_for = lambda binding, operation: recovery if operation == 'control' else None
    stop = _stopper(runtime, aid, 'unknown')
    async def actual(request, batch):
        assert not request.params.get('completion_context')
        return DeviceCallResult.from_outcome(_actual(request.ticket, finalized=True))
    result = _result_port(runtime, actual)
    save = runtime.capture.save_result_page
    requests = []
    def uncertain(command, key, connection):
        requests.append((command, key))
        if len(requests) == 1 and save_mode != 'reliable':
            if save_mode == 'after_commit': assert save(command, key, connection).kind is DbOutcomeKind.COMPLETED
            return DbOutcome(DbOutcomeKind.UNKNOWN)
        return save(command, key, connection)
    runtime.capture.save_result_page = uncertain
    try:
        advance = capture_handler(handler)
        if canceled and save_mode != 'reliable':
            with pytest.raises(ConsistencyError): await advance(aid, runtime)
            runtime = replace(runtime, natural_wait_states={}, timelapse_deadlines={})
        await advance(aid, runtime)
        assert runtime.action(aid)['status'] == (6 if canceled else 4)
        assert owned.connection.execute('SELECT sent_at,wait_completed_event_id,activity_state,occupancy_state FROM device_activities WHERE action_id=?', (aid,)).fetchone() == (None, None, 1, 1)
        assert runtime.driver.control.await_count == 0
        assert stop.stop.await_count == result.list_results.await_count == int(canceled)
        assert not _retained(owned, aid)
        assert all(request == requests[0] for request in requests)
        if canceled: await _finish_cancel(owned, wall, False)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('canceled', [False, True])
@pytest.mark.parametrize('opaque', [False, True])
async def test_v1_complete_source_remains_original_after_v2_listing(tmp_path, startup, canceled, opaque):
    from camctl.capture.handlers import ResultFilesPort
    from camctl.capture.result_inputs import ListedResult, observed_file
    from camctl.devices.evidence import DeviceObservation
    from .result_consumer_fixtures import RESULT_EVIDENCE
    from .test_record_file_completion_wait import _result_port
    owned, runtime, aid, advance, wall, mono, unused, stopper, due = await _world(
        tmp_path, startup, canceled, catalog=_legacy_catalog(startup))
    runtime.check_config = AttemptConfig(2, Decimal('1'), Decimal(0))
    runtime.evidence = RESULT_EVIDENCE
    old = create_autospec(ResultFilesPort, instance=True)
    async def actual_v1(ticket, *, timeout_s):
        outcome = _actual(ticket, entries=[_osv()], error=ErrorValue('device_error', 'device'))
        outcome = replace(outcome, observations=(DeviceObservation('result_files_listed', 1,
            {'activity_id': ticket.target_id, 'entries': outcome.observations[0].data['entries']}),))
        return ListedResult((replace(observed_file(_osv()), evidence={'listing': 'clip-1'} if opaque else dict(_osv())),), outcome)
    old.list_round.side_effect = actual_v1
    runtime.results = old
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        original = owned.connection.execute('SELECT id,completion_evidence_json FROM device_files WHERE completion_state=3').fetchone()
        assert original is not None
        async def actual_v2(request, batch):
            wall[0] = due-1
            return DeviceCallResult.from_outcome(_actual(request.ticket,
                entries=[{**_osv(), 'original_name': 'bad-new-name.OSV'} if opaque else _osv()], finalized=True))
        new = _result_port(runtime, actual_v2)
        await advance(aid, runtime)
        raw = _pages(owned)
        wall[0] = due+100
        for _ in range(3): await advance(aid, runtime)
        assert runtime.action(aid)['status'] == (6 if canceled else 4)
        assert len(_retained(owned, aid)) == 1 and _retained(owned, aid)[0][1:] == original
        saved = runtime.capture.read_result_sources(new.list_results.call_args.args[0].ticket, None, 1, owned)
        assert saved.items[0][0].original_name is None
        assert _pages(owned) == raw
        assert old.list_round.await_count == new.list_results.await_count == 1
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
@pytest.mark.parametrize('change', ['missing_identity', 'different_metadata'])
async def test_old_v2_missing_original_complete_entry_is_diagnosed_without_latest_fallback(tmp_path, startup, change, monkeypatch):
    from copy import deepcopy
    from camctl.persistence.repositories import result_pages
    owned, runtime, aid, advance, wall, mono, result, unused, due = await _world(
        tmp_path, startup, catalog=_legacy_catalog(startup))
    runtime.check_config = AttemptConfig(1, Decimal('1'), Decimal(0))
    async def actual(request, batch):
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()], finalized=True))
    result.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        await advance(aid, runtime)
        ticket = result.list_results.call_args.args[0].ticket
        before = tuple(owned.connection.iterdump())
        reader = result_pages.row_facts
        def inconsistent(connection, table, identity):
            fact = reader(connection, table, identity)
            if table == 'device_files':
                fact = deepcopy(fact)
                observation = fact['completion_evidence_json']['observation']
                if change == 'missing_identity': observation.pop('identity')
                else: observation['original_name'] = 'unknown-origin.OSV'
            return fact
        monkeypatch.setattr(result_pages, 'row_facts', inconsistent)
        with pytest.raises(ConsistencyError): runtime.capture.read_result_sources(ticket, None, 1, owned)
        assert tuple(owned.connection.iterdump()) == before
        assert result.list_results.await_count == 1 and len(_retained(owned, aid)) == 1
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [0, 1000])
async def test_closed_v1_natural_round_after_later_stop_keeps_only_current_raw_entries(tmp_path, startup):
    from camctl.capture.handlers import _finish_listing_result, _saved_result_listing
    from camctl.devices.evidence import DeviceObservation
    from .result_consumer_fixtures import RESULT_EVIDENCE
    from .test_result_consumer_saves import _result_port
    owned, runtime, aid, advance, wall, mono, unused, oldstop, due = await _world(
        tmp_path, startup, catalog=_legacy_catalog(startup))
    runtime.check_config = AttemptConfig(2, Decimal('1'), Decimal(0))
    runtime.evidence = RESULT_EVIDENCE
    driver = _result_port(runtime, returned('results', 'photo_taken', aid))
    stop = _stopper(runtime, aid, 'confirmed')
    calls = []
    async def actual(request, batch):
        calls.append(request.ticket)
        if len(calls) == 2:
            _apply_public_cancel(owned, aid, wall[0])
            await _stop_call(runtime, runtime.action(aid), 'stop_timelapse')
        value = _actual(request.ticket, entries=[_osv()] if len(calls) == 1 else [],
            error=ErrorValue('device_error', 'device') if len(calls) == 1 else None)
        value = replace(value, observations=(DeviceObservation('result_files_listed', 1,
            {'activity_id': request.ticket.target_id, 'entries': value.observations[0].data['entries']}),))
        return DeviceCallResult.from_outcome(value)
    driver.list_results.side_effect = actual
    try:
        wall[0] = due+100
        mono[0] += (startup+1200)*1000000
        assert _advance_timelapse_natural_wait(runtime, runtime.action(aid)) is not None
        first = await _listing_round(runtime, aid)
        _register_listing(runtime, aid, first)
        _finish_listing_result(runtime, first, retry_wait=True)
        second = await _listing_round(runtime, aid)
        _finish_listing_result(runtime, second, retry_wait=True)
        assert runtime.capture.read_result_source(second.ticket, owned).kind.value == 'natural'
        restored = _saved_result_listing(runtime, aid)
        assert tuple(restored.entries) == ()
        assert tuple(_register_listing(runtime, aid, restored)) == ()
        retained = runtime.capture.read_result_sources(second.ticket, None, 1, owned)
        assert len(retained.items) == 1 and retained.items[0][1].complete
        assert driver.list_results.await_count == 2 and stop.stop.await_count == 1
    finally:
        owned.connection.close()


@pytest.mark.parametrize('consumer', ['photo', 'record'])
@pytest.mark.parametrize('binding_failure', [False, True])
async def test_shared_current_filter_preserves_prior_complete_files_at_failure_boundary(tmp_path, consumer, binding_failure):
    from camctl.capture.handlers import _finish_listing_result, capture_handler
    from camctl.devices.bindings import BindingResult, BindingStatus
    from .result_consumer_fixtures import consumer_world
    from .test_record_file_completion_wait import _result_port
    from .test_result_page_history import _entry
    owned, runtime, aid, handler = await consumer_world(tmp_path, consumer)
    runtime.check_config = AttemptConfig(2, Decimal('1'), Decimal(0))
    calls = []
    entry = {**_entry('a'), 'kind': 'photo' if consumer == 'photo' else 'video'}
    async def actual(request, batch):
        calls.append(request.ticket)
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[entry] if len(calls) == 1 else [],
            finalized=len(calls) == 2,
            error=ErrorValue('device_error', 'device') if len(calls) == 1 or binding_failure else None))
    driver = _result_port(runtime, actual)
    try:
        first = await _listing_round(runtime, aid)
        _register_listing(runtime, aid, first)
        original = owned.connection.execute('SELECT id,completion_evidence_json FROM device_files WHERE completion_state=3').fetchone()
        assert original is not None
        _finish_listing_result(runtime, first, retry_wait=True)
        second = await _listing_round(runtime, aid)
        assert tuple(_register_listing(runtime, aid, second)) == ()
        if binding_failure:
            _finish_listing_result(runtime, second, retry_wait=True)
            runtime.binding_check = lambda saved: BindingResult(BindingStatus.DEVICE_MISSING, saved, 'missing')
        await capture_handler(handler)(aid, runtime)
        assert runtime.action(aid)['status'] == 4
        assert len(_retained(owned, aid)) == 1 and _retained(owned, aid)[0][1:] == original
        assert driver.list_results.await_count == 2
    finally:
        owned.connection.close()


async def test_recording_media_cache_keeps_original_current_set_instead_of_prior_complete_files(tmp_path):
    from types import SimpleNamespace
    from camctl.capture.handlers import _advance_recording_outcome, _finish_listing_result
    from camctl.capture.recording import RecordingPhase
    from .result_consumer_fixtures import ResultCatalog, consumer_world
    from .test_record_file_completion_wait import _result_port
    from .test_result_page_history import _entry
    class OneRecordCatalog(ResultCatalog):
        def parameter_definition(self, device_id, action_type, parameter_type):
            definition = super().parameter_definition(device_id, action_type, parameter_type)
            if definition is not None and action_type == 'camera_record':
                factory = definition.task_factory
                return replace(definition, task_factory=lambda params: replace(factory(params),
                    product_rules=({'kind': 'video', 'format_id': None, 'min_count': 1,
                        'exact_count': 1, 'require_pairing': False},)))
            return definition
    owned, runtime, aid, handler = await consumer_world(tmp_path, 'record', catalog=OneRecordCatalog())
    runtime.check_config = AttemptConfig(2, Decimal('1'), Decimal(0))
    calls = []
    async def actual(request, batch):
        calls.append(request.ticket)
        return DeviceCallResult.from_outcome(_actual(request.ticket,
            entries=[_entry('a' if len(calls) == 1 else 'b')], finalized=len(calls) == 2,
            error=ErrorValue('device_error', 'device') if len(calls) == 1 else None))
    driver = _result_port(runtime, actual)
    try:
        first = await _listing_round(runtime, aid)
        _register_listing(runtime, aid, first)
        _finish_listing_result(runtime, first, retry_wait=True)
        second = await _listing_round(runtime, aid)
        registered = _register_listing(runtime, aid, second)
        assert [file.file_id for file, _ in registered] == ['/DCIM/b.mp4']
        runtime.listing_cache = {aid: (registered.entries, registered)}
        await _advance_recording_outcome(runtime, runtime.action(aid), SimpleNamespace(phase=RecordingPhase.CONTROL_COMPLETE))
        assert runtime.action(aid)['status'] == 3
        assert [file.file_id for file, _ in runtime.listing_cache[aid][1]] == ['/DCIM/b.mp4']
        assert owned.connection.execute("SELECT json_extract(f.identity_key,'$[2]') FROM outputs o JOIN device_files f ON f.id=o.device_file_id WHERE o.source_action_id=? ORDER BY f.id", (aid,)).fetchall() == [('/DCIM/a.mp4',), ('/DCIM/b.mp4',)]
        assert owned.connection.execute('SELECT COUNT(*) FROM device_files WHERE completion_state=3').fetchone() == (2,)
        assert driver.list_results.await_count == 2
    finally:
        owned.connection.close()

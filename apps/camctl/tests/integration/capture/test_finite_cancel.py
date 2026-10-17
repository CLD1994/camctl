"""有限设备任务取消后的原责任与自然期限。"""
from dataclasses import replace
from decimal import Decimal
import json
from unittest.mock import create_autospec

import pytest

from camctl.capture.handlers import capture_handler
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.devices.ports import DeviceCallResult, StopDriver
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue, Settlement, SettlementBasis

from .test_capture_contract import _TIMELAPSE, _NOW, _environment, _runtime, _entry, ResultsDouble
from camctl.capture.result_inputs import page_from_outcome
from camctl.devices.evidence import DeviceObservation
from .test_cancel_timelapse_exhaustion_files import _apply_public_cancel

pytestmark = pytest.mark.asyncio


class _FiniteResults(ResultsDouble):
    async def list_page(self, ticket, *, cursor, timeout_s, output_scope=None, completion_context=None):
        if completion_context is not None:
            assert completion_context['kind'] == 'timelapse_natural'
        page = await super().list_page(ticket, cursor=cursor, timeout_s=timeout_s,
            output_scope=output_scope, completion_context=completion_context)
        observations = tuple(DeviceObservation(o.type, o.version,
            {**o.data, 'entries': [{**e, 'format_id': 'osv'} for e in o.data['entries']]})
            if o.type == 'result_files_listed' else o for o in page.outcome.observations)
        return page_from_outcome(ticket, replace(page.outcome, observations=observations), cursor=cursor)


def _freeze_finite_spec(owned, *, extra_rules=()):
    spec = json.loads(owned.connection.execute('SELECT execution_spec_json FROM actions WHERE id=13').fetchone()[0])
    spec.update(target_duration_ms=1000, result_wait_margin_ms=200,
                cancel_completion_policy='finite_device_end', file_completion_wait_ms=30000,
                product_rules=[{'kind':'video','format_id':'osv','min_count':1,
                    'exact_count':None,'require_pairing':False,'min_size_bytes':1}])
    spec['product_rules'].extend(extra_rules)
    owned.connection.execute('UPDATE actions SET execution_spec_json=? WHERE id=13', (json.dumps(spec),))
    owned.connection.execute('UPDATE actions SET input_fields_json=?, effective_params_json=? WHERE id=13',
        (json.dumps({'params':{'type':'finite_fake'},'policy':{'max_delay_ms':1000}}), json.dumps({'type':'finite_fake'})))


async def _world(tmp_path, *, unknown=False, extra_rules=(), startup_margin_ms=None):
    owned = _environment(tmp_path, _TIMELAPSE)
    _freeze_finite_spec(owned, extra_rules=extra_rules)
    if startup_margin_ms is not None:
        spec = json.loads(owned.connection.execute('SELECT execution_spec_json FROM actions WHERE id=13').fetchone()[0])
        spec['startup_margin_ms'] = startup_margin_ms
        owned.connection.execute('UPDATE actions SET execution_spec_json=? WHERE id=13', (json.dumps(spec),))
    wall, mono = [_NOW], [5_000_000_000]
    runtime = _runtime(owned, results=_FiniteResults({13: (_entry('finite-original'),)}),
        stop_config=AttemptConfig(3, Decimal('1'), Decimal('0')))
    runtime.wall_us = lambda: wall[0]
    runtime.monotonic_ns = lambda: mono[0]
    runtime.wait_config = lambda action: CaptureWaitConfig(1000, 200, 50, startup_margin_ms=startup_margin_ms or 0)
    advance = capture_handler('camera_timelapse')
    await advance(13, runtime)
    _apply_public_cancel(owned, 13, _NOW+100_000)
    stopper = create_autospec(StopDriver, instance=True)
    stopper.stop.return_value = DeviceCallResult.from_outcome(CallOutcome(
        status=AttemptStatus.UNKNOWN if unknown else AttemptStatus.FAILED,
        effect=EffectState.UNKNOWN,
        error=ErrorValue('device_error', 'device'),
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue('stop_returned', 1, {}))))
    runtime.stopper = stopper
    return owned, runtime, advance, stopper, wall, mono


@pytest.mark.parametrize('unknown', [False, True])
async def test_first_failed_stop_transfers_responsibility_without_retry_or_early_listing(tmp_path, unknown):
    owned, runtime, advance, stopper, wall, mono = await _world(tmp_path, unknown=unknown)
    try:
        await advance(13, runtime)
        assert owned.connection.execute("SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE responsibility_key='stop/13'").fetchone() == (4, 1, 0)
        assert owned.connection.execute('SELECT sent_at FROM device_activities WHERE id=13').fetchone() == (_NOW,)
        assert runtime.results.calls == []
        assert owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE id=13').fetchone() == (1, 1)
        await advance(13, runtime)
        stopper.stop.assert_awaited_once()
        assert runtime.driver.calls == ['start_timelapse']
        assert runtime.results.calls == []
        assert owned.connection.execute('SELECT status FROM actions WHERE id=13').fetchone() == (2,)
        stop_txn = owned.connection.execute("SELECT e.transaction_id FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id JOIN history_events e ON e.id=a.result_event_id WHERE r.responsibility_key='stop/13'").fetchone()[0]
        failed_txn = owned.connection.execute("SELECT e.transaction_id FROM operation_runs r JOIN history_events e ON e.event_type=10 AND json_extract(e.body_json,'$.rows[0].id')=r.id WHERE r.responsibility_key='stop/13' ORDER BY e.id DESC LIMIT 1").fetchone()[0]
        assert stop_txn == failed_txn
    finally:
        owned.connection.close()


async def test_restarted_finite_cancel_waits_only_remaining_original_deadline(tmp_path):
    owned, runtime, advance, stopper, wall, mono = await _world(tmp_path)
    try:
        await advance(13, runtime)
        restarted = replace(runtime, natural_wait_states={}, timelapse_deadlines={}, wall_us=lambda: _NOW+1_000_000,
            monotonic_ns=lambda: 9_000_000_000)
        await advance(13, restarted)
        assert restarted.timelapse_deadlines[13] == 9_250_000_000
        assert restarted.results.calls == []
        stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()


async def test_finite_cancel_ends_activity_without_claiming_capture_or_stop_success(tmp_path):
    owned, runtime, advance, stopper, wall, mono = await _world(tmp_path)
    try:
        await advance(13, runtime)
        wall[0] += 1_250_000
        mono[0] += 1_250_000_000
        await advance(13, runtime)
        assert owned.connection.execute('SELECT status FROM actions WHERE id=13').fetchone() == (6,)
        assert owned.connection.execute('SELECT activity_state,occupancy_state,completion_basis,capture_json FROM device_activities WHERE id=13').fetchone() == (3, 2, 1, None)
        assert owned.connection.execute("SELECT status,attempts_used FROM operation_runs WHERE responsibility_key='stop/13'").fetchone() == (4, 1)
        from camctl.cancellation.settlement import TargetSettlement
        outcome = TargetSettlement(owned, None, None, lambda _: '', lambda: wall[0])._settle_capture(13, 6)
        assert outcome.complete and outcome.failed
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs WHERE source_action_id=13').fetchone() == (1,)
        ended = owned.connection.execute("SELECT body_json FROM history_events WHERE event_type=13 AND json_extract(body_json,'$.reason')=4").fetchone()
        assert ended is not None
        evidence = json.loads(ended[0])['evidence']['cancel_completion']
        assert evidence['policy'] == 'finite_device_end'
        assert evidence['sent_at_us'] == _NOW
        assert evidence['expected_check_at_us'] == _NOW+1_250_000
        assert evidence['start_result_event_id'] < evidence['stop_result_event_id'] < evidence['wait_completed_event_id'] < evidence['result_page_event_id']
        stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()


async def _finish_cancel(owned, wall):
    from camctl.cancellation.models import FinishCancelAction
    from camctl.cancellation.service import ApplyCancel, CancellationRuntime, apply_cancel
    from camctl.cancellation.settlement import TargetSettlement
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.cancellation import CancellationRepository
    repository = CancellationRepository()
    origin, = owned.connection.execute('SELECT id FROM actions WHERE type=6').fetchone()
    items = tuple(row[0] for row in owned.connection.execute('SELECT id FROM cancel_items WHERE action_id=?', (origin,)))
    settlement = TargetSettlement(owned, None, repository, lambda _: '', lambda: wall[0])
    await apply_cancel(ApplyCancel(origin, items), CancellationRuntime(owned, repository, settlement, lambda: wall[0]))
    result = repository.finish_cancel_action(FinishCancelAction(origin, wall[0]), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert owned.connection.execute('SELECT status FROM actions WHERE id=?', (origin,)).fetchone() == (4,)


async def test_pending_natural_cancel_counts_as_work_and_completed_cancel_has_no_active_stop(tmp_path):
    from camctl.bootstrap.application import query_work_facts
    owned, runtime, advance, stopper, wall, mono = await _world(tmp_path)
    try:
        await advance(13, runtime)
        before = query_work_facts(owned.connection)
        assert before.unfinished_actions == 2
        assert before.required_settlements == 0
        wall[0] += 1_250_000
        mono[0] += 1_250_000_000
        await advance(13, runtime)
        await _finish_cancel(owned, wall)
        after = query_work_facts(owned.connection)
        assert after.unfinished_actions == 0
        assert after.required_settlements == 0
        assert not after.residual_device_facts
    finally:
        owned.connection.close()


@pytest.mark.parametrize('file_case', ['empty', 'missing_kind', 'incomplete', 'zero'])
async def test_insufficient_natural_cancel_results_preserve_unknown_activity_and_residual_occupancy(tmp_path, file_case):
    from camctl.capture.results import FileKind
    from camctl.bootstrap.application import query_work_facts
    owned, runtime, advance, stopper, wall, mono = await _world(tmp_path)
    runtime.check_config = AttemptConfig(2, Decimal('1'), Decimal('0'))
    runtime.results.files_by_action = {13: {
        'empty': (), 'missing_kind': (_entry('photo', kind=FileKind.PHOTO),),
        'incomplete': (_entry('pending', complete=False),), 'zero': (_entry('zero', size=0),),
    }[file_case]}
    try:
        await advance(13, runtime)
        wall[0] += 1_250_000
        mono[0] += 1_250_000_000
        await advance(13, runtime)
        if file_case == 'incomplete':
            assert runtime.action(13)['status'] == 2
            await advance(13, runtime)
            await advance(13, runtime)
        assert owned.connection.execute('SELECT status FROM actions WHERE id=13').fetchone() == (6,)
        assert owned.connection.execute('SELECT activity_state,occupancy_state,completion_basis FROM device_activities WHERE id=13').fetchone() == (1, 1, 1)
        assert owned.connection.execute("SELECT COUNT(*) FROM history_events WHERE event_type=13 AND json_extract(body_json,'$.reason')=4").fetchone() == (0,)
        await _finish_cancel(owned, wall)
        facts = query_work_facts(owned.connection)
        assert facts.unfinished_actions == 0 and facts.required_settlements == 0
        assert facts.residual_device_facts
        stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()


@pytest.mark.parametrize('after_commit', [False, True])
async def test_finite_cancel_end_unknown_reuses_exact_request_and_history_before_releasing(tmp_path, monkeypatch, after_commit):
    import camctl.capture.handlers as handlers
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.models import DbOutcomeKind
    from .test_record_media_result_settlement import _TrackedCommitFailure
    from pathlib import Path
    from camctl.persistence.repositories.history import HistoryRepository
    owned, runtime, advance, stopper, wall, mono = await _world(tmp_path)
    original = handlers.save_finite_cancel_end
    saves, outcomes = [], []
    def save(request, key, current):
        saves.append((request, key))
        if len(saves) == 1:
            current = replace(current, connection=_TrackedCommitFailure(current.connection, after_commit))
        receipt = original(request, key, current)
        outcomes.append(receipt)
        return receipt
    monkeypatch.setattr(handlers, 'save_finite_cancel_end', save)
    try:
        await advance(13, runtime)
        wall[0] += 1_250_000
        mono[0] += 1_250_000_000
        with pytest.raises(ConsistencyError):
            await advance(13, runtime)
        assert outcomes[0].kind is DbOutcomeKind.UNKNOWN
        assert (runtime.pending_result_closes[13].request, runtime.pending_result_closes[13].key) == saves[0]
        assert owned.connection.execute('SELECT status FROM actions WHERE id=13').fetchone() == (2,)
        assert owned.connection.execute('SELECT occupancy_state FROM device_activities WHERE id=13').fetchone() == (1,)
        wall[0] += 500_000
        await advance(13, runtime)
        assert len(saves) == 2 and saves[1] == saves[0]
        assert outcomes[-1].kind is DbOutcomeKind.COMPLETED
        assert not runtime.pending_result_closes
        assert len(runtime.results.calls) == 1
        stopper.stop.assert_awaited_once()
        path = Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
        history = HistoryRepository(path)
        restored = history.restore_entity('action', 13, history.current_boundary())
        assert restored[('device_activities', 13)]['activity_state'] == 3
        assert restored[('device_activities', 13)]['occupancy_state'] == 2
        assert restored[('device_activities', 13)]['capture_json'] is None
    finally:
        owned.connection.close()


async def test_unconfirmed_stop_return_without_error_still_ends_stop_run_failed(tmp_path):
    owned, runtime, advance, stopper, wall, mono = await _world(tmp_path)
    stopper.stop.return_value = DeviceCallResult(observations=(), error=None)
    try:
        await advance(13, runtime)
        assert owned.connection.execute("SELECT status,attempts_used,retry_wait_required,error_json FROM operation_runs WHERE responsibility_key='stop/13'").fetchone()[:3] == (4, 1, 0)
        stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()


async def test_failed_result_budget_survives_rescheduling_and_finishes_with_residual(tmp_path):
    owned, runtime, advance, stopper, wall, mono = await _world(tmp_path)
    class FailedResults(_FiniteResults):
        async def list_files(self, action_id):
            self.calls.append(action_id)
            raise RuntimeError('device unavailable')
    runtime.results = FailedResults({})
    runtime.check_config = AttemptConfig(2, Decimal('1'), Decimal('0'))
    try:
        await advance(13, runtime)
        wall[0] += 1_250_000
        mono[0] += 1_250_000_000
        await advance(13, runtime)
        assert runtime.action(13)['status'] == 2
        runtime = replace(runtime, natural_wait_states={}, timelapse_deadlines={})
        await advance(13, runtime)
        await advance(13, runtime)
        assert runtime.action(13)['status'] == 6
        assert owned.connection.execute("SELECT status,attempts_used FROM operation_runs WHERE responsibility_key='results/13'").fetchone() == (6, 2)
        assert owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE id=13').fetchone() == (1, 1)
        assert runtime.results.calls == [13, 13]
        stopper.stop.assert_awaited_once()
        await _finish_cancel(owned, wall)
    finally:
        owned.connection.close()


async def test_interrupted_stop_recovers_original_unknown_once_then_waits_natural_due(tmp_path):
    from camctl.capture.recovery import RecoveryBoundary
    from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
    owned, runtime, advance, stopper, wall, mono = await _world(tmp_path)
    stopper.stop.side_effect = RuntimeError('simulated interrupted call')
    try:
        with pytest.raises(RuntimeError):
            await advance(13, runtime)
        assert owned.connection.execute("SELECT status,attempts_used FROM operation_runs WHERE responsibility_key='stop/13'").fetchone() == (2, 1)
        recovery = EvidenceRegistry((EvidenceContract('adb_foreground_recovery', 1, 'stop', frozenset()),))
        runtime = replace(runtime, natural_wait_states={}, timelapse_deadlines={},
            recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()[0],
            recovery_evidence_for=lambda binding, operation: recovery if operation == 'stop' else None)
        await advance(13, runtime)
        assert owned.connection.execute("SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE responsibility_key='stop/13'").fetchone() == (4, 1, 0)
        assert runtime.action(13)['status'] == 2 and runtime.results.calls == []
        stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()


async def test_missing_sent_recovers_old_start_then_finishes_with_unknown_occupancy(tmp_path):
    from camctl.capture.recovery import RecoveryBoundary
    from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
    owned = _environment(tmp_path, _TIMELAPSE)
    _freeze_finite_spec(owned)
    runtime = _runtime(owned, results=_FiniteResults({}),
        stop_config=AttemptConfig(3, Decimal('1'), Decimal('0')))
    runtime.wait_config = lambda action: CaptureWaitConfig(1000, 200, 50)
    runtime.grant(runtime.action(13))
    _apply_public_cancel(owned, 13, _NOW)
    recovery = EvidenceRegistry((EvidenceContract('adb_foreground_recovery', 1, 'control', frozenset()),))
    runtime.recovery_boundary = RecoveryBoundary.HOST_LOCAL_SETTLED
    runtime.recovery_max_event_id = owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()[0]
    runtime.recovery_evidence_for = lambda binding, operation: recovery if operation == 'control' else None
    stopper = create_autospec(StopDriver, instance=True)
    stopper.stop.return_value = DeviceCallResult.from_outcome(CallOutcome(
        status=AttemptStatus.UNKNOWN, effect=EffectState.UNKNOWN,
        error=ErrorValue('device_error','device'),
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue('stop_returned', 1, {}))))
    runtime.stopper = stopper
    try:
        await capture_handler('camera_timelapse')(13, runtime)
        assert runtime.action(13)['status'] == 6
        assert owned.connection.execute('SELECT sent_at,wait_completed_event_id,activity_state,occupancy_state FROM device_activities WHERE id=13').fetchone() == (None, None, 1, 1)
        assert runtime.driver.calls == []
        stopper.stop.assert_awaited_once()
        await _finish_cancel(owned, [_NOW])
    finally:
        owned.connection.close()


async def test_confirmed_stop_uses_independent_wait_without_waiting_original_target(tmp_path):
    from .test_record_file_completion_wait import _result_port, _completed_result
    from .test_native_completion_context import _osv
    from camctl.cancellation.settlement import TargetSettlement
    owned, runtime, advance, stopper, wall, mono = await _world(tmp_path)
    stopper.stop.return_value = DeviceCallResult(
        observations=(DeviceObservation('stop_confirmed', 1, {'activity_id':'13'}),), error=None)
    seen = []
    async def read(request, batch):
        context = request.params['completion_context']
        assert context['kind'] == 'timelapse_stop'
        assert context['file_completion_wait_ms'] == 30000
        assert request.timeout_s == Decimal('40')
        completion = {'method':'stop_return_and_wait', 'version':1, 'activity_id':'13',
            'stop_result_event_id':context['stop_result_event_id'], 'required_wait_ms':30000,
            'observed_wait_ns':30_000_000_000, 'completed':True}
        seen.append(context)
        return _completed_result(request.ticket, context, entries=[_osv()], finalized=True, completion=completion)
    driver = _result_port(runtime, read)
    try:
        await advance(13, runtime)
        assert runtime.action(13)['status'] == 6
        assert owned.connection.execute('SELECT activity_state,occupancy_state,wait_completed_event_id,completion_basis,capture_json FROM device_activities WHERE id=13').fetchone() == (3, 2, None, 1, None)
        outcome = TargetSettlement(owned, None, None, lambda _: '', lambda: wall[0])._settle_capture(13, 6)
        assert outcome.complete and not outcome.failed
        assert len(seen) == 1
        driver.list_results.assert_awaited_once()
        stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()


async def test_finite_cancel_event_guard_independently_rejects_empty_completed_scan(tmp_path):
    import camctl.persistence.repositories.finite_cancel as finite
    from camctl.contracts.history_values import TransactionRange
    from camctl.history.validators import EventContext, EventValidationError
    from camctl.persistence.transaction import event_envelope, update_change
    from camctl.capture.handlers import _original_ticket
    owned, runtime, advance, stopper, wall, mono = await _world(tmp_path)
    runtime.results.files_by_action = {13: ()}
    try:
        await advance(13, runtime)
        wall[0] += 1_250_000
        mono[0] += 1_250_000_000
        await advance(13, runtime)
        ticket = _original_ticket(runtime, 'results/13', 'result')
        page = runtime.capture.read_last_result_page(ticket, owned)
        request = finite.FiniteCancelEndSave(13, page.ref, wall[0])
        state, action, activity = finite._load(owned, request)
        txn_id, event_id = owned.connection.execute('SELECT MAX(id)+1,last_event_id+1 FROM history_transactions').fetchone()
        event = event_envelope(event_id, txn_id, finite._TYPE, finite._REASON,
            (update_change('device_activities', 13, {'activity_state':1}, {'activity_state':3}),),
            wall[0], evidence={'cancel_completion': finite._evidence(state, request)})
        from camctl.persistence.event_reads import SqlEventReads
        owned.connection.execute('BEGIN')
        context = EventContext(TransactionRange(txn_id, event_id, event_id),
            {('device_activities',13):('action',13)}, state,
            current_reads=SqlEventReads(owned.connection, finite._read_scope(request)))
        with pytest.raises(EventValidationError):
            finite._guard(event, context)
        owned.connection.execute('ROLLBACK')
    finally:
        owned.connection.close()


async def test_finite_cancel_guard_reads_original_files_across_pages(tmp_path):
    from .test_record_file_completion_wait import _result_port
    from .test_result_page_history import _actual, _CURSOR
    from .test_native_completion_context import _osv
    owned, runtime, advance, stopper, wall, mono = await _world(tmp_path)
    seen = []
    async def read(request, batch):
        cursor = request.params.get('cursor')
        seen.append(cursor)
        return DeviceCallResult.from_outcome(_actual(request.ticket,
            entries=[_osv()] if cursor is None else [],
            cursor=None if cursor is None else _CURSOR,
            next_cursor=_CURSOR if cursor is None else None, finalized=cursor is not None))
    _result_port(runtime, read)
    try:
        await advance(13, runtime)
        wall[0] += 1_250_000
        mono[0] += 1_250_000_000
        await advance(13, runtime)
        assert runtime.action(13)['status'] == 6
        assert owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE id=13').fetchone() == (3, 2)
        assert len(seen) == 2
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs WHERE source_action_id=13').fetchone() == (1,)
        stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()


@pytest.mark.parametrize('latest', ['empty', 'missing_kind', 'zero', 'incomplete', 'unfinalized', 'error', 'sufficient'])
async def test_prior_complete_files_do_not_supply_current_insufficient_cancel_scan(tmp_path, latest):
    from .test_record_file_completion_wait import _result_port
    from .test_result_page_history import _actual
    from .test_native_completion_context import _osv
    owned, runtime, advance, stopper, wall, mono = await _world(tmp_path)
    runtime.check_config = AttemptConfig(2, Decimal('1'), Decimal('0'))
    calls = []
    async def read(request, batch):
        calls.append(request.ticket.attempt_id)
        if len(calls) == 1:
            return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()],
                error=ErrorValue('device_error','device')))
        entries = {'empty': [], 'missing_kind': [{**_osv(), 'identity':'/DCIM/photo.JPG',
            'locator':{'path':'/DCIM/photo.JPG'}, 'format_id':'jpeg','kind':'photo'}],
            'zero': [{**_osv(0), 'identity':'/DCIM/zero.OSV','locator':{'path':'/DCIM/zero.OSV'}}],
            'incomplete':[{**_osv(), 'complete':False, 'size_bytes':None}],
            'unfinalized':[_osv()], 'error':[], 'sufficient':[_osv()]}[latest]
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=entries,
            finalized=latest != 'unfinalized', error=ErrorValue('device_error','device') if latest == 'error' else None))
    _result_port(runtime, read)
    try:
        await advance(13, runtime)
        wall[0] += 1_250_000
        mono[0] += 1_250_000_000
        await advance(13, runtime)
        assert runtime.action(13)['status'] == 2
        await advance(13, runtime)
        if latest in ('incomplete', 'unfinalized', 'error'):
            assert runtime.action(13)['status'] == 2
            await advance(13, runtime)
        assert runtime.action(13)['status'] == 6
        assert not runtime.pending_result_closes
        assert owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE id=13').fetchone() == ((3, 2) if latest == 'sufficient' else (1, 1))
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs o JOIN device_files f ON f.id=o.device_file_id WHERE o.source_action_id=13 AND f.size_bytes=50 AND json_extract(f.identity_key,'$[2]')='/DCIM/a.OSV'").fetchone() == (1,)
        stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()


async def test_new_finite_cancel_waits_original_startup_then_preserves_dual_terminal_and_history(tmp_path):
    from pathlib import Path
    from camctl.persistence.repositories.history import HistoryRepository
    owned, runtime, advance, stopper, wall, mono = await _world(tmp_path, startup_margin_ms=1000)
    try:
        await advance(13, runtime)
        wall[0] += 1250000
        mono[0] += 1250000000
        await advance(13, runtime)
        assert runtime.results.calls == []
        assert runtime.action(13)['status'] == 2
        wall[0] += 1000000
        mono[0] += 1000000000
        await advance(13, runtime)
        assert runtime.action(13)['status'] == 6
        await _finish_cancel(owned, wall)
        ended = owned.connection.execute("SELECT body_json FROM history_events WHERE event_type=13 AND json_extract(body_json,'$.reason')=4").fetchone()
        evidence = json.loads(ended[0])['evidence']['cancel_completion']
        assert evidence['startup_margin_ms'] == 1000
        assert evidence['expected_check_at_us'] == _NOW+2250000
        assert owned.connection.execute('SELECT activity_state,occupancy_state,completion_basis,capture_json FROM device_activities WHERE id=13').fetchone() == (3, 2, 1, None)
        assert owned.connection.execute("SELECT status,attempts_used FROM operation_runs WHERE responsibility_key='stop/13'").fetchone() == (4, 1)
        path = Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
        history = HistoryRepository(path)
        restored = history.restore_entity('action', 13, history.current_boundary())
        assert restored[('actions', 13)]['status'] == 6
        stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()


@pytest.mark.parametrize('startup', [1000, 2000, None])
async def test_finite_cancel_guard_requires_same_frozen_startup_margin(tmp_path, startup):
    import camctl.persistence.repositories.finite_cancel as finite
    from camctl.contracts.history_values import TransactionRange
    from camctl.history.validators import EventContext, EventValidationError
    from camctl.persistence.transaction import event_envelope, update_change
    from camctl.capture.handlers import _original_ticket
    from camctl.persistence.event_reads import SqlEventReads
    owned, runtime, advance, stopper, wall, mono = await _world(tmp_path, startup_margin_ms=1000)
    try:
        await advance(13, runtime)
        wall[0] += 2250000
        mono[0] += 2250000000
        await advance(13, runtime)
        ticket = _original_ticket(runtime, 'results/13', 'result')
        page = runtime.capture.read_last_result_page(ticket, owned)
        request = finite.FiniteCancelEndSave(13, page.ref, wall[0])
        state, action, activity = finite._load(owned, request)
        txn_id, event_id = owned.connection.execute('SELECT MAX(id)+1,last_event_id+1 FROM history_transactions').fetchone()
        evidence = finite._evidence(state, request)
        if startup is None:
            del evidence['startup_margin_ms']
        else:
            evidence['startup_margin_ms'] = startup
        event = event_envelope(event_id, txn_id, finite._TYPE, finite._REASON,
            (update_change('device_activities', 13, {'activity_state':1}, {'activity_state':3}),),
            wall[0], evidence={'cancel_completion': evidence})
        owned.connection.execute('BEGIN')
        context = EventContext(TransactionRange(txn_id, event_id, event_id),
            {('device_activities',13):('action',13)}, state,
            current_reads=SqlEventReads(owned.connection, finite._read_scope(request)))
        if startup == 1000:
            finite._guard(event, context)
        else:
            with pytest.raises(EventValidationError):
                finite._guard(event, context)
        owned.connection.execute('ROLLBACK')
    finally:
        owned.connection.close()

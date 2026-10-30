"""有限设备任务取消后的原责任与自然期限。"""
from dataclasses import replace
from decimal import Decimal
import json

import pytest

from camctl.devices.ports import DeviceCallResult
from camctl.operations.models import AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue, Settlement, SettlementBasis

from .test_capture_contract import _NOW
from camctl.devices.evidence import DeviceObservation
from ..support.capture_cancellation_cases import cancellation_world

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize('unknown', [False, True])
async def test_first_failed_stop_transfers_responsibility_without_retry_or_early_listing(tmp_path, capture_resources, unknown):
    world = await cancellation_world(tmp_path, capture_resources)
    if unknown:
        async def stop(request):
            return DeviceCallResult.from_outcome(CallOutcome(status=AttemptStatus.UNKNOWN,
                effect=EffectState.UNKNOWN, error=ErrorValue('device_error', 'device'),
                settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue('stop_returned', 1, {}))))
        world.ports.stop_script = stop
    owned, identity = world.owned, world.action_id
    try:
        await world.advance()
        assert owned.connection.execute("SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE responsibility_key=?", (f'stop/{identity}',)).fetchone() == (4, 1, 0)
        assert owned.connection.execute('SELECT sent_at FROM device_activities WHERE id=?', (world.activity_id,)).fetchone() == (_NOW,)
        assert world.ports.pages == []
        assert owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE id=?', (world.activity_id,)).fetchone() == (1, 1)
        await world.advance()
        assert len(world.ports.stops) == 1
        assert [request.operation for request in world.ports.controls] == ['start_timelapse']
        assert world.ports.pages == []
        assert owned.connection.execute('SELECT status FROM actions WHERE id=?', (identity,)).fetchone() == (2,)
        stop_txn = owned.connection.execute("SELECT e.transaction_id FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id JOIN history_events e ON e.id=a.result_event_id WHERE r.responsibility_key=?", (f'stop/{identity}',)).fetchone()[0]
        failed_txn = owned.connection.execute("SELECT e.transaction_id FROM operation_runs r JOIN history_events e ON e.event_type=10 AND json_extract(e.body_json,'$.rows[0].id')=r.id WHERE r.responsibility_key=? ORDER BY e.id DESC LIMIT 1", (f'stop/{identity}',)).fetchone()[0]
        assert stop_txn == failed_txn
    finally:
        await world.close()


async def test_restarted_finite_cancel_waits_only_remaining_original_deadline(tmp_path, capture_resources):
    world = await cancellation_world(tmp_path, capture_resources)
    try:
        await world.advance()
        world.wall[0], world.mono[0] = _NOW + 1_000_000, 9_000_000_000
        await world.reopen()
        await world.advance()
        assert world.ports.pages == []
        world.wall[0] += 249_999
        world.mono[0] += 249_999_000
        await world.advance()
        assert world.ports.pages == []
        world.wall[0] += 1
        world.mono[0] += 1000
        await world.advance()
        assert len(world.ports.pages) == len(world.ports.stops) == 1
    finally:
        await world.close()


async def test_finite_cancel_ends_activity_without_claiming_capture_or_stop_success(tmp_path, capture_resources):
    world = await cancellation_world(tmp_path, capture_resources)
    try:
        await world.advance()
        world.elapse(1_250_000)
        await world.advance()
        assert world.owned.connection.execute('SELECT status FROM actions WHERE id=?', (world.action_id,)).fetchone() == (6,)
        assert world.owned.connection.execute('SELECT activity_state,occupancy_state,completion_basis,capture_json FROM device_activities WHERE id=?', (world.activity_id,)).fetchone() == (3, 2, 1, None)
        assert world.owned.connection.execute('SELECT status,attempts_used FROM operation_runs WHERE responsibility_key=?', (f'stop/{world.action_id}',)).fetchone() == (4, 1)
        await world.settle_cancel()
        assert world.owned.connection.execute('SELECT status FROM actions WHERE type=6').fetchone() == (4,)
        assert world.owned.connection.execute('SELECT COUNT(*) FROM outputs WHERE source_action_id=?', (world.action_id,)).fetchone() == (1,)
        ended = world.owned.connection.execute("SELECT body_json FROM history_events WHERE event_type=13 AND json_extract(body_json,'$.reason')=4").fetchone()
        assert ended is not None
        evidence = json.loads(ended[0])['evidence']['cancel_completion']
        assert evidence['policy'] == 'finite_device_end'
        assert evidence['sent_at_us'] == _NOW
        assert evidence['expected_check_at_us'] == _NOW + 1_250_000
        assert evidence['start_result_event_id'] < evidence['stop_result_event_id'] < evidence['wait_completed_event_id'] < evidence['result_page_event_id']
        assert len(world.ports.stops) == 1
    finally:
        await world.close()


async def test_pending_natural_cancel_counts_as_work_and_completed_cancel_has_no_active_stop(tmp_path, capture_resources):
    from camctl.bootstrap.application import query_work_facts
    world = await cancellation_world(tmp_path, capture_resources)
    try:
        await world.advance()
        before = query_work_facts(world.owned.connection)
        assert before.unfinished_actions == 2 and before.required_settlements == 0
        world.elapse(1_250_000)
        await world.advance()
        await world.settle_cancel()
        after = query_work_facts(world.owned.connection)
        assert after.unfinished_actions == after.required_settlements == 0
        assert not after.residual_device_facts
    finally:
        await world.close()


@pytest.mark.parametrize('file_case', ['empty', 'missing_kind', 'incomplete', 'zero'])
async def test_insufficient_natural_cancel_results_preserve_unknown_activity_and_residual_occupancy(tmp_path, capture_resources, file_case):
    from camctl.bootstrap.application import query_work_facts
    from .test_result_page_history import _actual
    from .test_native_completion_context import _osv
    world = await cancellation_world(tmp_path, capture_resources, check_attempts=2, check_timeout_s=Decimal('1'), check_retry_s=Decimal('0'))
    async def read(request, batch):
        entries = {'empty': [], 'missing_kind': [{**_osv(), 'kind': 'photo', 'format_id': 'jpeg'}],
            'incomplete': [{**_osv(), 'complete': False, 'size_bytes': None}], 'zero': [_osv(0)]}[file_case]
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=entries, finalized=True))
    world.ports.result_script = read
    try:
        await world.advance()
        world.elapse(1_250_000)
        await world.advance()
        if file_case == 'incomplete':
            assert world.owned.connection.execute('SELECT status FROM actions WHERE id=?', (world.action_id,)).fetchone() == (2,)
            await world.advance()
            await world.advance()
        assert world.owned.connection.execute('SELECT status FROM actions WHERE id=?', (world.action_id,)).fetchone() == (6,)
        assert world.owned.connection.execute('SELECT activity_state,occupancy_state,completion_basis FROM device_activities WHERE id=?', (world.activity_id,)).fetchone() == (1, 1, 1)
        assert world.owned.connection.execute("SELECT COUNT(*) FROM history_events WHERE event_type=13 AND json_extract(body_json,'$.reason')=4").fetchone() == (0,)
        await world.settle_cancel()
        facts = query_work_facts(world.owned.connection)
        assert facts.unfinished_actions == facts.required_settlements == 0 and facts.residual_device_facts
        assert len(world.ports.stops) == 1
    finally:
        await world.close()


@pytest.mark.parametrize('after_commit', [False, True])
async def test_finite_cancel_end_unknown_reuses_exact_request_and_history_before_releasing(tmp_path, capture_resources, monkeypatch, after_commit):
    import camctl.capture.handlers as handlers
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.history import HistoryRepository
    from ..support.bootstrap_consumers import TransactionFault
    world = await cancellation_world(tmp_path, capture_resources)
    original, saves, outcomes = handlers.save_finite_cancel_end, [], []
    def save(request, key, current):
        saves.append((request, key))
        if len(saves) == 1:
            fault = TransactionFault(current.connection, after_commit=after_commit)
            current = replace(current, connection=fault)
        receipt = original(request, key, current)
        outcomes.append(receipt)
        return receipt
    monkeypatch.setattr(handlers, 'save_finite_cancel_end', save)
    try:
        await world.advance()
        world.elapse(1_250_000)
        with pytest.raises(ConsistencyError):
            await world.advance()
        assert outcomes[0].kind is DbOutcomeKind.UNKNOWN
        assert world.owned.connection.execute('SELECT status FROM actions WHERE id=?', (world.action_id,)).fetchone() == (2,)
        assert world.owned.connection.execute('SELECT occupancy_state FROM device_activities WHERE id=?', (world.activity_id,)).fetchone() == (1,)
        monotonic_before_reopen = world.mono[0]
        world.wall[0] += 500_000
        await world.reopen(cold=False)
        assert world.mono[0] == monotonic_before_reopen
        await world.advance()
        assert world.mono[0] == monotonic_before_reopen
        assert len(saves) == 2 and saves[1] == saves[0]
        assert outcomes[-1].kind is DbOutcomeKind.COMPLETED
        assert world.owned.connection.execute(
            'SELECT COUNT(*) FROM history_transactions WHERE operation_key=?',
            (str(saves[0][1]),)).fetchone() == (1,)
        assert len(world.ports.pages) == len(world.ports.stops) == 1
        history = HistoryRepository(world.path)
        restored = history.restore_entity('action', world.action_id, history.current_boundary())
        assert restored[('device_activities', world.activity_id)]['activity_state'] == 3
        assert restored[('device_activities', world.activity_id)]['occupancy_state'] == 2
        assert restored[('device_activities', world.activity_id)]['capture_json'] is None
    finally:
        await world.close()


async def test_unconfirmed_stop_return_without_error_still_ends_stop_run_failed(tmp_path, capture_resources):
    world = await cancellation_world(tmp_path, capture_resources)
    async def stop(request):
        return DeviceCallResult(observations=(), error=None)
    world.ports.stop_script = stop
    try:
        await world.advance()
        assert world.owned.connection.execute('SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE responsibility_key=?', (f'stop/{world.action_id}',)).fetchone() == (4, 1, 0)
        assert len(world.ports.stops) == 1
    finally:
        await world.close()


async def test_failed_result_budget_survives_rescheduling_and_finishes_with_residual(tmp_path, capture_resources):
    from .test_result_page_history import _actual
    world = await cancellation_world(tmp_path, capture_resources, check_attempts=2, check_timeout_s=Decimal('1'), check_retry_s=Decimal('0'))
    async def read(request, batch):
        return DeviceCallResult.from_outcome(_actual(request.ticket, error=ErrorValue('device_error', 'device')))
    world.ports.result_script = read
    try:
        await world.advance()
        world.elapse(1_250_000)
        await world.advance()
        assert world.owned.connection.execute('SELECT status FROM actions WHERE id=?', (world.action_id,)).fetchone() == (2,)
        await world.reopen()
        await world.advance()
        await world.advance()
        assert world.owned.connection.execute('SELECT status FROM actions WHERE id=?', (world.action_id,)).fetchone() == (6,)
        assert world.owned.connection.execute('SELECT status,attempts_used FROM operation_runs WHERE responsibility_key=?', (f'results/{world.activity_id}',)).fetchone() == (6, 2)
        assert world.owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE id=?', (world.activity_id,)).fetchone() == (1, 1)
        assert len(world.ports.pages) == 2 and len(world.ports.stops) == 1
        await world.settle_cancel()
    finally:
        await world.close()


async def test_interrupted_stop_recovers_original_unknown_once_then_waits_natural_due(tmp_path, capture_resources):
    from camctl.capture.recovery import RecoveryBoundary
    world = await cancellation_world(tmp_path, capture_resources)
    async def stop(request):
        raise RuntimeError('实际停止端口中断')
    world.ports.stop_script = stop
    try:
        with pytest.raises(RuntimeError, match='实际停止端口中断'):
            await world.advance()
        assert world.owned.connection.execute('SELECT status,attempts_used FROM operation_runs WHERE responsibility_key=?', (f'stop/{world.action_id}',)).fetchone() == (2, 1)
        horizon = world.owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()[0]
        world.factory_options.update(recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=lambda: horizon)
        await world.reopen()
        await world.advance()
        assert world.owned.connection.execute('SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE responsibility_key=?', (f'stop/{world.action_id}',)).fetchone() == (4, 1, 0)
        assert world.owned.connection.execute('SELECT status FROM actions WHERE id=?', (world.action_id,)).fetchone() == (2,)
        assert world.ports.pages == [] and len(world.ports.stops) == 1
    finally:
        await world.close()


async def test_missing_sent_recovers_old_start_then_finishes_with_unknown_occupancy(tmp_path, capture_resources, monkeypatch):
    from camctl.capture.recovery import RecoveryBoundary
    from camctl.persistence.repositories.scheduling import SchedulingRepository
    from camctl.persistence.models import DbOutcomeKind
    world = await cancellation_world(tmp_path, capture_resources, start=False, canceled=False)
    from .test_result_page_history import _actual
    async def read(request, batch):
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[], finalized=True))
    world.ports.result_script = read
    class Granted(Exception):
        pass
    original = SchedulingRepository.grant_start
    def grant(repository, request, key, owned):
        receipt = original(repository, request, key, owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        return replace(receipt, handoff_error=Granted())
    try:
        with monkeypatch.context() as boundary:
            boundary.setattr(SchedulingRepository, 'grant_start', grant)
            with pytest.raises(Granted):
                await world.advance()
        await world.cancel()
        horizon = world.owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()[0]
        world.factory_options.update(recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=lambda: horizon)
        await world.reopen()
        await world.advance()
        assert world.owned.connection.execute('SELECT status FROM actions WHERE id=?', (world.action_id,)).fetchone() == (6,)
        assert world.owned.connection.execute('SELECT sent_at,wait_completed_event_id,activity_state,occupancy_state FROM device_activities WHERE id=?', (world.activity_id,)).fetchone() == (None, None, 1, 1)
        assert world.ports.controls == [] and len(world.ports.stops) == 1
        await world.settle_cancel()
    finally:
        await world.close()


async def test_confirmed_stop_uses_independent_wait_without_waiting_original_target(tmp_path, capture_resources):
    from .test_record_file_completion_wait import _completed_result
    from .test_native_completion_context import _osv
    world = await cancellation_world(tmp_path, capture_resources)
    async def stop(request):
        return DeviceCallResult((DeviceObservation('stop_confirmed', 1, {'activity_id': request.ticket.target_id}),), None)
    async def read(request, batch):
        context = request.params['completion_context']
        assert context['kind'] == 'timelapse_stop' and context['file_completion_wait_ms'] == 30000
        assert request.timeout_s == Decimal('40')
        completion = {'method': 'stop_return_and_wait', 'version': 1, 'activity_id': context['activity_id'],
            'stop_result_event_id': context['stop_result_event_id'], 'required_wait_ms': 30000,
            'observed_wait_ns': 30_000_000_000, 'completed': True}
        return _completed_result(request.ticket, context, entries=[_osv()], finalized=True, completion=completion)
    world.ports.stop_script, world.ports.result_script = stop, read
    try:
        await world.advance()
        assert world.owned.connection.execute('SELECT status FROM actions WHERE id=?', (world.action_id,)).fetchone() == (6,)
        assert world.owned.connection.execute('SELECT activity_state,occupancy_state,wait_completed_event_id,completion_basis,capture_json FROM device_activities WHERE id=?', (world.activity_id,)).fetchone() == (3, 2, None, 1, None)
        await world.settle_cancel()
        assert world.owned.connection.execute('SELECT status FROM actions WHERE type=6').fetchone() == (3,)
        assert len(world.ports.pages) == len(world.ports.stops) == 1
    finally:
        await world.close()


async def test_finite_cancel_event_guard_independently_rejects_empty_completed_scan(tmp_path, capture_resources):
    from camctl.history.validators import EventValidationError, validate_event
    from ..support.capture_cancellation_cases import finite_end_proposal
    from .test_result_page_history import _actual
    world = await cancellation_world(tmp_path, capture_resources)
    async def read(request, batch):
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[], finalized=True))
    world.ports.result_script = read
    try:
        await world.advance()
        world.elapse(1_250_000)
        await world.advance()
        event, context = finite_end_proposal(world)
        before = tuple(world.owned.connection.iterdump())
        world.owned.connection.execute('BEGIN')
        try:
            with pytest.raises(EventValidationError, match='文件|产物|扫描'):
                validate_event(event, context)
        finally:
            world.owned.connection.execute('ROLLBACK')
        assert tuple(world.owned.connection.iterdump()) == before
    finally:
        await world.close()


async def test_finite_cancel_guard_reads_original_files_across_pages(tmp_path, capture_resources):
    from .test_result_page_history import _actual
    from camctl.devices.directory import DirectoryCursor
    from .test_native_completion_context import _osv
    world = await cancellation_world(tmp_path, capture_resources, output_scope={"directories": ["/DCIM"]})
    seen = []
    async def read(request, batch):
        cursor = request.params.get('cursor')
        seen.append(cursor)
        next_page = DirectoryCursor(request.binding, tuple(request.params['output_scope']['directories']), 0, '/DCIM/a.OSV')
        return DeviceCallResult.from_outcome(_actual(request.ticket,
            entries=[_osv()] if cursor is None else [], cursor=None if cursor is None else next_page,
            next_cursor=next_page if cursor is None else None, finalized=cursor is not None))
    world.ports.result_script = read
    try:
        await world.advance()
        world.elapse(1_250_000)
        await world.advance()
        assert world.owned.connection.execute('SELECT status FROM actions WHERE id=?', (world.action_id,)).fetchone() == (6,)
        assert world.owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE id=?', (world.activity_id,)).fetchone() == (3, 2)
        assert len(seen) == 2
        assert world.owned.connection.execute('SELECT COUNT(*) FROM outputs WHERE source_action_id=?', (world.action_id,)).fetchone() == (1,)
        assert len(world.ports.stops) == 1
    finally:
        await world.close()


@pytest.mark.parametrize('latest', ['empty', 'missing_kind', 'zero', 'incomplete', 'unfinalized', 'error', 'sufficient'])
async def test_prior_complete_files_do_not_supply_current_insufficient_cancel_scan(tmp_path, capture_resources, latest):
    from .test_result_page_history import _actual
    from .test_native_completion_context import _osv
    world = await cancellation_world(tmp_path, capture_resources, check_attempts=2, check_timeout_s=Decimal('1'), check_retry_s=Decimal('0'))
    calls = []
    async def read(request, batch):
        calls.append(request.ticket.attempt_id)
        if len(calls) == 1:
            return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv()], error=ErrorValue('device_error', 'device')))
        entries = {'empty': [], 'missing_kind': [{**_osv(), 'identity': '/DCIM/photo.JPG',
            'locator': {'path': '/DCIM/photo.JPG'}, 'format_id': 'jpeg', 'kind': 'photo'}],
            'zero': [{**_osv(0), 'identity': '/DCIM/zero.OSV', 'locator': {'path': '/DCIM/zero.OSV'}}],
            'incomplete': [{**_osv(), 'complete': False, 'size_bytes': None}],
            'unfinalized': [_osv()], 'error': [], 'sufficient': [_osv()]}[latest]
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=entries,
            finalized=latest != 'unfinalized', error=ErrorValue('device_error', 'device') if latest == 'error' else None))
    world.ports.result_script = read
    try:
        await world.advance()
        world.elapse(1_250_000)
        await world.advance()
        assert world.owned.connection.execute('SELECT status FROM actions WHERE id=?', (world.action_id,)).fetchone() == (2,)
        await world.advance()
        if latest in ('incomplete', 'unfinalized', 'error'):
            assert world.owned.connection.execute('SELECT status FROM actions WHERE id=?', (world.action_id,)).fetchone() == (2,)
            await world.advance()
        assert world.owned.connection.execute('SELECT status FROM actions WHERE id=?', (world.action_id,)).fetchone() == (6,)
        assert world.owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE id=?', (world.activity_id,)).fetchone() == ((3, 2) if latest == 'sufficient' else (1, 1))
        assert world.owned.connection.execute("SELECT COUNT(*) FROM outputs o JOIN device_files f ON f.id=o.device_file_id WHERE o.source_action_id=? AND f.size_bytes=50 AND json_extract(f.identity_key,'$[2]')='/DCIM/a.OSV'", (world.action_id,)).fetchone() == (1,)
        assert len(world.ports.stops) == 1 and len(calls) == 2
        before = tuple(world.owned.connection.iterdump())
        await world.advance()
        assert tuple(world.owned.connection.iterdump()) == before and len(calls) == 2
    finally:
        await world.close()


@pytest.mark.parametrize('margin_ms,wait_us', [(0, 1250000), (1000, 2250000)])
async def test_new_finite_cancel_waits_original_startup_then_preserves_dual_terminal_and_history(tmp_path, capture_resources, margin_ms, wait_us):
    from camctl.persistence.repositories.history import HistoryRepository
    world = await cancellation_world(tmp_path, capture_resources, startup=margin_ms)
    try:
        await world.advance()
        world.elapse(wait_us - 1)
        await world.advance()
        assert world.ports.pages == []
        assert world.owned.connection.execute('SELECT status FROM actions WHERE id=?', (world.action_id,)).fetchone() == (2,)
        world.elapse(1)
        await world.advance()
        await world.settle_cancel()
        assert world.owned.connection.execute('SELECT status FROM actions WHERE id=?', (world.action_id,)).fetchone() == (6,)
        assert world.owned.connection.execute('SELECT status FROM actions WHERE type=6').fetchone() == (4,)
        ended = world.owned.connection.execute("SELECT body_json FROM history_events WHERE event_type=13 AND json_extract(body_json,'$.reason')=4").fetchone()
        evidence = json.loads(ended[0])['evidence']['cancel_completion']
        assert evidence['startup_margin_ms'] == margin_ms
        assert evidence['expected_check_at_us'] == _NOW + wait_us
        assert world.owned.connection.execute('SELECT activity_state,occupancy_state,completion_basis,capture_json FROM device_activities WHERE id=?', (world.activity_id,)).fetchone() == (3, 2, 1, None)
        assert world.owned.connection.execute('SELECT status,attempts_used FROM operation_runs WHERE responsibility_key=?', (f'stop/{world.action_id}',)).fetchone() == (4, 1)
        history = HistoryRepository(world.path)
        restored = history.restore_entity('action', world.action_id, history.current_boundary())
        assert restored[('actions', world.action_id)]['status'] == 6
        assert len(world.ports.stops) == 1
    finally:
        await world.close()


@pytest.mark.parametrize('startup', [0, 1000, 2000, None])
async def test_finite_cancel_guard_requires_same_frozen_startup_margin(tmp_path, capture_resources, startup):
    from camctl.history.validators import EventValidationError, validate_event
    from ..support.capture_cancellation_cases import finite_end_proposal
    world = await cancellation_world(tmp_path, capture_resources, startup=1000)
    try:
        await world.advance()
        world.elapse(2_250_000)
        await world.advance()
        source, = world.owned.connection.execute("SELECT id FROM history_events WHERE event_type=13 AND json_extract(body_json,'$.reason')=4").fetchone()
        event, context = finite_end_proposal(world, before_event=source)
        value = dict(event.evidence['cancel_completion'])
        if startup is None:
            del value['startup_margin_ms']
        else:
            value['startup_margin_ms'] = startup
        event = replace(event, evidence={'cancel_completion': value})
        before = tuple(world.owned.connection.iterdump())
        world.owned.connection.execute('BEGIN')
        try:
            if startup == 1000:
                validate_event(event, context)
            else:
                with pytest.raises(EventValidationError):
                    validate_event(event, context)
        finally:
            world.owned.connection.execute('ROLLBACK')
        assert tuple(world.owned.connection.iterdump()) == before
    finally:
        await world.close()

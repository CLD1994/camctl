"""有限取消评审边界：当前长度与可选预览独立核实。"""
from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.contracts.values import new_operation_key
from camctl.history.validators import EventContext, EventValidationError
from camctl.persistence.models import DbOutcomeKind
from camctl.devices.ports import DeviceCallResult
import camctl.persistence.repositories.finite_cancel as finite

from .test_result_page_history import _actual
from .test_native_completion_context import _osv

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize('current_size,allowed', [(0,False),(None,False),(49,False),(50,True)])
@pytest.mark.parametrize('entry', ['qualification','save','guard'])
async def test_current_page_length_cannot_borrow_prior_complete_size(tmp_path, capture_resources, monkeypatch, current_size, allowed, entry):
    from ..support.capture_cancellation_cases import stored_result_case, store_result_end, finite_end_proposal
    from camctl.history.validators import validate_event
    world = await stored_result_case(tmp_path, capture_resources, monkeypatch,
        entries=[_osv(current_size)], prior_entries=[_osv(50)],
        check_attempts=2, check_timeout_s=Decimal('1'), check_retry_s=Decimal('0'))
    try:
        receipt = await store_result_end(world, finalized=True)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        request = finite.FiniteCancelEndSave(world.action_id, world.result_page.ref, world.wall[0])
        if entry == 'qualification':
            assert finite.read_finite_cancel_assessment(request, world.owned).is_complete is allowed
        elif entry == 'save':
            receipt = finite.save_finite_cancel_end(request, new_operation_key(), world.owned)
            assert (receipt.kind is DbOutcomeKind.COMPLETED) is allowed, receipt.error
        else:
            event, context = finite_end_proposal(world)
            before = tuple(world.owned.connection.iterdump())
            world.owned.connection.execute('BEGIN')
            try:
                if allowed:
                    validate_event(event, context)
                else:
                    with pytest.raises(EventValidationError, match='文件|产物|扫描'):
                        validate_event(event, context)
            finally:
                world.owned.connection.execute('ROLLBACK')
            assert tuple(world.owned.connection.iterdump()) == before
    finally:
        await world.close()


async def test_optional_preview_pointing_to_baseline_original_does_not_block_valid_current_osv(tmp_path, capture_resources):
    from ..support.capture_cancellation_cases import cancellation_world
    world = await cancellation_world(tmp_path, capture_resources)
    async def read(request, batch):
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv(), _baseline_preview()], finalized=True))
    world.ports.result_script = read
    try:
        await world.advance()
        world.elapse(1_250_000)
        await world.advance()
        assert world.owned.connection.execute('SELECT status FROM actions WHERE id=?', (world.action_id,)).fetchone() == (6,)
        assert world.owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE id=?', (world.activity_id,)).fetchone() == (3, 2)
        assert world.owned.connection.execute('SELECT COUNT(*) FROM outputs WHERE kind=2').fetchone() == (0,)
    finally:
        await world.close()


def _baseline_preview():
    return {'identity':'/DCIM/baseline.LRF','locator':{'path':'/DCIM/baseline.LRF'},
            'complete':True,'size_bytes':20,'kind':'video','format_id':'lrf',
            'paired_identity':'/DCIM/baseline.OSV','original_name':'baseline.LRF','media_type':None}


async def test_normal_record_set_finalization_allows_only_explicit_optional_preview_discovery(tmp_path, capture_resources):
    from ..support.capture_cancellation_cases import cancellation_world
    from .test_record_file_completion_wait import _completed_result
    from camctl.devices.evidence import DeviceObservation
    world = await cancellation_world(tmp_path, capture_resources, handler='camera_record', canceled=False,
        max_delay_ms=5000, check_timeout_s=Decimal('1.25'), check_retry_s=Decimal('2'),
        stop_timeout_s=Decimal('10'), stop_retry_s=Decimal('3'), record_wait_s=30,
        record_rules=({'kind': 'video', 'format_id': 'osv', 'min_count': 1, 'exact_count': 1, 'require_pairing': False, 'min_size_bytes': 1},))
    async def stop(request):
        return DeviceCallResult((DeviceObservation('stop_confirmed', 1, {'activity_id': request.ticket.target_id}),), None)
    async def read(request, batch):
        context = request.params['completion_context']
        completion = {'method': 'stop_return_and_wait', 'version': 1, 'activity_id': context['activity_id'],
            'stop_result_event_id': context['stop_result_event_id'], 'required_wait_ms': 30000,
            'observed_wait_ns': 30_000_000_000, 'completed': True}
        return _completed_result(request.ticket, context, entries=[_osv(), _baseline_preview()], finalized=True, completion=completion)
    world.ports.stop_script, world.ports.result_script = stop, read
    try:
        world.elapse(60_000_000)
        await world.advance()
        assert world.owned.connection.execute('SELECT status FROM actions WHERE id=?', (world.action_id,)).fetchone() == (3,)
        assert world.owned.connection.execute('SELECT COUNT(*) FROM outputs WHERE kind=2').fetchone() == (0,)
    finally:
        await world.close()


def _preview_note(state):
    # 与正式reader相同：未配对、零长和无法核实预览只保存观察，不进入候选原片页。
    return {'preview_identity':'/DCIM/a.LRF','original_identity':None,'state':state,
        'osv_count':None,'lrf_count':None,'size_bytes':0 if state=='zero_size' else None,
        'scan_complete':False,'observed_original_identities':[], 'observed_preview_identities':[]}


def _noted_result(ticket, entries, state):
    from camctl.operations.models import Settlement, SettlementBasis, EvidenceValue
    actual=_actual(ticket,entries=entries,finalized=True)
    return DeviceCallResult.from_outcome(replace(actual,settlement=Settlement(SettlementBasis.OBSERVED,
        EvidenceValue('results_returned',1,{'optional_preview_observations':[_preview_note(state)]}))))


@pytest.mark.parametrize('state', ['missing_pair','completion_unknown','zero_size','metadata_failed'])
@pytest.mark.parametrize('include_osv', [False,True])
async def test_optional_preview_observations_do_not_replace_or_block_required_osv(tmp_path, capture_resources, state, include_osv):
    from ..support.capture_cancellation_cases import cancellation_world
    world = await cancellation_world(tmp_path, capture_resources)
    async def read(request, batch):
        return _noted_result(request.ticket, [_osv()] if include_osv else [], state)
    world.ports.result_script = read
    try:
        await world.advance()
        world.elapse(1_250_000)
        await world.advance()
        assert world.owned.connection.execute('SELECT status FROM actions WHERE id=?', (world.action_id,)).fetchone() == (6,)
        assert world.owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE id=?', (world.activity_id,)).fetchone() == ((3, 2) if include_osv else (1, 1))
        assert world.owned.connection.execute('SELECT COUNT(*) FROM outputs WHERE kind=2').fetchone() == (0,)
    finally:
        await world.close()


@pytest.mark.parametrize('state', ['missing_pair','completion_unknown','zero_size','metadata_failed'])
@pytest.mark.parametrize('required_by', ['min','exact'])
@pytest.mark.parametrize('entry', ['qualification','save','guard'])
async def test_required_preview_gaps_remain_blocked_in_all_completion_entries(tmp_path, capture_resources, monkeypatch, state, required_by, entry):
    from ..support.capture_cancellation_cases import stored_result_case, store_result_end, finite_end_proposal
    from camctl.history.validators import validate_event
    rule = {'kind': 'video', 'format_id': 'lrf', 'min_count': 1 if required_by == 'min' else 0,
        'exact_count': 1 if required_by == 'exact' else None, 'require_pairing': True, 'min_size_bytes': 1}
    world = await stored_result_case(tmp_path, capture_resources, monkeypatch, entries=[_osv()],
        prior_entries=[_osv()], extra_rules=(rule,), notes=[_preview_note(state)])
    try:
        receipt = await store_result_end(world, finalized=True)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        from camctl.persistence.repositories.capture import CaptureRepository
        first_ticket, current_ticket = world.actual_result_calls
        first = CaptureRepository().read_last_result_page(first_ticket, world.owned)
        current = world.result_page
        assert first_ticket != current_ticket and current.ref.ticket == current_ticket
        assert first.page.outcome.error is not None and current.page.outcome.error is None
        assert current.page.scan_complete and current.page.set_finalized
        assert first.file_ids == current.file_ids and len(current.file_ids) == 1
        file_id = current.file_ids[0][1]
        assert world.owned.connection.execute(
            'SELECT source_action_id,completion_state,size_bytes FROM device_files WHERE id=?',
            (file_id,)).fetchone() == (world.action_id, 3, 50)
        assert world.owned.connection.execute(
            'SELECT attempts_used,max_attempts_used,timeout_s_json,retry_interval_s_json FROM operation_runs WHERE id=?',
            (current_ticket.run_id,)).fetchone() == (2, 3, '10', '3')
        request = finite.FiniteCancelEndSave(world.action_id, world.result_page.ref, world.wall[0])
        before = tuple(world.owned.connection.iterdump())
        if entry == 'qualification':
            assert not finite.read_finite_cancel_assessment(request, world.owned).is_complete
        elif entry == 'save':
            receipt = finite.save_finite_cancel_end(request, new_operation_key(), world.owned)
            assert receipt.kind is DbOutcomeKind.ROLLED_BACK, receipt.error
        else:
            event, context = finite_end_proposal(world)
            world.owned.connection.execute('BEGIN')
            try:
                with pytest.raises(EventValidationError, match='文件|产物|扫描'):
                    validate_event(event, context)
            finally:
                world.owned.connection.execute('ROLLBACK')
        assert tuple(world.owned.connection.iterdump()) == before
    finally:
        await world.close()


@pytest.mark.parametrize('case', ['optional','required_min','required_exact','unknown_original'])
@pytest.mark.parametrize('entry', ['save','guard','reuse'])
async def test_set_finalized_entries_share_optional_ownership_classification(tmp_path, capture_resources, monkeypatch, case, entry):
    from ..support.capture_cancellation_cases import stored_result_case, store_result_end, output_set_proposal
    from camctl.capture.result_pages import OutputSetFinalizationSave
    from camctl.history.validators import validate_event
    from camctl.persistence.repositories.capture import CaptureRepository
    extra = ()
    if case.startswith('required_'):
        extra = ({'kind': 'video', 'format_id': 'lrf', 'min_count': 1 if case == 'required_min' else 0,
            'exact_count': 1 if case == 'required_exact' else None, 'require_pairing': True, 'min_size_bytes': 1},)
    additional = [{**_osv(), 'identity': '/DCIM/unknown.OSV', 'locator': {'path': '/DCIM/unknown.OSV'}}] if case == 'unknown_original' else [_baseline_preview()]
    world = await stored_result_case(tmp_path, capture_resources, monkeypatch,
        entries=[_osv(), *additional], prior_entries=[_osv()], extra_rules=extra,
        check_attempts=2, check_timeout_s=Decimal('1'), check_retry_s=Decimal('0'))
    try:
        receipt = await store_result_end(world)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        request = OutputSetFinalizationSave(world.action_id, world.result_page.ref, world.result_page.occurred_at)
        allowed = case == 'optional'
        before = tuple(world.owned.connection.iterdump())
        if entry in ('save','reuse'):
            key = new_operation_key()
            receipt = CaptureRepository().finalize_output_set(request, key, world.owned)
            assert (receipt.kind is DbOutcomeKind.COMPLETED) is allowed, receipt.error
            if allowed and entry == 'reuse':
                saved = tuple(world.owned.connection.iterdump())
                assert CaptureRepository().finalize_output_set(request, key, world.owned).kind is DbOutcomeKind.COMPLETED
                assert tuple(world.owned.connection.iterdump()) == saved
            if not allowed:
                assert tuple(world.owned.connection.iterdump()) == before
        else:
            event, context = output_set_proposal(world)
            world.owned.connection.execute('BEGIN')
            try:
                if allowed:
                    validate_event(event, context)
                else:
                    with pytest.raises(EventValidationError):
                        validate_event(event, context)
            finally:
                world.owned.connection.execute('ROLLBACK')
            assert tuple(world.owned.connection.iterdump()) == before
    finally:
        await world.close()

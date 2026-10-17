"""有限取消评审边界：当前长度与可选预览独立核实。"""
from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.capture.handlers import _listing_round, _finish_listing_result
from camctl.contracts.history_values import TransactionRange
from camctl.contracts.values import new_operation_key
from camctl.history.validators import EventContext, EventValidationError
from camctl.operations.attempts import AttemptConfig, RunOutcome
from camctl.operations.models import ErrorValue
from camctl.persistence.event_reads import SqlEventReads
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.transaction import event_envelope, update_change
from camctl.devices.ports import DeviceCallResult
import camctl.persistence.repositories.finite_cancel as finite

from .test_finite_cancel import _world
from .test_record_file_completion_wait import _result_port
from .test_result_page_history import _actual
from .test_native_completion_context import _osv

pytestmark = pytest.mark.asyncio


async def _prior_and_current(tmp_path, current_size):
    owned, runtime, advance, stopper, wall, mono = await _world(tmp_path)
    runtime.check_config = AttemptConfig(2, Decimal('1'), Decimal('0'))
    calls = []
    async def read(request, batch):
        calls.append(request.ticket.attempt_id)
        return DeviceCallResult.from_outcome(_actual(request.ticket,
            entries=[_osv(50 if len(calls) == 1 else current_size)],
            error=ErrorValue('device_error','device') if len(calls)==1 else None,
            finalized=len(calls)>1))
    _result_port(runtime, read)
    await advance(13, runtime)
    wall[0] += 1_250_000
    mono[0] += 1_250_000_000
    await advance(13, runtime)
    listing = await _listing_round(runtime, 13)
    # 只保存当前实际页与结果；文件完成守卫正是独立结束guard需要防御的上游边界。
    _finish_listing_result(runtime, listing, end_run=RunOutcome.SUCCEEDED, finalize_output_set=True)
    return owned, runtime, finite.FiniteCancelEndSave(13, listing.entries.last_ref, wall[0])


@pytest.mark.parametrize('current_size,allowed', [(0,False),(None,False),(49,False),(50,True)])
@pytest.mark.parametrize('entry', ['qualification','save','guard'])
async def test_current_page_length_cannot_borrow_prior_complete_size(tmp_path, current_size, allowed, entry):
    owned, runtime, request = await _prior_and_current(tmp_path, current_size)
    try:
        if entry == 'qualification':
            assessment = finite.read_finite_cancel_assessment(request, owned)
            assert assessment.is_complete is allowed
        elif entry == 'save':
            outcome = finite.save_finite_cancel_end(request, new_operation_key(), owned)
            assert (outcome.kind is DbOutcomeKind.COMPLETED) is allowed
        else:
            state, action, activity = finite._load(owned, request)
            txn, last = owned.connection.execute('SELECT MAX(id)+1,last_event_id+1 FROM history_transactions').fetchone()
            event = event_envelope(last, txn, finite._TYPE, finite._REASON,
                (update_change('device_activities',13,{'activity_state':1},{'activity_state':3}),),
                request.occurred_at, evidence={'cancel_completion':finite._evidence(state,request)})
            owned.connection.execute('BEGIN')
            context = EventContext(TransactionRange(txn,last,last),
                {('device_activities',13):('action',13)},state,
                current_reads=SqlEventReads(owned.connection, finite._read_scope(request)))
            if allowed:
                finite._guard(event,context)
            else:
                with pytest.raises(EventValidationError):
                    finite._guard(event,context)
            owned.connection.execute('ROLLBACK')
    finally:
        owned.connection.close()


async def test_optional_preview_pointing_to_baseline_original_does_not_block_valid_current_osv(tmp_path):
    owned, runtime, advance, stopper, wall, mono = await _world(tmp_path)
    async def read(request, batch):
        return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_osv(),
            {'identity':'/DCIM/baseline.LRF','locator':{'path':'/DCIM/baseline.LRF'},
             'complete':True,'size_bytes':20,'kind':'video','format_id':'lrf',
             'paired_identity':'/DCIM/baseline.OSV','original_name':'baseline.LRF','media_type':None}], finalized=True))
    _result_port(runtime, read)
    try:
        await advance(13,runtime)
        wall[0] += 1_250_000
        mono[0] += 1_250_000_000
        await advance(13,runtime)
        assert runtime.action(13)['status'] == 6
        assert owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE id=13').fetchone() == (3,2)
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs WHERE kind=2').fetchone() == (0,)
    finally:
        owned.connection.close()


def _baseline_preview():
    return {'identity':'/DCIM/baseline.LRF','locator':{'path':'/DCIM/baseline.LRF'},
            'complete':True,'size_bytes':20,'kind':'video','format_id':'lrf',
            'paired_identity':'/DCIM/baseline.OSV','original_name':'baseline.LRF','media_type':None}


async def test_normal_record_set_finalization_allows_only_explicit_optional_preview_discovery(tmp_path):
    from .result_consumer_fixtures import ResultCatalog, consumer_world
    from .test_record_file_completion_wait import _completed_result
    from camctl.capture.handlers import capture_handler
    class Catalog(ResultCatalog):
        def parameter_definition(self, device_id, action_type, parameter_type):
            definition=super().parameter_definition(device_id,action_type,parameter_type)
            if definition is not None and action_type=='camera_record':
                factory=definition.task_factory
                return replace(definition,task_factory=lambda params:replace(factory(params),
                    file_completion_wait_s=30,product_rules=({'kind':'video','format_id':'osv',
                        'min_count':1,'exact_count':1,'require_pairing':False,'min_size_bytes':1},)))
            return definition
    owned,runtime,action_id,handler=await consumer_world(tmp_path,'record',catalog=Catalog())
    async def read(request,batch):
        context=request.params['completion_context']
        completion={'method':'stop_return_and_wait','version':1,'activity_id':context['activity_id'],
            'stop_result_event_id':context['stop_result_event_id'],'required_wait_ms':30000,
            'observed_wait_ns':30_000_000_000,'completed':True}
        return _completed_result(request.ticket,context,entries=[_osv(),_baseline_preview()],
            finalized=True,completion=completion)
    _result_port(runtime,read)
    try:
        await capture_handler(handler)(action_id,runtime)
        assert runtime.action(action_id)['status']==3
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs WHERE kind=2').fetchone()==(0,)
    finally:
        owned.connection.close()


def _preview_note(state):
    # 与正式reader相同：未配对、零长和无法核实预览只保存观察，不进入候选原片页。
    return {'preview_identity':'/DCIM/a.LRF','original_identity':None,'state':state,
        'osv_count':None,'lrf_count':None,'size_bytes':0 if state=='zero_size' else None,
        'scan_complete':False,'observed_original_identities':[], 'observed_preview_identities':[]}


def _note_port(runtime, read):
    from camctl.bootstrap.capture_assembly import DriverResultListing
    from camctl.devices.evidence import EvidenceContract
    from .test_record_file_completion_wait import _REGISTRY
    from .test_result_page_history import _BINDING
    driver=_result_port(runtime,read)
    registry=_REGISTRY.with_contract(EvidenceContract('results_returned',1,'result',
        frozenset({'file_completion','file_completion_source_page_event_id','optional_preview_observations'})))
    runtime.evidence=registry
    runtime.results=DriverResultListing(driver,_BINDING,registry)
    return driver


def _noted_result(ticket, entries, state):
    from camctl.operations.models import Settlement, SettlementBasis, EvidenceValue
    actual=_actual(ticket,entries=entries,finalized=True)
    return DeviceCallResult.from_outcome(replace(actual,settlement=Settlement(SettlementBasis.OBSERVED,
        EvidenceValue('results_returned',1,{'optional_preview_observations':[_preview_note(state)]}))))


@pytest.mark.parametrize('state',['missing_pair','completion_unknown','zero_size','metadata_failed'])
@pytest.mark.parametrize('include_osv',[False,True])
async def test_optional_preview_observations_do_not_replace_or_block_required_osv(tmp_path,state,include_osv):
    owned,runtime,advance,stopper,wall,mono=await _world(tmp_path)
    async def read(request,batch):
        return _noted_result(request.ticket,[_osv()] if include_osv else [],state)
    _note_port(runtime,read)
    try:
        await advance(13,runtime)
        wall[0]+=1_250_000
        mono[0]+=1_250_000_000
        await advance(13,runtime)
        assert runtime.action(13)['status']==6
        assert owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE id=13').fetchone()==((3,2) if include_osv else (1,1))
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs WHERE kind=2').fetchone()==(0,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('state',['missing_pair','completion_unknown','zero_size','metadata_failed'])
@pytest.mark.parametrize('required_by',['min','exact'])
@pytest.mark.parametrize('entry',['qualification','save','guard'])
async def test_required_preview_gaps_remain_blocked_in_all_completion_entries(tmp_path,state,required_by,entry):
    from camctl.capture.handlers import _advance_timelapse_natural_wait, _register_listing
    rule={'kind':'video','format_id':'lrf','min_count':1 if required_by=='min' else 0,
        'exact_count':1 if required_by=='exact' else None,'require_pairing':True,'min_size_bytes':1}
    owned,runtime,advance,stopper,wall,mono=await _world(tmp_path,extra_rules=(rule,))
    async def read(request,batch):
        return _noted_result(request.ticket,[_osv()],state)
    _note_port(runtime,read)
    try:
        await advance(13,runtime)
        wall[0]+=1_250_000
        mono[0]+=1_250_000_000
        assert _advance_timelapse_natural_wait(runtime,runtime.action(13)) is not None
        listing=await _listing_round(runtime,13)
        _register_listing(runtime,13,listing)
        _finish_listing_result(runtime,listing,end_run=RunOutcome.SUCCEEDED,finalize_output_set=True)
        request=finite.FiniteCancelEndSave(13,listing.entries.last_ref,wall[0])
        if entry=='qualification':
            assert not finite.read_finite_cancel_assessment(request,owned).is_complete
        elif entry=='save':
            assert finite.save_finite_cancel_end(request,new_operation_key(),owned).kind is DbOutcomeKind.ROLLED_BACK
        else:
            rows,action,activity=finite._load(owned,request)
            txn,last=owned.connection.execute('SELECT MAX(id)+1,last_event_id+1 FROM history_transactions').fetchone()
            event=event_envelope(last,txn,finite._TYPE,finite._REASON,
                (update_change('device_activities',13,{'activity_state':1},{'activity_state':3}),),
                request.occurred_at,evidence={'cancel_completion':finite._evidence(rows,request)})
            owned.connection.execute('BEGIN')
            context=EventContext(TransactionRange(txn,last,last),{('device_activities',13):('action',13)},rows,
                current_reads=SqlEventReads(owned.connection,finite._read_scope(request)))
            with pytest.raises(EventValidationError):
                finite._guard(event,context)
            owned.connection.execute('ROLLBACK')
    finally:
        owned.connection.close()


@pytest.mark.parametrize('case',['optional','required_min','required_exact','unknown_original'])
@pytest.mark.parametrize('entry',['save','guard','reuse'])
async def test_set_finalized_entries_share_optional_ownership_classification(tmp_path,case,entry):
    from camctl.capture.handlers import _advance_timelapse_natural_wait,_register_listing
    from camctl.capture.result_pages import OutputSetFinalizationSave
    from camctl.persistence.repositories.capture import _result_check_guard
    from camctl.history.events import load_event_registry
    extra=()
    if case.startswith('required_'):
        extra=({'kind':'video','format_id':'lrf','min_count':1 if case=='required_min' else 0,
            'exact_count':1 if case=='required_exact' else None,'require_pairing':True,'min_size_bytes':1},)
    owned,runtime,advance,stopper,wall,mono=await _world(tmp_path,extra_rules=extra)
    runtime.check_config=AttemptConfig(2,Decimal('1'),Decimal('0'))
    calls=[]
    async def read(request,batch):
        calls.append(request.ticket.attempt_id)
        extra=[]
        if len(calls)>1:
            extra=[{**_osv(),'identity':'/DCIM/unknown.OSV','locator':{'path':'/DCIM/unknown.OSV'}}] if case=='unknown_original' else [_baseline_preview()]
        return DeviceCallResult.from_outcome(_actual(request.ticket,entries=[_osv(),*extra],finalized=True))
    _result_port(runtime,read)
    try:
        await advance(13,runtime)
        wall[0]+=1_250_000
        mono[0]+=1_250_000_000
        _advance_timelapse_natural_wait(runtime,runtime.action(13))
        first=await _listing_round(runtime,13)
        _register_listing(runtime,13,first)
        _finish_listing_result(runtime,first,retry_wait=True)
        current=await _listing_round(runtime,13)
        _finish_listing_result(runtime,current,end_run=RunOutcome.SUCCEEDED)
        request=OutputSetFinalizationSave(13,current.entries.last_ref,current.occurred_at)
        allowed=case=='optional'
        if entry in ('save','reuse'):
            key=new_operation_key()
            result=runtime.capture.finalize_output_set(request,key,owned)
            assert (result.kind is DbOutcomeKind.COMPLETED) is allowed
            if allowed and entry=='reuse':
                before=owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()
                assert runtime.capture.finalize_output_set(request,key,owned).kind is DbOutcomeKind.COMPLETED
                assert owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()==before
        else:
            end=finite.FiniteCancelEndSave(13,current.entries.last_ref,current.occurred_at)
            rows,action,activity=finite._load(owned,end)
            actual_attempt=finite._facts(rows,13)[-1]
            txn,last=owned.connection.execute('SELECT MAX(id)+1,last_event_id+1 FROM history_transactions').fetchone()
            registry=load_event_registry()['events']['RESULT_SET_CONFIRMED']
            event=event_envelope(last,txn,registry['id'],registry['branches']['OUTPUTS_FINALIZED']['reason'],
                (update_change('device_activities',13,{'output_set_finalized_event_id':None},
                    {'output_set_finalized_event_id':last}),),current.occurred_at,
                evidence={'attempt_id':actual_attempt['id'],'result_page_event_id':current.entries.last_ref.event_id})
            owned.connection.execute('BEGIN')
            context=EventContext(TransactionRange(txn,last,last),{('device_activities',13):('action',13)},rows,
                current_reads=SqlEventReads(owned.connection,finite._read_scope(end)))
            if allowed:
                _result_check_guard(event,context)
            else:
                with pytest.raises(EventValidationError):
                    _result_check_guard(event,context)
            owned.connection.execute('ROLLBACK')
    finally:
        owned.connection.close()

"""固定仓储和裸事件都限制 Hasselblad 自动预览的联动来源。"""
import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from camctl.cancellation.models import (FixCancelTargets,FixedCancelSet,FixedTarget,
    SelectionBasis,CancellationEffect,StartCancelAction)
from camctl.contracts.values import new_operation_key
from camctl.devices.drivers.hasselblad.driver import HasselbladDriver
from camctl.host_files.models import BoundDirectories
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.cancellation import CancellationRepository
from camctl.capture.handlers import capture_handler
from ..capture.test_host_photo_flow import _world,_input_time
from .test_hasselblad_start_boundary import HeldTransport

pytestmark=pytest.mark.asyncio


def preview():
    return {'name':'预览','type':'obtain_action_outputs','scheduled_at':_input_time(),
        'params':{'source':{'action_name':'单张 RAW'},'filter':'preview','purpose':'auto_preview'}}


async def world(tmp_path,state,monkeypatch):
    transport=HeldTransport(fail=state=='no_effect')
    cfg,owned,driver,runtime,clock=await _world(tmp_path,extra_actions=[preview()],cancel_action=True,
        driver_factory=lambda cfg:HasselbladDriver(cfg.devices,BoundDirectories(Path(cfg.paths.staging)),
            transport=transport,monotonic_ns=lambda:1))
    task=None
    if state in ('entered','no_effect','zero_tools','terminal'):
        if state in ('no_effect','zero_tools','terminal'):
            if state=='terminal':
                transport.release.set()
                await capture_handler('camera_take_photo')(1,runtime)
            else:
                monkeypatch.setattr('camctl.capture.host_photo._failure_after_return',lambda *args:True)
                if state=='zero_tools':
                    original=driver._request_config
                    def expire(request):
                        value=original(request)
                        clock[0]+=5_000_001
                        return value
                    monkeypatch.setattr(driver,'_request_config',expire)
                transport.release.set()
                await capture_handler('camera_take_photo')(1,runtime)
        else:
            task=asyncio.create_task(capture_handler('camera_take_photo')(1,runtime))
            await asyncio.wait_for(transport.entered.wait(),1)
    elif state=='unknown':
        from camctl.devices.start_entry import StartEntryCoordinator
        runtime.grant(runtime.action(1))
        runtime.start_entries=StartEntryCoordinator()
    repository=CancellationRepository(start_entries=runtime.start_entries)
    started=repository.start_cancel_action(StartCancelAction(3,runtime.wall_us()),new_operation_key(),owned)
    assert started.kind is DbOutcomeKind.COMPLETED,started.error
    return cfg,owned,driver,runtime,transport,task,repository


async def close(owned,transport,task):
    transport.release.set()
    if task: await asyncio.gather(task,return_exceptions=True)
    owned.connection.close()


def request(runtime,*,effect=CancellationEffect.NOT_APPLIED,basis=SelectionBasis.AUTO_PREVIEW):
    return FixCancelTargets(3,FixedCancelSet((FixedTarget(1,SelectionBasis.DIRECT,effect),
        FixedTarget(2,basis,CancellationEffect.NOT_APPLIED))),runtime.wall_us())


@pytest.mark.parametrize('state',['entered','zero_tools','no_effect','unknown','ready'])
@pytest.mark.parametrize('effect',[CancellationEffect.NOT_APPLIED,CancellationEffect.NOT_REQUIRED])
async def test_direct_fixed_repository_rejects_ineligible_link_source(tmp_path,monkeypatch,state,effect):
    cfg,owned,driver,runtime,transport,task,repository=await world(tmp_path,state,monkeypatch)
    try:
        before=tuple(owned.connection.iterdump())
        outcome=repository.fix_cancel_targets(request(runtime,effect=effect),new_operation_key(),owned)
        assert outcome.kind is not DbOutcomeKind.COMPLETED
        assert tuple(owned.connection.iterdump())==before
        assert runtime.action(1)['cancel_requested']==0 and runtime.action(2)['cancel_requested']==0
    finally:
        await close(owned,transport,task)


@pytest.mark.parametrize('state',['entered','unknown'])
@pytest.mark.parametrize('coverage',[True,False])
async def test_bare_fixed_event_rejects_ineligible_or_missing_source_facts(tmp_path,monkeypatch,state,coverage):
    from camctl.persistence.repositories import cancellation as module
    from camctl.persistence.transaction import row_facts,event_envelope,row_change,update_change
    from camctl.contracts.history_values import TransactionRange
    from camctl.history.validators import EventContext,EventValidationError
    from camctl.history.reads import ReadCoverage
    cfg,owned,driver,runtime,transport,task,repository=await world(tmp_path,state,monkeypatch)
    try:
        rows=(update_change('actions',3,{'target_selection_state':1},{'target_selection_state':2}),)
        facts={'actions':{i:row_facts(owned.connection,'actions',i) for i in (1,2,3)},
            'auto_preview_links':{i:row_facts(owned.connection,'auto_preview_links',i)
                for i, in owned.connection.execute('SELECT id FROM auto_preview_links')}}
        owners={('actions',3):('action',3)}
        ranges={}
        if coverage:
            more=module.include_photo_entry_rows(owned.connection,facts['actions'][1],facts,owners)
            ranges.update(more.ranges)
            ranges[('auto_preview_links','obtain_action_id')]=frozenset((1,2))
            ranges[('actions','id')]=frozenset((1,2,3))
            ranges[('plans','id')]=frozenset((1,))
            facts['plans']={1:row_facts(owned.connection,'plans',1)}
        for identity,target,basis in ((1,1,1),(2,2,2)):
            rows+=(row_change('cancel_items',identity,{'action_id':3,'target_action_id':target,
                'selection_basis':basis,'status':1,'cancellation_effect':1,'outcome':None,
                'error_code':None,'error_details_json':None}),)
        event=event_envelope(100,100,4,3,rows,runtime.wall_us())
        context=EventContext(TransactionRange(100,100,100),owners,facts,read_coverage=ReadCoverage(ranges))
        with pytest.raises(EventValidationError): module._cancel_targets_guard(event,context)
    finally:
        await close(owned,transport,task)


def accept_origin(cfg,owned,runtime,target):
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import CommandMode
    from camctl.devices.catalog import build_catalog,DriverDefinitions
    from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID,driver_definition
    from camctl.persistence.repositories.acceptance import AcceptanceRepository,ProcessInput
    outcome=AcceptanceRepository().process_input(ProcessInput(ParsedInput('fixed-scope.json',{
        'request_id':'99','created_at':_input_time(),'name':'固定边界',
        'actions':[{'name':'取消','type':'cancel_task','params':{'target':target}}]}),
        build_catalog(cfg,DriverDefinitions({DRIVER_ID:driver_definition()})),CommandMode.RUN,runtime.wall_us()),
        new_operation_key(),owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED,outcome.error
    identity=owned.connection.execute('SELECT MAX(id) FROM actions WHERE type=6').fetchone()[0]
    repository=CancellationRepository(start_entries=runtime.start_entries)
    outcome=repository.start_cancel_action(StartCancelAction(identity,runtime.wall_us()),new_operation_key(),owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED,outcome.error
    return identity,repository


def save_blocked(runtime):
    from camctl.devices.start_entry import PREVENTED,EntryPhase
    from camctl.operations.models import (CallOutcome,AttemptStatus,EffectState,ErrorValue,
        Settlement,SettlementBasis,EvidenceValue)
    from camctl.persistence.repositories.capture import ActivityObservationSave
    ticket,_=runtime.grant(runtime.action(1))
    runtime.start_entries.get(runtime.owned.metadata.instance_id,1).phase=EntryPhase.BLOCKED
    runtime.finish(ticket,CallOutcome(status=AttemptStatus.FAILED,effect=EffectState.NO_EFFECT,
        settlement=Settlement(SettlementBasis.NOT_DISPATCHED,EvidenceValue.of(PREVENTED)),
        error=ErrorValue('canceled','dispatch')),activity=ActivityObservationSave(1,runtime.wall_us(),dispatch_state=1))


@pytest.mark.parametrize('state',['terminal','blocked','canceled'])
async def test_fixed_repository_accepts_reliable_original_link_source(tmp_path,monkeypatch,state):
    cfg,owned,driver,runtime,transport,task,repository=await world(tmp_path,'terminal' if state=='terminal' else 'ready',monkeypatch)
    try:
        origin=3
        if state=='blocked': save_blocked(runtime)
        if state=='canceled':
            from camctl.bootstrap.flows import _resolve_and_fix
            raw=owned.connection.execute('SELECT input_fields_json FROM actions WHERE id=3').fetchone()[0]
            _resolve_and_fix(owned,repository,3,raw,runtime.wall_us)
            origin,repository=accept_origin(cfg,owned,runtime,{'action_instance_id':'1'})
        fixed=replace(request(runtime),action_id=origin)
        key=new_operation_key()
        saved=repository.fix_cancel_targets(fixed,key,owned)
        assert saved.kind is DbOutcomeKind.COMPLETED,saved.error
        before=tuple(owned.connection.iterdump())
        from camctl.persistence.repositories import cancellation as module
        def unavailable(*args): raise AssertionError('原键重送不重新读取当前来源')
        monkeypatch.setattr(module._FixCancelTargetsCommand,'_load_links',unavailable)
        # 后来来源状态不参与原合法申请重送；改变依据、效果或原发起者则拒绝。
        assert repository.fix_cancel_targets(fixed,key,owned).kind is DbOutcomeKind.COMPLETED
        altered=replace(fixed,targets=FixedCancelSet((fixed.targets.targets[0],replace(fixed.targets.targets[1],basis=SelectionBasis.BOTH))))
        assert repository.fix_cancel_targets(altered,key,owned).kind is not DbOutcomeKind.COMPLETED
        altered=replace(fixed,targets=FixedCancelSet((replace(fixed.targets.targets[0],cancellation_effect=CancellationEffect.NOT_REQUIRED),fixed.targets.targets[1])))
        assert repository.fix_cancel_targets(altered,key,owned).kind is not DbOutcomeKind.COMPLETED
        assert repository.fix_cancel_targets(replace(fixed,action_id=origin+100),key,owned).kind is not DbOutcomeKind.COMPLETED
        assert tuple(owned.connection.iterdump())==before
    finally:
        await close(owned,transport,task)


@pytest.mark.parametrize('address',['plan','request'])
@pytest.mark.parametrize('state,basis,allowed',[('entered',SelectionBasis.DIRECT,True),
    ('unknown',SelectionBasis.DIRECT,True),('entered',SelectionBasis.BOTH,False),
    ('terminal',SelectionBasis.BOTH,True)])
async def test_direct_preview_and_both_require_original_address_and_valid_link(tmp_path,monkeypatch,address,state,basis,allowed):
    cfg,owned,driver,runtime,transport,task,repository=await world(tmp_path,state,monkeypatch)
    try:
        target={'plan_instance_id':'1'} if address=='plan' else {'request_id':'1'}
        origin,repository=accept_origin(cfg,owned,runtime,target)
        fixed=replace(request(runtime,basis=basis),action_id=origin)
        saved=repository.fix_cancel_targets(fixed,new_operation_key(),owned)
        assert (saved.kind is DbOutcomeKind.COMPLETED)==allowed,saved.error
        if allowed and basis is SelectionBasis.DIRECT:
            from camctl.cancellation.service import ApplyCancel,CancellationRuntime,apply_cancel
            from camctl.cancellation.settlement import TargetSettlement
            from camctl.persistence.repositories.outputs import OutputsRepository
            result=await apply_cancel(ApplyCancel(origin,saved.value.item_ids),CancellationRuntime(
                owned,repository,TargetSettlement(owned,OutputsRepository(),repository,
                    lambda _:'unknown',runtime.wall_us),runtime.wall_us))
            assert result.items[0].status==4 and result.items[1].status==3
            assert runtime.action(1)['cancel_requested']==0 and runtime.action(2)['cancel_requested']==1
    finally:
        await close(owned,transport,task)


async def test_both_label_cannot_add_an_unaddressed_preview(tmp_path,monkeypatch):
    cfg,owned,driver,runtime,transport,task,repository=await world(tmp_path,'terminal',monkeypatch)
    try:
        before=tuple(owned.connection.iterdump())
        saved=repository.fix_cancel_targets(request(runtime,basis=SelectionBasis.BOTH),new_operation_key(),owned)
        assert saved.kind is not DbOutcomeKind.COMPLETED
        assert tuple(owned.connection.iterdump())==before
    finally:
        await close(owned,transport,task)


@pytest.mark.parametrize('source_state',['entered','unknown'])
@pytest.mark.parametrize('include_rejected_preview',[True,False])
async def test_mixed_composite_sources_keep_each_previews_own_source(tmp_path,monkeypatch,include_rejected_preview,source_state):
    from camctl.devices.start_entry import EntryBlock
    second={'name':'第二张','type':'camera_take_photo','device_id':'cam-1','group':'g',
        'scheduled_at':'2025-06-15 15:10:00','params':{'type':'hasselblad_photo','capture_format':'raw',
        'exposure':{'mode':'P','iso':100},'white_balance':{'mode':'auto'},'focus':{'mode':'MF'}},
        'policy':{'max_delay_ms':5000}}
    first_preview=preview()
    second_preview=preview()
    second_preview['name']='第二预览'
    second_preview['scheduled_at']='2025-06-15 15:10:00'
    second_preview['params']={**second_preview['params'],'source':{'action_name':'第二张'}}
    transport=HeldTransport()
    cfg,owned,driver,runtime,clock=await _world(tmp_path,extra_actions=[second,first_preview,second_preview],photo_group='g',
        driver_factory=lambda cfg:HasselbladDriver(cfg.devices,BoundDirectories(Path(cfg.paths.staging)),transport=transport,monotonic_ns=lambda:1))
    task=None
    if source_state=='entered':
        task=asyncio.create_task(capture_handler('camera_take_photo')(1,runtime))
        await asyncio.wait_for(transport.entered.wait(),1)
    else:
        from camctl.devices.start_entry import StartEntryCoordinator
        runtime.grant(runtime.action(1))
        runtime.start_entries=StartEntryCoordinator()
    try:
        origin,repository=accept_origin(cfg,owned,runtime,{'plan_instance_id':'1','group':'g'})
        permit=runtime.start_entries.reserve(owned.metadata.instance_id,2,no_intent=True)
        targets=[FixedTarget(i,SelectionBasis.DIRECT,CancellationEffect.NOT_APPLIED) for i in (1,2)]
        targets.extend(FixedTarget(i,SelectionBasis.AUTO_PREVIEW,CancellationEffect.NOT_APPLIED)
            for i in ((3,4) if include_rejected_preview else (4,)))
        fixed=FixCancelTargets(origin,FixedCancelSet(tuple(targets)),runtime.wall_us(),
            (EntryBlock(permit.database_id,2,None),))
        before=tuple(owned.connection.iterdump())
        saved=repository.fix_cancel_targets(fixed,new_operation_key(),owned)
        assert (saved.kind is DbOutcomeKind.COMPLETED)==(not include_rejected_preview),(saved.error,
            owned.connection.execute('SELECT id,group_name,status,error_details_json FROM actions').fetchall(),
            owned.connection.execute('SELECT * FROM auto_preview_links').fetchall())
        if include_rejected_preview: assert tuple(owned.connection.iterdump())==before
        else:
            assert runtime.action(1)['cancel_requested']==0 and runtime.action(2)['cancel_requested']==1
            assert owned.connection.execute('SELECT target_action_id FROM cancel_items WHERE action_id=? ORDER BY id',(origin,)).fetchall()==[(1,),(2,),(4,)]
    finally:
        await close(owned,transport,task)


def bare_context(owned,runtime):
    from camctl.persistence.repositories import cancellation as module
    from camctl.persistence.transaction import row_facts,event_envelope,row_change,update_change
    from camctl.contracts.history_values import TransactionRange
    from camctl.history.validators import EventContext
    from camctl.history.reads import ReadCoverage
    state={table:{i:row_facts(owned.connection,table,i)
        for i, in owned.connection.execute(f'SELECT id FROM {table}')}
        for table in ('actions','plans','auto_preview_links')}
    owners={('actions',3):('action',3)}
    ranges=dict(module.include_photo_entry_rows(owned.connection,state['actions'][1],state,owners).ranges)
    ranges.update({('auto_preview_links','obtain_action_id'):frozenset((1,2)),
        ('actions','id'):frozenset((1,2,3)),('plans','id'):frozenset((1,))})
    rows=(update_change('actions',3,{'target_selection_state':1},{'target_selection_state':2}),)
    for identity,target,basis in ((1001,1,1),(1002,2,2)):
        rows+=(row_change('cancel_items',identity,{'action_id':3,'target_action_id':target,
            'selection_basis':basis,'status':1,'cancellation_effect':1,'outcome':None,
            'error_code':None,'error_details_json':None}),)
    event=event_envelope(100,100,4,3,rows,runtime.wall_us())
    return event,EventContext(TransactionRange(100,100,100),owners,state,read_coverage=ReadCoverage(ranges))


@pytest.mark.parametrize('state',['terminal','blocked'])
async def test_independent_guard_accepts_reliable_preceding_original_source(tmp_path,monkeypatch,state):
    from camctl.persistence.repositories import cancellation as module
    cfg,owned,driver,runtime,transport,task,repository=await world(tmp_path,'terminal' if state=='terminal' else 'ready',monkeypatch)
    try:
        if state=='blocked': save_blocked(runtime)
        event,context=bare_context(owned,runtime)
        module._cancel_targets_guard(event,context)
    finally:
        await close(owned,transport,task)


@pytest.mark.parametrize('missing',[('auto_preview_links','obtain_action_id'),('actions','id'),
    ('device_activities','action_id'),('operation_runs','action_id'),('operation_attempts','run_id')])
async def test_independent_guard_requires_every_original_source_range(tmp_path,monkeypatch,missing):
    from camctl.persistence.repositories import cancellation as module
    from camctl.history.validators import EventValidationError
    from camctl.history.reads import ReadCoverage
    cfg,owned,driver,runtime,transport,task,repository=await world(tmp_path,'terminal',monkeypatch)
    try:
        event,context=bare_context(owned,runtime)
        context=replace(context,read_coverage=ReadCoverage({k:v for k,v in context.read_coverage.ranges.items() if k!=missing}))
        with pytest.raises(EventValidationError): module._cancel_targets_guard(event,context)
    finally:
        await close(owned,transport,task)


@pytest.mark.parametrize('corruption',['extra_link','duplicate_entry','wrong_entry_identity','future_result'])
async def test_independent_guard_rejects_extra_conflicting_or_future_facts(tmp_path,monkeypatch,corruption):
    from copy import deepcopy
    from camctl.persistence.repositories import cancellation as module
    from camctl.history.validators import EventValidationError
    cfg,owned,driver,runtime,transport,task,repository=await world(tmp_path,'unknown' if corruption=='future_result' else 'no_effect',monkeypatch)
    try:
        event,context=bare_context(owned,runtime)
        state=deepcopy(context.state_rows)
        if corruption=='extra_link':
            link=next(iter(state['auto_preview_links'].values()))
            state['auto_preview_links'][999]={**link,'id':999}
        elif corruption in ('duplicate_entry','wrong_entry_identity'):
            attempt=next(iter(state['operation_attempts'].values()))
            observations=attempt['result_json']['observations']
            if corruption=='duplicate_entry': observations.extend(deepcopy(observations))
            else: observations[0]['data']['activity_id']='999'
        else:
            future=deepcopy(state['operation_attempts'])
            for attempt in future.values():
                attempt.update(status=3,effect_state=2,result_event_id=101,result_json={
                    'settlement':{'basis':'not_dispatched','evidence':{'type':'start_call_prevented','version':1,'data':{}}},
                    'observations':[]})
            context=replace(context,transaction_rows={'operation_attempts':future})
        context=replace(context,state_rows=state)
        with pytest.raises(EventValidationError): module._cancel_targets_guard(event,context)
    finally:
        await close(owned,transport,task)


async def test_action_direct_preview_remains_independent(tmp_path,monkeypatch):
    from camctl.cancellation.service import ApplyCancel,CancellationRuntime,apply_cancel
    from camctl.cancellation.settlement import TargetSettlement
    from camctl.persistence.repositories.outputs import OutputsRepository
    cfg,owned,driver,runtime,transport,task,repository=await world(tmp_path,'entered',monkeypatch)
    try:
        target={'action_instance_id':'2'}
        origin,repository=accept_origin(cfg,owned,runtime,target)
        targets=(FixedTarget(2,SelectionBasis.DIRECT,CancellationEffect.NOT_APPLIED),)
        saved=repository.fix_cancel_targets(FixCancelTargets(origin,FixedCancelSet(targets),runtime.wall_us()),new_operation_key(),owned)
        assert saved.kind is DbOutcomeKind.COMPLETED,saved.error
        await apply_cancel(ApplyCancel(origin,saved.value.item_ids),CancellationRuntime(
            owned,repository,TargetSettlement(owned,OutputsRepository(),repository,
                lambda _:'unknown',runtime.wall_us),runtime.wall_us))
        assert runtime.action(1)['cancel_requested']==0 and runtime.action(2)['cancel_requested']==1
        assert not task.done()
    finally:
        await close(owned,transport,task)


@pytest.mark.parametrize('actual_transaction',[False,True])
async def test_no_intent_header_alone_cannot_replace_atomic_source_apply(tmp_path,monkeypatch,actual_transaction):
    from camctl.persistence.repositories import cancellation as module
    from camctl.persistence.transaction import commit_operation
    from camctl.history.reads import PlanReadScope
    from camctl.history.validators import EventValidationError
    cfg,owned,driver,runtime,transport,task,repository=await world(tmp_path,'ready',monkeypatch)
    fixed=request(runtime)
    header={'photo_entry_request':{'action_id':3,'occurred_at':runtime.wall_us(),
        'targets':[{'action_id':t.action_id,'basis':t.basis.value,'effect':t.cancellation_effect.value}
            for t in fixed.targets.targets],
        'blocks':[{'database_id':owned.metadata.instance_id,'action_id':1,'ticket':None}]}}
    try:
        if not actual_transaction:
            event,context=bare_context(owned,runtime)
            event=replace(event,evidence=header)
            with pytest.raises(EventValidationError): module._cancel_targets_guard(event,context)
        else:
            key=new_operation_key()
            class HeaderOnly:
                def plan(self,scope):
                    proposal=module._FixCancelTargetsCommand(fixed,key,reserved_sources=frozenset((1,))).plan(scope)
                    return replace(proposal,events=(replace(proposal.events[0],evidence=header),),
                        plan_read_scope=PlanReadScope(1,128))
            before=tuple(owned.connection.iterdump())
            outcome=commit_operation(HeaderOnly(),key,owned)
            assert outcome.kind!='completed'
            assert tuple(owned.connection.iterdump())==before
            assert runtime.action(1)['cancel_requested']==0
    finally:
        await close(owned,transport,task)


@pytest.mark.parametrize('missing',['type','driver_id','execution_spec_json'])
async def test_independent_guard_does_not_classify_partial_source_as_another_driver(tmp_path,monkeypatch,missing):
    from copy import deepcopy
    from camctl.persistence.repositories import cancellation as module
    from camctl.history.validators import EventValidationError
    cfg,owned,driver,runtime,transport,task,repository=await world(tmp_path,'entered',monkeypatch)
    try:
        event,context=bare_context(owned,runtime)
        state=deepcopy(context.state_rows)
        del state['actions'][1][missing]
        with pytest.raises(EventValidationError):
            module._cancel_targets_guard(event,replace(context,state_rows=state))
    finally:
        await close(owned,transport,task)


@pytest.mark.parametrize('fault',[None,'missing','wrong_source','wrong_item','wrong_txn',
    'outside','before_fixed','duplicate','split','wrong_time','wrong_mode'])
async def test_no_intent_guard_borrows_exact_same_transaction_source_apply(tmp_path,monkeypatch,fault):
    from camctl.persistence.repositories import cancellation as module
    from camctl.persistence.transaction import event_envelope,update_change
    from camctl.persistence.event_reads import SqlEventReads
    from camctl.history.reads import PlanReadScope
    from camctl.contracts.history_values import TransactionRange
    from camctl.history.validators import EventValidationError
    cfg,owned,driver,runtime,transport,task,repository=await world(tmp_path,'ready',monkeypatch)
    try:
        fixed,context=bare_context(owned,runtime)
        fixed=replace(fixed,evidence={'photo_entry_request':{
            'action_id':3,'occurred_at':fixed.occurred_at,
            'targets':[{'action_id':1,'basis':1,'effect':1},{'action_id':2,'basis':2,'effect':1}],
            'blocks':[{'database_id':owned.metadata.instance_id,'action_id':1,'ticket':None}]}})
        source=2 if fault=='wrong_source' else 1
        item=1002 if fault=='wrong_item' else 1001
        application=event_envelope(101,101 if fault=='wrong_txn' else 100,25,1,(
            update_change('actions',source,{'cancel_requested':0},{'cancel_requested':1}),
            update_change('cancel_items',item,{'status':1},{'status':2})),
            fixed.occurred_at+(1 if fault=='wrong_time' else 0),
            evidence={'cancel_request':{'item_id':item,'mode':'with_stop' if fault=='wrong_mode' else 'pre_start',
                'occurred_at':fixed.occurred_at}})
        planned=(fixed,application)
        transaction=TransactionRange(100,100,101)
        if fault=='missing': planned=(fixed,); transaction=TransactionRange(100,100,100)
        elif fault=='outside': application=replace(application,event_id=102); planned=(fixed,application)
        elif fault=='before_fixed':
            application=replace(application,event_id=99); planned=(application,fixed)
            transaction=TransactionRange(100,99,100)
        elif fault=='duplicate':
            planned=(fixed,application,replace(application,event_id=102)); transaction=TransactionRange(100,100,102)
        elif fault=='split': planned=(fixed,)
        owned.connection.execute('BEGIN')
        try:
            reads=SqlEventReads(owned.connection,PlanReadScope(1,128),planned_events=planned)
            context=replace(context,transaction=transaction,current_reads=reads)
            if fault is None: module._cancel_targets_guard(fixed,context)
            else:
                with pytest.raises(EventValidationError): module._cancel_targets_guard(fixed,context)
        finally:
            owned.connection.rollback()
    finally:
        await close(owned,transport,task)


@pytest.mark.parametrize('change',['sparse_other_plan','explicit_other_plan','new_in','move_out','delete','missing_owner','invalid_owner'])
async def test_plan_iteration_merges_only_verified_current_members(tmp_path,monkeypatch,change):
    from types import SimpleNamespace
    from camctl.persistence.transaction import event_envelope,update_change,row_change
    from camctl.persistence.event_reads import SqlEventReads
    from camctl.history.reads import PlanReadScope
    from camctl.contracts.values import ConsistencyError
    cfg,owned,driver,runtime,transport,task,repository=await world(tmp_path,'ready',monkeypatch)
    try:
        # 新取消发起者属于另一计划；读取端口只接收前序覆盖。
        origin,repository=accept_origin(cfg,owned,runtime,{'action_instance_id':'1'})
        expected={identity for identity, in owned.connection.execute('SELECT id FROM actions WHERE plan_id=1')}
        from camctl.history.events import RowChange,RowImage
        if change=='sparse_other_plan':
            rows=(update_change('actions',origin,{'cancel_requested':0},{'cancel_requested':1}),)
        elif change=='explicit_other_plan':
            rows=(RowChange('actions',origin,RowImage(True,{'cancel_requested':0}),RowImage(True,{'cancel_requested':1,'plan_id':2})),)
        elif change in ('new_in','invalid_owner'):
            rows=(row_change('actions',999,{'id':999,'plan_id':None if change=='invalid_owner' else 1,'status':1}),)
        elif change=='move_out': rows=(RowChange('actions',1,RowImage(True,{'plan_id':1}),RowImage(True,{'plan_id':2})),)
        elif change=='delete':
            rows=(RowChange('actions',1,RowImage(True,{'plan_id':1}),RowImage(False,{})),)
        else: rows=(update_change('actions',999,{'status':1},{'status':2}),)
        prior=SimpleNamespace(envelope=event_envelope(100,100,25,1,rows,runtime.wall_us()))
        owned.connection.execute('BEGIN')
        try:
            reads=SqlEventReads(owned.connection,PlanReadScope(1,1),validated=(prior,))
            if change in ('missing_owner','invalid_owner'):
                with pytest.raises(ConsistencyError): list(reads.iter_members('actions','plan_id',1))
            else:
                members=[identity for batch in reads.iter_members('actions','plan_id',1) for identity,_ in batch]
                if change=='new_in': expected.add(999)
                elif change in ('move_out','delete'): expected.remove(1)
                assert set(members)==expected
                with pytest.raises(ConsistencyError): reads.optional('actions',origin)
        finally:
            owned.connection.rollback()
    finally:
        await close(owned,transport,task)

"""原保存地址独立决定完整固定集合，提交成员不能控制 HB 识别。"""
from dataclasses import replace

import pytest

from camctl.cancellation.models import (FixCancelTargets,FixedCancelSet,FixedTarget,
    SelectionBasis,CancellationEffect)
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from .test_hasselblad_preview_fixed_boundary import world,close,accept_origin

pytestmark=pytest.mark.asyncio


def fixed(origin,runtime,entries):
    return FixCancelTargets(origin,FixedCancelSet(tuple(FixedTarget(identity,
        SelectionBasis(basis),CancellationEffect(effect)) for identity,basis,effect in entries)),runtime.wall_us())


def address(kind):
    return {'action':{'action_instance_id':'1'},'preview':{'action_instance_id':'2'},
        'group':{'plan_instance_id':'1','group':'g'},'plan':{'plan_instance_id':'1'},
        'request':{'request_id':'1'}}[kind]


def context_for(owned,runtime,origin,entries):
    from camctl.persistence.repositories import cancellation as module
    from camctl.persistence.transaction import row_facts,event_envelope,row_change,update_change
    from camctl.contracts.history_values import TransactionRange
    from camctl.history.validators import EventContext
    from camctl.history.reads import ReadCoverage
    state={table:{identity:row_facts(owned.connection,table,identity)
        for identity, in owned.connection.execute(f'SELECT id FROM {table}')}
        for table in ('actions','plans','auto_preview_links')}
    ids=frozenset(state['actions'])
    ranges={('actions','id'):ids,('actions','plan_id'):frozenset(state['plans']),
        ('plans','id'):frozenset(state['plans']),
        ('plans','request_id'):frozenset(p['request_id'] for p in state['plans'].values()),
        ('auto_preview_links','source_action_id'):ids,('auto_preview_links','obtain_action_id'):ids}
    owners={('actions',origin):('action',origin)}
    for source in state['actions'].values():
        if source['type'] in (1,2,3):
            for key,values in module.include_photo_entry_rows(owned.connection,source,state,owners).ranges.items():
                ranges[key]=frozenset((*ranges.get(key,()),*values))
    rows=(update_change('actions',origin,{'target_selection_state':1},{'target_selection_state':2}),)
    for index,(identity,basis,effect) in enumerate(entries,1001):
        rows+=(row_change('cancel_items',index,{'action_id':origin,'target_action_id':identity,
            'selection_basis':basis,'cancellation_effect':effect,'status':1,'outcome':None,
            'error_code':None,'error_details_json':None}),)
    event=event_envelope(100,100,4,3,rows,runtime.wall_us())
    return event,EventContext(TransactionRange(100,100,100),owners,state,read_coverage=ReadCoverage(ranges))


def reject(owned,runtime,repository,origin,entries,surface):
    from camctl.persistence.repositories import cancellation as module
    from camctl.history.validators import EventValidationError
    before=tuple(owned.connection.iterdump())
    if surface=='repository':
        result=repository.fix_cancel_targets(fixed(origin,runtime,entries),new_operation_key(),owned)
        assert result.kind is not DbOutcomeKind.COMPLETED
    else:
        event,context=context_for(owned,runtime,origin,entries)
        with pytest.raises(EventValidationError): module._cancel_targets_guard(event,context)
    assert tuple(owned.connection.iterdump())==before
    assert runtime.action(1)['cancel_requested']==0 and runtime.action(2)['cancel_requested']==0


@pytest.mark.parametrize('state',['entered','unknown'])
@pytest.mark.parametrize('kind',['action','group','plan','request'])
@pytest.mark.parametrize('basis',[1,2,3])
@pytest.mark.parametrize('surface',['repository','guard'])
async def test_original_source_cannot_disappear_behind_preview_label(tmp_path,monkeypatch,state,kind,basis,surface):
    cfg,owned,driver,runtime,transport,task,repository=await world(tmp_path,state,monkeypatch,photo_group='g')
    try:
        origin,repository=accept_origin(cfg,owned,runtime,address(kind))
        reject(owned,runtime,repository,origin,[(2,basis,1)],surface)
    finally:
        await close(owned,transport,task)


@pytest.mark.parametrize('surface',['repository','guard'])
@pytest.mark.parametrize('state,kind,entries',[
    ('terminal','action',[(1,1,3)]),  # 省略实际应联动的预览。
    ('entered','action',[(1,1,3)]),  # 初始效果不能由提交者伪造。
    ('terminal','action',[(1,1,1),(2,2,1)]),
    ('entered','preview',[(2,1,3)]),
    ('entered','preview',[(1,1,1),(2,1,1)]),  # 单独预览不反向扩源。
    ('entered','action',[(1,2,1)]),
    ('entered','action',[(1,3,1)]),
])
async def test_complete_model_rejects_omissions_extra_members_and_wrong_effects(tmp_path,monkeypatch,state,kind,entries,surface):
    cfg,owned,driver,runtime,transport,task,repository=await world(tmp_path,state,monkeypatch)
    try:
        origin,repository=accept_origin(cfg,owned,runtime,address(kind))
        reject(owned,runtime,repository,origin,entries,surface)
    finally:
        await close(owned,transport,task)


@pytest.mark.parametrize('camera',['hb','other'])
@pytest.mark.parametrize('state',['ready','unknown','entered','no_effect','zero_tools','terminal','blocked'])
async def test_original_sql_and_complete_rows_keep_same_start_partition(tmp_path,monkeypatch,state,camera):
    from camctl.persistence.repositories import capture_facts as module
    from camctl.persistence.transaction import row_facts
    from .test_hasselblad_preview_fixed_boundary import save_blocked
    cfg,owned,driver,runtime,transport,task,repository=await world(tmp_path,'ready' if state=='blocked' else state,monkeypatch)
    try:
        if state=='blocked': save_blocked(runtime)
        action=runtime.action(1)
        if camera=='other': action={**action,'driver_id':'generic-photo'}
        activities=[row_facts(owned.connection,'device_activities',identity) for identity, in
            owned.connection.execute('SELECT id FROM device_activities WHERE action_id=1')]
        runs=[row_facts(owned.connection,'operation_runs',identity) for identity, in
            owned.connection.execute('SELECT id FROM operation_runs WHERE action_id=1')]
        attempts={run['id']:[row_facts(owned.connection,'operation_attempts',identity) for identity, in
            owned.connection.execute('SELECT id FROM operation_attempts WHERE run_id=?',(run['id'],))] for run in runs}
        sql=module.load_start_facts(owned.connection,action)
        rows=module.start_facts_from_rows(action,activities,runs,attempts)
        assert rows==sql
        assert sql.not_started==(state in ('ready','blocked') or (camera=='other' and state in ('no_effect','zero_tools')))
        if camera=='hb':
            from camctl.cancellation.rules import capture_eligibility_facts,load_eligibility_facts
            assert capture_eligibility_facts(action,rows.activity,not_started=rows.not_started)==load_eligibility_facts(owned.connection,1)
    finally:
        await close(owned,transport,task)


@pytest.mark.parametrize('state',['entered','unknown','terminal','blocked'])
@pytest.mark.parametrize('kind',['action','preview','group','plan','request'])
async def test_authoritative_complete_scope_keeps_valid_direct_and_linked_members(tmp_path,monkeypatch,state,kind):
    from .test_hasselblad_preview_fixed_boundary import save_blocked
    from camctl.persistence.repositories import cancellation as module
    cfg,owned,driver,runtime,transport,task,repository=await world(tmp_path,'ready' if state=='blocked' else state,monkeypatch,photo_group='g')
    try:
        if state=='blocked': save_blocked(runtime)
        origin,repository=accept_origin(cfg,owned,runtime,address(kind))
        effect=3 if state=='terminal' else 1
        linked=state in ('terminal','blocked')
        if kind=='preview': entries=[(2,1,1)]
        elif kind in ('plan','request'): entries=[(1,1,effect),(2,3 if linked else 1,1),(3,1,1)]
        else: entries=[(1,1,effect)]+([(2,2,1)] if linked else [])
        event,context=context_for(owned,runtime,origin,entries)
        module._cancel_targets_guard(event,context)
        result=repository.fix_cancel_targets(fixed(origin,runtime,entries),new_operation_key(),owned)
        assert result.kind is DbOutcomeKind.COMPLETED,result.error
        assert owned.connection.execute('SELECT target_action_id,selection_basis,cancellation_effect FROM cancel_items WHERE action_id=? ORDER BY target_action_id',(origin,)).fetchall()==entries
        assert runtime.action(1)['cancel_requested']==0
    finally:
        await close(owned,transport,task)


@pytest.mark.parametrize('kind,missing',[
    ('action',('actions','id')),('group',('actions','plan_id')),('plan',('plans','id')),
    ('request',('plans','request_id')),('request',('actions','plan_id')),
    ('action',('auto_preview_links','source_action_id')),('action',('auto_preview_links','obtain_action_id')),
    ('action',('device_activities','action_id')),('action',('operation_runs','action_id')),
    ('action',('operation_attempts','run_id')),
])
async def test_complete_scope_requires_authoritative_address_links_and_start_ranges(tmp_path,monkeypatch,kind,missing):
    from camctl.persistence.repositories import cancellation as module
    from camctl.history.reads import ReadCoverage
    from camctl.history.validators import EventValidationError
    cfg,owned,driver,runtime,transport,task,repository=await world(tmp_path,'entered',monkeypatch,photo_group='g')
    try:
        origin,repository=accept_origin(cfg,owned,runtime,address(kind))
        entries=[(1,1,1),(2,1,1),(3,1,1)] if kind in ('plan','request') else [(1,1,1)]
        event,context=context_for(owned,runtime,origin,entries)
        module._cancel_targets_guard(event,context)
        context=replace(context,read_coverage=ReadCoverage({key:value for key,value in context.read_coverage.ranges.items() if key!=missing}))
        with pytest.raises(EventValidationError): module._cancel_targets_guard(event,context)
    finally:
        await close(owned,transport,task)


async def test_original_key_keeps_first_direct_decision_after_capture_finishes(tmp_path,monkeypatch):
    cfg,owned,driver,runtime,transport,task,repository=await world(tmp_path,'entered',monkeypatch)
    try:
        origin,repository=accept_origin(cfg,owned,runtime,address('action'))
        command=fixed(origin,runtime,[(1,1,1)])
        key=new_operation_key()
        assert repository.fix_cancel_targets(command,key,owned).kind is DbOutcomeKind.COMPLETED
        transport.release.set()
        await task
        assert runtime.action(1)['status'] in (3,4)
        before=tuple(owned.connection.iterdump())
        replay=repository.fix_cancel_targets(command,key,owned)
        assert replay.kind is DbOutcomeKind.COMPLETED,replay.error
        assert tuple(owned.connection.iterdump())==before
        assert runtime.action(1)['cancel_requested']==0
    finally:
        await close(owned,transport,task)


async def test_original_key_does_not_accept_a_duplicated_member_request(tmp_path,monkeypatch):
    cfg,owned,driver,runtime,transport,task,repository=await world(tmp_path,'entered',monkeypatch)
    try:
        origin,repository=accept_origin(cfg,owned,runtime,address('action'))
        command=fixed(origin,runtime,[(1,1,1)])
        key=new_operation_key()
        assert repository.fix_cancel_targets(command,key,owned).kind is DbOutcomeKind.COMPLETED
        before=tuple(owned.connection.iterdump())
        duplicate=replace(command,targets=FixedCancelSet(command.targets.targets*2))
        assert repository.fix_cancel_targets(duplicate,key,owned).kind is not DbOutcomeKind.COMPLETED
        assert tuple(owned.connection.iterdump())==before
    finally:
        await close(owned,transport,task)


@pytest.mark.parametrize('state',['applied','terminal','entered'])
async def test_new_origin_reuses_original_hb_cancel_and_preview_scope(tmp_path,monkeypatch,state):
    from camctl.bootstrap.flows import _resolve_and_fix
    cfg,owned,driver,runtime,transport,task,repository=await world(tmp_path,'entered' if state=='entered' else 'ready',monkeypatch)
    try:
        raw=owned.connection.execute('SELECT input_fields_json FROM actions WHERE id=3').fetchone()[0]
        original_items=_resolve_and_fix(owned,repository,3,raw,runtime.wall_us)
        if state=='terminal':
            from camctl.capture.handlers import capture_handler
            await capture_handler('camera_take_photo')(1,runtime)
            from camctl.cancellation.service import ApplyCancel,apply_cancel
            from .test_unfixed_cancel_origin import runtime_for
            await apply_cancel(ApplyCancel(3,original_items),runtime_for(owned,repository,runtime))
        assert runtime.action(1)['status']==(6 if state=='terminal' else 2)
        assert runtime.action(1)['cancel_requested']==int(state!='entered')
        origin,repository=accept_origin(cfg,owned,runtime,address('action'))
        raw=owned.connection.execute('SELECT input_fields_json FROM actions WHERE id=?',(origin,)).fetchone()[0]
        _resolve_and_fix(owned,repository,origin,raw,runtime.wall_us)
        expected=[(1,1,3 if state=='terminal' else 1)]+([] if state=='entered' else [(2,2,1)])
        assert owned.connection.execute('SELECT target_action_id,selection_basis,cancellation_effect FROM cancel_items WHERE action_id=? ORDER BY target_action_id',(origin,)).fetchall()==expected
        assert owned.connection.execute('SELECT COUNT(*) FROM operation_runs WHERE kind=1').fetchone()[0]==int(state=='entered')
        assert owned.connection.execute('SELECT id FROM operation_runs WHERE kind=2').fetchall()==[]
    finally:
        await close(owned,transport,task)

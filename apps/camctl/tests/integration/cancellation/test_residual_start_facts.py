"""跨计划残留责任保留旧目标，不改变触发动作自己的 START 分区。"""
from decimal import Decimal

import pytest

from camctl.contracts.values import new_operation_key
from camctl.operations.attempts import (AttemptConfig,AttemptFinish,AttemptIntent,AttemptTarget,
    OperationKind,QueryPurpose,RunOutcome,StaleRunFinish)
from camctl.operations.models import CallOutcome,AttemptStatus,EffectState,Settlement,SettlementBasis,EvidenceValue,ErrorValue
from camctl.devices.evidence import EvidenceRegistry,EvidenceContract
from camctl.operations.validation import validate_outcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.capture_facts import load_start_facts
from camctl.persistence.transaction import row_facts
from .test_start_scope_facts import save_generic_no_effect_start
from ..scheduling.test_resources import _seed_environment,_seed_plan,_seed_record_action,_seed_activity,_NOW


def begin(owned,action_id,kind,activity_id,purpose,occurred_at):
    result=OperationRepository().begin_attempt(AttemptIntent('query' if purpose is not None else 'stop',
        action_id,kind,AttemptTarget(activity_id=activity_id),purpose,
        AttemptConfig(2,Decimal(10),Decimal(1)),occurred_at),new_operation_key(),owned)
    assert result.kind is DbOutcomeKind.COMPLETED,result.error
    assert result.value.ticket is not None
    return result.value.ticket


def returned(owned,ticket,occurred_at):
    outcome=CallOutcome(status=AttemptStatus.SUCCEEDED,effect=EffectState.UNKNOWN,
        settlement=Settlement(SettlementBasis.OBSERVED,EvidenceValue('operation_returned',1,{})))
    result=OperationRepository().finish_attempt(AttemptFinish(ticket,validate_outcome(ticket,outcome,
        EvidenceRegistry((EvidenceContract('operation_returned',1,ticket.operation,frozenset()),))),occurred_at),
        new_operation_key(),owned)
    assert result.kind is DbOutcomeKind.COMPLETED,result.error


def residual(owned,trigger_id,old_activity_id,occurred_at,phase):
    stop=begin(owned,trigger_id,OperationKind.STOP_RESIDUAL,old_activity_id,None,occurred_at)
    query=None
    if phase!='stop_inflight': returned(owned,stop,occurred_at)
    if phase.startswith('confirm_'):
        query=begin(owned,trigger_id,OperationKind.QUERY_ACTIVITY,old_activity_id,
            QueryPurpose.RESIDUAL_STOP_CONFIRMATION,occurred_at)
        if phase!='confirm_inflight': returned(owned,query,occurred_at)
    if phase.endswith('_ended'):
        result=OperationRepository().finish_stale_runs(StaleRunFinish(
            tuple(t.responsibility_key for t in (stop,query) if t is not None),RunOutcome.UNCONFIRMED,occurred_at,
            ErrorValue('result_unconfirmed','device')),
            new_operation_key(),owned)
        assert result.kind is DbOutcomeKind.COMPLETED,result.error
    return stop,query


def complete_context(owned,action_id):
    from camctl.contracts.history_values import TransactionRange
    from camctl.history.reads import ReadCoverage
    from camctl.history.validators import EventContext
    runs={identity:row_facts(owned.connection,'operation_runs',identity) for identity, in
        owned.connection.execute('SELECT id FROM operation_runs WHERE action_id=?',(action_id,))}
    activities={identity:row_facts(owned.connection,'device_activities',identity) for identity, in
        owned.connection.execute('SELECT id FROM device_activities WHERE action_id=?',(action_id,))}
    targets={run['activity_id'] for run in runs.values() if run['activity_id'] is not None}
    for identity in targets: activities[identity]=row_facts(owned.connection,'device_activities',identity)
    attempts={identity:row_facts(owned.connection,'operation_attempts',identity) for run_id in runs for identity, in
        owned.connection.execute('SELECT id FROM operation_attempts WHERE run_id=?',(run_id,))}
    owners={('device_activities',identity):('action',row['action_id']) for identity,row in activities.items()}
    return EventContext(TransactionRange(1,1,1),owners,
        {'device_activities':activities,'operation_runs':runs,'operation_attempts':attempts},
        read_coverage=ReadCoverage({('device_activities','action_id'):frozenset((action_id,)),
            ('device_activities','id'):frozenset(targets),('operation_runs','action_id'):frozenset((action_id,)),
            ('operation_attempts','run_id'):frozenset(runs)}))


def environment(tmp_path,*,own_activity=True):
    owned=_seed_environment(tmp_path)
    _seed_plan(owned.connection,1)
    _seed_plan(owned.connection,2)
    _seed_record_action(owned.connection,11,2,status=6,cancel_requested=1)
    _seed_activity(owned.connection,11,dispatch_state=2,activity_state=2)
    _seed_record_action(owned.connection,12,1,status=2 if own_activity else 1)
    if own_activity: _seed_activity(owned.connection,12)
    owned.connection.commit()
    return owned


@pytest.mark.parametrize('phase',['stop_inflight','stop_returned','stop_ended','confirm_inflight','confirm_returned','confirm_ended'])
@pytest.mark.parametrize('start_confirmation',[False,True])
def test_foreign_residual_scope_keeps_sql_and_rows_start_facts_equal(tmp_path,phase,start_confirmation):
    from camctl.persistence.repositories.cancellation import _source_start_rows
    owned=environment(tmp_path)
    try:
        stop,query=residual(owned,12,11,_NOW,phase)
        if start_confirmation:
            save_generic_no_effect_start(owned,12,12,_NOW)
            begin(owned,12,OperationKind.QUERY_ACTIVITY,12,QueryPurpose.START_CONFIRMATION,_NOW)
        action=row_facts(owned.connection,'actions',12)
        assert row_facts(owned.connection,'actions',11)['plan_id']!=action['plan_id']
        sql=load_start_facts(owned.connection,action)
        assert sql.not_started is (not start_confirmation)
        before=tuple(owned.connection.iterdump())
        rows,_=_source_start_rows(complete_context(owned,12),action)
        assert rows==sql
        assert tuple(owned.connection.iterdump())==before
        assert stop.target_id=='11' and (query is None or query.target_id=='11')
        assert row_facts(owned.connection,'device_activities',11)['action_id']==11
    finally:
        owned.connection.close()


def pure_args(context,action_id):
    runs=context.state_rows['operation_runs']
    own=[row for row in context.state_rows['device_activities'].values() if row['action_id']==action_id]
    targets=[row for row in context.state_rows['device_activities'].values() if row['action_id']!=action_id]
    attempts={identity:[row for row in context.state_rows['operation_attempts'].values() if row['run_id']==identity]
        for identity in runs}
    return own,list(runs.values()),attempts,targets


@pytest.mark.parametrize('preflight',[False,True])
@pytest.mark.parametrize('phase',['stop_inflight','confirm_ended'])
def test_foreign_residual_and_preflight_do_not_require_trigger_activity(tmp_path,phase,preflight):
    from camctl.persistence.repositories.cancellation import _source_start_rows
    owned=environment(tmp_path,own_activity=False)
    try:
        residual(owned,12,11,_NOW,phase)
        if preflight:
            ticket=begin(owned,12,OperationKind.QUERY_ACTIVITY,None,QueryPurpose.BEFORE_EXECUTION,_NOW)
            assert ticket.target_id is None
        action=row_facts(owned.connection,'actions',12)
        sql=load_start_facts(owned.connection,action)
        rows,_=_source_start_rows(complete_context(owned,12),action)
        assert rows==sql and rows.not_started and rows.activity is None and rows.run is None
        assert owned.connection.execute('SELECT id FROM device_activities WHERE action_id=12').fetchall()==[]
    finally:
        owned.connection.close()


@pytest.mark.parametrize('purpose',[QueryPurpose.RESIDUAL_STOP_CONFIRMATION,QueryPurpose.START_CONFIRMATION])
def test_pending_query_is_only_a_complete_row_domain_input(tmp_path,purpose):
    from camctl.persistence.repositories.capture_facts import start_facts_from_rows
    owned=environment(tmp_path)
    try:
        residual(owned,12,11,_NOW,'confirm_inflight')
        if purpose is QueryPurpose.START_CONFIRMATION:
            save_generic_no_effect_start(owned,12,12,_NOW)
            begin(owned,12,OperationKind.QUERY_ACTIVITY,12,purpose,_NOW)
        context=complete_context(owned,12)
        own,runs,attempts,targets=pure_args(context,12)
        query=next(run for run in runs if run['query_purpose']==(2 if purpose is QueryPurpose.START_CONFIRMATION else 5))
        # 普通仓储直接建 ACTIVE；这里只核允许域的 PENDING/零尝试完整行，不写 SQL。
        query.update(status=1,attempts_used=0)
        attempts[query['id']]=[]
        before=tuple(owned.connection.iterdump())
        facts=start_facts_from_rows(row_facts(owned.connection,'actions',12),own,runs,attempts,activity_targets=targets)
        assert facts.not_started is (purpose is QueryPurpose.RESIDUAL_STOP_CONFIRMATION)
        assert tuple(owned.connection.iterdump())==before
    finally:
        owned.connection.close()


@pytest.mark.parametrize('phase',['active','returned','ended'])
def test_only_unfinished_original_start_confirmation_blocks_start_partition(tmp_path,phase):
    from camctl.persistence.repositories.cancellation import _source_start_rows
    owned=environment(tmp_path)
    try:
        residual(owned,12,11,_NOW,'confirm_inflight')
        save_generic_no_effect_start(owned,12,12,_NOW)
        query=begin(owned,12,OperationKind.QUERY_ACTIVITY,12,QueryPurpose.START_CONFIRMATION,_NOW)
        if phase!='active': returned(owned,query,_NOW)
        if phase=='ended':
            result=OperationRepository().finish_stale_runs(StaleRunFinish((query.responsibility_key,),
                RunOutcome.UNCONFIRMED,_NOW,ErrorValue('result_unconfirmed','device')),new_operation_key(),owned)
            assert result.kind is DbOutcomeKind.COMPLETED,result.error
        action=row_facts(owned.connection,'actions',12)
        rows,_=_source_start_rows(complete_context(owned,12),action)
        assert rows==load_start_facts(owned.connection,action)
        assert rows.not_started is (phase=='ended')
    finally:
        owned.connection.close()


@pytest.mark.parametrize('fault',['start_target','start_kind','residual_target','residual_key','residual_kind',
    'missing_stop','missing_target','target_range','run_range','attempt_range','run_identity',
    'attempt_identity','attempt_owner','attempt_count','target_identity'])
def test_complete_residual_scope_rejects_wrong_responsibility_identity_and_ranges(tmp_path,fault):
    from copy import deepcopy
    from dataclasses import replace
    from camctl.history.reads import ReadCoverage
    from camctl.history.validators import EventValidationError
    from camctl.persistence.repositories.cancellation import _source_start_rows
    owned=environment(tmp_path)
    try:
        stop,query=residual(owned,12,11,_NOW,'confirm_inflight')
        save_generic_no_effect_start(owned,12,12,_NOW)
        action=row_facts(owned.connection,'actions',12)
        context=complete_context(owned,12)
        assert _source_start_rows(context,action)[0]==load_start_facts(owned.connection,action)
        state=deepcopy(context.state_rows)
        coverage=dict(context.read_coverage.ranges)
        runs=state['operation_runs']
        start=next(row for row in runs.values() if row['responsibility_key']=='start/12')
        if fault=='start_target': start['activity_id']=11
        elif fault=='start_kind': start['kind']=2
        elif fault=='residual_target':
            runs[query.run_id].update(activity_id=12,responsibility_key='query/residual/12/12')
        elif fault=='residual_key': runs[stop.run_id]['responsibility_key']='followup/12/12'
        elif fault=='residual_kind': runs[stop.run_id]['kind']=2
        elif fault=='missing_stop':
            runs.pop(stop.run_id)
            state['operation_attempts']={key:row for key,row in state['operation_attempts'].items() if row['run_id']!=stop.run_id}
        elif fault=='missing_target': state['device_activities'].pop(11)
        elif fault=='target_range': coverage.pop(('device_activities','id'))
        elif fault=='run_range': coverage.pop(('operation_runs','action_id'))
        elif fault=='attempt_range': coverage[('operation_attempts','run_id')]=frozenset((start['id'],stop.run_id))
        elif fault=='run_identity': runs[query.run_id]['id']=1000
        elif fault=='target_identity': state['device_activities'][11]['id']=1000
        else:
            attempt=next(row for row in state['operation_attempts'].values() if row['run_id']==query.run_id)
            if fault=='attempt_identity': attempt['id']=1000
            elif fault=='attempt_owner': attempt['run_id']=stop.run_id
            else: attempt['attempt_no']=2
        broken=replace(context,state_rows=state,read_coverage=ReadCoverage(coverage))
        before=tuple(owned.connection.iterdump())
        with pytest.raises(EventValidationError): _source_start_rows(broken,action)
        assert tuple(owned.connection.iterdump())==before
    finally:
        owned.connection.close()


def test_complete_run_input_rejects_a_different_trigger_owner(tmp_path):
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.repositories.capture_facts import start_facts_from_rows
    owned=environment(tmp_path)
    try:
        _,query=residual(owned,12,11,_NOW,'confirm_inflight')
        own,runs,attempts,targets=pure_args(complete_context(owned,12),12)
        action=row_facts(owned.connection,'actions',12)
        assert start_facts_from_rows(action,own,runs,attempts,activity_targets=targets)==load_start_facts(owned.connection,action)
        next(run for run in runs if run['id']==query.run_id)['action_id']=11
        with pytest.raises(ConsistencyError):
            start_facts_from_rows(action,own,runs,attempts,activity_targets=targets)
    finally:
        owned.connection.close()


def test_actual_cancel_loader_keeps_foreign_target_owner_and_exact_coverage(tmp_path):
    from camctl.contracts.history_values import TransactionRange
    from camctl.history.validators import EventContext
    from camctl.persistence.repositories.cancellation import include_photo_entry_rows,_source_start_rows
    owned=environment(tmp_path)
    try:
        residual(owned,12,11,_NOW,'confirm_inflight')
        action=row_facts(owned.connection,'actions',12)
        state,owners={},{}
        coverage=include_photo_entry_rows(owned.connection,action,state,owners)
        assert owners[('device_activities',11)]==('action',11)
        assert state['device_activities'][11]==row_facts(owned.connection,'device_activities',11)
        assert coverage.covers('device_activities','id',11)
        assert not coverage.covers('device_activities','action_id',11)
        assert set(state)=={'device_activities','operation_runs','operation_attempts'}
        context=EventContext(TransactionRange(1,1,1),owners,state,read_coverage=coverage)
        assert _source_start_rows(context,action)[0]==load_start_facts(owned.connection,action)
    finally:
        owned.connection.close()


def test_copy_and_cleanup_fixed_combinations_do_not_require_parent_reads(tmp_path):
    from copy import deepcopy
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.repositories.capture_facts import start_facts_from_rows
    owned=environment(tmp_path)
    try:
        residual(owned,12,11,_NOW,'stop_inflight')
        context=complete_context(owned,12)
        own,runs,_,_=pure_args(context,12)
        # 仅核普通 run 固定组合的有限输入；不构造实际文件业务或伪造其父对象。
        for kind,key,column in ((3,'read/901','copy_id'),(4,'delete/901','cleanup_item_id'),(5,'exists/901','cleanup_item_id')):
            run=deepcopy(runs[0])
            run.update(kind=kind,activity_id=None,query_purpose=None,responsibility_key=key,
                status=1,attempts_used=0,**{column:901})
            args=(row_facts(owned.connection,'actions',12),own,[run],{run['id']:[]})
            assert start_facts_from_rows(*args).not_started
            run['responsibility_key']='wrong/901'
            with pytest.raises(ConsistencyError): start_facts_from_rows(*args)
    finally:
        owned.connection.close()



def emergency_environment(tmp_path,with_attempt):
    from camctl.capture.recovery import EmergencyOutcome,EmergencyRecord
    from camctl.persistence.repositories.capture import CaptureRepository,register_capture_guards
    register_capture_guards()
    owned=_seed_environment(tmp_path)
    _seed_plan(owned.connection,1)
    _seed_plan(owned.connection,2)
    for identity,plan in ((11,2),(12,1)):
        _seed_record_action(owned.connection,identity,plan,status=6,cancel_requested=1)
        _seed_activity(owned.connection,identity,dispatch_state=2,activity_state=2)
    owned.connection.commit()
    attempt={'status':4,'effect_state':1,'result_json':{'format_version':1,
        'settlement':{'basis':'observed','evidence':{'type':'operation_returned','version':1,'data':{}}},
        'observations':[]},'error_json':{'code':'timeout','stage':'transport'}}
    result=CaptureRepository().save_emergency(session_key='a'*32,action_id=12,activity_id=12,
        record=EmergencyRecord(EmergencyOutcome.UNCONFIRMED if with_attempt else EmergencyOutcome.NOT_ATTEMPTED,
            int(with_attempt),2,reason='停止结果未确认' if with_attempt else '未取得停止执行条件'),
        attempts=(attempt,) if with_attempt else (),occurred_at=_NOW,key=new_operation_key(),owned=owned,
        timeout_s=Decimal(10),retry_interval_s=Decimal(1))
    assert result.kind is DbOutcomeKind.COMPLETED,result.error
    return owned


@pytest.mark.parametrize('with_attempt',[False,True])
def test_saved_emergency_record_keeps_sql_and_rows_start_facts_equal(tmp_path,with_attempt):
    from camctl.persistence.repositories.cancellation import _source_start_rows
    owned=emergency_environment(tmp_path,with_attempt)
    try:
        action=row_facts(owned.connection,'actions',12)
        before=tuple(owned.connection.iterdump())
        rows,_=_source_start_rows(complete_context(owned,12),action)
        assert rows==load_start_facts(owned.connection,action)
        assert not rows.not_started and rows.run is None and rows.attempts_used==0
        assert tuple(owned.connection.iterdump())==before
    finally:
        owned.connection.close()


@pytest.mark.parametrize('fault',['owner','missing_target','attempt_count'])
def test_emergency_scope_keeps_original_owner_target_and_attempt_checks(tmp_path,fault):
    from copy import deepcopy
    from dataclasses import replace
    from camctl.history.validators import EventValidationError
    from camctl.persistence.repositories.cancellation import _source_start_rows
    owned=emergency_environment(tmp_path,True)
    try:
        action=row_facts(owned.connection,'actions',12)
        context=complete_context(owned,12)
        assert _source_start_rows(context,action)[0]==load_start_facts(owned.connection,action)
        state=deepcopy(context.state_rows)
        if fault=='owner':
            run=next(iter(state['operation_runs'].values()))
            run.update(activity_id=11,responsibility_key='emergency/'+('a'*32)+'/11')
            state['device_activities'][11]=row_facts(owned.connection,'device_activities',11)
            from camctl.history.reads import ReadCoverage
            context=replace(context,read_coverage=ReadCoverage({**context.read_coverage.ranges,
                ('device_activities','id'):frozenset((11,12))}))
        elif fault=='missing_target': state['device_activities'].pop(12)
        else: next(iter(state['operation_attempts'].values()))['attempt_no']=2
        with pytest.raises(EventValidationError): _source_start_rows(replace(context,state_rows=state),action)
    finally:
        owned.connection.close()

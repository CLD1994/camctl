"""从原已保存来源独立核实并保存有限结果决定。"""
from camctl.capture.segment_storage import (
    CurrentSegmentResult,PublicationKind,ResultCheckConclusionRef,ResultCheckConclusionSave,
    ResultCheckEndReason,SegmentResultRef,SegmentSourceKind,decode_enum,document,encode_enum)
from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError,OperationKey
from camctl.history.events import load_event_registry
from camctl.history.reads import SegmentReadScope
from camctl.persistence.transaction import (
    CommandPlan,TransactionScope,event_envelope,next_row_id,row_change,row_facts,saved_transaction_events)
from .segment_result_sets import _boundary,_one,_read_transaction,read_result_set

_EVENT=load_event_registry()['events']['SEGMENT_FACTS']
_REASON=_EVENT['branches']['CONCLUDE_CHECK']['reason']


def _values(db,request,setrow):
    if (request.reason is not ResultCheckEndReason.NO_RESULT_WORK
            or setrow['source_kind']!=encode_enum(SegmentSourceKind.NO_RESULTS)
            or setrow['publication_kind']!=encode_enum(PublicationKind.RESULT)
            or setrow['state']!=int(enum_for('segment_sets.state').MEMBERS_FIXED)
            or setrow['member_count']!=0 or request.anchor_event_id is None
            or any(value is not None for value in (request.prior_run_end_event_id,
                request.check_config,request.binding_failure,request.prior_conclusion_id))):
        raise ConsistencyError('该申请不是无RESULTS的原基准结束决定')
    action=row_facts(db,'actions',request.ref.action_id)
    activity=row_facts(db,'device_activities',setrow['activity_id'])
    if (action['status']!=int(enum_for('actions.status').RUNNING) or action['execution_started']!=1
            or action['cancel_requested'] or activity is None
            or activity['dispatch_state']!=int(enum_for('device_activities.dispatch_state').NOT_DISPATCHED)):
        raise ConsistencyError('基准结束决定缺少原运行中未派发资格')
    kinds=enum_for('operation_runs.kind')
    if _one(db,'SELECT id FROM operation_runs WHERE action_id=? AND kind IN (?,?) LIMIT 1',
            (request.ref.action_id,int(kinds.START),int(kinds.CHECK_CAPTURE_RESULTS))) is not None:
        raise ConsistencyError('已有START或RESULTS责任，不能采用基准结束分区')
    call=_one(db,'SELECT id FROM baseline_read_calls WHERE action_id=? AND activity_id=? AND result_event_id=?',
        (request.ref.action_id,setrow['activity_id'],request.anchor_event_id))
    if call is None:raise ConsistencyError('基准决定缺少同动作实际结果依据')
    from .baseline_reads import read_failure_anchor
    class Reader:connection=db
    boundary=HistoryBoundary(setrow['source_txn_id'],setrow['source_event_id'])
    actual=read_failure_anchor(request.ref.action_id,call[0],boundary,Reader)
    if actual is None or actual.result_event_id!=request.anchor_event_id:
        raise ConsistencyError('基准决定不是原H0内的可靠错误或返回未知')
    return dict(set_id=request.ref.set_id,reason=encode_enum(request.reason),
        anchor_event_id=request.anchor_event_id,prior_run_end_event_id=None,config_event_id=None,
        prior_conclusion_id=None,current_result=encode_enum(CurrentSegmentResult.NONE),current_failure_json=None)


def _request(raw):
    return ResultCheckConclusionSave(SegmentResultRef(**raw['ref']),
        decode_enum(ResultCheckEndReason,raw['reason']),raw['anchor_event_id'],raw['prior_run_end_event_id'],
        raw['check_config'],raw['binding_failure'],raw['prior_conclusion_id'],raw['occurred_at'])


def verify_conclusion_event(db,ref,event,context):
    request=_request(event.evidence['result_check_decision'])
    if (request.ref!=ref or request.occurred_at!=event.occurred_at
            or context.transaction.first_event_id!=event.event_id
            or context.transaction.last_event_id!=event.event_id):
        raise ConsistencyError('无RESULTS决定必须是原完整申请的独立事务')
    setrow=read_result_set(ref,context.current_reads)
    expected={**_values(db,request,setrow),'created_event_id':event.event_id}
    if (event.reason!=_REASON or event.rows!=(row_change('segment_check_conclusions',
            next_row_id(db,'segment_check_conclusions'),expected),)
            or not json_equal(event.evidence,{'result_check_decision':document(request)})):
        raise ConsistencyError('有限决定事件与实际来源、原申请或分配身份不同')


def verify_result_check_conclusion(request,key,owned,*,execution_finished=True):
    command=ConcludeResultCheckCommand(request,key)
    if not execution_finished:raise ConsistencyError('原有限决定保存尚未收场，不能核实缺键')
    with _read_transaction(owned) as db:
        if saved_transaction_events(db,OperationKey(key)) is None:return None
        boundary=_boundary(db)
        scope=TransactionScope(db,boundary.txn_id,boundary.last_event_id)
        plan=command.plan(scope)
        if not plan.read_only or scope.allocation is not None:
            raise ConsistencyError('原有限决定核实不能产生新写入')
        return plan.result


class ConcludeResultCheckCommand:
    def __init__(self,request,key):
        if not isinstance(request,ResultCheckConclusionSave):raise TypeError('有限决定需要完整原申请')
        self.request,self.key=request,OperationKey(key)

    def plan(self,scope):
        db,request=scope.connection,self.request
        evidence={'result_check_decision':document(request)}
        saved=saved_transaction_events(db,self.key)
        if saved is not None:
            event=saved[0]
            if (len(saved)!=1 or event['type']!=_EVENT['id'] or event['reason']!=_REASON
                    or event['occurred_at']!=request.occurred_at or not json_equal(event['body']['evidence'],evidence)
                    or len(event['body']['rows'])!=1):
                raise ConsistencyError('原有限决定的完整申请或事件不同')
            change=event['body']['rows'][0]
            row=row_facts(db,'segment_check_conclusions',change['id'])
            if (change['table']!='segment_check_conclusions' or change['before']['exists']
                    or row is None or row['set_id']!=request.ref.set_id
                    or not json_equal({name:row[name] for name in change['after']['values']},change['after']['values'])):
                raise ConsistencyError('原有限决定与实际不可变保存行不同')
            return CommandPlan((),{},{},ResultCheckConclusionRef(request.ref,row['id'],event['event_id']),True)
        setrow=read_result_set(request.ref,scope)
        values=_values(db,request,setrow)
        allocation=scope.allocate(1)
        identity=next_row_id(db,'segment_check_conclusions')
        event=event_envelope(allocation.first_event_id,allocation.txn_id,_EVENT['id'],_REASON,
            (row_change('segment_check_conclusions',identity,{**values,'created_event_id':allocation.first_event_id}),),
            request.occurred_at,evidence)
        return CommandPlan((event,),{('segment_check_conclusions',identity):('action',request.ref.action_id)},
            {'actions':{request.ref.action_id:row_facts(db,'actions',request.ref.action_id)},
                'segment_sets':{request.ref.set_id:setrow},'segment_check_conclusions':{}},
            ResultCheckConclusionRef(request.ref,identity,allocation.first_event_id),
            segment_read_scope=SegmentReadScope(request.ref))

"""从原已保存来源独立核实并保存有限结果决定。"""
from contextlib import closing
from dataclasses import replace
from camctl.capture.segment_storage import (
    CurrentSegmentResult,PublicationKind,ResultCheckConclusionRef,ResultCheckConclusionSave,
    ResultCheckEndReason,SegmentResultRef,SegmentSourceKind,decode_enum,document,encode_enum)
from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.json_values import json_equal,parse_exact_json
from camctl.contracts.values import ConsistencyError,OperationKey
from camctl.history.events import load_event_registry
from camctl.history.reads import ResultCheckWriteScope,SegmentReadScope
from camctl.persistence.transaction import (
    CommandPlan,TransactionScope,event_envelope,next_row_id,row_change,row_facts,saved_transaction_events,update_change)
from .segment_result_sets import _boundary,_one,_read_transaction,read_result_set

_EVENT=load_event_registry()['events']['SEGMENT_FACTS']
_REASON=_EVENT['branches']['CONCLUDE_CHECK']['reason']
_RUN_EVENT=load_event_registry()['events']['OPERATION_CONFIGURED']
_CONFIGURE=_RUN_EVENT['branches']['CONFIGURE']['reason']
_FINISH=_RUN_EVENT['branches']['FINISH']['reason']
_CONFIG_COLUMNS=('max_attempts_used','timeout_s_json','retry_interval_s_json')


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
    from camctl.operations.attempts import AttemptConfig
    config=None if raw['check_config'] is None else AttemptConfig(**raw['check_config'])
    request=ResultCheckConclusionSave(SegmentResultRef(**raw['ref']),
        decode_enum(ResultCheckEndReason,raw['reason']),raw['anchor_event_id'],raw['prior_run_end_event_id'],
        config,raw['binding_failure'],raw['prior_conclusion_id'],raw['occurred_at'])
    if not json_equal(document(request),raw):raise ConsistencyError('有限决定证据不是完整规范申请')
    return request


def requires_check_group(db,event):
    if event.event_type==_EVENT['id'] and event.reason==_REASON:return True
    if event.event_type!=_RUN_EVENT['id'] or event.reason not in (_CONFIGURE,_FINISH):return False
    created=_EVENT['branches']['CREATE_RESULT']['reason']
    for row in event.rows:
        if row.table=='operation_runs' and _one(db,
                'SELECT s.id FROM operation_runs r JOIN segment_sets s ON s.action_id=r.action_id AND s.run_id=r.id'
                ' JOIN history_events e ON e.id=s.created_event_id'
                " WHERE r.id=? AND e.event_type=? AND json_extract(e.body_json,'$.reason')=? LIMIT 1",
                (row.row_id,_EVENT['id'],created)) is not None:
            return True
    return False


def _adopted_config_event(db,run):
    from camctl.contracts.enums import load_registry
    owner=load_registry()['history_objects']['action']['id']
    found=_one(db,'SELECT e.id FROM entity_event_links l JOIN history_events e ON e.id=l.event_id'
        ' JOIN json_each(e.body_json,\'$.rows\') r'
        " WHERE l.entity_type=? AND l.entity_id=? AND json_extract(r.value,'$.table')='operation_runs'"
        " AND json_extract(r.value,'$.id')=?"
        " AND (json_type(r.value,'$.after.values.max_attempts_used') IS NOT NULL"
        " OR json_type(r.value,'$.after.values.timeout_s_json') IS NOT NULL"
        " OR json_type(r.value,'$.after.values.retry_interval_s_json') IS NOT NULL) ORDER BY l.event_id DESC LIMIT 1",
        (owner,run['action_id'],run['id']))
    if found is None:raise ConsistencyError('原RESULTS缺少实际采用配置事件')
    return found[0]


def _ended_attempts(db,run,boundary):
    """按原票序逐项核实际结束，不形成全部尝试字典。"""
    from camctl.history.decoding import decode_event_row
    from .result_pages import _COLUMNS
    expected=1;last=None
    with closing(db.execute('SELECT id,attempt_no,status,result_event_id,result_json FROM operation_attempts'
            ' WHERE run_id=? ORDER BY attempt_no',(run['id'],))) as cursor:
        for identity,number,status,result_event,result in cursor:
            if (number!=expected or status==int(enum_for('operation_attempts.status').RUNNING)
                    or result_event is None or result_event>boundary.last_event_id or result is None):
                raise ConsistencyError('有限决定必须具有原run全部连续且实际结束的尝试')
            raw=_one(db,f'SELECT {_COLUMNS} FROM history_events WHERE id=?',(result_event,))
            actual=None if raw is None else decode_event_row(raw)
            if (actual is None or actual.event_type!=load_event_registry()['events']['ATTEMPT_RESULT']['id']
                    or not any(row.table=='operation_attempts' and row.row_id==identity
                        and row.after.values.get('result_event_id')==result_event
                        and row.after.values.get('status')==status
                        and json_equal(row.after.values.get('result_json'),parse_exact_json(result)) for row in actual.rows)):
                raise ConsistencyError('有限决定的尝试结果没有同票实际结束事件')
            expected+=1;last=result_event
    if last is None or expected-1!=run['attempts_used']:
        raise ConsistencyError('原RESULTS累计次数与全部实际票数不同')
    return last


def _prepare(db,request):
    class Reader:connection=db
    setrow=read_result_set(request.ref,Reader)
    if request.reason is ResultCheckEndReason.NO_RESULT_WORK:
        return setrow,None,(),_values(db,request,setrow)
    if (request.reason is not ResultCheckEndReason.RESULT_BUDGET_EXHAUSTED
            or setrow['source_kind']!=encode_enum(SegmentSourceKind.RETAINED_SOURCES)
            or setrow['publication_kind']!=encode_enum(PublicationKind.RESULT)
            or setrow['state']!=int(enum_for('segment_sets.state').MEMBERS_FIXED)
            or setrow['current_attempt_id'] is not None or setrow['current_final_page_event_id'] is not None
            or request.prior_run_end_event_id is not None or request.binding_failure is not None
            or request.prior_conclusion_id is not None or request.check_config is None):
        raise ConsistencyError('该有限决定不属于已实现的原RESULTS无可靠当前扫描耗尽分区')
    action=row_facts(db,'actions',request.ref.action_id)
    run=row_facts(db,'operation_runs',setrow['run_id'])
    if (action['status']!=int(enum_for('actions.status').RUNNING) or action['execution_started']!=1
            or action['cancel_requested'] or run is None or run['action_id']!=action['id']
            or run['activity_id']!=setrow['activity_id']
            or run['kind']!=int(enum_for('operation_runs.kind').CHECK_CAPTURE_RESULTS)
            or run['status'] not in (int(enum_for('operation_runs.status').ACTIVE),int(enum_for('operation_runs.status').PENDING))):
        raise ConsistencyError('原RESULTS不具备运行中且可有限收场的责任')
    last=_ended_attempts(db,run,HistoryBoundary(setrow['source_txn_id'],setrow['source_event_id']))
    config=request.check_config
    if (request.anchor_event_id!=last or config.timeout_s is None or config.retry_interval_s is None
            or run['attempts_used']<config.max_attempts):
        raise ConsistencyError('实际最后结果、完整采用配置或耗尽资格不成立')
    desired=dict(zip(_CONFIG_COLUMNS,(config.max_attempts,config.timeout_s,config.retry_interval_s)))
    changed={name:value for name,value in desired.items() if not json_equal(run[name],value)}
    prefix=[]
    if changed:prefix.append((_CONFIGURE,update_change('operation_runs',run['id'],
        {name:run[name] for name in changed},changed)))
    from .capture import result_run_close_row,_unconfirmed_run_error
    prefix.append((_FINISH,result_run_close_row(run,int(enum_for('operation_runs.status').UNCONFIRMED),
        _unconfirmed_run_error(run['activity_id']))))
    values=dict(set_id=request.ref.set_id,reason=encode_enum(request.reason),anchor_event_id=last,
        prior_run_end_event_id=None,config_event_id=None if changed else _adopted_config_event(db,run),
        prior_conclusion_id=None,current_result=encode_enum(CurrentSegmentResult.NONE),current_failure_json=None)
    return setrow,run,tuple(prefix),values


def _events(db,request,prepared,transaction):
    _,_,prefix,values=prepared
    if transaction.last_event_id-transaction.first_event_id!=len(prefix):
        raise ConsistencyError('有限决定的完整事务范围与适用配置和关闭不同')
    events=[event_envelope(transaction.first_event_id+offset,transaction.txn_id,_RUN_EVENT['id'],reason,
        (row,),request.occurred_at) for offset,(reason,row) in enumerate(prefix)]
    if prefix and prefix[0][0]==_CONFIGURE:values={**values,'config_event_id':transaction.first_event_id}
    events.append(event_envelope(transaction.last_event_id,transaction.txn_id,_EVENT['id'],_REASON,
        (row_change('segment_check_conclusions',next_row_id(db,'segment_check_conclusions'),
            {**values,'created_event_id':transaction.last_event_id}),),request.occurred_at,
        {'result_check_decision':document(request)}))
    return tuple(events)


class ResultCheckGroup:
    """原内核以同连接推导并封存至多三条规范事件。"""
    def __init__(self,scope,transaction,connection):
        if not isinstance(scope,ResultCheckWriteScope):raise ConsistencyError('有限决定缺少完整原申请范围')
        self.request,self.transaction,self.connection=scope.request,transaction,connection
        self.events=_events(connection,self.request,_prepare(connection,self.request),transaction)
        self.position=0;self.pending=None

    def bind(self,context,event):
        if (context.transaction!=self.transaction or not isinstance(context.current_reads.scope,SegmentReadScope)
                or context.current_reads.scope.ref!=self.request.ref
                or self.pending is not None or self.position>=len(self.events)
                or replace(event,change_seq=None)!=self.events[self.position]):
            raise ConsistencyError('有限决定组与原连接实际配置、关闭或决定顺序不同')
        self.pending=event

    def verify_decision(self,ref,event):
        if (ref!=self.request.ref or event.event_id!=self.transaction.last_event_id
                or self.pending is None or replace(event,change_seq=None)!=self.events[-1]):
            raise ConsistencyError('有限决定不是原完整组的实际末事件')

    def seal(self,validated):
        if self.pending is None or replace(validated.envelope,change_seq=None)!=self.events[self.position]:
            raise ConsistencyError('有限决定组缺少全部守卫通过的规范事件')
        self.pending=None;self.position+=1

    def finish(self):
        if self.pending is not None or self.position!=len(self.events):
            raise ConsistencyError('有限决定组缺少适用配置、关闭或唯一末决定')


def verify_conclusion_event(db,ref,event,context):
    request=_request(event.evidence['result_check_decision'])
    group=getattr(context.current_reads,'_result_check_group',None)
    if not isinstance(group,ResultCheckGroup) or request!=group.request:
        raise ConsistencyError('有限决定缺少原内核可靠完整组')
    group.verify_decision(ref,event)


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
            event=saved[-1]
            if (not 1<=len(saved)<=3 or event['type']!=_EVENT['id'] or event['reason']!=_REASON
                    or event['occurred_at']!=request.occurred_at or not json_equal(event['body']['evidence'],evidence)
                    or len(event['body']['rows'])!=1):
                raise ConsistencyError('原有限决定的完整申请或事件不同')
            change=event['body']['rows'][0]
            row=row_facts(db,'segment_check_conclusions',change['id'])
            from camctl.history.events import business_columns
            if (change['table']!='segment_check_conclusions' or change['before']['exists']
                    or row is None or row['set_id']!=request.ref.set_id
                    or row['created_event_id']!=event['event_id']
                    or not json_equal({name:row[name] for name in business_columns('segment_check_conclusions')},change['after']['values'])):
                raise ConsistencyError('原有限决定与实际不可变保存行不同')
            _verify_saved_prefix(db,request,saved,row)
            return CommandPlan((),{},{},ResultCheckConclusionRef(request.ref,row['id'],event['event_id']),True)
        prepared=_prepare(db,request)
        setrow,run,prefix,_=prepared
        allocation=scope.allocate(len(prefix)+1)
        events=_events(db,request,prepared,allocation)
        identity=events[-1].rows[0].row_id
        owners={('segment_check_conclusions',identity):('action',request.ref.action_id)}
        state={'actions':{request.ref.action_id:row_facts(db,'actions',request.ref.action_id)},
            'segment_sets':{request.ref.set_id:setrow},'segment_check_conclusions':{}}
        if run is not None:
            owners[('operation_runs',run['id'])]=('action',request.ref.action_id)
            state['operation_runs']={run['id']:run}
            state['device_activities']={run['activity_id']:row_facts(db,'device_activities',run['activity_id'])}
        return CommandPlan(events,owners,state,
            ResultCheckConclusionRef(request.ref,identity,allocation.last_event_id),
            segment_read_scope=SegmentReadScope(request.ref),result_check_write_scope=ResultCheckWriteScope(request))


def _verify_saved_prefix(db,request,saved,conclusion):
    prefix=saved[:-1]
    class Reader:connection=db
    setrow=read_result_set(request.ref,Reader)
    if request.reason is ResultCheckEndReason.NO_RESULT_WORK:
        if prefix:raise ConsistencyError('原无RESULTS基准决定不能混入其他事件')
        return
    if (request.reason is not ResultCheckEndReason.RESULT_BUDGET_EXHAUSTED or len(prefix) not in (1,2)
            or request.check_config is None):
        raise ConsistencyError('原有限决定不是已实现的完整组')
    run=row_facts(db,'operation_runs',setrow['run_id'])
    config=request.check_config
    desired=dict(zip(_CONFIG_COLUMNS,(config.max_attempts,config.timeout_s,config.retry_interval_s)))
    if run is None or any(not json_equal(run[name],value) for name,value in desired.items()):
        raise ConsistencyError('原有限决定的实际采用配置不同')
    for event in prefix:
        rows=event['body']['rows']
        if (event['type']!=_RUN_EVENT['id'] or event['occurred_at']!=request.occurred_at
                or event['body']['evidence'] or len(rows)!=1 or rows[0]['table']!='operation_runs'
                or rows[0]['id']!=run['id'] or not rows[0]['before']['exists'] or not rows[0]['after']['exists']):
            raise ConsistencyError('原有限决定的前序事件身份或完整行不同')
    if len(prefix)==2:
        event=prefix[0];row=event['body']['rows'][0]
        before,after=row['before']['values'],row['after']['values']
        if (event['reason']!=_CONFIGURE or not after or set(before)!=set(after)
                or not set(after)<=set(_CONFIG_COLUMNS)
                or any(not json_equal(value,desired[name]) or json_equal(value,before[name]) for name,value in after.items())
                or conclusion['config_event_id']!=event['event_id']):
            raise ConsistencyError('原有限决定的配置事件不等于原完整申请')
    elif (conclusion['config_event_id'] is None or conclusion['config_event_id']>=prefix[0]['event_id']
            or conclusion['config_event_id']!=_adopted_config_event(db,run)):
        raise ConsistencyError('复用配置缺少原先实际采用事件')
    if (_ended_attempts(db,run,HistoryBoundary(setrow['source_txn_id'],setrow['source_event_id']))!=request.anchor_event_id
            or run['attempts_used']<config.max_attempts or conclusion['anchor_event_id']!=request.anchor_event_id):
        raise ConsistencyError('原有限决定的实际结果或已采用耗尽资格不同')
    event=prefix[-1];row=event['body']['rows'][0]
    from .capture import _unconfirmed_run_error
    expected={'status':int(enum_for('operation_runs.status').UNCONFIRMED),'retry_wait_required':0,
        'error_json':_unconfirmed_run_error(run['activity_id'])}
    if (event['reason']!=_FINISH or set(row['before']['values'])!=set(expected)
            or row['before']['values']['status'] not in (1,2) or not json_equal(row['after']['values'],expected)
            or any(not json_equal(run[name],value) for name,value in expected.items())):
        raise ConsistencyError('原有限决定缺少原RESULTS的可靠完整关闭')

"""分段完整收场：有限引用、原连接逐事件提交及原键逐页核实。"""
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace
from itertools import zip_longest

from camctl.capture.segment_models import SegmentSetRef, SegmentAssessmentStatus, DefaultScopeState
from camctl.capture.segment_finish import FinishSegmentRecording, SegmentFinishResult, validate_finish_header, request_document
from camctl.capture.segment_finish import SavedSegmentCompletionProof
from camctl.capture.segment_storage import document, assessment_from_document, facts_from_document
from camctl.capture.segment_errors import segment_error_header
from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import HistoryBoundary, TransactionRange
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError
from camctl.contracts.workflow_errors import registered_error
from camctl.history.events import load_event_registry, foreign_key_targets
from camctl.history.reads import PlanReadScope, ReadCoverage
from camctl.history.validators import EventContext
from camctl.persistence.event_reads import SqlEventReads
from camctl.persistence.transaction import (
    BoundedCommandPlan, CommandPlan, TransactionError, commit_operation, row_facts,
    row_change, update_change, event_envelope, next_row_id, read_transaction_range,
    read_saved_transaction_page,
)
from camctl.outputs.catalog import OutputKind
from . import segments as storage



def _one(db,sql,params=()):
    with closing(db.execute(sql,params)) as cursor:
        return cursor.fetchone()


def _candidates(request,reader,batch=128):
    cursor=None
    while True:
        page=storage.read_segment_output_candidates(request.ref,cursor,batch,reader)
        yield from page.items
        if page.next_cursor is None:return
        cursor=page.next_cursor


def _scopes(request,reader,batch=128):
    cursor=None
    while True:
        page=storage.read_segment_scope(request.ref,cursor,batch,reader,published=False)
        yield page.items
        if page.next_cursor is None:return
        cursor=page.next_cursor


def _verify_scope_members(request,assessment,reader):
    """将正式关联与冻结范围的实际有序原片逐项对应，不能只比计数。"""
    selected=assessment.default_scope
    def expected():
        if selected.state is not DefaultScopeState.NONEMPTY:return
        cursor=None
        while True:
            page=storage.read_segment_members(request.ref,cursor,128,reader)
            for stored in page.items:
                member=stored.member
                if member.cursor is None or not selected.first_cursor<=member.cursor<=selected.last_cursor:continue
                candidate=storage.segment_output_candidate(request.ref,stored.member_id,reader)
                if candidate is not None and candidate.kind is OutputKind.ORIGINAL:yield candidate.member_id
            if page.next_cursor is None:return
            cursor=page.next_cursor
    actual=(row for page in _scopes(request,reader) for row in page)
    count=0
    for member_id,row in zip_longest(expected(),actual):
        count+=1
        if row is None or member_id is None or row['member_id']!=member_id or row['ordinal']!=count:
            raise ConsistencyError('默认范围没有完整覆盖冻结选段的实际有序原片')
    if count!=(selected.member_count or 0):raise ConsistencyError('冻结选段计数与实际成员不同')


def _failure(request,assessment,original_count,setrow,db,*,before_event_id):
    from camctl.capture.segment_models import SegmentAssessmentReason
    facts=facts_from_document(setrow['facts_json'])
    failure=request.failure
    if assessment.status is SegmentAssessmentStatus.SUCCEEDED:
        if failure is not None:raise ConsistencyError('成功判断不能携带失败申请')
        return 3,None,None
    if failure is None:raise ConsistencyError('非成功完成必须保存原实际失败')
    reason=assessment.reason
    if reason is SegmentAssessmentReason.EXPLICIT_ERROR:
        anchor=request.failure_event_id
        if anchor is None or anchor>=before_event_id:
            raise ConsistencyError('明确错误必须引用完成组之前已保存的实际错误事件')
        from camctl.contracts.enums import load_registry
        owner=load_registry()['history_objects']['action']['id']
        if _one(db,'SELECT 1 FROM entity_event_links WHERE entity_type=? AND entity_id=? AND event_id=?',(owner,request.ref.action_id,anchor)) is None:
            raise ConsistencyError('原错误事件不属于来源动作')
        raw=_one(db,'SELECT id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json FROM history_events WHERE id=?',(anchor,))
        from camctl.history.decoding import decode_event_row
        event=decode_event_row(raw)
        # 只接受操作责任/实际尝试的明确错误列，不搜索任意嵌套正文。
        registry=load_event_registry()['events']
        matched=False
        for changed in event.rows:
            source_attempt=None
            if changed.table=='operation_runs' and event.event_type==registry['OPERATION_CONFIGURED']['id'] and event.reason==3:
                source_run=row_facts(db,'operation_runs',changed.row_id)
            elif changed.table=='operation_attempts' and event.event_type==registry['ATTEMPT_RESULT']['id']:
                source_attempt=row_facts(db,'operation_attempts',changed.row_id)
                source_run=None if source_attempt is None else row_facts(db,'operation_runs',source_attempt['run_id'])
                if source_attempt is None or source_attempt['result_event_id']!=anchor:continue
            else:continue
            error=changed.after.values.get('error_json')
            if (source_run is not None and source_run['action_id']==request.ref.action_id
                    and source_run['activity_id']==request.activity_id and isinstance(error,dict)
                    and ('activity_id' not in failure.details or failure.details['activity_id']==str(request.activity_id))
                    and ('operation_run_id' not in failure.details or failure.details['operation_run_id']==str(source_run['id']))
                    and error.get('code')==failure.code
                    and error.get('stage')==registered_error(failure.code)['stage']
                    and json_equal(error.get('details'),failure.details)):
                matched=True
            # 实际采集调用明确失败的既有公共包装：原实际错误仍只在
            # attempt.error_json，完成申请保存capture_failed/device_failed。
            if (source_attempt is not None and source_run is not None
                    and source_run['action_id']==request.ref.action_id and source_run['activity_id']==request.activity_id
                    and source_run['kind'] in tuple(int(enum_for('operation_runs.kind')[kind]) for kind in ('START','STOP','CHECK_CAPTURE_RESULTS'))
                    and source_attempt['status']==int(enum_for('operation_attempts.status').FAILED)
                    and isinstance(error,dict) and isinstance(error.get('code'),str)
                    and failure.code=='capture_failed'
                    and json_equal(failure.details,{'activity_id':str(request.activity_id),'reason':'device_failed'})):
                matched=True
        if not matched:raise ConsistencyError('明确错误与原run/attempt明确错误列的实际登记错误不同')
    elif reason is SegmentAssessmentReason.GAP:
        expected=segment_error_header(assessment,original_count).details('capture_segment_gap')
        if failure.code!='capture_segment_gap' or not json_equal(failure.details,expected):raise ConsistencyError('缺口错误与完整冻结判断不同')
    elif reason is SegmentAssessmentReason.NO_NONZERO_ORIGINAL:
        expected={'activity_id':str(request.activity_id),'reason':'no_outputs' if setrow['member_count']==0 else 'invalid_outputs'}
        if failure.code!='capture_failed' or not json_equal(failure.details,expected):raise ConsistencyError('必要原片缺失错误与实际冻结集合不同')
    else:
        if not facts.finite_check_complete:raise ConsistencyError('原有限核实责任尚未结束')
        completion_unknown=reason in (SegmentAssessmentReason.STOP_UNCONFIRMED,
            SegmentAssessmentReason.COMPLETION_UNCONFIRMED,SegmentAssessmentReason.LOWER_BOUND_INSUFFICIENT)
        expected={'activity_id':str(request.activity_id),'reason':'completion_unknown' if completion_unknown else 'outputs_unknown'}
        if failure.code!='capture_result_unconfirmed' or not json_equal(failure.details,expected):raise ConsistencyError('未确认结果与原有限核实分区不同')
    if reason is not SegmentAssessmentReason.EXPLICIT_ERROR and request.failure_event_id is not None:
        raise ConsistencyError('派生判断不接受无关原错误事件')
    return 4,registered_error(failure.code)['action_error_id'],failure.details


def _identity(request,db):
    action,activity,run,attempt,_=storage._source(db,request.ref)
    setrow=storage._set(db,request.ref,fixed=True)
    if activity['id']!=request.activity_id or run['id']!=request.run_id or setrow['activity_id']!=request.activity_id or setrow['run_id']!=request.run_id:
        raise ConsistencyError('完成申请改变原活动或RESULTS流程身份')
    if setrow['assessment_json'] is None:
        raise ConsistencyError('完成申请缺少原完整分段判断')
    return action,run,setrow,assessment_from_document(setrow['assessment_json'])


def verify_completion_proof(request,reader):
    """只采用原scan意图之前的实际控制和原页等待，不升级未知标志。"""
    from camctl.capture.segment_models import EvidenceState
    from camctl.devices.capture_timing import CaptureTiming
    from camctl.devices.recording_completion import completed_file_wait_source
    from .capture import _CaptureStopCommand
    from .result_sources import load_result_source,ResultSourceKind
    from camctl.history.decoding import decode_event_row
    db=reader.connection
    action,activity,run,scan,last=storage._source(db,request.ref)
    setrow=storage._set(db,request.ref,fixed=True)
    facts=facts_from_document(setrow['facts_json'])
    upper=scan['intent_event_id']
    stop_id=wait_id=control_id=elapsed=None
    stop_required=(facts.stop is EvidenceState.CONFIRMED
        or facts.control_completion is EvidenceState.CONFIRMED or facts.control_overrun is EvidenceState.CONFIRMED)
    if stop_required:
        found=_one(db,'SELECT t.id,t.result_event_id FROM operation_runs r JOIN operation_attempts t ON t.run_id=r.id '
            'WHERE r.responsibility_key=? AND r.action_id=? AND r.activity_id=? AND t.result_event_id<? '
            'AND t.status!=? AND t.effect_state=? ORDER BY t.result_event_id DESC LIMIT 1 /* segment_stop_proof */',
            (f"stop/{action['id']}",action['id'],activity['id'],upper,
             int(enum_for('operation_attempts.status').RUNNING),int(enum_for('operation_attempts.effect_state').CONFIRMED)))
        if found is None:raise ConsistencyError('冻结CONFIRMED停止缺少原scan之前的实际STOP证明')
        stopped,_=_CaptureStopCommand.source(db,found[1])
        stop_id=found[1]
        if stopped['id']!=request.activity_id:raise ConsistencyError('原STOP证明改变实际活动')
    wait_ms=action['execution_spec_json'].get('file_completion_wait_ms')
    if facts.file_wait is EvidenceState.CONFIRMED and wait_ms is not None:
        source=load_result_source(db,last.ref.ticket)
        if source.kind is not ResultSourceKind.STOP or source.context is None:
            raise ConsistencyError('冻结完成等待缺少原scan的可靠STOP来源')
        original_stop=source.context['stop_result_event_id']
        if stop_id is not None and original_stop!=stop_id:raise ConsistencyError('等待来源改变原STOP身份')
        settlement=last.page.outcome.settlement
        data=None if settlement is None else settlement.evidence.data
        prior=None
        source_id=None if data is None else data.get('file_completion_source_page_event_id')
        if source_id is not None:
            page,_=storage._at(db,source_id)
            if (page.ref.ticket.run_id!=last.ref.ticket.run_id
                    or page.ref.ticket.target_id!=last.ref.ticket.target_id
                    or page.ref.ticket.responsibility_key!=last.ref.ticket.responsibility_key
                    or page.ref.ticket.attempt_id>last.ref.ticket.attempt_id
                    or source_id>request.ref.final_page_event_id):
                raise ConsistencyError('实际等待来源页改变原RESULTS责任、活动或冻结上界')
            prior={'event_id':source_id,'activity_id':str(request.activity_id),
                'data':None if page.page.outcome.settlement is None else page.page.outcome.settlement.evidence.data}
        wait_id=completed_file_wait_source(data,page_event_id=request.ref.final_page_event_id,
            activity_id=str(request.activity_id),stop_result_event_id=original_stop,required_wait_ms=wait_ms,source_page=prior)
        if wait_id is None:raise ConsistencyError('冻结完成等待缺少原页可靠实际等待')
    if facts.control_completion is EvidenceState.CONFIRMED or facts.control_overrun is EvidenceState.CONFIRMED:
        elapsed=activity['control_elapsed_ns']
        if elapsed is None:raise ConsistencyError('冻结控制完成没有原活动可靠连续计时')
        from camctl.contracts.enums import load_registry
        owner=load_registry()['history_objects']['action']['id']
        event_type=load_event_registry()['events']['DEVICE_OBSERVED']['id']
        found=_one(db,'SELECT e.id,e.transaction_id,e.event_type,e.event_version,e.occurred_at,e.clock_status,e.change_seq,e.body_json '
            'FROM entity_event_links l JOIN history_events e ON e.id=l.event_id '
            'WHERE l.entity_type=? AND l.entity_id=? AND l.event_id<? AND e.event_type=? '
            "AND json_extract(e.body_json,'$.evidence.observation.stop_result_event_id')=? "
            "AND EXISTS(SELECT 1 FROM json_each(e.body_json,'$.rows') r WHERE json_extract(r.value,'$.table')='device_activities' "
            "AND json_extract(r.value,'$.id')=? AND json_extract(r.value,'$.after.values.control_elapsed_ns')=?) "
            'ORDER BY l.event_id DESC LIMIT 1',(owner,action['id'],upper,event_type,stop_id,activity['id'],elapsed))
        if found is None:raise ConsistencyError('原连续计时没有随原STOP在scan之前保存')
        control=decode_event_row(found);control_id=control.event_id
        timing=CaptureTiming(setrow['target_duration_ms'],action['execution_spec_json'].get('startup_margin_ms',0))
        lower=timing.recording_lower_bound_ns(elapsed)
        if facts.control_completion is EvidenceState.CONFIRMED and lower<timing.duration_ms*1_000_000:
            raise ConsistencyError('冻结控制完成超过原连续计时所能证明的下界')
        if facts.control_overrun is EvidenceState.CONFIRMED and lower<=timing.duration_ms*1_000_000:
            raise ConsistencyError('冻结控制多录没有原连续计时的严格超目标下界')
    return SavedSegmentCompletionProof(upper,stop_id,wait_id,control_id,elapsed)


def _header_request(header):
    validate_finish_header(header)
    from camctl.capture.media import RecordingFailure
    raw=header['request']
    return FinishSegmentRecording(SegmentSetRef(**raw['ref']),raw['activity_id'],raw['run_id'],raw['occurred_at'],
        None if raw['failure'] is None else RecordingFailure(**raw['failure']),raw['failure_event_id'])


def _has_freezable_scan(db,action_id):
    if _one(db,'SELECT 1 FROM segment_sets WHERE action_id=? LIMIT 1',(action_id,)) is not None:return True
    activity=_one(db,'SELECT id FROM device_activities WHERE action_id=?',(action_id,))
    if activity is None:return False
    after=None
    while True:
        predicate='' if after is None else ' AND t.attempt_no<?'
        parameters=(f'results/{activity[0]}',) if after is None else (f'results/{activity[0]}',after)
        latest=_one(db,'SELECT t.result_last_page_event_id,t.attempt_no FROM operation_runs r '
            'JOIN operation_attempts t ON t.run_id=r.id WHERE r.responsibility_key=? '
            'AND t.result_last_page_event_id IS NOT NULL'+predicate+' ORDER BY t.attempt_no DESC LIMIT 1',parameters)
        if latest is None:return False
        saved,_=storage._at(db,latest[0])
        page=saved.page
        if page.next_cursor is None and page.scan_complete and page.set_finalized and page.outcome.error is None:return True
        after=latest[1]


def _completion_qualification(db,request,*,before_event_id=None,transaction=None):
    """只恢复原组前的有限主行字段；兄弟以实际原status查存在性。"""
    action=row_facts(db,'actions',request.ref.action_id)
    run=row_facts(db,'operation_runs',request.run_id)
    plan=row_facts(db,'plans',action['plan_id'])
    if before_event_id is None:
        original_action=action;original_run=run;original_plan=plan
        pending=_one(db,'SELECT 1 FROM actions WHERE plan_id=? AND id!=? AND status NOT IN (3,4,5,6) LIMIT 1',
            (plan['id'],action['id'])) is not None
    else:
        from camctl.persistence.row_history import read_row_values_at_boundary
        from camctl.contracts.enums import load_registry
        if transaction is None or transaction.first_event_id!=before_event_id:
            raise ConsistencyError('原完成资格缺少实际已核完整事务')
        previous=HistoryBoundary(transaction.txn_id-1,before_event_id-1)
        head=_one(db,'SELECT id FROM history_transactions ORDER BY id DESC LIMIT 1')
        current=read_transaction_range(db,head[0],transaction if head[0]==transaction.txn_id else None)
        boundary=HistoryBoundary(current.txn_id,current.last_event_id)
        def restore(table,row,owner,columns):
            return read_row_values_at_boundary(db,owner=owner,table=table,row_id=row['id'],columns=frozenset(columns),
                current_values=row,boundary=previous,current_boundary=boundary,validated_transaction=transaction)
        original_action=restore('actions',action,('action',action['id']),('status','cancel_requested'))
        original_run=restore('operation_runs',run,('action',action['id']),('status','retry_wait_required','error_json'))
        original_plan=restore('plans',plan,('plan',plan['id']),('status',))
        owner=load_registry()['history_objects']['action']['id']
        after=0;pending=False
        while True:
            # status可能在受理创建时已终态，也可能由取消等事件改变；不只查ACTION_FINISHED。
            candidate=_one(db,"SELECT a.id FROM actions a WHERE a.plan_id=? AND a.id!=? AND a.id>? AND a.created_event_id<? "
                "AND COALESCE((SELECT json_extract(r.value,'$.after.values.status') FROM entity_event_links l "
                "JOIN history_events e ON e.id=l.event_id JOIN json_each(e.body_json,'$.rows') r "
                "WHERE l.entity_type=? AND l.entity_id=a.id AND l.event_id<? AND json_extract(r.value,'$.table')='actions' "
                "AND json_extract(r.value,'$.id')=a.id AND json_type(r.value,'$.after.values.status')='integer' "
                "ORDER BY l.event_id DESC LIMIT 1),0) NOT IN (3,4,5,6) ORDER BY a.id LIMIT 1",
                (plan['id'],action['id'],after,before_event_id,owner,before_event_id))
            if candidate is None:break
            sibling=row_facts(db,'actions',candidate[0])
            facts=restore('actions',sibling,('action',sibling['id']),('status',))
            if facts['status'] not in (3,4,5,6):pending=True;break
            after=sibling['id']
    if original_action['status']!=2 or original_action['cancel_requested']:
        raise ConsistencyError('原组前来源不具备未取消的普通执行资格')
    complete=original_plan['status'] in (1,2) and not pending
    return {'action_status':original_action['status'],'cancel_requested':original_action['cancel_requested'],
        'run_status':original_run['status'],'run_retry_wait_required':original_run['retry_wait_required'],
        'run_error_json':original_run['error_json'],'plan_status':original_plan['status'],
        'finish_run':original_run['status'] in (1,2),'complete_plan':complete,
        'plan_result_status':3 if complete else original_plan['status']}


def _verify_qualification_header(header,qualification):
    if (header['finish_run']!=qualification['finish_run'] or header['complete_plan']!=qualification['complete_plan']
            or header['plan_status']!=qualification['plan_result_status']):
        raise ConsistencyError('原RESULTS或父计划适用收场与可靠原F前事实不同')


def verify_finish_request_event(db,event,context,action_id):
    """从原保存事实决定适用域，再核原有限申请及最终提案结构。"""
    from camctl.devices.tasks import CaptureResultPolicy
    action=row_facts(db,'actions',action_id)
    header=event.evidence.get('segment_finish_request')
    segmented=(action['type']==2 and action['execution_spec_json'].get('result_policy')==CaptureResultPolicy.OSMO360II_RECORD_SEGMENTS_V1.value)
    changed=next(row for row in event.rows if row.table=='actions' and row.row_id==action_id)
    status=changed.after.values.get('status')
    required=segmented and status in (3,4) and (status==3 or _has_freezable_scan(db,action_id))
    if header is None:
        if required:raise ConsistencyError('已有分段完成依据的来源必须提交原完整分段申请和同组最终ACTIVATE')
        return
    if not segmented or not required:raise ConsistencyError('分段完成原申请不适用于该来源终态')
    request=_header_request(header)
    if (request.ref.action_id!=action_id or request.occurred_at!=event.occurred_at
            or event.event_id!=context.transaction.first_event_id or len(event.rows)!=1):
        raise ConsistencyError('原有限分段申请改变来源身份、实际时刻或首事件位置')
    action,run,setrow,assessment=_identity(request,db)
    if action['status']!=2 or action['cancel_requested'] or setrow['published_event_id'] is not None:
        raise ConsistencyError('分段终态缺少原普通执行资格')
    if _one(db,'SELECT 1 FROM outputs WHERE source_action_id=? LIMIT 1',(action_id,)) is not None:
        raise ConsistencyError('分段来源终态之前已有正式产物')
    if _one(db,'SELECT 1 FROM operation_attempts WHERE run_id=? AND status=1 LIMIT 1',(request.run_id,)) is not None:
        raise ConsistencyError('原RESULTS实际尝试尚未完成')
    reader=SimpleNamespace(connection=db)
    verify_completion_proof(request,reader)
    storage._verify_assessment(db,setrow,facts_from_document(setrow['facts_json']),assessment)
    _verify_scope_members(request,assessment,reader)
    originals=previews=0
    for candidate in _candidates(request,reader,header['batch_size']):
        if candidate.kind is OutputKind.ORIGINAL:originals+=1
        else:previews+=1
    scope_count=sum(len(page) for page in _scopes(request,reader,header['batch_size']))
    if (header['original_count'],header['preview_count'],header['scope_count'])!=(originals,previews,scope_count):
        raise ConsistencyError('原有限头没有完整覆盖冻结候选和范围')
    expected_status,error,details=_failure(request,assessment,originals,setrow,db,before_event_id=event.event_id)
    before={'status':2};after={'status':expected_status}
    if expected_status==4:
        before.update(error_code=action['error_code'],error_details_json=action['error_details_json'])
        after.update(error_code=error,error_details_json=details)
    if changed!=update_change('actions',action_id,before,after) or header['action_status']!=expected_status:
        raise ConsistencyError('来源终态与原冻结判断或实际有限失败不同')
    qualification=_completion_qualification(db,request)
    _verify_qualification_header(header,qualification)
    complete_plan=qualification['complete_plan']
    output_count=originals+previews
    if (header['first_output_id']!=(next_row_id(db,'outputs') if output_count else 1)
            or header['first_origin_id']!=(next_row_id(db,'output_origins') if previews else 1)):
        raise ConsistencyError('原完整组的产物或配对身份范围不同')
    expected_events=2+output_count+(scope_count+header['batch_size']-1)//header['batch_size']+int(header['finish_run'])+int(complete_plan)
    if context.transaction.last_event_id-context.transaction.first_event_id+1!=expected_events:
        raise ConsistencyError('原完整分段组没有预分配完整事件范围和最终ACTIVATE')
    storage._verify_final_activation(request.ref,setrow,event,context)
    final=context.transaction_rows['segment_sets'][setrow['id']]
    if final['published_event_id']!=context.transaction.last_event_id:
        raise ConsistencyError('原正式ACTIVATE不是完整完成组的最后事件')
    context.current_reads.prepare_segment_finish(event,context.transaction,header,qualification)


def _created_output(db,transaction,action_id,file_id,kind):
    # LIMIT 2 保留跨批重复检测；实际查找使用既有device_file_id唯一索引。
    with closing(db.execute(
        'SELECT id,created_event_id FROM outputs WHERE source_action_id=? AND device_file_id=? AND kind=? ORDER BY id LIMIT 2',
        (action_id,file_id,kind))) as cursor:
        rows=cursor.fetchmany(2)
    if len(rows)!=1 or not transaction.first_event_id<=rows[0][1]<=transaction.last_event_id:
        raise ConsistencyError('成员没有同完成组的唯一实际产物')
    event=_one(db,'SELECT event_type,transaction_id FROM history_events WHERE id=?',(rows[0][1],))
    if event!=(load_event_registry()['events']['OUTPUT_REGISTERED']['id'],transaction.txn_id):
        raise ConsistencyError('产物创建没有本完成组已验证登记事件')
    return rows[0][0]


def _run_finish_row(request,qualification):
    before={name:qualification['run_'+name] for name in ('status','retry_wait_required','error_json')}
    if before['status'] not in (1,2):raise ConsistencyError('原RESULTS不适用新的成功收场')
    return update_change('operation_runs',request.run_id,before,{'status':3,'retry_wait_required':0,'error_json':None})


def _verify_run_finish_event(event,request,responsibility_key,qualification):
    evidence={'stale_run_request':{'kind':'stale_run_finish','responsibility_keys':[responsibility_key],'status':3,'error':None}}
    if (event.event_type!=load_event_registry()['events']['OPERATION_CONFIGURED']['id'] or event.reason!=3
            or event.occurred_at!=request.occurred_at or event.clock_status!=2 or not json_equal(event.evidence,evidence)
            or event.rows!=(_run_finish_row(request,qualification),)):
        raise ConsistencyError('原RESULTS共同收场的完整事件或前后行不同')


def _verify_group_closure(db,request,header,transaction,marker_id,qualification):
    """最后具名守卫核实际先前收场，独立于命令complete回调。"""
    if marker_id!=transaction.last_event_id:raise ConsistencyError('正式ACTIVATE必须是完整组最后事件')
    _verify_qualification_header(header,qualification)
    from camctl.history.decoding import decode_event_row
    def saved(identity):
        raw=_one(db,'SELECT id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json FROM history_events WHERE id=?',(identity,))
        if raw is None or raw[1]!=transaction.txn_id:raise ConsistencyError('原收场位置没有同组实际已验证事件')
        return decode_event_row(raw)
    identity=transaction.first_event_id+1+header['output_count']+(header['scope_count']+header['batch_size']-1)//header['batch_size']
    run=row_facts(db,'operation_runs',request.run_id)
    if header['finish_run']:
        _verify_run_finish_event(saved(identity),request,run['responsibility_key'],qualification);identity+=1
        if (run['status'],run['retry_wait_required'],run['error_json'])!=(3,0,None):
            raise ConsistencyError('最后守卫没有原RESULTS实际共同收场')
    elif run['status'] not in (3,4,5,6,7):raise ConsistencyError('最后守卫仍有未结束原RESULTS责任')
    action=row_facts(db,'actions',request.ref.action_id);plan=row_facts(db,'plans',action['plan_id'])
    if header['complete_plan']:
        event=saved(identity);identity+=1
        if (event.event_type!=load_event_registry()['events']['PLAN_STATUS_CHANGED']['id'] or event.reason!=2
                or event.occurred_at!=request.occurred_at or event.clock_status!=2 or event.evidence or len(event.rows)!=1):
            raise ConsistencyError('最后守卫缺少同组实际父计划完成')
        expected=update_change('plans',plan['id'],{'status':qualification['plan_status']},{'status':3})
        if event.rows!=(expected,):raise ConsistencyError('原父计划共同完成的完整前后行不同')
    if plan['status']!=header['plan_status'] or identity!=marker_id:
        raise ConsistencyError('最后守卫的父计划结果或共同收场范围不同')


def verify_publication(db,ref,event,context):
    """独立核对已验证且已写投影；提案只用于查证唯一marker结构。"""
    setrow=storage._set(db,ref,fixed=True)
    storage._verify_final_activation(ref,setrow,event,context)
    if setrow['published_event_id'] is not None or _one(db,'SELECT 1 FROM segment_sets WHERE action_id=? AND published_event_id IS NOT NULL',(ref.action_id,)):
        raise ConsistencyError('动作已经存在正式分段集合')
    tx=context.transaction
    action=row_facts(db,'actions',ref.action_id)
    terminal=_one(db,'SELECT id FROM history_events WHERE transaction_id=? AND event_type=? ORDER BY id LIMIT 1',
        (tx.txn_id,load_event_registry()['events']['ACTION_FINISHED']['id']))
    if terminal is None or action['status'] not in (3,4,5,6):
        raise ConsistencyError('分段发布必须引用本完成组已验证来源终态')
    # 终态事件的实际身份必须准确对应本action，不能使用其它动作的终态。
    from camctl.history.decoding import decode_event_row
    raw=_one(db,'SELECT id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json FROM history_events WHERE id=?',(terminal[0],))
    accepted=decode_event_row(raw)
    if not any(row.table=='actions' and row.row_id==ref.action_id and row.after.values.get('status')==action['status'] for row in accepted.rows):
        raise ConsistencyError('来源终态不属于原完成组')
    assessment=assessment_from_document(setrow['assessment_json'])
    if action['status']==3 and assessment.status is not SegmentAssessmentStatus.SUCCEEDED:
        raise ConsistencyError('来源成功与冻结判断不同')
    if assessment.gap_count is None and (action['status']==3 or assessment.default_scope.state is not DefaultScopeState.UNKNOWN):
        raise ConsistencyError('缺口未知不能提升成功或默认范围')
    for changed in event.rows:
        if changed.table=='segment_scope_members':
            scope=row_facts(db,'segment_scope_members',changed.row_id)
            member=row_facts(db,'segment_members',scope['member_id'])
            if (scope['set_id']!=setrow['id'] or scope['output_id'] is not None
                    or _created_output(db,tx,ref.action_id,member['file_id'],int(enum_for('outputs.kind').ORIGINAL))!=changed.after.values['output_id']):
                raise ConsistencyError('范围关联没有同组已验证实际原片')
    if event.reason!=storage._EVENT['branches']['ACTIVATE']['reason']:return
    # 末组重新执行两遍生产分类和完整gap核对，不能只信任处理器摘要。
    storage._verify_assessment(db,setrow,facts_from_document(setrow['facts_json']),assessment)
    reader=SimpleNamespace(connection=db)
    count=originals=0
    header=accepted.evidence.get('segment_finish_request')
    request=_header_request(header)
    if request.ref!=ref or request.activity_id!=setrow['activity_id'] or request.run_id!=setrow['run_id'] or request.occurred_at!=event.occurred_at:
        raise ConsistencyError('最后守卫改变原完整分段申请')
    verify_completion_proof(request,reader)
    _verify_scope_members(request,assessment,reader)
    for candidate in _candidates(request,reader):
        kind=int(enum_for('outputs.kind')[candidate.kind.name])
        output_id=_created_output(db,tx,ref.action_id,candidate.file_id,kind)
        count+=1
        if candidate.kind is OutputKind.ORIGINAL:originals+=1
        else:
            original=_created_output(db,tx,ref.action_id,candidate.original_file_id,int(enum_for('outputs.kind').ORIGINAL))
            links=_one(db,'SELECT COUNT(*),MIN(original_output_id),MAX(original_output_id) FROM output_origins WHERE output_id=?',(output_id,))
            if links!=(1,original,original):raise ConsistencyError('预览未准确关联本组真实原片')
    # 双向覆盖：同数量的外来/重复/缺项产物不能通过。
    total=_one(db,'SELECT COUNT(*) FROM outputs WHERE source_action_id=?',(ref.action_id,))[0]
    if total!=count:raise ConsistencyError('完成组存在额外或重复产物')
    after=0
    while True:
        with closing(db.execute('SELECT id,device_file_id,kind FROM outputs WHERE source_action_id=? AND id>? ORDER BY id LIMIT 128',(ref.action_id,after))) as cursor:
            rows=cursor.fetchmany(128)
        if not rows:break
        for identity,file_id,kind in rows:
            found=_one(db,'SELECT id FROM segment_members WHERE set_id=? AND file_id=?',(setrow['id'],file_id))
            candidate=None if found is None else storage.segment_output_candidate(ref,found[0],reader)
            if candidate is None or kind!=int(enum_for('outputs.kind')[candidate.kind.name]) or _created_output(db,tx,ref.action_id,file_id,kind)!=identity:
                raise ConsistencyError('登记产物不是原冻结合格候选')
        after=rows[-1][0]
    expected=assessment.default_scope.member_count or 0
    actual=0
    for page in _scopes(request,reader):
        for scope in page:
            actual+=1
            member=row_facts(db,'segment_members',scope['member_id'])
            if scope['output_id']!=_created_output(db,tx,ref.action_id,member['file_id'],int(enum_for('outputs.kind').ORIGINAL)):
                raise ConsistencyError('正式默认范围缺少完整实际关联')
    if actual!=expected:raise ConsistencyError('默认范围数量与冻结判断不同')
    status,error,details=_failure(request,assessment,originals,setrow,db,before_event_id=tx.first_event_id)
    if (action['status'],action['error_code'],action['error_details_json'])!=(status,error,details):
        raise ConsistencyError('实际来源终态与冻结判断或有限错误头不同')
    qualification=context.current_reads.validated_segment_finish(tx,header)
    _verify_group_closure(db,request,header,tx,event.event_id,qualification)


def _event_state(db,event,ref,proposal):
    """沿当前事件的固定外键加载有限关联，不复制整个动作或计划。"""
    state={}
    tables=load_event_registry()['tables']
    pending=[(row.table,row.row_id) for row in event.rows]
    for row in event.rows:
        for column,value in row.after.values.items():
            target=foreign_key_targets().get((row.table,column))
            if value is not None and target in tables:pending.append((target,value))
    seen=set()
    while pending:
        table,identity=pending.pop()
        if (table,identity) in seen:continue
        seen.add((table,identity))
        facts=row_facts(db,table,identity)
        state.setdefault(table,{})
        if facts is None:continue
        state[table][identity]=facts
        for column,value in facts.items():
            target=foreign_key_targets().get((table,column))
            if value is not None and target in tables:pending.append((target,value))
    # 关系守卫读取的范围以数据库实际完整查询为准，合法反向集合至多两类。
    coverage={}
    for row in event.rows:
        if row.table!='outputs':continue
        identities=[row.row_id]
        identities += [change.after.values['original_output_id'] for change in event.rows if change.table=='output_origins']
        for identity in identities:
            for column in ('output_id','original_output_id'):
                with closing(db.execute(f'SELECT id FROM output_origins WHERE {column}=? ORDER BY id LIMIT 3',(identity,))) as cursor:
                    relations=cursor.fetchmany(3)
                if len(relations)>2:raise ConsistencyError('原片派生关系超过登记最大类别数')
                coverage.setdefault(('output_origins',column),set()).add(identity)
                for item in relations:
                    relation=row_facts(db,'output_origins',item[0]);state.setdefault('output_origins',{})[item[0]]=relation
                    prior=row_facts(db,'outputs',relation['output_id']);state.setdefault('outputs',{})[prior['id']]=prior
    state.setdefault('output_origins',{})
    owners={}
    for row in event.rows:
        if row.table=='outputs':owners[row.table,row.row_id]=('output',row.row_id)
        elif row.table=='output_origins':owners[row.table,row.row_id]=('output',row.after.values['output_id'])
        elif row.table=='plans':owners[row.table,row.row_id]=('plan',row.row_id)
        else:owners[row.table,row.row_id]=('action',ref.action_id)
    # 归属解析使用本事件after；报告路由仍使用当前有限行和本事件after。
    return state,owners,ReadCoverage(coverage)


class _FinishCommand:
    def __init__(self,request,key,batch):self.request,self.key,self.batch=request,key,batch

    def plan(self,scope):
        db=scope.connection; request=self.request
        saved=_one(db,'SELECT id FROM history_transactions WHERE operation_key=?',(str(self.key),))
        if saved is not None:
            result=verify_segment_finish(request,self.key,scope,execution_finished=True)
            return CommandPlan((),{}, {},result=result,read_only=True)
        action,run,setrow,assessment=_identity(request,db)
        verify_completion_proof(request,scope)
        if action['status']!=2 or action['cancel_requested'] or setrow['published_event_id'] is not None:
            raise ConsistencyError('原分段完成申请已无普通执行资格')
        if _one(db,'SELECT 1 FROM outputs WHERE source_action_id=? LIMIT 1',(request.ref.action_id,)):
            raise ConsistencyError('未完成来源已经存在正式产物')
        if _one(db,'SELECT 1 FROM operation_attempts WHERE run_id=? AND status=1 LIMIT 1',(request.run_id,)):
            raise ConsistencyError('RESULTS实际尝试仍在执行')
        storage._verify_assessment(db,setrow,facts_from_document(setrow['facts_json']),assessment)
        originals=previews=0
        for candidate in _candidates(request,scope,self.batch):
            if candidate.kind is OutputKind.ORIGINAL:originals+=1
            else:previews+=1
        scope_count=sum(len(page) for page in _scopes(request,scope,self.batch))
        if scope_count!=(assessment.default_scope.member_count or 0):
            raise ConsistencyError('默认范围尚未完整保存')
        _verify_scope_members(request,assessment,scope)
        output_count=originals+previews
        first_output=next_row_id(db,'outputs') if output_count else 1
        first_origin=next_row_id(db,'output_origins') if previews else 1
        storage._capacity(first_output,output_count);storage._capacity(first_origin,previews)
        plan=row_facts(db,'plans',action['plan_id'])
        qualification=_completion_qualification(db,request)
        complete_plan=qualification['complete_plan'];finish_run=qualification['finish_run']
        event_count=1+output_count+(scope_count+self.batch-1)//self.batch+int(finish_run)+int(complete_plan)+1
        allocation=scope.allocate(event_count)
        status,error,details=_failure(request,assessment,originals,setrow,db,before_event_id=scope.max_event_id+1)
        result=SegmentFinishResult(request.ref,status,3 if complete_plan else plan['status'],HistoryBoundary(allocation.txn_id,allocation.last_event_id),output_count,originals,previews,scope_count)
        header={'request':request_document(request),'output_count':output_count,'original_count':originals,'preview_count':previews,'scope_count':scope_count,
            'first_output_id':first_output,'first_origin_id':first_origin,'action_status':status,'plan_status':result.plan_status,'batch_size':self.batch,
            'finish_run':finish_run,'complete_plan':complete_plan}
        events=load_event_registry()['events']
        marker=storage.segment_event(request.ref,'ACTIVATE',storage.segment_activation_rows(request.ref,allocation.last_event_id,scope),allocation.last_event_id,allocation.txn_id,request.occurred_at,request_input=None)
        def proposal(identity):
            if identity!=marker.event_id:raise ConsistencyError('原提案只有唯一最终marker可查证')
            return marker
        guard_state={}
        def context(event,transaction):
            state,owners,coverage=_event_state(db,event,request.ref,proposal)
            # 固定提案事实只含marker身份，不含未来业务资格。
            final={**state,'segment_sets':{setrow['id']:{**setrow,'published_event_id':marker.event_id}}}
            reads=SqlEventReads(db,PlanReadScope(plan['id'],self.batch),proposal_event=proposal,segment_ref=request.ref,segment_guard_state=guard_state)
            return EventContext(transaction,owners,state,final,coverage,current_reads=reads)
        def generate():
            identity=allocation.first_event_id
            before={'status':action['status']};after={'status':status}
            if status==4:
                before.update(error_code=action['error_code'],error_details_json=action['error_details_json'])
                after.update(error_code=error,error_details_json=details)
            yield event_envelope(identity,allocation.txn_id,events['ACTION_FINISHED']['id'],1 if status==3 else 2,(update_change('actions',action['id'],before,after),),request.occurred_at,{'segment_finish_request':header});identity+=1
            output_id=first_output;origin_id=first_origin
            for wanted in (OutputKind.ORIGINAL,OutputKind.PREVIEW):
                for candidate in _candidates(request,scope,self.batch):
                    if candidate.kind is not wanted:continue
                    member=row_facts(db,'segment_members',candidate.member_id)
                    file=storage._fixed_file(db,candidate.file_id,member['file_event_id'])
                    values=dict(source_action_id=request.ref.action_id,kind=int(enum_for('outputs.kind')[wanted.name]),device_file_id=candidate.file_id,intermediate_file_id=None,
                        original_name=file['original_name'],media_type=file['media_type'],availability=1,cleanup_status=1,cleanup_error_json=None,
                        media_json={'status':'unchecked'},error_json=None)
                    from .capture import _unchecked_media
                    values['media_json']=_unchecked_media()
                    rows=(row_change('outputs',output_id,values),)
                    if wanted is OutputKind.PREVIEW:
                        original=_created_output(db,TransactionRange(allocation.txn_id,allocation.first_event_id,allocation.last_event_id),request.ref.action_id,candidate.original_file_id,int(enum_for('outputs.kind').ORIGINAL))
                        rows+=(row_change('output_origins',origin_id,dict(output_id=output_id,original_output_id=original)),);origin_id+=1
                    yield event_envelope(identity,allocation.txn_id,events['OUTPUT_REGISTERED']['id'],values['kind'],rows,request.occurred_at)
                    identity+=1;output_id+=1
            for page in _scopes(request,scope,self.batch):
                if not page:continue
                bindings=[]
                for row in page:
                    member=row_facts(db,'segment_members',row['member_id'])
                    output=_created_output(db,TransactionRange(allocation.txn_id,allocation.first_event_id,allocation.last_event_id),request.ref.action_id,member['file_id'],int(enum_for('outputs.kind').ORIGINAL))
                    bindings.append((row['id'],output))
                bindings=tuple(bindings)
                yield storage.segment_event(request.ref,'PUBLISH',storage.segment_scope_publication_rows(request.ref,bindings,scope),identity,allocation.txn_id,request.occurred_at,request_input=bindings);identity+=1
            if finish_run:
                rows=(_run_finish_row(request,qualification),)
                evidence={'stale_run_request':{'kind':'stale_run_finish','responsibility_keys':[run['responsibility_key']],'status':3,'error':None}}
                yield event_envelope(identity,allocation.txn_id,events['OPERATION_CONFIGURED']['id'],3,rows,request.occurred_at,evidence);identity+=1
            if complete_plan:
                yield event_envelope(identity,allocation.txn_id,events['PLAN_STATUS_CHANGED']['id'],2,(update_change('plans',plan['id'],{'status':plan['status']},{'status':3}),),request.occurred_at);identity+=1
            yield marker
        def complete(db):
            from camctl.history.decoding import decode_event_row
            raw=_one(db,'SELECT id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json FROM history_events WHERE id=?',(marker.event_id,))
            actual=None if raw is None else decode_event_row(raw)
            current=storage._set(db,request.ref,fixed=True)
            if actual is None or replace(actual,change_seq=None)!=marker or current['published_event_id']!=marker.event_id:
                raise ConsistencyError('完整完成组没有实际提交唯一最终ACTIVATE')
            if row_facts(db,'operation_runs',request.run_id)['status'] not in (3,4,5,6,7):
                raise ConsistencyError('完成组没有共同结束原RESULTS责任')
            if row_facts(db,'plans',plan['id'])['status']!=result.plan_status:
                raise ConsistencyError('完成组父计划结果与原有限回执不同')
        return BoundedCommandPlan(generate(),context,result,complete)


def finish_segment_recording(request,key,owned,*,batch_size=128):
    if not isinstance(request,FinishSegmentRecording):raise TypeError('必须保存原有限分段申请')
    if type(batch_size) is not int or not 1<=batch_size<=128:raise ValueError('批次必须为1—128')
    from .capture import register_capture_guards
    from .operations import register_operation_guards
    register_capture_guards();register_operation_guards();storage.register_segment_guards()
    return commit_operation(_FinishCommand(request,key,batch_size),key,owned)


def verify_segment_finish(request,key,reader,*,execution_finished,batch_size=128):
    """可靠原工作结束后核实固定完整组；在途缺键不能成为可靠不存在。"""
    if execution_finished is not True:raise ConsistencyError('原保存仍可能在途，不能核实为可靠缺键')
    if type(batch_size) is not int or not 1<=batch_size<=128:raise ValueError('原组批次必须为1—128')
    db=reader.connection
    opened=not db.in_transaction
    if opened:db.execute('BEGIN')
    try:return _verify_segment_finish(request,key,reader,batch_size)
    finally:
        if opened:db.execute('ROLLBACK')


def _verify_segment_finish(request,key,reader,batch_size):
    db=reader.connection
    found=_one(db,'SELECT id FROM history_transactions WHERE operation_key=?',(str(key),))
    if found is None:return None
    tx=read_transaction_range(db,found[0])
    first_page=read_saved_transaction_page(db,key,tx,None,1)
    first=first_page.items[0]
    header=first.evidence.get('segment_finish_request')
    if not isinstance(header,dict) or not json_equal(header.get('request'),request_document(request)):
        raise ConsistencyError('原操作键的有限完整申请不同')
    if first.event_type!=load_event_registry()['events']['ACTION_FINISHED']['id']:
        raise ConsistencyError('原完成组没有来源终态申请头')
    # 原固定事实重生成逐项预期，只使用原头中的身份，不调用分配器。
    result=_verify_events(request,key,tx,header,reader,batch_size,first_page)
    return result


def _verify_events(request,key,tx,header,reader,batch_size,first_page):
    """按固定完整组和冻结输入双向比较，只有读到原L才返回原结果。"""
    validate_finish_header(header)
    db=reader.connection
    action,run,setrow,assessment=_identity(request,db)
    qualification=_completion_qualification(db,request,before_event_id=tx.first_event_id,transaction=tx)
    _verify_qualification_header(header,qualification)
    verify_completion_proof(request,reader)
    storage._verify_assessment(db,setrow,facts_from_document(setrow['facts_json']),assessment)
    _verify_scope_members(request,assessment,reader)
    def stream():
        yield from first_page.items
        position=first_page.next_cursor
        if position is None:return
        while True:
            page=read_saved_transaction_page(db,key,tx,position,batch_size)
            yield from page.items
            if page.next_cursor is None:return
            position=page.next_cursor
    events=iter(stream())
    types=load_event_registry()['events']
    expected_id=tx.first_event_id
    def take(name,reason,evidence):
        nonlocal expected_id
        event=next(events,None)
        if (event is None or event.event_id!=expected_id or event.event_type!=types[name]['id'] or event.reason!=reason
                or event.occurred_at!=request.occurred_at or event.clock_status!=2 or not json_equal(event.evidence,evidence)):
            raise ConsistencyError('原完成组事件身份、顺序、时刻、分支或申请不同')
        expected_id+=1
        return event
    def rows_equal(event,expected):
        if event.rows!=tuple(expected):raise ConsistencyError('原完成组逐事件行与冻结完整申请不同')
    status,error,details=_failure(request,assessment,header['original_count'],setrow,db,before_event_id=tx.first_event_id)
    if status!=header['action_status']:raise ConsistencyError('原结果与原冻结判断不同')
    first=take('ACTION_FINISHED',1 if status==3 else 2,{'segment_finish_request':header})
    if len(first.rows)!=1 or first.rows[0].table!='actions' or first.rows[0].row_id!=request.ref.action_id:
        raise ConsistencyError('原完成组来源身份不同')
    change=first.rows[0]
    expected_before={'status':2};expected_after={'status':status}
    if status==4:
        # actions结构保证执行中没有动作错误；原完整行不可夹入另一份旧错误。
        expected_before.update(error_code=None,error_details_json=None)
        expected_after.update(error_code=error,error_details_json=details)
    if change!=update_change('actions',request.ref.action_id,expected_before,expected_after):
        raise ConsistencyError('原完成组动作终态的完整前后行不同')
    output_id=header['first_output_id'];origin_id=header['first_origin_id']
    originals=previews=0
    for wanted in (OutputKind.ORIGINAL,OutputKind.PREVIEW):
        for candidate in _candidates(request,reader,header['batch_size']):
            if candidate.kind is not wanted:continue
            member=row_facts(db,'segment_members',candidate.member_id)
            file=storage._fixed_file(db,candidate.file_id,member['file_event_id'])
            from .capture import _unchecked_media
            values=dict(source_action_id=request.ref.action_id,kind=int(enum_for('outputs.kind')[wanted.name]),device_file_id=candidate.file_id,intermediate_file_id=None,
                original_name=file['original_name'],media_type=file['media_type'],availability=1,cleanup_status=1,cleanup_error_json=None,media_json=_unchecked_media(),error_json=None)
            rows=[row_change('outputs',output_id,values)]
            if wanted is OutputKind.PREVIEW:
                original=_created_output(db,tx,request.ref.action_id,candidate.original_file_id,int(enum_for('outputs.kind').ORIGINAL))
                rows.append(row_change('output_origins',origin_id,dict(output_id=output_id,original_output_id=original)))
                origin_id+=1;previews+=1
            else:originals+=1
            rows_equal(take('OUTPUT_REGISTERED',values['kind'],{}),rows)
            if _created_output(db,tx,request.ref.action_id,candidate.file_id,values['kind'])!=output_id:
                raise ConsistencyError('原完整组产物身份范围不同')
            output_id+=1
    count=0
    for page in _scopes(request,reader,header['batch_size']):
        if not page:continue
        bindings=[];rows=[]
        for row in page:
            member=row_facts(db,'segment_members',row['member_id'])
            identity=_created_output(db,tx,request.ref.action_id,member['file_id'],int(enum_for('outputs.kind').ORIGINAL))
            bindings.append((row['id'],identity));rows.append(update_change('segment_scope_members',row['id'],{'output_id':None},{'output_id':identity}));count+=1
        evidence={'segment_request':{'ref':document(request.ref),'input':storage._request_document(tuple(bindings))}}
        rows_equal(take('SEGMENT_FACTS',storage._EVENT['branches']['PUBLISH']['reason'],evidence),rows)
    if (originals,previews,count)!=(header['original_count'],header['preview_count'],header['scope_count']):
        raise ConsistencyError('原完成组计数与冻结完整候选或范围不同')
    if header['finish_run']:
        evidence={'stale_run_request':{'kind':'stale_run_finish','responsibility_keys':[run['responsibility_key']],'status':3,'error':None}}
        event=take('OPERATION_CONFIGURED',3,evidence)
        _verify_run_finish_event(event,request,run['responsibility_key'],qualification)
    if header['complete_plan']:
        event=take('PLAN_STATUS_CHANGED',2,{})
        rows_equal(event,(update_change('plans',action['plan_id'],{'status':qualification['plan_status']},{'status':3}),))
    marker=take('SEGMENT_FACTS',storage._EVENT['branches']['ACTIVATE']['reason'],{'segment_request':{'ref':document(request.ref),'input':None}})
    rows_equal(marker,(update_change('segment_sets',setrow['id'],{'published_event_id':None},{'published_event_id':tx.last_event_id}),))
    if expected_id!=tx.last_event_id+1 or next(events,None) is not None:
        raise ConsistencyError('原完成组存在额外事件或缺失最终事件')
    return SegmentFinishResult(request.ref,status,header['plan_status'],HistoryBoundary(tx.txn_id,tx.last_event_id),originals+previews,originals,previews,count)

"""统一结果集合的实际来源、固定边界及有限原申请。"""
from contextlib import closing,contextmanager

from camctl.capture.segment_models import SegmentSetRef
from camctl.capture.segment_storage import (
    PublicationKind,ResultSetCreate,ResultMemberInputCursor,SegmentResultRef,SegmentSourceKind,document,encode_enum)
from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.json_values import json_equal
from camctl.contracts.pages import Page
from camctl.contracts.values import ConsistencyError,ObjectId,OperationKey
from camctl.devices.tasks import CaptureResultPolicy
from camctl.history.events import load_event_registry
from camctl.history.events import business_columns
from camctl.history.reads import SegmentReadScope
from camctl.persistence.row_history import read_row_values_at_boundary
from camctl.persistence.read_reuse import reuse_read, typed_read_key
from camctl.persistence.page_read_cache import opened_read_page_scope
from camctl.persistence.transaction import (
    CommandPlan,event_envelope,next_row_id,read_transaction_range,row_change,row_facts,
    saved_transaction_events)

from .result_pages import _at,read_pages

_EVENT=load_event_registry()['events']['SEGMENT_FACTS']


def _one(db,sql,parameters=()):
    with closing(db.execute(sql,parameters)) as cursor:
        return cursor.fetchone()


def result_run_has_set(db,run_id):
    return _one(db,'SELECT id FROM segment_sets WHERE run_id=? LIMIT 1',(run_id,)) is not None


def _boundary(db):
    found=_one(db,'SELECT t.id,t.last_event_id FROM history_transactions t'
        ' JOIN history_events e ON e.id=t.last_event_id ORDER BY t.id DESC LIMIT 1')
    if found is None:
        raise ConsistencyError('结果集合缺少原动作的完整历史边界')
    return HistoryBoundary(*found)


def _check_boundary(db,boundary):
    current=_boundary(db)
    transaction=read_transaction_range(db,boundary.txn_id)
    if transaction.last_event_id!=boundary.last_event_id or boundary.last_event_id>current.last_event_id:
        raise ConsistencyError('结果集合来源不是已完成的原历史边界')
    return current


def _row_at(db,table,identity,action_id,boundary,current):
    row=row_facts(db,table,identity)
    if row is None:raise ConsistencyError('固定来源行不存在')
    values=read_row_values_at_boundary(db,owner=('action',action_id),table=table,row_id=identity,
        columns=business_columns(table),current_values=row,boundary=boundary,current_boundary=current)
    return {'id':identity,**values}


def read_result_set(ref,owned):
    if not isinstance(ref,SegmentResultRef):
        raise TypeError('结果集合读取必须使用实际集合身份')
    row=row_facts(owned.connection,'segment_sets',ref.set_id)
    if row is None or row['action_id']!=ref.action_id or row['version']!=ref.version:
        raise ConsistencyError('结果集合引用与实际动作、集合或版本不同')
    return row


def find_canceled_result_ref(action_id,predecessor_set_id,cancel_event_id,owned):
    ObjectId(action_id);ObjectId(cancel_event_id)
    if predecessor_set_id is not None:ObjectId(predecessor_set_id)
    found=_one(owned.connection,'SELECT id,version FROM segment_sets WHERE action_id=?'
        ' AND publication_kind=? AND predecessor_set_id IS ? AND cancel_event_id=?',
        (action_id,encode_enum(PublicationKind.CANCELED),predecessor_set_id,cancel_event_id))
    return None if found is None else SegmentResultRef(action_id,found[0],found[1])


def read_current_segment_ref(ref,owned):
    row=read_result_set(ref,owned)
    if row['current_attempt_id'] is None:
        return None
    return SegmentSetRef(ref.action_id,row['current_attempt_id'],
        row['current_final_page_event_id'],ref.version)


def read_prepared_result_ref(action_id, owned):
    """恢复尚未发布的普通结果；公开消费者仍只读取明确发布的集合。"""
    ObjectId(action_id)
    row = _one(
        owned.connection,
        "SELECT id,version FROM segment_sets WHERE action_id=? AND publication_kind=?"
        " AND published_event_id IS NULL ORDER BY version DESC LIMIT 1",
        (action_id, encode_enum(PublicationKind.RESULT)),
    )
    return None if row is None else SegmentResultRef(action_id, *row)


def prepare_result_set(action_id, occurred_at, owned):
    """从同一实际读取边界形成有限来源申请，写守卫独立重算。"""
    with _read_transaction(owned) as db:
        boundary = _boundary(db)
        activity = _one(
            db, "SELECT id FROM device_activities WHERE action_id=?", (action_id,)
        )
        run = _one(
            db,
            "SELECT id FROM operation_runs WHERE action_id=? AND kind=?",
            (action_id, int(enum_for("operation_runs.kind").CHECK_CAPTURE_RESULTS)),
        )
        current = None
        source = SegmentSourceKind.NO_RESULTS
        if run is not None:
            runrow = row_facts(db, "operation_runs", run[0])
            latest = _one(
                db,
                "SELECT id FROM operation_attempts WHERE run_id=? ORDER BY attempt_no DESC LIMIT 1",
                run,
            )
            if latest is not None:
                attempt = row_facts(db, "operation_attempts", latest[0])
                current = _current_scan(db, runrow, attempt, boundary)
            source = (
                SegmentSourceKind.COMPLETE_SCAN
                if current is not None
                and _same_current_files(db, runrow, attempt, current, boundary)
                else SegmentSourceKind.RETAINED_SOURCES
            )
        return ResultSetCreate(
            action_id,
            None if activity is None else activity[0],
            None if run is None else run[0],
            source,
            boundary,
            current,
            PublicationKind.RESULT,
            None,
            None,
            occurred_at,
        )


@contextmanager
def _read_transaction(owned):
    db=owned.connection
    opened=not db.in_transaction
    if opened:db.execute('BEGIN')
    try:
        with opened_read_page_scope(db, opened):
            yield db
    finally:
        if opened:db.execute('ROLLBACK')


def read_result_member_inputs(ref,cursor,limit,owned):
    if type(limit) is not int or not 1<=limit<=128:
        raise ValueError('来源成员读取批次必须为1—128')
    if cursor is not None and (not isinstance(cursor,ResultMemberInputCursor) or cursor.ref!=ref):
        raise ConsistencyError('来源成员游标不属于原集合')
    with _read_transaction(owned) as db:
        row=read_result_set(ref,owned)
        if row['run_id'] is None:
            if cursor is not None:raise ConsistencyError('无RESULTS集合没有来源成员游标')
            return Page((),None)
        boundary=HistoryBoundary(row['source_txn_id'],row['source_event_id'])
        with closing(db.execute('SELECT file_id,MAX(page_event_id) FROM result_page_files'
                ' WHERE run_id=? AND file_id>? AND created_event_id<=?'
                ' GROUP BY file_id ORDER BY file_id LIMIT ?',
                (row['run_id'],0 if cursor is None else cursor.after_file_id,boundary.last_event_id,limit+1))) as query:
            selected=query.fetchmany(limit+1)
        items=tuple(_retained_input(db,*source,boundary) for source in selected[:limit])
        following=None if len(selected)<=limit else ResultMemberInputCursor(ref,selected[limit-1][0])
        return Page(items,following)


def _run_files(db,run_id,boundary):
    """逐file_id消费既有索引，保持一个文件的有限来源引用。"""
    previous=0
    while True:
        row=_one(db,'SELECT file_id,MAX(page_event_id) FROM result_page_files'
            ' WHERE run_id=? AND file_id>? AND created_event_id<=?'
            ' GROUP BY file_id ORDER BY file_id LIMIT 1',(run_id,previous,boundary.last_event_id))
        if row is None:return
        yield row
        previous=row[0]


def _retained_input(db,file_id,page_event_id,boundary):
    key = (__name__, "retained-input", typed_read_key(file_id),
           typed_read_key(page_event_id), typed_read_key(boundary))
    return reuse_read(db, key, lambda: _load_retained_input(db, file_id, page_event_id, boundary))


def _load_retained_input(db, file_id, page_event_id, boundary):
    from .result_file_proofs import _file_at,load_strict_result_file_input
    saved,_=_at(db,page_event_id)
    file=_file_at(db,file_id,boundary)
    if file['original_device_file_id'] is not None:
        return load_strict_result_file_input(db,saved.ref,file_id,boundary)
    fact=_one(db,'SELECT f.id FROM device_file_pairing_facts f JOIN operation_attempts a ON a.id=f.attempt_id'
        ' WHERE f.preview_file_id=? AND a.run_id=? AND f.created_event_id<=?'
        ' ORDER BY f.created_event_id DESC LIMIT 1',(file_id,saved.ref.ticket.run_id,boundary.last_event_id))
    return load_strict_result_file_input(db,saved.ref,file_id,boundary,
        pairing_fact_id=None if fact is None else fact[0])


def _check_fixed_qualifications(db,run_id,boundary):
    from .result_file_proofs import _file_at
    columns=('source_action_id','role','ownership_evidence_json','size_bytes',
        'completion_state','completion_evidence_json','original_device_file_id','pairing_evidence_json')
    if _one(db,'SELECT f.id FROM device_file_pairing_facts f JOIN operation_attempts a ON a.id=f.attempt_id'
            ' WHERE a.run_id=? AND f.created_event_id>? LIMIT 1',(run_id,boundary.last_event_id)) is not None:
        raise ConsistencyError('原H0之后已有本run的新配对资格')
    for file_id,_ in _run_files(db,run_id,boundary):
        fixed=_file_at(db,file_id,boundary)
        current=row_facts(db,'device_files',file_id)
        if any(not json_equal(fixed[name],current[name]) for name in columns):
            raise ConsistencyError('原H0之后已有本run文件的资格变化')


def _current_input(db,page,file_id,attempt,current,boundary):
    from .result_file_proofs import load_completion_input,load_strict_result_file_input
    result=load_completion_input(db,page,file_id,boundary)
    from camctl.capture.segment_models import SegmentMemberRole
    if result.role is not SegmentMemberRole.PREVIEW:return result
    fact=_one(db,'SELECT id FROM device_file_pairing_facts WHERE preview_file_id=?'
        ' AND attempt_id=? AND final_page_event_id=? AND created_event_id<=?',
        (file_id,attempt['id'],current.event_id,boundary.last_event_id))
    return result if fact is None else load_strict_result_file_input(db,page,file_id,boundary,pairing_fact_id=fact[0])


def _current_scan(db,run,attempt,boundary):
    if attempt['result_last_page_event_id'] is None:
        return None
    last,raw=_at(db,attempt['result_last_page_event_id'])
    if (last.ref.event_id>boundary.last_event_id or last.page.outcome.error is not None
            or last.page.next_cursor is not None or not last.page.scan_complete or not last.page.set_finalized):
        return None
    if raw['run_id']!=run['id'] or raw['attempt_no']!=attempt['attempt_no']:
        raise ConsistencyError('当前扫描末页不属于原run最大实际轮次')
    class Reader:
        connection=db
    cursor=None
    while True:
        page=read_pages(last.ref.ticket,cursor,1,Reader)
        for saved in page.items:
            difference={identity for identity,_ in saved.file_ids}
            if (saved.ref.event_id>boundary.last_event_id or saved.page.outcome.error is not None
                    or any(entry.identity in difference and (entry.size_bytes is None or not entry.complete)
                        for entry in saved.page.entries)):
                return None
        if page.next_cursor is None:break
        cursor=page.next_cursor
    return last.ref


def _same_current_files(db,run,attempt,current,boundary):
    """R按文件去重，C只使用最后轮次原页；旧完成证明不能替换当前观察。"""
    from .segments import _fixed_file
    from camctl.capture.segment_models import OwnershipState,FileCompletionState
    for file_id,last_page in _run_files(db,run['id'],boundary):
        actual=_one(db,'SELECT MAX(page_event_id) FROM result_page_files'
            ' WHERE run_id=? AND file_id=? AND attempt_id=? AND created_event_id<=?',
            (run['id'],file_id,attempt['id'],boundary.last_event_id))[0]
        if actual is None:return False
        retained=_retained_input(db,file_id,last_page,boundary)
        if retained.ownership is not OwnershipState.OWNED or retained.completion is not FileCompletionState.COMPLETE:
            return False
        file=_fixed_file(db,file_id,retained.file_event_id)
        saved,_=_at(db,actual)
        current_input=_current_input(db,saved.ref,file_id,attempt,current,boundary)
        identity=next(identity for identity,identity_id in saved.file_ids if identity_id==file_id)
        entry=next(entry for entry in saved.page.entries if entry.identity==identity)
        if not entry.complete or entry.size_bytes!=file['size_bytes']:
            return False
        if any(getattr(retained,name)!=getattr(current_input,name) for name in (
                'role','ownership','completion','pairing_state','pairing_identity')):
            return False
    return True


def _source(db,request):
    boundary=request.source_boundary
    current_boundary=_check_boundary(db,boundary)
    action=row_facts(db,'actions',request.action_id)
    if (action is None or action['created_event_id']>boundary.last_event_id
            or action['execution_spec_json'].get('result_policy') not in {p.value for p in CaptureResultPolicy}):
        raise ConsistencyError('统一结果集合必须属于原OSMO结果策略动作')
    original_action=_row_at(db,'actions',action['id'],action['id'],boundary,current_boundary)
    if original_action['execution_started']!=1:
        raise ConsistencyError('未执行动作不建立分段结果集合或发布标记')
    activity_row=_one(db,'SELECT id FROM device_activities WHERE action_id=?',(action['id'],))
    activity=None if activity_row is None else _row_at(db,'device_activities',activity_row[0],
        action['id'],boundary,current_boundary)
    if (None if activity is None else activity['id'])!=request.activity_id:
        raise ConsistencyError('结果集合活动不是原动作在H0的实际活动')
    kind=int(enum_for('operation_runs.kind').CHECK_CAPTURE_RESULTS)
    run_row=_one(db,'SELECT id FROM operation_runs WHERE action_id=? AND kind=?',(action['id'],kind))
    run=None if run_row is None else _row_at(db,'operation_runs',run_row[0],
        action['id'],boundary,current_boundary)
    if run is None:
        source,current=SegmentSourceKind.NO_RESULTS,None
        if request.run_id is not None:
            raise ConsistencyError('无RESULTS集合不得声明其他run')
    else:
        if run['id']!=request.run_id or run['activity_id']!=request.activity_id:
            raise ConsistencyError('结果集合没有原活动在H0的实际RESULTS责任')
        running=int(enum_for('operation_attempts.status').RUNNING)
        if _one(db,'SELECT id FROM operation_attempts WHERE run_id=? AND'
                ' (status=? OR intent_event_id>? OR result_event_id>?) LIMIT 1',
                (run['id'],running,boundary.last_event_id,boundary.last_event_id)) is not None:
            raise ConsistencyError('结果集合不能越过原H0或尚未结束的实际尝试')
        if _one(db,'SELECT id FROM result_page_files WHERE run_id=? AND created_event_id>? LIMIT 1',
                (run['id'],boundary.last_event_id)) is not None:
            raise ConsistencyError('原H0之后已有本run的新实际页')
        last=_one(db,'SELECT id FROM operation_attempts WHERE run_id=? ORDER BY attempt_no DESC LIMIT 1',
            (run['id'],))
        _check_fixed_qualifications(db,run['id'],boundary)
        current = None
        if last is not None:
            attempt = row_facts(db, "operation_attempts", last[0])
            current = _current_scan(db, run, attempt, boundary)
        source=(SegmentSourceKind.COMPLETE_SCAN if current is not None
            and _same_current_files(db,run,attempt,current,boundary) else SegmentSourceKind.RETAINED_SOURCES)
    if request.source_kind is not source or request.current_scan!=current:
        raise ConsistencyError('结果集合来源或当前扫描与原H0实际事实不同')
    return action,activity,run


def _publication(db,request):
    if request.publication_kind is not PublicationKind.CANCELED:
        if request.cancel_event_id is not None or request.predecessor_set_id is not None:
            raise ConsistencyError('非取消集合不携带取消前版和请求事件')
        return
    from camctl.history.decoding import decode_event_row
    from .result_pages import _COLUMNS
    if request.cancel_event_id is None:
        raise ConsistencyError('取消集合必须引用原动作实际取消请求')
    raw=_one(db,f'SELECT {_COLUMNS} FROM history_events WHERE id=?',(request.cancel_event_id,))
    if raw is None:
        raise ConsistencyError('原取消请求事件不存在')
    event=decode_event_row(raw)
    if not any(row.table=='actions' and row.row_id==request.action_id
            and row.before.values.get('cancel_requested')==0
            and row.after.values.get('cancel_requested')==1 for row in event.rows):
        raise ConsistencyError('取消集合引用不是同一动作实际取消变化')
    if request.predecessor_set_id is not None:
        previous=row_facts(db,'segment_sets',request.predecessor_set_id)
        if (previous is None or previous['action_id']!=request.action_id
                or previous['published_event_id'] is not None
                or previous['publication_kind']==encode_enum(PublicationKind.CANCELED)
                or previous['source_kind']!=encode_enum(request.source_kind)
                or (previous['source_txn_id'],previous['source_event_id'])!=(
                    request.source_boundary.txn_id,request.source_boundary.last_event_id)):
            raise ConsistencyError('取消集合没有接手同一原动作的未发布来源版本')
    elif _one(db,'SELECT id FROM segment_sets WHERE action_id=? AND published_event_id IS NULL LIMIT 1',
            (request.action_id,)) is not None:
        raise ConsistencyError('已有未发布结果的取消集合必须明确原前版')


def _values(db,request):
    action,activity,run=_source(db,request)
    _publication(db,request)
    attempt=None
    if request.current_scan is not None:
        attempt=_one(db,'SELECT id FROM operation_attempts WHERE run_id=? AND attempt_no=?',
            (request.run_id,request.current_scan.ticket.attempt_id))[0]
    return dict(action_id=request.action_id,activity_id=request.activity_id,run_id=request.run_id,
        current_attempt_id=attempt,current_final_page_event_id=None if request.current_scan is None else request.current_scan.event_id,
        source_kind=encode_enum(request.source_kind),source_txn_id=request.source_boundary.txn_id,
        source_event_id=request.source_boundary.last_event_id,publication_kind=encode_enum(request.publication_kind),
        predecessor_set_id=request.predecessor_set_id,cancel_event_id=request.cancel_event_id,
        target_duration_ms=action['execution_spec_json']['target_duration_ms'],
        state=int(enum_for('segment_sets.state').COLLECTING),member_count=None,
        facts_json=None,assessment_json=None,published_event_id=None)


def verify_create_event(db,ref,event,context):
    raw=event.evidence['segment_request']['input']
    from camctl.capture.segment_storage import decode_enum
    from camctl.capture.result_pages import ResultPageRef
    from camctl.operations.models import AttemptTicket
    scan=raw['current_scan']
    scan=None if scan is None else ResultPageRef(AttemptTicket(**scan['ticket']),
        scan['page_no'],scan['event_id'],None)
    request=ResultSetCreate(raw['action_id'],raw['activity_id'],raw['run_id'],
        decode_enum(SegmentSourceKind,raw['source_kind']),HistoryBoundary(**raw['source_boundary']),scan,
        decode_enum(PublicationKind,raw['publication_kind']),raw['predecessor_set_id'],
        raw['cancel_event_id'],raw['occurred_at'])
    version=_one(db,'SELECT COALESCE(MAX(version),0)+1 FROM segment_sets WHERE action_id=?',
        (request.action_id,))[0]
    expected=_values(db,request)
    expected.update(version=version,created_event_id=event.event_id)
    if (event.reason!=_EVENT['branches']['CREATE_RESULT']['reason'] or len(event.rows)!=1
            or request.action_id!=ref.action_id or request.occurred_at!=event.occurred_at or ref.version!=version
            or ref.set_id!=next_row_id(db,'segment_sets')
            or event.rows[0].row_id!=ref.set_id or event.rows[0].before.exists
            or not json_equal(dict(event.rows[0].after.values),expected)
            or not json_equal(event.evidence,{'segment_request':{'ref':document(ref),'input':document(request)}})):
        raise ConsistencyError('统一集合事件与原完整申请或同连接实际来源不同')


class CreateResultSetCommand:
    def __init__(self,request,key):
        if not isinstance(request,ResultSetCreate):
            raise TypeError('建立结果集合需要完整原申请')
        self.request,self.key=request,OperationKey(key)

    def plan(self,scope):
        db,request=scope.connection,self.request
        saved=saved_transaction_events(db,self.key)
        if saved is not None:
            original=saved[0]
            raw=original['body']['evidence']['segment_request']
            ref=SegmentResultRef(**raw['ref'])
            if (len(saved)!=1 or original['type']!=_EVENT['id']
                    or original['reason']!=_EVENT['branches']['CREATE_RESULT']['reason']
                    or not json_equal(raw['input'],document(request)) or original['occurred_at']!=request.occurred_at):
                raise ConsistencyError('原键结果集合的完整申请不同')
            row=read_result_set(ref,scope)
            change=original['body']['rows'][0]
            if (change['id']!=ref.set_id or not json_equal(
                    {name:row[name] for name in change['after']['values'] if name not in (
                        'state','member_count','facts_json','assessment_json','published_event_id')},
                    {name:value for name,value in change['after']['values'].items() if name not in (
                        'state','member_count','facts_json','assessment_json','published_event_id')})):
                raise ConsistencyError('原集合实际身份与原创建事务不同')
            return CommandPlan((),{},{},ref,True)
        if request.publication_kind is PublicationKind.CANCELED:
            published=_one(db,'SELECT id,version FROM segment_sets WHERE action_id=?'
                ' AND published_event_id IS NOT NULL',(request.action_id,))
            if published is not None:
                return CommandPlan((),{},{},SegmentResultRef(request.action_id,*published),True)
        values=_values(db,request)
        version=_one(db,'SELECT COALESCE(MAX(version),0)+1 FROM segment_sets WHERE action_id=?',
            (request.action_id,))[0]
        ref=SegmentResultRef(request.action_id,next_row_id(db,'segment_sets'),version)
        allocation=scope.allocate(1)
        values.update(version=version,created_event_id=allocation.first_event_id)
        event=event_envelope(allocation.first_event_id,allocation.txn_id,_EVENT['id'],
            _EVENT['branches']['CREATE_RESULT']['reason'],(row_change('segment_sets',ref.set_id,values),),
            request.occurred_at,{'segment_request':{'ref':document(ref),'input':document(request)}})
        return CommandPlan((event,),{('segment_sets',ref.set_id):('action',ref.action_id)},
            {'actions':{ref.action_id:row_facts(db,'actions',ref.action_id)},'segment_sets':{}},
            result=ref,segment_read_scope=SegmentReadScope(ref))

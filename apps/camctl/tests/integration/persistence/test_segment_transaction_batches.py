"""分段完整收场的有限申请、原子提交和原键精确核实。"""
from dataclasses import replace
from contextlib import closing
from pathlib import Path
import pytest
from camctl.capture.segment_models import DefaultScopeState
from camctl.contracts.values import new_operation_key
from .test_segment_storage import _assessed_world

def _finish_attempt(owned,ref,runtime):
    from .test_segment_storage import _finish_saved_scan
    _finish_saved_scan(owned,runtime,ref)

def _request_for(ref,row,at,assessment,original_count):
    from camctl.capture.media import RecordingFailure
    from camctl.capture.segment_errors import segment_error_header
    failure=None
    if assessment.status is not SegmentAssessmentStatus.SUCCEEDED:
        if assessment.reason is SegmentAssessmentReason.GAP:
            failure=RecordingFailure('capture_segment_gap',segment_error_header(assessment,original_count).details('capture_segment_gap'))
        elif assessment.reason is SegmentAssessmentReason.NO_NONZERO_ORIGINAL:
            failure=RecordingFailure('capture_failed',{'activity_id':str(row['activity_id']),'reason':'no_outputs' if row['member_count']==0 else 'invalid_outputs'})
        else:
            reason='completion_unknown' if assessment.reason in (SegmentAssessmentReason.STOP_UNCONFIRMED,SegmentAssessmentReason.COMPLETION_UNCONFIRMED,SegmentAssessmentReason.LOWER_BOUND_INSUFFICIENT) else 'outputs_unknown'
            failure=RecordingFailure('capture_result_unconfirmed',{'activity_id':str(row['activity_id']),'reason':reason})
    return transactions.FinishSegmentRecording(ref,row['activity_id'],row['run_id'],at,failure)

@pytest.mark.asyncio
async def test_bounded_finish_is_atomic_and_reuses_original_receipt(tmp_path):
    from camctl.persistence.repositories.segment_transactions import FinishSegmentRecording, finish_segment_recording
    owned,runtime,repo,ref,_=await _assessed_world(tmp_path,DefaultScopeState.NONEMPTY)
    try:
        row=repo.read_set(ref,owned)
        _finish_attempt(owned,ref,runtime)
        request=FinishSegmentRecording(ref,row['activity_id'],row['run_id'],runtime.wall_us())
        key=new_operation_key()
        result=finish_segment_recording(request,key,owned)
        assert result.kind=='completed',result.error
        assert result.change_set is None
        assert not hasattr(result.result,'output_ids')
        assert result.result.output_count==2
        assert result.result.scope_count==2
        assert repo.read_active_set(ref.action_id,owned)==ref
        count=owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()
        again=finish_segment_recording(request,key,owned)
        assert again.kind=='completed',again.error
        assert again.result==result.result
        assert owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()==count
        assert finish_segment_recording(replace(request,occurred_at=request.occurred_at+1),key,owned).kind=='rolled_back'
    finally:owned.connection.close()

from camctl.persistence.repositories import segment_transactions as transactions
from camctl.persistence.runtime import DbConfig,DbOpenMode,open_existing
from camctl.persistence.transaction import commit_operation
from camctl.capture.segment_models import *
from .test_segment_storage import _world,_assessment,_ok

async def _ready_world(tmp_path,count=5,*,sizes=None,indexes=None,unknown_keys=False,facts_change=None,siblings=None):
    if count:
        owned,runtime,repo,ref,members=await _world(tmp_path,indexes=tuple(range(1,count+1)) if indexes is None else indexes,sizes=sizes,unknown_keys=unknown_keys,
            independent_activity=siblings=='pending_with_invalid',invalid_sibling=siblings is not None)
    else:
        from .test_segment_storage import SegmentedCatalog,SegmentRepository,SegmentSetCreate
        from ..capture.result_consumer_fixtures import consumer_world
        from ..capture.test_result_page_history import _request,_actual
        from camctl.capture.handlers import _begin_check_round
        owned,runtime,action_id,_=await consumer_world(tmp_path,'record',catalog=SegmentedCatalog(),record_control_elapsed_ns=60_000_000_000)
        ticket=_begin_check_round(runtime,action_id).ticket
        saved=_ok(runtime.capture.save_result_page(_request(runtime,ticket,_actual(ticket,finalized=True)),new_operation_key(),owned))
        attempt=owned.connection.execute('SELECT id FROM operation_attempts WHERE run_id=?',(ticket.run_id,)).fetchone()[0]
        ref=SegmentSetRef(action_id,attempt,saved.event_id,1);repo=SegmentRepository();members=()
        _ok(repo.create(SegmentSetCreate(ref,int(ticket.target_id),ticket.run_id,60000,runtime.wall_us()),new_operation_key(),owned))
    for offset in range(0,len(members),128):_ok(repo.append_members(ref,members[offset:offset+128],runtime.wall_us(),new_operation_key(),owned))
    _ok(repo.fix_members(ref,runtime.wall_us(),new_operation_key(),owned))
    facts,assessment,gaps=_assessment(ref,members,owned)
    if facts_change:
        facts=replace(facts,**facts_change)
        from camctl.persistence.repositories.segments import iter_segment_members
        from camctl.capture.segments import advance_segment_assessment,finish_segment_assessment,iter_segment_gaps
        state=SegmentAccumulator(ref,60000,facts.start_key)
        for member in iter_segment_members(ref,owned):state=advance_segment_assessment(state,member)
        assessment=finish_segment_assessment(state,facts)
        gaps=tuple(iter_segment_gaps(iter_segment_members(ref,owned),last_nonzero_cursor=assessment.last_nonzero_cursor,last_owned_cursor=assessment.last_owned_cursor,start_key=facts.start_key)) if assessment.gap_count is not None else ()
    for offset in range(0,len(gaps),128):_ok(repo.append_gaps(ref,gaps[offset:offset+128],runtime.wall_us(),new_operation_key(),owned))
    _ok(repo.save_assessment(ref,facts,assessment,runtime.wall_us(),new_operation_key(),owned))
    selected=assessment.default_scope
    if selected.state is DefaultScopeState.NONEMPTY:
        cursor=None;pending=[]
        while True:
            page=repo.read_members(ref,cursor,128,owned)
            for stored in page.items:
                member=stored.member
                if member.cursor is not None and selected.first_cursor<=member.cursor<=selected.last_cursor and member.size_bytes and member.size_bytes>0:
                    pending.append(stored.member_id)
                    if len(pending)==128:_ok(repo.append_scope(ref,tuple(pending),runtime.wall_us(),new_operation_key(),owned));pending=[]
            if page.next_cursor is None:break
            cursor=page.next_cursor
        if pending:_ok(repo.append_scope(ref,tuple(pending),runtime.wall_us(),new_operation_key(),owned))
    _finish_attempt(owned,ref,runtime)
    setrow=repo.read_set(ref,owned)
    request=_request_for(ref,setrow,runtime.wall_us(),assessment,sum(1 for size in (sizes if sizes is not None else (10,)*count) if size is not None and size>0))
    return owned,runtime,repo,ref,request

class ConnectionProbe:
    def __init__(self,db,hook):self.db,self.hook=db,hook
    def __getattr__(self,name):return getattr(self.db,name)
    def execute(self,sql,params=()):
        self.hook('before',sql,params)
        result=self.db.execute(sql,params)
        self.hook('after',sql,params)
        return result

def _fingerprint(db):
    return tuple((table,*db.execute(f'SELECT COUNT(*),MAX(id) FROM {table}').fetchone()) for table in
        ('outputs','output_origins','segment_scope_members','segment_sets','actions','plans','operation_runs','history_transactions','history_events','entity_event_links','report_entity_changes','entity_snapshot_progress'))

@pytest.mark.asyncio
async def test_every_output_batch_remains_invisible_until_full_commit(tmp_path):
    owned,runtime,repo,ref,request=await _ready_world(tmp_path)
    path=owned.connection.execute('PRAGMA database_list').fetchone()[2]
    observer=open_existing(Path(path),DbOpenMode.EXISTING_RO,DbConfig())
    seen=[]
    def hook(stage,sql,params):
        if stage=='after' and sql.startswith('INSERT INTO outputs'):
            seen.append(observer.connection.execute('SELECT COUNT(*) FROM outputs').fetchone()[0])
            assert observer.connection.execute('SELECT status FROM actions WHERE id=?',(ref.action_id,)).fetchone()==(2,)
            assert repo.read_active_set(ref.action_id,observer) is None
    try:
        saved=transactions.finish_segment_recording(request,new_operation_key(),replace(owned,connection=ConnectionProbe(owned.connection,hook)),batch_size=2)
        assert saved.kind=='completed',saved.error
        assert seen==[0]*5
        assert observer.connection.execute('SELECT COUNT(*) FROM outputs').fetchone()==(5,)
        assert repo.read_active_set(ref.action_id,observer)==ref
        tx=saved.boundary.txn_id
        assert observer.connection.execute('SELECT COUNT(*),MIN(id),MAX(id) FROM history_events WHERE transaction_id=?',(tx,)).fetchone()==(12,saved.boundary.last_event_id-11,saved.boundary.last_event_id)
    finally:observer.connection.close();owned.connection.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('cut',[2,5,'final'])
async def test_late_batch_or_final_guard_failure_rolls_back_entire_group(tmp_path,cut,monkeypatch):
    import sqlite3
    owned,runtime,repo,ref,request=await _ready_world(tmp_path)
    before=_fingerprint(owned.connection);number=0
    def hook(stage,sql,params):
        nonlocal number
        if stage=='before' and sql.startswith('INSERT INTO outputs'):
            number+=1
            if number==cut:raise sqlite3.OperationalError('controlled batch failure')
    if cut=='final':
        def fail(*args):raise transactions.ConsistencyError('controlled final guard failure')
        monkeypatch.setattr(transactions,'verify_publication',fail)
    try:
        saved=transactions.finish_segment_recording(request,new_operation_key(),replace(owned,connection=ConnectionProbe(owned.connection,hook)),batch_size=2)
        assert saved.kind=='rolled_back'
        assert _fingerprint(owned.connection)==before
        assert repo.read_active_set(ref.action_id,owned) is None
    finally:owned.connection.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('cut',['event','commit_before','commit_after'])
async def test_control_exit_is_verified_only_after_original_connection_closes(tmp_path,monkeypatch,cut):
    from camctl.persistence.transaction import write_receipt_for
    class ControlledExit(BaseException):pass
    owned,runtime,repo,ref,request=await _ready_world(tmp_path)
    path=owned.connection.execute('PRAGMA database_list').fetchone()[2]
    before=_fingerprint(owned.connection)
    calls=(runtime.driver.control.await_count,runtime.stopper.stop.await_count)
    key=new_operation_key();observer=None;closed=False
    def hook(stage,sql,params):
        if ((cut=='event' and stage=='before' and sql.startswith('INSERT INTO outputs'))
                or (cut=='commit_before' and stage=='before' and sql=='COMMIT')
                or (cut=='commit_after' and stage=='after' and sql=='COMMIT')):
            raise ControlledExit('controlled original save exit')
    try:
        with pytest.raises(ControlledExit) as control:
            transactions.finish_segment_recording(request,key,replace(owned,connection=ConnectionProbe(owned.connection,hook)),batch_size=2)
        assert write_receipt_for(control.value).kind==('rolled_back' if cut=='event' else 'unknown')
        owned.connection.close();closed=True
        observer=open_existing(Path(path),DbOpenMode.EXISTING_RW,DbConfig())
        def forbidden(*args,**kwargs):raise AssertionError('核实不能重新分配编号')
        monkeypatch.setattr(transactions,'next_row_id',forbidden)
        result=transactions.verify_segment_finish(request,key,observer,execution_finished=True,batch_size=2)
        if cut=='commit_after':
            assert result.output_count==5 and repo.read_active_set(ref.action_id,observer)==ref
        else:
            assert result is None and _fingerprint(observer.connection)==before
        assert (runtime.driver.control.await_count,runtime.stopper.stop.await_count)==calls
    finally:
        if not closed:owned.connection.close()
        if observer is not None:observer.connection.close()


@pytest.mark.asyncio
async def test_commit_receipt_unknown_is_verified_by_original_range_without_allocation(tmp_path,monkeypatch):
    import sqlite3
    owned,runtime,repo,ref,request=await _ready_world(tmp_path)
    def hook(stage,sql,params):
        if stage=='after' and sql=='COMMIT':raise sqlite3.OperationalError('controlled lost receipt')
    try:
        key=new_operation_key()
        saved=transactions.finish_segment_recording(request,key,replace(owned,connection=ConnectionProbe(owned.connection,hook)),batch_size=2)
        assert saved.kind=='unknown'
        before=_fingerprint(owned.connection)
        def forbidden(*args,**kwargs):raise AssertionError('核实禁止重新分配或全量读原事件')
        monkeypatch.setattr(transactions,'next_row_id',forbidden)
        import camctl.persistence.transaction as kernel
        monkeypatch.setattr(kernel,'saved_transaction_events',forbidden)
        result=transactions.verify_segment_finish(request,key,owned,execution_finished=True,batch_size=2)
        again=transactions.finish_segment_recording(request,key,owned)
        assert again.kind=='completed',again.error
        assert result==again.result and result.output_count==5
        assert _fingerprint(owned.connection)==before
        with pytest.raises(transactions.ConsistencyError):transactions.verify_segment_finish(request,key,owned,execution_finished=False)
    finally:owned.connection.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('state',list(DefaultScopeState))
async def test_bounded_terminal_preserves_three_scope_states(tmp_path,state):
    owned,runtime,repo,ref,_=await _assessed_world(tmp_path,state)
    try:
        _finish_attempt(owned,ref,runtime)
        row=repo.read_set(ref,owned);request=_request_for(ref,row,runtime.wall_us(),repo.read_assessment(ref,owned),2)
        saved=transactions.finish_segment_recording(request,new_operation_key(),owned,batch_size=1)
        assert saved.kind=='completed',saved.error
        assert saved.result.scope_count==(2 if state is DefaultScopeState.NONEMPTY else 0)
        assert repo.read_assessment(ref,owned).default_scope.state is state
    finally:owned.connection.close()

@pytest.mark.asyncio
async def test_empty_candidate_pages_continue_to_later_valid_members(tmp_path):
    owned,runtime,repo,ref,request=await _ready_world(tmp_path,count=5,sizes=(0,0,10,10,10))
    try:
        saved=transactions.finish_segment_recording(request,new_operation_key(),owned,batch_size=1)
        assert saved.kind=='completed',saved.error
        assert saved.result.output_count==3 and saved.result.scope_count==0
        assert saved.result.action_status==4
    finally:owned.connection.close()

@pytest.mark.asyncio
async def test_saved_range_page_error_does_not_return_success_prefix(tmp_path):
    import sqlite3
    owned,runtime,repo,ref,request=await _ready_world(tmp_path)
    try:
        key=new_operation_key();assert transactions.finish_segment_recording(request,key,owned,batch_size=2).kind=='completed'
        reads=0
        def hook(stage,sql,params):
            nonlocal reads
            if stage=='before' and 'FROM history_events WHERE transaction_id=? AND id>?' in sql:
                reads+=1
                if reads==3:raise sqlite3.OperationalError('controlled verify page failure')
        with pytest.raises(sqlite3.OperationalError):
            transactions.verify_segment_finish(request,key,replace(owned,connection=ConnectionProbe(owned.connection,hook)),execution_finished=True,batch_size=2)
    finally:owned.connection.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['short','extra','wrong_file','wrong_scope','fake_marker','duplicate_output'])
async def test_independent_cross_batch_guards_reject_tampered_proposal(tmp_path,mode,monkeypatch):
    count=21 if mode=='wrong_file' else 6
    owned,runtime,repo,ref,request=await _ready_world(tmp_path,count=count,sizes=(10,)*(count-1)+(0,))
    from camctl.persistence.transaction import row_change,update_change
    original=transactions._FinishCommand.plan
    def plan(command,scope):
        result=original(command,scope)
        def events():
            number=0;first=None;last=None
            for event in result.events:
                last=event
                if event.event_type==20:
                    number+=1
                    if first is None:first=event.rows[0].after.values['device_file_id']
                    if mode=='short' and number==3:continue
                    if mode in ('wrong_file','duplicate_output') and number==(16 if mode=='wrong_file' else 3):
                        values=dict(event.rows[0].after.values)
                        values['device_file_id']=(scope.connection.execute('SELECT MAX(file_id) FROM segment_members').fetchone()[0] if mode=='wrong_file' else first)
                        event=replace(event,rows=(row_change('outputs',event.rows[0].row_id,values),))
                if event.event_type==transactions.storage._EVENT['id']:
                    if mode=='wrong_scope' and event.reason==transactions.storage._EVENT['branches']['PUBLISH']['reason']:
                        row=event.rows[0]
                        changed=update_change(row.table,row.row_id,{'output_id':None},{'output_id':999999})
                        event=replace(event,rows=(changed,*event.rows[1:]))
                    if mode=='fake_marker' and event.reason==transactions.storage._EVENT['branches']['ACTIVATE']['reason']:
                        event=replace(event,evidence={'segment_request':{'ref':{**transactions.document(ref),'version':ref.version+1},'input':None}})
                yield event
            if mode=='extra':yield replace(last,event_id=last.event_id+1)
        return replace(result,events=events())
    monkeypatch.setattr(transactions._FinishCommand,'plan',plan)
    try:
        before=_fingerprint(owned.connection)
        saved=transactions.finish_segment_recording(request,new_operation_key(),owned,batch_size=2)
        assert saved.kind=='rolled_back',saved.error
        assert _fingerprint(owned.connection)==before
    finally:owned.connection.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('bad_origin',[False,True])
async def test_cross_page_preview_keeps_exact_original_and_origin(tmp_path,bad_origin,monkeypatch):
    from .test_segment_storage import _preview_world
    from camctl.persistence.transaction import row_change
    owned,runtime,repo,ref,members,files,assessment=await _preview_world(tmp_path,original_sizes=(10,)*5,paired_index=1,page_size=1)
    _finish_attempt(owned,ref,runtime)
    row=repo.read_set(ref,owned);request=transactions.FinishSegmentRecording(ref,row['activity_id'],row['run_id'],runtime.wall_us())
    if bad_origin:
        original=transactions._FinishCommand.plan
        def plan(command,scope):
            result=original(command,scope)
            def events():
                for event in result.events:
                    if event.event_type==20 and event.reason==3:
                        relation=event.rows[1]
                        event=replace(event,rows=(event.rows[0],row_change('output_origins',relation.row_id,{**relation.after.values,'original_output_id':relation.after.values['original_output_id']+1})))
                    yield event
            return replace(result,events=events())
        monkeypatch.setattr(transactions._FinishCommand,'plan',plan)
    try:
        key=new_operation_key();saved=transactions.finish_segment_recording(request,key,owned,batch_size=2)
        if bad_origin:
            assert saved.kind=='rolled_back'
            assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone()==(0,)
        else:
            assert saved.kind=='completed',saved.error
            assert (saved.result.original_count,saved.result.preview_count)==(5,1)
            assert transactions.verify_segment_finish(request,key,owned,execution_finished=True,batch_size=1)==saved.result
            paired=owned.connection.execute('SELECT p.device_file_id,o.device_file_id FROM output_origins r JOIN outputs p ON p.id=r.output_id JOIN outputs o ON o.id=r.original_output_id').fetchone()
            assert paired==(files['/DCIM/CAM001/CAM_20261021090000_0001_D.LRF'],files['/DCIM/CAM001/CAM_20261021090000_0001_D.OSV'])
    finally:owned.connection.close()

@pytest.mark.asyncio
async def test_original_receipt_compares_entire_terminal_before_image(tmp_path):
    import json
    owned,runtime,repo,ref,request=await _ready_world(tmp_path,count=2,indexes=(1,3))
    try:
        key=new_operation_key();saved=transactions.finish_segment_recording(request,key,owned,batch_size=2)
        assert saved.kind=='completed',saved.error
        first=owned.connection.execute('SELECT first_event_id FROM history_transactions WHERE id=?',(saved.boundary.txn_id,)).fetchone()[0]
        body=json.loads(owned.connection.execute('SELECT body_json FROM history_events WHERE id=?',(first,)).fetchone()[0])
        changed=body['rows'][0]
        changed['before']['values'].update(error_code=transactions.registered_error('capture_failed')['action_error_id'],
            error_details_json={'activity_id':str(request.activity_id),'reason':'device_failed'})
        owned.connection.execute('UPDATE history_events SET body_json=? WHERE id=?',(json.dumps(body),first))
        with pytest.raises(transactions.ConsistencyError):
            transactions.verify_segment_finish(request,key,owned,execution_finished=True,batch_size=2)
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_original_receipt_rechecks_complete_frozen_gap_relations(tmp_path):
    owned,runtime,repo,ref,request=await _ready_world(tmp_path,count=3,indexes=(1,3,5))
    try:
        key=new_operation_key()
        saved=transactions.finish_segment_recording(request,key,owned,batch_size=2)
        assert saved.kind=='completed',saved.error
        gaps=owned.connection.execute('SELECT id FROM segment_gaps ORDER BY id').fetchall()
        assert len(gaps)==2
        owned.connection.execute('DELETE FROM segment_gaps WHERE id=?',gaps[-1])
        with pytest.raises(transactions.ConsistencyError):
            transactions.verify_segment_finish(request,key,owned,execution_finished=True,batch_size=2)
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_original_receipt_does_not_change_after_output_or_file_observation(tmp_path):
    from camctl.capture.files import FileChecksumSave
    from camctl.persistence.transaction import row_facts,update_change,event_envelope,CommandPlan
    from camctl.history.reads import ProductReadScope
    owned,runtime,repo,ref,request=await _ready_world(tmp_path)
    try:
        key=new_operation_key();saved=transactions.finish_segment_recording(request,key,owned,batch_size=2)
        assert saved.kind=='completed',saved.error
        file_id=owned.connection.execute('SELECT MIN(device_file_id) FROM outputs').fetchone()[0]
        _ok(runtime.capture.save_file_checksum(FileChecksumSave(file_id,2,runtime.wall_us(),sha256='a'*64),new_operation_key(),owned))
        assert transactions.verify_segment_finish(request,key,owned,execution_finished=True,batch_size=2)==saved.result
    finally:owned.connection.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('field',['activity_id','run_id','ref'])
async def test_same_key_changed_immutable_reference_is_rejected(tmp_path,field):
    owned,runtime,repo,ref,request=await _ready_world(tmp_path)
    try:
        key=new_operation_key();saved=transactions.finish_segment_recording(request,key,owned,batch_size=2)
        assert saved.kind=='completed',saved.error
        value=replace(ref,version=2) if field=='ref' else getattr(request,field)+1
        assert transactions.finish_segment_recording(replace(request,**{field:value}),key,owned).kind=='rolled_back'
    finally:owned.connection.close()

@pytest.mark.asyncio
async def test_reliable_absence_and_bound_cursor_errors_are_distinct(tmp_path):
    from camctl.persistence.transaction import read_saved_transaction_page
    from camctl.contracts.history_values import TransactionRange
    owned,runtime,repo,ref,request=await _ready_world(tmp_path)
    try:
        assert transactions.verify_segment_finish(request,new_operation_key(),owned,execution_finished=True) is None
        with pytest.raises(transactions.ConsistencyError):transactions.verify_segment_finish(request,new_operation_key(),owned,execution_finished=False)
        key=new_operation_key();saved=transactions.finish_segment_recording(request,key,owned,batch_size=2)
        tx=transactions.read_transaction_range(owned.connection,saved.boundary.txn_id)
        page=read_saved_transaction_page(owned.connection,key,tx,None,2)
        assert len(page.items)==2 and page.next_cursor is not None
        with pytest.raises(transactions.ConsistencyError):read_saved_transaction_page(owned.connection,new_operation_key(),tx,page.next_cursor,2)
        with pytest.raises(transactions.ConsistencyError):read_saved_transaction_page(owned.connection,key,tx,replace(page.next_cursor,instance_id='0'*32),2)
    finally:owned.connection.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('sizes',[(0,0,0),()])
async def test_missing_required_original_uses_frozen_actual_partition(tmp_path,sizes):
    owned,runtime,repo,ref,request=await _ready_world(tmp_path,count=len(sizes),sizes=sizes)
    try:
        assert request.failure.code=='capture_failed' and request.failure.details['reason']==('invalid_outputs' if sizes else 'no_outputs')
        key=new_operation_key();saved=transactions.finish_segment_recording(request,key,owned,batch_size=1)
        assert saved.kind=='completed',saved.error
        assert saved.result.output_count==0
        assert transactions.verify_segment_finish(request,key,owned,execution_finished=True,batch_size=1)==saved.result
    finally:owned.connection.close()

@pytest.mark.asyncio
async def test_unfinished_finite_verification_cannot_be_forced_to_terminal(tmp_path):
    owned,runtime,repo,ref,request=await _ready_world(tmp_path,facts_change={'control_completion':EvidenceState.UNKNOWN,'finite_check_complete':False})
    try:
        saved=transactions.finish_segment_recording(request,new_operation_key(),owned,batch_size=2)
        assert saved.kind=='rolled_back'
        assert '尚未结束' in str(saved.error)
    finally:owned.connection.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['valid','missing','wrong_error','future','foreign'])
async def test_explicit_failure_requires_original_saved_error_reference(tmp_path,mode):
    from camctl.capture.media import RecordingFailure
    from camctl.operations.attempts import StaleRunFinish,RunOutcome
    from camctl.operations.models import ErrorValue
    from camctl.persistence.repositories.operations import OperationRepository
    from camctl.contracts.workflow_errors import registered_error
    owned,runtime,repo,ref,request=await _ready_world(tmp_path,facts_change={'explicit_error':True})
    try:
        row=repo.read_set(ref,owned)
        failure=RecordingFailure('capture_failed',{'activity_id':str(row['activity_id']),'reason':'device_failed'})
        original=OperationRepository().finish_stale_runs(StaleRunFinish((f"results/{row['activity_id']}",),RunOutcome.UNCONFIRMED,runtime.wall_us(),error=ErrorValue(failure.code,registered_error(failure.code)['stage'],failure.details)),new_operation_key(),owned)
        assert original.kind.value=='completed',original.error
        anchor=owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()[0]
        if mode=='missing':anchor=None
        elif mode=='future':anchor+=1
        elif mode=='foreign':anchor=1
        elif mode=='wrong_error':failure=replace(failure,details={**failure.details,'reason':'invalid_outputs'})
        request=replace(request,failure=failure,failure_event_id=anchor)
        saved=transactions.finish_segment_recording(request,new_operation_key(),owned,batch_size=2)
        assert saved.kind==('completed' if mode=='valid' else 'rolled_back'),saved.error
        if mode=='valid':assert saved.result.action_status==4
    finally:owned.connection.close()

@pytest.mark.asyncio
async def test_fixed_batch_write_and_verification_have_bounded_residency(tmp_path,monkeypatch):
    import gc,tracemalloc,json
    records=[]
    class CursorProbe:
        def __init__(self,cursor,stats):self.cursor,self.stats=cursor,stats
        def __getattr__(self,name):return getattr(self.cursor,name)
        def fetchmany(self,size):
            rows=self.cursor.fetchmany(size)
            self.stats['max_fetch']=max(self.stats['max_fetch'],len(rows))
            self.stats['fetched_rows']+=len(rows)
            assert size<=128
            return rows
        def fetchone(self):
            row=self.cursor.fetchone()
            self.stats['fetched_rows']+=int(row is not None)
            return row
        def fetchall(self):raise AssertionError('增长路径不得调用fetchall')
        def __iter__(self):return self
        def __next__(self):
            row=self.fetchone()
            if row is None:raise StopIteration
            return row
    class MeasuredConnection:
        def __init__(self,db,stats,tx):self.db,self.stats,self.tx=db,stats,tx
        def __getattr__(self,name):return getattr(self.db,name)
        def execute(self,sql,params=()):
            self.stats['sql_calls']+=1
            if 'SELECT COUNT(*), MIN(id), MAX(id) FROM history_events' in sql and params==(self.tx,):self.stats['whole_group_checks']+=1
            return CursorProbe(self.db.execute(sql,params),self.stats)
    for count in (20,200,400):
        owned,runtime,repo,ref,request=await _ready_world(tmp_path/str(count),count=count)
        try:
            stats={'max_fetch':0,'fetched_rows':0,'sql_calls':0,'whole_group_checks':0}
            next_tx=owned.connection.execute('SELECT MAX(id)+1 FROM history_transactions').fetchone()[0]
            measured=replace(owned,connection=MeasuredConnection(owned.connection,stats,next_tx))
            gc.collect();tracemalloc.start();tracemalloc.reset_peak()
            key=new_operation_key();saved=transactions.finish_segment_recording(request,key,measured,batch_size=4)
            write_peak=tracemalloc.get_traced_memory()[1];tracemalloc.stop()
            assert saved.kind=='completed',saved.error
            gc.collect();tracemalloc.start();tracemalloc.reset_peak()
            result=transactions.verify_segment_finish(request,key,measured,execution_finished=True,batch_size=4)
            verify_peak=tracemalloc.get_traced_memory()[1];tracemalloc.stop()
            assert result==saved.result and result.output_count==count
            assert stats['max_fetch']<=128 and stats['whole_group_checks']<=3
            lookup='SELECT id,created_event_id FROM outputs WHERE source_action_id=? AND device_file_id=? AND kind=? ORDER BY id LIMIT 2'
            file_id=repo.read_members(ref,None,1,owned).items[0].file_id
            plans=owned.connection.execute('EXPLAIN QUERY PLAN '+lookup,(ref.action_id,file_id,1)).fetchall()
            assert any('INDEX' in row[-1] and 'sqlite_autoindex_outputs' in row[-1] for row in plans)
            steps=0
            def step():
                nonlocal steps
                steps+=1
                return 0
            owned.connection.set_progress_handler(step,1)
            try:
                with closing(owned.connection.execute(lookup,(ref.action_id,file_id,1))) as query:
                    lookup_rows=query.fetchmany(2)
            finally:owned.connection.set_progress_handler(None,0)
            assert len(lookup_rows)==1 and steps<80
            records.append({'N':count,'B':4,'write_peak':write_peak,'verify_peak':verify_peak,
                'cache_size':owned.connection.execute('PRAGMA cache_size').fetchone()[0],
                'lookup_vm_steps':steps,'lookup_rows':len(lookup_rows),'lookup_plan':[row[-1] for row in plans],**stats})
        finally:owned.connection.close()
    print('P3A_GROWTH '+json.dumps(records))
    # 原结果页上限128高于本次候选B=4；N=200包含完整原页，仍须在固定空间内。
    assert records[1]['write_peak']<records[0]['write_peak']+2_000_000
    assert records[1]['verify_peak']<records[0]['verify_peak']+2_000_000
    assert records[2]['write_peak']<records[1]['write_peak']+400_000
    assert records[2]['verify_peak']<records[1]['verify_peak']+400_000

@pytest.mark.asyncio
@pytest.mark.parametrize('omit_progress',[False,True,'missing_reads','unreliable_reads'])
async def test_actual_last_event_cannot_substitute_for_proved_activation(tmp_path,monkeypatch,omit_progress):
    from camctl.persistence.transaction import update_change,event_envelope
    from camctl.persistence.repositories.outputs import register_outputs_guards
    register_outputs_guards()
    owned,runtime,repo,ref,request=await _ready_world(tmp_path)
    original=transactions._FinishCommand.plan
    def plan(command,scope):
        result=original(command,scope)
        def events():
            for event in result.events:
                if event.event_type==transactions.storage._EVENT['id'] and event.reason==transactions.storage._EVENT['branches']['ACTIVATE']['reason']:
                    row=scope.connection.execute('SELECT id,original_name FROM outputs ORDER BY id LIMIT 1').fetchone()
                    event=event_envelope(event.event_id,event.transaction_id,20,4,(update_change('outputs',row[0],{'original_name':row[1]},{'original_name':'changed.osv'}),),request.occurred_at)
                yield event
        def context(event,transaction):
            current=result.context(event,transaction)
            if omit_progress is True:current.current_reads._segment_guard_state=None
            if event.event_id==transaction.last_event_id:
                if omit_progress=='missing_reads':current=replace(current,current_reads=None)
                elif omit_progress=='unreliable_reads':
                    from types import SimpleNamespace
                    current=replace(current,current_reads=SimpleNamespace(seal_segment_finish=lambda _:None))
            return current
        return replace(result,events=events(),context=context,complete=lambda _:None)
    monkeypatch.setattr(transactions._FinishCommand,'plan',plan)
    try:
        before=_complete_persisted_state(owned)
        saved=transactions.finish_segment_recording(request,new_operation_key(),owned,batch_size=2)
        assert saved.kind=='rolled_back',saved.error
        assert _complete_persisted_state(owned)==before
    finally:owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('omit_header',[False,True])
async def test_direct_terminal_request_cannot_omit_its_complete_group(tmp_path,omit_header):
    from camctl.persistence.transaction import TransactionScope,CommandPlan
    from camctl.contracts.history_values import TransactionRange
    from camctl.history.reads import PlanReadScope
    owned,runtime,repo,ref,request=await _ready_world(tmp_path)
    key=new_operation_key()
    class IncompleteTerminal:
        def plan(self,scope):
            proposal_scope=TransactionScope(scope.connection,scope.max_txn_id,scope.max_event_id)
            complete=transactions._FinishCommand(request,key,2).plan(proposal_scope)
            terminal=next(iter(complete.events))
            if omit_header:terminal=replace(terminal,evidence={})
            allocation=scope.allocate(1)
            context=complete.context(terminal,TransactionRange(allocation.txn_id,
                allocation.first_event_id,allocation.last_event_id))
            action=context.state_rows['actions'][ref.action_id]
            return CommandPlan((terminal,),context.owners,context.state_rows,
                plan_read_scope=PlanReadScope(action['plan_id'],2))
    try:
        before=_fingerprint(owned.connection)
        saved=commit_operation(IncompleteTerminal(),key,owned)
        assert saved.kind=='rolled_back',saved.error
        assert _fingerprint(owned.connection)==before
    finally:owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('earlier_complete',[False,True])
async def test_unheaded_failure_keeps_earlier_complete_scan_applicability(tmp_path,earlier_complete):
    from decimal import Decimal
    from .test_segment_storage import SegmentedCatalog
    from ..capture.result_consumer_fixtures import consumer_world
    from ..capture.test_result_page_history import _request,_actual,_REGISTRY
    from camctl.capture.handlers import _begin_check_round
    from camctl.operations.attempts import AttemptConfig,AttemptFinish
    from camctl.operations.models import ErrorValue
    from camctl.operations.validation import validate_outcome
    from camctl.persistence.repositories.capture import FinishCapture
    from camctl.outputs.catalog import OutputCatalogFacts
    from camctl.capture.media import RecordingFailure
    owned,runtime,action_id,_=await consumer_world(tmp_path,'record',catalog=SegmentedCatalog(),record_control_elapsed_ns=60_000_000_000)
    runtime.check_config=AttemptConfig(3,Decimal('10'),Decimal('0'))
    try:
        for number in (1,2):
            ticket=_begin_check_round(runtime,action_id).ticket
            actual=_actual(ticket,finalized=earlier_complete if number==1 else False,
                error=None if number==1 else ErrorValue('directory_read_failed','device',{'received_bytes':9}))
            _ok(runtime.capture.save_result_page(_request(runtime,ticket,actual),new_operation_key(),owned))
            _ok(runtime.operations.finish_attempt(AttemptFinish(ticket,validate_outcome(ticket,actual,_REGISTRY),
                runtime.wall_us(),retry_wait=number==1),new_operation_key(),owned))
        assert owned.connection.execute('SELECT COUNT(*) FROM segment_sets').fetchone()==(0,)
        before=_fingerprint(owned.connection)
        request=FinishCapture(action_id,(),OutputCatalogFacts(action_id,True),runtime.wall_us(),
            RecordingFailure('capture_result_unconfirmed',{'activity_id':ticket.target_id,'reason':'outputs_unknown'}))
        saved=runtime.capture.finish_capture(request,new_operation_key(),owned)
        assert saved.kind.value==('rolled_back' if earlier_complete else 'completed'),saved.error
        if earlier_complete:assert _fingerprint(owned.connection)==before
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_segmented_prestart_failure_keeps_original_finite_contract(tmp_path):
    from ..capture.result_consumer_fixtures import consumer_world
    from .test_segment_storage import SegmentedCatalog
    from camctl.operations.models import ErrorValue,AttemptStatus,EffectState
    from camctl.operations.attempts import AttemptConfig
    from camctl.devices.ports import DeviceCallResult
    from camctl.capture.handlers import capture_handler
    from decimal import Decimal
    owned,runtime,action_id,_=await consumer_world(tmp_path,'record',catalog=SegmentedCatalog(),start=False)
    try:
        actual=runtime.driver.control.return_value.outcome
        runtime.driver.control.return_value=DeviceCallResult.from_outcome(replace(actual,status=AttemptStatus.FAILED,
            error=ErrorValue('device_error','device'),effect=EffectState.NO_EFFECT,observations=()))
        runtime.start_config=AttemptConfig(1,Decimal('10'))
        await capture_handler('camera_record')(action_id,runtime)
        assert owned.connection.execute('SELECT status FROM actions WHERE id=?',(action_id,)).fetchone()==(4,)
        assert owned.connection.execute('SELECT COUNT(*) FROM segment_sets').fetchone()==(0,)
    finally:owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('closure',['run','plan'])
async def test_final_activation_independently_requires_applicable_closure(tmp_path,monkeypatch,closure):
    from camctl.persistence.transaction import update_change,event_envelope
    from camctl.persistence.repositories.outputs import register_outputs_guards
    register_outputs_guards()
    owned,runtime,repo,ref,request=await _ready_world(tmp_path)
    original=transactions._FinishCommand.plan
    def plan(command,scope):
        result=original(command,scope)
        def events():
            for event in result.events:
                table='operation_runs' if closure=='run' else 'plans'
                if any(row.table==table and row.after.values.get('status')==3 for row in event.rows):
                    row=scope.connection.execute('SELECT id,original_name FROM outputs ORDER BY id LIMIT 1').fetchone()
                    event=event_envelope(event.event_id,event.transaction_id,20,4,
                        (update_change('outputs',row[0],{'original_name':row[1]},{'original_name':'changed.osv'}),),request.occurred_at)
                yield event
        return replace(result,events=events(),complete=lambda db:None)
    monkeypatch.setattr(transactions._FinishCommand,'plan',plan)
    try:
        before=_fingerprint(owned.connection)
        saved=transactions.finish_segment_recording(request,new_operation_key(),owned,batch_size=2)
        assert saved.kind=='rolled_back',saved.error
        assert _fingerprint(owned.connection)==before
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_original_receipt_rejects_inconsistent_finite_plan_header(tmp_path):
    import json
    owned,runtime,repo,ref,request=await _ready_world(tmp_path)
    try:
        key=new_operation_key();saved=transactions.finish_segment_recording(request,key,owned,batch_size=2)
        assert saved.kind=='completed',saved.error
        first=owned.connection.execute('SELECT first_event_id FROM history_transactions WHERE id=?',(saved.boundary.txn_id,)).fetchone()[0]
        raw=owned.connection.execute('SELECT body_json FROM history_events WHERE id=?',(first,)).fetchone()[0]
        body=json.loads(raw);header=body['evidence']['segment_finish_request']
        assert header['complete_plan'] is True
        header['plan_status']=1
        owned.connection.execute('UPDATE history_events SET body_json=? WHERE id=?',(json.dumps(body),first))
        with pytest.raises(transactions.ConsistencyError):
            transactions.verify_segment_finish(request,key,owned,execution_finished=True,batch_size=2)
    finally:owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('siblings',['invalid_only','pending_with_invalid'])
async def test_original_closure_qualification_keeps_birth_terminal_and_later_sibling_facts(tmp_path,siblings):
    from camctl.reporting.policy import start_sync,register_sync_guard
    from camctl.reporting.models import SyncMode
    register_sync_guard()
    owned,runtime,repo,ref,request=await _ready_world(tmp_path,siblings=siblings)
    try:
        invalid=owned.connection.execute('SELECT status,execution_spec_json FROM actions WHERE id=1').fetchone()
        assert invalid==(4,None)
        key=new_operation_key();saved=transactions.finish_segment_recording(request,key,owned,batch_size=2)
        assert saved.kind=='completed',saved.error
        assert saved.result.plan_status==(3 if siblings=='invalid_only' else 2)
        first=owned.connection.execute('SELECT first_event_id FROM history_transactions WHERE id=?',(saved.boundary.txn_id,)).fetchone()[0]
        if siblings=='pending_with_invalid':
            later_key=new_operation_key()
            later=start_sync(later_key,owned,action_id=2,mode=SyncMode.INCREMENTAL,
                after_report_id=999,occurred_at=request.occurred_at+1)
            assert later.kind.value=='completed',later.error
            assert owned.connection.execute('SELECT status FROM plans').fetchone()==(3,)
            actual=owned.connection.execute('SELECT e.id,e.event_type FROM history_events e JOIN history_transactions t '
                'ON t.id=e.transaction_id WHERE t.operation_key=? ORDER BY e.id',(str(later_key),)).fetchall()
            events=transactions.load_event_registry()['events']
            assert [kind for _,kind in actual]==[events[name]['id'] for name in
                ('ACTION_STARTED','ACTION_FINISHED','PLAN_STATUS_CHANGED')]
            assert [identity for identity,_ in actual]==list(range(actual[0][0],actual[0][0]+3))
        assert transactions.verify_segment_finish(request,key,owned,execution_finished=True,batch_size=2)==saved.result
        before=_fingerprint(owned.connection)
        again=transactions.finish_segment_recording(request,key,owned,batch_size=2)
        assert again.kind=='completed' and again.result==saved.result,again.error
        assert _fingerprint(owned.connection)==before
        owned.connection.execute('BEGIN')
        try:qualification=transactions._completion_qualification(owned.connection,request,before_event_id=first,
            transaction=transactions.read_transaction_range(owned.connection,saved.boundary.txn_id))
        finally:owned.connection.execute('ROLLBACK')
        assert qualification['complete_plan']==(siblings=='invalid_only')
        assert qualification['finish_run'] is True
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_preended_results_run_is_not_finished_again_in_original_group(tmp_path):
    from camctl.operations.attempts import StaleRunFinish,RunOutcome
    owned,runtime,repo,ref,request=await _ready_world(tmp_path)
    try:
        run=transactions.row_facts(owned.connection,'operation_runs',request.run_id)
        ended=runtime.operations.finish_stale_runs(StaleRunFinish((run['responsibility_key'],),RunOutcome.SUCCEEDED,
            request.occurred_at),new_operation_key(),owned)
        assert ended.kind.value=='completed',ended.error
        key=new_operation_key();saved=transactions.finish_segment_recording(request,key,owned,batch_size=2)
        assert saved.kind=='completed',saved.error
        first=owned.connection.execute('SELECT first_event_id FROM history_transactions WHERE id=?',(saved.boundary.txn_id,)).fetchone()[0]
        owned.connection.execute('BEGIN')
        try:qualification=transactions._completion_qualification(owned.connection,request,before_event_id=first,
            transaction=transactions.read_transaction_range(owned.connection,saved.boundary.txn_id))
        finally:owned.connection.execute('ROLLBACK')
        assert qualification['finish_run'] is False and qualification['complete_plan'] is True
        assert transactions.verify_segment_finish(request,key,owned,execution_finished=True)==saved.result
    finally:owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('field',['finish_run','complete_plan'])
async def test_original_key_cannot_suppress_applicable_closure_with_false_header(tmp_path,field):
    import json
    owned,runtime,repo,ref,request=await _ready_world(tmp_path)
    try:
        key=new_operation_key();saved=transactions.finish_segment_recording(request,key,owned,batch_size=2)
        assert saved.kind=='completed',saved.error
        first=owned.connection.execute('SELECT first_event_id FROM history_transactions WHERE id=?',(saved.boundary.txn_id,)).fetchone()[0]
        raw=owned.connection.execute('SELECT body_json FROM history_events WHERE id=?',(first,)).fetchone()[0]
        body=json.loads(raw);header=body['evidence']['segment_finish_request'];header[field]=False
        if field=='complete_plan':header['plan_status']=2
        owned.connection.execute('UPDATE history_events SET body_json=? WHERE id=?',(json.dumps(body),first))
        with pytest.raises(transactions.ConsistencyError,match='原F前事实'):
            transactions.verify_segment_finish(request,key,owned,execution_finished=True,batch_size=2)
    finally:owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('siblings,field,value',[(None,'finish_run',False),(None,'complete_plan',False),('pending_with_invalid','complete_plan',True)])
async def test_direct_proposal_cannot_choose_its_own_closure_flags(tmp_path,monkeypatch,siblings,field,value):
    owned,runtime,repo,ref,request=await _ready_world(tmp_path,siblings=siblings)
    original=transactions._FinishCommand.plan
    def plan(command,scope):
        result=original(command,scope)
        def events():
            for event in result.events:
                if 'segment_finish_request' in event.evidence:
                    header={**event.evidence['segment_finish_request'],field:value}
                    if field=='complete_plan':header['plan_status']=3 if value else 2
                    event=replace(event,evidence={'segment_finish_request':header})
                yield event
        return replace(result,events=events(),complete=lambda db:None)
    monkeypatch.setattr(transactions._FinishCommand,'plan',plan)
    try:
        before=_fingerprint(owned.connection)
        saved=transactions.finish_segment_recording(request,new_operation_key(),owned,batch_size=2)
        assert saved.kind=='rolled_back' and '原F前事实' in str(saved.error)
        assert _fingerprint(owned.connection)==before
    finally:owned.connection.close()

@pytest.mark.asyncio
async def test_completion_proof_uses_original_scan_bound_and_actual_stop(tmp_path):
    owned,runtime,repo,ref,request=await _ready_world(tmp_path)
    try:
        proof=transactions.verify_completion_proof(request,owned)
        assert proof.stop_result_event_id<proof.scan_intent_event_id
        assert proof.control_event_id<proof.scan_intent_event_id
        assert proof.control_elapsed_ns==60_000_000_000
    finally:owned.connection.close()

@pytest.mark.asyncio
async def test_confirmed_control_flag_without_original_stop_is_rejected(tmp_path,monkeypatch):
    owned,runtime,repo,ref,request=await _ready_world(tmp_path)
    original=transactions._one
    def one(db,sql,params=()):
        if 'FROM operation_runs r JOIN operation_attempts t' in sql and 'segment_stop_proof' in sql:return None
        return original(db,sql,params)
    monkeypatch.setattr(transactions,'_one',one)
    try:
        saved=transactions.finish_segment_recording(request,new_operation_key(),owned,batch_size=2)
        assert saved.kind=='rolled_back'
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone()==(0,)
    finally:owned.connection.close()

async def _pending_stop_world(tmp_path):
    from ..capture.result_consumer_fixtures import consumer_world,returned,RESULT_EVIDENCE
    from .test_segment_storage import SegmentedCatalog
    from camctl.capture.handlers import capture_handler
    from camctl.operations.attempts import AttemptIntent,AttemptConfig,AttemptTarget,OperationKind,AttemptFinish,RunFinish,RunOutcome
    from camctl.operations.validation import validate_outcome
    owned,runtime,action_id,_=await consumer_world(tmp_path,'record',catalog=SegmentedCatalog(),start=False)
    await capture_handler('camera_record')(action_id,runtime)
    activity_id=owned.connection.execute('SELECT id FROM device_activities WHERE action_id=?',(action_id,)).fetchone()[0]
    intent=AttemptIntent('stop',action_id,OperationKind.STOP,AttemptTarget(activity_id=activity_id),None,runtime.stop_config,runtime.wall_us())
    granted=runtime.operations.begin_attempt(intent,new_operation_key(),owned)
    assert granted.kind.value=='completed',granted.error
    ticket=granted.value.ticket
    actual=returned('stop','stop_confirmed',activity_id)
    finish=AttemptFinish(ticket,validate_outcome(ticket,actual,RESULT_EVIDENCE),runtime.wall_us()+60_000_000,run_finish=RunFinish(RunOutcome.SUCCEEDED))
    return owned,runtime,action_id,finish

@pytest.mark.asyncio
@pytest.mark.parametrize('elapsed',[None,60_000_000_000])
async def test_record_stop_saves_exact_elapsed_with_original_group_and_reuses_key(tmp_path,elapsed):
    from camctl.capture.models import HostTimerStopSave
    owned,runtime,action_id,finish=await _pending_stop_world(tmp_path)
    try:
        key=new_operation_key();saved=runtime.capture.finish_host_timer_stop(finish,HostTimerStopSave(elapsed),key,owned)
        assert saved.kind.value=='completed',saved.error
        activity=owned.connection.execute('SELECT activity_state,control_elapsed_ns FROM device_activities WHERE action_id=?',(action_id,)).fetchone()
        assert activity==(3,elapsed)
        before=_fingerprint(owned.connection)
        again=runtime.capture.finish_host_timer_stop(finish,HostTimerStopSave(elapsed),key,owned)
        assert again.kind.value=='completed',again.error
        assert runtime.capture.finish_host_timer_stop(finish,HostTimerStopSave(1 if elapsed is None else elapsed+1),key,owned).kind.value=='rolled_back'
        assert runtime.capture.finish_host_timer_stop(replace(finish,occurred_at=finish.occurred_at+1),HostTimerStopSave(elapsed),key,owned).kind.value=='rolled_back'
        assert runtime.capture.finish_host_timer_stop(finish,HostTimerStopSave(elapsed),new_operation_key(),owned).kind.value=='rolled_back'
        assert _fingerprint(owned.connection)==before
    finally:owned.connection.close()

@pytest.mark.asyncio
async def test_direct_elapsed_update_cannot_supplement_an_already_saved_stop(tmp_path):
    from camctl.persistence.transaction import CommandPlan,update_change,event_envelope,row_facts
    from camctl.history.reads import PlanReadScope
    owned,runtime,action_id,finish=await _pending_stop_world(tmp_path)
    try:
        saved=runtime.capture.finish_capture_stop(finish,new_operation_key(),owned)
        assert saved.kind.value=='completed',saved.error
        class Supplement:
            def plan(self,scope):
                activity_id=scope.connection.execute('SELECT id FROM device_activities WHERE action_id=?',(action_id,)).fetchone()[0]
                activity=row_facts(scope.connection,'device_activities',activity_id)
                stop=scope.connection.execute('SELECT result_event_id FROM operation_attempts WHERE run_id=?',(finish.ticket.run_id,)).fetchone()[0]
                allocation=scope.allocate(1)
                event=event_envelope(allocation.first_event_id,allocation.txn_id,transactions.load_event_registry()['events']['DEVICE_OBSERVED']['id'],2,
                    (update_change('device_activities',activity_id,{'control_elapsed_ns':None},{'control_elapsed_ns':60_000_000_000}),),finish.occurred_at,{'observation':{'stop_result_event_id':stop,'control_elapsed_ns':60_000_000_000}})
                action=row_facts(scope.connection,'actions',action_id)
                return CommandPlan((event,),{('device_activities',activity_id):('action',action_id)},
                    {'device_activities':{activity_id:activity},'actions':{action_id:action}},plan_read_scope=PlanReadScope(action['plan_id'],128))
        before=_fingerprint(owned.connection)
        result=commit_operation(Supplement(),new_operation_key(),owned)
        assert result.kind=='rolled_back',result.error
        assert _fingerprint(owned.connection)==before
    finally:owned.connection.close()

@pytest.mark.asyncio
async def test_later_stronger_control_fact_does_not_upgrade_frozen_unknown(tmp_path):
    owned,runtime,repo,ref,request=await _ready_world(tmp_path,facts_change={'control_completion':EvidenceState.UNKNOWN})
    try:
        assert request.failure.code=='capture_result_unconfirmed'
        proof=transactions.verify_completion_proof(request,owned)
        assert proof.control_event_id is None and proof.control_elapsed_ns is None
        saved=transactions.finish_segment_recording(request,new_operation_key(),owned,batch_size=2)
        assert saved.kind=='completed',saved.error
        assert saved.result.action_status==4 and saved.result.output_count==5
    finally:owned.connection.close()

@pytest.mark.asyncio
async def test_control_saved_after_original_scan_cannot_strengthen_frozen_request(tmp_path,monkeypatch):
    owned,runtime,repo,ref,request=await _ready_world(tmp_path)
    original=transactions._one
    def one(db,sql,params=()):
        if 'json_extract(e.body_json' in sql and 'control_elapsed_ns' in sql:return None
        return original(db,sql,params)
    monkeypatch.setattr(transactions,'_one',one)
    try:
        saved=transactions.finish_segment_recording(request,new_operation_key(),owned,batch_size=2)
        assert saved.kind=='rolled_back' and 'scan之前' in str(saved.error)
    finally:owned.connection.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('reuse_wait',[False,True])
async def test_original_file_wait_proof_can_reuse_earlier_actual_results_attempt(tmp_path,reuse_wait):
    from decimal import Decimal
    from .test_segment_storage import SegmentedCatalog,SegmentRepository,SegmentSetCreate,SegmentMemberInput
    from ..capture.result_consumer_fixtures import consumer_world
    from ..capture.test_record_file_completion_wait import _result_port,_stop_context,_completed_result
    from camctl.capture.handlers import _listing_round,_finish_listing_result,_register_listing
    from camctl.operations.models import ErrorValue
    from camctl.operations.attempts import AttemptConfig
    class SegmentWaitCatalog(SegmentedCatalog):
        def parameter_definition(self,*args):
            definition=super().parameter_definition(*args)
            if definition is not None and args[1]=='camera_record':
                factory=definition.task_factory
                definition=replace(definition,task_factory=lambda params:replace(factory(params),file_completion_wait_s=Decimal('5')))
            return definition
    owned,runtime,action_id,_=await consumer_world(tmp_path,'record',catalog=SegmentWaitCatalog(),record_control_elapsed_ns=60_000_000_000)
    runtime.check_config=AttemptConfig(3,Decimal('10'),Decimal('0'))
    try:
        context=_stop_context(owned,action_id);source_id=None
        if reuse_wait:
            async def first(request,batch):return _completed_result(request.ticket,context,error=ErrorValue('metadata_failed','result',{}))
            _result_port(runtime,first)
            listing=await _listing_round(runtime,action_id)
            source_id=runtime.capture.read_last_result_page(listing.ticket,owned).ref.event_id
            _finish_listing_result(runtime,listing,retry_wait=True)
        entry={'identity':'/DCIM/CAM001/CAM_20261021090000_0001_D.OSV','locator':{'path':'/DCIM/CAM001/CAM_20261021090000_0001_D.OSV'},'complete':True,'size_bytes':10,'kind':'video','format_id':'osv'}
        async def final(request,batch):return _completed_result(request.ticket,context,entries=[entry],finalized=True,source_page_event_id=source_id)
        _result_port(runtime,final)
        listing=await _listing_round(runtime,action_id)
        saved=runtime.capture.read_last_result_page(listing.ticket,owned)
        _register_listing(runtime,action_id,listing)
        _finish_listing_result(runtime,listing)
        attempt_id=owned.connection.execute('SELECT id FROM operation_attempts WHERE run_id=? AND attempt_no=?',(listing.ticket.run_id,listing.ticket.attempt_id)).fetchone()[0]
        ref=SegmentSetRef(action_id,attempt_id,saved.ref.event_id,1);repo=SegmentRepository()
        _ok(repo.create(SegmentSetCreate(ref,int(listing.ticket.target_id),listing.ticket.run_id,60000,runtime.wall_us()),new_operation_key(),owned))
        file_id=saved.file_ids[0][1]
        file_event=owned.connection.execute('SELECT last_event_id FROM device_files WHERE id=?',(file_id,)).fetchone()[0]
        member=SegmentMemberInput(file_id,saved.ref.event_id,file_event,SegmentMemberRole.ORIGINAL,OwnershipState.OWNED,FileCompletionState.COMPLETE)
        _ok(repo.append_members(ref,(member,),runtime.wall_us(),new_operation_key(),owned))
        _ok(repo.fix_members(ref,runtime.wall_us(),new_operation_key(),owned))
        facts,assessment,gaps=_assessment(ref,(member,),owned)
        assert not gaps
        _ok(repo.save_assessment(ref,facts,assessment,runtime.wall_us(),new_operation_key(),owned))
        member_id=repo.read_members(ref,None,1,owned).items[0].member_id
        _ok(repo.append_scope(ref,(member_id,),runtime.wall_us(),new_operation_key(),owned))
        row=repo.read_set(ref,owned);request=_request_for(ref,row,runtime.wall_us(),assessment,1)
        proof=transactions.verify_completion_proof(request,owned)
        assert proof.wait_source_page_event_id==(source_id or saved.ref.event_id)
        key=new_operation_key();receipt=transactions.finish_segment_recording(request,key,owned,batch_size=1)
        assert receipt.kind=='completed',receipt.error
        assert transactions.verify_segment_finish(request,key,owned,execution_finished=True,batch_size=1)==receipt.result
    finally:owned.connection.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('elapsed',[None,60_000_000_000])
async def test_record_stop_unknown_commit_receipt_reuses_exact_original_group(tmp_path,elapsed):
    import sqlite3
    from camctl.capture.models import HostTimerStopSave
    owned,runtime,action_id,finish=await _pending_stop_world(tmp_path)
    def hook(stage,sql,params):
        if stage=='after' and sql=='COMMIT':raise sqlite3.OperationalError('controlled lost STOP receipt')
    try:
        key=new_operation_key()
        saved=runtime.capture.finish_host_timer_stop(finish,HostTimerStopSave(elapsed),key,replace(owned,connection=ConnectionProbe(owned.connection,hook)))
        assert saved.kind.value=='unknown'
        before=_fingerprint(owned.connection)
        again=runtime.capture.finish_host_timer_stop(finish,HostTimerStopSave(elapsed),key,owned)
        assert again.kind.value=='completed',again.error
        assert _fingerprint(owned.connection)==before
    finally:owned.connection.close()

@pytest.mark.asyncio
async def test_unknown_actual_stop_cannot_create_an_end_or_control_duration(tmp_path):
    from camctl.capture.models import HostTimerStopSave
    from camctl.operations.models import CallOutcome,AttemptStatus,EffectState,ErrorValue,Settlement,SettlementBasis,EvidenceValue
    from camctl.operations.validation import validate_outcome
    from ..capture.result_consumer_fixtures import RESULT_EVIDENCE
    owned,runtime,action_id,finish=await _pending_stop_world(tmp_path)
    try:
        actual=CallOutcome(status=AttemptStatus.UNKNOWN,effect=EffectState.UNKNOWN,error=ErrorValue('result_unconfirmed','device'),
            settlement=Settlement(SettlementBasis.OBSERVED,EvidenceValue('stop_returned',1,{})))
        request=replace(finish,outcome=validate_outcome(finish.ticket,actual,RESULT_EVIDENCE))
        saved=runtime.capture.finish_host_timer_stop(request,HostTimerStopSave(60_000_000_000),new_operation_key(),owned)
        assert saved.kind.value=='rolled_back'
        assert owned.connection.execute('SELECT activity_state,control_elapsed_ns FROM device_activities WHERE action_id=?',(action_id,)).fetchone()==(2,None)
    finally:owned.connection.close()


def _complete_persisted_state(owned):
    """小型反例比较全部业务/历史/目录/水位行，不以数量代替内容。"""
    tables=tuple(row[0] for row in owned.connection.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"))
    return {table:tuple(owned.connection.execute(f'SELECT * FROM {table} ORDER BY rowid')) for table in tables}


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['valid','name','time','id','batch','direct'])
async def test_accepted_completion_matches_original_full_request(tmp_path,monkeypatch,mode):
    from .test_segment_storage import _completion_command
    from camctl.persistence.transaction import row_change
    if mode=='direct':
        owned,runtime,repo,ref,members=await _assessed_world(tmp_path,DefaultScopeState.NONEMPTY)
        row=repo.read_set(ref,owned)
        request=transactions.FinishSegmentRecording(ref,row['activity_id'],row['run_id'],runtime.wall_us())
    else:
        owned,runtime,repo,ref,request=await _ready_world(tmp_path,count=5)
    original=transactions._FinishCommand.plan
    def altered_plan(command,scope):
        result=original(command,scope)
        def events():
            pending=None
            for event in result.events:
                if event.event_type==20:
                    row=event.rows[0]
                    if mode=='name':event=replace(event,rows=(row_change('outputs',row.row_id,{**row.after.values,'original_name':'changed.osv'}),*event.rows[1:]))
                    elif mode=='time':event=replace(event,occurred_at=event.occurred_at+1)
                    elif mode=='id':event=replace(event,rows=(replace(row,row_id=row.row_id+100),*event.rows[1:]))
                if mode=='batch' and event.event_type==transactions.storage._EVENT['id'] and event.reason==transactions.storage._EVENT['branches']['PUBLISH']['reason']:
                    if pending is None:
                        pending=event
                        continue
                    if pending is not False:
                        def repartition(base,rows):
                            bindings=tuple((r.row_id,r.after.values['output_id']) for r in rows)
                            return replace(base,rows=tuple(rows),evidence={'segment_request':{'ref':transactions.document(ref),'input':transactions.storage._request_document(bindings)}})
                        yield repartition(pending,(pending.rows[0],))
                        event=repartition(event,(pending.rows[1],*event.rows))
                        pending=False
                yield event
        return replace(result,events=events(),complete=lambda _:None)
    try:
        key=new_operation_key()
        before=_complete_persisted_state(owned)
        if mode=='direct':receipt=commit_operation(_completion_command(ref,members,request.occurred_at,key),key,owned)
        else:
            if mode!='valid':monkeypatch.setattr(transactions._FinishCommand,'plan',altered_plan)
            receipt=transactions.finish_segment_recording(request,key,owned,batch_size=2)
        if mode in ('valid','direct'):
            assert receipt.kind=='completed',receipt.error
            result=transactions.verify_segment_finish(request,key,owned,execution_finished=True,batch_size=1)
            assert result is not None
            assert result.output_count==(2 if mode=='direct' else 5)
            assert transactions.verify_segment_finish(request,key,owned,execution_finished=True,batch_size=128)==result
        else:
            assert receipt.kind=='rolled_back',receipt.error
            assert _complete_persisted_state(owned)==before
    finally:owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('entry',['bounded','finite'])
@pytest.mark.parametrize('stage,field',[
    ('F','evidence'),('original','clock'),('original','after'),
    ('preview','name'),('preview','origin_id'),('publish','order'),
    ('run','time'),('run','before'),('plan','clock'),('L','time')])
async def test_every_actual_stage_must_match_frozen_protocol(tmp_path,monkeypatch,entry,stage,field):
    from .test_segment_storage import _preview_world,_candidate_drafts,_completion_command
    from camctl.persistence.transaction import row_change
    owned,runtime,repo,ref,members,_,_=await _preview_world(tmp_path,original_sizes=(10,)*3,paired_index=1,page_size=1)
    _finish_attempt(owned,ref,runtime)
    row=repo.read_set(ref,owned)
    request=transactions.FinishSegmentRecording(ref,row['activity_id'],row['run_id'],runtime.wall_us())
    changed=False
    def mutate(event):
        nonlocal changed
        types=transactions.load_event_registry()['events']
        current=('F' if event.event_type==types['ACTION_FINISHED']['id'] else
            ('original' if event.reason==1 else 'preview') if event.event_type==types['OUTPUT_REGISTERED']['id'] else
            ('publish' if event.reason==7 else 'L') if event.event_type==types['SEGMENT_FACTS']['id'] else
            'run' if event.event_type==types['OPERATION_CONFIGURED']['id'] else 'plan')
        if changed or current!=stage:return event
        changed=True
        if field=='clock':return replace(event,clock_status=1)
        if field=='time':return replace(event,occurred_at=event.occurred_at+1)
        if field=='evidence':return replace(event,evidence={**event.evidence,'other':True})
        if field=='order':
            rows=tuple(reversed(event.rows))
            bindings=tuple((row.row_id,row.after.values['output_id']) for row in rows)
            return replace(event,rows=rows,evidence={'segment_request':{'ref':transactions.document(ref),'input':transactions.storage._request_document(bindings)}})
        if field=='origin_id':return replace(event,rows=(event.rows[0],replace(event.rows[1],row_id=event.rows[1].row_id+100)))
        row=event.rows[0]
        if field=='before':return replace(event,rows=(replace(row,before=replace(row.before,values={**row.before.values,'retry_wait_required':1})),))
        values={**row.after.values,**({'original_name':'other.LRF'} if field=='name' else {'availability':2})}
        return replace(event,rows=(row_change(row.table,row.row_id,values),*event.rows[1:]))
    try:
        key=new_operation_key();before=_complete_persisted_state(owned)
        if entry=='bounded':
            original=transactions._FinishCommand.plan
            def plan(command,scope):
                saved=original(command,scope)
                return replace(saved,events=(mutate(event) for event in saved.events),complete=lambda _:None)
            monkeypatch.setattr(transactions._FinishCommand,'plan',plan)
            receipt=transactions.finish_segment_recording(request,key,owned,batch_size=2)
        else:
            command=_completion_command(ref,members,request.occurred_at,key,drafts=_candidate_drafts(repo,ref,owned),batch_size=2)
            class Altered:
                def plan(self,scope):
                    saved=command.plan(scope)
                    return replace(saved,events=tuple(mutate(event) for event in saved.events))
            receipt=commit_operation(Altered(),key,owned)
        assert changed,(stage,field,receipt.error)
        assert receipt.kind=='rolled_back',receipt.error
        assert _complete_persisted_state(owned)==before
    finally:owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('entry',['bounded','finite'])
@pytest.mark.parametrize('batch',[1,2,128])
async def test_both_entries_use_one_canonical_order_and_original_batch(tmp_path,entry,batch):
    from .test_segment_storage import _preview_world,_candidate_drafts,_completion_command
    owned,runtime,repo,ref,members,_,_=await _preview_world(tmp_path,original_sizes=(10,)*5,paired_index=1,page_size=1)
    _finish_attempt(owned,ref,runtime)
    row=repo.read_set(ref,owned)
    request=transactions.FinishSegmentRecording(ref,row['activity_id'],row['run_id'],runtime.wall_us())
    try:
        key=new_operation_key()
        if entry=='bounded':saved=transactions.finish_segment_recording(request,key,owned,batch_size=batch)
        else:saved=commit_operation(_completion_command(ref,members,request.occurred_at,key,drafts=_candidate_drafts(repo,ref,owned),batch_size=batch),key,owned)
        assert saved.kind=='completed',saved.error
        # 手算的阶段次序：5原片、1预览、5条范围、原RESULTS与最后计划共同收场。
        types=transactions.load_event_registry()['events']
        actual=tuple(owned.connection.execute('SELECT event_type,json_extract(body_json,\'$.reason\'),json_array_length(body_json,\'$.rows\') FROM history_events WHERE transaction_id=? ORDER BY id',(saved.boundary.txn_id,)))
        expected=[(types['ACTION_FINISHED']['id'],1,1)]+[(types['OUTPUT_REGISTERED']['id'],1,1)]*5+[(types['OUTPUT_REGISTERED']['id'],3,2)]
        expected.extend((types['SEGMENT_FACTS']['id'],7,min(batch,5-offset)) for offset in range(0,5,batch))
        expected.extend([(types['OPERATION_CONFIGURED']['id'],3,1),(types['PLAN_STATUS_CHANGED']['id'],2,1),(types['SEGMENT_FACTS']['id'],8,1)])
        assert actual==tuple(expected)
        result=transactions.verify_segment_finish(request,key,owned,execution_finished=True,batch_size=1)
        before=_complete_persisted_state(owned)
        repeated=transactions.finish_segment_recording(request,key,owned,batch_size=128 if batch==1 else 1)
        assert repeated.kind=='completed' and repeated.result==result,repeated.error
        assert _complete_persisted_state(owned)==before
    finally:owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('entry',['bounded','finite'])
async def test_context_cannot_replace_original_transaction_connection(tmp_path,monkeypatch,entry):
    from .test_segment_storage import _completion_command
    from camctl.persistence.event_reads import SqlEventReads
    owned,runtime,repo,ref,members=await _assessed_world(tmp_path,DefaultScopeState.UNKNOWN)
    _finish_attempt(owned,ref,runtime)
    row=repo.read_set(ref,owned)
    request=_request_for(ref,row,runtime.wall_us(),repo.read_assessment(ref,owned),2)
    path=owned.connection.execute('PRAGMA database_list').fetchone()[2]
    observer=open_existing(Path(path),DbOpenMode.EXISTING_RO,DbConfig())
    observer.connection.execute('BEGIN')
    observer.connection.execute('SELECT MAX(id) FROM history_events').fetchone()
    original=SqlEventReads.__init__
    replaced=False
    def initialize(reads,*args,**kwargs):
        nonlocal replaced
        original(reads,*args,**kwargs)
        if not replaced:
            replaced=True
            reads.connection=observer.connection
    monkeypatch.setattr(SqlEventReads,'__init__',initialize)
    try:
        before=_complete_persisted_state(owned);key=new_operation_key()
        if entry=='bounded':saved=transactions.finish_segment_recording(request,key,owned,batch_size=2)
        else:saved=commit_operation(_completion_command(ref,members,request.occurred_at,key),key,owned)
        assert replaced
        assert saved.kind=='rolled_back',saved.error
        assert _complete_persisted_state(owned)==before
    finally:observer.connection.close();owned.connection.close()

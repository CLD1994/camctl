"""统一结果集合绑定原动作、完整H0和真实来源，不假造完整末页。"""
from dataclasses import replace

import pytest

from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.segments import SegmentRepository,register_segment_guards
from camctl.persistence.transaction import BoundedCommandPlan,commit_operation
from camctl.history.validators import EventContext

from .test_segment_file_proofs import _boundary
from .test_segment_storage import SegmentedCatalog
from ..capture.result_consumer_fixtures import consumer_world

pytestmark=pytest.mark.asyncio
register_segment_guards()


def _request(runtime,owned,action_id):
    from camctl.capture.segment_storage import ResultSetCreate,SegmentSourceKind,PublicationKind
    activity=owned.connection.execute('SELECT id FROM device_activities WHERE action_id=?',
        (action_id,)).fetchone()[0]
    return ResultSetCreate(action_id,activity,None,SegmentSourceKind.NO_RESULTS,_boundary(owned),
        None,PublicationKind.RESULT,None,None,runtime.wall_us())


@pytest.mark.parametrize('bounded',[False,True])
async def test_no_results_has_actual_set_identity_and_original_boundary(tmp_path,bounded):
    from camctl.capture.segment_storage import SegmentResultRef
    from camctl.persistence.repositories.segment_result_sets import CreateResultSetCommand
    owned,runtime,action_id,_=await consumer_world(tmp_path,'record',catalog=SegmentedCatalog())
    try:
        request=_request(runtime,owned,action_id)
        key=new_operation_key()
        repository=SegmentRepository()
        if bounded:
            class Command:
                def plan(self,scope):
                    plan=CreateResultSetCommand(request,key).plan(scope)
                    from camctl.persistence.event_reads import SqlEventReads
                    return BoundedCommandPlan(plan.events,
                        lambda event,transaction:EventContext(transaction,plan.owners,plan.state_rows,
                            current_reads=SqlEventReads(scope.connection,plan.segment_read_scope)),
                        plan.result,lambda connection:None)
            receipt=commit_operation(Command(),key,owned)
            assert receipt.kind=='completed',receipt.error
            ref=receipt.result
        else:
            receipt=repository.create_result_set(request,key,owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED,receipt.error
            ref=receipt.value
        assert isinstance(ref,SegmentResultRef) and ref.action_id==action_id and ref.version==1
        row=repository.read_result_set(ref,owned)
        assert row['id']==ref.set_id and row['activity_id']==request.activity_id and row['run_id'] is None
        assert (row['source_txn_id'],row['source_event_id'])==(
            request.source_boundary.txn_id,request.source_boundary.last_event_id)
        assert row['current_attempt_id'] is None and row['current_final_page_event_id'] is None
        assert repository.read_current_segment_ref(ref,owned) is None
        assert repository.create_result_set(request,key,owned).value==ref
        assert repository.create_result_set(replace(request,occurred_at=request.occurred_at+1),
            key,owned).kind is DbOutcomeKind.ROLLED_BACK
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone()==(0,)
    finally:
        owned.connection.close()


def _finish_scan(owned,runtime,saved,*,retry_wait=False):
    from camctl.operations.attempts import AttemptFinish
    from camctl.operations.validation import validate_outcome
    from camctl.persistence.repositories.operations import OperationRepository
    from ..capture.test_record_file_completion_wait import _REGISTRY
    receipt=OperationRepository().finish_attempt(AttemptFinish(saved.ref.ticket,
        validate_outcome(saved.ref.ticket,saved.page.outcome,_REGISTRY),runtime.wall_us(),
        retry_wait=retry_wait),new_operation_key(),owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED,receipt.error


@pytest.mark.parametrize('latest',['first','same','missing','failed','metadata'])
async def test_source_uses_latest_actual_scan_and_preserves_all_run_files(tmp_path,latest):
    from camctl.capture.segment_storage import ResultSetCreate,SegmentSourceKind,PublicationKind
    from camctl.capture.handlers import _begin_check_round
    from camctl.capture.result_pages import ResultPageSave
    from camctl.operations.models import ErrorValue
    from camctl.operations.validation import validate_outcome
    from .test_segment_scan_pairing import _ready
    from .test_segment_file_pairing import _entry
    from ..capture.test_result_page_history import _actual
    from ..capture.test_record_file_completion_wait import _REGISTRY
    owned,runtime,first,ids,_=await _ready(tmp_path,[_entry('a.OSV')])
    try:
        _finish_scan(owned,runtime,first,retry_wait=latest!='first')
        current=first
        if latest!='first':
            action_id=owned.connection.execute('SELECT action_id FROM operation_runs WHERE id=?',
                (first.ref.ticket.run_id,)).fetchone()[0]
            ticket=_begin_check_round(runtime,action_id).ticket
            actual=_actual(ticket,entries=[_entry('b.OSV' if latest=='missing' else 'a.OSV',
                size=None if latest=='metadata' else 10)],finalized=latest!='failed',
                error=ErrorValue('transport_timeout','baseline') if latest=='failed' else None)
            ref=runtime.capture.save_result_page(ResultPageSave(ticket,1,None,
                validate_outcome(ticket,actual,_REGISTRY),runtime.wall_us()),new_operation_key(),owned)
            assert ref.kind is DbOutcomeKind.COMPLETED,ref.error
            current=runtime.capture.read_result_page(ref.value,owned)
            _finish_scan(owned,runtime,current)
        action_id=owned.connection.execute('SELECT action_id FROM operation_runs WHERE id=?',
            (first.ref.ticket.run_id,)).fetchone()[0]
        expected=SegmentSourceKind.COMPLETE_SCAN if latest in ('first','same') else SegmentSourceKind.RETAINED_SOURCES
        scan=None if latest in ('failed','metadata') else current.ref
        request=ResultSetCreate(action_id,int(first.ref.ticket.target_id),first.ref.ticket.run_id,
            expected,_boundary(owned),scan,PublicationKind.RESULT,None,None,runtime.wall_us())
        receipt=SegmentRepository().create_result_set(request,new_operation_key(),owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED,receipt.error
        found=SegmentRepository().read_current_segment_ref(receipt.value,owned)
        assert (None if found is None else found.final_page_event_id)==(None if scan is None else scan.event_id)
        # 来源输入仍须包含早轮A，最后一轮不完整也不能将它丢掉。
        page=SegmentRepository().read_result_member_inputs(receipt.value,None,1,owned)
        assert page.items[0].file_id==ids['/DCIM/a.OSV']
        assert page.items[0].file_event_id<=request.source_boundary.last_event_id
        repository=SegmentRepository()
        inputs=repository.read_result_member_inputs(receipt.value,None,128,owned).items
        appended=repository.append_result_members(receipt.value,inputs,runtime.wall_us(),new_operation_key(),owned)
        assert appended.kind is DbOutcomeKind.COMPLETED,appended.error
        fixed=repository.fix_result_members(receipt.value,runtime.wall_us(),new_operation_key(),owned)
        assert fixed.kind is DbOutcomeKind.COMPLETED,fixed.error
        assert repository.read_result_set(receipt.value,owned)['member_count']==(2 if latest=='missing' else 1)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('changed',['activity','run','kind','boundary'])
async def test_create_result_set_rejects_unrelated_source(tmp_path,changed):
    from camctl.capture.segment_storage import SegmentSourceKind
    from camctl.contracts.history_values import HistoryBoundary
    owned,runtime,action_id,_=await consumer_world(tmp_path,'record',catalog=SegmentedCatalog())
    try:
        request=_request(runtime,owned,action_id)
        if changed=='activity':request=replace(request,activity_id=request.activity_id+1)
        elif changed=='run':request=replace(request,run_id=999)
        elif changed=='kind':request=replace(request,source_kind=SegmentSourceKind.RETAINED_SOURCES)
        else:request=replace(request,source_boundary=HistoryBoundary(
            request.source_boundary.txn_id,request.source_boundary.last_event_id-1))
        before=owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()
        receipt=SegmentRepository().create_result_set(request,new_operation_key(),owned)
        assert receipt.kind is DbOutcomeKind.ROLLED_BACK,receipt.error
        assert owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()==before
        assert owned.connection.execute('SELECT COUNT(*) FROM segment_sets').fetchone()==(0,)
    finally:
        owned.connection.close()


async def test_create_rejects_file_qualification_that_changed_after_original_boundary(tmp_path):
    from camctl.capture.files import FileCompletionSave
    from camctl.capture.segment_storage import ResultSetCreate,SegmentSourceKind,PublicationKind
    from .test_segment_file_proofs import _proof_world
    from .test_segment_file_pairing import _entry
    owned,runtime,action_id,saved,file_id,context=await _proof_world(tmp_path,entries=[_entry('a.OSV')])
    try:
        _finish_scan(owned,runtime,saved)
        request=ResultSetCreate(action_id,int(saved.ref.ticket.target_id),saved.ref.ticket.run_id,
            SegmentSourceKind.RETAINED_SOURCES,_boundary(owned),saved.ref,
            PublicationKind.RESULT,None,None,runtime.wall_us())
        receipt=runtime.capture.save_file_completion(FileCompletionSave(file_id,3,runtime.wall_us(),
            basis=3,observation=saved.page.entries[0].evidence,size_bytes=10,
            activity_id=int(context['activity_id']),result_page_event_id=saved.ref.event_id,
            stop_result_event_id=context['stop_result_event_id']),new_operation_key(),owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED,receipt.error
        created=SegmentRepository().create_result_set(request,new_operation_key(),owned)
        assert created.kind is DbOutcomeKind.ROLLED_BACK,created.error
        assert owned.connection.execute('SELECT COUNT(*) FROM segment_sets').fetchone()==(0,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('bypass',[None,'finite','bounded'])
async def test_saved_result_set_prevents_another_actual_results_attempt(tmp_path,monkeypatch,bypass):
    from camctl.capture.segment_storage import ResultSetCreate,SegmentSourceKind,PublicationKind
    from camctl.capture.handlers import _begin_check_round
    from .test_segment_scan_pairing import _ready
    from .test_segment_file_pairing import _entry
    owned,runtime,saved,_,_=await _ready(tmp_path,[_entry('a.OSV')])
    try:
        _finish_scan(owned,runtime,saved,retry_wait=True)
        action_id=owned.connection.execute('SELECT action_id FROM operation_runs WHERE id=?',
            (saved.ref.ticket.run_id,)).fetchone()[0]
        request=ResultSetCreate(action_id,int(saved.ref.ticket.target_id),saved.ref.ticket.run_id,
            SegmentSourceKind.COMPLETE_SCAN,_boundary(owned),saved.ref,
            PublicationKind.RESULT,None,None,runtime.wall_us())
        result=SegmentRepository().create_result_set(request,new_operation_key(),owned)
        assert result.kind is DbOutcomeKind.COMPLETED,result.error
        before=owned.connection.execute('SELECT COUNT(*) FROM operation_attempts').fetchone()
        if bypass is None:
            decision=_begin_check_round(runtime,action_id)
            assert decision.ticket is None
        else:
            from camctl.operations.attempts import AttemptIntent,AttemptTarget,OperationKind
            from camctl.persistence.repositories.operations import BeginAttemptCommand
            from camctl.persistence.repositories import segment_result_sets
            from camctl.persistence.event_reads import SqlEventReads
            intent=AttemptIntent(operation='result',action_id=action_id,
                kind=OperationKind.CHECK_CAPTURE_RESULTS,
                target=AttemptTarget(activity_id=int(saved.ref.ticket.target_id)),
                query_purpose=None,config=runtime.check_config,occurred_at=runtime.wall_us())
            key=new_operation_key()
            class Command:
                def plan(self,scope):
                    # 仅跳过命令的预检；内核具名守卫仍读取真实已固定集合。
                    with monkeypatch.context() as patch:
                        patch.setattr(segment_result_sets,'result_run_has_set',lambda *args:False)
                        plan=BeginAttemptCommand(intent,key).plan(scope)
                    if bypass=='bounded':
                        return BoundedCommandPlan(plan.events,
                            lambda event,transaction:EventContext(transaction,plan.owners,plan.state_rows,
                                current_reads=SqlEventReads(scope.connection,plan.plan_read_scope)),
                            None,lambda connection:None)
                    return plan
            receipt=commit_operation(Command(),key,owned)
            assert receipt.kind=='rolled_back' and '已固定' in str(receipt.error)
        assert owned.connection.execute('SELECT COUNT(*) FROM operation_attempts').fetchone()==before
    finally:
        owned.connection.close()


@pytest.mark.parametrize('bounded',[False,True])
@pytest.mark.parametrize('mutation',['source','identity','no_scope'])
async def test_create_guard_rechecks_self_reported_values(tmp_path,bounded,mutation):
    from camctl.capture.segment_storage import document
    from camctl.persistence.repositories.segment_result_sets import CreateResultSetCommand
    from camctl.persistence.event_reads import SqlEventReads
    owned,runtime,action_id,_=await consumer_world(tmp_path,'record',catalog=SegmentedCatalog())
    try:
        request=_request(runtime,owned,action_id)
        key=new_operation_key()
        before=owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()
        class Command:
            def plan(self,scope):
                plan=CreateResultSetCommand(request,key).plan(scope)
                event=plan.events[0]
                ref=plan.result
                if mutation=='identity':
                    ref=replace(ref,set_id=ref.set_id+100)
                    event=replace(event,rows=(replace(event.rows[0],row_id=ref.set_id),),
                        evidence={'segment_request':{'ref':document(ref),'input':document(request)}})
                    plan=replace(plan,owners={('segment_sets',ref.set_id):('action',ref.action_id)},
                        segment_read_scope=replace(plan.segment_read_scope,ref=ref))
                elif mutation=='source':
                    row=event.rows[0]
                    values={**row.after.values,'activity_id':request.activity_id+100}
                    raw={**document(request),'activity_id':request.activity_id+100}
                    event=replace(event,rows=(replace(row,after=replace(row.after,values=values)),),
                        evidence={'segment_request':{'ref':document(ref),'input':raw}})
                if bounded:
                    return BoundedCommandPlan((event,),
                        lambda event,transaction:EventContext(transaction,plan.owners,plan.state_rows,
                            current_reads=None if mutation=='no_scope' else SqlEventReads(
                                scope.connection,plan.segment_read_scope)),None,lambda connection:None)
                return replace(plan,events=(event,),segment_read_scope=None if mutation=='no_scope' else plan.segment_read_scope)
        receipt=commit_operation(Command(),key,owned)
        assert receipt.kind=='rolled_back',receipt.error
        assert owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()==before
        assert owned.connection.execute('SELECT COUNT(*) FROM segment_sets').fetchone()==(0,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('limit',[1,2,128])
async def test_result_member_inputs_use_fixed_ref_and_bounded_file_cursor(tmp_path,limit):
    from camctl.capture.segment_storage import ResultSetCreate,SegmentSourceKind,PublicationKind
    from camctl.contracts.values import ConsistencyError
    from .test_segment_scan_pairing import _ready
    from .test_segment_file_pairing import _entry
    owned,runtime,saved,ids,_=await _ready(tmp_path,[_entry(name+'.OSV') for name in ('a','b','c')])
    try:
        _finish_scan(owned,runtime,saved)
        action_id=owned.connection.execute('SELECT action_id FROM operation_runs WHERE id=?',
            (saved.ref.ticket.run_id,)).fetchone()[0]
        request=ResultSetCreate(action_id,int(saved.ref.ticket.target_id),saved.ref.ticket.run_id,
            SegmentSourceKind.COMPLETE_SCAN,_boundary(owned),saved.ref,
            PublicationKind.RESULT,None,None,runtime.wall_us())
        repository=SegmentRepository()
        receipt=repository.create_result_set(request,new_operation_key(),owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED,receipt.error
        cursor=None
        found=[]
        while True:
            page=repository.read_result_member_inputs(receipt.value,cursor,limit,owned)
            assert len(page.items)<=limit
            found.extend(item.file_id for item in page.items)
            if page.next_cursor is None:break
            cursor=page.next_cursor
            with pytest.raises(ConsistencyError):
                repository.read_result_member_inputs(receipt.value,
                    replace(cursor,ref=replace(receipt.value,version=2)),limit,owned)
        assert found==sorted(ids.values())
        plan=owned.connection.execute('EXPLAIN QUERY PLAN SELECT file_id,MAX(page_event_id)'
            ' FROM result_page_files WHERE run_id=? AND file_id>? AND created_event_id<=?'
            ' GROUP BY file_id ORDER BY file_id LIMIT ?',
            (saved.ref.ticket.run_id,0,request.source_boundary.last_event_id,limit+1)).fetchall()
        assert any('result_page_files_run_file' in row[-1] for row in plan)
    finally:
        owned.connection.close()


async def test_no_results_is_readable_through_existing_action_history(tmp_path):
    from camctl.persistence.repositories.history import HistoryRepository,SqliteSnapshotStore
    from camctl.persistence.executor import DbExecutor
    from camctl.persistence.runtime import DbConfig,DbOpenMode,open_existing
    from camctl.history.snapshots import SnapshotRef,prepare_snapshot
    from camctl.contracts.values import ConsistencyError
    owned,runtime,action_id,_=await consumer_world(tmp_path,'record',catalog=SegmentedCatalog())
    try:
        request=_request(runtime,owned,action_id)
        repository=SegmentRepository()
        saved=repository.create_result_set(request,new_operation_key(),owned)
        assert saved.kind is DbOutcomeKind.COMPLETED,saved.error
        history=HistoryRepository(tmp_path/'state.db')
        boundary=history.current_boundary()
        original=repository.read_result_set(saved.value,owned)
        executor=DbExecutor(lambda:open_existing(tmp_path/'state.db',DbOpenMode.EXISTING_RW,DbConfig()),
            capacity=4,enqueue_timeout_seconds=9)
        try:
            store=SqliteSnapshotStore(tmp_path/'state.db',executor)
            image=(await store.load_entity_images((SnapshotRef(1,action_id),)))[0]
            await store.save_snapshots((prepare_snapshot(image),))
        finally:
            await executor.close()
        later=repository.create_result_set(request,new_operation_key(),owned)
        assert later.kind is DbOutcomeKind.COMPLETED,later.error
        for strategy in ('initial','current','snapshot'):
            with history.open_segment_history(action_id,boundary,strategy=strategy,batch_size=1) as handle:
                assert handle.read_result(saved.value)==original
                with pytest.raises(ConsistencyError):handle.read_result(later.value)
                assert handle.active_result_ref() is None
                page=handle.read_page('segment_members',ref=saved.value,limit=1)
                assert page.items==() and page.next_cursor is None
    finally:
        owned.connection.close()


@pytest.mark.parametrize('has_original',[False,True])
async def test_complete_scan_uses_actual_positive_and_negative_pairing_facts(tmp_path,has_original):
    from camctl.capture.files import PairResultFilesSave
    from camctl.capture.segment_storage import ResultSetCreate,SegmentSourceKind,PublicationKind
    from .test_segment_scan_pairing import _ready
    from .test_segment_file_pairing import _entry
    entries=[_entry('raw.LRF')]+([_entry('raw.OSV')] if has_original else [])
    owned,runtime,saved,ids,_=await _ready(tmp_path,entries)
    try:
        preview=ids['/DCIM/raw.LRF']
        paired=runtime.capture.save_result_file_pairing(PairResultFilesSave(preview,saved.ref,
            _boundary(owned),runtime.wall_us()),new_operation_key(),owned)
        assert paired.kind is DbOutcomeKind.COMPLETED,paired.error
        _finish_scan(owned,runtime,saved)
        action_id=owned.connection.execute('SELECT action_id FROM operation_runs WHERE id=?',
            (saved.ref.ticket.run_id,)).fetchone()[0]
        request=ResultSetCreate(action_id,int(saved.ref.ticket.target_id),saved.ref.ticket.run_id,
            SegmentSourceKind.COMPLETE_SCAN,_boundary(owned),saved.ref,
            PublicationKind.RESULT,None,None,runtime.wall_us())
        repository=SegmentRepository()
        result=repository.create_result_set(request,new_operation_key(),owned)
        assert result.kind is DbOutcomeKind.COMPLETED,result.error
        page=repository.read_result_member_inputs(result.value,None,128,owned)
        retained=next(item for item in page.items if item.file_id==preview)
        assert retained.pairing_fact_id==paired.value
        assert retained.pairing_state.name==('PAIRED' if has_original else 'UNPAIRED')
    finally:
        owned.connection.close()


async def test_an_unexecuted_action_does_not_create_a_result_marker_set(tmp_path):
    from datetime import datetime,timezone
    from camctl.capture.segment_storage import ResultSetCreate,SegmentSourceKind,PublicationKind
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import CommandMode
    from camctl.persistence.repositories.acceptance import AcceptanceRepository,ProcessInput
    from camctl.persistence.initialization import initialize_state,InitOutcome
    from camctl.persistence.runtime import DbConfig,DbOpenMode,open_existing
    from ..capture.result_consumer_fixtures import _config,_NOW
    cfg=_config(tmp_path)
    assert initialize_state(cfg,tmp_path/'state.db').outcome is InitOutcome.CREATED
    owned=open_existing(tmp_path/'state.db',DbOpenMode.EXISTING_RW,DbConfig())
    try:
        instant=datetime.fromtimestamp(_NOW//1_000_000,timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        body={'request_id':'1','created_at':instant,'name':'未执行拍摄',
            'actions':[{'name':'拍摄','type':'camera_record','device_id':'cam-1','scheduled_at':instant,
                'params':{'type':'video'},'policy':{'max_delay_ms':5000}}]}
        accepted=AcceptanceRepository().process_input(ProcessInput(ParsedInput('source.json',body),
            SegmentedCatalog(),CommandMode.RUN,_NOW),new_operation_key(),owned)
        assert accepted.kind is DbOutcomeKind.COMPLETED,accepted.error
        request=ResultSetCreate(1,None,None,SegmentSourceKind.NO_RESULTS,_boundary(owned),None,
            PublicationKind.RESULT,None,None,_NOW)
        result=SegmentRepository().create_result_set(request,new_operation_key(),owned)
        assert result.kind is DbOutcomeKind.ROLLED_BACK,result.error
        assert owned.connection.execute('SELECT COUNT(*) FROM segment_sets').fetchone()==(0,)
    finally:
        owned.connection.close()


async def test_complete_directory_without_file_completion_proof_is_retained(tmp_path):
    from camctl.capture.segment_storage import ResultSetCreate,SegmentSourceKind,PublicationKind
    from .test_segment_file_proofs import _proof_world
    from .test_segment_file_pairing import _entry
    owned,runtime,action_id,saved,_,_ =await _proof_world(tmp_path,entries=[_entry('a.OSV')])
    try:
        _finish_scan(owned,runtime,saved)
        request=ResultSetCreate(action_id,int(saved.ref.ticket.target_id),saved.ref.ticket.run_id,
            SegmentSourceKind.RETAINED_SOURCES,_boundary(owned),saved.ref,
            PublicationKind.RESULT,None,None,runtime.wall_us())
        repository=SegmentRepository()
        result=repository.create_result_set(request,new_operation_key(),owned)
        assert result.kind is DbOutcomeKind.COMPLETED,result.error
        item=repository.read_result_member_inputs(result.value,None,1,owned).items[0]
        assert item.completion.name=='UNKNOWN'
        assert repository.create_result_set(replace(request,source_kind=SegmentSourceKind.COMPLETE_SCAN),
            new_operation_key(),owned).kind is DbOutcomeKind.ROLLED_BACK
    finally:
        owned.connection.close()


@pytest.mark.parametrize('bounded',[False,True])
async def test_canonical_members_fix_exact_original_sources(tmp_path,bounded):
    from camctl.capture.segment_storage import ResultSetCreate,SegmentSourceKind,PublicationKind
    from .test_segment_scan_pairing import _ready
    from .test_segment_file_pairing import _entry
    owned,runtime,saved,ids,_=await _ready(tmp_path,[_entry('a.OSV'),_entry('b.OSV')])
    try:
        _finish_scan(owned,runtime,saved)
        action_id=owned.connection.execute('SELECT action_id FROM operation_runs WHERE id=?',
            (saved.ref.ticket.run_id,)).fetchone()[0]
        request=ResultSetCreate(action_id,int(saved.ref.ticket.target_id),saved.ref.ticket.run_id,
            SegmentSourceKind.COMPLETE_SCAN,_boundary(owned),saved.ref,
            PublicationKind.RESULT,None,None,runtime.wall_us())
        repository=SegmentRepository()
        receipt=repository.create_result_set(request,new_operation_key(),owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED,receipt.error
        ref=receipt.value
        inputs=repository.read_result_member_inputs(ref,None,128,owned).items
        from camctl.capture.segment_models import FileCompletionState
        for changed in (replace(inputs[0],file_event_id=inputs[0].page_event_id),
                replace(inputs[0],completion=FileCompletionState.UNKNOWN)):
            refused=repository.append_result_members(ref,(changed,),runtime.wall_us(),new_operation_key(),owned)
            assert refused.kind is DbOutcomeKind.ROLLED_BACK
        key=new_operation_key()
        def save(kind,payload,key):
            from camctl.persistence.repositories.segment_result_members import ResultMembersCommand
            if bounded:
                class Command:
                    def plan(self,scope):
                        plan=ResultMembersCommand(kind,ref,payload,runtime.wall_us(),key).plan(scope)
                        if plan.read_only:return plan
                        from camctl.persistence.event_reads import SqlEventReads
                        return BoundedCommandPlan(plan.events,
                            lambda event,transaction:EventContext(transaction,plan.owners,plan.state_rows,
                                current_reads=SqlEventReads(scope.connection,plan.segment_read_scope)),
                            plan.result,lambda connection:None)
                result=commit_operation(Command(),key,owned)
                assert result.kind=='completed',result.error
                return result.result
            result=(repository.append_result_members(ref,payload,runtime.wall_us(),key,owned)
                if kind=='MEMBERS' else repository.fix_result_members(ref,runtime.wall_us(),key,owned))
            assert result.kind is DbOutcomeKind.COMPLETED,result.error
            return result.value
        assert save('MEMBERS',inputs[:1],key)==ref
        assert save('MEMBERS',inputs[:1],key)==ref
        missing=repository.fix_result_members(ref,runtime.wall_us(),new_operation_key(),owned)
        assert missing.kind is DbOutcomeKind.ROLLED_BACK
        assert save('MEMBERS',inputs[1:],new_operation_key())==ref
        assert save('FIX_RESULT_MEMBERS',None,new_operation_key())==ref
        assert repository.read_result_set(ref,owned)['member_count']==2
        rows=owned.connection.execute('SELECT file_id FROM segment_members ORDER BY file_id').fetchall()
        assert rows==[(value,) for value in sorted(ids.values())]
        repeat=repository.append_result_members(ref,inputs[:1],runtime.wall_us(),new_operation_key(),owned)
        assert repeat.kind is DbOutcomeKind.ROLLED_BACK
        assert repository.append_result_members(ref,inputs[:1],runtime.wall_us(),key,owned).value==ref
        # 后来集合不替换原H的固定成员与不可变来源。
        from camctl.persistence.repositories.history import HistoryRepository
        history=HistoryRepository(tmp_path/'state.db')
        boundary=history.current_boundary()
        expected=repository.read_result_set(ref,owned)
        assert repository.create_result_set(request,new_operation_key(),owned).kind is DbOutcomeKind.COMPLETED
        for strategy in ('initial','current'):
            with history.open_segment_history(action_id,boundary,strategy=strategy,batch_size=1) as handle:
                assert handle.read_result(ref)==expected
                cursor=None
                found=[]
                while True:
                    page=handle.read_page('segment_members',ref=ref,cursor=cursor,limit=1)
                    found.extend(row.values['file_id'] for row in page.items)
                    if page.next_cursor is None:break
                    cursor=page.next_cursor
                assert sorted(found)==sorted(ids.values())
    finally:
        owned.connection.close()


@pytest.mark.parametrize('bounded',[False,True])
@pytest.mark.parametrize('mutation',['member','identity','fixed_count','no_scope'])
async def test_canonical_member_guard_does_not_trust_plan_rows(tmp_path,bounded,mutation):
    from camctl.capture.segment_storage import ResultSetCreate,SegmentSourceKind,PublicationKind,document
    from camctl.persistence.repositories.segment_result_members import ResultMembersCommand
    from camctl.persistence.event_reads import SqlEventReads
    from .test_segment_scan_pairing import _ready
    from .test_segment_file_pairing import _entry
    owned,runtime,saved,_,_=await _ready(tmp_path,[_entry('a.OSV')])
    try:
        _finish_scan(owned,runtime,saved)
        action_id=owned.connection.execute('SELECT action_id FROM operation_runs WHERE id=?',
            (saved.ref.ticket.run_id,)).fetchone()[0]
        repository=SegmentRepository()
        request=ResultSetCreate(action_id,int(saved.ref.ticket.target_id),saved.ref.ticket.run_id,
            SegmentSourceKind.COMPLETE_SCAN,_boundary(owned),saved.ref,PublicationKind.RESULT,None,None,runtime.wall_us())
        receipt=repository.create_result_set(request,new_operation_key(),owned)
        ref=receipt.value
        inputs=repository.read_result_member_inputs(ref,None,1,owned).items
        if mutation=='fixed_count':
            assert repository.append_result_members(ref,inputs,runtime.wall_us(),new_operation_key(),owned).kind is DbOutcomeKind.COMPLETED
        key=new_operation_key()
        before=tuple(owned.connection.execute('SELECT * FROM segment_members'))
        count=owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()
        class Command:
            def plan(self,scope):
                kind='FIX_RESULT_MEMBERS' if mutation=='fixed_count' else 'MEMBERS'
                plan=ResultMembersCommand(kind,ref,None if mutation=='fixed_count' else inputs,runtime.wall_us(),key).plan(scope)
                event=plan.events[0]
                row=event.rows[0]
                values=dict(row.after.values)
                if mutation=='fixed_count':values['member_count']+=1
                elif mutation=='member':
                    values['file_event_id']=inputs[0].page_event_id
                    changed=replace(inputs[0],file_event_id=inputs[0].page_event_id)
                    event=replace(event,evidence={'segment_request':{'ref':document(ref),'input':[document(changed)]}})
                elif mutation=='identity':
                    row=replace(row,row_id=row.row_id+100)
                    plan=replace(plan,owners={('segment_members',row.row_id):('action',action_id)})
                event=replace(event,rows=(replace(row,after=replace(row.after,values=values)),))
                if bounded:
                    return BoundedCommandPlan((event,),lambda event,transaction:EventContext(transaction,plan.owners,plan.state_rows,
                        current_reads=None if mutation=='no_scope' else SqlEventReads(scope.connection,plan.segment_read_scope)),
                        None,lambda connection:None)
                return replace(plan,events=(event,),segment_read_scope=None if mutation=='no_scope' else plan.segment_read_scope)
        outcome=commit_operation(Command(),key,owned)
        assert outcome.kind=='rolled_back',outcome.error
        assert tuple(owned.connection.execute('SELECT * FROM segment_members'))==before
        assert owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()==count
        assert repository.read_result_set(ref,owned)['state']==1
    finally:
        owned.connection.close()


@pytest.mark.parametrize('predecessor',[False,True])
async def test_canceled_version_has_unique_original_request_and_preserves_source(tmp_path,predecessor):
    from camctl.capture.segment_storage import PublicationKind
    from camctl.history.decoding import decode_event_row
    from camctl.persistence.repositories.result_pages import _COLUMNS
    owned,runtime,action_id,_=await consumer_world(tmp_path,'cancel',catalog=SegmentedCatalog())
    try:
        repository=SegmentRepository()
        request=_request(runtime,owned,action_id)
        previous=None
        if predecessor:
            previous=repository.create_result_set(request,new_operation_key(),owned)
            assert previous.kind is DbOutcomeKind.COMPLETED,previous.error
        cancel=None
        for raw in owned.connection.execute(f'SELECT {_COLUMNS} FROM history_events ORDER BY id'):
            event=decode_event_row(raw)
            if any(row.table=='actions' and row.row_id==action_id
                    and row.after.values.get('cancel_requested')==1 for row in event.rows):
                cancel=event.event_id
        assert cancel is not None
        request=replace(request,publication_kind=PublicationKind.CANCELED,cancel_event_id=cancel,
            predecessor_set_id=None if previous is None else previous.value.set_id)
        key=new_operation_key()
        created=repository.create_result_set(request,key,owned)
        assert created.kind is DbOutcomeKind.COMPLETED,created.error
        row=repository.read_result_set(created.value,owned)
        assert row['source_kind']==3 and row['source_event_id']==request.source_boundary.last_event_id
        assert repository.create_result_set(request,key,owned).value==created.value
        assert repository.create_result_set(request,new_operation_key(),owned).kind is DbOutcomeKind.ROLLED_BACK
        fixed=repository.fix_result_members(created.value,runtime.wall_us(),new_operation_key(),owned)
        assert fixed.kind is DbOutcomeKind.COMPLETED,fixed.error
        assert repository.read_result_set(created.value,owned)['member_count']==0
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone()==(0,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('later',['missing','ambiguous','conflict'])
async def test_later_pair_fact_does_not_demote_reliable_retained_preview(tmp_path,later):
    from camctl.capture.files import PairResultFilesSave,OwnershipFromResultPageSave,FileCompletionSave
    from camctl.capture.segment_storage import ResultSetCreate,SegmentSourceKind,PublicationKind
    from camctl.capture.handlers import _begin_check_round
    from camctl.capture.result_pages import ResultPageSave
    from camctl.operations.models import EvidenceValue,Settlement,SettlementBasis
    from camctl.operations.validation import validate_outcome
    from .test_segment_scan_pairing import _ready
    from .test_segment_file_pairing import _entry
    from ..capture.test_record_file_completion_wait import _REGISTRY,_completion
    from ..capture.test_result_page_history import _actual
    owned,runtime,first,ids,context=await _ready(tmp_path,[_entry('raw.LRF'),_entry('raw.OSV')])
    try:
        preview=ids['/DCIM/raw.LRF']
        paired=runtime.capture.save_result_file_pairing(PairResultFilesSave(preview,first.ref,
            _boundary(owned),runtime.wall_us()),new_operation_key(),owned)
        assert paired.kind is DbOutcomeKind.COMPLETED,paired.error
        _finish_scan(owned,runtime,first,retry_wait=True)
        source=owned.connection.execute('SELECT action_id FROM operation_runs WHERE id=?',
            (first.ref.ticket.run_id,)).fetchone()[0]
        ticket=_begin_check_round(runtime,source).ticket
        names=['raw.LRF']+(['raw.OSV','raw.osv'] if later=='ambiguous' else ['raw.osv'] if later=='conflict' else [])
        actual=replace(_actual(ticket,entries=[_entry(name) for name in names],finalized=True),
            settlement=Settlement(SettlementBasis.OBSERVED,EvidenceValue('results_returned',1,
                {'file_completion':_completion(context),'file_completion_source_page_event_id':None})))
        last=runtime.capture.save_result_page(ResultPageSave(ticket,1,None,
            validate_outcome(ticket,actual,_REGISTRY),runtime.wall_us()),new_operation_key(),owned)
        assert last.kind is DbOutcomeKind.COMPLETED,last.error
        saved=runtime.capture.read_result_page(last.value,owned)
        for entry in saved.page.entries:
            fid=dict(saved.file_ids)[entry.identity]
            if fid in ids.values():continue
            proof=runtime.capture.save_ownership_from_result_page(
                OwnershipFromResultPageSave(saved.ref,fid,runtime.wall_us()),new_operation_key(),owned)
            assert proof.kind is DbOutcomeKind.COMPLETED,proof.error
            proof=runtime.capture.save_file_completion(FileCompletionSave(fid,3,runtime.wall_us(),basis=3,
                observation=entry.evidence,size_bytes=10,activity_id=int(context['activity_id']),
                result_page_event_id=saved.ref.event_id,stop_result_event_id=context['stop_result_event_id']),
                new_operation_key(),owned)
            assert proof.kind is DbOutcomeKind.COMPLETED,proof.error
        negative=runtime.capture.save_result_file_pairing(PairResultFilesSave(preview,last.value,
            _boundary(owned),runtime.wall_us()),new_operation_key(),owned)
        assert negative.kind is DbOutcomeKind.COMPLETED,negative.error
        reason=owned.connection.execute('SELECT reason FROM device_file_pairing_facts WHERE id=?',
            (negative.value,)).fetchone()[0]
        from camctl.contracts.enums import enum_for
        assert reason==int(enum_for('device_file_pairing_facts.reason')[later.upper()])
        _finish_scan(owned,runtime,runtime.capture.read_result_page(last.value,owned))
        request=ResultSetCreate(source,int(ticket.target_id),ticket.run_id,
            SegmentSourceKind.RETAINED_SOURCES,_boundary(owned),last.value,
            PublicationKind.RESULT,None,None,runtime.wall_us())
        repository=SegmentRepository()
        ref=repository.create_result_set(request,new_operation_key(),owned)
        assert ref.kind is DbOutcomeKind.COMPLETED,ref.error
        page=repository.read_result_member_inputs(ref.value,None,128,owned)
        retained=next(item for item in page.items if item.file_id==preview)
        assert retained.pairing_state.name=='PAIRED' and retained.pairing_fact_id==paired.value
    finally:
        owned.connection.close()


async def _two_scan_result(tmp_path):
    from camctl.capture.segment_storage import ResultSetCreate,SegmentSourceKind,PublicationKind
    from camctl.capture.handlers import _begin_check_round
    from camctl.capture.result_pages import ResultPageSave
    from camctl.operations.validation import validate_outcome
    from .test_segment_scan_pairing import _ready
    from .test_segment_file_pairing import _entry
    from ..capture.test_record_file_completion_wait import _REGISTRY
    from ..capture.test_result_page_history import _actual
    owned,runtime,first,_,_=await _ready(tmp_path,[_entry('CAM_001/CAM_20261021090000_0001_D.OSV')])
    try:
        _finish_scan(owned,runtime,first,retry_wait=True)
        action_id=owned.connection.execute('SELECT action_id FROM operation_runs WHERE id=?',
            (first.ref.ticket.run_id,)).fetchone()[0]
        ticket=_begin_check_round(runtime,action_id).ticket
        actual=_actual(ticket,entries=[_entry('CAM_001/CAM_20261021090000_0002_D.OSV')],finalized=True)
        saved=runtime.capture.save_result_page(ResultPageSave(ticket,1,None,
            validate_outcome(ticket,actual,_REGISTRY),runtime.wall_us()),new_operation_key(),owned)
        assert saved.kind is DbOutcomeKind.COMPLETED,saved.error
        _finish_scan(owned,runtime,runtime.capture.read_result_page(saved.value,owned))
        repository=SegmentRepository()
        request=ResultSetCreate(action_id,int(ticket.target_id),ticket.run_id,
            SegmentSourceKind.RETAINED_SOURCES,_boundary(owned),saved.value,
            PublicationKind.RESULT,None,None,runtime.wall_us())
        result=repository.create_result_set(request,new_operation_key(),owned)
        assert result.kind is DbOutcomeKind.COMPLETED,result.error
        return owned,runtime,repository,result.value
    except BaseException:
        owned.connection.close()
        raise


@pytest.mark.parametrize('bounded',[False,True])
@pytest.mark.parametrize('kind',['MEMBERS','FIX_MEMBERS','ASSESS'])
@pytest.mark.parametrize('bypass',[False,True])
async def test_complete_scan_writer_cannot_replace_retained_result(tmp_path,monkeypatch,bounded,kind,bypass):
    from camctl.persistence.repositories import segments
    from camctl.persistence.event_reads import SqlEventReads
    from .test_segment_storage import _assessment
    owned,runtime,repository,ref=await _two_scan_result(tmp_path)
    try:
        strict=repository.read_current_segment_ref(ref,owned)
        inputs=repository.read_result_member_inputs(ref,None,128,owned).items
        late=next(item for item in inputs if item.page_event_id==strict.final_page_event_id)
        payload=(late,) if kind=='MEMBERS' else None
        if kind!='MEMBERS':
            items=(late,) if kind=='FIX_MEMBERS' else inputs
            appended=repository.append_result_members(ref,items,runtime.wall_us(),new_operation_key(),owned)
            assert appended.kind is DbOutcomeKind.COMPLETED,appended.error
        if kind=='ASSESS':
            fixed=repository.fix_result_members(ref,runtime.wall_us(),new_operation_key(),owned)
            assert fixed.kind is DbOutcomeKind.COMPLETED,fixed.error
            facts,assessment,gaps=_assessment(strict,(),owned)
            assert not gaps
            payload=(facts,assessment)
        before=owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()
        key=new_operation_key()
        class Command:
            def plan(self,scope):
                with monkeypatch.context() as patch:
                    if bypass:
                        patch.setattr(segments,'_check_write_kind',lambda *args:None,raising=False)
                    plan=segments._Command(kind,strict,payload,runtime.wall_us(),key).plan(scope)
                if bounded:
                    return BoundedCommandPlan(plan.events,
                        lambda event,transaction:EventContext(transaction,plan.owners,plan.state_rows,
                            current_reads=SqlEventReads(scope.connection,plan.segment_read_scope)),
                        None,lambda connection:None)
                return plan
        saved=commit_operation(Command(),key,owned)
        assert saved.kind=='rolled_back' and '完整扫描' in str(saved.error),saved.error
        assert owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()==before
        assert repository.read_result_set(ref,owned)['source_kind']==2
    finally:
        owned.connection.close()


@pytest.mark.parametrize('layout',['same_page','old_page','old_only'])
@pytest.mark.parametrize('new_complete',[False,True])
async def test_current_scan_checks_metadata_only_for_actual_difference(tmp_path,layout,new_complete):
    from camctl.capture.segment_storage import ResultSetCreate,SegmentSourceKind,PublicationKind
    from camctl.capture.handlers import _begin_check_round,capture_handler
    from camctl.capture.result_pages import ResultPageSave
    from camctl.devices.bindings import DeviceBinding
    from camctl.devices.directory import DirectoryCursor
    from camctl.devices.file_identity import FileIdentity
    from camctl.operations.validation import validate_outcome
    from ..capture.test_baseline_history import _append,_fix
    from ..capture.test_result_page_history import _actual,_REGISTRY
    class BaselineCatalog(SegmentedCatalog):
        def parameter_definition(self,*args):
            definition=super().parameter_definition(*args)
            if args[1]!='camera_record':return definition
            factory=definition.task_factory
            return replace(definition,task_factory=lambda params:replace(factory(params),
                ownership_mode=2,output_scope={'directories':['/DCIM']}))
    owned,runtime,action_id,_=await consumer_world(tmp_path,'record',catalog=BaselineCatalog(),start=False)
    try:
        activity=owned.connection.execute('SELECT id FROM device_activities').fetchone()[0]
        binding=DeviceBinding('cam-1',runtime.action(action_id)['driver_id'])
        assert _append(owned,activity,(FileIdentity(binding,'/DCIM/old.OSV'),)).kind is DbOutcomeKind.COMPLETED
        assert _fix(owned,activity,chunks=1,entries=1).kind is DbOutcomeKind.COMPLETED
        await capture_handler('camera_record')(action_id,runtime)
        ticket=_begin_check_round(runtime,action_id).ticket
        old={'identity':'/DCIM/old.OSV','locator':{'path':'/DCIM/old.OSV'},
            'complete':False,'size_bytes':None,'kind':'video','format_id':'osv'}
        new={**old,'identity':'/DCIM/recent.OSV','locator':{'path':'/DCIM/recent.OSV'},
            'complete':new_complete,'size_bytes':10 if new_complete else None}
        cursor=DirectoryCursor(binding,('/DCIM',),0,old['identity'])
        batches=([old,new],) if layout=='same_page' else ([old],[new]) if layout=='old_page' else ([old],)
        previous=None
        for number,entries in enumerate(batches,1):
            following=cursor if number<len(batches) else None
            actual=_actual(ticket,entries=entries,cursor=previous,next_cursor=following,
                finalized=following is None)
            saved=runtime.capture.save_result_page(ResultPageSave(ticket,number,previous,
                validate_outcome(ticket,actual,_REGISTRY),runtime.wall_us()),new_operation_key(),owned)
            assert saved.kind is DbOutcomeKind.COMPLETED,saved.error
            previous=following
        final=runtime.capture.read_result_page(saved.value,owned)
        _finish_scan(owned,runtime,final)
        has_scan=new_complete or layout=='old_only'
        request=ResultSetCreate(action_id,activity,ticket.run_id,
            SegmentSourceKind.COMPLETE_SCAN if layout=='old_only' else SegmentSourceKind.RETAINED_SOURCES,
            _boundary(owned),saved.value if has_scan else None,
            PublicationKind.RESULT,None,None,runtime.wall_us())
        repository=SegmentRepository()
        created=repository.create_result_set(request,new_operation_key(),owned)
        assert created.kind is DbOutcomeKind.COMPLETED,created.error
        current=repository.read_current_segment_ref(created.value,owned)
        assert (current is not None)==has_scan
        inputs=repository.read_result_member_inputs(created.value,None,128,owned).items
        assert len(inputs)==(0 if layout=='old_only' else 1)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('bounded',[False,True])
@pytest.mark.parametrize('kind',['MEMBERS','FIX_MEMBERS'])
async def test_unified_complete_result_keeps_h0_member_writer_and_ordinary_assessment(tmp_path,monkeypatch,bounded,kind):
    from camctl.capture.segment_storage import ResultSetCreate,SegmentSourceKind,PublicationKind
    from camctl.persistence.repositories import segments
    from camctl.persistence.event_reads import SqlEventReads
    from .test_segment_scan_pairing import _ready
    from .test_segment_file_pairing import _entry
    from .test_segment_storage import _assessment
    owned,runtime,saved,_,_=await _ready(tmp_path,[_entry('CAM_001/CAM_20261021090000_0001_D.OSV')])
    try:
        _finish_scan(owned,runtime,saved)
        action_id=owned.connection.execute('SELECT action_id FROM operation_runs WHERE id=?',
            (saved.ref.ticket.run_id,)).fetchone()[0]
        repository=SegmentRepository()
        created=repository.create_result_set(ResultSetCreate(action_id,int(saved.ref.ticket.target_id),
            saved.ref.ticket.run_id,SegmentSourceKind.COMPLETE_SCAN,_boundary(owned),saved.ref,
            PublicationKind.RESULT,None,None,runtime.wall_us()),new_operation_key(),owned)
        assert created.kind is DbOutcomeKind.COMPLETED,created.error
        ref=created.value
        strict=repository.read_current_segment_ref(ref,owned)
        inputs=repository.read_result_member_inputs(ref,None,128,owned).items
        if kind=='FIX_MEMBERS':
            appended=repository.append_result_members(ref,inputs,runtime.wall_us(),new_operation_key(),owned)
            assert appended.kind is DbOutcomeKind.COMPLETED,appended.error
        before=owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()
        key=new_operation_key()
        class Command:
            def plan(self,scope):
                with monkeypatch.context() as patch:
                    patch.setattr(segments,'_check_write_kind',lambda *args:None)
                    plan=segments._Command(kind,strict,inputs if kind=='MEMBERS' else None,
                        runtime.wall_us(),key).plan(scope)
                if bounded:
                    return BoundedCommandPlan(plan.events,
                        lambda event,transaction:EventContext(transaction,plan.owners,plan.state_rows,
                            current_reads=SqlEventReads(scope.connection,plan.segment_read_scope)),
                        None,lambda connection:None)
                return plan
        rejected=commit_operation(Command(),key,owned)
        assert rejected.kind=='rolled_back' and '原H0' in str(rejected.error),rejected.error
        assert owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()==before
        if kind=='MEMBERS':
            appended=repository.append_result_members(ref,inputs,runtime.wall_us(),new_operation_key(),owned)
            assert appended.kind is DbOutcomeKind.COMPLETED,appended.error
        fixed=repository.fix_result_members(ref,runtime.wall_us(),new_operation_key(),owned)
        assert fixed.kind is DbOutcomeKind.COMPLETED,fixed.error
        facts,assessment,gaps=_assessment(strict,(),owned)
        assert not gaps
        from camctl.capture.segment_models import EvidenceState, SegmentAccumulator
        from camctl.capture.segments import (
            advance_segment_assessment,
            finish_segment_assessment,
        )
        from camctl.persistence.repositories.segments import iter_segment_members
        from dataclasses import replace

        start = repository.read_start(ref, owned)
        facts = replace(
            facts,
            start=EvidenceState.UNKNOWN
            if start.key is None
            else EvidenceState.CONFIRMED,
            start_key=start.key,
            start_observation=start.observation,
        )
        state = SegmentAccumulator(strict, 60000, start.key)
        for member in iter_segment_members(strict, owned):
            state = advance_segment_assessment(state, member)
        assessment = finish_segment_assessment(state, facts)
        assessed=repository.save_assessment(strict,facts,assessment,runtime.wall_us(),new_operation_key(),owned)
        assert assessed.kind is DbOutcomeKind.COMPLETED,assessed.error
        assert repository.read_result_set(ref,owned)['assessment_json'] is not None
    finally:
        owned.connection.close()

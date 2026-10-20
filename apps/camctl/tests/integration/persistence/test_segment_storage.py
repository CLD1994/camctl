"""P2 分段事实的真实 SQLite 结构、有限保存与归属。"""
import pytest
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from .test_runtime import _create_valid_database


def test_segment_relations_are_available_in_initialized_schema(tmp_path):
    path = tmp_path / 'state.db'
    _create_valid_database(path)
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        actual = {row[0] for row in owned.connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        assert {'segment_sets', 'segment_members', 'segment_gaps', 'segment_scope_members'} <= actual
        for table in ('segment_sets','segment_members','segment_gaps','segment_scope_members'):
            assert 'created_event_id' in {row[1] for row in owned.connection.execute(f'PRAGMA table_info({table})')}
    finally:
        owned.connection.close()

from dataclasses import replace
from camctl.acceptance.ports import ParameterDefinition
from camctl.capture.files import OwnershipSave, FileCompletionSave
from camctl.capture.models import CaptureTask
from camctl.capture.segment_models import *
from camctl.capture.segment_storage import SegmentSetCreate, SegmentMemberInput
from camctl.capture.segments import advance_segment_assessment, finish_segment_assessment, iter_segment_gaps
from camctl.capture.results import ProductRule, FileKind
from camctl.capture.handlers import _begin_check_round
from camctl.contracts.values import new_operation_key, ConsistencyError
from camctl.devices.segments import SegmentKey
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.segments import SegmentRepository
from ..capture.result_consumer_fixtures import ResultCatalog, consumer_world
from ..capture.test_result_page_history import _actual, _request


class SegmentedCatalog(ResultCatalog):
    def parameter_definition(self,*args):
        definition=super().parameter_definition(*args)
        if args[1] in ('camera_record','camera_timelapse'):
            factory=definition.task_factory
            def task(params):
                original=factory(params)
                return replace(original,result_policy='osmo360ii_record_segments_v1' if args[1]=='camera_record' else 'osmo_empty_tail_classification_v1',product_rules=(ProductRule(FileKind.VIDEO,'osv',min_size_bytes=1).as_json(),))
            return replace(definition,task_factory=task)
        return definition


def _ok(receipt):
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    return receipt.value


async def _world(tmp_path, indexes=(1,2,4), *, sizes=None, own=True, consumer="record", unique_names=False, unknown_keys=False):
    owned,runtime,action_id,_=await consumer_world(tmp_path,consumer,catalog=SegmentedCatalog())
    ticket=_begin_check_round(runtime,action_id).ticket
    entries=[]
    for i,index in enumerate(indexes):
        name=f'/DCIM/CAM001/CAM_20261021{90000+i if unique_names else 90000:06d}_{index:04d}_D.OSV'
        if unknown_keys:name=f'/DCIM/UNKNOWN_{i:04d}.OSV'
        entries.append({'identity':name,'locator':{'path':name},'complete':True,'size_bytes':10 if sizes is None else sizes[i],'kind':'video','format_id':'osv'})
    from camctl.devices.directory import DirectoryCursor
    from camctl.devices.bindings import DeviceBinding
    file_ids={}; source_pages={}; previous=None
    for offset in range(0,len(entries),128):
        chunk=entries[offset:offset+128]
        following=None if offset+128>=len(entries) else DirectoryCursor(DeviceBinding('cam-1','camctl-adb'),('/DCIM',),0,chunk[-1]['identity'])
        saved=_ok(runtime.capture.save_result_page(_request(runtime,ticket,_actual(ticket,entries=chunk,cursor=previous,next_cursor=following,finalized=following is None),page_no=offset//128+1,cursor=previous),new_operation_key(),owned))
        page=runtime.capture.read_result_pages(ticket,None if previous is None else last_event,1,owned).items[0]
        file_ids.update(page.file_ids)
        source_pages.update((entry['identity'],saved.event_id) for entry in chunk)
        previous=following; last_event=saved.event_id
    if own:
        for entry in entries:
            fid=file_ids[entry['identity']]
            _ok(runtime.capture.save_file_ownership(OwnershipSave(fid,action_id,1,2,{},runtime.wall_us()),new_operation_key(),owned))
            _ok(runtime.capture.save_file_completion(FileCompletionSave(fid,3,runtime.wall_us(),basis=1,observation={},size_bytes=entry['size_bytes']),new_operation_key(),owned))
    attempt_id,=owned.connection.execute('SELECT id FROM operation_attempts WHERE run_id=? AND attempt_no=?',(ticket.run_id,ticket.attempt_id)).fetchone()
    ref=SegmentSetRef(action_id,attempt_id,saved.event_id,1)
    members=tuple(SegmentMemberInput(file_ids[entry['identity']],source_pages[entry['identity']],
        owned.connection.execute('SELECT last_event_id FROM device_files WHERE id=?',(file_ids[entry['identity']],)).fetchone()[0],
        SegmentMemberRole.ORIGINAL,OwnershipState.OWNED if own else OwnershipState.UNKNOWN,
        FileCompletionState.COMPLETE if own else FileCompletionState.UNKNOWN) for entry in entries)
    repo=SegmentRepository()
    request=SegmentSetCreate(ref,int(ticket.target_id),ticket.run_id,runtime.action(action_id)['execution_spec_json']['target_duration_ms'],runtime.wall_us())
    _ok(repo.create(request,new_operation_key(),owned))
    return owned,runtime,repo,ref,members


@pytest.mark.asyncio
async def test_frozen_members_roundtrip_and_child_only_history_count(tmp_path):
    owned,runtime,repo,ref,members=await _world(tmp_path)
    try:
        before=owned.connection.execute('SELECT change_count FROM actions WHERE id=?',(ref.action_id,)).fetchone()[0]
        key=new_operation_key()
        _ok(repo.append_members(ref,members,runtime.wall_us(),key,owned))
        assert owned.connection.execute('SELECT change_count FROM actions WHERE id=?',(ref.action_id,)).fetchone()[0]==before+1
        _ok(repo.append_members(ref,members,runtime.wall_us(),key,owned))
        with pytest.raises(ConsistencyError):repo.read_members(ref,None,2,owned)
        _ok(repo.fix_members(ref,runtime.wall_us(),new_operation_key(),owned))
        first=repo.read_members(ref,None,2,owned)
        last=repo.read_members(ref,first.next_cursor,2,owned)
        assert tuple(item.file_id for item in first.items+last.items)==tuple(item.file_id for item in members)
        assert all(item.member.size_bytes==10 for item in first.items+last.items)
        assert len(first.items)==2 and last.next_cursor is None
        assert not hasattr(first.items[0],'size_bytes')
        with pytest.raises(ConsistencyError):repo.read_members(replace(ref,version=2),first.next_cursor,2,owned)
    finally:owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('case',['duplicate','false_role','wrong_page','false_ownership'])
async def test_member_save_rejects_conflicting_actual_facts(tmp_path,case):
    owned,runtime,repo,ref,members=await _world(tmp_path,own=case!='false_ownership')
    try:
        values=members
        if case=='duplicate':values=(members[0],members[0])
        elif case=='false_role':values=(replace(members[0],role=SegmentMemberRole.OTHER),)
        elif case=='wrong_page':values=(replace(members[0],page_event_id=1),)
        else:values=(replace(members[0],ownership=OwnershipState.OWNED),)
        receipt=repo.append_members(ref,values,runtime.wall_us(),new_operation_key(),owned)
        assert receipt.kind is DbOutcomeKind.ROLLED_BACK
        assert owned.connection.execute('SELECT COUNT(*) FROM segment_members').fetchone()==(0,)
    finally:owned.connection.close()


def _assessment(ref,members,owned):
    facts=CompletionFacts(EvidenceState.CONFIRMED,EvidenceState.CONFIRMED,EvidenceState.CONFIRMED,EvidenceState.CONFIRMED,SegmentKey(1,1),EvidenceState.CONFIRMED,EvidenceState.UNKNOWN,True,False)
    state=SegmentAccumulator(ref,60000,facts.start_key)
    from camctl.persistence.repositories.segments import iter_segment_members
    for member in iter_segment_members(ref,owned):state=advance_segment_assessment(state,member)
    result=finish_segment_assessment(state,facts)
    gaps=tuple(iter_segment_gaps(iter_segment_members(ref,owned),last_nonzero_cursor=result.last_nonzero_cursor,last_owned_cursor=result.last_owned_cursor,start_key=result.confirmed_start_key))
    return facts,result,gaps


@pytest.mark.asyncio
async def test_full_second_pass_and_assessment_are_required(tmp_path):
    owned,runtime,repo,ref,members=await _world(tmp_path,indexes=(1,2,3,5),sizes=(10,0,0,10))
    try:
        _ok(repo.append_members(ref,members,runtime.wall_us(),new_operation_key(),owned))
        _ok(repo.fix_members(ref,runtime.wall_us(),new_operation_key(),owned))
        facts,result,gaps=_assessment(ref,members,owned)
        assert len(gaps)==3
        _ok(repo.append_gaps(ref,gaps[:2],runtime.wall_us(),new_operation_key(),owned))
        assert repo.save_assessment(ref,facts,result,runtime.wall_us(),new_operation_key(),owned).kind is DbOutcomeKind.ROLLED_BACK
        _ok(repo.append_gaps(ref,gaps[2:],runtime.wall_us(),new_operation_key(),owned))
        _ok(repo.save_assessment(ref,facts,result,runtime.wall_us(),new_operation_key(),owned))
        assert repo.read_assessment(ref,owned)==result
        page=repo.read_gaps(ref,None,2,owned)
        assert page.items==gaps[:2]
        assert repo.read_gaps(ref,page.next_cursor,2,owned).items==gaps[2:]
        first_id=repo.read_members(ref,None,1,owned).items[0].member_id
        _ok(repo.append_scope(ref,(first_id,),runtime.wall_us(),new_operation_key(),owned))
        assert repo.read_scope(ref,None,1,owned,published=False).items[0]['output_id'] is None
        with pytest.raises(ConsistencyError):repo.read_scope(ref,None,1,owned)
    finally:owned.connection.close()

from camctl.history.events import load_event_registry
from camctl.operations.models import AttemptTicket
from camctl.history.reads import SegmentReadScope
from camctl.persistence.transaction import CommandPlan,commit_operation,row_change,event_envelope,next_row_id


@pytest.mark.asyncio
async def test_direct_event_cannot_forge_original_set_identity(tmp_path):
    owned,runtime,repo,ref,members=await _world(tmp_path)
    try:
        other=replace(ref,version=2)
        original=owned.connection.execute('SELECT id FROM segment_sets').fetchone()[0]
        from camctl.persistence.transaction import row_facts
        row=row_facts(owned.connection,'segment_sets',original)
        values={k:v for k,v in row.items() if k!='id'}
        values.update(version=2,target_duration_ms=1)
        class Forged:
            def plan(self,scope):
                allocation=scope.allocate(1)
                event_type=load_event_registry()['events']['SEGMENT_FACTS']
                change=row_change('segment_sets',next_row_id(scope.connection,'segment_sets'),{**values,'created_event_id':allocation.first_event_id})
                event=event_envelope(allocation.first_event_id,allocation.txn_id,event_type['id'],event_type['branches']['CREATE']['reason'],(change,),runtime.wall_us(),{'segment_request':{}})
                return CommandPlan((event,),{('segment_sets',change.row_id):('action',ref.action_id)},
                    {'actions':{ref.action_id:row_facts(scope.connection,'actions',ref.action_id)},'segment_sets':{}},segment_read_scope=SegmentReadScope(other))
        saved=commit_operation(Forged(),new_operation_key(),owned)
        assert saved.kind=='rolled_back'
        assert owned.connection.execute('SELECT COUNT(*) FROM segment_sets').fetchone()==(1,)
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_scope_members_follow_saved_device_order(tmp_path):
    owned,runtime,repo,ref,members=await _world(tmp_path,indexes=(1,2,3))
    try:
        _ok(repo.append_members(ref,members,runtime.wall_us(),new_operation_key(),owned))
        _ok(repo.fix_members(ref,runtime.wall_us(),new_operation_key(),owned))
        facts,result,gaps=_assessment(ref,members,owned)
        _ok(repo.save_assessment(ref,facts,result,runtime.wall_us(),new_operation_key(),owned))
        ids=tuple(item.member_id for item in repo.read_members(ref,None,128,owned).items)
        saved=repo.append_scope(ref,(ids[1],ids[0]),runtime.wall_us(),new_operation_key(),owned)
        assert saved.kind is DbOutcomeKind.ROLLED_BACK
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_multi_page_members_and_many_zero_gaps_stay_in_finite_batches(tmp_path):
    owned,runtime,repo,ref,members=await _world(tmp_path,indexes=tuple(range(1,132)),sizes=(10,)+(0,)*129+(10,))
    try:
        with pytest.raises(ValueError):repo.append_members(ref,members,runtime.wall_us(),new_operation_key(),owned)
        for offset in range(0,len(members),128):
            _ok(repo.append_members(ref,members[offset:offset+128],runtime.wall_us(),new_operation_key(),owned))
        _ok(repo.fix_members(ref,runtime.wall_us(),new_operation_key(),owned))
        facts,result,gaps=_assessment(ref,members,owned)
        assert len(gaps)==129
        for offset in range(0,len(gaps),128):
            _ok(repo.append_gaps(ref,gaps[offset:offset+128],runtime.wall_us(),new_operation_key(),owned))
        _ok(repo.save_assessment(ref,facts,result,runtime.wall_us(),new_operation_key(),owned))
        cursor=None;count=0
        while True:
            page=repo.read_members(ref,cursor,7,owned)
            assert len(page.items)<=7
            count+=len(page.items)
            if page.next_cursor is None:break
            cursor=page.next_cursor
        assert count==131
        setrow=repo.read_set(ref,owned)
        assert owned.connection.execute('SELECT attempts_used FROM operation_runs WHERE id=?',(setrow['run_id'],)).fetchone()==(1,)
        pages=runtime.capture.read_result_pages(
            AttemptTicket(1,'result',str(setrow['activity_id']),f"results/{setrow['activity_id']}",setrow['run_id']),None,128,owned).items
        assert len(pages)==2 and pages[0].ref.ticket==pages[1].ref.ticket
        assert pages[1].ref.page_no==2 and pages[1].ref.event_id==ref.final_page_event_id
        plan=owned.connection.execute('EXPLAIN QUERY PLAN SELECT id FROM segment_members WHERE set_id=? AND (COALESCE(directory_number,0),COALESCE(segment_index,0),identity,id)>(?,?,?,?) ORDER BY COALESCE(directory_number,0),COALESCE(segment_index,0),identity,id LIMIT 8',(1,1,1,'',0)).fetchall()
        assert any('segment_member_order' in row[-1] for row in plan)
        assert not any('TEMP B-TREE' in row[-1] for row in plan)
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_fixed_file_reference_survives_later_completion_checksum_and_absence(tmp_path):
    from camctl.capture.files import FileChecksumSave,FilePresenceSave
    owned,runtime,repo,ref,members=await _world(tmp_path,own=False)
    try:
        original=members[0]
        for item in members:
            _ok(runtime.capture.save_file_ownership(OwnershipSave(item.file_id,ref.action_id,1,2,{},runtime.wall_us()),new_operation_key(),owned))
            _ok(runtime.capture.save_file_completion(FileCompletionSave(item.file_id,3,runtime.wall_us(),basis=1,observation={},size_bytes=10),new_operation_key(),owned))
        _ok(runtime.capture.save_file_checksum(FileChecksumSave(original.file_id,2,runtime.wall_us(),sha256='a'*64),new_operation_key(),owned))
        _ok(runtime.capture.save_file_presence(FilePresenceSave(original.file_id,3,runtime.wall_us()),new_operation_key(),owned))
        key=new_operation_key()
        _ok(repo.append_members(ref,members,runtime.wall_us(),key,owned))
        _ok(repo.fix_members(ref,runtime.wall_us(),new_operation_key(),owned))
        saved=repo.read_members(ref,None,1,owned).items[0]
        assert saved.file_event_id==original.file_event_id
        assert saved.member.ownership is OwnershipState.UNKNOWN
        assert saved.member.completion is FileCompletionState.UNKNOWN
        assert saved.member.size_bytes is None
        changed=replace(original,file_event_id=owned.connection.execute('SELECT last_event_id FROM device_files WHERE id=?',(original.file_id,)).fetchone()[0])
        assert repo.append_members(ref,(changed,*members[1:]),runtime.wall_us(),key,owned).kind is DbOutcomeKind.ROLLED_BACK
    finally:owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('case',['another_file','future','wrong_attempt','wrong_activity'])
async def test_reference_only_request_rejects_unrelated_history_and_scan(tmp_path,case):
    owned,runtime,repo,ref,members=await _world(tmp_path)
    try:
        if case in ('another_file','future'):
            event=members[1].file_event_id if case=='another_file' else ref.final_page_event_id+100000
            receipt=repo.append_members(ref,(replace(members[0],file_event_id=event),),runtime.wall_us(),new_operation_key(),owned)
        else:
            setrow=repo.read_set(ref,owned)
            changed=replace(ref,version=2,scan_id=1) if case=='wrong_attempt' else replace(ref,version=2)
            receipt=repo.create(SegmentSetCreate(changed,setrow['activity_id']+1 if case=='wrong_activity' else setrow['activity_id'],setrow['run_id'],60000,runtime.wall_us()),new_operation_key(),owned)
        assert receipt.kind is DbOutcomeKind.ROLLED_BACK
    finally:owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('scope_state,indexes,start',[(DefaultScopeState.UNKNOWN,(1,2),None),(DefaultScopeState.EMPTY,(2,3),SegmentKey(1,1)),(DefaultScopeState.NONEMPTY,(1,2,4),SegmentKey(1,1))])
async def test_saved_default_scope_three_states_roundtrip(tmp_path,scope_state,indexes,start):
    from camctl.persistence.repositories.segments import iter_segment_members
    owned,runtime,repo,ref,members=await _world(tmp_path,indexes=indexes)
    try:
        _ok(repo.append_members(ref,members,runtime.wall_us(),new_operation_key(),owned))
        _ok(repo.fix_members(ref,runtime.wall_us(),new_operation_key(),owned))
        facts=CompletionFacts(EvidenceState.CONFIRMED,EvidenceState.CONFIRMED,EvidenceState.CONFIRMED,EvidenceState.UNKNOWN if start is None else EvidenceState.CONFIRMED,start,EvidenceState.CONFIRMED,EvidenceState.UNKNOWN,True,False)
        state=SegmentAccumulator(ref,60000,start)
        for member in iter_segment_members(ref,owned):state=advance_segment_assessment(state,member)
        result=finish_segment_assessment(state,facts)
        gaps=tuple(iter_segment_gaps(iter_segment_members(ref,owned),last_nonzero_cursor=result.last_nonzero_cursor,last_owned_cursor=result.last_owned_cursor,start_key=start))
        if gaps:_ok(repo.append_gaps(ref,gaps,runtime.wall_us(),new_operation_key(),owned))
        _ok(repo.save_assessment(ref,facts,result,runtime.wall_us(),new_operation_key(),owned))
        saved=repo.read_assessment(ref,owned)
        assert saved.default_scope.state is scope_state
        assert saved.confirmed_start_key==start
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_preview_pairing_requires_its_actual_original_name_stem(tmp_path):
    owned,runtime,action_id,_=await consumer_world(tmp_path,'record',catalog=SegmentedCatalog())
    try:
        ticket=_begin_check_round(runtime,action_id).ticket
        original='/DCIM/CAM001/CAM_20261021090000_0002_D.OSV'
        preview='/DCIM/CAM001/CAM_20261021090000_0001_D.LRF'
        entries=[{'identity':name,'locator':{'path':name},'complete':True,'size_bytes':10,'kind':'video','format_id':format,
            **({'paired_identity':original} if name==preview else {})} for name,format in [(preview,'lrf'),(original,'osv')]]
        last=_ok(runtime.capture.save_result_page(_request(runtime,ticket,_actual(ticket,entries=entries,finalized=True)),new_operation_key(),owned))
        ids=dict(runtime.capture.read_result_pages(ticket,None,1,owned).items[0].file_ids)
        _ok(runtime.capture.save_file_ownership(OwnershipSave(ids[original],action_id,1,2,{},runtime.wall_us()),new_operation_key(),owned))
        _ok(runtime.capture.save_file_ownership(OwnershipSave(ids[preview],action_id,1,3,{},runtime.wall_us(),paired_device_file_id=ids[original],pairing_observation={}),new_operation_key(),owned))
        for name in (original,preview):
            _ok(runtime.capture.save_file_completion(FileCompletionSave(ids[name],3,runtime.wall_us(),basis=1,observation={},size_bytes=10),new_operation_key(),owned))
        scan_id=owned.connection.execute('SELECT id FROM operation_attempts WHERE run_id=? AND attempt_no=?',(ticket.run_id,ticket.attempt_id)).fetchone()[0]
        ref=SegmentSetRef(action_id,scan_id,last.event_id,1);repo=SegmentRepository()
        _ok(repo.create(SegmentSetCreate(ref,int(ticket.target_id),ticket.run_id,60000,runtime.wall_us()),new_operation_key(),owned))
        event=owned.connection.execute('SELECT last_event_id FROM device_files WHERE id=?',(ids[preview],)).fetchone()[0]
        request=SegmentMemberInput(ids[preview],last.event_id,event,SegmentMemberRole.PREVIEW,OwnershipState.OWNED,FileCompletionState.COMPLETE,original,PreviewPairingState.PAIRED)
        saved=repo.append_members(ref,(request,),runtime.wall_us(),new_operation_key(),owned)
        assert saved.kind is DbOutcomeKind.ROLLED_BACK
        assert '主干' in str(saved.error)
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_native_timelapse_frozen_facts_reject_ordinary_record_assessment(tmp_path):
    from camctl.persistence.repositories.segments import iter_segment_members
    owned,runtime,repo,ref,members=await _world(tmp_path,indexes=(1,2),consumer='timelapse')
    try:
        _ok(repo.append_members(ref,members,runtime.wall_us(),new_operation_key(),owned))
        _ok(repo.fix_members(ref,runtime.wall_us(),new_operation_key(),owned))
        target=repo.read_set(ref,owned)['target_duration_ms']
        facts=CompletionFacts(EvidenceState.CONFIRMED,EvidenceState.CONFIRMED,EvidenceState.CONFIRMED,EvidenceState.CONFIRMED,SegmentKey(1,1),EvidenceState.CONFIRMED,EvidenceState.UNKNOWN,True,False)
        state=SegmentAccumulator(ref,target,SegmentKey(1,1))
        for member in iter_segment_members(ref,owned):state=advance_segment_assessment(state,member)
        result=finish_segment_assessment(state,facts)
        saved=repo.save_assessment(ref,facts,result,runtime.wall_us(),new_operation_key(),owned)
        assert saved.kind is DbOutcomeKind.ROLLED_BACK
        assert '普通录像' in str(saved.error)
    finally:owned.connection.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('indexes', [(1,1),(1,3,3)])
async def test_real_duplicate_keys_preserve_all_facts_and_unknown_gaps(tmp_path,indexes):
    owned,runtime,repo,ref,members=await _world(tmp_path,indexes=indexes,unique_names=True)
    try:
        _ok(repo.append_members(ref,members,runtime.wall_us(),new_operation_key(),owned))
        _ok(repo.fix_members(ref,runtime.wall_us(),new_operation_key(),owned))
        facts=CompletionFacts(EvidenceState.CONFIRMED,EvidenceState.CONFIRMED,EvidenceState.CONFIRMED,EvidenceState.CONFIRMED,SegmentKey(1,1),EvidenceState.CONFIRMED,EvidenceState.UNKNOWN,True,False)
        from camctl.persistence.repositories.segments import iter_segment_members
        state=SegmentAccumulator(ref,60000,facts.start_key)
        for member in iter_segment_members(ref,owned):state=advance_segment_assessment(state,member)
        result=finish_segment_assessment(state,facts)
        assert result.gap_count is None and result.first_gap is None
        _ok(repo.save_assessment(ref,facts,result,runtime.wall_us(),new_operation_key(),owned))
        assert repo.read_assessment(ref,owned)==result
        assert len(repo.read_members(ref,None,128,owned).items)==len(indexes)
        assert repo.read_gaps(ref,None,128,owned).items==()
    finally:owned.connection.close()

@pytest.mark.asyncio
async def test_unpublished_assessment_is_not_an_active_set(tmp_path):
    owned,runtime,repo,ref,members=await _world(tmp_path,indexes=(1,2))
    try:
        assert repo.read_active_set(ref.action_id,owned) is None
        assert repo.read_set(ref,owned)['published_event_id'] is None
    finally:owned.connection.close()

async def _assessed_world(tmp_path,scope_state):
    from camctl.persistence.repositories.segments import iter_segment_members
    indexes=(2,3) if scope_state is DefaultScopeState.EMPTY else (1,2)
    owned,runtime,repo,ref,members=await _world(tmp_path,indexes=indexes)
    _ok(repo.append_members(ref,members,runtime.wall_us(),new_operation_key(),owned))
    _ok(repo.fix_members(ref,runtime.wall_us(),new_operation_key(),owned))
    start=None if scope_state is DefaultScopeState.UNKNOWN else SegmentKey(1,1)
    facts=CompletionFacts(EvidenceState.CONFIRMED,EvidenceState.CONFIRMED,EvidenceState.CONFIRMED,EvidenceState.UNKNOWN if start is None else EvidenceState.CONFIRMED,start,EvidenceState.CONFIRMED,EvidenceState.UNKNOWN,True,False)
    state=SegmentAccumulator(ref,60000,start)
    for member in iter_segment_members(ref,owned):state=advance_segment_assessment(state,member)
    result=finish_segment_assessment(state,facts)
    gaps=tuple(iter_segment_gaps(iter_segment_members(ref,owned),last_nonzero_cursor=result.last_nonzero_cursor,last_owned_cursor=result.last_owned_cursor,start_key=start))
    if gaps:_ok(repo.append_gaps(ref,gaps,runtime.wall_us(),new_operation_key(),owned))
    _ok(repo.save_assessment(ref,facts,result,runtime.wall_us(),new_operation_key(),owned))
    if scope_state is DefaultScopeState.NONEMPTY:
        ids=tuple(item.member_id for item in repo.read_members(ref,None,128,owned).items)
        _ok(repo.append_scope(ref,ids,runtime.wall_us(),new_operation_key(),owned))
    return owned,runtime,repo,ref,members


def _completion_command(ref,members,at,key,*,mode='valid',drafts=None):
    from camctl.outputs.catalog import OutputDraft,OutputKind,FileReference,OutputCatalogFacts
    from camctl.persistence.repositories.capture import FinishCapture,FinishCaptureCommand,_CompositeScope
    from camctl.persistence.repositories.segments import segment_event,segment_scope_publication_rows,segment_activation_rows
    from camctl.persistence.transaction import CommandPlan
    request=FinishCapture(ref.action_id,() if mode=='omit_outputs' else tuple(drafts) if drafts is not None else tuple(OutputDraft(OutputKind.ORIGINAL,FileReference(device_file_id=item.file_id),True) for item in members),OutputCatalogFacts(ref.action_id,True),at)
    class Composed:
        def plan(self,scope):
            sub=_CompositeScope(scope,scope.max_event_id+1)
            assessment=SegmentRepository().read_assessment(ref,scope)
            actual_request=request
            if assessment.status is not SegmentAssessmentStatus.SUCCEEDED:
                from camctl.capture.media import RecordingFailure
                from camctl.capture.segment_errors import segment_error_header
                if assessment.gap_count:
                    count=sum(draft.kind is OutputKind.ORIGINAL for draft in request.drafts)
                    failure=RecordingFailure('capture_segment_gap',segment_error_header(assessment,count).details('capture_segment_gap'))
                else:
                    saved=SegmentRepository().read_set(ref,scope)
                    failure=RecordingFailure('capture_result_unconfirmed',{'activity_id':str(saved['activity_id']),'reason':'outputs_unknown'})
                actual_request=replace(request,failure=failure)
            plan=FinishCaptureCommand(actual_request,key).plan(sub)
            if mode in ('omit_origin','wrong_origin'):
                changed=[]
                originals=[identity for draft,identity in zip(actual_request.drafts,plan.result.output_ids) if draft.kind is OutputKind.ORIGINAL]
                for event in plan.events:
                    rows=[]
                    for row in event.rows:
                        if row.table=='output_origins':
                            if mode=='omit_origin':continue
                            from camctl.persistence.transaction import row_change
                            wrong=next(identity for identity in originals if identity!=row.after.values['original_output_id'])
                            row=row_change(row.table,row.row_id,{**row.after.values,'original_output_id':wrong})
                        rows.append(row)
                    changed.append(replace(event,rows=tuple(rows)))
                plan=replace(plan,events=tuple(changed))
            rows=SegmentRepository().read_scope(ref,None,128,scope,published=False).items
            next_event=plan.events[-1].event_id+1
            events=list(plan.events);owners=dict(plan.owners);state={table:dict(values) for table,values in plan.state_rows.items()}
            state['segment_sets']={};state['segment_scope_members']={}
            if rows and mode!='omit_scope':
                from camctl.persistence.transaction import row_facts
                outputs={draft.file.device_file_id:identity for draft,identity in zip(actual_request.drafts,plan.result.output_ids)}
                expected=tuple(outputs[row_facts(scope.connection,'segment_members',row['member_id'])['file_id']] for row in rows)
                bindings=tuple((row['id'],expected[i if mode!='wrong_identity' else len(rows)-i-1]) for i,row in enumerate(rows))
                changes=segment_scope_publication_rows(ref,bindings,scope)
                events.append(segment_event(ref,'PUBLISH',changes,next_event,events[0].transaction_id,at,request_input=bindings));next_event+=1
                for row in rows:
                    owners['segment_scope_members',row['id']]=('action',ref.action_id)
                    state['segment_scope_members'][row['id']]=row
            changes=segment_activation_rows(ref,next_event,scope)
            setrow=SegmentRepository().read_set(ref,scope);state['segment_sets'][setrow['id']]=setrow
            owners['segment_sets',setrow['id']]=('action',ref.action_id)
            events.append(segment_event(ref,'ACTIVATE',changes,next_event,events[0].transaction_id,at,request_input=None))
            if mode=='omit_marker':events.pop()
            scope.allocate(len(events))
            return replace(plan,events=tuple(events),owners=owners,state_rows=state)
    return Composed()


@pytest.mark.asyncio
@pytest.mark.parametrize('scope_state',list(DefaultScopeState))
async def test_unique_active_set_is_composed_with_terminal_and_outputs(tmp_path,scope_state):
    from camctl.persistence.transaction import commit_operation
    owned,runtime,repo,ref,members=await _assessed_world(tmp_path,scope_state)
    try:
        key=new_operation_key()
        saved=commit_operation(_completion_command(ref,members,runtime.wall_us(),key),key,owned)
        assert saved.kind=='completed',saved.error
        assert repo.read_active_set(ref.action_id,owned)==ref
        assert repo.read_set(ref,owned)['published_event_id']==owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()[0]
        marker=repo.read_set(ref,owned)['published_event_id']
        assert owned.connection.execute('SELECT change_seq FROM history_events WHERE id=?',(marker,)).fetchone()[0] is not None
        assert owned.connection.execute('SELECT last_event_id FROM history_transactions ORDER BY id DESC LIMIT 1').fetchone()==(marker,)
        page=repo.read_scope(ref,None,128,owned)
        assert len(page.items)==(len(members) if scope_state is DefaultScopeState.NONEMPTY else 0)
    finally:owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['omit_scope','wrong_identity','omit_outputs'])
async def test_activation_refuses_incomplete_or_wrong_identity_scope(tmp_path,mode):
    from camctl.persistence.transaction import commit_operation
    owned,runtime,repo,ref,members=await _assessed_world(tmp_path,DefaultScopeState.NONEMPTY)
    try:
        key=new_operation_key()
        before=owned.connection.execute('SELECT MAX(id),MAX(change_seq) FROM history_events').fetchone()
        saved=commit_operation(_completion_command(ref,members,runtime.wall_us(),key,mode=mode),key,owned)
        assert saved.kind=='rolled_back'
        assert repo.read_active_set(ref.action_id,owned) is None
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone()==(0,)
        assert owned.connection.execute('SELECT MAX(id),MAX(change_seq) FROM history_events').fetchone()==before
    finally:owned.connection.close()

@pytest.mark.asyncio
async def test_unknown_key_observations_page_without_fabricated_segment_cursor(tmp_path):
    owned,runtime,repo,ref,members=await _world(tmp_path,indexes=tuple(range(1,132)),unknown_keys=True)
    try:
        for offset in range(0,len(members),128):
            _ok(repo.append_members(ref,members[offset:offset+128],runtime.wall_us(),new_operation_key(),owned))
        _ok(repo.fix_members(ref,runtime.wall_us(),new_operation_key(),owned))
        cursor=None;identities=[]
        while True:
            page=repo.read_members(ref,cursor,7,owned)
            assert all(item.member.key is None for item in page.items)
            identities.extend(item.member.identity for item in page.items)
            if page.next_cursor is None:break
            cursor=page.next_cursor
        assert len(identities)==len(set(identities))==131
        assert identities==sorted(identities)
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_terminal_action_cannot_be_activated_later_or_again_with_new_key(tmp_path):
    from camctl.persistence.repositories.capture import CaptureRepository,FinishCapture
    from camctl.outputs.catalog import OutputDraft,OutputKind,FileReference,OutputCatalogFacts
    from camctl.persistence.repositories.segments import segment_event,segment_activation_rows
    from camctl.persistence.transaction import commit_operation,CommandPlan,row_facts
    from camctl.history.reads import SegmentReadScope
    owned,runtime,repo,ref,members=await _assessed_world(tmp_path,DefaultScopeState.UNKNOWN)
    try:
        at=runtime.wall_us()
        request=FinishCapture(ref.action_id,tuple(OutputDraft(OutputKind.ORIGINAL,FileReference(device_file_id=item.file_id),True) for item in members),OutputCatalogFacts(ref.action_id,True),at)
        _ok(CaptureRepository().finish_capture(request,new_operation_key(),owned))
        class Late:
            def plan(self,scope):
                allocation=scope.allocate(1)
                rows=segment_activation_rows(ref,allocation.first_event_id,scope)
                setrow=repo.read_set(ref,scope)
                event=segment_event(ref,'ACTIVATE',rows,allocation.first_event_id,allocation.txn_id,at,request_input=None)
                return CommandPlan((event,),{('segment_sets',setrow['id']):('action',ref.action_id)},
                    {'segment_sets':{setrow['id']:setrow},'actions':{ref.action_id:row_facts(scope.connection,'actions',ref.action_id)}},segment_read_scope=SegmentReadScope(ref))
        saved=commit_operation(Late(),new_operation_key(),owned)
        assert saved.kind=='rolled_back' and '同一完成事务' in str(saved.error)
        assert repo.read_active_set(ref.action_id,owned) is None
    finally:owned.connection.close()

@pytest.mark.asyncio
async def test_multiple_versions_never_replace_the_explicit_active_reference(tmp_path):
    from camctl.persistence.repositories.segments import iter_segment_members,segment_event
    from camctl.persistence.transaction import commit_operation,CommandPlan,update_change,row_facts
    from camctl.history.reads import SegmentReadScope
    owned,runtime,repo,ref,members=await _assessed_world(tmp_path,DefaultScopeState.NONEMPTY)
    try:
        second=replace(ref,version=2);setrow=repo.read_set(ref,owned)
        _ok(repo.create(SegmentSetCreate(second,setrow['activity_id'],setrow['run_id'],60000,runtime.wall_us()),new_operation_key(),owned))
        _ok(repo.append_members(second,members,runtime.wall_us(),new_operation_key(),owned))
        _ok(repo.fix_members(second,runtime.wall_us(),new_operation_key(),owned))
        facts=CompletionFacts(EvidenceState.CONFIRMED,EvidenceState.CONFIRMED,EvidenceState.CONFIRMED,EvidenceState.UNKNOWN,None,EvidenceState.CONFIRMED,EvidenceState.UNKNOWN,True,False)
        state=SegmentAccumulator(second,60000,None)
        for member in iter_segment_members(second,owned):state=advance_segment_assessment(state,member)
        _ok(repo.save_assessment(second,facts,finish_segment_assessment(state,facts),runtime.wall_us(),new_operation_key(),owned))
        assert repo.read_active_set(ref.action_id,owned) is None
        key=new_operation_key();at=runtime.wall_us()
        saved=commit_operation(_completion_command(ref,members,at,key),key,owned)
        assert saved.kind=='completed',saved.error
        assert repo.read_active_set(ref.action_id,owned)==ref
        assert repo.read_set(second,owned)['published_event_id'] is None
        class Again:
            def __init__(self,target):self.target=target
            def plan(self,scope):
                allocation=scope.allocate(1);row=repo.read_set(self.target,scope)
                # 绕过辅助构造器，直接伪造同类发布事件，仍由独立守卫拒绝。
                changes=(update_change('segment_sets',row['id'],{'published_event_id':row['published_event_id']},{'published_event_id':allocation.first_event_id}),)
                event=segment_event(self.target,'ACTIVATE',changes,allocation.first_event_id,allocation.txn_id,at,request_input=None)
                return CommandPlan((event,),{('segment_sets',row['id']):('action',ref.action_id)},
                    {'segment_sets':{row['id']:row},'actions':{ref.action_id:row_facts(scope.connection,'actions',ref.action_id)}},segment_read_scope=SegmentReadScope(self.target))
        before=owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()
        for target in (ref,second):
            assert commit_operation(Again(target),new_operation_key(),owned).kind=='rolled_back'
        assert owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()==before
        assert repo.read_active_set(ref.action_id,owned)==ref
    finally:owned.connection.close()

@pytest.mark.asyncio
async def test_unpublished_version_under_failed_source_does_not_advance_public_watermark(tmp_path):
    from camctl.persistence.transaction import commit_operation
    owned,runtime,repo,ref,members=await _assessed_world(tmp_path,DefaultScopeState.EMPTY)
    try:
        key=new_operation_key()
        saved=commit_operation(_completion_command(ref,members,runtime.wall_us(),key),key,owned)
        assert saved.kind=='completed',saved.error
        setrow=repo.read_set(ref,owned)
        watermark=owned.connection.execute('SELECT MAX(change_seq) FROM history_events').fetchone()
        _ok(repo.create(SegmentSetCreate(replace(ref,version=2),setrow['activity_id'],setrow['run_id'],60000,runtime.wall_us()),new_operation_key(),owned))
        assert owned.connection.execute('SELECT change_seq FROM history_events ORDER BY id DESC LIMIT 1').fetchone()==(None,)
        assert owned.connection.execute('SELECT MAX(change_seq) FROM history_events').fetchone()==watermark
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_publish_without_same_transaction_marker_rolls_back(tmp_path):
    from camctl.persistence.transaction import commit_operation
    owned,runtime,repo,ref,members=await _assessed_world(tmp_path,DefaultScopeState.NONEMPTY)
    try:
        before=_publication_snapshot(owned)
        key=new_operation_key()
        receipt=commit_operation(_completion_command(ref,members,runtime.wall_us(),key,mode='omit_marker'),key,owned)
        assert receipt.kind=='rolled_back',receipt.error
        assert _publication_snapshot(owned)==before
    finally:owned.connection.close()


def _publication_snapshot(owned):
    # 比较所有受影响持久状态；仅为有限真实SQLite回归夹具。
    tables=('actions','outputs','output_origins','segment_scope_members','segment_sets','history_transactions','history_events','entity_event_links','report_entity_changes','entity_snapshot_progress')
    return tuple((table,tuple(owned.connection.execute(f'SELECT * FROM {table} ORDER BY id'))) for table in tables)


async def _preview_world(tmp_path, *, original_sizes=(10,0), paired_index=2, preview_size=10, original_frozen='complete', preview_pairing=PreviewPairingState.PAIRED, preview_owned=True, preview_incomplete=False, exclude_paired_original=False, page_size=128):
    from camctl.devices.directory import DirectoryCursor
    from camctl.devices.bindings import DeviceBinding
    from camctl.persistence.repositories.segments import iter_segment_members
    owned,runtime,action_id,_=await consumer_world(tmp_path,'record',catalog=SegmentedCatalog())
    ticket=_begin_check_round(runtime,action_id).ticket
    names=tuple(f'/DCIM/CAM001/CAM_20261021090000_{index:04d}_D.OSV' for index in range(1,len(original_sizes)+1))
    preview=names[paired_index-1].replace('.OSV','.LRF')
    entries=[{'identity':name,'locator':{'path':name},'complete':size is not None,'size_bytes':size,'kind':'video','format_id':'osv'} for name,size in zip(names,original_sizes)]
    entries.append({'identity':preview,'locator':{'path':preview},'complete':preview_size is not None,'size_bytes':preview_size,'kind':'video','format_id':'lrf',**({'paired_identity':names[paired_index-1]} if preview_pairing is PreviewPairingState.PAIRED else {})})
    entries.sort(key=lambda entry:entry['identity'])
    files={};pages={};cursor=None;last=None
    if exclude_paired_original:
        from camctl.capture.files import FileObservationSave
        external=names[paired_index-1]
        observed=_ok(runtime.capture.save_file_observation(FileObservationSave(action_id,external,{'path':external},runtime.wall_us()),new_operation_key(),owned))
        files[external]=observed.file_id
        entries=[entry for entry in entries if entry['identity']!=external]
    for offset in range(0,len(entries),page_size):
        chunk=entries[offset:offset+page_size]
        following=None if offset+page_size>=len(entries) else DirectoryCursor(DeviceBinding('cam-1','camctl-adb'),('/DCIM',),0,chunk[-1]['identity'])
        saved=_ok(runtime.capture.save_result_page(_request(runtime,ticket,_actual(ticket,entries=chunk,cursor=cursor,next_cursor=following,finalized=following is None),page_no=offset//page_size+1,cursor=cursor),new_operation_key(),owned))
        page=runtime.capture.read_result_pages(ticket,last,1,owned).items[0]
        files.update(page.file_ids);pages.update((entry['identity'],saved.event_id) for entry in chunk)
        cursor=following;last=saved.event_id
    anchors={name:owned.connection.execute('SELECT last_event_id FROM device_files WHERE id=?',(identity,)).fetchone()[0] for name,identity in files.items()}
    for name,size in zip(names,original_sizes):
        _ok(runtime.capture.save_file_ownership(OwnershipSave(files[name],action_id,1,2,{},runtime.wall_us()),new_operation_key(),owned))
        if original_frozen!='ownership_unknown' or name!=names[paired_index-1]:
            anchors[name]=owned.connection.execute('SELECT last_event_id FROM device_files WHERE id=?',(files[name],)).fetchone()[0]
        if original_frozen=='incomplete' and name==names[paired_index-1]:
            _ok(runtime.capture.save_file_completion(FileCompletionSave(files[name],4,runtime.wall_us(),error={'reason':'incomplete'}),new_operation_key(),owned))
            anchors[name]=owned.connection.execute('SELECT last_event_id FROM device_files WHERE id=?',(files[name],)).fetchone()[0]
        elif size is not None:
            _ok(runtime.capture.save_file_completion(FileCompletionSave(files[name],3,runtime.wall_us(),basis=1,observation={},size_bytes=size),new_operation_key(),owned))
            if original_frozen=='complete' or name!=names[paired_index-1]:
                anchors[name]=owned.connection.execute('SELECT last_event_id FROM device_files WHERE id=?',(files[name],)).fetchone()[0]
    if preview_owned and preview_pairing is PreviewPairingState.PAIRED:
        _ok(runtime.capture.save_file_ownership(OwnershipSave(files[preview],action_id,1,3,{},runtime.wall_us(),paired_device_file_id=files[names[paired_index-1]],pairing_observation={}),new_operation_key(),owned))
    if preview_incomplete:
        _ok(runtime.capture.save_file_completion(FileCompletionSave(files[preview],4,runtime.wall_us(),error={'reason':'incomplete'}),new_operation_key(),owned))
    elif preview_size is not None:
        _ok(runtime.capture.save_file_completion(FileCompletionSave(files[preview],3,runtime.wall_us(),basis=1,observation={},size_bytes=preview_size),new_operation_key(),owned))
    anchors[preview]=owned.connection.execute('SELECT last_event_id FROM device_files WHERE id=?',(files[preview],)).fetchone()[0]
    attempt_id=owned.connection.execute('SELECT id FROM operation_attempts WHERE run_id=? AND attempt_no=?',(ticket.run_id,ticket.attempt_id)).fetchone()[0]
    ref=SegmentSetRef(action_id,attempt_id,last,1);repo=SegmentRepository()
    _ok(repo.create(SegmentSetCreate(ref,int(ticket.target_id),ticket.run_id,60000,runtime.wall_us()),new_operation_key(),owned))
    from camctl.persistence.repositories.segments import _fixed_file
    requests=[]
    for entry in entries:
        name=entry['identity'];frozen=_fixed_file(owned.connection,files[name],anchors[name])
        requests.append(SegmentMemberInput(files[name],pages[name],anchors[name],SegmentMemberRole.PREVIEW if name==preview else SegmentMemberRole.ORIGINAL,OwnershipState.OWNED if frozen['source_action_id']==action_id else OwnershipState.UNKNOWN,FileCompletionState.COMPLETE if frozen['completion_state']==3 else FileCompletionState.INCOMPLETE if frozen['completion_state']==4 else FileCompletionState.UNKNOWN,names[paired_index-1] if name==preview and preview_pairing is PreviewPairingState.PAIRED else None,preview_pairing if name==preview else PreviewPairingState.NOT_APPLICABLE))
    for offset in range(0,len(requests),128):_ok(repo.append_members(ref,tuple(requests[offset:offset+128]),runtime.wall_us(),new_operation_key(),owned))
    _ok(repo.fix_members(ref,runtime.wall_us(),new_operation_key(),owned))
    facts=CompletionFacts(EvidenceState.CONFIRMED,EvidenceState.CONFIRMED,EvidenceState.CONFIRMED,EvidenceState.CONFIRMED,SegmentKey(1,1),EvidenceState.CONFIRMED,EvidenceState.UNKNOWN,True,False)
    state=SegmentAccumulator(ref,60000,facts.start_key)
    for member in iter_segment_members(ref,owned):state=advance_segment_assessment(state,member)
    assessment=finish_segment_assessment(state,facts)
    if assessment.gap_count is not None:
        gaps=tuple(iter_segment_gaps(iter_segment_members(ref,owned),last_nonzero_cursor=assessment.last_nonzero_cursor,last_owned_cursor=assessment.last_owned_cursor,start_key=facts.start_key))
        for offset in range(0,len(gaps),128):_ok(repo.append_gaps(ref,gaps[offset:offset+128],runtime.wall_us(),new_operation_key(),owned))
    _ok(repo.save_assessment(ref,facts,assessment,runtime.wall_us(),new_operation_key(),owned))
    selected=assessment.default_scope
    if selected.state is DefaultScopeState.NONEMPTY:
        ids=[];cursor=None
        while True:
            page=repo.read_members(ref,cursor,128,owned)
            ids.extend(item.member_id for item in page.items if item.member.role is SegmentMemberRole.ORIGINAL and item.member.cursor is not None and selected.first_cursor<=item.member.cursor<=selected.last_cursor and item.member.size_bytes and item.member.completion is FileCompletionState.COMPLETE)
            if page.next_cursor is None:break
            cursor=page.next_cursor
        for offset in range(0,len(ids),128):_ok(repo.append_scope(ref,tuple(ids[offset:offset+128]),runtime.wall_us(),new_operation_key(),owned))
    return owned,runtime,repo,ref,tuple(requests),files,assessment


@pytest.mark.asyncio
async def test_preview_paired_to_zero_tail_is_optional_and_does_not_block_completion(tmp_path):
    from camctl.persistence.transaction import commit_operation
    owned,runtime,repo,ref,members,files,assessment=await _preview_world(tmp_path)
    try:
        assert assessment.status is SegmentAssessmentStatus.SUCCEEDED
        assert assessment.empty_tail_count==1 and assessment.gap_count==0
        original=next(item for item in members if item.role is SegmentMemberRole.ORIGINAL and item.file_id==files['/DCIM/CAM001/CAM_20261021090000_0001_D.OSV'])
        key=new_operation_key()
        saved=commit_operation(_completion_command(ref,(original,),runtime.wall_us(),key),key,owned)
        assert saved.kind=='completed',saved.error
        assert repo.read_active_set(ref.action_id,owned)==ref
        assert owned.connection.execute('SELECT device_file_id FROM outputs').fetchall()==[(original.file_id,)]
        assert owned.connection.execute('SELECT COUNT(*) FROM segment_members').fetchone()==(3,)
    finally:owned.connection.close()


def _tampered_completion(ref,members,at,key,*,tamper='valid',read_scope='plan'):
    from camctl.persistence.repositories.capture import _CompositeScope
    from camctl.persistence.transaction import update_change
    from camctl.history.reads import PlanReadScope
    from camctl.history.events import load_event_registry
    class Tampered:
        def plan(self,scope):
            inner=_CompositeScope(scope,scope.max_event_id+1)
            plan=_completion_command(ref,members,at,key).plan(inner)
            events=list(plan.events);state={table:dict(values) for table,values in plan.state_rows.items()}
            if tamper in ('omit_marker','fake_final_marker'):
                events.pop()
                if tamper=='fake_final_marker':
                    sid=next(iter(state['segment_sets']))
                    state['segment_sets'][sid]={**state['segment_sets'][sid],'published_event_id':events[-1].event_id}
            elif tamper in ('wrong_action','wrong_version','wrong_scan'):
                event=events[-1];changed=replace(ref,**{'wrong_action':{'action_id':ref.action_id+1},'wrong_version':{'version':ref.version+1},'wrong_scan':{'scan_id':ref.scan_id+1}}[tamper])
                events[-1]=replace(event,evidence={'segment_request':{'ref':{'action_id':changed.action_id,'scan_id':changed.scan_id,'final_page_event_id':changed.final_page_event_id,'version':changed.version},'input':None}})
            elif tamper=='wrong_set':
                event=events[-1];events[-1]=replace(event,rows=(replace(event.rows[0],row_id=event.rows[0].row_id+1),))
            elif tamper=='duplicate_marker':
                events.append(events[-1])
            elif tamper=='wrong_event_type':
                events[-1]=replace(events[-1],event_type=load_event_registry()['events']['ACTION_FINISHED']['id'])
            elif tamper=='omit_terminal':
                events=[event for event in events if event.event_type!=load_event_registry()['events']['ACTION_FINISHED']['id']]
            elif tamper=='unexpected_scope':
                from camctl.persistence.repositories.segments import segment_event
                from camctl.persistence.transaction import next_row_id
                setrow=SegmentRepository().read_set(ref,scope)
                member=SegmentRepository().read_members(ref,None,1,scope).items[0]
                identity=next_row_id(scope.connection,'segment_scope_members')
                state['segment_scope_members'][identity]={'id':identity,'set_id':setrow['id'],'created_event_id':setrow['created_event_id'],'member_id':member.member_id,'ordinal':1,'output_id':None}
                owner=dict(plan.owners);owner['segment_scope_members',identity]=('action',ref.action_id);plan=replace(plan,owners=owner)
                output_id=plan.result.output_ids[0]
                changed=(update_change('segment_scope_members',identity,{'output_id':None},{'output_id':output_id}),)
                extra=segment_event(ref,'PUBLISH',changed,events[-1].event_id,events[-1].transaction_id,at,request_input=((identity,output_id),))
                events.insert(len(events)-1,extra)
            allocation=scope.allocate(len(events))
            fixed=[]
            for index,event in enumerate(events):
                event_id=allocation.first_event_id+index
                if any(row.table=='segment_sets' and 'published_event_id' in row.after.values for row in event.rows):
                    target=event_id-1 if tamper=='marker_points_to_publish' else event_id
                    event=replace(event,rows=tuple(update_change(row.table,row.row_id,dict(row.before.values),{'published_event_id':target}) for row in event.rows))
                fixed.append(replace(event,event_id=event_id,transaction_id=allocation.txn_id))
            if read_scope=='cross_plan':
                plan=replace(plan,plan_read_scope=PlanReadScope(plan.plan_read_scope.plan_id+999,128))
            return replace(plan,events=tuple(fixed),state_rows=state)
    return Tampered()


@pytest.mark.asyncio
@pytest.mark.parametrize('read_scope',['plan','segment'])
@pytest.mark.parametrize('tamper',['omit_marker','fake_final_marker','wrong_action','wrong_version','wrong_scan','wrong_set','wrong_event_type','duplicate_marker','marker_points_to_publish','omit_terminal'])
async def test_marker_proof_rejects_tampered_direct_transaction_and_preserves_all_state(tmp_path,read_scope,tamper,monkeypatch):
    from camctl.persistence.transaction import commit_operation
    owned,runtime,repo,ref,members=await _assessed_world(tmp_path,DefaultScopeState.NONEMPTY)
    try:
        if read_scope=='segment':_segment_guard_scope(monkeypatch,ref)
        before=_publication_snapshot(owned);key=new_operation_key()
        saved=commit_operation(_tampered_completion(ref,members,runtime.wall_us(),key,tamper=tamper,read_scope=read_scope),key,owned)
        assert saved.kind=='rolled_back',saved.error
        assert _publication_snapshot(owned)==before
    finally:owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('read_scope',['plan','segment','cross_plan'])
@pytest.mark.parametrize('scope_state',list(DefaultScopeState))
async def test_marker_proof_validates_same_reference_through_segments_guard_ports(tmp_path,read_scope,scope_state,monkeypatch):
    from camctl.persistence.transaction import commit_operation
    owned,runtime,repo,ref,members=await _assessed_world(tmp_path,scope_state)
    try:
        if read_scope=='segment':_segment_guard_scope(monkeypatch,ref)
        before=_publication_snapshot(owned);key=new_operation_key()
        saved=commit_operation(_tampered_completion(ref,members,runtime.wall_us(),key,read_scope=read_scope),key,owned)
        if read_scope=='cross_plan':
            assert saved.kind=='rolled_back'
            assert _publication_snapshot(owned)==before
        else:
            assert saved.kind=='completed',saved.error
            assert repo.read_active_set(ref.action_id,owned)==ref
    finally:owned.connection.close()



def _segment_guard_scope(monkeypatch,ref):
    """只切换P2具名端口；原录像/计划资格继续使用其生产PlanReadScope。"""
    from camctl.history.validators import NAMED_GUARDS
    from camctl.history.reads import SegmentReadScope
    from camctl.persistence.event_reads import SqlEventReads
    original=NAMED_GUARDS['segments']
    def segment(event,context):
        source=context.current_reads
        reads=SqlEventReads(source.connection,SegmentReadScope(ref),source._validated,source._planned_events)
        original(event,replace(context,current_reads=reads))
    monkeypatch.setitem(NAMED_GUARDS,'segments',segment)


def _candidate_drafts(repo,ref,owned,*,batch=2):
    from camctl.outputs.catalog import OutputDraft,FileReference
    candidates=[];cursor=None
    while True:
        page=repo.read_output_candidates(ref,cursor,batch,owned)
        assert len(page.items)<=batch
        candidates.extend(page.items)
        if page.next_cursor is None:break
        cursor=page.next_cursor
    return tuple(OutputDraft(item.kind,FileReference(device_file_id=item.file_id),True,
        **({'original_batch_file_id':item.original_file_id} if item.original_file_id is not None else {})) for item in candidates)


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['valid','tail_zero','non_tail_zero','original_size_unknown','original_completion_unknown','original_incomplete','original_ownership_unknown','preview_zero','preview_completion_unknown','preview_incomplete','preview_unpaired','preview_pair_unknown'])
async def test_frozen_preview_candidates_and_publication_use_identical_eligibility(tmp_path,case):
    from camctl.persistence.transaction import commit_operation
    from camctl.outputs.catalog import OutputKind
    arguments={'original_sizes':(10,10)}
    if case=='tail_zero':arguments['original_sizes']=(10,0)
    elif case=='non_tail_zero':arguments['original_sizes']=(10,0,10)
    elif case=='original_size_unknown':arguments['original_sizes']=(10,None)
    elif case=='original_completion_unknown':arguments['original_frozen']='completion_unknown'
    elif case=='original_incomplete':arguments['original_frozen']='incomplete'
    elif case=='original_ownership_unknown':arguments['original_frozen']='ownership_unknown'
    elif case=='preview_zero':arguments['preview_size']=0
    elif case=='preview_completion_unknown':arguments['preview_size']=None
    elif case=='preview_incomplete':arguments['preview_incomplete']=True
    elif case=='preview_unpaired':arguments.update(preview_pairing=PreviewPairingState.UNPAIRED,preview_owned=False)
    elif case=='preview_pair_unknown':arguments.update(preview_pairing=PreviewPairingState.UNKNOWN,preview_owned=False)
    owned,runtime,repo,ref,members,files,assessment=await _preview_world(tmp_path,**arguments)
    try:
        drafts=_candidate_drafts(repo,ref,owned,batch=1)
        preview=next(name for name in files if name.endswith('.LRF'))
        assert {draft.file.device_file_id for draft in drafts if draft.kind is OutputKind.PREVIEW}==({files[preview]} if case=='valid' else set())
        key=new_operation_key()
        saved=commit_operation(_completion_command(ref,members,runtime.wall_us(),key,drafts=drafts),key,owned)
        assert saved.kind=='completed',saved.error
        actual=owned.connection.execute('SELECT device_file_id,kind FROM outputs ORDER BY device_file_id').fetchall()
        expected=sorted((draft.file.device_file_id,1 if draft.kind is OutputKind.ORIGINAL else 3) for draft in drafts)
        assert actual==expected
        assert repo.read_active_set(ref.action_id,owned)==ref
        assert owned.connection.execute('SELECT COUNT(*) FROM segment_members').fetchone()==(len(members),)
        assert repo.read_assessment(ref,owned)==assessment
        if case=='valid':
            assert owned.connection.execute('SELECT COUNT(*) FROM output_origins').fetchone()==(1,)
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_preview_candidate_pair_and_original_cross_page_and_batch_boundaries(tmp_path):
    from camctl.persistence.transaction import commit_operation
    owned,runtime,repo,ref,members,files,assessment=await _preview_world(tmp_path,original_sizes=(10,)*129,paired_index=128)
    try:
        drafts=_candidate_drafts(repo,ref,owned,batch=7)
        assert len(drafts)==130
        paired=[item for item in members if item.role is SegmentMemberRole.PREVIEW][0]
        original=[item for item in members if item.file_id==files['/DCIM/CAM001/CAM_20261021090000_0128_D.OSV']][0]
        assert paired.page_event_id!=original.page_event_id
        key=new_operation_key();saved=commit_operation(_completion_command(ref,members,runtime.wall_us(),key,drafts=drafts),key,owned)
        assert saved.kind=='completed',saved.error
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone()==(130,)
        assert repo.read_active_set(ref.action_id,owned)==ref
    finally:owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['omit_preview','duplicate_preview','omit_origin','wrong_origin','extra_zero_preview','extra_frozen_unknown_original'])
async def test_preview_output_and_origin_identity_errors_roll_back_entire_completion(tmp_path,mode):
    from camctl.persistence.transaction import commit_operation
    from camctl.outputs.catalog import OutputDraft,FileReference,OutputKind
    arguments={'original_sizes':(10,10)}
    if mode=='extra_zero_preview':arguments['preview_size']=0
    if mode=='extra_frozen_unknown_original':arguments['original_frozen']='completion_unknown'
    owned,runtime,repo,ref,members,files,assessment=await _preview_world(tmp_path,**arguments)
    try:
        drafts=_candidate_drafts(repo,ref,owned)
        if mode=='omit_preview':drafts=tuple(draft for draft in drafts if draft.kind is not OutputKind.PREVIEW)
        elif mode=='duplicate_preview':drafts+=tuple(draft for draft in drafts if draft.kind is OutputKind.PREVIEW)
        elif mode=='extra_zero_preview':
            name=next(name for name in files if name.endswith('.LRF'))
            drafts+=(OutputDraft(OutputKind.PREVIEW,FileReference(device_file_id=files[name]),True,original_batch_file_id=files[name.replace('.LRF','.OSV')]),)
        elif mode=='extra_frozen_unknown_original':
            drafts+=(OutputDraft(OutputKind.ORIGINAL,FileReference(device_file_id=files['/DCIM/CAM001/CAM_20261021090000_0002_D.OSV']),True),)
        before=_publication_snapshot(owned);key=new_operation_key()
        saved=commit_operation(_completion_command(ref,members,runtime.wall_us(),key,drafts=drafts,mode=mode),key,owned)
        assert saved.kind=='rolled_back',saved.error
        assert _publication_snapshot(owned)==before
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_candidate_cursor_can_advance_past_empty_page_and_point_reads_keep_fixed_facts(tmp_path):
    from camctl.persistence.repositories.segments import segment_output_candidate
    owned,runtime,repo,ref,members,files,assessment=await _preview_world(tmp_path,original_sizes=(10,10),paired_index=1,preview_size=0,original_frozen='completion_unknown')
    try:
        page=repo.read_output_candidates(ref,None,1,owned)
        assert page.items==() and page.next_cursor is not None
        second=repo.read_output_candidates(ref,page.next_cursor,1,owned)
        assert second.items==() and second.next_cursor is not None
        last=repo.read_output_candidates(ref,second.next_cursor,1,owned)
        assert len(last.items)==1 and last.next_cursor is None
        first=repo.read_members(ref,None,128,owned)
        target=next(item for item in first.items if item.file_id==files['/DCIM/CAM001/CAM_20261021090000_0001_D.OSV'])
        assert owned.connection.execute('SELECT size_bytes FROM device_files WHERE id=?',(target.file_id,)).fetchone()==(10,)
        assert target.member.size_bytes is None
        assert segment_output_candidate(ref,target.member_id,owned) is None
        with pytest.raises(ConsistencyError):segment_output_candidate(replace(ref,version=99),target.member_id,owned)
        with pytest.raises(ValueError):repo.read_output_candidates(ref,None,129,owned)
    finally:owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('preview_size',[10,0])
async def test_pair_outside_frozen_set_is_rejected_even_when_preview_would_be_skipped(tmp_path,preview_size):
    from camctl.persistence.transaction import commit_operation
    from camctl.persistence.repositories.segments import segment_output_candidate
    owned,runtime,repo,ref,members,files,assessment=await _preview_world(tmp_path,exclude_paired_original=True,preview_size=preview_size)
    try:
        raw=repo.read_members(ref,None,128,owned)
        preview=next(item for item in raw.items if item.member.role is SegmentMemberRole.PREVIEW)
        with pytest.raises(ConsistencyError):segment_output_candidate(ref,preview.member_id,owned)
        with pytest.raises(ConsistencyError):repo.read_output_candidates(ref,None,128,owned)
        original=next(item for item in members if item.role is SegmentMemberRole.ORIGINAL)
        before=_publication_snapshot(owned);key=new_operation_key()
        saved=commit_operation(_completion_command(ref,(original,),runtime.wall_us(),key),key,owned)
        assert saved.kind=='rolled_back'
        assert _publication_snapshot(owned)==before
    finally:owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('scope_state',[DefaultScopeState.UNKNOWN,DefaultScopeState.EMPTY])
async def test_no_publish_members_can_be_invented_for_unknown_or_empty_scope(tmp_path,scope_state):
    from camctl.persistence.transaction import commit_operation
    owned,runtime,repo,ref,members=await _assessed_world(tmp_path,scope_state)
    try:
        before=_publication_snapshot(owned);key=new_operation_key()
        saved=commit_operation(_tampered_completion(ref,members,runtime.wall_us(),key,tamper='unexpected_scope'),key,owned)
        assert saved.kind=='rolled_back'
        assert '未知或空范围' in str(saved.error)
        assert _publication_snapshot(owned)==before
    finally:owned.connection.close()

"""统一来源与成员经过真实满批事件；分页不遗漏跨批来源。"""
import pytest

from camctl.capture.segment_storage import PublicationKind,ResultSetCreate,SegmentSourceKind
from camctl.capture.handlers import _begin_check_round
from camctl.contracts.values import new_operation_key
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryCursor
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.segments import SegmentRepository

from .test_segment_file_proofs import _boundary
from .test_segment_result_sets import _finish_scan
from .test_segment_result_versions import _commit
from .test_segment_storage import SegmentedCatalog
from ..capture.result_consumer_fixtures import consumer_world
from ..capture.test_result_page_history import _actual,_request

pytestmark=pytest.mark.asyncio


@pytest.mark.parametrize('bounded',[False,True])
async def test_full_member_batch_and_following_page_fix_all_129_actual_sources(tmp_path,bounded):
    from camctl.persistence.repositories.segment_result_members import ResultMembersCommand
    owned,runtime,action_id,_=await consumer_world(tmp_path,'record',catalog=SegmentedCatalog())
    try:
        ticket=_begin_check_round(runtime,action_id).ticket
        binding=DeviceBinding('cam-1','camctl-adb')
        entries=[{'identity':f'/DCIM/CAM_001/CAM_20261021090000_{number:04d}_D.OSV',
            'locator':{'path':f'/DCIM/CAM_001/CAM_20261021090000_{number:04d}_D.OSV'},
            'complete':True,'size_bytes':10,'kind':'video','format_id':'osv'}
            for number in range(1,130)]
        previous=None
        for number,offset in enumerate(range(0,129,128),1):
            chunk=entries[offset:offset+128]
            following=DirectoryCursor(binding,('/DCIM',),0,chunk[-1]['identity']) if offset==0 else None
            saved=runtime.capture.save_result_page(_request(runtime,ticket,_actual(ticket,entries=chunk,
                cursor=previous,next_cursor=following,finalized=following is None),page_no=number,cursor=previous),
                new_operation_key(),owned)
            assert saved.kind is DbOutcomeKind.COMPLETED,saved.error
            previous=following
        final=runtime.capture.read_result_page(saved.value,owned)
        _finish_scan(owned,runtime,final)
        repository=SegmentRepository()
        created=repository.create_result_set(ResultSetCreate(action_id,int(ticket.target_id),ticket.run_id,
            SegmentSourceKind.RETAINED_SOURCES,_boundary(owned),saved.value,PublicationKind.RESULT,
            None,None,runtime.wall_us()),new_operation_key(),owned)
        assert created.kind is DbOutcomeKind.COMPLETED,created.error
        ref=created.value
        first=repository.read_result_member_inputs(ref,None,128,owned)
        assert len(first.items)==128 and first.next_cursor is not None
        last=repository.read_result_member_inputs(ref,first.next_cursor,128,owned)
        assert len(last.items)==1 and last.next_cursor is None
        for page in (first,last):
            key=new_operation_key()
            _commit(ResultMembersCommand('MEMBERS',ref,page.items,runtime.wall_us(),key),key,owned,bounded)
        key=new_operation_key()
        _commit(ResultMembersCommand('FIX_RESULT_MEMBERS',ref,None,runtime.wall_us(),key),key,owned,bounded)
        assert repository.read_result_set(ref,owned)['member_count']==129
        assert owned.connection.execute('SELECT COUNT(DISTINCT file_id) FROM segment_members WHERE set_id=?',
            (ref.set_id,)).fetchone()==(129,)
        rows=owned.connection.execute("SELECT json_array_length(body_json,'$.rows') FROM history_events"
            " WHERE json_extract(body_json,'$.evidence.segment_request.ref.set_id')=?"
            " ORDER BY id",(ref.set_id,)).fetchall()
        assert [row[0] for row in rows]==[1,128,1,1]
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone()==(0,)
    finally:
        owned.connection.close()

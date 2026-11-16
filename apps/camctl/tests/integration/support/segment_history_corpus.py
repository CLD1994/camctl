"""按正式入口生成分段历史固定样本，供独立读取使用。"""
from camctl.contracts.values import new_operation_key
from camctl.persistence.repositories.history import HistoryRepository
from ..persistence.test_segment_storage import _ok
from ..capture.test_capture_contract import _NOW

_RESULT_TIME = _NOW + 60_000_000

async def _large_fixture(tmp_path,count,start_number=0):
    """正式装配只负责准备；读取与计量前完整关闭其线程和文件责任。"""
    from ..support.capture import capture_test_session
    from ..support.result_cases import CaseClock
    from ..capture.test_capture_contract import _NOW, _PAGE_EVIDENCE, ResultsDouble
    from ..capture.result_consumer_fixtures import returned
    from camctl.devices.ports import DeviceCallResult, StopDriver
    from unittest.mock import create_autospec

    clock = CaseClock(_NOW, 5_000_000_000)
    async with capture_test_session() as session:
        async def runtime_factory(owned, *, driver):
            activity_id, = owned.connection.execute(
                "SELECT id FROM device_activities").fetchone()
            stop = create_autospec(StopDriver, instance=True)
            stop.stop.return_value = DeviceCallResult.from_outcome(
                returned("stop", "stop_confirmed", activity_id))
            return await session.runtime(
                owned, driver=driver, evidence=_PAGE_EVIDENCE, results=ResultsDouble({}),
                stopper=stop, wall_us=clock.wall_us, monotonic_ns=clock.monotonic_ns)
        return await _prepare_large_fixture(tmp_path, count, runtime_factory, clock, start_number)


async def _prepare_large_fixture(tmp_path,count,runtime_factory,clock,start_number=0):
    """从真实P2端口逐页建跨CAM目录的夹具；准备阶段也只持有128项。"""
    import sqlite3
    from itertools import islice
    from camctl.capture.files import OwnershipSave,FileCompletionSave
    from decimal import Decimal
    from camctl.operations.attempts import AttemptConfig
    from camctl.capture.result_pages import ResultPageSave
    from camctl.operations.validation import validate_outcome
    from camctl.persistence.repositories.capture import CaptureRepository
    from ..support.result_cases import begin_result_attempt
    from camctl.capture.segment_models import (SegmentSetRef,SegmentMemberRole,OwnershipState,
        FileCompletionState,CompletionFacts,EvidenceState,SegmentAccumulator)
    from camctl.capture.segment_storage import SegmentSetCreate,SegmentMemberInput
    from camctl.capture.segments import advance_segment_assessment,finish_segment_assessment,iter_segment_gaps
    from camctl.devices.bindings import DeviceBinding
    from camctl.devices.directory import DirectoryCursor
    from camctl.devices.segments import SegmentKey
    from camctl.persistence.repositories.segments import SegmentRepository,iter_segment_members
    from ..persistence.test_segment_storage import SegmentedCatalog
    from ..capture.result_consumer_fixtures import consumer_world
    from ..capture.test_result_page_history import _actual,_REGISTRY
    owned,runtime,action_id,_=await consumer_world(
        tmp_path,'record',catalog=SegmentedCatalog(),runtime_factory=runtime_factory,clock=clock)
    stage=None
    try:
        stage=sqlite3.connect(tmp_path/'fixture-members.db',isolation_level=None)
        stage.execute('CREATE TABLE members (id INTEGER PRIMARY KEY,file_id INTEGER,page_event INTEGER,file_event INTEGER)')
        check_config = AttemptConfig(3, Decimal("10"), Decimal("3"))
        ticket=begin_result_attempt(owned,action_id,check_config,_RESULT_TIME)
        capture=CaptureRepository()
        previous=None;last_event=None
        for offset in range(0,count,128):
            entries=[]
            for number in range(offset,min(offset+128,count)):
                directory,index=divmod(start_number+number,999)
                name=f'/DCIM/CAM_{directory+1:03d}/CAM_20261021090000_{index+1:04d}_D.OSV'
                entries.append({'identity':name,'locator':{'path':name},'complete':True,
                    'size_bytes':10 if number in (0,count-1) else 0,'kind':'video','format_id':'osv'})
            following=(None if offset+128>=count else DirectoryCursor(
                DeviceBinding('cam-1','camctl-adb'),('/DCIM',),0,entries[-1]['identity']))
            outcome=_actual(ticket,entries=entries,cursor=previous,next_cursor=following,finalized=following is None)
            request=ResultPageSave(ticket,offset//128+1,previous,
                validate_outcome(ticket,outcome,_REGISTRY),_RESULT_TIME)
            saved=_ok(capture.save_result_page(request,new_operation_key(),owned))
            page=capture.read_result_pages(ticket,last_event,1,owned).items[0]
            files=dict(page.file_ids)
            with stage:
                stage.execute('BEGIN IMMEDIATE')
                for entry in entries:
                    file_id=files[entry['identity']]
                    _ok(capture.save_file_ownership(OwnershipSave(file_id,action_id,1,2,{},_RESULT_TIME),new_operation_key(),owned))
                    _ok(capture.save_file_completion(FileCompletionSave(file_id,3,_RESULT_TIME,basis=1,
                        observation={},size_bytes=entry['size_bytes']),new_operation_key(),owned))
                    event,=owned.connection.execute('SELECT last_event_id FROM device_files WHERE id=?',(file_id,)).fetchone()
                    stage.execute('INSERT INTO members(file_id,page_event,file_event) VALUES (?,?,?)',(file_id,saved.event_id,event))
            previous=following;last_event=saved.event_id
        attempt,=owned.connection.execute('SELECT id FROM operation_attempts WHERE run_id=? AND attempt_no=?',
            (ticket.run_id,ticket.attempt_id)).fetchone()
        ref=SegmentSetRef(action_id,attempt,last_event,1)
        repo=SegmentRepository()
        from camctl.contracts.json_values import parse_exact_json
        execution_spec,=owned.connection.execute(
            'SELECT execution_spec_json FROM actions WHERE id=?',(action_id,)).fetchone()
        target_duration_ms=parse_exact_json(execution_spec)['target_duration_ms']
        _ok(repo.create(SegmentSetCreate(ref,int(ticket.target_id),ticket.run_id,
            target_duration_ms,_RESULT_TIME),new_operation_key(),owned))
        previous_id=0
        while True:
            selected=stage.execute('SELECT id,file_id,page_event,file_event FROM members WHERE id>? ORDER BY id LIMIT 128',(previous_id,)).fetchall()
            if not selected:break
            inputs=tuple(SegmentMemberInput(file_id,page_event,file_event,SegmentMemberRole.ORIGINAL,
                OwnershipState.OWNED,FileCompletionState.COMPLETE) for _,file_id,page_event,file_event in selected)
            _ok(repo.append_members(ref,inputs,_RESULT_TIME,new_operation_key(),owned))
            previous_id=selected[-1][0]
        _ok(repo.fix_members(ref,_RESULT_TIME,new_operation_key(),owned))
        facts=CompletionFacts(EvidenceState.CONFIRMED,EvidenceState.CONFIRMED,EvidenceState.CONFIRMED,
            EvidenceState.CONFIRMED,SegmentKey(start_number//999+1,start_number%999+1),EvidenceState.CONFIRMED,EvidenceState.UNKNOWN,True,False)
        state=SegmentAccumulator(ref,60000,facts.start_key)
        for member in iter_segment_members(ref,owned):state=advance_segment_assessment(state,member)
        result=finish_segment_assessment(state,facts)
        gaps=iter_segment_gaps(iter_segment_members(ref,owned),last_nonzero_cursor=result.last_nonzero_cursor,
            last_owned_cursor=result.last_owned_cursor,start_key=result.confirmed_start_key)
        while chunk:=tuple(islice(gaps,128)):
            _ok(repo.append_gaps(ref,chunk,_RESULT_TIME,new_operation_key(),owned))
        _ok(repo.save_assessment(ref,facts,result,_RESULT_TIME,new_operation_key(),owned))
        first=repo.read_members(ref,None,1,owned).items[0].member_id
        _ok(repo.append_scope(ref,(first,),_RESULT_TIME,new_operation_key(),owned))
        return owned,runtime,repo,ref,HistoryRepository(owned.connection.execute('PRAGMA database_list').fetchone()[2])
    except BaseException:
        owned.connection.close()
        raise
    finally:
        if stage is not None:stage.close()



async def prepare_source(directory, count, start_number=0):
    """经真实入口准备来源，关闭资源并返回读取所需身份和边界。"""
    directory.mkdir(parents=True, exist_ok=False)
    owned, runtime, repo, ref, history = await _large_fixture(directory, count, start_number)
    try:
        boundary=history.current_boundary()
        metadata={'count':count,'action_id':ref.action_id,'scan_id':ref.scan_id,
            'final_page_event_id':ref.final_page_event_id,'version':ref.version,
            'boundary':{'txn_id':boundary.txn_id,'last_event_id':boundary.last_event_id}}
        if start_number:
            metadata['start_number']=start_number
        owned.connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()
        return metadata
    finally:
        owned.connection.close()


async def generate(directory, count, start_number=0):
    from contextlib import closing
    import gzip
    import hashlib
    import json
    import sqlite3
    import time
    from camctl.capture.segment_models import SegmentSetRef
    from camctl.contracts.history_values import HistoryBoundary
    started=time.monotonic()
    metadata=await prepare_source(directory,count,start_number)
    path=directory/'state.db'
    ref=SegmentSetRef(*(metadata[name] for name in ('action_id','scan_id','final_page_event_id','version')))
    boundary=HistoryBoundary(**metadata['boundary'])
    with HistoryRepository(path).open_segment_history(ref.action_id,boundary,batch_size=7) as handle:
        for table, expected in (('segment_members',count),('segment_gaps',count-2)):
            cursor=None
            total=0
            while True:
                page=handle.read_page(table,ref=ref,cursor=cursor,limit=7)
                total+=len(page.items)
                cursor=page.next_cursor
                if cursor is None:break
            assert total==expected
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        assert connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
        assert connection.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert connection.execute('SELECT COUNT(*) FROM segment_members').fetchone()[0] == count
        assert connection.execute('SELECT COUNT(*) FROM segment_gaps').fetchone()[0] == count-2
    data=path.read_bytes()
    archive=directory/f'segment-history-{count}.sqlite.gz'
    with archive.open('wb') as raw:
        with gzip.GzipFile(fileobj=raw,mode='wb',mtime=0) as output:
            output.write(data)
    metadata.update(database_bytes=len(data),database_sha256=hashlib.sha256(data).hexdigest(),
        gzip_sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
    (directory/f'segment-history-{count}.json').write_text(json.dumps(metadata,indent=2)+'\n')
    print('CORPUS',count,'elapsed',time.monotonic()-started,flush=True)


def main():
    import argparse
    import asyncio
    from pathlib import Path
    parser=argparse.ArgumentParser()
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--count',type=int,choices=(256,2048),required=True)
    args=parser.parse_args()
    asyncio.run(generate(args.output_dir,args.count))


def prepare_copy(directory, count):
    """解压到独立路径，只读核验固定输入，所有连接在业务进程启动前关闭。"""
    from contextlib import closing
    from pathlib import Path
    import gzip
    import hashlib
    import json
    import shutil
    import sqlite3
    assets=Path(__file__).resolve().parents[1]/'history'/'fixtures'
    manifest=json.loads((assets/f'segment-history-{count}.json').read_text())
    archive=assets/f'segment-history-{count}.sqlite.gz'
    assert hashlib.sha256(archive.read_bytes()).hexdigest()==manifest['gzip_sha256']
    directory.mkdir()
    path=directory/'state.db'
    with gzip.open(archive,'rb') as source, path.open('xb') as dest:
        shutil.copyfileobj(source,dest)
    assert path.stat().st_size==manifest['database_bytes']
    assert hashlib.sha256(path.read_bytes()).hexdigest()==manifest['database_sha256']
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()==('ok',)
        assert db.execute('PRAGMA journal_mode').fetchone()==('wal',)
        for table, expected in (('segment_members',count),('segment_gaps',count-2)):
            assert db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]==expected
    (directory/'input.json').write_text(json.dumps(manifest))
    return manifest


def verify_output(directory, manifest):
    """在组外比对全部分页、完整快照和固定库成员身份。"""
    from contextlib import closing
    import json
    import sqlite3
    from camctl.history.snapshots import decode_snapshot, SnapshotRef
    from camctl.contracts.json_values import parse_exact_json
    result=json.loads((directory/'result.json').read_text())
    size=manifest['count']
    assert result['counts']=={'segment_members':size,'segment_gaps':size-2}
    content=(directory/'snapshot.jsonl').read_bytes()
    assert len(content)==result['snapshot_length']
    header,rows=decode_snapshot(content,expect_ref=SnapshotRef(1,manifest['action_id']))
    assert header['boundary_event_id']==manifest['boundary']['last_event_id']
    actual=[parse_exact_json(line) for line in (directory/'rows.jsonl').read_text().splitlines()]
    selected={table:[item['row'] for item in actual if item['table']==table]
        for table in ('segment_members','segment_gaps')}
    snapshot={table:{row.row_id:dict(row.values) for row in rows if row.table==table}
        for table in selected}
    expected_tables={'actions','device_activities','operation_attempts','operation_runs',
        'recording_processing','result_page_files','segment_gaps','segment_members',
        'segment_scope_members','segment_sets'}
    assert {row.table for row in rows}==expected_tables
    with closing(sqlite3.connect((directory/'state.db').resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        for table in expected_tables:
            saved=[row for row in rows if row.table==table]
            source={row['id']:{key:(parse_exact_json(value) if key.endswith('_json') and value is not None else value)
                for key,value in dict(row).items()} for row in db.execute(f'SELECT * FROM {table}')}
            assert len(saved)==len(source)
            assert {row.row_id:dict(row.values) for row in saved}==source
        for table, values in selected.items():
            assert len(values)==len(snapshot[table])==result['counts'][table]
            assert {row['id']:row for row in values}==snapshot[table]
            assert {row['id'] for row in values}=={row[0] for row in db.execute(f'SELECT id FROM {table}')}
        indexes=[row['segment_index'] for row in selected['segment_members']]
        start_number=manifest.get('start_number',0)
        assert indexes==[number % 999 + 1 for number in range(start_number,start_number+size)]
        assert [row['directory_number'] for row in selected['segment_members']]==[
            number // 999 + 1 for number in range(start_number,start_number+size)]


if __name__ == '__main__':
    main()

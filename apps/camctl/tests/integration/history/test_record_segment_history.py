"""分段动作使用磁盘句柄恢复，三条路径保持同一 H。"""
from dataclasses import replace

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.persistence.repositories.history import HistoryRepository
from ..persistence.test_segment_storage import _world, _ok, _assessment


async def _fixture(tmp_path, count=5, *, many_gaps=False):
    owned, runtime, repo, ref, members = await _world(
        tmp_path, indexes=tuple(range(1, count + 1)),
        sizes=tuple((10 if i in (0,count-1) else 0) if many_gaps else (0 if i == 2 else 10) for i in range(count)))
    for offset in range(0, count, 128):
        _ok(repo.append_members(ref, members[offset:offset+128], runtime.wall_us(), new_operation_key(), owned))
    _ok(repo.fix_members(ref, runtime.wall_us(), new_operation_key(), owned))
    facts, result, gaps = _assessment(ref, members, owned)
    for offset in range(0, len(gaps), 128):
        _ok(repo.append_gaps(ref, gaps[offset:offset+128], runtime.wall_us(), new_operation_key(), owned))
    _ok(repo.save_assessment(ref, facts, result, runtime.wall_us(), new_operation_key(), owned))
    first = repo.read_members(ref, None, 1, owned).items[0].member_id
    _ok(repo.append_scope(ref, (first,), runtime.wall_us(), new_operation_key(), owned))
    history = HistoryRepository(owned.connection.execute('PRAGMA database_list').fetchone()[2])
    return owned, runtime, repo, ref, history


def _rows(handle, table, ref=None, limit=2):
    cursor = None
    while True:
        page = handle.read_page(table, ref=ref, cursor=cursor, limit=limit)
        yield from page.items
        cursor = page.next_cursor
        if cursor is None:
            return


@pytest.mark.asyncio
@pytest.mark.parametrize('strategy', ['initial', 'current'])
async def test_complete_segment_rows_without_legacy_restore(tmp_path, monkeypatch, strategy):
    owned, runtime, repo, ref, history = await _fixture(tmp_path)
    try:
        boundary = history.current_boundary()
        monkeypatch.setattr(history, 'restore_entity', lambda *a, **kw: pytest.fail('整对象恢复'))
        with history.open_segment_history(ref.action_id, boundary, strategy=strategy, batch_size=2) as handle:
            assert [r.row_id for r in _rows(handle, 'segment_members', ref)] == [1, 2, 3, 4, 5]
            assert len(tuple(_rows(handle, 'segment_sets'))) == 1
            assert len(tuple(_rows(handle, 'segment_gaps', ref))) == 1
            assert len(tuple(_rows(handle, 'segment_scope_members', ref))) == 1
            assert not tuple(_rows(handle, 'device_files'))
            assert handle.primary()['id'] == ref.action_id
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_cursor_is_bound_to_handle_boundary_ref_and_table(tmp_path):
    owned, runtime, repo, ref, history = await _fixture(tmp_path)
    try:
        boundary = history.current_boundary()
        with history.open_segment_history(ref.action_id, boundary, batch_size=2) as handle:
            page = handle.read_page('segment_members', ref=ref, limit=1)
            assert page.next_cursor is not None
            with pytest.raises(ConsistencyError):
                handle.read_page('segment_gaps', ref=ref, cursor=page.next_cursor, limit=1)
            with pytest.raises(ConsistencyError):
                handle.read_page('segment_members', ref=replace(ref, version=2), cursor=page.next_cursor, limit=1)
            with history.open_segment_history(ref.action_id, boundary, batch_size=2) as other:
                with pytest.raises(ConsistencyError):
                    other.read_page('segment_members', ref=ref, cursor=page.next_cursor, limit=1)
    finally:
        owned.connection.close()


async def _save(history, prepared):
    from camctl.persistence.repositories.history import SqliteSnapshotStore
    from camctl.persistence.executor import DbExecutor
    from camctl.persistence.runtime import open_existing, DbOpenMode, DbConfig
    executor = DbExecutor(lambda: open_existing(history.path, DbOpenMode.EXISTING_RW, DbConfig()), capacity=4, enqueue_timeout_seconds=9)
    try:
        return await SqliteSnapshotStore(history.path,executor).save_snapshots((prepared,))
    finally:
        await executor.close()


def _version(owned, runtime, repo, ref, version):
    from camctl.capture.segment_storage import SegmentSetCreate
    old = owned.connection.execute('SELECT activity_id,run_id,target_duration_ms FROM segment_sets WHERE version=1').fetchone()
    ref = replace(ref,version=version)
    _ok(repo.create(SegmentSetCreate(ref,*old,runtime.wall_us()),new_operation_key(),owned))
    return ref


@pytest.mark.asyncio
async def test_stream_snapshot_matches_all_paths_and_preserves_later_pending(tmp_path):
    from camctl.history.snapshots import prepare_snapshot
    owned, runtime, repo, ref, history = await _fixture(tmp_path)
    try:
        start = history.current_boundary()
        with history.open_segment_history(ref.action_id,start,batch_size=2) as source:
            prepared = prepare_snapshot(source)
        try:
            _version(owned,runtime,repo,ref,2)
            boundary = history.current_boundary()
            saved = await _save(history,prepared)
            assert saved[0].remaining_changes == 1
            assert await _save(history,prepared) == saved
            _version(owned,runtime,repo,ref,3)
            for strategy in ('initial','current','snapshot'):
                with history.open_segment_history(ref.action_id,boundary,strategy=strategy,batch_size=2) as handle:
                    assert [r.values['version'] for r in _rows(handle,'segment_sets')] == [1,2]
                    assert len(tuple(_rows(handle,'segment_members',ref))) == 5
        finally:
            prepared.close()
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_current_restores_old_h_when_new_version_arrives_between_candidate_pages(tmp_path,monkeypatch):
    from contextlib import contextmanager
    owned,runtime,repo,ref,history = await _fixture(tmp_path)
    try:
        boundary = history.current_boundary()
        original = history._read_connection
        calls = 0
        @contextmanager
        def changing():
            nonlocal calls
            with original() as connection:
                yield connection
            calls += 1
            if calls == 4:
                _version(owned,runtime,repo,ref,2)
        monkeypatch.setattr(history,'_read_connection',changing)
        with history.open_segment_history(ref.action_id,boundary,strategy='current',batch_size=1) as handle:
            assert [row.values['version'] for row in _rows(handle,'segment_sets')] == [1]
            assert len(tuple(_rows(handle,'segment_members',ref))) == 5
        assert calls > 4
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_current_near_h_never_decodes_old_history_and_main_reader_is_finite(tmp_path,monkeypatch):
    owned,runtime,repo,ref,history = await _fixture(tmp_path,20)
    try:
        for version in range(2,15):
            _version(owned,runtime,repo,ref,version)
        boundary = history.current_boundary()
        from camctl.persistence.repositories import history as module
        original = module.decode_event_row
        decoded = []
        def track(raw):
            decoded.append(raw[0])
            return original(raw)
        monkeypatch.setattr(module,'decode_event_row',track)
        monkeypatch.setattr(history,'_seed_entity_rows',lambda *a: pytest.fail('全集主行种子'))
        with history.open_segment_history(ref.action_id,boundary,batch_size=3) as handle:
            assert handle.stats['events'] == 0
            assert len(tuple(_rows(handle,'segment_members',ref))) == 20
        assert history.restore_primary_at_h('action',ref.action_id,boundary)['id'] == ref.action_id
        assert decoded == []
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_candidate_pages_reuse_verified_current_transaction_range(tmp_path,monkeypatch):
    owned,runtime,repo,ref,history = await _fixture(tmp_path,20)
    try:
        boundary = history.current_boundary()
        observed=[]
        original=history._connect
        def connect():
            connection=original()
            connection.set_trace_callback(lambda sql: observed.append(sql) if 'SELECT COUNT(*), MIN(id), MAX(id) FROM history_events' in sql else None)
            return connection
        monkeypatch.setattr(history,'_connect',connect)
        with history.open_segment_history(ref.action_id,boundary,batch_size=1):
            pass
        assert len(observed) <= 2, observed
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_snapshot_maintenance_uses_stream_source(tmp_path,monkeypatch):
    from camctl.history.snapshots import SnapshotRef,prepare_snapshot
    from camctl.persistence.repositories.history import SqliteSnapshotStore
    from camctl.persistence.executor import DbExecutor
    from camctl.persistence.runtime import open_existing,DbOpenMode,DbConfig
    owned,runtime,repo,ref,history = await _fixture(tmp_path)
    executor=DbExecutor(lambda:open_existing(history.path,DbOpenMode.EXISTING_RW,DbConfig()),capacity=4,enqueue_timeout_seconds=9)
    try:
        monkeypatch.setattr(HistoryRepository,'restore_within',lambda *a,**kw:pytest.fail('整动作快照'))
        images=await SqliteSnapshotStore(history.path,executor).load_entity_images((SnapshotRef(1,ref.action_id),))
        try:
            prepared=prepare_snapshot(images[0])
            try:
                assert not isinstance(prepared.chunks,tuple)
                assert sum(map(len,prepared.chunks)) == prepared.length
                await SqliteSnapshotStore(history.path,executor).save_snapshots((prepared,))
            finally:prepared.close()
        finally:images[0].close()
    finally:
        await executor.close()
        owned.connection.close()


@pytest.mark.asyncio
async def test_snapshot_same_s_compares_all_bytes_and_broken_content_fails(tmp_path):
    from camctl.history.snapshots import prepare_snapshot,SnapshotMaintenanceError
    owned,runtime,repo,ref,history = await _fixture(tmp_path,220)
    try:
        boundary=history.current_boundary()
        with history.open_segment_history(ref.action_id,boundary,batch_size=7) as handle:
            prepared=prepare_snapshot(handle)
        try:
            assert prepared.length > 65536
            await _save(history,prepared)
            with prepared.path.open('r+b') as source:
                source.seek(65540)
                before=source.read(1)
                source.seek(65540)
                source.write(b'x' if before != b'x' else b'y')
            with pytest.raises(SnapshotMaintenanceError):
                await _save(history,prepared)
            # 存储中的损坏也不能降级到另一恢复路径。
            snapshot_id=owned.connection.execute('SELECT id FROM entity_snapshots').fetchone()[0]
            with owned.connection.blobopen('entity_snapshots','content',snapshot_id) as blob:
                blob.seek(65540);blob.write(b'\xff')
            _version(owned,runtime,repo,ref,2)
            with pytest.raises(ConsistencyError):
                history.open_segment_history(ref.action_id,boundary,strategy='snapshot',batch_size=7)
        finally:prepared.close()
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_empty_filtered_page_continues_and_query_failure_is_not_eof(tmp_path):
    import sqlite3
    owned,runtime,repo,ref,history = await _fixture(tmp_path)
    try:
        other=_version(owned,runtime,repo,ref,2)
        with history.open_segment_history(ref.action_id,history.current_boundary(),batch_size=2) as handle:
            page=handle.read_page('segment_sets',ref=other,limit=1)
            assert page.items == () and page.next_cursor is not None
            assert handle.read_page('segment_sets',ref=other,cursor=page.next_cursor,limit=1).items[0].values['version'] == 2
            handle._db.execute('DROP TABLE rows').close()
            with pytest.raises(sqlite3.Error):
                handle.read_page('segment_members',ref=ref,limit=2)
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_growth_has_bounded_python_peak_and_linear_projection_reads(tmp_path):
    import gc,resource,tracemalloc
    evidence=[]
    for size in (16,64,256):
        directory=tmp_path/str(size);directory.mkdir()
        owned,runtime,repo,ref,history=await _fixture(directory,size,many_gaps=True)
        try:
            boundary=history.current_boundary()
            gc.collect();tracemalloc.start()
            with history.open_segment_history(ref.action_id,boundary,batch_size=7) as handle:
                assert sum(1 for _ in _rows(handle,'segment_members',ref,limit=7)) == size
                assert sum(1 for _ in _rows(handle,'segment_gaps',ref,limit=7)) == size-2
                stats=dict(handle.stats)
                prepared=__import__('camctl.history.snapshots',fromlist=['prepare_snapshot']).prepare_snapshot(handle)
                try:
                    assert sum(map(len,prepared.chunks)) == prepared.length
                finally:prepared.close()
                peak=tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
            evidence.append((size,peak,resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,stats))
            assert stats['events'] == 0
            # 成员、缺口及原页文件索引各随真实文件数量线性增长。
            assert stats['projection_rows'] < 3*size+30
            assert peak < 4_000_000
        finally:owned.connection.close()
    print('GROWTH',evidence)
    assert evidence[-1][1] < evidence[0][1]+1_500_000


@pytest.mark.asyncio
async def test_owner_child_id_index_avoids_repeated_prefix_reads(tmp_path):
    owned,runtime,repo,ref,history = await _fixture(tmp_path,96)
    try:
        db=owned.connection
        set_id=db.execute('SELECT id FROM segment_sets').fetchone()[0]
        statement='SELECT id FROM segment_members WHERE set_id=? AND id>? ORDER BY id LIMIT 7'
        def read():
            steps=0
            def count():
                nonlocal steps
                steps+=1
                return 0
            db.set_progress_handler(count,1)
            previous,total=0,0
            try:
                while True:
                    rows=db.execute(statement,(set_id,previous)).fetchall()
                    total+=len(rows)
                    if not rows:break
                    previous=rows[-1][0]
            finally:db.set_progress_handler(None,0)
            assert total == 96
            return steps
        green_plan=db.execute('EXPLAIN QUERY PLAN '+statement,(set_id,0)).fetchall()
        green=read()
        db.execute('DROP INDEX segment_member_history_ids')
        red_plan=db.execute('EXPLAIN QUERY PLAN '+statement+' ',(set_id,0)).fetchall()
        red=read()
        print('INDEX_READS',{'before':red,'after':green,'before_plan':red_plan,'after_plan':green_plan})
        assert green < red/3
        assert any('segment_member_history_ids' in row[3] and 'id>?' in row[3] for row in green_plan)
    finally:owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage',['range','previous','instance'])
async def test_verified_range_cache_rejects_metadata_or_instance_changes(tmp_path,damage):
    owned,runtime,repo,ref,history = await _fixture(tmp_path)
    try:
        boundary=history.current_boundary()
        if damage == 'range':
            owned.connection.execute('UPDATE history_transactions SET first_event_id=2 WHERE id=?',(boundary.txn_id,))
        elif damage == 'previous':
            owned.connection.execute('UPDATE history_transactions SET last_event_id=last_event_id+100000 WHERE id=?',(boundary.txn_id-1,))
        else:
            owned.connection.execute('UPDATE database_metadata SET instance_id=?',(str(new_operation_key()),))
        with pytest.raises(ConsistencyError):history.current_boundary()
        assert history._current_range is None
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_old_full_action_entry_refuses_segment_growth(tmp_path):
    owned,runtime,repo,ref,history = await _fixture(tmp_path)
    try:
        boundary=history.current_boundary()
        with pytest.raises(ConsistencyError):history.restore_entity('action',ref.action_id,boundary)
        with history._read_connection() as connection:
            with pytest.raises(ConsistencyError):history.restore_within(connection,'action',ref.action_id,boundary)
        assert history.restore_primary_at_h('action',ref.action_id,boundary)['id'] == ref.action_id
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_member_pages_use_device_order_when_rows_were_saved_in_reverse_order(tmp_path):
    owned,runtime,repo,ref,members=await _world(tmp_path,indexes=(1,2,3,4))
    try:
        _ok(repo.append_members(ref,tuple(reversed(members)),runtime.wall_us(),new_operation_key(),owned))
        _ok(repo.fix_members(ref,runtime.wall_us(),new_operation_key(),owned))
        history=HistoryRepository(owned.connection.execute('PRAGMA database_list').fetchone()[2])
        with history.open_segment_history(ref.action_id,history.current_boundary(),batch_size=2) as handle:
            assert [row.values['segment_index'] for row in _rows(handle,'segment_members',ref,limit=1)] == [1,2,3,4]
    finally:owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('scope',['UNKNOWN','EMPTY','NONEMPTY'])
async def test_published_marker_scope_and_independent_files_remain_at_same_h(tmp_path,scope):
    from ..persistence.test_segment_storage import _assessed_world,_completion_command
    from camctl.capture.segment_models import DefaultScopeState
    from camctl.capture.files import FilePresenceSave,FileChecksumSave
    from camctl.persistence.transaction import commit_operation
    from camctl.history.snapshots import prepare_snapshot
    owned,runtime,repo,ref,members=await _assessed_world(tmp_path,DefaultScopeState[scope])
    try:
        key=new_operation_key()
        receipt=commit_operation(_completion_command(ref,members,runtime.wall_us(),key),key,owned)
        assert receipt.kind == 'completed', receipt.error
        history=HistoryRepository(owned.connection.execute('PRAGMA database_list').fetchone()[2])
        boundary=history.current_boundary()
        with history.open_segment_history(ref.action_id,boundary,batch_size=1) as handle:
            assert handle.active_ref() == ref
            assert handle.read_assessment(ref).default_scope.state is DefaultScopeState[scope]
            prepared=prepare_snapshot(handle)
        try:
            await _save(history,prepared)
            fid=members[0].file_id
            _ok(runtime.capture.save_file_checksum(FileChecksumSave(fid,2,runtime.wall_us(),'a'*64),new_operation_key(),owned))
            _ok(runtime.capture.save_file_presence(FilePresenceSave(fid,3,runtime.wall_us()),new_operation_key(),owned))
            for strategy in ('initial','snapshot','current'):
                with history.open_segment_history(ref.action_id,boundary,strategy=strategy,batch_size=1) as handle:
                    assert handle.active_ref() == ref
                    saved=handle.read_set(ref)
                    assert saved['published_event_id'] == boundary.last_event_id
                    assert handle.read_assessment(ref).default_scope.state is DefaultScopeState[scope]
                    rows=tuple(_rows(handle,'segment_scope_members',ref,limit=1))
                    assert len(rows) == (len(members) if scope=='NONEMPTY' else 0)
                    assert all(row.values['output_id'] is not None for row in rows)
                    assert not tuple(_rows(handle,'outputs'))
            file=history.restore_entity('device_file',fid,boundary)[('device_files',fid)]
            assert file['sha256'] is None and file['presence_state'] != 3
        finally:prepared.close()
    finally:owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('removed_table',['segment_members','segment_sets','device_activities'])
async def test_snapshot_missing_self_rows_is_not_a_valid_complete_snapshot(tmp_path,removed_table):
    from camctl.history.snapshots import prepare_snapshot
    from camctl.contracts.json_values import parse_exact_json
    from camctl.persistence.transaction import encode_json_value
    owned,runtime,repo,ref,history=await _fixture(tmp_path)
    try:
        boundary=history.current_boundary()
        with history.open_segment_history(ref.action_id,boundary,batch_size=2) as handle:
            prepared=prepare_snapshot(handle)
        try:
            await _save(history,prepared)
            records=[parse_exact_json(line) for line in prepared.path.read_text().splitlines()]
            removed=False
            kept=[]
            for record in records:
                if not removed and record.get('table') == removed_table:
                    removed=True
                else:kept.append(record)
            assert removed
            body=('\n'.join(encode_json_value(row) for row in kept)+'\n').encode()
            owned.connection.execute('UPDATE entity_snapshots SET content=?',(body,))
            with pytest.raises(ConsistencyError):
                history.open_segment_history(ref.action_id,boundary,strategy='snapshot',batch_size=2)
        finally:prepared.close()
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_disk_failure_closes_partial_handle_and_does_not_change_business(tmp_path,monkeypatch):
    import sqlite3
    from pathlib import Path
    from camctl.history.bounded import SegmentHistoryHandle
    owned,runtime,repo,ref,history=await _fixture(tmp_path)
    try:
        boundary=history.current_boundary()
        opened=[]
        original=SegmentHistoryHandle._put
        def fail(self,*args):
            opened.append((self,Path(self._temporary.name)))
            raise sqlite3.OperationalError('database or disk is full')
        monkeypatch.setattr(SegmentHistoryHandle,'_put',fail)
        with pytest.raises(sqlite3.OperationalError):
            history.open_segment_history(ref.action_id,boundary,strategy='initial',batch_size=1)
        assert opened and all(handle._db is None and not path.exists() for handle,path in opened)
        assert history.current_boundary() == boundary
        assert owned.connection.execute('SELECT COUNT(*) FROM segment_members').fetchone() == (5,)
    finally:owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage',['member_count','gap_summary'])
async def test_snapshot_cannot_change_write_once_summary(tmp_path,damage):
    from camctl.history.snapshots import prepare_snapshot
    from camctl.contracts.json_values import parse_exact_json
    from camctl.persistence.transaction import encode_json_value
    owned,runtime,repo,ref,history=await _fixture(tmp_path)
    try:
        boundary=history.current_boundary()
        with history.open_segment_history(ref.action_id,boundary) as handle:prepared=prepare_snapshot(handle)
        try:
            await _save(history,prepared)
            records=[parse_exact_json(line) for line in prepared.path.read_text().splitlines()]
            row=next(record['row'] for record in records if record.get('table')=='segment_sets')
            if damage=='member_count':row['member_count']+=1
            else:row['assessment_json'].update(gap_count=0,first_gap=None)
            owned.connection.execute('UPDATE entity_snapshots SET content=?',(('\n'.join(map(encode_json_value,records))+'\n').encode(),))
            with pytest.raises(ConsistencyError):history.open_segment_history(ref.action_id,boundary,strategy='snapshot')
        finally:prepared.close()
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_nullable_gap_summary_stays_unknown_in_all_paths(tmp_path):
    from camctl.history.snapshots import prepare_snapshot
    owned,runtime,repo,ref,members=await _world(tmp_path,unknown_keys=True)
    try:
        _ok(repo.append_members(ref,members,runtime.wall_us(),new_operation_key(),owned))
        _ok(repo.fix_members(ref,runtime.wall_us(),new_operation_key(),owned))
        from camctl.capture.segment_models import CompletionFacts,EvidenceState,SegmentAccumulator
        from camctl.capture.segments import advance_segment_assessment,finish_segment_assessment
        from camctl.persistence.repositories.segments import iter_segment_members
        from camctl.devices.segments import SegmentKey
        facts=CompletionFacts(EvidenceState.CONFIRMED,EvidenceState.CONFIRMED,EvidenceState.CONFIRMED,
            EvidenceState.CONFIRMED,SegmentKey(1,1),EvidenceState.CONFIRMED,EvidenceState.UNKNOWN,True,False)
        state=SegmentAccumulator(ref,60000,facts.start_key)
        for member in iter_segment_members(ref,owned):state=advance_segment_assessment(state,member)
        result=finish_segment_assessment(state,facts)
        assert result.gap_count is None
        _ok(repo.save_assessment(ref,facts,result,runtime.wall_us(),new_operation_key(),owned))
        history=HistoryRepository(owned.connection.execute('PRAGMA database_list').fetchone()[2])
        boundary=history.current_boundary()
        with history.open_segment_history(ref.action_id,boundary) as handle:prepared=prepare_snapshot(handle)
        try:
            await _save(history,prepared)
            for strategy in ('initial','snapshot','current'):
                with history.open_segment_history(ref.action_id,boundary,strategy=strategy,batch_size=1) as handle:
                    assert handle.read_assessment(ref).gap_count is None
                    assert not tuple(_rows(handle,'segment_gaps',ref))
        finally:prepared.close()
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_connection_failure_cannot_leave_a_new_range_marked_verified(tmp_path,monkeypatch):
    import sqlite3
    owned,runtime,repo,ref,history=await _fixture(tmp_path)
    try:
        history.current_boundary()
        assert history._current_range is not None
        def fail():raise sqlite3.OperationalError('cannot open database')
        monkeypatch.setattr(history,'_connect',fail)
        with pytest.raises(sqlite3.OperationalError):history.current_boundary()
        assert history._current_range is None
    finally:owned.connection.close()


async def _large_fixture(tmp_path,count):
    """从真实P2端口逐页建跨CAM目录的夹具；准备阶段也只持有128项。"""
    import sqlite3
    from itertools import islice
    from camctl.capture.files import OwnershipSave,FileCompletionSave
    from camctl.capture.handlers import _begin_check_round
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
    from ..capture.test_result_page_history import _actual,_request
    owned,runtime,action_id,_=await consumer_world(tmp_path,'record',catalog=SegmentedCatalog())
    stage=sqlite3.connect(tmp_path/'fixture-members.db',isolation_level=None)
    stage.execute('CREATE TABLE members (id INTEGER PRIMARY KEY,file_id INTEGER,page_event INTEGER,file_event INTEGER)')
    try:
        ticket=_begin_check_round(runtime,action_id).ticket
        previous=None;last_event=None
        for offset in range(0,count,128):
            entries=[]
            for number in range(offset,min(offset+128,count)):
                directory,index=divmod(number,999)
                name=f'/DCIM/CAM_{directory+1:03d}/CAM_20261021090000_{index+1:04d}_D.OSV'
                entries.append({'identity':name,'locator':{'path':name},'complete':True,
                    'size_bytes':10 if number in (0,count-1) else 0,'kind':'video','format_id':'osv'})
            following=(None if offset+128>=count else DirectoryCursor(
                DeviceBinding('cam-1','camctl-adb'),('/DCIM',),0,entries[-1]['identity']))
            saved=_ok(runtime.capture.save_result_page(_request(runtime,ticket,
                _actual(ticket,entries=entries,cursor=previous,next_cursor=following,finalized=following is None),
                page_no=offset//128+1,cursor=previous),new_operation_key(),owned))
            page=runtime.capture.read_result_pages(ticket,last_event,1,owned).items[0]
            files=dict(page.file_ids)
            with stage:
                stage.execute('BEGIN IMMEDIATE')
                for entry in entries:
                    file_id=files[entry['identity']]
                    _ok(runtime.capture.save_file_ownership(OwnershipSave(file_id,action_id,1,2,{},runtime.wall_us()),new_operation_key(),owned))
                    _ok(runtime.capture.save_file_completion(FileCompletionSave(file_id,3,runtime.wall_us(),basis=1,
                        observation={},size_bytes=entry['size_bytes']),new_operation_key(),owned))
                    event,=owned.connection.execute('SELECT last_event_id FROM device_files WHERE id=?',(file_id,)).fetchone()
                    stage.execute('INSERT INTO members(file_id,page_event,file_event) VALUES (?,?,?)',(file_id,saved.event_id,event))
            previous=following;last_event=saved.event_id
        attempt,=owned.connection.execute('SELECT id FROM operation_attempts WHERE run_id=? AND attempt_no=?',
            (ticket.run_id,ticket.attempt_id)).fetchone()
        ref=SegmentSetRef(action_id,attempt,last_event,1)
        repo=SegmentRepository()
        _ok(repo.create(SegmentSetCreate(ref,int(ticket.target_id),ticket.run_id,
            runtime.action(action_id)['execution_spec_json']['target_duration_ms'],runtime.wall_us()),new_operation_key(),owned))
        previous_id=0
        while True:
            selected=stage.execute('SELECT id,file_id,page_event,file_event FROM members WHERE id>? ORDER BY id LIMIT 128',(previous_id,)).fetchall()
            if not selected:break
            inputs=tuple(SegmentMemberInput(file_id,page_event,file_event,SegmentMemberRole.ORIGINAL,
                OwnershipState.OWNED,FileCompletionState.COMPLETE) for _,file_id,page_event,file_event in selected)
            _ok(repo.append_members(ref,inputs,runtime.wall_us(),new_operation_key(),owned))
            previous_id=selected[-1][0]
        _ok(repo.fix_members(ref,runtime.wall_us(),new_operation_key(),owned))
        facts=CompletionFacts(EvidenceState.CONFIRMED,EvidenceState.CONFIRMED,EvidenceState.CONFIRMED,
            EvidenceState.CONFIRMED,SegmentKey(1,1),EvidenceState.CONFIRMED,EvidenceState.UNKNOWN,True,False)
        state=SegmentAccumulator(ref,60000,facts.start_key)
        for member in iter_segment_members(ref,owned):state=advance_segment_assessment(state,member)
        result=finish_segment_assessment(state,facts)
        gaps=iter_segment_gaps(iter_segment_members(ref,owned),last_nonzero_cursor=result.last_nonzero_cursor,
            last_owned_cursor=result.last_owned_cursor,start_key=result.confirmed_start_key)
        while chunk:=tuple(islice(gaps,128)):
            _ok(repo.append_gaps(ref,chunk,runtime.wall_us(),new_operation_key(),owned))
        _ok(repo.save_assessment(ref,facts,result,runtime.wall_us(),new_operation_key(),owned))
        first=repo.read_members(ref,None,1,owned).items[0].member_id
        _ok(repo.append_scope(ref,(first,),runtime.wall_us(),new_operation_key(),owned))
        return owned,runtime,repo,ref,HistoryRepository(owned.connection.execute('PRAGMA database_list').fetchone()[2])
    except BaseException:
        owned.connection.close()
        raise
    finally:stage.close()


class _MeasuredCursor:
    def __init__(self,inner,metrics):self.inner,self.metrics=inner,metrics
    def _record(self,rows):
        self.metrics['fetch_calls']+=1
        self.metrics['fetch_rows']+=rows
        self.metrics['largest_fetch']=max(self.metrics['largest_fetch'],rows)
    def fetchall(self):
        rows=self.inner.fetchall();self._record(len(rows));return rows
    def fetchone(self):
        row=self.inner.fetchone();self._record(0 if row is None else 1);return row
    def __getattr__(self,name):return getattr(self.inner,name)


class _MeasuredConnection:
    def __init__(self,inner,metrics):self.inner,self.metrics=inner,metrics
    def execute(self,*args):return _MeasuredCursor(self.inner.execute(*args),self.metrics)
    def __getattr__(self,name):return getattr(self.inner,name)


@pytest.mark.asyncio
async def test_large_growth_crosses_page_and_disk_cache_capacity(tmp_path,monkeypatch):
    import gc,resource,tracemalloc
    from camctl.history.snapshots import prepare_snapshot
    evidence=[]
    for size in (256,2048):
        directory=tmp_path/str(size);directory.mkdir()
        owned,runtime,repo,ref,history=await _large_fixture(directory,size)
        try:
            boundary=history.current_boundary()
            metrics={'fetch_calls':0,'fetch_rows':0,'largest_fetch':0,'vm_steps':0}
            original=history._connect
            def count():metrics['vm_steps']+=1;return 0
            def connect():
                connection=original();connection.set_progress_handler(count,1)
                return _MeasuredConnection(connection,metrics)
            monkeypatch.setattr(history,'_connect',connect)
            gc.collect();tracemalloc.start()
            with history.open_segment_history(ref.action_id,boundary,batch_size=7) as handle:
                assert sum(1 for _ in _rows(handle,'segment_members',ref,limit=7)) == size
                assert sum(1 for _ in _rows(handle,'segment_gaps',ref,limit=7)) == size-2
                page_count=handle._db.execute('PRAGMA page_count').fetchone()[0]
                page_size=handle._db.execute('PRAGMA page_size').fetchone()[0]
                disk_bytes=page_count*page_size
                cache=handle._db.execute('PRAGMA cache_size').fetchone()[0]
                assert cache == -2048
                prepared=prepare_snapshot(handle)
                try:
                    for chunk in prepared.chunks:assert len(chunk)<=65536
                finally:prepared.close()
                peak=tracemalloc.get_traced_memory()[1]
                stats=dict(handle.stats)
            tracemalloc.stop()
            evidence.append({'members':size,'gaps':size-2,'peak_python':peak,'rss_highwater_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                'disk_bytes':disk_bytes,'cache_kib':-cache,'source':dict(metrics),'handle':stats})
            print('LARGE_GROWTH',evidence[-1],flush=True)
            assert metrics['largest_fetch']<=7
            assert stats['events']==0
            if size==2048:assert disk_bytes>2048*1024
        finally:owned.connection.close()
    assert evidence[-1]['peak_python']<evidence[0]['peak_python']*3
    assert evidence[-1]['peak_python']<evidence[0]['peak_python']+1_000_000
    assert evidence[-1]['source']['vm_steps']<evidence[0]['source']['vm_steps']*12


@pytest.mark.asyncio
async def test_current_keeps_h_when_completion_commits_between_candidate_pages(tmp_path,monkeypatch):
    from contextlib import contextmanager
    from ..persistence.test_segment_storage import _assessed_world,_completion_command
    from camctl.capture.segment_models import DefaultScopeState
    from camctl.persistence.transaction import commit_operation
    owned,runtime,repo,ref,members=await _assessed_world(tmp_path,DefaultScopeState.NONEMPTY)
    try:
        history=HistoryRepository(owned.connection.execute('PRAGMA database_list').fetchone()[2])
        boundary=history.current_boundary()
        status=owned.connection.execute('SELECT status FROM actions WHERE id=?',(ref.action_id,)).fetchone()[0]
        original=history._read_connection
        calls=0
        @contextmanager
        def changing():
            nonlocal calls
            with original() as connection:yield connection
            calls+=1
            if calls==4:
                key=new_operation_key()
                receipt=commit_operation(_completion_command(ref,members,runtime.wall_us(),key),key,owned)
                assert receipt.kind == 'completed',receipt.error
        monkeypatch.setattr(history,'_read_connection',changing)
        with history.open_segment_history(ref.action_id,boundary,strategy='current',batch_size=1) as handle:
            assert handle.active_ref() is None
            assert handle.primary()['status'] == status
            assert all(row.values['output_id'] is None for row in _rows(handle,'segment_scope_members',ref,limit=1))
        assert calls>4 and repo.read_active_set(ref.action_id,owned)==ref
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_snapshot_seed_keeps_database_instance_before_bounded_read(tmp_path,monkeypatch):
    from camctl.history.snapshots import SnapshotRef,SnapshotMaintenanceError
    from camctl.persistence.repositories.history import SqliteSnapshotStore
    from camctl.persistence.executor import DbExecutor
    from camctl.persistence.runtime import open_existing,DbOpenMode,DbConfig
    owned,runtime,repo,ref,history=await _fixture(tmp_path)
    executor=DbExecutor(lambda:open_existing(history.path,DbOpenMode.EXISTING_RW,DbConfig()),capacity=4,enqueue_timeout_seconds=9)
    try:
        original=HistoryRepository.open_segment_history
        def change_instance(self,*args,**kwargs):
            import sqlite3
            from contextlib import closing
            with closing(sqlite3.connect(history.path,isolation_level=None)) as changed:
                changed.execute('UPDATE database_metadata SET instance_id=?',(str(new_operation_key()),))
            return original(self,*args,**kwargs)
        monkeypatch.setattr(HistoryRepository,'open_segment_history',change_instance)
        with pytest.raises(SnapshotMaintenanceError):
            await SqliteSnapshotStore(history.path,executor).load_entity_images((SnapshotRef(1,ref.action_id),))
    finally:
        await executor.close()
        owned.connection.close()


@pytest.mark.asyncio
async def test_page_ref_must_be_the_immutable_segment_reference(tmp_path):
    from types import SimpleNamespace
    owned,runtime,repo,ref,history=await _fixture(tmp_path)
    try:
        mutable=SimpleNamespace(action_id=ref.action_id,scan_id=ref.scan_id,
            final_page_event_id=ref.final_page_event_id,version=ref.version)
        with history.open_segment_history(ref.action_id,history.current_boundary()) as handle:
            with pytest.raises(ConsistencyError):handle.read_page('segment_members',ref=mutable,limit=1)
    finally:owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cache_mode',['hit','miss','unbound'])
async def test_external_restore_view_cannot_borrow_another_instances_verified_range(tmp_path,cache_mode):
    first=tmp_path/'first';first.mkdir()
    second=tmp_path/'second';second.mkdir()
    owned_a,_,_,_,history_a=await _fixture(first)
    owned_b,_,_,_,history_b=await _fixture(second)
    try:
        boundary=history_a.current_boundary()
        assert history_b.current_boundary() == boundary
        assert history_a._current_range is not None
        assert history_a._instance_id != history_b._instance_id
        if cache_mode == 'miss':
            history_a._current_range=None
        elif cache_mode == 'unbound':
            history_a=HistoryRepository(history_a.path)
        file_id=owned_b.connection.execute('SELECT MIN(id) FROM device_files').fetchone()[0]
        owned_b.connection.execute('BEGIN')
        try:
            with pytest.raises(ConsistencyError):
                history_a.restore_within(owned_b.connection,'device_file',file_id,boundary)
            assert history_a._current_range is None
        finally:owned_b.connection.execute('ROLLBACK')
    finally:
        owned_a.connection.close()
        owned_b.connection.close()


@pytest.mark.asyncio
async def test_external_view_read_failure_clears_range_qualification(tmp_path):
    import sqlite3
    owned,_,_,_,history=await _fixture(tmp_path)
    try:
        boundary=history.current_boundary()
        assert history._current_range is not None
        def deny_file_read(operation,table,*_):
            return sqlite3.SQLITE_DENY if operation == sqlite3.SQLITE_READ and table == 'device_files' else sqlite3.SQLITE_OK
        owned.connection.execute('BEGIN')
        owned.connection.set_authorizer(deny_file_read)
        try:
            with pytest.raises(sqlite3.DatabaseError):
                history.restore_within(owned.connection,'device_file',1,boundary)
            assert history._current_range is None
        finally:
            owned.connection.set_authorizer(None)
            owned.connection.execute('ROLLBACK')
    finally:owned.connection.close()

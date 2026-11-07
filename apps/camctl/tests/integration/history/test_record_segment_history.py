"""分段动作使用磁盘句柄恢复，三条路径保持同一 H。"""
from dataclasses import replace

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.persistence.repositories.history import HistoryRepository
from ..persistence.test_segment_storage import _world, _ok, _assessment
from ..capture.test_capture_contract import _NOW

# 录制完成后的事实时刻，与正式拍摄准备的时钟推进保持一致。
_RESULT_TIME = _NOW + 60_000_000


async def _fixture(tmp_path, count=5, *, many_gaps=False):
    owned, runtime, repo, ref, members = await _world(
        tmp_path, indexes=tuple(range(1, count + 1)),
        sizes=tuple((10 if i in (0,count-1) else 0) if many_gaps else (0 if i == 2 else 10) for i in range(count)))
    for offset in range(0, count, 128):
        _ok(repo.append_members(ref, members[offset:offset+128], _RESULT_TIME, new_operation_key(), owned))
    _ok(repo.fix_members(ref, _RESULT_TIME, new_operation_key(), owned))
    facts, result, gaps = _assessment(ref, members, owned)
    for offset in range(0, len(gaps), 128):
        _ok(repo.append_gaps(ref, gaps[offset:offset+128], _RESULT_TIME, new_operation_key(), owned))
    _ok(repo.save_assessment(ref, facts, result, _RESULT_TIME, new_operation_key(), owned))
    first = repo.read_members(ref, None, 1, owned).items[0].member_id
    _ok(repo.append_scope(ref, (first,), _RESULT_TIME, new_operation_key(), owned))
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
async def test_complete_segment_rows_at_boundary(tmp_path, strategy):
    owned, runtime, repo, ref, history = await _fixture(tmp_path)
    try:
        boundary = history.current_boundary()
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
    _ok(repo.create(SegmentSetCreate(ref,*old,_RESULT_TIME),new_operation_key(),owned))
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


def _after_read_transactions(monkeypatch, path, target, action):
    """在业务库只读事务结束后提交，控制历史构建跨页竞争。"""
    import sqlite3
    original = sqlite3.connect
    commits = 0
    class ReadConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            nonlocal commits
            cursor = super().execute(sql, *args, **kwargs)
            if sql.strip().upper() == 'COMMIT':
                commits += 1
                if commits == target:
                    action()
            return cursor
    def connect(database, *args, **kwargs):
        if str(database) == path.resolve().as_uri() + '?mode=ro':
            kwargs['factory'] = ReadConnection
        return original(database, *args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', connect)
    return lambda: commits


@pytest.mark.asyncio
async def test_current_restores_old_h_when_new_version_arrives_between_candidate_pages(tmp_path,monkeypatch):
    owned,runtime,repo,ref,history = await _fixture(tmp_path)
    try:
        boundary = history.current_boundary()
        commits = _after_read_transactions(monkeypatch, history.path, 4,
            lambda: _version(owned,runtime,repo,ref,2))
        with history.open_segment_history(ref.action_id,boundary,strategy='current',batch_size=1) as handle:
            assert [row.values['version'] for row in _rows(handle,'segment_sets')] == [1]
            assert len(tuple(_rows(handle,'segment_members',ref))) == 5
        assert commits() >= 4
        with history.open_segment_history(ref.action_id,history.current_boundary(),strategy='current',batch_size=1) as latest:
            assert [row.values['version'] for row in _rows(latest,'segment_sets')] == [1,2]
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_snapshot_maintenance_uses_stream_source(tmp_path):
    from camctl.history.snapshots import SnapshotRef,prepare_snapshot
    from camctl.persistence.repositories.history import SqliteSnapshotStore
    from camctl.persistence.executor import DbExecutor
    from camctl.persistence.runtime import open_existing,DbOpenMode,DbConfig
    owned,runtime,repo,ref,history = await _fixture(tmp_path)
    executor=DbExecutor(lambda:open_existing(history.path,DbOpenMode.EXISTING_RW,DbConfig()),capacity=4,enqueue_timeout_seconds=9)
    try:
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


def _observe_derived_database(tmp_path, monkeypatch):
    """只接管测试专用临时目录里的 SQLite 文件，不拦截业务状态库。"""
    import sqlite3
    import tempfile
    from pathlib import Path

    directory = tmp_path / 'history-temporary'
    directory.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(directory))
    original_connect = sqlite3.connect
    observed = []
    failure = sqlite3.OperationalError('database or disk is full')

    class ObservedConnection(sqlite3.Connection):
        fail_next_read = False
        fail_after_write = False
        successful_writes = 0

        def execute(self, sql, *args, **kwargs):
            operation = sql.lstrip().split(None, 1)[0].upper()
            if self.fail_next_read and operation == 'SELECT':
                raise failure
            if self.fail_after_write and operation == 'INSERT' and self.successful_writes:
                assert Path(self.database_path).is_file()
                assert Path(self.database_path).stat().st_size > 0
                raise failure
            cursor = super().execute(sql, *args, **kwargs)
            if operation == 'INSERT':
                self.successful_writes += 1
            return cursor

    def connect(database, *args, **kwargs):
        path = Path(database) if isinstance(database, (str, Path)) else None
        if path is not None and path.is_absolute() and path.is_relative_to(directory):
            assert 'factory' not in kwargs
            kwargs['factory'] = ObservedConnection
            connection = original_connect(database, *args, **kwargs)
            connection.database_path = str(path)
            observed.append(connection)
            return connection
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', connect)
    return observed, failure


@pytest.mark.asyncio
async def test_empty_filtered_page_continues_and_query_failure_is_not_eof(tmp_path, monkeypatch):
    import sqlite3
    owned,runtime,repo,ref,history = await _fixture(tmp_path)
    try:
        other=_version(owned,runtime,repo,ref,2)
        observed, failure = _observe_derived_database(tmp_path, monkeypatch)
        with history.open_segment_history(ref.action_id,history.current_boundary(),batch_size=2) as handle:
            page=handle.read_page('segment_sets',ref=other,limit=1)
            assert page.items == () and page.next_cursor is not None
            assert handle.read_page('segment_sets',ref=other,cursor=page.next_cursor,limit=1).items[0].values['version'] == 2
            active = []
            for connection in observed:
                try:
                    connection.execute('SELECT 1').close()
                except sqlite3.ProgrammingError:
                    continue
                active.append(connection)
            assert len(active) == 1
            active[0].fail_next_read = True
            with pytest.raises(sqlite3.OperationalError) as caught:
                handle.read_page('segment_members',ref=ref,limit=2)
            assert caught.value is failure
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
async def test_boundary_rejects_metadata_or_instance_changes(tmp_path,damage):
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
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_old_full_action_entry_refuses_segment_growth(tmp_path):
    owned,runtime,repo,ref,history = await _fixture(tmp_path)
    try:
        boundary=history.current_boundary()
        with pytest.raises(ConsistencyError):history.restore_entity('action',ref.action_id,boundary)
        owned.connection.execute('BEGIN')
        try:
            with pytest.raises(ConsistencyError):history.restore_within(owned.connection,'action',ref.action_id,boundary)
        finally:owned.connection.execute('ROLLBACK')
        assert history.restore_primary_at_h('action',ref.action_id,boundary)['id'] == ref.action_id
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_member_pages_use_device_order_when_rows_were_saved_in_reverse_order(tmp_path):
    owned,runtime,repo,ref,members=await _world(tmp_path,indexes=(1,2,3,4))
    try:
        _ok(repo.append_members(ref,tuple(reversed(members)),_RESULT_TIME,new_operation_key(),owned))
        _ok(repo.fix_members(ref,_RESULT_TIME,new_operation_key(),owned))
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
    from camctl.persistence.repositories.capture import CaptureRepository
    from camctl.persistence.transaction import commit_operation
    from camctl.history.snapshots import prepare_snapshot
    owned,runtime,repo,ref,members=await _assessed_world(tmp_path,DefaultScopeState[scope])
    try:
        key=new_operation_key()
        receipt=commit_operation(_completion_command(owned,ref,members,_RESULT_TIME,key),key,owned)
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
            _ok(CaptureRepository().save_file_checksum(FileChecksumSave(fid,2,_RESULT_TIME,'a'*64),new_operation_key(),owned))
            _ok(CaptureRepository().save_file_presence(FilePresenceSave(fid,3,_RESULT_TIME),new_operation_key(),owned))
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
async def test_snapshot_validation_detects_missing_owned_rows_at_responsible_boundary(tmp_path,removed_table):
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
            if removed_table=='device_activities':
                # 构建完整性按相同S的独立回放验收全部自身成员。
                with history.open_segment_history(ref.action_id,boundary,strategy='initial',batch_size=2) as initial:
                    expected={(row.table,row.row_id):dict(row.values) for row in initial.iter_rows()}
                with history.open_segment_history(ref.action_id,boundary,strategy='snapshot',batch_size=2) as snapshot:
                    actual={(row.table,row.row_id):dict(row.values) for row in snapshot.iter_rows()}
                assert expected!=actual
                missing=set(expected)-set(actual)
                assert len(missing)==1 and next(iter(missing))[0]=='device_activities'
                assert all(actual[key]==value for key,value in expected.items() if key in actual)
            else:
                with pytest.raises(ConsistencyError):
                    history.open_segment_history(ref.action_id,boundary,strategy='snapshot',batch_size=2)
            assert history.current_boundary()==boundary
        finally:prepared.close()
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_disk_failure_closes_partial_handle_and_does_not_change_business(tmp_path,monkeypatch):
    import sqlite3
    from pathlib import Path
    owned,runtime,repo,ref,history=await _fixture(tmp_path)
    try:
        boundary=history.current_boundary()
        observed, failure = _observe_derived_database(tmp_path, monkeypatch)
        # 注入的是派生库的写错误，业务库连接不受影响。
        original_connect = sqlite3.connect
        def connect(database, *args, **kwargs):
            connection = original_connect(database, *args, **kwargs)
            if connection in observed:
                connection.fail_after_write = True
            return connection
        monkeypatch.setattr(sqlite3, 'connect', connect)
        with pytest.raises(sqlite3.OperationalError) as caught:
            history.open_segment_history(ref.action_id,boundary,strategy='initial',batch_size=1)
        assert caught.value is failure
        assert len(observed) == 1 and observed[0].successful_writes >= 1
        with pytest.raises(sqlite3.ProgrammingError):
            observed[0].execute('SELECT 1')
        assert not Path(observed[0].database_path).exists()
        assert not Path(observed[0].database_path).parent.exists()
        assert history.current_boundary() == boundary
        assert owned.connection.execute('SELECT COUNT(*) FROM segment_members').fetchone() == (5,)
    finally:owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage',['member_count','gap_summary'])
async def test_snapshot_rejects_summary_inconsistent_with_required_members(tmp_path,damage):
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
        _ok(repo.append_members(ref,members,_RESULT_TIME,new_operation_key(),owned))
        _ok(repo.fix_members(ref,_RESULT_TIME,new_operation_key(),owned))
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
        _ok(repo.save_assessment(ref,facts,result,_RESULT_TIME,new_operation_key(),owned))
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
async def test_connection_failure_is_reported_and_later_read_recovers(tmp_path,monkeypatch):
    import sqlite3
    from camctl.persistence.runtime import DatabaseInvalidError
    owned,_,_,_,history=await _fixture(tmp_path)
    try:
        boundary=history.current_boundary()
        original=sqlite3.connect
        def fail(database,*args,**kwargs):
            if str(database).startswith(history.path.resolve().as_uri()):
                raise sqlite3.OperationalError('cannot open database')
            return original(database,*args,**kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3,'connect',fail)
            with pytest.raises(DatabaseInvalidError, match='状态库打开失败'):
                history.current_boundary()
        assert history.current_boundary() == boundary
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_current_keeps_h_when_completion_commits_between_candidate_pages(tmp_path,monkeypatch):
    from ..persistence.test_segment_storage import _assessed_world,_completion_command
    from camctl.capture.segment_models import DefaultScopeState
    from camctl.persistence.transaction import commit_operation
    owned,runtime,repo,ref,members=await _assessed_world(tmp_path,DefaultScopeState.NONEMPTY)
    try:
        history=HistoryRepository(owned.connection.execute('PRAGMA database_list').fetchone()[2])
        boundary=history.current_boundary()
        status=owned.connection.execute('SELECT status FROM actions WHERE id=?',(ref.action_id,)).fetchone()[0]
        def complete():
            key=new_operation_key()
            receipt=commit_operation(_completion_command(owned,ref,members,_RESULT_TIME,key),key,owned)
            assert receipt.kind == 'completed',receipt.error
        commits=_after_read_transactions(monkeypatch,history.path,4,complete)
        with history.open_segment_history(ref.action_id,boundary,strategy='current',batch_size=1) as handle:
            assert handle.active_ref() is None
            assert handle.primary()['status'] == status
            assert all(row.values['output_id'] is None for row in _rows(handle,'segment_scope_members',ref,limit=1))
        assert commits() >= 4 and repo.read_active_set(ref.action_id,owned)==ref
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
async def test_external_restore_view_rejects_another_database_instance(tmp_path):
    first=tmp_path/'first';first.mkdir()
    second=tmp_path/'second';second.mkdir()
    owned_a,_,_,_,history_a=await _fixture(first)
    owned_b,_,_,_,history_b=await _fixture(second)
    try:
        boundary=history_a.current_boundary()
        assert history_b.current_boundary() == boundary
        file_id=owned_b.connection.execute('SELECT MIN(id) FROM device_files').fetchone()[0]
        owned_b.connection.execute('BEGIN')
        try:
            with pytest.raises(ConsistencyError):
                history_a.restore_within(owned_b.connection,'device_file',file_id,boundary)
        finally:owned_b.connection.execute('ROLLBACK')
        assert history_a.current_boundary() == boundary
    finally:
        owned_a.connection.close()
        owned_b.connection.close()


@pytest.mark.asyncio
async def test_external_view_read_failure_is_reported_and_later_read_recovers(tmp_path):
    import sqlite3
    owned,_,_,_,history=await _fixture(tmp_path)
    try:
        boundary=history.current_boundary()
        def deny_file_read(operation,table,*_):
            return sqlite3.SQLITE_DENY if operation == sqlite3.SQLITE_READ and table == 'device_files' else sqlite3.SQLITE_OK
        owned.connection.execute('BEGIN')
        owned.connection.set_authorizer(deny_file_read)
        try:
            with pytest.raises(sqlite3.DatabaseError):
                history.restore_within(owned.connection,'device_file',1,boundary)
        finally:
            owned.connection.set_authorizer(None)
            owned.connection.execute('ROLLBACK')
        assert history.current_boundary() == boundary
        assert history.restore_entity('device_file',1,boundary)
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_real_large_segment_source_preparation(tmp_path):
    """真实逐页提交 2048 项并核对完整库；准备耗时属于本次测试。"""
    from ..support.segment_history_corpus import generate
    await generate(tmp_path/'world',2048)


@pytest.mark.parametrize('size',[256,2048])
@pytest.mark.parametrize('diagnostics',[False,True],ids=['budget','growth-diagnostic'])
def test_large_segment_history_budget(tmp_path,size,diagnostics):
    """独立进程在合计预算内消费全部分页和快照，组外核对输出。"""
    import json, os, sys, time
    from pathlib import Path
    from ..support.source_capacity import memory_budget_backend
    from ..support.segment_history_corpus import prepare_copy, verify_output
    backend=memory_budget_backend()
    budget=backend.get_budget_config()
    backend.ensure_memory_budget_available(budget)
    started=time.perf_counter()
    directory=tmp_path/'input'
    manifest=prepare_copy(directory,size)
    prepare_seconds=time.perf_counter()-started
    root=Path(__file__).resolve().parents[5]
    worker=root/'apps/camctl/tests/integration/support/segment_history_worker.py'
    environment=dict(os.environ)
    environment['PYTHONPATH']=os.pathsep.join(str(root/p) for p in ('apps/camctl/src','apps/camctl/tests'))
    started=time.perf_counter()
    with (tmp_path/'worker.stdout').open('w') as out, (tmp_path/'worker.stderr').open('w') as err:
        backend.run_budgeted([sys.executable,str(worker),str(directory/'input.json'),str(directory)]
            + (['--diagnose'] if diagnostics else []),
            budget=budget,receipt_path=tmp_path/'memory-budget.json',timeout=600,check=True,
            cwd=root,env=environment,stdout=out,stderr=err,text=True)
    run_seconds=time.perf_counter()-started
    started=time.perf_counter()
    verify_output(directory,manifest)
    receipt=json.loads((tmp_path/'memory-budget.json').read_text())
    assert receipt['status']=='completed' and receipt['returncode']==0 and receipt['group_removed']
    evidence=receipt['evidence']
    assert evidence['limit_bytes']==evidence['combined_limit_bytes']==budget.limit_bytes
    assert evidence['combined_peak_bytes']<=budget.limit_bytes
    assert evidence['events'].get('oom_kill',evidence['events'].get('oom',0))==0
    print('SEGMENT_HISTORY_BUDGET',json.dumps({'count':size,'prepare_seconds':prepare_seconds,
        'run_seconds':run_seconds,'verify_seconds':time.perf_counter()-started,
        'diagnostics':json.loads((directory/'result.json').read_text())['diagnostics'],
        'evidence':evidence}),flush=True)

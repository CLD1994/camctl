"""快照构建验收、正向读取、受管线程和取消来源的回归。"""
import asyncio
from pathlib import Path
from threading import Event, get_ident

import pytest

from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.history.snapshots import prepare_snapshot
from camctl.persistence.repositories.history import HistoryRepository
from camctl.persistence.transaction import encode_json_value
from .test_record_segment_history import (
    _fixture,
    _save,
    _version,
)


async def _assert_same_s_snapshot_bytes(owned, repo, ref, history, boundary, previous_paths):
    """重新准备同一边界的快照，检查重复保存和损坏拒绝。"""
    from camctl.history.snapshots import SnapshotMaintenanceError

    with history.open_segment_history(ref.action_id, boundary, strategy='current', batch_size=7) as handle:
        prepared = prepare_snapshot(handle)
    try:
        assert prepared.path not in previous_paths
        assert prepared.path.exists() and prepared.length > 65540
        assert prepared.boundary == boundary
        await _save(history, prepared)
        assert owned.connection.execute('SELECT COUNT(*) FROM entity_snapshots').fetchone()[0] == 1
        assert history.current_boundary() == boundary
        with prepared.path.open('r+b') as source:
            source.seek(65540)
            before = source.read(1)
            assert len(before) == 1
            source.seek(65540)
            source.write(b'x' if before != b'x' else b'y')
        with pytest.raises(SnapshotMaintenanceError):
            await _save(history, prepared)
        snapshot_id = owned.connection.execute('SELECT id FROM entity_snapshots').fetchone()[0]
        with owned.connection.blobopen('entity_snapshots', 'content', snapshot_id) as blob:
            blob.seek(65540)
            blob.write(b'\xff')
        _version(owned, None, repo, ref, 2)
        with pytest.raises(ConsistencyError):
            history.open_segment_history(ref.action_id, boundary, strategy='snapshot', batch_size=7)
    finally:
        prepared.close()


def _change_snapshot(owned, prepared, transform):
    records=[parse_exact_json(line) for line in prepared.path.read_text().splitlines()]
    transform(records)
    owned.connection.execute('UPDATE entity_snapshots SET content=?',
        ((''.join(encode_json_value(record)+'\n' for record in records)).encode(),))


def _snapshot_matches_initial(owned, history, ref, prepared):
    """构建验收独立回放S，并比较已登记快照的完整映像。"""
    content = owned.connection.execute(
        'SELECT content FROM entity_snapshots WHERE entity_type=? AND entity_id=? AND boundary_event_id=?',
        (prepared.entity_type, prepared.entity_id, prepared.boundary.last_event_id)).fetchone()[0]
    records = [parse_exact_json(line) for line in content.decode().splitlines()]
    snapshot_rows = {(record['table'], record['row']['id']): record['row'] for record in records[1:]}
    with history.open_segment_history(ref.action_id, prepared.boundary, strategy='initial', batch_size=2) as replay:
        replay_rows = {(row.table, row.row_id): dict(row.values) for row in replay.iter_rows()}
    return snapshot_rows == replay_rows


def _finish_recording(owned, runtime, repo, ref):
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.capture import CaptureRepository
    from camctl.persistence.repositories.segment_transactions import FinishSegmentRecording

    row = repo.read_set(ref, owned)
    request = FinishSegmentRecording(ref, row['activity_id'], row['run_id'], runtime.wall_us())
    result = CaptureRepository().finish_segment_recording(request, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error


@pytest.mark.asyncio
@pytest.mark.parametrize('damage',['assessment_null','member_count_null','marker_null','marker_scope_null'])
async def test_snapshot_acceptance_compares_write_once_values_with_independent_replay(tmp_path,damage):
    if damage.startswith('marker'):
        from ..persistence.test_segment_storage import _assessed_world
        from camctl.capture.segment_models import DefaultScopeState
        owned,runtime,repo,ref,members=await _assessed_world(tmp_path,DefaultScopeState.NONEMPTY)
        _finish_recording(owned,runtime,repo,ref)
        history=HistoryRepository(owned.connection.execute('PRAGMA database_list').fetchone()[2])
    else:
        owned,runtime,repo,ref,history=await _fixture(tmp_path)
    try:
        boundary=history.current_boundary()
        with history.open_segment_history(ref.action_id,boundary) as handle:
            prepared=prepare_snapshot(handle)
        try:
            await _save(history,prepared)
            assert _snapshot_matches_initial(owned,history,ref,prepared)
            def change(records):
                saved=next(row['row'] for row in records if row.get('table')=='segment_sets')
                if damage in ('assessment_null','member_count_null'):
                    saved.update(state=2,facts_json=None,assessment_json=None)
                    if damage=='member_count_null':saved.update(state=1,member_count=None)
                else:
                    saved['published_event_id']=None
                    if damage=='marker_scope_null':
                        for row in records:
                            if row.get('table')=='segment_scope_members':row['row']['output_id']=None
            _change_snapshot(owned,prepared,change)
            assert not _snapshot_matches_initial(owned,history,ref,prepared)
            assert history.current_boundary()==boundary
        finally:prepared.close()
    finally:owned.connection.close()


async def _before_assessment(tmp_path):
    from ..persistence.test_segment_storage import _world,_ok,_assessment
    owned,runtime,repo,ref,members=await _world(tmp_path,indexes=(1,2))
    _ok(repo.append_members(ref,members,runtime.wall_us(),new_operation_key(),owned))
    _ok(repo.fix_members(ref,runtime.wall_us(),new_operation_key(),owned))
    history=HistoryRepository(owned.connection.execute('PRAGMA database_list').fetchone()[2])
    boundary=history.current_boundary()
    with history.open_segment_history(ref.action_id,boundary) as handle:prepared=prepare_snapshot(handle)
    await _save(history,prepared)
    facts,result,gaps=_assessment(ref,members,owned)
    assert not gaps
    _ok(repo.save_assessment(ref,facts,result,runtime.wall_us(),new_operation_key(),owned))
    return owned,runtime,repo,ref,history,boundary,prepared


@pytest.mark.asyncio
@pytest.mark.parametrize('after_assignment',[False,True])
async def test_snapshot_null_before_assignment_is_valid_at_original_h(tmp_path,after_assignment):
    owned,runtime,repo,ref,history,before,prepared=await _before_assessment(tmp_path)
    try:
        boundary=history.current_boundary() if after_assignment else before
        for strategy in ('initial','snapshot','current'):
            with history.open_segment_history(ref.action_id,boundary,strategy=strategy,batch_size=1) as handle:
                assert (handle.read_set(ref)['assessment_json'] is not None)==after_assignment
    finally:
        prepared.close();owned.connection.close()


@pytest.mark.asyncio
async def test_snapshot_acceptance_compares_assessment_at_s_with_independent_replay(tmp_path):
    owned,runtime,repo,ref,history,before,prepared=await _before_assessment(tmp_path)
    try:
        current=repo.read_set(ref,owned)
        def change(records):
            saved=next(row['row'] for row in records if row.get('table')=='segment_sets')
            saved.update(state=3,facts_json=current['facts_json'],assessment_json=current['assessment_json'])
        _change_snapshot(owned,prepared,change)
        assert not _snapshot_matches_initial(owned,history,ref,prepared)
    finally:
        prepared.close();owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('future',['marker_and_outputs','outputs_only'])
async def test_snapshot_acceptance_compares_publication_at_s_with_independent_replay(tmp_path,future):
    from ..persistence.test_segment_storage import _assessed_world
    from camctl.capture.segment_models import DefaultScopeState
    owned,runtime,repo,ref,members=await _assessed_world(tmp_path,DefaultScopeState.NONEMPTY)
    history=HistoryRepository(owned.connection.execute('PRAGMA database_list').fetchone()[2])
    try:
        before=history.current_boundary()
        with history.open_segment_history(ref.action_id,before) as handle:prepared=prepare_snapshot(handle)
        try:
            await _save(history,prepared)
            _finish_recording(owned,runtime,repo,ref)
            marker=repo.read_set(ref,owned)['published_event_id']
            outputs=dict(owned.connection.execute('SELECT id,output_id FROM segment_scope_members'))
            def change(records):
                for record in records:
                    if record.get('table')=='segment_sets' and future=='marker_and_outputs':record['row']['published_event_id']=marker
                    elif record.get('table')=='segment_scope_members':record['row']['output_id']=outputs[record['row']['id']]
            _change_snapshot(owned,prepared,change)
            assert not _snapshot_matches_initial(owned,history,ref,prepared)
        finally:prepared.close()
    finally:owned.connection.close()


async def _snapshot_store(history):
    from camctl.persistence.repositories.history import SqliteSnapshotStore
    from camctl.persistence.executor import DbExecutor
    from camctl.persistence.runtime import open_existing,DbOpenMode,DbConfig
    executor=DbExecutor(lambda:open_existing(history.path,DbOpenMode.EXISTING_RW,DbConfig()),capacity=8,enqueue_timeout_seconds=9)
    return SqliteSnapshotStore(history.path,executor),executor


@pytest.mark.asyncio
async def test_growth_snapshot_restore_runs_in_managed_thread_and_event_loop_is_live(tmp_path,monkeypatch):
    from camctl.history.snapshots import SnapshotRef
    owned,_,_,ref,history=await _fixture(tmp_path)
    store,executor=await _snapshot_store(history)
    main=get_ident();seen=[];started=asyncio.Event();release=Event()
    loop=asyncio.get_running_loop()
    original=HistoryRepository.open_segment_history
    def restore(self,*args,**kwargs):
        seen.append(get_ident())
        loop.call_soon_threadsafe(started.set)
        # 主线程实证直接留下线程ID，避免让失败版本把事件循环锁死。
        if get_ident()!=main:
            assert release.wait(60),'test gate was not released'
        return original(self,*args,**kwargs)
    monkeypatch.setattr(HistoryRepository,'open_segment_history',restore)
    task=asyncio.create_task(store.load_entity_images((SnapshotRef(1,ref.action_id),)))
    images=()
    try:
        await asyncio.wait_for(started.wait(),60)
        assert seen and seen[0]!=main
        assert not task.done()
        notice=asyncio.Event();loop.call_soon(notice.set)
        await asyncio.wait_for(notice.wait(),60)
        assert not task.done()
        release.set()
        images=await task
        assert images[0]._db is None
        assert images[0].primary()['id']==ref.action_id
    finally:
        release.set()
        if not task.done():images=await task
        elif not images and not task.cancelled() and task.exception() is None:images=task.result()
        for image in images:image.close()
        await executor.close();owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['first', 'middle', 'later_source', 'rollback'])
async def test_cancel_started_save_keeps_source_until_actual_completion(tmp_path, monkeypatch, phase):
    import sqlite3
    import tempfile
    from camctl.history.snapshots import (
        MaintenanceContext, MaintenanceState, SnapshotMaintenanceError, SnapshotRef, maintain_snapshots,
    )
    from camctl.persistence.models import DbOutcomeKind

    if phase == 'middle':
        from camctl.capture.segment_models import SegmentSetRef
        from camctl.persistence.repositories.segments import SegmentRepository
        from camctl.persistence.runtime import open_existing, DbOpenMode, DbConfig
        from ..support.segment_history_corpus import prepare_copy

        directory = tmp_path / 'source'
        manifest = prepare_copy(directory, 256)
        ref = SegmentSetRef(*(manifest[name] for name in ('action_id', 'scan_id', 'final_page_event_id', 'version')))
        owned = open_existing(directory / 'state.db', DbOpenMode.EXISTING_RW, DbConfig())
        repo = SegmentRepository()
        history = HistoryRepository(directory / 'state.db')
    else:
        owned, _, repo, ref, history = await _fixture(tmp_path)

    boundary = history.current_boundary()
    refs = [SnapshotRef(1, ref.action_id)]
    if phase == 'later_source':
        from camctl.contracts.enums import load_registry
        file_kind = load_registry()['history_objects']['device_file']['id']
        file_id = owned.connection.execute('SELECT MIN(id) FROM device_files').fetchone()[0]
        refs.insert(0, SnapshotRef(file_kind, file_id))
    progress_before = {
        (item.entity_type, item.entity_id): owned.connection.execute(
            'SELECT snapshot_change_count, latest_snapshot_id FROM entity_snapshot_progress'
            ' WHERE entity_type=? AND entity_id=?', (item.entity_type, item.entity_id)).fetchone()
        for item in refs
    }
    temporary = tmp_path / 'snapshot-temporary'
    temporary.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(temporary))
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = Event()
    opened, writes, closed, transactions, updated_progress = [], [], [], [], []
    real_connect = sqlite3.connect
    target_uri = history.path.resolve().as_uri() + '?mode=rw'
    failure = sqlite3.OperationalError('测试在快照写入后触发回滚')

    def gate():
        loop.call_soon_threadsafe(started.set)
        assert release.wait(60), '快照写入门闩未释放'

    class Blob:
        def __init__(self, blob, index):
            self.blob, self.index = blob, index

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            self.blob.close()
            closed.append(self.index)

        def write(self, data):
            self.blob.write(data)
            writes.append((self.index, len(data)))
            if self.index == 1 and len(writes) == 1 and phase == 'middle':
                gate()

    class Connection(sqlite3.Connection):
        def blobopen(self, table, column, row, **kwargs):
            blob = super().blobopen(table, column, row, **kwargs)
            if (table, column) != ('entity_snapshots', 'content'):
                return blob
            index = len(opened) + 1
            opened.append(index)
            if (phase == 'first' and index == 1) or (phase == 'later_source' and index == 2):
                gate()
            return Blob(blob, index)

        def execute(self, sql, *args, **kwargs):
            cursor = super().execute(sql, *args, **kwargs)
            operation = ' '.join(sql.upper().split())
            if phase == 'rollback' and operation.startswith('UPDATE ENTITY_SNAPSHOT_PROGRESS'):
                updated_progress.append(super().execute(
                    'SELECT snapshot_change_count, latest_snapshot_id FROM entity_snapshot_progress'
                    ' WHERE entity_type=? AND entity_id=?', (1, ref.action_id)).fetchone())
                gate()
                raise failure
            if opened and operation in ('COMMIT', 'ROLLBACK'):
                transactions.append(operation)
            return cursor

    def connect(database, *args, **kwargs):
        if str(database) == target_uri:
            assert 'factory' not in kwargs
            kwargs['factory'] = Connection
        return real_connect(database, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, 'connect', connect)
            store, executor = await _snapshot_store(history)
            selected = False

            async def candidates(*_):
                nonlocal selected
                if selected:
                    return ()
                selected = True
                return tuple(refs)

            patch.setattr(store, 'snapshot_candidates', candidates)
            task = asyncio.create_task(maintain_snapshots(MaintenanceContext(
                store=store, state=MaintenanceState(), threshold=1, batch_size=3)))
            try:
                await asyncio.wait_for(started.wait(), 60)
                paths = list(temporary.rglob('content.jsonl'))
                assert len(paths) == 1 and paths[0].is_file()
                expected_content = paths[0].read_bytes()
                if phase == 'middle':
                    assert len(expected_content) > 65536
                    assert writes == [(1, 65536)]
                elif phase == 'later_source':
                    assert opened == [1, 2] and closed == [1]
                    assert writes and all(index == 1 for index, _ in writes)
                elif phase == 'first':
                    assert opened == [1] and not writes
                else:
                    assert opened == [1] and writes and writes[0][0] == 1
                    assert closed == [1] and len(updated_progress) == 1
                    assert updated_progress[0][0] > progress_before[(1, ref.action_id)][0]
                    assert updated_progress[0][1] is not None
                # 独立连接在写事务未结束时不能看到已写的快照。
                assert owned.connection.execute('SELECT COUNT(*) FROM entity_snapshots').fetchone()[0] == 0
                task.cancel()
                delivered = loop.create_future()
                loop.call_soon(delivered.set_result, None)
                await delivered
                assert all(path.exists() for path in paths)
                assert not task.done()
                task.cancel()
                release.set()
                if phase == 'rollback':
                    with pytest.raises(SnapshotMaintenanceError, match='rolled_back') as raised:
                        await task
                    assert raised.value.result.kind is DbOutcomeKind.ROLLED_BACK
                    assert raised.value.result.error is failure
                else:
                    with pytest.raises(asyncio.CancelledError):
                        await task
                await executor.close()
                assert closed == opened
                assert transactions == (['ROLLBACK'] if phase == 'rollback' else ['COMMIT'])
                assert all(not path.exists() for path in paths)
                rows = owned.connection.execute(
                    'SELECT entity_type, entity_id, content, change_count FROM entity_snapshots ORDER BY entity_type'
                ).fetchall()
                if phase == 'rollback':
                    assert rows == []
                    for item in refs:
                        assert owned.connection.execute(
                            'SELECT snapshot_change_count, latest_snapshot_id FROM entity_snapshot_progress'
                            ' WHERE entity_type=? AND entity_id=?', (item.entity_type, item.entity_id)
                        ).fetchone() == progress_before[(item.entity_type, item.entity_id)]
                else:
                    assert {(row[0], row[1]) for row in rows} == {
                        (item.entity_type, item.entity_id) for item in refs}
                    assert next(row[2] for row in rows if row[:2] == (1, ref.action_id)) == expected_content
                    for entity_type, entity_id, _, count in rows:
                        progress = owned.connection.execute(
                            'SELECT snapshot_change_count, latest_snapshot_id FROM entity_snapshot_progress'
                            ' WHERE entity_type=? AND entity_id=?', (entity_type, entity_id)).fetchone()
                        assert progress[0] == count and progress[1] is not None
                assert history.current_boundary() == boundary
            finally:
                release.set()
                if not task.done():
                    try:
                        await task
                    except (asyncio.CancelledError, SnapshotMaintenanceError):
                        pass
                await executor.close()

    except BaseException:
        owned.connection.close()
        raise

    try:
        if phase == 'middle':
            await _assert_same_s_snapshot_bytes(owned, repo, ref, history, boundary, paths)
    finally:
        owned.connection.close()


async def _block_business(executor):
    from camctl.persistence.models import DbJob,DbJobKind,DbPriority
    loop=asyncio.get_running_loop();started=asyncio.Event();release=Event()
    def block(_):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(60),'business gate was not released'
    future=executor.submit_read(DbJob(new_operation_key(),'test business gate',DbJobKind.READ,DbPriority.BUSINESS,block))
    await asyncio.wait_for(started.wait(),60)
    return release,future


@pytest.mark.asyncio
async def test_business_waiting_before_snapshot_restore_runs_first(tmp_path,monkeypatch):
    from camctl.history.snapshots import SnapshotRef
    from camctl.persistence.models import DbJob,DbJobKind,DbPriority
    owned,_,_,ref,history=await _fixture(tmp_path)
    store,executor=await _snapshot_store(history)
    release,blocked=await _block_business(executor)
    admitted=asyncio.Event();order=[];priorities=[]
    original_submit=executor.submit_read
    def submit(job):
        if job.priority is DbPriority.SNAPSHOT:
            priorities.append(job.priority);admitted.set()
        return original_submit(job)
    monkeypatch.setattr(executor,'submit_read',submit)
    original_restore=HistoryRepository.open_segment_history
    def restore(self,*args,**kwargs):
        order.append('restore');return original_restore(self,*args,**kwargs)
    monkeypatch.setattr(HistoryRepository,'open_segment_history',restore)
    load=asyncio.create_task(store.load_entity_images((SnapshotRef(1,ref.action_id),)))
    images=()
    try:
        await asyncio.wait_for(admitted.wait(),60)
        business=executor.submit_read(DbJob(new_operation_key(),'waiting business',DbJobKind.READ,DbPriority.BUSINESS,lambda _:order.append('business')))
        release.set()
        images=await load
        assert (await business).error is None
        assert order==['business','restore']
        assert len(priorities)==2
    finally:
        release.set();await blocked
        if not load.done():images=await load
        for image in images:image.close()
        await executor.close();owned.connection.close()


@pytest.mark.asyncio
async def test_preparation_uses_separate_worker_and_releases_database_for_business(tmp_path,monkeypatch):
    from camctl.history import snapshots as module
    from camctl.persistence.models import DbJob,DbJobKind,DbPriority,DbOutcome,DbOutcomeKind
    from .test_record_segment_history import _version
    owned,runtime,repo,ref,history=await _fixture(tmp_path)
    store,executor=await _snapshot_store(history)
    async def candidates(*_):return (module.SnapshotRef(1,ref.action_id),)
    monkeypatch.setattr(store,'snapshot_candidates',candidates)
    loop=asyncio.get_running_loop();started=asyncio.Event();release=Event();preparation_threads=[];business_threads=[]
    original=module.prepare_snapshot
    def prepare(image):
        preparation_threads.append(get_ident());loop.call_soon_threadsafe(started.set)
        assert release.wait(60),'preparation gate was not released'
        return original(image)
    monkeypatch.setattr(module,'prepare_snapshot',prepare)
    task=asyncio.create_task(module.maintain_snapshots(module.MaintenanceContext(store=store,state=module.MaintenanceState(),threshold=1,batch_size=2)))
    try:
        await asyncio.wait_for(started.wait(),60)
        def business(owned):
            business_threads.append(get_ident());_version(owned,runtime,repo,ref,2)
            return DbOutcome(DbOutcomeKind.COMPLETED,value=True)
        completed=await executor.submit_write(DbJob(new_operation_key(),'business during preparation',DbJobKind.WRITE,DbPriority.BUSINESS,business))
        assert completed.kind is DbOutcomeKind.COMPLETED
        assert preparation_threads[0] not in (get_ident(),business_threads[0])
        assert not task.done()
        release.set();result=await task
        assert result.saved[0].remaining_changes==1
        assert owned.connection.execute('SELECT change_count FROM entity_snapshots WHERE entity_type=1').fetchone()[0] < owned.connection.execute('SELECT change_count FROM actions WHERE id=?',(ref.action_id,)).fetchone()[0]
    finally:
        release.set()
        if not task.done():await task
        await executor.close();owned.connection.close()


@pytest.mark.asyncio
async def test_cancel_queued_snapshot_load_withdraws_without_late_resources(tmp_path,monkeypatch):
    from camctl.history.snapshots import SnapshotRef
    from camctl.persistence.models import DbOutcomeKind,DbPriority,WithdrawalStatus
    owned,_,_,ref,history=await _fixture(tmp_path)
    store,executor=await _snapshot_store(history)
    release,blocked=await _block_business(executor)
    admitted=asyncio.Event();restored=[];submitted=[];withdrawals=[]
    original_submit=executor.submit_read
    def submit(job):
        result=original_submit(job)
        if job.priority is DbPriority.SNAPSHOT:
            submitted.append((job.key,result));admitted.set()
        return result
    monkeypatch.setattr(executor,'submit_read',submit)
    original_withdraw=executor.withdraw
    async def withdraw(key,*,expected_result=None):
        result=await original_withdraw(key,expected_result=expected_result)
        withdrawals.append((result,expected_result))
        return result
    monkeypatch.setattr(executor,'withdraw',withdraw)
    original_restore=HistoryRepository.open_segment_history
    def restore(self,*args,**kwargs):
        restored.append(True)
        return original_restore(self,*args,**kwargs)
    monkeypatch.setattr(HistoryRepository,'open_segment_history',restore)
    task=asyncio.create_task(store.load_entity_images((SnapshotRef(1,ref.action_id),)))
    try:
        await asyncio.wait_for(admitted.wait(),60)
        assert len(submitted)==1 and not submitted[0][1].done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        key,future=submitted[0]
        assert withdrawals and withdrawals[0][0].key==key
        assert withdrawals[0][0].status is WithdrawalStatus.WITHDRAWN
        assert withdrawals[0][1] is future
        assert (await asyncio.shield(future)).kind is DbOutcomeKind.NOT_EXECUTED
        release.set();await blocked
        await executor.close()
        assert restored==[]
    finally:
        release.set();await blocked
        await executor.close();owned.connection.close()


@pytest.mark.asyncio
async def test_cancel_started_load_receives_and_closes_late_detached_handle(tmp_path,monkeypatch):
    import sqlite3
    import tempfile
    from camctl.history.snapshots import SnapshotRef
    from camctl.history.bounded import SegmentHistoryHandle
    from camctl.persistence.models import DbPriority,ReadReceipt
    owned,_,_,ref,history=await _fixture(tmp_path)
    store,executor=await _snapshot_store(history)
    temporary=tmp_path/'history-temporary'
    temporary.mkdir()
    loop=asyncio.get_running_loop();started=asyncio.Event();release=Event();opened=[];closed=[];derived=[];submitted=[]
    real_connect=sqlite3.connect
    class DerivedConnection(sqlite3.Connection):
        def close(self):
            super().close()
            self.was_closed=True
            try:
                self.execute('SELECT 1')
            except sqlite3.ProgrammingError:
                self.closed_query_rejected=True
            else:
                raise AssertionError('派生连接关闭后仍可查询')
    def connect(database,*args,**kwargs):
        path=Path(database) if isinstance(database,(str,Path)) else None
        if path is not None and path.is_absolute() and path.is_relative_to(temporary):
            assert 'factory' not in kwargs
            connection=real_connect(database,*args,factory=DerivedConnection,**kwargs)
            connection.was_closed=False
            derived.append((connection,path))
            return connection
        return real_connect(database,*args,**kwargs)
    monkeypatch.setattr(tempfile,'tempdir',str(temporary))
    monkeypatch.setattr(sqlite3,'connect',connect)
    original_submit=executor.submit_read
    def submit(job):
        future=original_submit(job)
        if job.priority is DbPriority.SNAPSHOT:submitted.append((job.key,future))
        return future
    monkeypatch.setattr(executor,'submit_read',submit)
    original_close=SegmentHistoryHandle.close
    def close(handle):
        result=original_close(handle)
        closed.append(handle)
        return result
    monkeypatch.setattr(SegmentHistoryHandle,'close',close)
    original=HistoryRepository.open_segment_history
    def restore(self,*args,**kwargs):
        handle=original(self,*args,**kwargs)
        opened.append(handle)
        loop.call_soon_threadsafe(started.set)
        assert release.wait(60),'load gate was not released'
        return handle
    monkeypatch.setattr(HistoryRepository,'open_segment_history',restore)
    task=asyncio.create_task(store.load_entity_images((SnapshotRef(1,ref.action_id),)))
    try:
        await asyncio.wait_for(started.wait(),60);task.cancel()
        delivered=loop.create_future();loop.call_soon(delivered.set_result,None);await delivered
        task.cancel()
        assert len(opened)==1 and derived
        active=[(connection,path) for connection,path in derived if not connection.was_closed]
        assert active and all(path.is_file() and path.parent.is_dir()
                              for connection,path in active)
        pending=[future for _,future in submitted if not future.done()]
        assert pending and not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):await task
        assert all(future.done() and not future.cancelled() for _,future in submitted)
        receipts=[future.result() for future in pending]
        delivered=[receipt for receipt in receipts if isinstance(receipt,ReadReceipt)
                   and receipt.error is None and receipt.value==(opened[0],)]
        assert len(delivered)==1
        assert all(any(actual is handle for actual in closed) for handle in opened)
        assert all(connection.was_closed and connection.closed_query_rejected
                   and not path.exists() and not path.parent.exists()
                   for connection,path in derived)
        await executor.close()
    finally:
        release.set()
        if not task.done():
            try:await task
            except asyncio.CancelledError:pass
        await executor.close();owned.connection.close()


@pytest.mark.asyncio
async def test_cancel_commit_unknown_waits_for_connection_cleanup_and_preserves_classification(tmp_path, monkeypatch):
    import sqlite3
    import tempfile
    from camctl.history.snapshots import MaintenanceContext, MaintenanceState, SnapshotMaintenanceError, maintain_snapshots
    from camctl.persistence.executor import DbExecutor
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.history import SqliteSnapshotStore
    from camctl.persistence.runtime import open_existing, DbOpenMode, DbConfig

    owned, _, _, ref, history = await _fixture(tmp_path)
    boundary = history.current_boundary()
    temporary = tmp_path / 'snapshot-temporary'
    temporary.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(temporary))
    loop = asyncio.get_running_loop()
    closing = asyncio.Event()
    release = Event()
    state = {'saving': False, 'closed': False}
    real_connect = sqlite3.connect
    target_uri = history.path.resolve().as_uri() + '?mode=rw'
    failure = sqlite3.OperationalError('commit acknowledgement lost')

    class Connection(sqlite3.Connection):
        def blobopen(self, table, column, row, **kwargs):
            blob = super().blobopen(table, column, row, **kwargs)
            if (table, column) == ('entity_snapshots', 'content'):
                state['saving'] = True
            return blob

        def execute(self, statement, *args, **kwargs):
            result = super().execute(statement, *args, **kwargs)
            if statement == 'COMMIT' and state['saving']:
                result.close()
                raise failure
            return result

        def close(self):
            loop.call_soon_threadsafe(closing.set)
            assert release.wait(60), '数据库连接关闭门闩未释放'
            super().close()
            state['closed'] = True

    def connect(database, *args, **kwargs):
        if str(database) == target_uri:
            assert 'factory' not in kwargs
            kwargs['factory'] = Connection
        return real_connect(database, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, 'connect', connect)
            executor = DbExecutor(
                lambda: open_existing(history.path, DbOpenMode.EXISTING_RW, DbConfig()),
                capacity=8, enqueue_timeout_seconds=9)
            store = SqliteSnapshotStore(history.path, executor)
            task = asyncio.create_task(maintain_snapshots(MaintenanceContext(
                store=store, state=MaintenanceState(), threshold=1, batch_size=2)))
            try:
                await asyncio.wait_for(closing.wait(), 60)
                paths = list(temporary.rglob('content.jsonl'))
                assert len(paths) == 1 and paths[0].is_file()
                expected_content = paths[0].read_bytes()
                task.cancel()
                delivered = loop.create_future()
                loop.call_soon(delivered.set_result, None)
                await delivered
                assert all(path.exists() for path in paths) and not task.done() and not state['closed']
                release.set()
                with pytest.raises(SnapshotMaintenanceError, match='unknown') as raised:
                    await task
                assert raised.value.result.kind is DbOutcomeKind.UNKNOWN
                assert raised.value.result.error is failure and raised.value.operation_key is not None
                assert state['closed'] and all(not path.exists() for path in paths)
                row = owned.connection.execute(
                    'SELECT s.content, s.change_count, p.snapshot_change_count'
                    ' FROM entity_snapshots s JOIN entity_snapshot_progress p'
                    ' ON p.latest_snapshot_id=s.id WHERE s.entity_type=1 AND s.entity_id=?',
                    (ref.action_id,)).fetchone()
                assert row is not None and row[0] == expected_content and row[1] == row[2]
                assert history.current_boundary() == boundary
            finally:
                release.set()
                if not task.done():
                    try:
                        await task
                    except (asyncio.CancelledError, SnapshotMaintenanceError):
                        pass
                await executor.close()
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_cancel_during_preparation_waits_for_source_owner_to_finish(tmp_path,monkeypatch):
    from camctl.history import snapshots as module
    owned,_,_,ref,history=await _fixture(tmp_path)
    store,executor=await _snapshot_store(history)
    async def candidates(*_):return (module.SnapshotRef(1,ref.action_id),)
    monkeypatch.setattr(store,'snapshot_candidates',candidates)
    loop=asyncio.get_running_loop();started=asyncio.Event();release=Event();images=[];prepared=[]
    original=module.prepare_snapshot
    def prepare(image):
        images.append((image,Path(image._temporary.name)))
        value=original(image);prepared.append(value)
        loop.call_soon_threadsafe(started.set)
        assert release.wait(60),'preparation owner gate was not released'
        return value
    monkeypatch.setattr(module,'prepare_snapshot',prepare)
    task=asyncio.create_task(module.maintain_snapshots(module.MaintenanceContext(store=store,state=module.MaintenanceState(),threshold=1,batch_size=2)))
    try:
        await asyncio.wait_for(started.wait(),60);task.cancel()
        delivered=loop.create_future();loop.call_soon(delivered.set_result,None);await delivered
        task.cancel()
        assert images[0][1].exists() and prepared[0].path.exists() and not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):await task
        assert all(not path.exists() and image._db is None for image,path in images)
        assert all(not item.path.exists() for item in prepared)
        assert owned.connection.execute('SELECT COUNT(*) FROM entity_snapshots').fetchone()[0]==0
    finally:
        release.set()
        if not task.done():
            try:await task
            except asyncio.CancelledError:pass
        await executor.close();owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['missing_event', 'query_error'])
async def test_forward_delta_missing_evidence_never_returns_a_partial_handle(tmp_path, monkeypatch, failure):
    import sqlite3
    import tempfile

    owned, runtime, repo, ref, history, before, prepared = await _before_assessment(tmp_path)
    temporary = tmp_path / 'history-temporary'
    temporary.mkdir()
    real_connect = sqlite3.connect
    target_uri = history.path.resolve().as_uri() + '?mode=ro'
    failed = []
    derived = []
    query_error = sqlite3.OperationalError('forward page read failed')

    class ReadConnection(sqlite3.Connection):
        def execute(self, statement, *args, **kwargs):
            reads = set()

            def authorize(action, table, column, database, source):
                if action == sqlite3.SQLITE_READ:
                    reads.add((table, column))
                return sqlite3.SQLITE_OK

            self.set_authorizer(authorize)
            try:
                cursor = super().execute(statement, *args, **kwargs)
            finally:
                self.set_authorizer(None)
            if (failure == 'query_error' and
                    {('entity_event_links', 'change_count'), ('history_events', 'body_json')} <= reads):
                cursor.close()
                failed.append(True)
                raise query_error
            return cursor

    class DerivedConnection(sqlite3.Connection):
        def close(self):
            super().close()
            self.was_closed = True

    def connect(database, *args, **kwargs):
        if str(database) == target_uri:
            assert 'factory' not in kwargs
            kwargs['factory'] = ReadConnection
            return real_connect(database, *args, **kwargs)
        path = Path(database) if isinstance(database, (str, Path)) else None
        if path is not None and path.is_absolute() and path.is_relative_to(temporary):
            assert 'factory' not in kwargs
            kwargs['factory'] = DerivedConnection
            connection = real_connect(database, *args, **kwargs)
            connection.was_closed = False
            derived.append((connection, path, path.is_file()))
            return connection
        return real_connect(database, *args, **kwargs)

    try:
        if failure == 'missing_event':
            from .test_record_segment_history import _version
            _version(owned, runtime, repo, ref, 2)
        boundary = history.current_boundary()
        if failure == 'missing_event':
            event = owned.connection.execute(
                'SELECT MIN(event_id) FROM entity_event_links WHERE entity_type=? AND entity_id=?'
                ' AND event_id>? AND event_id<?',
                (prepared.entity_type, ref.action_id, before.last_event_id, boundary.last_event_id),
            ).fetchone()[0]
            assert event is not None
            owned.connection.execute(
                'DELETE FROM entity_event_links WHERE entity_type=? AND entity_id=? AND event_id=?',
                (prepared.entity_type, ref.action_id, event),
            )
        monkeypatch.setattr(tempfile, 'tempdir', str(temporary))
        monkeypatch.setattr(sqlite3, 'connect', connect)
        with pytest.raises(ConsistencyError if failure == 'missing_event' else sqlite3.OperationalError) as raised:
            history.open_segment_history(ref.action_id, boundary, strategy='snapshot', batch_size=1)
        if failure == 'query_error':
            assert failed == [True] and raised.value is query_error
        assert derived and all(
            existed and connection.was_closed and not path.exists() and not path.parent.exists()
            for connection, path, existed in derived
        )
        assert history.current_boundary() == boundary
    finally:
        prepared.close()
        owned.connection.close()


@pytest.mark.asyncio
async def test_restore_keeps_original_s_when_business_commits_between_source_pages(tmp_path,monkeypatch):
    from contextlib import contextmanager
    from camctl.history.snapshots import SnapshotRef
    from camctl.persistence.runtime import open_existing,DbOpenMode,DbConfig
    from .test_record_segment_history import _version,_rows
    owned,runtime,repo,ref,history=await _fixture(tmp_path)
    store,executor=await _snapshot_store(history)
    calls=0;committed=[];original=HistoryRepository._read_connection
    @contextmanager
    def changing(self):
        nonlocal calls
        with original(self) as connection:yield connection
        calls+=1
        if calls==4:
            writer=open_existing(history.path,DbOpenMode.EXISTING_RW,DbConfig())
            try:
                committed.append(_version(writer,runtime,repo,ref,2))
            finally:writer.connection.close()
    monkeypatch.setattr(HistoryRepository,'_read_connection',changing)
    images=()
    try:
        images=await store.load_entity_images((SnapshotRef(1,ref.action_id),))
        assert committed and calls>4
        assert [row.values['version'] for row in _rows(images[0],'segment_sets')]==[1]
        assert owned.connection.execute('SELECT COUNT(*) FROM segment_sets').fetchone()[0]==2
    finally:
        for image in images:image.close()
        await executor.close();owned.connection.close()


@pytest.mark.asyncio
async def test_cancel_after_job_end_before_delivery_still_receives_original_result(tmp_path,monkeypatch):
    from camctl.history.snapshots import MaintenanceContext,MaintenanceState,SnapshotRef,maintain_snapshots
    from camctl.persistence.models import DbJob,DbJobKind,DbPriority
    owned,_,_,ref,history=await _fixture(tmp_path)
    boundary=history.current_boundary()
    store,executor=await _snapshot_store(history)
    async def candidates(*_):return (SnapshotRef(1,ref.action_id),)
    monkeypatch.setattr(store,'snapshot_candidates',candidates)
    loop=asyncio.get_running_loop();delayed=asyncio.Event();held=[];paths=[];keys=[];receipts=[]
    original_save=store.save_snapshots
    async def save(prepared):
        paths.extend(item.path for item in prepared)
        return await original_save(prepared)
    monkeypatch.setattr(store,'save_snapshots',save)
    original_submit=executor.submit_write
    def submit(job):
        keys.append(job.key)
        result=original_submit(job);receipts.append(result)
        return result
    monkeypatch.setattr(executor,'submit_write',submit)
    original_schedule=loop.call_soon_threadsafe
    def schedule(callback,*args,**kwargs):
        # 在公开事件循环的线程交付边界保留原回执，不替换执行器结果。
        if args and isinstance(args[0],DbJob) and args[0].key in keys:
            held.append((callback,args,kwargs))
            return original_schedule(delayed.set)
        return original_schedule(callback,*args,**kwargs)
    monkeypatch.setattr(loop,'call_soon_threadsafe',schedule)
    def deliver():
        for callback,args,kwargs in held:
            original_schedule(callback,*args,**kwargs)
        held.clear()
    task=asyncio.create_task(maintain_snapshots(MaintenanceContext(store=store,state=MaintenanceState(),threshold=1,batch_size=2)))
    try:
        await asyncio.wait_for(delayed.wait(),60)
        # 同一真实工作线程已执行后续读取，证明保存及COMMIT已实际结束。
        later=executor.submit_read(DbJob(new_operation_key(),'confirm saved snapshot',DbJobKind.READ,DbPriority.BUSINESS,
            lambda actual:actual.connection.execute('SELECT COUNT(*) FROM entity_snapshots').fetchone()[0]))
        assert (await later).value==1
        assert receipts and not receipts[0].done()
        task.cancel()
        delivered=loop.create_future();loop.call_soon(delivered.set_result,None);await delivered
        assert paths[0].exists() and not task.done()
        deliver()
        with pytest.raises(asyncio.CancelledError):await task
        assert receipts[0].done() and receipts[0].result().kind.value=='completed'
        assert not paths[0].exists()
        assert owned.connection.execute('SELECT COUNT(*) FROM entity_snapshots').fetchone()[0]==1
        assert history.current_boundary()==boundary
    finally:
        deliver()
        if not task.done():
            try:await task
            except asyncio.CancelledError:pass
        await executor.close();owned.connection.close()


async def _stream_snapshot(tmp_path):
    owned,_,_,ref,history=await _fixture(tmp_path)
    boundary=history.current_boundary()
    with history.open_segment_history(ref.action_id,boundary) as handle:
        prepared=prepare_snapshot(handle)
    try:
        await _save(history,prepared)
        content=prepared.path.read_bytes()
    finally:
        prepared.close()
    with history.open_segment_history(ref.action_id,boundary,strategy='snapshot'):
        pass
    return owned,ref,history,boundary,content


def _replace_snapshot_body(owned,content):
    owned.connection.execute('UPDATE entity_snapshots SET content=?',(content,))


@pytest.mark.asyncio
async def test_unframed_snapshot_body_has_fixed_decoder_residency(tmp_path):
    import gc,tracemalloc
    owned,ref,history,boundary,content=await _stream_snapshot(tmp_path)
    header=content.split(b'\n',1)[0]+b'\n';chunk=b'x'*65536;evidence=[]
    try:
        for count in (4,64):
            _replace_snapshot_body(owned,header+chunk*count)
            gc.collect();tracemalloc.start()
            try:
                with pytest.raises(ConsistencyError) as raised:
                    history.open_segment_history(ref.action_id,boundary,strategy='snapshot')
                assert '结尾换行' in str(raised.value.__cause__)
                peak=tracemalloc.get_traced_memory()[1]
            finally:tracemalloc.stop()
            evidence.append((count*len(chunk),peak))
        print('UNFRAMED_RESIDENCY',evidence)
        assert evidence[-1][1]<512000
        assert evidence[-1][1]<evidence[0][1]+200000
    finally:owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',[b'{"x":1,"\\u0078":2}\n',b'{"x":"\\ud800"}\n',b'{"x":01}\n',b'{"x":NaN}\n',b'{"x":"\xf0\x9f\n',b'{"x":1}\n{"y":'])
async def test_framed_snapshot_keeps_exact_json_and_utf8_rejection(tmp_path,bad):
    owned,ref,history,boundary,content=await _stream_snapshot(tmp_path)
    try:
        # 合法前缀来自真实保存，末分区保留合法行之后的截断值。
        invalid=bad.split(b'\n',1)[1] if bad.startswith(b'{"x":1}\n') else bad
        _replace_snapshot_body(owned,content+invalid)
        with pytest.raises(ConsistencyError) as raised:
            history.open_segment_history(ref.action_id,boundary,strategy='snapshot')
        from camctl.history.snapshots import SnapshotMaintenanceError
        assert isinstance(raised.value.__cause__,SnapshotMaintenanceError)
    finally:owned.connection.close()


def _large_snapshot_record(content):
    from decimal import Decimal
    from camctl.history.snapshots import CHUNK_BYTES
    records=[parse_exact_json(line) for line in content.decode().splitlines()]
    action=next(record for record in records if record.get('table')=='actions')
    probe={'a_padding':'','emoji':'😀','exact':Decimal('1.234567890123456789'),'n':9007199254740993,'text':'x'*(8*65536)}
    action['row']['effective_params_json']={**action['row']['effective_params_json'],'stream_probe':probe}
    def encode():return b''.join((encode_json_value(record)+'\n').encode() for record in records)
    first=encode();offset=first.index('😀'.encode())
    probe['a_padding']='p'*((CHUNK_BYTES-1-offset)%CHUNK_BYTES)
    body=encode()
    assert body.index('😀'.encode())%CHUNK_BYTES==CHUNK_BYTES-1
    return body


@pytest.mark.asyncio
async def test_snapshot_accepts_necessary_large_row_and_split_exact_values(tmp_path):
    import tracemalloc
    from decimal import Decimal
    owned,ref,history,boundary,content=await _stream_snapshot(tmp_path)
    try:
        _replace_snapshot_body(owned,_large_snapshot_record(content))
        tracemalloc.start()
        try:
            with history.open_segment_history(ref.action_id,boundary,strategy='snapshot') as handle:
                row=handle.primary()['effective_params_json']['stream_probe']
                assert len(row['text'])==8*65536 and row['text'].startswith('x') and row['text'].endswith('x')
                assert row['emoji']=='😀' and row['exact']==Decimal('1.234567890123456789')
                assert row['n']==9007199254740993
            print('NECESSARY_SINGLE_ROW',8*65536,tracemalloc.get_traced_memory()[1])
        finally:tracemalloc.stop()
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_framed_invalid_large_row_is_rejected_as_one_record(tmp_path):
    import tracemalloc
    owned,ref,history,boundary,content=await _stream_snapshot(tmp_path)
    try:
        records=_large_snapshot_record(content).splitlines(keepends=True)
        index=next(index for index,record in enumerate(records) if b'stream_probe' in record)
        records[index]=records[index][:-2]+b'!}\n'
        _replace_snapshot_body(owned,b''.join(records))
        tracemalloc.start()
        try:
            with pytest.raises(ConsistencyError) as raised:
                history.open_segment_history(ref.action_id,boundary,strategy='snapshot')
            assert '增量解码' in str(raised.value.__cause__)
            print('INVALID_FRAMED_SINGLE_ROW',8*65536,tracemalloc.get_traced_memory()[1])
        finally:tracemalloc.stop()
    finally:owned.connection.close()


@pytest.mark.parametrize('failure',['write','read','truncate','header'])
@pytest.mark.asyncio
async def test_snapshot_line_tempfile_failure_or_upper_validation_closes_all_resources(tmp_path,monkeypatch,failure):
    import errno
    import sqlite3
    import tempfile
    from camctl.history import snapshot_stream as module
    owned,_,_,ref,history=await _fixture(tmp_path)
    boundary=history.current_boundary()
    with history.open_segment_history(ref.action_id,boundary) as handle:prepared=prepare_snapshot(handle)
    temporary=tmp_path/'history-temporary'
    temporary.mkdir()
    opened=[];derived=[];original_file=module.TemporaryFile;real_connect=sqlite3.connect
    class DerivedConnection(sqlite3.Connection):
        def close(self):
            super().close()
            self.was_closed=True
    def connect(database,*args,**kwargs):
        path=Path(database) if isinstance(database,(str,Path)) else None
        if path is not None and path.is_absolute() and path.is_relative_to(temporary):
            assert 'factory' not in kwargs
            connection=real_connect(database,*args,factory=DerivedConnection,**kwargs)
            connection.was_closed=False
            derived.append((connection,path,path.is_file()))
            return connection
        return real_connect(database,*args,**kwargs)
    class FailingRowFile:
        def __init__(self,inner):self.inner=inner;self.calls={'write':0,'read':0,'truncate':0}
        def __enter__(self):return self
        def __exit__(self,*_):self.inner.close()
        def __getattr__(self,name):return getattr(self.inner,name)
        def _call(self,name,*args):
            self.calls[name]+=1
            if name==failure and self.calls[name]==2:raise OSError(errno.ENOSPC,'row spool full')
            return getattr(self.inner,name)(*args)
        def write(self,*args):return self._call('write',*args)
        def read(self,*args):return self._call('read',*args)
        def truncate(self,*args):return self._call('truncate',*args)
    def create(*args,**kwargs):
        value=FailingRowFile(original_file(*args,**kwargs));opened.append(value);return value
    try:
        await _save(history,prepared)
        if failure=='header':_change_snapshot(owned,prepared,lambda records:records[0].update(entity_id=999))
        monkeypatch.setattr(tempfile,'tempdir',str(temporary))
        monkeypatch.setattr(sqlite3,'connect',connect)
        monkeypatch.setattr(module,'TemporaryFile',create)
        with pytest.raises(ConsistencyError if failure=='header' else OSError):
            history.open_segment_history(ref.action_id,boundary,strategy='snapshot',batch_size=2)
        assert opened and all(value.inner.closed for value in opened)
        assert derived and all(existed and connection.was_closed and not path.exists() and not path.parent.exists()
                               for connection,path,existed in derived)
        assert history.current_boundary()==boundary
    finally:prepared.close();owned.connection.close()


@pytest.mark.asyncio
async def test_actual_unframed_blob_never_reads_a_whole_row_and_closes_tempfile(tmp_path,monkeypatch):
    from camctl.history import snapshot_stream as module
    owned,_,_,ref,history=await _fixture(tmp_path)
    boundary=history.current_boundary()
    with history.open_segment_history(ref.action_id,boundary) as handle:prepared=prepare_snapshot(handle)
    original=module.TemporaryFile;opened=[];reads=[]
    class ObservedFile:
        def __init__(self,inner):self.inner=inner
        def __enter__(self):return self
        def __exit__(self,*_):self.inner.close()
        def __getattr__(self,name):return getattr(self.inner,name)
        def read(self,*args):reads.append(self.inner.tell());return self.inner.read(*args)
    def create(*args,**kwargs):
        value=ObservedFile(original(*args,**kwargs));opened.append(value);return value
    try:
        await _save(history,prepared)
        with prepared.path.open('rb') as source:header=source.readline()
        identity=owned.connection.execute('SELECT id FROM entity_snapshots').fetchone()[0]
        chunk=b'x'*65536
        owned.connection.execute('UPDATE entity_snapshots SET content=zeroblob(?) WHERE id=?',(len(header)+64*len(chunk),identity))
        with owned.connection.blobopen('entity_snapshots','content',identity) as blob:
            blob.write(header)
            for _ in range(64):blob.write(chunk)
        monkeypatch.setattr(module,'TemporaryFile',create)
        with pytest.raises(ConsistencyError):history.open_segment_history(ref.action_id,boundary,strategy='snapshot',batch_size=2)
        assert len(reads)==1  # 只有可靠LF结束的有限头行被回读。
        assert opened and all(value.inner.closed for value in opened)
    finally:prepared.close();owned.connection.close()


@pytest.mark.asyncio
async def test_snapshot_null_and_acceptance_member_count_use_s(tmp_path):
    from ..persistence.test_segment_storage import _world,_ok
    owned,runtime,repo,ref,members=await _world(tmp_path,indexes=(1,2))
    _ok(repo.append_members(ref,members,runtime.wall_us(),new_operation_key(),owned))
    history=HistoryRepository(owned.connection.execute('PRAGMA database_list').fetchone()[2])
    try:
        before=history.current_boundary()
        with history.open_segment_history(ref.action_id,before) as handle:prepared=prepare_snapshot(handle)
        try:
            await _save(history,prepared)
            _ok(repo.fix_members(ref,runtime.wall_us(),new_operation_key(),owned))
            with history.open_segment_history(ref.action_id,before,strategy='snapshot',batch_size=1) as handle:
                assert handle.read_set(ref)['member_count'] is None
            def change(records):
                next(row['row'] for row in records if row.get('table')=='segment_sets').update(state=2,member_count=2)
            _change_snapshot(owned,prepared,change)
            assert not _snapshot_matches_initial(owned,history,ref,prepared)
        finally:prepared.close()
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_snapshot_acceptance_compares_other_owned_write_once_values(tmp_path):
    owned,_,_,ref,history=await _fixture(tmp_path)
    try:
        boundary=history.current_boundary()
        with history.open_segment_history(ref.action_id,boundary) as handle:prepared=prepare_snapshot(handle)
        try:
            await _save(history,prepared)
            def change(records):
                target=next(row['row'] for row in records if row.get('table')=='operation_attempts' and row['row']['result_last_page_event_id'] is not None)
                target.update(result_first_page_event_id=None,result_last_page_event_id=None)
            _change_snapshot(owned,prepared,change)
            assert not _snapshot_matches_initial(owned,history,ref,prepared)
        finally:prepared.close()
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_snapshot_decodes_only_forward_s_to_h_interval_once(tmp_path,monkeypatch):
    from camctl.persistence.repositories import history as module
    from .test_record_segment_history import _version,_rows
    owned,runtime,repo,ref,history,before,prepared=await _before_assessment(tmp_path)
    try:
        boundary=history.current_boundary()
        _version(owned,runtime,repo,ref,2)
        count=owned.connection.execute('SELECT change_count FROM entity_event_links WHERE entity_type=? AND entity_id=? AND event_id<=? ORDER BY event_id DESC LIMIT 1',
            (prepared.entity_type,ref.action_id,boundary.last_event_id)).fetchone()[0]
        original=module.decode_event_row;previous=None;decoded=0
        def decode(*args,**kwargs):
            nonlocal previous,decoded
            value=original(*args,**kwargs)
            assert previous is None or value.event_id>previous
            assert before.last_event_id<value.event_id<=boundary.last_event_id
            previous=value.event_id;decoded+=1
            return value
        monkeypatch.setattr(module,'decode_event_row',decode)
        with history.open_segment_history(ref.action_id,boundary,strategy='snapshot',batch_size=1) as handle:
            assert decoded==handle.stats['events']==count-prepared.change_count
            assert handle.read_set(ref)['assessment_json'] is not None
            assert [row.values['version'] for row in _rows(handle,'segment_sets',limit=1)]==[1]
    finally:prepared.close();owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('storage',['rows','validator'])
async def test_private_rebuildable_storage_keeps_statement_rollback_and_source_pragmas(tmp_path,storage):
    import sqlite3
    from camctl.persistence.row_validation import ProjectionRowValidator,ProjectionRowValidationError
    owned,_,_,ref,history=await _fixture(tmp_path)
    def pragmas(connection):
        return tuple(connection.execute('PRAGMA '+name).fetchone()[0]
            for name in ('journal_mode','synchronous','cache_size','temp_store'))
    before=pragmas(owned.connection)
    try:
        with history.open_segment_history(ref.action_id,history.current_boundary()) as handle:
            primary=handle.primary()
            if storage=='rows':
                assert pragmas(handle._db)==('persist',0,-2048,1)
                handle._db.execute('CREATE TABLE statement_check(id INTEGER PRIMARY KEY)')
                handle._db.execute('INSERT INTO statement_check VALUES (1)')
                with pytest.raises(sqlite3.IntegrityError):handle._db.execute('INSERT INTO statement_check VALUES (1)')
                assert handle._db.execute('SELECT COUNT(*) FROM statement_check').fetchone()[0]==1
                handle.detach()
                assert handle.primary()==primary
                assert pragmas(handle._db)==('persist',0,-2048,1)
            else:
                with ProjectionRowValidator(storage_path=tmp_path/'private-validation.db') as validator:
                    assert pragmas(validator._connection)==('persist',0,-2048,1)
                    invalid=dict(primary,status=999)
                    with pytest.raises(ProjectionRowValidationError):validator.validate('actions',invalid)
                    assert validator._connection.execute('SELECT COUNT(*) FROM actions').fetchone()[0]==0
                    validator.validate('actions',primary)
                    assert validator._connection.execute('SELECT COUNT(*) FROM actions').fetchone()[0]==0
        assert not Path(handle._temporary.name).exists()
        assert pragmas(owned.connection)==before
    finally:owned.connection.close()

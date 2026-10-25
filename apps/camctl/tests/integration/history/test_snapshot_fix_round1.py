"""P3b第一轮：原固定事实、受管线程和取消来源的独立回归。"""
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
    _assert_same_s_snapshot_bytes,
    _fixture,
    _save,
)


def _change_snapshot(owned, prepared, transform):
    records=[parse_exact_json(line) for line in prepared.path.read_text().splitlines()]
    transform(records)
    owned.connection.execute('UPDATE entity_snapshots SET content=?',
        ((''.join(encode_json_value(record)+'\n' for record in records)).encode(),))


@pytest.mark.asyncio
@pytest.mark.parametrize('damage',['assessment_null','member_count_null','marker_null','marker_scope_null'])
async def test_snapshot_cannot_erase_write_once_assignment(tmp_path,monkeypatch,damage):
    from camctl.history.bounded import SegmentHistoryHandle
    if damage.startswith('marker'):
        from ..persistence.test_segment_storage import _assessed_world,_completion_command
        from camctl.capture.segment_models import DefaultScopeState
        from camctl.persistence.transaction import commit_operation
        owned,runtime,repo,ref,members=await _assessed_world(tmp_path,DefaultScopeState.NONEMPTY)
        key=new_operation_key()
        result=commit_operation(_completion_command(ref,members,runtime.wall_us(),key),key,owned)
        assert result.kind == 'completed',result.error
        history=HistoryRepository(owned.connection.execute('PRAGMA database_list').fetchone()[2])
    else:
        owned,runtime,repo,ref,history=await _fixture(tmp_path)
    try:
        boundary=history.current_boundary()
        with history.open_segment_history(ref.action_id,boundary) as handle:
            prepared=prepare_snapshot(handle)
        try:
            await _save(history,prepared)
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
            opened=[]
            original=SegmentHistoryHandle.__init__
            def capture(self,*args,**kwargs):
                original(self,*args,**kwargs)
                opened.append((self,Path(self._temporary.name)))
            monkeypatch.setattr(SegmentHistoryHandle,'__init__',capture)
            with pytest.raises(ConsistencyError):
                history.open_segment_history(ref.action_id,boundary,strategy='snapshot',batch_size=2)
            assert opened and all(handle._db is None and not path.exists() for handle,path in opened)
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
async def test_snapshot_cannot_borrow_a_future_assessment_equal_to_current(tmp_path):
    owned,runtime,repo,ref,history,before,prepared=await _before_assessment(tmp_path)
    try:
        current=repo.read_set(ref,owned)
        def change(records):
            saved=next(row['row'] for row in records if row.get('table')=='segment_sets')
            saved.update(state=3,facts_json=current['facts_json'],assessment_json=current['assessment_json'])
        _change_snapshot(owned,prepared,change)
        with pytest.raises(ConsistencyError):
            history.open_segment_history(ref.action_id,before,strategy='snapshot',batch_size=1)
    finally:
        prepared.close();owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('future',['marker_and_outputs','outputs_only'])
async def test_snapshot_cannot_borrow_future_publication_values(tmp_path,future):
    from ..persistence.test_segment_storage import _assessed_world,_completion_command
    from camctl.capture.segment_models import DefaultScopeState
    from camctl.persistence.transaction import commit_operation
    owned,runtime,repo,ref,members=await _assessed_world(tmp_path,DefaultScopeState.NONEMPTY)
    history=HistoryRepository(owned.connection.execute('PRAGMA database_list').fetchone()[2])
    try:
        before=history.current_boundary()
        with history.open_segment_history(ref.action_id,before) as handle:prepared=prepare_snapshot(handle)
        try:
            await _save(history,prepared)
            key=new_operation_key()
            result=commit_operation(_completion_command(ref,members,runtime.wall_us(),key),key,owned)
            assert result.kind=='completed',result.error
            marker=repo.read_set(ref,owned)['published_event_id']
            outputs=dict(owned.connection.execute('SELECT id,output_id FROM segment_scope_members'))
            def change(records):
                for record in records:
                    if record.get('table')=='segment_sets' and future=='marker_and_outputs':record['row']['published_event_id']=marker
                    elif record.get('table')=='segment_scope_members':record['row']['output_id']=outputs[record['row']['id']]
            _change_snapshot(owned,prepared,change)
            with pytest.raises(ConsistencyError):history.open_segment_history(ref.action_id,before,strategy='snapshot',batch_size=1)
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
@pytest.mark.parametrize('phase',['first','middle','later_source','rollback'])
async def test_cancel_started_save_keeps_source_until_actual_completion(tmp_path,monkeypatch,phase):
    from camctl.history.snapshots import (MaintenanceContext,MaintenanceState,SnapshotRef,maintain_snapshots)
    from camctl.persistence.repositories import history as module
    owned,runtime,repo,ref,history=await _fixture(tmp_path,220 if phase=='middle' else 5)
    boundary=history.current_boundary()
    store,executor=await _snapshot_store(history)
    selected=False
    async def candidates(*_):
        nonlocal selected
        if selected:return ()
        selected=True
        if phase=='later_source':
            from camctl.contracts.enums import load_registry
            file_kind=load_registry()['history_objects']['device_file']['id']
            file_id=owned.connection.execute('SELECT MIN(id) FROM device_files').fetchone()[0]
            return (SnapshotRef(file_kind,file_id),SnapshotRef(1,ref.action_id))
        return (SnapshotRef(1,ref.action_id),)
    monkeypatch.setattr(store,'snapshot_candidates',candidates)
    loop=asyncio.get_running_loop();started=asyncio.Event();release=Event();paths=[];actual=[]
    from camctl.history import snapshots as snapshots_module
    original_prepare=snapshots_module.prepare_snapshot
    def prepare(image):
        item=original_prepare(image)
        if hasattr(item,'path'):paths.append(item.path)
        return item
    monkeypatch.setattr(snapshots_module,'prepare_snapshot',prepare)
    if phase=='middle':
        from camctl.history.snapshot_stream import PreparedSnapshotSource
        original_chunks=PreparedSnapshotSource.chunks
        chunks=original_chunks.fget
        def middle(self):
            assert self.length>65536
            for index,chunk in enumerate(chunks(self)):
                yield chunk
                if index==0:
                    loop.call_soon_threadsafe(started.set)
                    assert release.wait(60),'middle chunk gate was not released'
        monkeypatch.setattr(PreparedSnapshotSource,'chunks',property(middle))
    original=module._save_one_snapshot
    calls=0
    def save(connection,item):
        nonlocal calls
        calls+=1
        if phase!='middle' and (phase!='later_source' or calls==2):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(60),'test gate was not released'
        try:
            value=original(connection,item)
            if phase=='rollback':
                import sqlite3
                raise sqlite3.OperationalError('test write failure before commit')
            actual.append('completed');return value
        except BaseException as error:
            actual.append(type(error).__name__);raise
    monkeypatch.setattr(module,'_save_one_snapshot',save)
    task=asyncio.create_task(maintain_snapshots(MaintenanceContext(store=store,state=MaintenanceState(),threshold=1,batch_size=3)))
    try:
        await asyncio.wait_for(started.wait(),60)
        task.cancel()
        delivered=loop.create_future();loop.call_soon(delivered.set_result,None)
        await delivered
        assert paths and all(path.exists() for path in paths)
        assert not task.done()
        task.cancel()
        release.set()
        if phase=='rollback':
            from camctl.history.snapshots import SnapshotMaintenanceError
            with pytest.raises(SnapshotMaintenanceError,match='rolled_back'):await task
        else:
            with pytest.raises(asyncio.CancelledError):await task
        await executor.close()
        assert actual==(['OperationalError'] if phase=='rollback' else ['completed']*(2 if phase=='later_source' else 1))
        assert all(not path.exists() for path in paths)
        assert owned.connection.execute('SELECT COUNT(*) FROM entity_snapshots').fetchone()[0]==(0 if phase=='rollback' else 2 if phase=='later_source' else 1)
        assert history.current_boundary()==boundary, '取消后的实际保存不得推进业务历史'
        if phase=='middle':
            # 实际保存及来源清理完成后，从相同 H 的当前投影独立准备同 S 内容。
            monkeypatch.setattr(module,'_save_one_snapshot',original)
            monkeypatch.setattr(PreparedSnapshotSource,'chunks',original_chunks)
            monkeypatch.setattr(snapshots_module,'prepare_snapshot',original_prepare)
            await _assert_same_s_snapshot_bytes(
                owned,runtime,repo,ref,history,boundary,paths)
    finally:
        release.set()
        if not task.done():
            try:await task
            except asyncio.CancelledError:pass
        await executor.close();owned.connection.close()


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
    from camctl.persistence.models import DbPriority
    owned,_,_,ref,history=await _fixture(tmp_path)
    store,executor=await _snapshot_store(history)
    release,blocked=await _block_business(executor)
    admitted=asyncio.Event();restored=[]
    original_submit=executor.submit_read
    def submit(job):
        result=original_submit(job)
        if job.priority is DbPriority.SNAPSHOT:admitted.set()
        return result
    monkeypatch.setattr(executor,'submit_read',submit)
    monkeypatch.setattr(HistoryRepository,'open_segment_history',lambda *a,**k:restored.append(True))
    task=asyncio.create_task(store.load_entity_images((SnapshotRef(1,ref.action_id),)))
    try:
        await asyncio.wait_for(admitted.wait(),60);task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        release.set();await blocked
        await executor.close()
        assert restored==[]
        assert not executor._results
    finally:
        release.set();await blocked
        await executor.close();owned.connection.close()


@pytest.mark.asyncio
async def test_cancel_started_load_receives_and_closes_late_detached_handle(tmp_path,monkeypatch):
    from camctl.history.snapshots import SnapshotRef
    from camctl.history.bounded import SegmentHistoryHandle
    owned,_,_,ref,history=await _fixture(tmp_path)
    store,executor=await _snapshot_store(history)
    loop=asyncio.get_running_loop();started=asyncio.Event();release=Event();opened=[]
    original=HistoryRepository.open_segment_history
    def restore(self,*args,**kwargs):
        handle=original(self,*args,**kwargs)
        opened.append((handle,Path(handle._temporary.name)))
        loop.call_soon_threadsafe(started.set)
        assert release.wait(60),'load gate was not released'
        return handle
    monkeypatch.setattr(HistoryRepository,'open_segment_history',restore)
    task=asyncio.create_task(store.load_entity_images((SnapshotRef(1,ref.action_id),)))
    try:
        await asyncio.wait_for(started.wait(),60);task.cancel()
        delivered=loop.create_future();loop.call_soon(delivered.set_result,None);await delivered
        task.cancel()
        assert opened[0][1].exists() and not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):await task
        assert all(handle._db is None and not directory.exists() for handle,directory in opened)
        assert not executor._results
    finally:
        release.set()
        if not task.done():
            try:await task
            except asyncio.CancelledError:pass
        await executor.close();owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('waiting_admission',[False,True])
async def test_cancel_queued_save_confirms_withdrawal_before_closing_sources(tmp_path,monkeypatch,waiting_admission):
    from camctl.history.snapshots import MaintenanceContext,MaintenanceState,SnapshotRef,maintain_snapshots
    from camctl.persistence.repositories import history as history_module
    owned,_,_,ref,history=await _fixture(tmp_path)
    store,executor=await _snapshot_store(history)
    async def candidates(*_):return (SnapshotRef(1,ref.action_id),)
    monkeypatch.setattr(store,'snapshot_candidates',candidates)
    blockers=[];paths=[];admitted=asyncio.Event();executed=[];queued_business=[];timers=[];receipts=[]
    original_save=store.save_snapshots
    async def queued(prepared):
        paths.extend(item.path for item in prepared if hasattr(item,'path'))
        blockers.append(await _block_business(executor))
        if waiting_admission:
            from camctl.persistence.models import DbJob,DbJobKind,DbPriority
            for _ in range(executor._core._capacity):
                queued_business.append(executor.submit_read(DbJob(new_operation_key(),'fill actual queue',DbJobKind.READ,DbPriority.BUSINESS,lambda _:None)))
        return await original_save(prepared)
    monkeypatch.setattr(store,'save_snapshots',queued)
    original_submit=executor.submit_write
    def submit(job):
        before=asyncio.all_tasks()
        future=original_submit(job);receipts.append(future)
        if waiting_admission:
            assert any(item.job.key==job.key for item in executor._core._admissions)
            timers.extend(asyncio.all_tasks()-before)
        admitted.set();return future
    monkeypatch.setattr(executor,'submit_write',submit)
    monkeypatch.setattr(history_module,'_save_one_snapshot',lambda *a:executed.append(True))
    task=asyncio.create_task(maintain_snapshots(MaintenanceContext(store=store,state=MaintenanceState(),threshold=1,batch_size=2)))
    try:
        await asyncio.wait_for(admitted.wait(),60)
        assert all(path.exists() for path in paths)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert all(not path.exists() for path in paths)
        assert receipts and receipts[0].done() and receipts[0].result().kind.value=='not_executed'
        blockers[0][0].set();await blockers[0][1]
        await asyncio.gather(*queued_business)
        await executor.close()
        # 已撤回操作可靠NOT_EXECUTED；既有executor拥有的准入计时监督
        # 按原截止自行结束，测试只等待该真实Task，不修改执行器。
        await asyncio.gather(*timers)
        assert executed==[] and not executor._results
        assert owned.connection.execute('SELECT COUNT(*) FROM entity_snapshots').fetchone()[0]==0
    finally:
        for release,future in blockers:release.set();await future
        if not task.done():
            try:await task
            except asyncio.CancelledError:pass
        await executor.close()
        await asyncio.gather(*timers,return_exceptions=True)
        owned.connection.close()


@pytest.mark.asyncio
async def test_cancel_commit_unknown_waits_for_connection_cleanup_and_preserves_classification(tmp_path,monkeypatch):
    import sqlite3
    from camctl.history.snapshots import MaintenanceContext,MaintenanceState,SnapshotRef,SnapshotMaintenanceError,maintain_snapshots
    from camctl.persistence.repositories import history as module
    from camctl.persistence.runtime import open_existing,DbOpenMode,DbConfig,OwnedConnection
    from camctl.persistence.executor import DbExecutor
    owned,_,_,ref,history=await _fixture(tmp_path)
    loop=asyncio.get_running_loop();closing=asyncio.Event();release=Event();paths=[];state={'saving':False,'closed':False}
    class UnknownCommitConnection:
        def __init__(self,inner):self.inner=inner
        def execute(self,statement,*args):
            result=self.inner.execute(statement,*args)
            if statement=='COMMIT' and state['saving']:
                result.close()
                raise sqlite3.OperationalError('commit acknowledgement lost')
            return result
        def close(self):
            loop.call_soon_threadsafe(closing.set)
            assert release.wait(60),'connection cleanup gate was not released'
            self.inner.close();state['closed']=True
        def __getattr__(self,name):return getattr(self.inner,name)
    def factory():
        actual=open_existing(history.path,DbOpenMode.EXISTING_RW,DbConfig())
        return OwnedConnection(UnknownCommitConnection(actual.connection),actual.metadata)
    executor=DbExecutor(factory,capacity=8,enqueue_timeout_seconds=9)
    store=module.SqliteSnapshotStore(history.path,executor)
    async def candidates(*_):return (SnapshotRef(1,ref.action_id),)
    monkeypatch.setattr(store,'snapshot_candidates',candidates)
    original=module._save_one_snapshot
    def save(connection,item):
        paths.append(item.path);state['saving']=True
        return original(connection,item)
    monkeypatch.setattr(module,'_save_one_snapshot',save)
    task=asyncio.create_task(maintain_snapshots(MaintenanceContext(store=store,state=MaintenanceState(),threshold=1,batch_size=2)))
    try:
        await asyncio.wait_for(closing.wait(),60)
        task.cancel()
        delivered=loop.create_future();loop.call_soon(delivered.set_result,None);await delivered
        assert paths and paths[0].exists() and not task.done() and not state['closed']
        release.set()
        with pytest.raises(SnapshotMaintenanceError,match='unknown') as raised:await task
        assert raised.value.result.kind.value=='unknown' and raised.value.operation_key is not None
        assert state['closed'] and all(not path.exists() for path in paths)
        assert owned.connection.execute('SELECT COUNT(*) FROM entity_snapshots').fetchone()[0]==1
    finally:
        release.set()
        if not task.done():
            try:await task
            except (asyncio.CancelledError,SnapshotMaintenanceError):pass
        await executor.close();owned.connection.close()


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
@pytest.mark.parametrize('failure',['missing_assignment','query_error'])
async def test_write_once_proof_missing_evidence_never_returns_a_partial_handle(tmp_path,monkeypatch,failure):
    import sqlite3
    from camctl.history.bounded import SegmentHistoryHandle
    owned,_,_,ref,history,before,prepared=await _before_assessment(tmp_path)
    opened=[];original_init=SegmentHistoryHandle.__init__
    def capture(self,*args,**kwargs):
        original_init(self,*args,**kwargs);opened.append((self,Path(self._temporary.name)))
    monkeypatch.setattr(SegmentHistoryHandle,'__init__',capture)
    try:
        if failure=='missing_assignment':
            event=owned.connection.execute('SELECT MAX(event_id) FROM entity_event_links WHERE entity_type=1 AND entity_id=?',(ref.action_id,)).fetchone()[0]
            owned.connection.execute('DELETE FROM entity_event_links WHERE entity_type=1 AND entity_id=? AND event_id=?',(ref.action_id,event))
            error=ConsistencyError
        else:
            def fail(*args,**kwargs):raise sqlite3.OperationalError('proof page read failed')
            monkeypatch.setattr(history,'_object_event_page',fail);error=sqlite3.OperationalError
        with pytest.raises(error):history.open_segment_history(ref.action_id,before,strategy='snapshot',batch_size=1)
        assert all(handle._db is None and not directory.exists() for handle,directory in opened)
    finally:prepared.close();owned.connection.close()


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
    from camctl.persistence.repositories import history as module
    owned,_,_,ref,history=await _fixture(tmp_path)
    store,executor=await _snapshot_store(history)
    async def candidates(*_):return (SnapshotRef(1,ref.action_id),)
    monkeypatch.setattr(store,'snapshot_candidates',candidates)
    loop=asyncio.get_running_loop();delayed=asyncio.Event();ended=asyncio.Event();held=[];paths=[]
    original_save=module._save_one_snapshot
    def save(connection,item):paths.append(item.path);return original_save(connection,item)
    monkeypatch.setattr(module,'_save_one_snapshot',save)
    original_finish=executor._finish
    def finish(job,payload):
        if job.description=='快照保存':held.append((job,payload));delayed.set()
        else:original_finish(job,payload)
    monkeypatch.setattr(executor,'_finish',finish)
    original_complete=executor._core.complete
    def complete(key):
        original_complete(key)
        if held and key==held[0][0].key:loop.call_soon_threadsafe(ended.set)
        elif paths:loop.call_soon_threadsafe(ended.set)
    monkeypatch.setattr(executor._core,'complete',complete)
    task=asyncio.create_task(maintain_snapshots(MaintenanceContext(store=store,state=MaintenanceState(),threshold=1,batch_size=2)))
    try:
        await asyncio.wait_for(delayed.wait(),60);await asyncio.wait_for(ended.wait(),60)
        task.cancel()
        delivered=loop.create_future();loop.call_soon(delivered.set_result,None);await delivered
        assert paths[0].exists() and not task.done()
        original_finish(*held[0])
        with pytest.raises(asyncio.CancelledError):await task
        assert not paths[0].exists() and not executor._results
        assert owned.connection.execute('SELECT COUNT(*) FROM entity_snapshots').fetchone()[0]==1
    finally:
        if held and not task.done():original_finish(*held[0])
        if not task.done():
            try:await task
            except asyncio.CancelledError:pass
        await executor.close();owned.connection.close()


def test_unframed_snapshot_body_has_fixed_decoder_residency():
    import gc,tracemalloc
    from camctl.history.snapshot_stream import _records
    from camctl.history.snapshots import SnapshotMaintenanceError
    chunk=b'x'*65536;evidence=[]
    for count in (4,64):
        gc.collect();tracemalloc.start()
        try:
            with pytest.raises(SnapshotMaintenanceError):
                for _ in _records(chunk for _ in range(count)):pytest.fail('unframed body produced a record')
            peak=tracemalloc.get_traced_memory()[1]
        finally:tracemalloc.stop()
        evidence.append((count*len(chunk),peak))
    print('UNFRAMED_RESIDENCY',evidence)
    assert evidence[-1][1]<512000
    assert evidence[-1][1]<evidence[0][1]+200000


@pytest.mark.parametrize('bad',[b'{"x":1,"\\u0078":2}\n',b'{"x":"\\ud800"}\n',b'{"x":01}\n',b'{"x":NaN}\n',b'{"x":"\xf0\x9f\n',b'{"x":1}\n{"y":'])
def test_framed_snapshot_keeps_exact_json_and_utf8_rejection(bad):
    from camctl.history.snapshot_stream import _records
    from camctl.history.snapshots import SnapshotMaintenanceError
    with pytest.raises(SnapshotMaintenanceError):
        for _ in _records(bytes((byte,)) for byte in bad):pass


def test_snapshot_accepts_necessary_large_row_and_split_exact_values():
    import tracemalloc
    from decimal import Decimal
    from camctl.history.snapshot_stream import _records
    def chunks():
        yield b'{"text":"'
        for _ in range(8):yield b'x'*65536
        yield b'","emoji":"\xf0';yield b'\x9f\x98';yield b'\x80","exact":1.234567890123456789,"n":9007199254740993}\n'
    tracemalloc.start()
    try:
        rows=_records(chunks());row=next(rows)
        assert len(row['text'])==8*65536 and row['text'].startswith('x') and row['text'].endswith('x')
        assert row['emoji']=='😀' and row['exact']==Decimal('1.234567890123456789')
        assert row['n']==9007199254740993
        with pytest.raises(StopIteration):next(rows)
        print('NECESSARY_SINGLE_ROW',8*65536,tracemalloc.get_traced_memory()[1])
    finally:tracemalloc.stop()


def test_framed_invalid_large_row_is_rejected_as_one_record():
    import tracemalloc
    from camctl.history.snapshot_stream import _records
    from camctl.history.snapshots import SnapshotMaintenanceError
    def chunks():
        yield b'{"text":"'
        for _ in range(8):yield b'x'*65536
        yield b'"!}\n'
    tracemalloc.start()
    try:
        with pytest.raises(SnapshotMaintenanceError):
            for _ in _records(chunks()):pytest.fail('invalid row produced a value')
        print('INVALID_FRAMED_SINGLE_ROW',8*65536,tracemalloc.get_traced_memory()[1])
    finally:tracemalloc.stop()


@pytest.mark.parametrize('failure',['write','read','truncate','header'])
@pytest.mark.asyncio
async def test_snapshot_line_tempfile_failure_or_upper_validation_closes_all_resources(tmp_path,monkeypatch,failure):
    import errno
    from camctl.history import snapshot_stream as module
    from camctl.history.bounded import SegmentHistoryHandle
    owned,_,_,ref,history=await _fixture(tmp_path)
    boundary=history.current_boundary()
    with history.open_segment_history(ref.action_id,boundary) as handle:prepared=prepare_snapshot(handle)
    opened=[];handles=[];original_file=module.TemporaryFile;original_init=SegmentHistoryHandle.__init__
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
    def capture(self,*args,**kwargs):
        original_init(self,*args,**kwargs);handles.append((self,Path(self._temporary.name)))
    try:
        await _save(history,prepared)
        if failure=='header':_change_snapshot(owned,prepared,lambda records:records[0].update(entity_id=999))
        monkeypatch.setattr(module,'TemporaryFile',create)
        monkeypatch.setattr(SegmentHistoryHandle,'__init__',capture)
        with pytest.raises(ConsistencyError if failure=='header' else OSError):
            history.open_segment_history(ref.action_id,boundary,strategy='snapshot',batch_size=2)
        assert opened and all(value.inner.closed for value in opened)
        assert handles and all(handle._db is None and not directory.exists() for handle,directory in handles)
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
async def test_snapshot_cannot_borrow_member_count_fixed_after_s(tmp_path):
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
            with pytest.raises(ConsistencyError):history.open_segment_history(ref.action_id,before,strategy='snapshot',batch_size=1)
        finally:prepared.close()
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_snapshot_write_once_proof_also_covers_other_action_owned_tables(tmp_path):
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
            with pytest.raises(ConsistencyError):history.open_segment_history(ref.action_id,boundary,strategy='snapshot',batch_size=1)
        finally:prepared.close()
    finally:owned.connection.close()


@pytest.mark.asyncio
async def test_snapshot_proof_decodes_required_interval_once_and_replays_disk_evidence(tmp_path,monkeypatch):
    from camctl.persistence.repositories import history as module
    from .test_record_segment_history import _version,_rows
    owned,runtime,repo,ref,history,before,prepared=await _before_assessment(tmp_path)
    try:
        boundary=history.current_boundary()
        _version(owned,runtime,repo,ref,2)
        count=owned.connection.execute('SELECT change_count FROM actions WHERE id=?',(ref.action_id,)).fetchone()[0]
        original=module.decode_event_row;previous=None;decoded=0
        def decode(*args,**kwargs):
            nonlocal previous,decoded
            value=original(*args,**kwargs)
            assert previous is None or value.event_id<previous
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

"""取消版本接手原H0全集，恢复按明确前版及取消事件定位。"""
from dataclasses import replace
from datetime import datetime,timezone
from pathlib import Path

import pytest

from camctl.capture.segment_storage import (
    PublicationKind,ResultSetCreate,SegmentSourceKind)
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.values import new_operation_key
from camctl.history.validators import EventContext
from camctl.persistence.event_reads import SqlEventReads
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.segments import SegmentRepository
from camctl.persistence.transaction import BoundedCommandPlan,commit_operation

from .test_segment_result_sets import _two_scan_result

pytestmark=pytest.mark.asyncio


def _ok(receipt):
    assert receipt.kind is DbOutcomeKind.COMPLETED,receipt.error
    return receipt.value


def _cancel(owned,runtime,action_id):
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import CommandMode,ProcessInput
    from camctl.cancellation.models import (
        ApplyCancelTarget,CancelApplyMode,CancellationEffect,FixCancelTargets,
        FixedCancelSet,FixedTarget,SelectionBasis,StartCancelAction)
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from camctl.persistence.repositories.cancellation import CancellationRepository
    from .test_segment_storage import SegmentedCatalog
    from camctl.history.decoding import decode_event_row
    from camctl.persistence.repositories.result_pages import _COLUMNS
    at=runtime.wall_us()
    instant=datetime.fromtimestamp(at/1_000_000,timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    _ok(AcceptanceRepository().process_input(ProcessInput(ParsedInput('cancel.json',{
        'request_id':'200','created_at':instant,'name':'取消原录像','actions':[
            {'name':'取消','type':'cancel_task','params':{'target':{'action_instance_id':str(action_id)}}}],
    }),SegmentedCatalog(),CommandMode.RUN,at),new_operation_key(),owned))
    cancel_id=owned.connection.execute('SELECT MAX(id) FROM actions').fetchone()[0]
    repository=CancellationRepository()
    _ok(repository.start_cancel_action(StartCancelAction(cancel_id,at),new_operation_key(),owned))
    fixed=_ok(repository.fix_cancel_targets(FixCancelTargets(cancel_id,FixedCancelSet((
        FixedTarget(action_id,SelectionBasis.DIRECT,CancellationEffect.NOT_APPLIED),)),at),
        new_operation_key(),owned))
    _ok(repository.apply_cancel_target(ApplyCancelTarget(fixed.item_ids[0],CancelApplyMode.WITH_STOP,at),
        new_operation_key(),owned))
    event_id=owned.connection.execute('SELECT last_event_id FROM actions WHERE id=?',(action_id,)).fetchone()[0]
    event=decode_event_row(owned.connection.execute(
        f'SELECT {_COLUMNS} FROM history_events WHERE id=?',(event_id,)).fetchone())
    assert any(row.table=='actions' and row.row_id==action_id
        and row.before.values.get('cancel_requested')==0
        and row.after.values.get('cancel_requested')==1 for row in event.rows)
    return event_id


def _request(repository,ref,owned,runtime,cancel):
    from camctl.persistence.repositories.result_pages import _at
    row=repository.read_result_set(ref,owned)
    scan=None if row['current_final_page_event_id'] is None else _at(
        owned.connection,row['current_final_page_event_id'])[0].ref
    return ResultSetCreate(ref.action_id,row['activity_id'],row['run_id'],
        SegmentSourceKind.RETAINED_SOURCES,HistoryBoundary(row['source_txn_id'],row['source_event_id']),
        scan,PublicationKind.CANCELED,ref.set_id,cancel,runtime.wall_us())


def _commit(command,key,owned,bounded):
    class Command:
        def plan(self,scope):
            plan=command.plan(scope)
            if not bounded:return plan
            return BoundedCommandPlan(plan.events,
                lambda event,transaction:EventContext(transaction,plan.owners,plan.state_rows,
                    current_reads=SqlEventReads(scope.connection,plan.segment_read_scope)),
                plan.result,lambda connection:None,result_check_write_scope=plan.result_check_write_scope)
    receipt=commit_operation(Command(),key,owned)
    assert receipt.kind=='completed',receipt.error
    return receipt.result


async def _snapshot_action(path,action_id):
    from camctl.contracts.enums import load_registry
    from camctl.persistence.repositories.history import SqliteSnapshotStore
    from camctl.persistence.executor import DbExecutor
    from camctl.persistence.runtime import DbConfig,DbOpenMode,open_existing
    from camctl.history.snapshots import SnapshotRef,prepare_snapshot
    executor=DbExecutor(lambda:open_existing(path,DbOpenMode.EXISTING_RW,DbConfig()),
        capacity=4,enqueue_timeout_seconds=9)
    try:
        store=SqliteSnapshotStore(path,executor)
        owner=load_registry()['history_objects']['action']['id']
        image=(await store.load_entity_images((SnapshotRef(owner,action_id),)))[0]
        await store.save_snapshots((prepare_snapshot(image),))
    finally:
        await executor.close()


def _history_members(handle,ref):
    cursor=None
    result=[]
    while True:
        page=handle.read_page('segment_members',ref=ref,cursor=cursor,limit=1)
        result.extend(row.values['file_id'] for row in page.items)
        if page.next_cursor is None:return sorted(result)
        cursor=page.next_cursor


@pytest.mark.parametrize('bounded',[False,True])
@pytest.mark.parametrize('previous_state',['collecting_prefix','fixed'])
async def test_canceled_result_rebuilds_all_original_inputs_and_recovers_exact_version(tmp_path,bounded,previous_state):
    from camctl.persistence.repositories.segment_result_members import ResultMembersCommand
    from camctl.persistence.repositories.segment_result_sets import CreateResultSetCommand
    from camctl.persistence.repositories.history import HistoryRepository
    from camctl.persistence.runtime import DbConfig,DbOpenMode,open_existing
    from camctl.contracts.values import ConsistencyError
    owned,runtime,repository,previous=await _two_scan_result(tmp_path)
    try:
        inputs=repository.read_result_member_inputs(previous,None,128,owned).items
        count=1 if previous_state=='collecting_prefix' else len(inputs)
        _ok(repository.append_result_members(previous,inputs[:count],runtime.wall_us(),new_operation_key(),owned))
        if previous_state=='fixed':
            _ok(repository.fix_result_members(previous,runtime.wall_us(),new_operation_key(),owned))
        old_members=tuple(owned.connection.execute('SELECT * FROM segment_members WHERE set_id=?',
            (previous.set_id,)))
        path=Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
        history=HistoryRepository(path)
        before_cancel=history.current_boundary()
        old_set=repository.read_result_set(previous,owned)
        await _snapshot_action(path,previous.action_id)
        cancel=_cancel(owned,runtime,previous.action_id)
        request=_request(repository,previous,owned,runtime,cancel)
        key=new_operation_key()
        ref=_commit(CreateResultSetCommand(request,key),key,owned,bounded)
        assert ref.version==previous.version+1 and ref.set_id!=previous.set_id
        pending=None
        while True:
            page=repository.read_result_member_inputs(ref,pending,1,owned)
            for item in page.items:
                member_key=new_operation_key()
                _commit(ResultMembersCommand('MEMBERS',ref,(item,),runtime.wall_us(),member_key),
                    member_key,owned,bounded)
            if page.next_cursor is None:break
            pending=page.next_cursor
        fix_key=new_operation_key()
        _commit(ResultMembersCommand('FIX_RESULT_MEMBERS',ref,None,runtime.wall_us(),fix_key),
            fix_key,owned,bounded)
        assert repository.read_result_set(ref,owned)['member_count']==2
        assert tuple(owned.connection.execute('SELECT * FROM segment_members WHERE set_id=?',
            (previous.set_id,)))==old_members
        row=repository.read_result_set(ref,owned)
        assert row['source_kind']==2 and row['predecessor_set_id']==previous.set_id
        assert (row['source_txn_id'],row['source_event_id'])==(
            request.source_boundary.txn_id,request.source_boundary.last_event_id)
        fresh=open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())
        try:
            before=fresh.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()
            assert _ok(repository.create_result_set(request,key,fresh))==ref
            assert fresh.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()==before
        finally:
            fresh.connection.close()
        fresh=open_existing(path,DbOpenMode.EXISTING_RO,DbConfig())
        try:
            assert repository.find_canceled_result_ref(previous.action_id,previous.set_id,cancel,fresh)==ref
            assert repository.find_canceled_result_ref(previous.action_id,None,cancel,fresh) is None
        finally:
            fresh.connection.close()
        boundary=history.current_boundary()
        for strategy in ('initial','current','snapshot'):
            with history.open_segment_history(previous.action_id,before_cancel,strategy=strategy,batch_size=1) as handle:
                assert handle.read_result(previous)==old_set
                assert len(_history_members(handle,previous))==count
                with pytest.raises(ConsistencyError):handle.read_result(ref)
            with history.open_segment_history(previous.action_id,boundary,strategy=strategy,batch_size=1) as handle:
                assert handle.read_result(ref)==row
                assert _history_members(handle,ref)==sorted(item.file_id for item in inputs)
                assert handle.read_result(previous)==old_set
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone()==(0,)
        assert owned.connection.execute('SELECT COUNT(*) FROM segment_scope_members').fetchone()==(0,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('bounded',[False,True])
@pytest.mark.parametrize('changed',['omit_predecessor','canceled_predecessor'])
async def test_cancel_create_guard_requires_original_unpublished_predecessor(tmp_path,monkeypatch,bounded,changed):
    from camctl.persistence.repositories import segment_result_sets as storage
    owned,runtime,repository,previous=await _two_scan_result(tmp_path)
    try:
        cancel=_cancel(owned,runtime,previous.action_id)
        request=_request(repository,previous,owned,runtime,cancel)
        if changed=='canceled_predecessor':
            prior=_ok(repository.create_result_set(request,new_operation_key(),owned))
            request=replace(request,predecessor_set_id=prior.set_id)
        else:
            request=replace(request,predecessor_set_id=None)
        before=tuple(owned.connection.execute('SELECT * FROM segment_sets'))
        events=owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()
        key=new_operation_key()
        class Command:
            def plan(self,scope):
                with monkeypatch.context() as patch:
                    patch.setattr(storage,'_publication',lambda *args:None)
                    plan=storage.CreateResultSetCommand(request,key).plan(scope)
                if not bounded:return plan
                return BoundedCommandPlan(plan.events,
                    lambda event,transaction:EventContext(transaction,plan.owners,plan.state_rows,
                        current_reads=SqlEventReads(scope.connection,plan.segment_read_scope)),
                    plan.result,lambda connection:None)
        receipt=commit_operation(Command(),key,owned)
        assert receipt.kind=='rolled_back' and '未发布' in str(receipt.error),receipt.error
        assert tuple(owned.connection.execute('SELECT * FROM segment_sets'))==before
        assert owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()==events
    finally:
        owned.connection.close()


async def test_cancel_after_publication_returns_original_result_without_new_version(tmp_path):
    from camctl.capture.segment_models import DefaultScopeState
    from camctl.capture.segment_finish import FinishSegmentRecording
    from camctl.persistence.repositories.segment_transactions import finish_segment_recording
    from camctl.capture.segment_storage import SegmentResultRef
    from .test_segment_storage import _assessed_world
    owned,runtime,repository,strict,_=await _assessed_world(tmp_path,DefaultScopeState.NONEMPTY)
    try:
        row=repository.read_set(strict,owned)
        finished=finish_segment_recording(FinishSegmentRecording(strict,row['activity_id'],row['run_id'],
            runtime.wall_us()),new_operation_key(),owned)
        assert finished.kind=='completed',finished.error
        ref=SegmentResultRef(strict.action_id,row['id'],strict.version)
        request=replace(_request(repository,ref,owned,runtime,None),
            source_kind=SegmentSourceKind.COMPLETE_SCAN,predecessor_set_id=None)
        before=tuple(owned.connection.execute('SELECT * FROM segment_sets'))
        outputs=tuple(owned.connection.execute('SELECT * FROM outputs'))
        events=owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()
        for _ in range(2):
            assert _ok(repository.create_result_set(request,new_operation_key(),owned))==ref
        assert tuple(owned.connection.execute('SELECT * FROM segment_sets'))==before
        assert tuple(owned.connection.execute('SELECT * FROM outputs'))==outputs
        assert owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()==events
        assert repository.read_active_set(strict.action_id,owned)==strict
    finally:
        owned.connection.close()


@pytest.mark.parametrize('bounded',[False,True])
@pytest.mark.parametrize('cut',['before','after'])
async def test_unknown_cancel_create_closes_original_and_reuses_same_request_key(tmp_path,monkeypatch,bounded,cut):
    import sqlite3
    from camctl.persistence.repositories import segment_result_sets as storage
    from camctl.persistence.runtime import DbConfig,DbOpenMode,open_existing
    from .test_segment_transaction_batches import ConnectionProbe
    owned,runtime,repository,previous=await _two_scan_result(tmp_path)
    closed=False
    try:
        cancel=_cancel(owned,runtime,previous.action_id)
        request=_request(repository,previous,owned,runtime,cancel)
        key=new_operation_key()
        path=Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
        calls=(runtime.driver.control.await_count,runtime.stopper.stop.await_count)
        class Command:
            def plan(self,scope):
                plan=storage.CreateResultSetCommand(request,key).plan(scope)
                if not bounded:return plan
                return BoundedCommandPlan(plan.events,
                    lambda event,transaction:EventContext(transaction,plan.owners,plan.state_rows,
                        current_reads=SqlEventReads(scope.connection,plan.segment_read_scope)),
                    plan.result,lambda connection:None)
        def hook(stage,statement,parameters):
            if statement=='COMMIT' and stage==cut:
                raise sqlite3.OperationalError('取消版本提交回执未知')
        receipt=commit_operation(Command(),key,replace(owned,
            connection=ConnectionProbe(owned.connection,hook)))
        assert receipt.kind=='unknown',receipt.error
        owned.connection.close();closed=True
        fresh=open_existing(path,DbOpenMode.EXISTING_RO,DbConfig())
        try:
            found=repository.find_canceled_result_ref(previous.action_id,previous.set_id,cancel,fresh)
            assert (found is not None)==(cut=='after')
        finally:
            fresh.connection.close()
        fresh=open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())
        try:
            if cut=='after':
                def no_new_id(*args):raise AssertionError('原键恢复不分配新版本或集合身份')
                monkeypatch.setattr(storage,'next_row_id',no_new_id)
            ref=_ok(repository.create_result_set(request,key,fresh))
            assert cut=='before' or ref==found
            assert ref.version==previous.version+1
            assert fresh.connection.execute('SELECT COUNT(*) FROM segment_sets').fetchone()==(2,)
            changed=repository.create_result_set(replace(request,occurred_at=request.occurred_at+1),key,fresh)
            assert changed.kind is DbOutcomeKind.ROLLED_BACK
            assert (runtime.driver.control.await_count,runtime.stopper.stop.await_count)==calls
        finally:
            fresh.connection.close()
    finally:
        if not closed:owned.connection.close()

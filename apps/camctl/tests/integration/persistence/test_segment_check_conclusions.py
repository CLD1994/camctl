"""有限决定引用终态前实际来源；本夹具只验证持久化接缝。"""
from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.capture import segment_storage as models
from camctl.capture.baseline_models import (
    BaselineReadIntent,BaselineReadReturn,BaselineReadUnconfirmedSave)
from camctl.contracts.values import new_operation_key
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryRead
from camctl.operations.models import ErrorValue
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.segments import SegmentRepository

from .test_segment_file_proofs import _boundary
from .test_segment_result_versions import _commit
from .test_segment_storage import SegmentedCatalog
from ..capture.result_consumer_fixtures import consumer_world

pytestmark=pytest.mark.asyncio


def _ok(receipt):
    assert receipt.kind is DbOutcomeKind.COMPLETED,receipt.error
    return receipt.value


class _BaselineCatalog(SegmentedCatalog):
    def parameter_definition(self,*args):
        definition=super().parameter_definition(*args)
        if args[1]!='camera_record':return definition
        factory=definition.task_factory
        return replace(definition,task_factory=lambda params:replace(factory(params),
            ownership_mode=2,output_scope={'directories':['/DCIM']}))


async def _baseline_end_world(tmp_path,*,unconfirmed=False,success=False,early_boundary=False,catalog=None):
    owned,runtime,action_id,_=await consumer_world(tmp_path,'record',
        catalog=_BaselineCatalog() if catalog is None else catalog,start=False)
    try:
        return _save_baseline_end(owned,runtime,action_id,unconfirmed=unconfirmed,success=success,
            early_boundary=early_boundary)
    except BaseException:
        owned.connection.close()
        raise


def _save_baseline_end(owned,runtime,action_id,*,unconfirmed=False,success=False,early_boundary=False):
    activity=owned.connection.execute('SELECT id FROM device_activities WHERE action_id=?',
        (action_id,)).fetchone()[0]
    action=runtime.action(action_id)
    request=BaselineReadIntent(action_id,activity,DeviceBinding(action['device_id'],action['driver_id']),
        ('/DCIM',),None,Decimal('1.25'),runtime.capture.baseline_ref(activity,owned),1,
        new_operation_key(),new_operation_key(),runtime.wall_us())
    intent=_ok(runtime.capture.save_baseline_read_intent(request,request.call_key,owned))
    repository=SegmentRepository()
    def freeze():
        return _ok(repository.create_result_set(models.ResultSetCreate(action_id,activity,None,
            models.SegmentSourceKind.NO_RESULTS,_boundary(owned),None,models.PublicationKind.RESULT,
            None,None,runtime.wall_us()),new_operation_key(),owned))
    if early_boundary:ref=freeze()
    if unconfirmed:
        actual=_ok(runtime.capture.save_baseline_read_unconfirmed(
            BaselineReadUnconfirmedSave(intent.call_id,runtime.wall_us()),request.result_save_key,owned))
    else:
        from camctl.contracts.pages import Page
        error=None if success else ErrorValue('adb_directory_failed','device_file_access',{'reason':'actual_read_failure'})
        actual=_ok(runtime.capture.save_baseline_read_return(BaselineReadReturn(intent.call_id,
            runtime.wall_us(),error,DirectoryRead(None,error,Page((),None) if success else None)),
            request.result_save_key,owned))
    if not early_boundary:ref=freeze()
    _ok(repository.fix_result_members(ref,runtime.wall_us(),new_operation_key(),owned))
    return owned,runtime,repository,ref,actual


@pytest.mark.parametrize('bounded',[False,True])
@pytest.mark.parametrize('unconfirmed',[False,True])
async def test_no_result_decision_anchors_saved_baseline_end_and_keeps_action_running(tmp_path,bounded,unconfirmed):
    owned,runtime,repository,ref,actual=await _baseline_end_world(tmp_path,unconfirmed=unconfirmed)
    try:
        request=models.ResultCheckConclusionSave(ref,models.ResultCheckEndReason.NO_RESULT_WORK,
            actual.result_event_id,None,None,None,None,runtime.wall_us())
        key=new_operation_key()
        if bounded:
            from camctl.persistence.repositories.segment_check_conclusions import ConcludeResultCheckCommand
            result=_commit(ConcludeResultCheckCommand(request,key),key,owned,True)
        else:
            result=_ok(repository.conclude_result_check(request,key,owned))
        assert isinstance(result,models.ResultCheckConclusionRef) and result.ref==ref
        row=owned.connection.execute('SELECT set_id,anchor_event_id,prior_run_end_event_id,config_event_id,'
            'prior_conclusion_id,current_result,current_failure_json FROM segment_check_conclusions WHERE id=?',
            (result.conclusion_id,)).fetchone()
        assert row==(ref.set_id,actual.result_event_id,None,None,None,1,None)
        assert _ok(repository.conclude_result_check(request,key,owned))==result
        assert repository.conclude_result_check(replace(request,occurred_at=request.occurred_at+1),
            key,owned).kind is DbOutcomeKind.ROLLED_BACK
        assert owned.connection.execute('SELECT status FROM actions WHERE id=?',(ref.action_id,)).fetchone()==(2,)
        assert owned.connection.execute('SELECT occupancy_state FROM device_activities').fetchone()==(1,)
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone()==(0,)
        assert owned.connection.execute('SELECT COUNT(*) FROM operation_runs').fetchone()==(0,)
        assert runtime.driver.control.await_count==0
    finally:
        owned.connection.close()


@pytest.mark.parametrize('self_reported_singleton',[False,True])
async def test_decision_cannot_share_actual_transaction_with_another_legal_decision(tmp_path,self_reported_singleton):
    from datetime import datetime,timezone
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import CommandMode,ProcessInput
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from camctl.persistence.repositories.scheduling import ObserveWindowRequest,SchedulingRepository,StartActionRequest
    from camctl.history.validators import EventContext
    from camctl.persistence.event_reads import SqlEventReads
    from camctl.persistence.transaction import BoundedCommandPlan,TransactionScope,commit_operation
    from camctl.persistence.repositories.segment_check_conclusions import ConcludeResultCheckCommand
    from .test_segment_transaction_batches import _fingerprint
    class Catalog(_BaselineCatalog):
        def device_exists(self,device_id):return device_id in ('cam-1','cam-2')
        def driver_id(self,device_id):return 'camctl-adb' if self.device_exists(device_id) else None
    catalog=Catalog()
    owned,runtime,repository,ref,actual=await _baseline_end_world(tmp_path,catalog=catalog)
    try:
        at=runtime.wall_us()
        instant=datetime.fromtimestamp(at/1_000_000,timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        _ok(AcceptanceRepository().process_input(ProcessInput(ParsedInput('second.json',{
            'request_id':'2','created_at':instant,'name':'第二独立录像','actions':[
                {'name':'拍摄','type':'camera_record','device_id':'cam-2','scheduled_at':instant,
                    'params':{'type':'video'},'policy':{'max_delay_ms':5000}}],
        }),catalog,CommandMode.RUN,at),new_operation_key(),owned))
        action_id=owned.connection.execute('SELECT MAX(id) FROM actions').fetchone()[0]
        scheduling=SchedulingRepository()
        _ok(scheduling.observe_window(ObserveWindowRequest(action_id,at,at),new_operation_key(),owned))
        _ok(scheduling.start_action(StartActionRequest(action_id,at,at),new_operation_key(),owned))
        _,_,_,other,other_actual=_save_baseline_end(owned,runtime,action_id)
        requests=tuple(models.ResultCheckConclusionSave(candidate,models.ResultCheckEndReason.NO_RESULT_WORK,
            source.result_event_id,None,None,None,None,runtime.wall_us())
            for candidate,source in ((ref,actual),(other,other_actual)))
        key=new_operation_key()
        before=_fingerprint(owned.connection)
        class Command:
            def plan(self,scope):
                allocation=scope.allocate(2)
                plans=tuple(ConcludeResultCheckCommand(request,key).plan(TransactionScope(
                    scope.connection,scope.max_txn_id,scope.max_event_id)) for request in requests)
                events=[]
                for index,plan in enumerate(plans):
                    event=plan.events[0];row=event.rows[0]
                    event_id=allocation.first_event_id+index
                    row=replace(row,row_id=row.row_id+index,after=replace(row.after,
                        values={**row.after.values,'created_event_id':event_id}))
                    events.append(replace(event,event_id=event_id,rows=(row,)))
                def context(event,transaction):
                    plan=plans[event.event_id-allocation.first_event_id]
                    if self_reported_singleton:
                        transaction=replace(transaction,first_event_id=event.event_id,last_event_id=event.event_id)
                    return EventContext(transaction,
                        {('segment_check_conclusions',event.rows[0].row_id):('action',plan.segment_read_scope.ref.action_id)},
                        plan.state_rows,current_reads=SqlEventReads(scope.connection,plan.segment_read_scope))
                return BoundedCommandPlan(tuple(events),context,None,lambda connection:None)
        receipt=commit_operation(Command(),key,owned)
        assert receipt.kind=='rolled_back',receipt.error
        assert owned.connection.execute('SELECT COUNT(*) FROM segment_check_conclusions').fetchone()==(0,)
        assert _fingerprint(owned.connection)==before
    finally:
        owned.connection.close()


@pytest.mark.parametrize('bounded',[False,True])
@pytest.mark.parametrize('damage',['anchor','identity','current_result','config','no_scope'])
async def test_decision_guard_rechecks_actual_source_and_fields(tmp_path,bounded,damage):
    from camctl.history.validators import EventContext
    from camctl.persistence.event_reads import SqlEventReads
    from camctl.persistence.transaction import BoundedCommandPlan,commit_operation
    from camctl.persistence.repositories.segment_check_conclusions import ConcludeResultCheckCommand
    owned,runtime,repository,ref,actual=await _baseline_end_world(tmp_path)
    try:
        request=models.ResultCheckConclusionSave(ref,models.ResultCheckEndReason.NO_RESULT_WORK,
            actual.result_event_id,None,None,None,None,runtime.wall_us())
        key=new_operation_key()
        before=owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()
        class Command:
            def plan(self,scope):
                plan=ConcludeResultCheckCommand(request,key).plan(scope)
                event=plan.events[0];row=event.rows[0];values=dict(row.after.values)
                if damage=='anchor':
                    changed=replace(request,anchor_event_id=actual.intent_event_id)
                    values['anchor_event_id']=changed.anchor_event_id
                    event=replace(event,evidence={'result_check_decision':models.document(changed)})
                elif damage=='identity':
                    row=replace(row,row_id=row.row_id+10)
                    plan=replace(plan,owners={('segment_check_conclusions',row.row_id):('action',ref.action_id)})
                elif damage=='current_result':values['current_result']=2
                elif damage=='config':values['config_event_id']=actual.intent_event_id
                event=replace(event,rows=(replace(row,after=replace(row.after,values=values)),))
                if bounded:
                    return BoundedCommandPlan((event,),
                        lambda event,transaction:EventContext(transaction,plan.owners,plan.state_rows,
                            current_reads=None if damage=='no_scope' else SqlEventReads(
                                scope.connection,plan.segment_read_scope)),None,lambda connection:None,
                        result_check_write_scope=plan.result_check_write_scope)
                return replace(plan,events=(event,),segment_read_scope=None if damage=='no_scope' else plan.segment_read_scope)
        receipt=commit_operation(Command(),key,owned)
        assert receipt.kind=='rolled_back',receipt.error
        assert owned.connection.execute('SELECT COUNT(*) FROM segment_check_conclusions').fetchone()==(0,)
        assert owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()==before
    finally:
        owned.connection.close()


@pytest.mark.parametrize('unconfirmed',[False,True])
async def test_decision_original_key_fresh_read_and_same_h_history(tmp_path,unconfirmed):
    from pathlib import Path
    from camctl.persistence.runtime import DbConfig,DbOpenMode,open_existing
    from camctl.persistence.repositories.history import HistoryRepository
    from camctl.contracts.values import ConsistencyError
    from .test_segment_result_versions import _snapshot_action
    owned,runtime,repository,ref,actual=await _baseline_end_world(tmp_path,unconfirmed=unconfirmed)
    try:
        request=models.ResultCheckConclusionSave(ref,models.ResultCheckEndReason.NO_RESULT_WORK,
            actual.result_event_id,None,None,None,None,runtime.wall_us())
        path=Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
        await _snapshot_action(path,ref.action_id)
        key=new_operation_key()
        saved=_ok(repository.conclude_result_check(request,key,owned))
        history=HistoryRepository(path)
        boundary=history.current_boundary()
        original=owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()
        fresh=open_existing(path,DbOpenMode.EXISTING_RO,DbConfig())
        try:
            assert repository.verify_result_check_conclusion(request,key,fresh)==saved
            assert repository.verify_result_check_conclusion(request,new_operation_key(),fresh) is None
            with pytest.raises(ConsistencyError):
                repository.verify_result_check_conclusion(request,key,fresh,execution_finished=False)
            with pytest.raises(ConsistencyError):
                repository.verify_result_check_conclusion(replace(request,occurred_at=request.occurred_at+1),key,fresh)
        finally:
            fresh.connection.close()
        for strategy in ('initial','current','snapshot'):
            with history.open_segment_history(ref.action_id,boundary,strategy=strategy,batch_size=1) as handle:
                page=handle.read_page('segment_check_conclusions',ref=ref,limit=1)
                assert len(page.items)==1 and page.next_cursor is None
                assert page.items[0].row_id==saved.conclusion_id
                assert page.items[0].values['anchor_event_id']==actual.result_event_id
                assert handle.primary()['status']==2
        await _snapshot_action(path,ref.action_id)
        with history.open_segment_history(ref.action_id,boundary,strategy='snapshot',batch_size=1) as handle:
            page=handle.read_page('segment_check_conclusions',ref=ref,limit=1)
            assert page.items[0].row_id==saved.conclusion_id
            assert page.items[0].values['anchor_event_id']==actual.result_event_id
        assert owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()==original
    finally:
        owned.connection.close()


@pytest.mark.parametrize('case',['success','later_return'])
async def test_no_result_decision_cannot_use_success_or_return_after_h0(tmp_path,case):
    owned,runtime,repository,ref,actual=await _baseline_end_world(tmp_path,
        success=case=='success',early_boundary=case=='later_return')
    try:
        request=models.ResultCheckConclusionSave(ref,models.ResultCheckEndReason.NO_RESULT_WORK,
            actual.result_event_id,None,None,None,None,runtime.wall_us())
        receipt=repository.conclude_result_check(request,new_operation_key(),owned)
        assert receipt.kind is DbOutcomeKind.ROLLED_BACK,receipt.error
        assert owned.connection.execute('SELECT COUNT(*) FROM segment_check_conclusions').fetchone()==(0,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('bounded',[False,True])
@pytest.mark.parametrize('cut',['before','after'])
async def test_decision_commit_unknown_recovers_original_only_after_save_closes(tmp_path,monkeypatch,bounded,cut):
    import sqlite3
    from pathlib import Path
    from camctl.persistence.runtime import DbConfig,DbOpenMode,open_existing
    from camctl.persistence.transaction import BoundedCommandPlan,commit_operation
    from camctl.history.validators import EventContext
    from camctl.persistence.event_reads import SqlEventReads
    from camctl.persistence.repositories import segment_check_conclusions as storage
    from .test_segment_transaction_batches import ConnectionProbe
    owned,runtime,repository,ref,actual=await _baseline_end_world(tmp_path)
    closed=False
    try:
        request=models.ResultCheckConclusionSave(ref,models.ResultCheckEndReason.NO_RESULT_WORK,
            actual.result_event_id,None,None,None,None,runtime.wall_us())
        key=new_operation_key()
        path=Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
        class Command:
            def plan(self,scope):
                plan=storage.ConcludeResultCheckCommand(request,key).plan(scope)
                if not bounded:return plan
                return BoundedCommandPlan(plan.events,
                    lambda event,transaction:EventContext(transaction,plan.owners,plan.state_rows,
                        current_reads=SqlEventReads(scope.connection,plan.segment_read_scope)),
                    plan.result,lambda connection:None,result_check_write_scope=plan.result_check_write_scope)
        def hook(stage,statement,parameters):
            if statement=='COMMIT' and stage==cut:raise sqlite3.OperationalError('有限决定提交回执未知')
        outcome=commit_operation(Command(),key,replace(owned,connection=ConnectionProbe(owned.connection,hook)))
        assert outcome.kind=='unknown',outcome.error
        owned.connection.close();closed=True
        fresh=open_existing(path,DbOpenMode.EXISTING_RO,DbConfig())
        try:
            restored=repository.verify_result_check_conclusion(request,key,fresh)
            assert (restored is not None)==(cut=='after')
        finally:
            fresh.connection.close()
        fresh=open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())
        try:
            if cut=='after':
                def no_new_id(*args):raise AssertionError('原决定核实及重送不能重新分配身份')
                monkeypatch.setattr(storage,'next_row_id',no_new_id)
            saved=_ok(repository.conclude_result_check(request,key,fresh))
            assert cut=='before' or saved==restored
            assert fresh.connection.execute('SELECT COUNT(*) FROM segment_check_conclusions').fetchone()==(1,)
            assert fresh.connection.execute('SELECT status FROM actions WHERE id=?',(ref.action_id,)).fetchone()==(2,)
            assert runtime.driver.control.await_count==0
        finally:
            fresh.connection.close()
    finally:
        if not closed:owned.connection.close()

"""实际RESULTS配置、有限结束和决定作为完整事务共同保存。"""
from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.capture import segment_storage as models
from camctl.capture.handlers import _begin_check_round
from camctl.capture.result_pages import ResultPageSave
from camctl.contracts.values import new_operation_key
from camctl.history.validators import EventContext
from camctl.operations.attempts import AttemptConfig
from camctl.operations.validation import validate_outcome
from camctl.persistence.event_reads import SqlEventReads
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.segments import SegmentRepository
from camctl.persistence.transaction import BoundedCommandPlan,commit_operation

from .test_segment_check_conclusions import _ok
from .test_segment_file_proofs import _boundary
from .test_segment_result_sets import _finish_scan
from .test_segment_storage import SegmentedCatalog
from ..capture.result_consumer_fixtures import consumer_world
from ..capture.test_record_file_completion_wait import _REGISTRY
from ..capture.test_result_page_history import _actual

pytestmark=pytest.mark.asyncio


async def _partial_result_world(tmp_path,*,maximum=3,retained=False):
    if retained:
        from .test_segment_scan_pairing import _ready
        from .test_segment_file_pairing import _entry
        owned,runtime,first,_,_=await _ready(tmp_path,[_entry('CAM001/CAM_20261021090000_0001_D.OSV')])
        action_id=owned.connection.execute('SELECT action_id FROM operation_runs WHERE id=?',
            (first.ref.ticket.run_id,)).fetchone()[0]
        _finish_scan(owned,runtime,first,retry_wait=True)
    else:
        owned,runtime,action_id,_=await consumer_world(tmp_path,'record',catalog=SegmentedCatalog())
    try:
        runtime.check_config=AttemptConfig(maximum,Decimal('1.25'),Decimal('2'))
        ticket=_begin_check_round(runtime,action_id).ticket
        actual=_actual(ticket,entries=[],finalized=False)
        saved=_ok(runtime.capture.save_result_page(ResultPageSave(ticket,1,None,
            validate_outcome(ticket,actual,_REGISTRY),runtime.wall_us()),new_operation_key(),owned))
        page=runtime.capture.read_result_page(saved,owned)
        _finish_scan(owned,runtime,page,retry_wait=True)
        repository=SegmentRepository()
        ref=_ok(repository.create_result_set(models.ResultSetCreate(action_id,int(ticket.target_id),ticket.run_id,
            models.SegmentSourceKind.RETAINED_SOURCES,_boundary(owned),None,models.PublicationKind.RESULT,
            None,None,runtime.wall_us()),new_operation_key(),owned))
        inputs=repository.read_result_member_inputs(ref,None,128,owned).items
        if inputs:_ok(repository.append_result_members(ref,inputs,runtime.wall_us(),new_operation_key(),owned))
        _ok(repository.fix_result_members(ref,runtime.wall_us(),new_operation_key(),owned))
        result_event=owned.connection.execute('SELECT result_event_id FROM operation_attempts WHERE run_id=? ORDER BY attempt_no DESC LIMIT 1',
            (ticket.run_id,)).fetchone()[0]
        return owned,runtime,repository,ref,ticket.run_id,result_event
    except BaseException:
        owned.connection.close()
        raise


def _command(request,key,bounded):
    from camctl.persistence.repositories.segment_check_conclusions import ConcludeResultCheckCommand
    class Command:
        def plan(self,scope):
            plan=ConcludeResultCheckCommand(request,key).plan(scope)
            if not bounded or plan.read_only:return plan
            return BoundedCommandPlan(plan.events,
                lambda event,transaction:EventContext(transaction,plan.owners,plan.state_rows,
                    current_reads=SqlEventReads(scope.connection,plan.segment_read_scope)),
                plan.result,lambda connection:None,
                result_check_write_scope=plan.result_check_write_scope)
    return Command()


@pytest.mark.parametrize('bounded',[False,True])
@pytest.mark.parametrize('changed',[False,True])
async def test_actual_exhaustion_adopts_config_closes_run_and_saves_decision_together(tmp_path,bounded,changed):
    owned,runtime,repository,ref,run_id,result_event=await _partial_result_world(tmp_path,maximum=3 if changed else 1)
    try:
        config=AttemptConfig(1,Decimal('0.125') if changed else Decimal('1.25'),Decimal('0') if changed else Decimal('2'))
        request=models.ResultCheckConclusionSave(ref,models.ResultCheckEndReason.RESULT_BUDGET_EXHAUSTED,
            result_event,None,config,None,None,runtime.wall_us())
        key=new_operation_key()
        attempts=owned.connection.execute('SELECT * FROM operation_attempts ORDER BY id').fetchall()
        calls=(runtime.driver.control.await_count,runtime.stopper.stop.await_count)
        receipt=commit_operation(_command(request,key,bounded),key,owned)
        assert receipt.kind=='completed',receipt.error
        saved=receipt.result
        assert saved.ref==ref
        run=owned.connection.execute('SELECT status,attempts_used,max_attempts_used,timeout_s_json,'
            'retry_interval_s_json,retry_wait_required FROM operation_runs WHERE id=?',(run_id,)).fetchone()
        assert run[:3]==(6,1,1) and run[-1]==0
        from camctl.contracts.json_values import parse_exact_json
        assert parse_exact_json(run[3])==config.timeout_s and parse_exact_json(run[4])==config.retry_interval_s
        assert owned.connection.execute('SELECT * FROM operation_attempts ORDER BY id').fetchall()==attempts
        events=owned.connection.execute('SELECT id,event_type,body_json FROM history_events WHERE transaction_id='
            '(SELECT id FROM history_transactions WHERE operation_key=?) ORDER BY id',(str(key),)).fetchall()
        from camctl.history.events import load_event_registry
        conclusion_type=load_event_registry()['events']['SEGMENT_FACTS']['id']
        assert [row[1] for row in events]==([10,10,conclusion_type] if changed else [10,conclusion_type])
        assert [parse_exact_json(row[2])['reason'] for row in events[:-1]]==([2,3] if changed else [3])
        conclusion=owned.connection.execute('SELECT anchor_event_id,config_event_id,prior_run_end_event_id,'
            'current_result,current_failure_json FROM segment_check_conclusions WHERE id=?',(saved.conclusion_id,)).fetchone()
        assert conclusion[0]==result_event and conclusion[2:]==(None,1,None)
        assert conclusion[1]==(events[0][0] if changed else owned.connection.execute(
            'SELECT intent_event_id FROM operation_attempts WHERE run_id=?',(run_id,)).fetchone()[0])
        assert _ok(repository.conclude_result_check(request,key,owned))==saved
        assert repository.conclude_result_check(replace(request,check_config=replace(config,max_attempts=2)),
            key,owned).kind is DbOutcomeKind.ROLLED_BACK
        assert owned.connection.execute('SELECT status FROM actions WHERE id=?',(ref.action_id,)).fetchone()==(2,)
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone()==(0,)
        assert (runtime.driver.control.await_count,runtime.stopper.stop.await_count)==calls
    finally:
        owned.connection.close()


@pytest.mark.parametrize('bounded',[False,True])
async def test_failed_latest_scan_keeps_retained_files_without_reusing_prior_complete_scan(tmp_path,bounded):
    owned,runtime,repository,ref,run_id,result_event=await _partial_result_world(tmp_path,retained=True)
    try:
        members=owned.connection.execute('SELECT * FROM segment_members WHERE set_id=? ORDER BY id',(ref.set_id,)).fetchall()
        assert len(members)==1
        inputs=repository.read_result_member_inputs(ref,None,128,owned).items
        from camctl.contracts.json_values import parse_exact_json
        identity=parse_exact_json(owned.connection.execute('SELECT identity_key FROM device_files WHERE id=?',
            (inputs[0].file_id,)).fetchone()[0])[2]
        assert identity.endswith('_0001_D.OSV')
        assert repository.read_current_segment_ref(ref,owned) is None
        request=models.ResultCheckConclusionSave(ref,models.ResultCheckEndReason.RESULT_BUDGET_EXHAUSTED,
            result_event,None,AttemptConfig(1,Decimal('1.25'),Decimal('2')),None,None,runtime.wall_us())
        key=new_operation_key()
        receipt=commit_operation(_command(request,key,bounded),key,owned)
        assert receipt.kind=='completed',receipt.error
        assert owned.connection.execute('SELECT current_result,current_failure_json FROM segment_check_conclusions').fetchone()==(1,None)
        assert owned.connection.execute('SELECT attempts_used,max_attempts_used,status FROM operation_runs WHERE id=?',
            (run_id,)).fetchone()==(2,1,6)
        assert owned.connection.execute('SELECT * FROM segment_members WHERE set_id=? ORDER BY id',(ref.set_id,)).fetchall()==members
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone()==(0,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('bounded',[False,True])
@pytest.mark.parametrize('damage',['omit_config','omit_close','omit_decision','close_only_no_scope','no_scope','wrong_config','false_range'])
async def test_kernel_rejects_incomplete_or_changed_result_decision_groups(tmp_path,bounded,damage):
    from camctl.persistence.repositories.segment_check_conclusions import ConcludeResultCheckCommand
    from .test_segment_transaction_batches import _fingerprint
    owned,runtime,repository,ref,run_id,result_event=await _partial_result_world(tmp_path)
    try:
        request=models.ResultCheckConclusionSave(ref,models.ResultCheckEndReason.RESULT_BUDGET_EXHAUSTED,
            result_event,None,AttemptConfig(1,Decimal('0.125'),Decimal('0')),None,None,runtime.wall_us())
        key=new_operation_key();before=_fingerprint(owned.connection)
        run=owned.connection.execute('SELECT * FROM operation_runs WHERE id=?',(run_id,)).fetchone()
        class Command:
            def plan(self,scope):
                plan=ConcludeResultCheckCommand(request,key).plan(scope)
                events=plan.events
                if damage.startswith('omit_'):
                    index={'omit_config':0,'omit_close':1,'omit_decision':2}[damage]
                    events=events[:index]+events[index+1:]
                    scope.allocation=replace(scope.allocation,last_event_id=scope.allocation.first_event_id+len(events)-1)
                    events=tuple(replace(event,event_id=scope.allocation.first_event_id+index)
                        for index,event in enumerate(events))
                elif damage=='close_only_no_scope':
                    events=(replace(events[1],event_id=scope.allocation.first_event_id),)
                    scope.allocation=replace(scope.allocation,last_event_id=scope.allocation.first_event_id)
                elif damage=='wrong_config':
                    event=events[0];row=event.rows[0]
                    row=replace(row,after=replace(row.after,values={**row.after.values,'timeout_s_json':Decimal('7')}))
                    events=(replace(event,rows=(row,)),*events[1:])
                group=None if damage in ('no_scope','close_only_no_scope') else plan.result_check_write_scope
                if bounded:
                    def context(event,transaction):
                        if damage=='false_range':
                            transaction=replace(transaction,first_event_id=event.event_id,last_event_id=event.event_id)
                        return EventContext(transaction,plan.owners,plan.state_rows,
                            current_reads=SqlEventReads(scope.connection,plan.segment_read_scope))
                    return BoundedCommandPlan(events,context,None,lambda connection:None,result_check_write_scope=group)
                if damage=='false_range':
                    # 有限入口上下文由内核建立；篡改真实分配范围也不能制造单事件证明。
                    scope.allocation=replace(scope.allocation,last_event_id=scope.allocation.first_event_id)
                return replace(plan,events=events,result_check_write_scope=group)
        receipt=commit_operation(Command(),key,owned)
        assert receipt.kind=='rolled_back',receipt.error
        assert _fingerprint(owned.connection)==before
        assert owned.connection.execute('SELECT * FROM operation_runs WHERE id=?',(run_id,)).fetchone()==run
        assert owned.connection.execute('SELECT COUNT(*) FROM segment_check_conclusions').fetchone()==(0,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('bounded',[False,True])
@pytest.mark.parametrize('cut',['before','after'])
async def test_result_decision_unknown_restores_original_complete_group_from_fresh_connection(tmp_path,monkeypatch,bounded,cut):
    import sqlite3
    from pathlib import Path
    from camctl.persistence.repositories import segment_check_conclusions as storage
    from camctl.persistence.runtime import DbConfig,DbOpenMode,open_existing
    from .test_segment_transaction_batches import ConnectionProbe,_fingerprint
    owned,runtime,repository,ref,run_id,result_event=await _partial_result_world(tmp_path)
    closed=False
    try:
        path=Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
        request=models.ResultCheckConclusionSave(ref,models.ResultCheckEndReason.RESULT_BUDGET_EXHAUSTED,
            result_event,None,AttemptConfig(1,Decimal('0.125'),Decimal('0')),None,None,runtime.wall_us())
        key=new_operation_key()
        def hook(stage,statement,parameters):
            if statement=='COMMIT' and stage==cut:raise sqlite3.OperationalError('决定完整组提交回执未知')
        receipt=commit_operation(_command(request,key,bounded),key,
            replace(owned,connection=ConnectionProbe(owned.connection,hook)))
        assert receipt.kind=='unknown',receipt.error
        owned.connection.close();closed=True
        fresh=open_existing(path,DbOpenMode.EXISTING_RO,DbConfig())
        try:
            before=_fingerprint(fresh.connection)
            restored=repository.verify_result_check_conclusion(request,key,fresh)
            assert (restored is not None)==(cut=='after')
            assert _fingerprint(fresh.connection)==before
        finally:fresh.connection.close()
        fresh=open_existing(path,DbOpenMode.EXISTING_RW,DbConfig())
        try:
            if cut=='after':
                def forbidden(*args):raise AssertionError('原决定恢复不能重新分配身份')
                monkeypatch.setattr(storage,'next_row_id',forbidden)
            saved=_ok(repository.conclude_result_check(request,key,fresh))
            assert cut=='before' or saved==restored
            assert fresh.connection.execute('SELECT COUNT(*) FROM segment_check_conclusions').fetchone()==(1,)
            assert fresh.connection.execute('SELECT status FROM operation_runs WHERE id=?',(run_id,)).fetchone()==(6,)
            assert fresh.connection.execute('SELECT status FROM actions WHERE id=?',(ref.action_id,)).fetchone()==(2,)
        finally:fresh.connection.close()
    finally:
        if not closed:owned.connection.close()


@pytest.mark.parametrize('changed',[False,True])
async def test_result_decision_history_preserves_config_run_and_conclusion_at_same_boundary(tmp_path,changed):
    from pathlib import Path
    from camctl.persistence.repositories.history import HistoryRepository
    from .test_segment_result_versions import _snapshot_action
    owned,runtime,repository,ref,run_id,result_event=await _partial_result_world(tmp_path,maximum=3 if changed else 1)
    try:
        path=Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
        await _snapshot_action(path,ref.action_id)
        history=HistoryRepository(path)
        original=history.current_boundary()
        request=models.ResultCheckConclusionSave(ref,models.ResultCheckEndReason.RESULT_BUDGET_EXHAUSTED,
            result_event,None,AttemptConfig(1,Decimal('1.25'),Decimal('2')),None,None,runtime.wall_us())
        saved=_ok(repository.conclude_result_check(request,new_operation_key(),owned))
        boundary=history.current_boundary()
        for selected in (original,boundary):
            for strategy in ('initial','current','snapshot'):
                with history.open_segment_history(ref.action_id,selected,strategy=strategy,batch_size=1) as handle:
                    conclusion=handle.read_page('segment_check_conclusions',ref=ref,limit=1)
                    assert len(conclusion.items)==int(selected==boundary)
                    if selected==boundary:
                        assert conclusion.items[0].row_id==saved.conclusion_id
                        assert conclusion.items[0].values['anchor_event_id']==result_event
                    runs=handle.read_page('operation_runs',limit=128)
                    run=next(row.values for row in runs.items if row.row_id==run_id)
                    assert run['status']==(6 if selected==boundary else 2)
                    assert run['max_attempts_used']==(1 if selected==boundary or not changed else 3)
                    assert run['timeout_s_json']==Decimal('1.25') and run['retry_interval_s_json']==Decimal('2')
                    assert handle.primary()['status']==2
        await _snapshot_action(path,ref.action_id)
        with history.open_segment_history(ref.action_id,boundary,strategy='snapshot',batch_size=1) as handle:
            assert handle.read_page('segment_check_conclusions',ref=ref,limit=1).items[0].row_id==saved.conclusion_id
    finally:
        owned.connection.close()


@pytest.mark.parametrize('bounded',[False,True])
@pytest.mark.parametrize('damage',['not_exhausted','wrong_anchor','missing_timeout','missing_interval','wrong_version'])
async def test_exhaustion_requires_actual_last_result_and_complete_adopted_budget(tmp_path,bounded,damage):
    owned,runtime,repository,ref,run_id,result_event=await _partial_result_world(tmp_path)
    try:
        request=models.ResultCheckConclusionSave(ref,models.ResultCheckEndReason.RESULT_BUDGET_EXHAUSTED,
            result_event,None,AttemptConfig(1,Decimal('0.125'),Decimal('0')),None,None,runtime.wall_us())
        if damage=='not_exhausted':request=replace(request,check_config=replace(request.check_config,max_attempts=2))
        elif damage=='wrong_anchor':request=replace(request,anchor_event_id=owned.connection.execute(
            'SELECT intent_event_id FROM operation_attempts WHERE run_id=?',(run_id,)).fetchone()[0])
        elif damage=='missing_timeout':request=replace(request,check_config=replace(request.check_config,timeout_s=None))
        elif damage=='missing_interval':request=replace(request,check_config=replace(request.check_config,retry_interval_s=None))
        else:request=replace(request,ref=replace(ref,version=ref.version+1))
        key=new_operation_key()
        receipt=commit_operation(_command(request,key,bounded),key,owned)
        assert receipt.kind=='rolled_back',receipt.error
        assert owned.connection.execute('SELECT COUNT(*) FROM segment_check_conclusions').fetchone()==(0,)
    finally:
        owned.connection.close()

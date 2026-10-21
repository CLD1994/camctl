"""C2 取消未固定目标的 C1；不执行 C1 原先尚未解析的目标。"""
from dataclasses import replace

import pytest

from camctl.cancellation.models import ApplyCancelTarget,CancelApplyMode,FinishCancelAction
from camctl.cancellation.service import ApplyCancel,CancellationRuntime,apply_cancel,OriginSettle,settle_origin_cancel
from camctl.cancellation.settlement import TargetSettlement
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository
from .test_hasselblad_preview_fixed_boundary import world,close,accept_origin
from .test_hasselblad_fixed_request_scope import fixed

pytestmark=pytest.mark.asyncio


def runtime_for(owned,repository,runtime):
    return CancellationRuntime(owned,repository,TargetSettlement(owned,OutputsRepository(),repository,
        lambda _:'unknown',runtime.wall_us),runtime.wall_us)


def mark_c1(cfg,owned,runtime):
    origin,repository=accept_origin(cfg,owned,runtime,{'action_instance_id':'3'})
    result=repository.fix_cancel_targets(fixed(origin,runtime,[(3,1,1)]),new_operation_key(),owned)
    assert result.kind is DbOutcomeKind.COMPLETED,result.error
    applied=repository.apply_cancel_target(ApplyCancelTarget(result.value.item_ids[0],CancelApplyMode.WITH_STOP,
        runtime.wall_us()),new_operation_key(),owned)
    assert applied.kind is DbOutcomeKind.COMPLETED,applied.error
    return repository


async def test_real_c2_cancels_running_unfixed_c1_without_touching_its_targets(tmp_path,monkeypatch):
    cfg,owned,driver,runtime,transport,task,repository=await world(tmp_path,'ready',monkeypatch)
    try:
        origin,repository=accept_origin(cfg,owned,runtime,{'action_instance_id':'3'})
        result=repository.fix_cancel_targets(fixed(origin,runtime,[(3,1,1)]),new_operation_key(),owned)
        assert result.kind is DbOutcomeKind.COMPLETED,result.error
        progress=await apply_cancel(ApplyCancel(origin,result.value.item_ids),runtime_for(owned,repository,runtime))
        assert [item.status for item in progress.items]==[3]
        assert runtime.action(3)['status']==6 and runtime.action(3)['target_selection_state']==1
        assert owned.connection.execute('SELECT id FROM cancel_items WHERE action_id=3').fetchall()==[]
        assert owned.connection.execute('SELECT id FROM operation_runs').fetchall()==[]
        assert runtime.action(1)['cancel_requested']==0 and runtime.action(2)['cancel_requested']==0
        before=tuple(owned.connection.iterdump())
        again=await settle_origin_cancel(OriginSettle(3),runtime_for(owned,repository,runtime))
        assert again.kind is DbOutcomeKind.COMPLETED and again.value.succeeded==again.value.failed==0
        assert tuple(owned.connection.iterdump())==before
    finally:
        await close(owned,transport,task)


@pytest.mark.parametrize('fault',['pending_members','fixed_empty','failed_empty','read_failure'])
async def test_finish_itself_rechecks_complete_pending_empty_members(tmp_path,monkeypatch,fault):
    import sqlite3
    cfg,owned,driver,runtime,transport,task,repository=await world(tmp_path,'ready',monkeypatch)
    try:
        repository=mark_c1(cfg,owned,runtime)
        if fault=='pending_members':
            owned.connection.execute('INSERT INTO cancel_items(id,action_id,target_action_id,selection_basis,status,cancellation_effect) VALUES (99,3,1,1,1,1)')
        elif fault=='fixed_empty':
            owned.connection.execute('UPDATE actions SET target_selection_state=2 WHERE id=3')
        elif fault=='failed_empty':
            # SQL CHECK 禁止持久化该组合；在行读取边界验证矛盾拒绝。
            from camctl.persistence.repositories import cancellation as module
            original=module.row_facts
            def contradictory(connection,table,identity):
                row=original(connection,table,identity)
                return {**row,'target_selection_state':3} if table=='actions' and identity==3 else row
            monkeypatch.setattr(module,'row_facts',contradictory)
        owned.connection.commit()
        before=tuple(owned.connection.iterdump())
        if fault=='read_failure':
            owned.connection.set_authorizer(lambda operation,table,*rest:sqlite3.SQLITE_DENY
                if operation==sqlite3.SQLITE_READ and table=='cancel_items' else sqlite3.SQLITE_OK)
        try: result=repository.finish_origin_canceled(FinishCancelAction(3,runtime.wall_us()),new_operation_key(),owned)
        finally: owned.connection.set_authorizer(None)
        assert result.kind is not DbOutcomeKind.COMPLETED
        assert tuple(owned.connection.iterdump())==before
        assert runtime.action(3)['status']==2 and runtime.action(1)['cancel_requested']==0
    finally:
        await close(owned,transport,task)


@pytest.mark.parametrize('committed',[False,True])
async def test_empty_finish_reuses_original_key_and_time_after_unknown(tmp_path,monkeypatch,committed):
    from camctl.persistence.repositories import cancellation as module
    from camctl.persistence.transaction import WriteReceipt
    cfg,owned,driver,runtime,transport,task,repository=await world(tmp_path,'ready',monkeypatch)
    try:
        repository=mark_c1(cfg,owned,runtime)
        command=FinishCancelAction(3,runtime.wall_us())
        key=new_operation_key()
        original=module.commit_operation
        def lost_receipt(planner,operation_key,connection):
            if committed:
                saved=original(planner,operation_key,connection)
                assert saved.kind=='completed',saved.error
                return replace(saved,kind='unknown',error=OSError('receipt'))
            return WriteReceipt(kind='unknown',error=OSError('receipt'))
        with monkeypatch.context() as patch:
            patch.setattr(module,'commit_operation',lost_receipt)
            assert repository.finish_origin_canceled(command,key,owned).kind is DbOutcomeKind.UNKNOWN
        result=repository.finish_origin_canceled(command,key,owned)
        assert result.kind is DbOutcomeKind.COMPLETED,result.error
        assert result.value.succeeded==result.value.failed==0
        before=tuple(owned.connection.iterdump())
        assert repository.finish_origin_canceled(command,key,owned).kind is DbOutcomeKind.COMPLETED
        assert repository.finish_origin_canceled(replace(command,occurred_at=command.occurred_at+1),key,owned).kind is not DbOutcomeKind.COMPLETED
        assert tuple(owned.connection.iterdump())==before
        assert runtime.action(1)['cancel_requested']==0
    finally:
        await close(owned,transport,task)

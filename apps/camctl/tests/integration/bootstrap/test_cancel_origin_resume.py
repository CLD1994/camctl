"""真实 cancel_flow 重入以新仓储和连接核实原收场阶段。"""
from types import SimpleNamespace
from pathlib import Path

import pytest

from camctl.bootstrap.flows import cancel_flow,_resolve_and_fix
from camctl.persistence.models import DbOutcome,DbOutcomeKind
from camctl.session.service import StateDbFailure
from camctl.persistence.repositories.cancellation import CancellationRepository
from camctl.persistence.runtime import open_existing,DbOpenMode,DbConfig
from ..cancellation.test_hasselblad_preview_fixed_boundary import world,close
from ..cancellation.test_unfixed_cancel_origin import mark_c1

pytestmark=pytest.mark.asyncio


@pytest.mark.parametrize('phase',['pending_finish','fixed_stop','fixed_finish'])
@pytest.mark.parametrize('receipt',['before_commit','after_commit','exception'])
async def test_real_flow_resumes_original_phase_before_current_terminal(tmp_path,monkeypatch,phase,receipt):
    from camctl.cancellation.settlement import TargetSettlement
    cfg,owned,driver,runtime,transport,task,repository=await world(tmp_path,'ready',monkeypatch)
    try:
        if phase!='pending_finish':
            raw=owned.connection.execute('SELECT input_fields_json FROM actions WHERE id=3').fetchone()[0]
            _resolve_and_fix(owned,repository,3,raw,runtime.wall_us)
        mark_c1(cfg,owned,runtime)
        time=[runtime.wall_us()]
        opened=[]
        def connect():
            value=open_existing(Path(cfg.paths.state_db),DbOpenMode.EXISTING_RW,DbConfig())
            opened.append(value)
            return value
        context=SimpleNamespace(open_connection=connect,clock=SimpleNamespace(utc_micros=lambda:time[0]))
        flow=cancel_flow(ready=tmp_path/'ready',processing=tmp_path/'processing',start_entries=runtime.start_entries)
        selected='stop_wait_cancel_items' if phase=='fixed_stop' else 'finish_origin_canceled'
        original=getattr(CancellationRepository,selected)
        calls=[]
        lost=[False]
        def lose_receipt(self,request,key,connection):
            calls.append((request,key,connection,self))
            if not lost[0]:
                lost[0]=True
                if receipt=='exception': raise OSError('interrupted')
                if receipt=='after_commit':
                    result=original(self,request,key,connection)
                    assert result.kind is DbOutcomeKind.COMPLETED,result.error
                return DbOutcome(DbOutcomeKind.UNKNOWN,error=OSError('receipt'))
            return original(self,request,key,connection)
        monkeypatch.setattr(CancellationRepository,selected,lose_receipt)
        containers=[]
        initialize=TargetSettlement.__init__
        def track(self,*args,**kwargs):
            if kwargs.get('pending_origin_settlements') is not None:
                containers.append(kwargs['pending_origin_settlements'])
            initialize(self,*args,**kwargs)
        monkeypatch.setattr(TargetSettlement,'__init__',track)
        with pytest.raises((ValueError,OSError)): await flow(context)
        assert len(calls)==1
        time[0]+=1000
        await flow(context)
        assert len(calls)==2 and calls[1][:2]==calls[0][:2]
        assert calls[1][2] is not calls[0][2] and calls[1][3] is not calls[0][3]
        assert containers and all(value is containers[0] for value in containers) and containers[0]=={}
        assert runtime.action(3)['status']==6
        assert runtime.action(1)['cancel_requested']==int(phase!='pending_finish')
        assert runtime.action(2)['cancel_requested']==0
        assert owned.connection.execute('SELECT id FROM operation_runs WHERE kind IN (1,2)').fetchall()==[]
        before=tuple(owned.connection.iterdump())
        await flow(context)
        assert len(calls)==2 and tuple(owned.connection.iterdump())==before
    finally:
        await close(owned,transport,task)


async def test_flow_rejects_other_database_then_resumes_original_pending_finish(tmp_path,monkeypatch):
    (tmp_path/'first').mkdir()
    (tmp_path/'second').mkdir()
    cfg,owned,driver,runtime,transport,task,repository=await world(tmp_path/'first','ready',monkeypatch)
    other=None
    try:
        mark_c1(cfg,owned,runtime)
        path=[cfg.paths.state_db]
        context=SimpleNamespace(open_connection=lambda:open_existing(Path(path[0]),DbOpenMode.EXISTING_RW,DbConfig()),
            clock=SimpleNamespace(utc_micros=runtime.wall_us))
        flow=cancel_flow(ready=tmp_path/'ready',processing=tmp_path/'processing',start_entries=runtime.start_entries)
        original=CancellationRepository.finish_origin_canceled
        calls=[]
        def unknown(self,request,key,connection):
            calls.append((request,key,connection.metadata.instance_id))
            if len(calls)==1: return DbOutcome(DbOutcomeKind.UNKNOWN,error=OSError('receipt'))
            return original(self,request,key,connection)
        monkeypatch.setattr(CancellationRepository,'finish_origin_canceled',unknown)
        with pytest.raises(ValueError): await flow(context)
        other=await world(tmp_path/'second','ready',monkeypatch)
        path[0]=other[0].paths.state_db
        before=tuple(other[1].connection.iterdump())
        with pytest.raises(StateDbFailure): await flow(context)
        assert len(calls)==1 and tuple(other[1].connection.iterdump())==before
        path[0]=cfg.paths.state_db
        await flow(context)
        assert len(calls)==2 and calls[0]==calls[1]
    finally:
        if other is not None: await close(other[1],other[4],other[5])
        await close(owned,transport,task)

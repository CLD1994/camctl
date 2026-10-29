"""仅目标测试的取消前观察；不更改业务预算、返回值或同步点。"""
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import inspect
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import traceback
from urllib.parse import quote

import pytest_asyncio

_OUT = Path(os.environ['CAMCTL_EXIT_PROBE'])

def emit(event, **values):
    with _OUT.open('a') as out:
        out.write(json.dumps({'utc':datetime.now(timezone.utc).isoformat(),'event':event,**values},ensure_ascii=False,default=str)+'\n')

def task_brief(task):
    if task is None:
        return None
    result={'id':id(task),'name':task.get_name(),'done':task.done(),'cancelled':task.cancelled(),'cancelling':task.cancelling()}
    if task.done() and not task.cancelled():
        try:
            error=task.exception()
            result['error']=None if error is None else repr(error)
        except BaseException as error:
            result['inspection_error']=repr(error)
    return result

def owners(context):
    group=getattr(context,'local_work',None)
    values=[]
    for owner in getattr(group,'_owners',()):
        item={'id':id(owner),'type':type(owner).__qualname__}
        try:item['required']=owner.required_settlements()
        except BaseException as error:item['required_error']=repr(error)
        for name in ('_task','_stopping','_active','_preparation_at'):
            value=getattr(owner,name,None)
            if isinstance(value,asyncio.Task):value=task_brief(value)
            if value is not None:item[name]=value
        item['flow']=getattr(getattr(owner,'_flow',None),'__qualname__',None)
        for name in ('pending_media_results','_pending_results','pending_results','_devices','_pending','_handed_off'):
            value=getattr(owner,name,None)
            if isinstance(value,(dict,list,tuple,set)):
                item[name+'_count']=len(value)
        if isinstance(getattr(owner,'_devices',None),dict):
            item['devices']={str(k):{'task':task_brief(getattr(v,'_task',None)),'required':v.required_settlements()} for k,v in owner._devices.items()}
        round_owner=getattr(owner,'_round',None)
        if round_owner is not None:
            item['round']={'type':type(round_owner).__qualname__,'active':getattr(round_owner,'_active',None),'pending_lengths':[len(x) for x in getattr(round_owner,'_pending',())]}
        background=getattr(owner,'_background',None)
        if background is not None:item['background']={'task':task_brief(getattr(background,'_task',None)),'required':background.required_settlements()}
        values.append(item)
    return values

def await_chain(coro):
    result=[]; seen=set()
    while coro is not None and id(coro) not in seen and len(result)<32:
        seen.add(id(coro)); frame=getattr(coro,'cr_frame',None) or getattr(coro,'gi_frame',None) or getattr(coro,'ag_frame',None)
        item={'awaitable_type':type(coro).__name__}
        if frame is not None:
            item.update(file=frame.f_code.co_filename,line=frame.f_lineno,function=frame.f_code.co_name)
            if frame.f_code.co_name in ('drive','_run_session','_wait_for_more_work'):
                item['selected_locals']={key:repr(frame.f_locals[key])[:300] for key in ('decision','outcome','observed_wake','observed','wait_s') if key in frame.f_locals}
        result.append(item)
        coro=getattr(coro,'cr_await',None) or getattr(coro,'gi_yieldfrom',None) or getattr(coro,'ag_await',None)
    return result

def queue_state(database):
    executor=database.executor
    with executor._lock:
        core=executor._core
        result={'business':[{'key':str(j.key),'description':j.description,'kind':j.kind.value} for j in core._business], 'snapshot':[{'key':str(j.key),'description':j.description,'kind':j.kind.value} for j in core._snapshot], 'dispatched':[str(k) for k in core._dispatched], 'admissions':len(core._admissions),'results':{str(k):{'done':v.done(),'cancelled':v.cancelled()} for k,v in executor._results.items()},'stopping':executor._stopping,'closed':executor._closed}
    thread=executor._thread
    if thread is not None:
        result['thread']={'name':thread.name,'ident':thread.ident,'alive':thread.is_alive()}
        frame=sys._current_frames().get(thread.ident)
        if frame is not None:
            result['thread_stack']=[{'file':f.filename,'line':f.lineno,'function':f.name,'source':f.line} for f in traceback.extract_stack(frame)[-24:]]
            current=frame
            while current is not None:
                job=current.f_locals.get('job')
                if job is not None and hasattr(job,'description'):
                    result['observed_job']={'key':str(job.key),'description':job.description,'kind':job.kind.value}
                    break
                current=current.f_back
    return result

def stored_state(path):
    from camctl.bootstrap.application import query_work_facts
    from camctl.session.work import classify_work
    connection=sqlite3.connect('file:'+quote(str(path),safe='/')+'?mode=ro',uri=True,timeout=1)
    try:
        connection.execute('PRAGMA query_only=ON');connection.execute('BEGIN')
        facts=query_work_facts(connection)
        data={'facts':asdict(facts),'decision':classify_work(facts).kind.value,'actions':connection.execute('SELECT id,plan_id,type,status FROM actions ORDER BY id').fetchall(),'deliveries':connection.execute('SELECT status,COUNT(*) FROM deliveries GROUP BY status').fetchall(),'reports':connection.execute('SELECT id,status,to_wm FROM reports ORDER BY id').fetchall()}
        connection.rollback();return data
    finally:connection.close()

@pytest_asyncio.fixture(autouse=True)
async def probe(request,monkeypatch):
    if request.node.name!='test_submit_during_initial_discovery_is_not_lost':
        yield;return
    from camctl.bootstrap import lifecycle
    from camctl.session import service
    from camctl.session.work import classify_work
    state={'context':None,'facts_signature':None,'last_fact_emit':0,'round':0}
    real_drive=service._drive_flows
    async def drive(context):
        state['context']=context
        return await real_drive(context)
    monkeypatch.setattr(service,'_drive_flows',drive)
    real_facts=service._run_work_facts
    async def facts(context):
        value=await real_facts(context);state['round']+=1
        dimensions=asdict(value);signature=json.dumps(dimensions,sort_keys=True)
        now=time.monotonic()
        if signature!=state['facts_signature'] or now-state['last_fact_emit']>=2:
            emit('session_work_facts',round=state['round'],facts=dimensions,decision=classify_work(value).kind.value,owners=owners(context))
            state.update(facts_signature=signature,last_fact_emit=now)
        return value
    monkeypatch.setattr(service,'_run_work_facts',facts)
    real_close=service._try_close
    async def close(*args,**kwargs):
        emit('close_admission_started',owners=owners(args[0]));value=await real_close(*args,**kwargs);emit('close_admission_finished',outcome=repr(value));return value
    monkeypatch.setattr(service,'_try_close',close)
    real_execute=lifecycle.execute_command
    async def execute(deps,*args,**kwargs):
        async def monitor():
            last=0
            for at in (2,10,20,28):
                await asyncio.sleep(at-last);last=at
                context=state['context']
                snapshot={'target_elapsed':at,'round':state['round'],'tasks':[{**task_brief(t),'chain':await_chain(t.get_coro()),'waiter':repr(getattr(t,'_fut_waiter',None))[:300]} for t in asyncio.all_tasks()]}
                if context is not None:
                    snapshot['owners']=owners(context)
                    snapshot['queue']=queue_state(context.database)
                emit('before_cancel_snapshot',**snapshot)
                try:emit('before_cancel_stored_state',target_elapsed=at,state=await asyncio.to_thread(stored_state,deps.state_db))
                except BaseException as error:emit('stored_state_error',target_elapsed=at,error=repr(error))
        task=asyncio.create_task(monitor(),name='test-exit-probe')
        emit('execute_started')
        try:return await real_execute(deps,*args,**kwargs)
        finally:
            task.cancel()
            try:await task
            except asyncio.CancelledError:pass
            emit('execute_finished')
    monkeypatch.setattr(lifecycle,'execute_command',execute)
    yield

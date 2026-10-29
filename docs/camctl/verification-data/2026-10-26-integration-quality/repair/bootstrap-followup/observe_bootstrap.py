import asyncio
from collections import Counter, deque
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import time
import pytest

BASE_OUT = Path(__file__).parent
OUT = BASE_OUT
active = None
started = None
facts = deque(maxlen=128)

def task_fact(task):
    return dict(id=id(task), name=task.get_name(), done=task.done(), cancelled=task.cancelled(),
        coro=repr(task.get_coro()), stack=[dict(file=f.f_code.co_filename, line=f.f_lineno, function=f.f_code.co_name) for f in task.get_stack()])

def sample(label):
    if active is None:
        return
    from camctl.bootstrap.application import query_work_facts
    record = dict(label=label, elapsed=time.monotonic()-started, state_db=str(active.state_db),
        flows=[dict(type=type(f).__name__, id=id(f), stopping=getattr(f, '_stopping', None),
            handoff=getattr(f, '_completion_handoff', None), required=f.required_settlements(),
            task=None if getattr(f, '_task', None) is None else task_fact(f._task)) for f in active.background_flows],
        tasks=[task_fact(t) for t in asyncio.all_tasks()], actual_session_facts=list(facts))
    connection=sqlite3.connect(active.state_db)
    try:
        record['persisted_work_facts']=asdict(query_work_facts(connection))
        record['rows']={table:connection.execute('SELECT * FROM '+table).fetchall() for table in
            ('actions','operation_runs','operation_attempts','file_copies','deliveries','obtain_items','reports')}
        backup=sqlite3.connect(OUT/(label+'.sqlite'))
        try:
            connection.backup(backup)
        finally:
            backup.close()
    finally:
        connection.close()
    (OUT/(label+'.json')).write_text(json.dumps(record,ensure_ascii=False,indent=2,default=repr)+'\n')

@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_call(item):
    global OUT
    if not item.name.startswith(("test_default_obtain_reaches_candidates_beyond_first_batch", "test_cancel_during_discovery_preserves_active_read_settlement")):
        yield
        return
    OUT=BASE_OUT/item.name
    OUT.mkdir(exist_ok=True)
    facts.clear()
    from camctl.bootstrap import lifecycle
    from camctl.session import service
    original=lifecycle.execute_command
    original_facts=service._run_work_facts
    async def observe_facts(context):
        result=await original_facts(context)
        value=asdict(result)
        if not facts or facts[-1]['facts']!=value:
            facts.append(dict(elapsed=time.monotonic()-started,facts=value))
        return result
    async def execute(deps,*args,**kwargs):
        global active, started
        active, started=deps,time.monotonic()
        loop=asyncio.get_running_loop()
        handles=[loop.call_later(delay,sample,'at-'+str(delay)) for delay in (5,25,29)]
        try:
            return await original(deps,*args,**kwargs)
        finally:
            for handle in handles:
                handle.cancel()
            sample('finished')
    lifecycle.execute_command=execute
    service._run_work_facts=observe_facts
    try:
        yield
    finally:
        lifecycle.execute_command=original
        service._run_work_facts=original_facts

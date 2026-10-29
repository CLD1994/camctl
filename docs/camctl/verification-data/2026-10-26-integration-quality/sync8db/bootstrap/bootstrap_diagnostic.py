"""只观察真实执行；不改变任务、返回值、预算或业务输入。"""
import asyncio
from collections import Counter
from dataclasses import asdict
import json
import os
from pathlib import Path
import time

BASE = Path(os.environ['BOOTSTRAP_PROGRESS_PATH']).parent
COUNTS = Counter()
LAST = 0


def record(event, **facts):
    with (BASE / 'diagnostic.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'monotonic':time.monotonic(), 'event':event, **facts}, ensure_ascii=False, default=str)+'\n')


def pytest_configure(config):
    from camctl.bootstrap.background_flow import BackgroundFlow
    from camctl.session import service
    original_run = BackgroundFlow._run
    original_consume = BackgroundFlow._consume
    original_facts = service._run_work_facts

    async def run(self, context):
        label = getattr(self._flow, '__qualname__', type(self._flow).__qualname__)
        COUNTS[('started', label)] += 1
        result = await original_run(self, context)
        COUNTS[('completed', label)] += 1
        return result

    def consume(self):
        label = getattr(self._flow, '__qualname__', type(self._flow).__qualname__)
        COUNTS[('consumed', label)] += 1
        return original_consume(self)

    async def facts(context):
        global LAST
        result = await original_facts(context)
        now = time.monotonic()
        if now - LAST >= 2:
            LAST = now
            owners = []
            for owner in getattr(context.local_work, '_owners', ()):
                task = getattr(owner, 'unconsumed_task', None)
                owners.append({'class':type(owner).__qualname__, 'required':owner.required_settlements(), 'flow':getattr(getattr(owner,'_flow',None),'__qualname__',None), 'task_done':None if task is None else task.done()})
            record('work-facts', facts=asdict(result), owners=owners, counters={'|'.join(key):value for key,value in COUNTS.items()}, tasks=[{'name':task.get_name(),'done':task.done(),'cancelling':task.cancelling(),'frames':[f'{frame.f_code.co_filename}:{frame.f_lineno}:{frame.f_code.co_name}' for frame in task.get_stack()]} for task in asyncio.all_tasks()])
        return result

    BackgroundFlow._run = run
    BackgroundFlow._consume = consume
    service._run_work_facts = facts


def pytest_runtest_call(item):
    from integration.bootstrap import test_output_binding_changes as bindings
    original_read = bindings.Driver.open_read
    original_accept = bindings._accept
    # 目标模块保存的是同一受理函数引用，分别观察两个正式测试入口。
    original_target_accept = item.module._accept

    async def read(self, *args, **kwargs):
        result = await original_read(self, *args, **kwargs)
        record('read-opened', reads=len(self.reads), bindings=self.reads)
        return result

    def accept(owned, request_id, actions):
        result = original_target_accept(owned, request_id, actions)
        record('input-accepted', request_id=request_id, actions=len(actions), read_count=None)
        return result

    bindings.Driver.open_read = read
    item.module._accept = accept

import asyncio
import json
import pathlib
import sys
import tempfile

repo = pathlib.Path('/tmp/camctl-full-272f-session-observer')
sys.path[:0] = [str(repo / 'apps/camctl/src'), str(repo / 'apps/camctl/tests')]

from integration.session.test_local_work_observer_control import (
    test_helper_observer_and_real_typed_control_keep_first_identity,
)


async def main():
    created = []
    original = asyncio.create_task

    def record(coro, **kwargs):
        task = original(coro, **kwargs)
        created.append((coro.cr_code.co_name, task))
        return task

    asyncio.create_task = record
    with tempfile.TemporaryDirectory(prefix='camctl-observer-context-red-') as tmp:
        test = original(test_helper_observer_and_real_typed_control_keep_first_identity(
            pathlib.Path(tmp), 'stop', 'single', False, False))
        for _ in range(100):
            await asyncio.sleep(0)
            observers = [task for name, task in created if name == '_settle_local_work']
            if observers and observers[0].done():
                break
        assert len(observers) == 1 and observers[0].done()
        observer = observers[0]
        error = observer.exception()
        frame = test.get_coro().cr_frame
        result = {
            'observer_done': observer.done(),
            'exception_type': type(error).__name__,
            'exception': str(error),
            'test_done': test.done(),
            'entered': frame.f_locals['entered'].is_set(),
            'swept': frame.f_locals['swept'].is_set(),
            'finished': frame.f_locals['finished'].is_set(),
            'calls': frame.f_locals['calls'],
            'test_waiting_line': frame.f_lineno,
        }
        assert isinstance(error, AttributeError) and 'snapshot_owner' in str(error)
        assert result['entered'] and not result['swept'] and not result['test_done']
        print(json.dumps(result, ensure_ascii=False), flush=True)
        frame.f_locals['release'].set()
        test.cancel('bounded reproduction cleanup')
        await asyncio.gather(test, return_exceptions=True)
        await asyncio.gather(*(task for _, task in created), return_exceptions=True)
        print(json.dumps({'all_created_tasks_done': all(task.done() for _, task in created),
                          'test_done_after_cleanup': test.done()}), flush=True)


asyncio.run(main())

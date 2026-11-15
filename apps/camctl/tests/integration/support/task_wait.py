"""等待测试门闩，同时观察负责推进业务的原任务。"""
import asyncio
import sys


async def wait_for_signal(signal, task, *, timeout=30):
    waiter = asyncio.create_task(signal.wait())
    try:
        done, _ = await asyncio.wait((waiter, task), timeout=timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
            raise AssertionError("原任务在到达测试门闩前结束")
        if waiter not in done:
            raise TimeoutError("等待测试门闩超时")
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def settle_tasks(*tasks, timeout=30):
    tasks = tuple(task for task in tasks if task is not None)
    if not tasks:
        return
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in done:
        if not task.cancelled():
            task.exception()
    if pending:
        error = TimeoutError("放开测试门闩后原任务未在期限内结束")
        active = sys.exception()
        if active is not None:
            active.add_note(str(error))
        else:
            raise error

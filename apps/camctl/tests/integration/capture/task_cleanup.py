"""收场测试持有的异步任务，并保留业务异常。"""

import sys

from ..support.task_wait import settle_tasks


async def settle_capture_task(task, *, timeout=30):
    active = sys.exception()
    await settle_tasks(task, timeout=timeout)
    if task is None or not task.done():
        return
    try:
        await task
    except BaseException as error:
        if active is None:
            raise
        if error is not active:
            active.add_note(f"收场任务失败：{error!r}")


def close_observer(owned):
    active = sys.exception()
    try:
        owned.connection.close()
    except BaseException as error:
        if active is None:
            raise
        active.add_note(f"关闭观察连接失败：{error!r}")

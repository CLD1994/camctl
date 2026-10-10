"""取回独立推进与会话实际收场；拥有者持有任务直到结果交付。"""

from __future__ import annotations

import asyncio


class BackgroundFlow:
    """一次只推进一个流程，允许设备调度在长读取期间继续。"""

    def __init__(self, flow):
        self._flow = flow
        self._task: asyncio.Task | None = None
        self._stopping = False

    def _consume(self):
        task, self._task = self._task, None
        return task.result()

    async def __call__(self, context):
        if self._task is not None:
            if self._task.done():
                self._consume()
            return
        if self._stopping:
            return
        self._task = asyncio.create_task(self._flow(context))
        # 只让执行体开始；实际等待及连接生命周期由原流程拥有。
        await asyncio.sleep(0)
        if self._task.done():
            self._consume()

    def required_settlements(self):
        # 已形成的失败也须交付，不能先被会话解释为空闲。
        return int(self._task is not None)

    def stop_new_work(self):
        if self._stopping:
            return
        self._stopping = True
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def settle(self):
        task = self._task
        if task is None:
            return
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not (self._stopping and task.cancelled()):
                raise
        finally:
            if task.done() and self._task is task:
                self._task = None


class CombinedLocalWork:
    """先收场读取及其连接，再收场依赖文件责任，保留每项失败。"""

    def __init__(self, owners):
        self._owners = tuple(owners)

    def required_settlements(self):
        return sum(owner.required_settlements() for owner in self._owners)

    def stop_new_work(self):
        for owner in self._owners:
            owner.stop_new_work()

    async def settle(self):
        failures = []
        for owner in self._owners:
            try:
                await owner.settle()
            except Exception as error:
                failures.append(error)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise ExceptionGroup("本地实际责任收场失败", failures)

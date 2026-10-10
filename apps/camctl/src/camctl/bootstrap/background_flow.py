"""取回独立推进与会话实际收场；拥有者持有任务直到结果交付。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True)
class _FlowResult:
    error: BaseException | None = None


class BackgroundFlow:
    """一次只推进一个流程，允许设备调度在长读取期间继续。"""

    def __init__(self, flow):
        self._flow = flow
        self._task: asyncio.Task | None = None
        self._stopping = False

    def _consume(self):
        task, self._task = self._task, None
        try:
            result = task.result()
        except asyncio.CancelledError:
            # 尚未进入执行体就被拥有者停止，没有已开始的实际责任。
            if self._stopping:
                return
            raise
        error = result.error
        if isinstance(error, asyncio.CancelledError) and self._stopping:
            # 文件拥有者将实际执行或保存失败作为原取消的结构化原因交付。
            error = error.__cause__
        if error is not None:
            raise error

    async def _run(self, context):
        try:
            await self._flow(context)
            return _FlowResult()
        except BaseException as error:
            # shield 不传递内部取消异常；把原异常作为结果保存到消费时。
            return _FlowResult(error)

    async def __call__(self, context):
        if self._task is not None:
            if self._task.done():
                self._consume()
            return
        if self._stopping:
            return
        self._task = asyncio.create_task(self._run(context))
        # 只让执行体开始；实际等待及连接生命周期由原流程拥有。
        await asyncio.sleep(0)
        if self._task.done():
            self._consume()

    def check_completed(self):
        if self._task is not None and self._task.done():
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
        if not task.cancelled():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not (task.cancelled() and not asyncio.current_task().cancelling()):
                    raise
        # 外部等待取消时不消费，即使原任务已经完成也保留未交付结果。
        if self._task is task:
            self._consume()


class CombinedLocalWork:
    """先收场读取及其连接，再收场依赖文件责任，保留每项失败。"""

    def __init__(self, owners):
        self._owners = tuple(owners)

    def required_settlements(self):
        return sum(owner.required_settlements() for owner in self._owners)

    def stop_new_work(self):
        for owner in self._owners:
            owner.stop_new_work()

    def check_completed(self):
        for owner in self._owners:
            check = getattr(owner, "check_completed", None)
            if check is not None:
                check()

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

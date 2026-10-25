"""取回独立推进与会话实际收场；拥有者持有任务直到结果交付。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class _FlowResult:
    error: BaseException | None = None


class BackgroundFlow:
    """一次只推进一个流程，允许设备调度在长读取期间继续。"""

    def __init__(self, flow):
        self._flow = flow
        self._task: asyncio.Task | None = None
        self._stopping = False

    @property
    def unconsumed_task(self):
        """只观察现有未交付任务；不改变任务或结果的所有权。"""
        return self._task

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


class ReportBackgroundFlow(BackgroundFlow):
    """报告先公开前提错误，实际任务继续拥有日志与文件收场。"""

    owns_report_error_boundary = True

    def __init__(self, flow):
        from camctl.session.service import _advance_report_flow

        self._fatal_error: Exception | None = None
        self._interrupted_error: asyncio.CancelledError | None = None
        self._file_step_result = None
        self._maintenance_flow = flow
        self._probe_error: Exception | None = None

        async def maintain(context):
            error, self._probe_error = self._probe_error, None
            if error is not None:
                raise error
            await flow(context)

        async def advance(context):
            await _advance_report_flow(context, maintain, on_fatal=self._notice_fatal)

        super().__init__(advance)

    @property
    def interrupted_error(self):
        """已收场的原取消诊断；实际文件结果不随任务消费丢失。"""
        return self._interrupted_error

    @property
    def file_step_result(self):
        """停止期间当前文件阶段的实际结果，不授权继续发布。"""
        return self._file_step_result

    def _notice_fatal(self, error):
        self._fatal_error = error

    def _consume(self):
        task = self._task
        if not task.cancelled():
            error = task.result().error
            if isinstance(error, asyncio.CancelledError):
                self._interrupted_error = error
                self._file_step_result = getattr(error, "report_file_result", None)
        try:
            super()._consume()
        finally:
            self._fatal_error = None

    def check_completed(self):
        super().check_completed()
        if self._fatal_error is not None:
            raise self._fatal_error

    async def __call__(self, context):
        if self._task is None and not self._stopping:
            probe = getattr(self._maintenance_flow, "should_advance", None)
            if probe is not None:
                try:
                    if not probe(context):
                        return
                except Exception as error:
                    # 观察错误仍经原后台报告边界交付；日志等待不扣住调度。
                    self._probe_error = error
        await super().__call__(context)
        self.check_completed()


class CombinedLocalWork:
    """先收场读取及其连接，再收场依赖文件责任，保留每项失败。"""

    def __init__(self, owners):
        self._owners = tuple(owners)
        self._settlement_runs: list[
            tuple[Any, asyncio.Task[tuple[BaseException, ...]]]
        ] = []

    def required_settlements(self):
        return sum(owner.required_settlements() for owner in self._owners)

    def stop_new_work(self):
        failures = []
        for owner in self._owners:
            try:
                owner.stop_new_work()
            except BaseException as error:
                failures.append(error)
        self._raise_failures(tuple(failures))

    def check_completed(self):
        for owner in self._owners:
            check = getattr(owner, "check_completed", None)
            if check is not None:
                check()

    async def settle(self):
        await self._settle(excluded_owner=None)

    async def settle_excluding(self, excluded_owner):
        """在既有 owner 顺序中收场，但暂不消费指定 owner。"""
        await self._settle(excluded_owner=excluded_owner)

    async def _settle(self, *, excluded_owner):
        task = self._settlement_task(excluded_owner)
        observer_cancel: asyncio.CancelledError | None = None
        try:
            while True:
                try:
                    failures = await asyncio.shield(task)
                    break
                except asyncio.CancelledError as error:
                    if task.done() and task.cancelled():
                        raise
                    if observer_cancel is None:
                        observer_cancel = error

            if observer_cancel is not None:
                self._attach_failures(observer_cancel, failures)
                raise observer_cancel
            self._raise_failures(failures)
        finally:
            if task.done():
                self._settlement_runs = [
                    (exclusion, active_task)
                    for exclusion, active_task in self._settlement_runs
                    if active_task is not task
                ]

    def _settlement_task(
            self, excluded_owner: Any) -> asyncio.Task[tuple[BaseException, ...]]:
        for existing_exclusion, task in self._settlement_runs:
            if existing_exclusion is excluded_owner:
                return task

        async def collect() -> tuple[BaseException, ...]:
            failures: list[BaseException] = []
            for owner in self._owners:
                if owner is excluded_owner:
                    continue
                try:
                    await owner.settle()
                except BaseException as error:
                    failures.append(error)
            return tuple(failures)

        task = asyncio.create_task(collect(), name="camctl-local-work-settlement")
        self._settlement_runs.append((excluded_owner, task))
        return task

    @staticmethod
    def _attach_failures(
            target: BaseException, failures: tuple[BaseException, ...]) -> None:
        for failure in failures:
            if failure is target:
                continue
            notes = tuple(getattr(failure, "__notes__", ()))
            target.add_note(
                "本地实际责任收场失败: "
                f"{type(failure).__name__}: {failure}")
            for note in notes:
                target.add_note(f"本地实际责任收场失败附注: {note}")

    @staticmethod
    def _raise_failures(failures: tuple[BaseException, ...]) -> None:
        if not failures:
            return
        control = next(
            (failure for failure in failures if not isinstance(failure, Exception)),
            None,
        )
        if control is not None:
            CombinedLocalWork._attach_failures(
                control,
                tuple(failure for failure in failures if failure is not control),
            )
            raise control.with_traceback(control.__traceback__)
        if len(failures) == 1:
            raise failures[0].with_traceback(failures[0].__traceback__)
        raise ExceptionGroup("本地实际责任收场失败", list(failures))

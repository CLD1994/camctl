"""持有一次设备检查及其实际结果的会话内交接。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from camctl.logging_runtime.diagnostics import DiagnosticState
from camctl.operations.owned_calls import owned_tool_call
from camctl.persistence.business import SharedSave


@dataclass
class AvailabilityAttemptOwner:
    """同一尝试只调用一次；结果保存由所有等待者共同接手。"""

    ticket: Any
    invoke: Callable[[], Awaitable[Any]]
    save: Callable[[Any, DiagnosticState], Awaitable[Any]]
    cancelled_result: Callable[[asyncio.CancelledError], Any | None]
    actual: Any = None
    entered: bool = False
    saved: bool = False
    task: asyncio.Task | None = None
    diagnostic: DiagnosticState = field(default_factory=DiagnosticState)
    result_save: SharedSave = field(default_factory=SharedSave)

    async def save_actual(self, *, retry: bool = False):
        if self.actual is None or self.saved:
            return
        if retry:
            self.result_save.retry_failed()

        async def save_and_apply():
            await self.save(self.actual, self.diagnostic)
            self.saved = True

        await self.result_save.run(save_and_apply)

    @owned_tool_call
    async def _call(self):
        self.entered = True
        interrupted = None
        try:
            self.actual = await self.invoke()
        except asyncio.CancelledError as error:
            self.actual = self.cancelled_result(error)
            interrupted = error
        if self.actual is not None:
            await self.save_actual()
        if interrupted is not None:
            raise interrupted
        if self.actual is None:
            raise ValueError("设备检查没有交付实际结果")

    async def advance(self):
        if self.task is None:
            self.task = asyncio.create_task(self._call())
        await asyncio.shield(self.task)

    def stop(self):
        if self.entered and self.task is not None and not self.task.done():
            self.task.cancel()

    async def settle(self):
        failure = None
        if self.task is not None:
            try:
                await asyncio.shield(self.task)
            except asyncio.CancelledError as error:
                if not self.task.done():
                    raise
                failure = error
            except Exception as error:
                failure = error
        if self.actual is not None and not self.saved:
            await self.save_actual(retry=True)
        if failure is not None and not isinstance(failure, asyncio.CancelledError):
            raise failure

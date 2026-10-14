"""日志文件责任拥有者的队列撤回与实际任务收场。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

from camctl.logging_runtime.copies import (
    CopyRequest,
    FailureLogService,
    MarkerStore,
    RunFileOwnership,
    _LoggingOperationStopped,
)
from camctl.logging_runtime.models import LogFileStage, LogLevel
from camctl.logging_runtime.ownership import LoggingOwner
from camctl.logging_runtime.service import LogRecord
from camctl.session.supervision import Supervisor


_ORDINARY_STAGES = tuple(stage for stage in LogFileStage if not stage.is_startup)


@dataclass
class _QueuedCall:
    future: asyncio.Future[Any]
    operation: Callable[..., Any]
    args: tuple[Any, ...]


class _MemoryExecutor:
    """只暂存 run_in_executor 回调，由测试显式选择其开始时点。"""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self.calls: list[_QueuedCall] = []
        self.queued = asyncio.Event()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def submit(_executor, operation, *args):
            future = self._loop.create_future()
            self.calls.append(_QueuedCall(future, operation, args))
            self.queued.set()
            return future

        monkeypatch.setattr(self._loop, "run_in_executor", submit)

    def start(self, index: int = 0) -> None:
        call = self.calls[index]
        if call.future.done():
            return
        try:
            result = call.operation(*call.args)
        except BaseException as error:
            call.future.set_exception(error)
        else:
            call.future.set_result(result)


def _logging_owner(
        supervisor: Supervisor | None = None,
        ownership: RunFileOwnership | None = None) -> LoggingOwner:
    async def diagnose(_message: str) -> None:
        return None

    return LoggingOwner(
        logs_dir=Path("unused"),
        ownership=ownership or RunFileOwnership(),
        supervisor=supervisor or Supervisor(),
        owner_settled=False,
        diagnose=diagnose,
    )


async def _wait(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), timeout=1)


@pytest.mark.parametrize(
    "stage", _ORDINARY_STAGES, ids=lambda stage: stage.name.lower())
@pytest.mark.asyncio
async def test_owned_stage_cancellation_preserves_queued_and_started_results(
        stage: LogFileStage, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = asyncio.get_running_loop()
    executor = _MemoryExecutor(loop)
    executor.install(monkeypatch)
    owner = _logging_owner()
    calls: list[str] = []
    sentinel = object()
    queued_task: asyncio.Task | None = None

    async def queued_case() -> None:
        nonlocal queued_task
        queued_task = asyncio.create_task(owner.owned_call(
            stage, lambda: calls.append("queued")))
        await _wait(executor.queued)
        owner.stop_new_work()
        executor.start()
        with pytest.raises(_LoggingOperationStopped) as stopped:
            await queued_task
        assert type(stopped.value) is _LoggingOperationStopped
        assert stopped.value.stage is stage
        assert calls == []

        # 停止后还未提交的下一普通阶段也不进入 executor。
        submitted = len(executor.calls)
        with pytest.raises(_LoggingOperationStopped) as next_stopped:
            await owner.owned_call(stage, lambda: calls.append("next"))
        assert type(next_stopped.value) is _LoggingOperationStopped
        assert next_stopped.value.stage is stage
        assert len(executor.calls) == submitted
        assert calls == []

    # 每个枚举阶段都用独立 owner 验证“已开始”调用返回真实结果。
    started_executor: _MemoryExecutor | None = None
    started_task: asyncio.Task | None = None
    try:
        await queued_case()
        started_owner = _logging_owner()
        started_executor = _MemoryExecutor(loop)
        started_executor.install(monkeypatch)
        started_calls: list[object] = []

        def started_operation() -> object:
            started_calls.append(sentinel)
            started_owner.stop_new_work()
            return sentinel

        started_task = asyncio.create_task(started_owner.owned_call(
            stage, started_operation))
        await _wait(started_executor.queued)
        started_executor.start()
        assert await started_task is sentinel
        assert started_calls == [sentinel]

        submitted = len(started_executor.calls)
        with pytest.raises(_LoggingOperationStopped) as next_stopped:
            await started_owner.owned_call(stage, lambda: None)
        assert type(next_stopped.value) is _LoggingOperationStopped
        assert next_stopped.value.stage is stage
        assert len(started_executor.calls) == submitted
    finally:
        for executor_to_drain in (executor, started_executor):
            if executor_to_drain is None:
                continue
            for index, call in enumerate(executor_to_drain.calls):
                if not call.future.done():
                    executor_to_drain.start(index)
        if queued_task is not None and not queued_task.done():
            await asyncio.wait_for(
                asyncio.gather(queued_task, return_exceptions=True), timeout=1)
        if started_task is not None and not started_task.done():
            await asyncio.wait_for(
                asyncio.gather(started_task, return_exceptions=True), timeout=1)


@pytest.mark.asyncio
async def test_required_cleanup_runs_when_queued_before_stop(
        monkeypatch: pytest.MonkeyPatch) -> None:
    loop = asyncio.get_running_loop()
    executor = _MemoryExecutor(loop)
    executor.install(monkeypatch)
    ownership = RunFileOwnership()
    owner = _logging_owner(ownership=ownership)
    cleanup_calls: list[str] = []

    task = asyncio.create_task(owner.owned_call(
        LogFileStage.COPY_CLEANUP,
        lambda: cleanup_calls.append("cleaned") or "cleanup-result",
        required=True,
    ))
    try:
        await _wait(executor.queued)
        owner.stop_new_work()
        executor.start()
        assert await task == "cleanup-result"
        assert cleanup_calls == ["cleaned"]
    finally:
        for index, call in enumerate(executor.calls):
            if not call.future.done():
                executor.start(index)
        if not task.done():
            await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True), timeout=1)


class _UnusedCopyChannel:
    def copy_with_write(self, _record: LogRecord, _destination: Path) -> tuple:
        raise AssertionError("注入的交付失败不应使用复制通道")


@pytest.mark.asyncio
async def test_internal_cancelled_error_is_settled_without_canceling_supervisor(
) -> None:
    supervisor = Supervisor()
    diagnosed_by_owner: list[str] = []

    async def owner_diagnose(message: str) -> None:
        diagnosed_by_owner.append(message)

    ownership = RunFileOwnership()
    owner = LoggingOwner(
        logs_dir=Path("unused"),
        ownership=ownership,
        supervisor=supervisor,
        owner_settled=False,
        diagnose=owner_diagnose,
    )
    marker = MarkerStore(Path("unused"), ownership=ownership)
    request = CopyRequest(
        trigger=LogRecord(level=LogLevel.ERROR, message="controlled failure"),
        staging_root=Path("unused/staging"),
        ready_dir=Path("unused/ready"),
        channel=_UnusedCopyChannel(),
        marker=marker,
    )

    delivery_started = asyncio.Event()
    release_delivery = asyncio.Event()
    diagnostic_started = asyncio.Event()
    release_diagnostic = asyncio.Event()
    inner_cancel = asyncio.CancelledError("actual log operation canceled")
    delivery_calls = 0
    service_diagnostics: list[BaseException | None] = []

    async def deliver(_request: CopyRequest):
        nonlocal delivery_calls
        delivery_calls += 1
        delivery_started.set()
        await release_delivery.wait()
        raise inner_cancel

    service = FailureLogService(marker, deliver=deliver)
    service.bind_owner(owner)

    async def diagnose(_result, failure: BaseException | None) -> None:
        service_diagnostics.append(failure)
        diagnostic_started.set()
        await release_diagnostic.wait()

    parent = asyncio.create_task(owner.run_service_operation(
        "report_failure",
        lambda: service.on_report_failure_with_factory(
            lambda: request, diagnose=diagnose),
    ))
    drain: asyncio.Task | None = None
    try:
        await _wait(delivery_started)
        parent.cancel("first caller cancellation")
        with pytest.raises(asyncio.CancelledError) as external:
            await parent
        assert external.value.args == ("first caller cancellation",)
        assert delivery_calls == 1
        assert owner.required_settlements() == 1
        assert len(supervisor.outstanding()) == 1

        drain = asyncio.create_task(supervisor.drain_required())
        release_delivery.set()
        await _wait(diagnostic_started)
        assert service_diagnostics == [inner_cancel]
        assert service_diagnostics[0] is inner_cancel
        assert not drain.done()

        release_diagnostic.set()
        result = await asyncio.wait_for(asyncio.shield(drain), timeout=1)
        assert len(result.settled) == 1
        assert result.failed == ()
        assert owner.required_settlements() == 0
        assert delivery_calls == 1

        repeated = await supervisor.drain_required()
        assert repeated.settled == () and repeated.failed == ()
        assert len(service_diagnostics) == 1
        assert diagnosed_by_owner == []
    finally:
        release_delivery.set()
        release_diagnostic.set()
        if not parent.done():
            parent.cancel("test cleanup")
            await asyncio.wait_for(
                asyncio.gather(parent, return_exceptions=True), timeout=1)
        if drain is not None and not drain.done():
            await asyncio.wait_for(
                asyncio.gather(drain, return_exceptions=True), timeout=1)
        elif owner.required_settlements():
            await asyncio.wait_for(supervisor.drain_required(), timeout=1)

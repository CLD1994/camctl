"""O3 受管工具期限、停止与实际退出的单元测试。

进程与输出经受真实接口约束的替身提供；发出信号不等于退出，宽
限只决定升级，重复取消不续期，部分输出不延长期限。
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from camctl.operations.process import (
    LocalExit,
    RawToolOutcome,
    StopSignal,
    ToolSpec,
    execute_tool,
)
from camctl.operations import process as process_module

pytestmark = pytest.mark.asyncio

GRACE = Decimal("0.02")


def _spec(
    *, timeout_s: Decimal | None = None, grace: Decimal = GRACE
) -> ToolSpec:
    return ToolSpec(
        argv=("tool", "--flag"),
        timeout_s=timeout_s,
        terminate_grace_s=grace,
    )


class FakeProcess:
    """受受管进程端口约束的替身：信号与退出分别控制。"""

    def __init__(self, output: bytes = b"") -> None:
        self.output = output
        self.output_failure = None
        self._exited: asyncio.Future[LocalExit] | None = None
        self.terminate_requests = 0
        self.kill_requests = 0

    def request_terminate(self) -> None:
        self.terminate_requests += 1

    def request_kill(self) -> None:
        self.kill_requests += 1

    async def wait(self) -> LocalExit:
        if self._exited is None:
            loop = asyncio.get_running_loop()
            self._exited = loop.create_future()
        return await self._exited

    async def wait_output(self) -> None:
        return None

    def exit(self, exit_code: int | None = None, signal: int | None = None) -> None:
        if self._exited is None:
            self._exited = asyncio.get_event_loop().create_future()
        if not self._exited.done():
            self._exited.set_result(LocalExit(exit_code=exit_code, signal=signal))


class FakeStop:
    """停止信号替身：重复请求不产生新事件。"""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def request(self) -> None:
        self._event.set()

    async def requested(self) -> None:
        await self._event.wait()


async def _drive(
    spec: ToolSpec,
    process: FakeProcess,
    stop: FakeStop | None = None,
) -> asyncio.Task[RawToolOutcome]:
    return asyncio.ensure_future(
        execute_tool(
            spec, stop=stop or FakeStop(), spawner=lambda _: _ready(process)
        )
    )


async def _ready(process: FakeProcess) -> FakeProcess:
    return process


async def _within(predicate, budget: float = 1.0) -> None:
    """轮询等待谓词成立；预算内不成立即失败，不固定睡眠节拍。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("等待谓词超时")
        await asyncio.sleep(0.001)


async def test_signal_is_not_exit() -> None:
    process = FakeProcess()
    stop = FakeStop()
    task = await _drive(_spec(), process, stop)
    stop.request()
    await _within(lambda: process.terminate_requests == 1)
    # 信号已发送但进程未退出：调用未结束，is_finished 语义上为 False。
    assert not task.done()
    process.exit(exit_code=3)
    outcome = await task
    assert outcome.exit == LocalExit(exit_code=3)
    assert outcome.error == "cancelled"
    assert outcome.used_grace_s == GRACE


async def test_already_exited_sends_no_signal() -> None:
    process = FakeProcess()
    process.exit(exit_code=0)
    stop = FakeStop()
    task = await _drive(_spec(), process, stop)
    stop.request()
    outcome = await task
    assert process.terminate_requests == 0
    assert process.kill_requests == 0
    assert outcome.used_grace_s is None
    # 调用在取消请求前已正常完成，不把取消补写为调用错误。
    assert outcome.error is None
    assert outcome.exit == LocalExit(exit_code=0)


async def test_grace_exit_continues_without_kill() -> None:
    process = FakeProcess()
    stop = FakeStop()
    task = await _drive(_spec(), process, stop)
    stop.request()
    await _within(lambda: process.terminate_requests == 1)
    process.exit(exit_code=0)
    outcome = await task
    assert process.terminate_requests == 1
    assert process.kill_requests == 0
    assert outcome.used_grace_s == GRACE
    assert outcome.error == "cancelled"


async def test_grace_expiry_escalates_and_waits_for_exit() -> None:
    process = FakeProcess()
    stop = FakeStop()
    task = await _drive(_spec(grace=Decimal("0.01")), process, stop)
    stop.request()
    # 宽限到期升级强制终止，但退出确认前调用仍不结束。
    await _within(lambda: process.kill_requests == 1)
    assert not task.done()
    process.exit(signal=9)
    outcome = await task
    assert outcome.exit == LocalExit(signal=9)
    assert outcome.used_grace_s == Decimal("0.01")


async def test_repeated_cancel_does_not_extend_grace() -> None:
    process = FakeProcess()
    stop = FakeStop()
    task = await _drive(_spec(grace=Decimal("0.01")), process, stop)
    stop.request()
    stop.request()
    await _within(lambda: process.kill_requests == 1)
    assert process.terminate_requests == 1
    process.exit(exit_code=0)
    await task


async def test_timeout_uses_same_termination_flow() -> None:
    process = FakeProcess(output=b"partial")
    task = await _drive(
        _spec(timeout_s=Decimal("0.01"), grace=Decimal("0.01")), process
    )
    await _within(lambda: process.kill_requests == 1)
    assert process.terminate_requests == 1
    process.exit(exit_code=0)
    outcome = await task
    assert outcome.error == "timeout"
    # 部分输出已取得但不延长调用总期限。
    assert outcome.output == b"partial"
    assert outcome.used_grace_s == Decimal("0.01")


async def test_normal_exit_has_no_error_and_bounded_output() -> None:
    process = FakeProcess(output=b"ok")
    task = await _drive(_spec(), process)
    process.exit(exit_code=0)
    outcome = await task
    assert outcome.error is None
    assert outcome.exit == LocalExit(exit_code=0)
    assert outcome.output == b"ok"
    assert process.terminate_requests == 0


def test_local_exit_is_exclusive() -> None:
    assert LocalExit(exit_code=0).exit_code == 0
    assert LocalExit(signal=9).signal == 9
    with pytest.raises(ValueError):
        LocalExit()
    with pytest.raises(ValueError):
        LocalExit(exit_code=0, signal=9)


@pytest.mark.parametrize("phase", ["terminate", "kill", "both"])
@pytest.mark.parametrize("trigger", ["cancelled", "timeout", "output_failed"])
async def test_signal_errors_keep_waiting_for_actual_exit(phase, trigger) -> None:
    class FailingSignals(FakeProcess):
        def request_terminate(self):
            super().request_terminate()
            if phase in ("terminate", "both"):
                raise PermissionError("terminate denied")

        def request_kill(self):
            super().request_kill()
            if phase in ("kill", "both"):
                raise PermissionError("kill denied")

    process = FailingSignals()
    process.output_failure = "pipe read failed"
    stop = FakeStop()
    task = await _drive(_spec(timeout_s=Decimal(0) if trigger == "timeout" else None), process, stop)
    if trigger == "cancelled":
        stop.request()
    try:
        await _within(lambda: task.done() or process.kill_requests == 1)
        assert not task.done(), "发送信号失败不能结束尚在运行的工具调用"
    finally:
        process.exit(exit_code=7)
        results = await asyncio.gather(task, return_exceptions=True)
    outcome = results[0]
    assert isinstance(outcome, RawToolOutcome)
    assert outcome.exit == LocalExit(exit_code=7)
    assert outcome.error == trigger
    assert outcome.output_failure == "pipe read failed"
    assert outcome.used_grace_s == GRACE
    expected = ["terminate", "kill"] if phase == "both" else [phase]
    assert [failure.stage.value for failure in outcome.signal_failures] == expected
    assert all("denied" in failure.message for failure in outcome.signal_failures)
    assert process.terminate_requests == process.kill_requests == 1


async def test_exit_during_terminate_retains_exit_result() -> None:
    class ExitedDuringSignal(FakeProcess):
        def request_terminate(self):
            super().request_terminate()
            self.exit(exit_code=0)
            raise ProcessLookupError("already exited")

    process = ExitedDuringSignal(output=b"complete")
    stop = FakeStop()
    task = await _drive(_spec(), process, stop)
    stop.request()
    outcome = await task
    assert outcome.exit == LocalExit(exit_code=0)
    assert outcome.output == b"complete"
    assert outcome.error == "cancelled"
    assert process.kill_requests == 0
    assert len(outcome.signal_failures) == 1
    assert "already exited" in outcome.signal_failures[0].message


@pytest.mark.parametrize("fails", [False, True])
async def test_exit_completion_joins_stop_watcher(fails) -> None:
    started, closed = asyncio.Event(), asyncio.Event()

    class WatchingStop:
        async def requested(self):
            started.set()
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                closed.set()

    class CompletingProcess(FakeProcess):
        async def wait(self):
            await started.wait()
            if fails:
                raise OSError("wait failed")
            return LocalExit(exit_code=0)

    task = await _drive(_spec(), CompletingProcess(), WatchingStop())
    if fails:
        with pytest.raises(OSError, match="wait failed"):
            await task
    else:
        await task
    assert closed.is_set()


@pytest.mark.parametrize("error", [FileNotFoundError("missing"), PermissionError("denied")])
async def test_start_failure_is_distinct_and_preserves_cause(error) -> None:
    async def fail_start(spec):
        raise error

    with pytest.raises(OSError) as caught:
        await execute_tool(_spec(), stop=FakeStop(), spawner=fail_start)
    assert type(caught.value).__name__ == "ToolStartError"
    assert caught.value.__cause__ is error


@pytest.mark.parametrize("cancelled", [False, True])
async def test_output_read_failure_retains_prefix_and_actual_exit(cancelled):
    class BrokenStream:
        def __init__(self):
            self.calls = 0

        async def read(self, size):
            self.calls += 1
            if self.calls == 1:
                return b"known-prefix"
            if cancelled:
                await local.terminated.wait()
            raise OSError("pipe read failed")

    class LocalProcess:
        def __init__(self):
            self.exited = asyncio.Event()
            self.terminated = asyncio.Event()

        async def wait(self):
            await self.exited.wait()
            return 7

        def terminate(self):
            self.terminated.set()

        def kill(self):
            self.exited.set()

    local = LocalProcess()
    reader = asyncio.create_task(process_module._read_bounded(BrokenStream(), 1024))
    handle = process_module._SubprocessHandle(local, reader)
    stop = FakeStop()
    task = await _drive(_spec(), handle, stop)
    try:
        if cancelled:
            stop.request()
        await asyncio.wait_for(local.terminated.wait(), timeout=1)
        assert not task.done()
    finally:
        local.exited.set()
    outcome = await task
    assert outcome.exit == LocalExit(exit_code=7)
    assert outcome.output == b"known-prefix"
    assert "pipe read failed" in outcome.output_failure
    assert outcome.error == ("cancelled" if cancelled else "output_failed")


async def test_exit_before_output_failure_waits_for_output_without_signalling():
    output_started, release = asyncio.Event(), asyncio.Event()

    class LateOutput(FakeProcess):
        async def wait_output(self):
            output_started.set()
            await release.wait()
            self.output_failure = "late read failure"

    process = LateOutput(b"prefix")
    process.exit(exit_code=0)
    task = await _drive(_spec(), process)
    await output_started.wait()
    assert not task.done()
    release.set()
    outcome = await task
    assert outcome.exit == LocalExit(exit_code=0)
    assert outcome.output == b"prefix"
    assert outcome.error == "output_failed"
    assert outcome.output_failure == "late read failure"
    assert outcome.used_grace_s is None
    assert process.terminate_requests == process.kill_requests == 0

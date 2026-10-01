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

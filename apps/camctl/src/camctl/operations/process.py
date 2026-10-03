"""受管工具启动、期限及实际回收。

工具保持所属 camctl 进程组，统一经一个受管启动边界执行。发出信
号不等于退出：宽限只决定何时升级强制终止，实际退出确认前调用
不结束。重复取消与部分输出都不刷新期限；正常等待期限与宽限分
别计量。stdout 只作受约束输出返回，不进入 CLI 结果。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Awaitable, Callable, Literal, Protocol, runtime_checkable

__all__ = [
    "LocalExit",
    "ManagedProcess",
    "RawToolOutcome",
    "SignalFailure",
    "SignalStage",
    "StopSignal",
    "ToolError",
    "ToolSpec",
    "ToolStartError",
    "execute_tool",
    "spawn_subprocess",
]

#: 受约束输出的捕获上限；超出部分丢弃，只保留前缀。
OUTPUT_LIMIT_BYTES = 1 << 20

ToolError = Literal["timeout", "cancelled", "output_failed"]
#: 调用错误：正常期限、外部停止或输出读取失败；组合原因另存详情。


class ToolStartError(OSError):
    """启动边界未取得进程句柄；原始系统错误由异常链保留。"""


class SignalStage(StrEnum):
    TERMINATE = "terminate"
    KILL = "kill"


@dataclass(frozen=True)
class SignalFailure:
    stage: SignalStage
    message: str


@dataclass(frozen=True)
class ToolSpec:
    """一次受管工具调用：argv、正常期限及本地终止宽限。

    timeout_s 为 None 表示没有正常等待期限；停止或输出错误仍可终止工具。工具
    与包装程序共用此启动路径；期限与宽限以精确秒数表达。
    """

    argv: tuple[str, ...]
    timeout_s: Decimal | None
    terminate_grace_s: Decimal

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError("argv 不能为空")
        grace = self.terminate_grace_s
        if (
            isinstance(grace, bool)
            or not isinstance(grace, Decimal)
            or not grace.is_finite()
            or grace <= 0
        ):
            raise ValueError(f"terminate_grace_s 必须是有限正秒数: {grace!r}")
        if self.timeout_s is not None:
            timeout = self.timeout_s
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, Decimal)
                or not timeout.is_finite()
                or timeout < 0
            ):
                raise ValueError(f"timeout_s 必须是有限非负秒数: {timeout!r}")


@dataclass(frozen=True)
class LocalExit:
    """实际取得的本地退出结果：正常退出码或终止信号，恰好其一。"""

    exit_code: int | None = None
    signal: int | None = None

    def __post_init__(self) -> None:
        if (self.exit_code is None) == (self.signal is None):
            raise ValueError("退出码与终止信号必须恰好提供其一")


@dataclass(frozen=True)
class RawToolOutcome:
    """一次受管调用的实际结果：退出、受约束输出及错误分区。

    execute_tool 返回即本地收场完成（退出已确认）；used_grace_s
    仅在进入终止宽限等待时携带实际采用值，未使用不补造。
    信号发送与输出读取错误独立保存，不改变已发生的终止触发原因。
    """

    exit: LocalExit | None
    output: bytes | None
    error: ToolError | None
    used_grace_s: Decimal | None
    signal_failures: tuple[SignalFailure, ...] = ()
    output_failure: str | None = None


class StopSignal(Protocol):
    """停止请求端口：等待取消不结束实际调用。"""

    async def requested(self) -> None:
        ...


@runtime_checkable
class ManagedProcess(Protocol):
    """受管进程端口：信号、退出与输出结束分别观察。

    wait 仅在确认实际退出后返回；输出的系统错误由 output_failure
    表达，不从 wait 或 wait_output 抛出。输出字段在 wait_output
    完成后可读。停止由调用方发信号，不直接取消这两个观察任务。
    """

    @property
    def output(self) -> bytes | None: ...

    @property
    def output_failure(self) -> str | None: ...

    async def wait(self) -> LocalExit: ...

    async def wait_output(self) -> None: ...

    def request_terminate(self) -> None: ...

    def request_kill(self) -> None: ...


Spawner = Callable[[ToolSpec], Awaitable[ManagedProcess]]


async def execute_tool(
    spec: ToolSpec,
    *,
    stop: StopSignal,
    spawner: Spawner | None = None,
) -> RawToolOutcome:
    """执行一次受管工具调用并等待实际收场。

    正常退出、期限届满、停止请求与输出错误分别观察；后三者进入同一终止流
    程：已退出不发信号，否则请求终止并在宽限内等待，宽限到期升
    级强制终止，随后仍等待可靠退出。取消不重复发信号，宽限不自
    请求终止时开始后延长。
    """
    try:
        process = await (spawner or spawn_subprocess)(spec)
    except OSError as error:
        raise ToolStartError(f"{type(error).__name__}: {error}") from error
    exit_task = asyncio.ensure_future(process.wait())
    output_task = asyncio.ensure_future(process.wait_output())
    watch: set[asyncio.Future] = {exit_task, output_task}
    timeout_task: asyncio.Task[None] | None = None
    if spec.timeout_s is not None:
        timeout_task = asyncio.ensure_future(asyncio.sleep(float(spec.timeout_s)))
        watch.add(timeout_task)
    stop_task = asyncio.ensure_future(stop.requested())
    watch.add(stop_task)

    try:
        done, _ = await asyncio.wait(watch, return_when=asyncio.FIRST_COMPLETED)
        if output_task in done:
            output_task.result()
            if process.output_failure is None and done == {output_task}:
                # EOF 不表示进程已经退出；避免已结束输出使等待立即反复返回。
                done, _ = await asyncio.wait(watch - {output_task}, return_when=asyncio.FIRST_COMPLETED)
        error: ToolError | None = None
        signal_failures: tuple[SignalFailure, ...] = ()
        if exit_task not in done:
            if timeout_task is not None and timeout_task in done:
                error = "timeout"
            elif stop_task in done:
                error = "cancelled"
            else:
                error = "output_failed"
            used_grace, exit_value, signal_failures = await _terminate_and_wait(
                process, exit_task, spec.terminate_grace_s
            )
        else:
            used_grace = None
            exit_value = exit_task.result()
        await output_task
        output_failure = process.output_failure
        if error is None and output_failure is not None:
            error = "output_failed"
        return RawToolOutcome(
            exit=exit_value,
            output=process.output,
            error=error,
            used_grace_s=used_grace,
            signal_failures=signal_failures,
            output_failure=output_failure,
        )
    finally:
        watchers = [stop_task] + ([timeout_task] if timeout_task is not None else [])
        for watcher in watchers:
            watcher.cancel()
        await asyncio.gather(*watchers, return_exceptions=True)


async def _terminate_and_wait(
    process: ManagedProcess,
    exit_task: asyncio.Future[LocalExit],
    grace: Decimal,
) -> tuple[Decimal | None, LocalExit, tuple[SignalFailure, ...]]:
    """终止流程：宽限只决定升级时机，不猜测退出。"""
    if exit_task.done():
        # 开始终止处理时已经退出：不发信号，不等待宽限。
        return None, exit_task.result(), ()
    failures: list[SignalFailure] = []
    try:
        process.request_terminate()
    except OSError as error:
        failures.append(SignalFailure(SignalStage.TERMINATE, f"{type(error).__name__}: {error}"))
    done, _ = await asyncio.wait({exit_task}, timeout=float(grace))
    if exit_task in done:
        return grace, exit_task.result(), tuple(failures)
    try:
        process.request_kill()
    except OSError as error:
        failures.append(SignalFailure(SignalStage.KILL, f"{type(error).__name__}: {error}"))
    return grace, await exit_task, tuple(failures)


@dataclass(frozen=True)
class _CapturedOutput:
    data: bytes
    error: str | None = None


async def _read_bounded(stream: asyncio.StreamReader | None, limit: int) -> _CapturedOutput:
    if stream is None:
        return _CapturedOutput(b"")
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = await stream.read(65536)
        except OSError as error:
            return _CapturedOutput(b"".join(chunks), f"{type(error).__name__}: {error}")
        if not chunk:
            break
        # 保留上限只限制返回值；继续排空管道，才能让工具写完并实际退出。
        if total < limit:
            retained = chunk[:limit - total]
            chunks.append(retained)
            total += len(retained)
    return _CapturedOutput(b"".join(chunks))


class _SubprocessHandle:
    """asyncio 子进程的受管封装：退出结果与受约束输出。"""

    def __init__(
        self, process: asyncio.subprocess.Process, reader: asyncio.Task[_CapturedOutput]
    ) -> None:
        self._process = process
        self._reader = reader

    @property
    def output(self) -> bytes | None:
        return self._reader.result().data if self._reader.done() else None

    @property
    def output_failure(self) -> str | None:
        return self._reader.result().error if self._reader.done() else None

    async def wait_output(self) -> None:
        await self._reader

    async def wait(self) -> LocalExit:
        code = await self._process.wait()
        if code is not None and code < 0:
            return LocalExit(signal=-code)
        return LocalExit(exit_code=code)

    def request_terminate(self) -> None:
        self._process.terminate()

    def request_kill(self) -> None:
        self._process.kill()


async def spawn_subprocess(spec: ToolSpec) -> _SubprocessHandle:
    """统一受管启动边界：留在本进程组，捕获 stdout 至上限。"""
    process = await asyncio.create_subprocess_exec(
        *spec.argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    reader = asyncio.ensure_future(_read_bounded(process.stdout, OUTPUT_LIMIT_BYTES))
    return _SubprocessHandle(process, reader)

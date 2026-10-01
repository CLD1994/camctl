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
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

__all__ = [
    "LocalExit",
    "ManagedProcess",
    "RawToolOutcome",
    "StopSignal",
    "ToolError",
    "ToolSpec",
    "execute_tool",
    "spawn_subprocess",
]

#: 受约束输出的捕获上限；超出部分丢弃，只保留前缀。
OUTPUT_LIMIT_BYTES = 1 << 20

ToolError = str
#: 触发终止的原因分区：正常等待期限届满或外部取消请求。


@dataclass(frozen=True)
class ToolSpec:
    """一次受管工具调用：argv、正常期限及本地终止宽限。

    timeout_s 为 None 表示没有正常等待期限，仅由取消终止。工具
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
    仅在实际发出终止信号时携带本次采用的宽限，未使用不补造。
    """

    exit: LocalExit | None
    output: bytes | None
    error: ToolError | None
    used_grace_s: Decimal | None


class StopSignal(Protocol):
    """停止请求端口：等待取消不结束实际调用。"""

    async def requested(self) -> None:
        ...


@runtime_checkable
class ManagedProcess(Protocol):
    """受管进程端口：信号请求与退出确认分开。"""

    @property
    def output(self) -> bytes | None: ...

    async def wait(self) -> LocalExit: ...

    def request_terminate(self) -> None: ...

    def request_kill(self) -> None: ...


Spawner = Callable[[ToolSpec], Awaitable[Any]]


async def execute_tool(
    spec: ToolSpec,
    *,
    stop: StopSignal,
    spawner: Spawner | None = None,
) -> RawToolOutcome:
    """执行一次受管工具调用并等待实际收场。

    正常退出、期限届满与取消请求分别触发；后两者进入同一终止流
    程：已退出不发信号，否则请求终止并在宽限内等待，宽限到期升
    级强制终止，随后仍等待可靠退出。取消不重复发信号，宽限不自
    请求终止时开始后延长。
    """
    process = await (spawner or spawn_subprocess)(spec)
    exit_task = asyncio.ensure_future(process.wait())
    watch: set[asyncio.Future[None]] = {exit_task}
    timeout_task: asyncio.Task[None] | None = None
    if spec.timeout_s is not None:
        timeout_task = asyncio.ensure_future(asyncio.sleep(float(spec.timeout_s)))
        watch.add(timeout_task)  # type: ignore[arg-type]
    stop_task = asyncio.ensure_future(stop.requested())
    watch.add(stop_task)  # type: ignore[arg-type]

    done, _ = await asyncio.wait(watch, return_when=asyncio.FIRST_COMPLETED)
    error: ToolError | None = None
    if exit_task not in done:
        if timeout_task is not None and timeout_task in done:
            error = "timeout"
        else:
            error = "cancelled"
        used_grace, exit_value = await _terminate_and_wait(
            process, exit_task, spec.terminate_grace_s
        )
    else:
        used_grace = None
        exit_value = exit_task.result()
    stop_task.cancel()
    if timeout_task is not None:
        timeout_task.cancel()
    return RawToolOutcome(
        exit=exit_value,
        output=getattr(process, "output", None),
        error=error,
        used_grace_s=used_grace,
    )


async def _terminate_and_wait(
    process: Any,
    exit_task: asyncio.Future[LocalExit],
    grace: Decimal,
) -> tuple[Decimal, LocalExit]:
    """终止流程：宽限只决定升级时机，不猜测退出。"""
    if exit_task.done():
        # 开始终止处理时已经退出：不发信号，不等待宽限。
        return None, exit_task.result()  # type: ignore[return-value]
    process.request_terminate()
    done, _ = await asyncio.wait({exit_task}, timeout=float(grace))
    if exit_task in done:
        return grace, exit_task.result()
    process.request_kill()
    return grace, await exit_task


async def _read_bounded(stream: asyncio.StreamReader | None, limit: int) -> bytes:
    if stream is None:
        return b""
    chunks: list[bytes] = []
    total = 0
    while total < limit:
        chunk = await stream.read(min(65536, limit - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


class _SubprocessHandle:
    """asyncio 子进程的受管封装：退出结果与受约束输出。"""

    def __init__(
        self, process: asyncio.subprocess.Process, reader: asyncio.Task[bytes]
    ) -> None:
        self._process = process
        self._reader = reader

    @property
    def output(self) -> bytes | None:
        return self._reader.result() if self._reader.done() else None

    async def wait(self) -> LocalExit:
        code = await self._process.wait()
        await self._reader
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

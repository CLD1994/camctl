"""O3 受管工具生命周期的组件集成测试。

真实子进程、输出管道与统一启动边界：正常退出、超时终止、取消收
场及受约束输出上限。信号与退出分别确认，输出不进入 CLI 结果。
"""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal

import pytest

from camctl.operations.process import (
    OUTPUT_LIMIT_BYTES,
    RawToolOutcome,
    StopSignal,
    ToolSpec,
    execute_tool,
    spawn_subprocess,
)
from ..support.managed_process import ObservedManagedProcess

pytestmark = pytest.mark.asyncio

GRACE = Decimal("0.5")


class _CancelRequest:
    """由测试直接触发的停止信号。"""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def request(self) -> None:
        self._event.set()

    async def requested(self) -> None:
        await self._event.wait()


def _tool(script: str, *, timeout_s: Decimal | None = None) -> ToolSpec:
    return ToolSpec(
        argv=(sys.executable, "-c", script),
        timeout_s=timeout_s,
        terminate_grace_s=GRACE,
    )


async def _run(spec: ToolSpec, stop: StopSignal) -> RawToolOutcome:
    return await execute_tool(spec, stop=stop)


async def _reap(handle) -> None:
    """测试持有的进程必须实际退出并完成两条输出管道的读取。"""
    try:
        handle.request_kill()
    except ProcessLookupError:
        pass
    async with asyncio.timeout(5):
        await handle.wait()
        await handle.wait_output()


async def test_normal_tool_exit_and_output() -> None:
    class Never:
        async def requested(self) -> None:
            await asyncio.Future()

    outcome = await _run(_tool("print('ok')"), Never())
    assert outcome.error is None
    assert outcome.used_grace_s is None
    assert outcome.exit is not None
    assert outcome.exit.exit_code == 0
    assert outcome.output is not None
    assert b"ok" in outcome.output


async def test_timeout_terminates_and_records_grace() -> None:
    class Never:
        async def requested(self) -> None:
            await asyncio.Future()

    outcome = await _run(
        _tool("import time; time.sleep(30)", timeout_s=Decimal("0.1")), Never()
    )
    assert outcome.error == "timeout"
    assert outcome.used_grace_s == GRACE
    # 本地实际退出已确认；具体退出形态随平台，不猜测远端含义。
    assert outcome.exit is not None


@pytest.mark.parametrize("already_exited,gate_fails", [(False, False), (True, False), (False, True)])
async def test_expired_spawn_handoff_reaps_real_child_and_retains_both_channels(tmp_path, already_exited, gate_fails):
    handles = []
    marker = tmp_path / "output-flushed"

    async def delayed_spawn(spec):
        handle = await spawn_subprocess(spec)
        handed_off = False
        try:
            handles.append(handle)
            if already_exited:
                await handle.wait()
                await handle.wait_output()
            else:
                async with asyncio.timeout(5):
                    while not marker.exists():
                        await asyncio.sleep(.001)
            if gate_fails:
                raise RuntimeError("启动门失败")
            await asyncio.sleep(.03)
            handed_off = True
            return handle
        finally:
            if not handed_off:
                await _reap(handle)

    script = ("import sys; from pathlib import Path; print('original stdout',flush=True); "
              "print('original stderr',file=sys.stderr,flush=True); "
              "Path(" + repr(str(marker)) + ").write_text('ready')")
    if not already_exited:
        script += "; import time; time.sleep(30)"
    try:
        if gate_fails:
            with pytest.raises(RuntimeError, match="启动门失败"):
                await execute_tool(_tool(script, timeout_s=Decimal("0.001")),
                                   stop=_CancelRequest(), spawner=delayed_spawn)
            assert len(handles) == 1
            assert await handles[0].wait() is not None
            await handles[0].wait_output()
            assert handles[0].output == b"original stdout\n"
            assert handles[0].stderr == b"original stderr\n"
            return
        raw = await execute_tool(_tool(script, timeout_s=Decimal("0.001")),
                                 stop=_CancelRequest(), spawner=delayed_spawn)
        assert raw.error == "timeout"
        assert raw.output == b"original stdout\n" and raw.stderr == b"original stderr\n"
        assert len(handles) == 1
        assert await handles[0].wait() == raw.exit
        await handles[0].wait_output()
        assert raw.used_grace_s == (None if already_exited else GRACE)
        if already_exited:
            assert raw.exit.exit_code == 0
    finally:
        for handle in handles:
            await _reap(handle)


async def test_cancel_uses_same_termination_flow(tmp_path) -> None:
    stop = _CancelRequest()
    marker = tmp_path / "started"
    script = ("import time; from pathlib import Path; Path(" + repr(str(marker)) +
              ").write_text('started'); time.sleep(30)")
    task = asyncio.create_task(_run(_tool(script), stop))
    try:
        async with asyncio.timeout(5):
            while not marker.exists():
                await asyncio.sleep(.005)
        stop.request()
        outcome = await task
        assert outcome.error == "cancelled"
        assert outcome.used_grace_s == GRACE
        assert outcome.exit is not None
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_canceled_waiter_reaps_real_process_and_finishes_both_output_readers(tmp_path):
    marker = tmp_path / "started"
    handles = []
    async def spawn(spec):
        handle = ObservedManagedProcess(await spawn_subprocess(spec))
        handles.append(handle)
        return handle
    script = ("import sys,time; from pathlib import Path; "
              "print('original stdout',flush=True); print('original stderr',file=sys.stderr,flush=True); "
              "Path(" + repr(str(marker)) + ").write_text('started'); time.sleep(30)")
    task = asyncio.create_task(execute_tool(_tool(script), stop=_CancelRequest(), spawner=spawn))
    try:
        async with asyncio.timeout(5):
            while not marker.exists():
                await asyncio.sleep(.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        raw = caught.value.outcome
        assert raw.exit is not None and raw.error == "cancelled"
        assert b"original stdout" in raw.output and b"original stderr" in raw.stderr
        assert len(handles) == 1
        assert handles[0].exit == raw.exit
        assert handles[0].output_finished
        assert handles[0].output == raw.output and handles[0].stderr == raw.stderr
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_output_captured_up_to_limit() -> None:
    class Never:
        async def requested(self) -> None:
            await asyncio.Future()

    script = (
        "import sys\n"
        f"sys.stdout.write('x' * {OUTPUT_LIMIT_BYTES})\n"
        "sys.stdout.flush()\n"
    )
    outcome = await _run(
        _tool(script, timeout_s=Decimal("5")), Never()
    )
    assert outcome.error is None
    assert outcome.output is not None
    assert len(outcome.output) == OUTPUT_LIMIT_BYTES


async def test_output_beyond_pipe_capacity_is_drained_before_exit() -> None:
    class Never:
        async def requested(self) -> None:
            await asyncio.Future()

    async with asyncio.timeout(5):
        outcome = await _run(
            _tool(
                f"import sys; sys.stdout.buffer.write(b'x' * {OUTPUT_LIMIT_BYTES * 4}); sys.stdout.flush()",
                timeout_s=Decimal("2"),
            ),
            Never(),
        )
    assert outcome.error == "output_failed"
    assert "output_limit_exceeded" in outcome.output_failure
    assert outcome.exit is not None
    assert outcome.output == b"x" * OUTPUT_LIMIT_BYTES

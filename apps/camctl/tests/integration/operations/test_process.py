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
)

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


async def test_cancel_uses_same_termination_flow() -> None:
    stop = _CancelRequest()
    task = asyncio.ensure_future(_run(_tool("import time; time.sleep(30)"), stop))
    await asyncio.sleep(0.1)
    stop.request()
    outcome = await task
    assert outcome.error == "cancelled"
    assert outcome.used_grace_s == GRACE
    assert outcome.exit is not None


async def test_output_captured_up_to_limit() -> None:
    class Never:
        async def requested(self) -> None:
            await asyncio.Future()

    script = (
        "import sys\n"
        f"sys.stdout.write('x' * {OUTPUT_LIMIT_BYTES + 4096})\n"
        "sys.stdout.flush()\n"
    )
    outcome = await _run(
        _tool(script, timeout_s=Decimal("5")), Never()
    )
    assert outcome.error is None
    assert outcome.output is not None
    assert len(outcome.output) == OUTPUT_LIMIT_BYTES

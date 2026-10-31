"""实际工具互斥的单进程协作契约；工具均由内存替身提供。"""

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from camctl.devices.drivers.hasselblad.tool_gate import CameraToolGate


@dataclass(frozen=True)
class Spec:
    name: str
    timeout_s: Decimal = Decimal("10")


@pytest.mark.asyncio
async def test_same_body_waits_for_actual_call_to_finish():
    gate = CameraToolGate(monotonic_ns=lambda: 0)
    entered, finish, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []
    async def run(spec, stop):
        calls.append(spec.name)
        if spec.name == "download":
            entered.set()
            await finish.wait()
        return spec.name
    transport = AsyncMock()
    transport.run.side_effect = run
    first = asyncio.create_task(gate.run("body", transport, Spec("download"), None))
    await entered.wait()
    async def delete():
        queued.set()
        return await gate.run("body", transport, Spec("delete"), None)
    second = asyncio.create_task(delete())
    await queued.wait()
    assert calls == ["download"]
    finish.set()
    assert await first == "download"
    assert await second == "delete"
    assert calls == ["download", "delete"]


@pytest.mark.asyncio
async def test_other_body_can_progress_while_first_is_busy():
    gate = CameraToolGate(monotonic_ns=lambda: 0)
    entered, finish = asyncio.Event(), asyncio.Event()
    async def run(spec, stop):
        if spec.name == "first":
            entered.set()
            await finish.wait()
        return spec.name
    transport = AsyncMock()
    transport.run.side_effect = run
    first = asyncio.create_task(gate.run("body-a", transport, Spec("first"), None))
    await entered.wait()
    assert await gate.run("body-b", transport, Spec("second"), None) == "second"
    assert not first.done()
    finish.set()
    await first


@pytest.mark.asyncio
async def test_canceling_waiter_does_not_release_active_body():
    gate = CameraToolGate(monotonic_ns=lambda: 0)
    entered, finish, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def run(spec, stop):
        entered.set()
        await finish.wait()
        return spec.name
    transport = AsyncMock()
    transport.run.side_effect = run
    first = asyncio.create_task(gate.run("body", transport, Spec("active"), None))
    await entered.wait()
    async def wait():
        queued.set()
        await gate.run("body", transport, Spec("waiting"), None)
    second = asyncio.create_task(wait())
    await queued.wait()
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert not first.done()
    transport.run.assert_awaited_once()
    finish.set()
    await first


@pytest.mark.asyncio
async def test_settled_failure_releases_body_for_next_command():
    gate = CameraToolGate(monotonic_ns=lambda: 0)
    transport = AsyncMock()
    transport.run.side_effect = [ValueError("tool failed after settlement"), "next"]
    with pytest.raises(ValueError):
        await gate.run("body", transport, Spec("first"), None)
    assert await gate.run("body", transport, Spec("second"), None) == "next"


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_ns,expected", [(400_000_000, Decimal("0.6")), (2_000_000_000, None)])
async def test_waiting_consumes_original_budget_before_dispatch(wait_ns, expected):
    clock = [0]
    gate = CameraToolGate(monotonic_ns=lambda: clock[0])
    entered, finish, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observed = []
    async def run(spec, stop):
        observed.append(spec)
        if spec.name == "first":
            entered.set()
            await finish.wait()
    transport = AsyncMock()
    transport.run.side_effect = run
    first = asyncio.create_task(gate.run("body", transport, Spec("first"), None))
    await entered.wait()
    async def second():
        queued.set()
        return await gate.run("body", transport, Spec("second", Decimal("1")), None)
    waiting = asyncio.create_task(second())
    await queued.wait()
    clock[0] = wait_ns
    finish.set()
    await first
    if expected is None:
        with pytest.raises(TimeoutError):
            await waiting
        assert [spec.name for spec in observed] == ["first"]
    else:
        await waiting
        assert observed[-1].timeout_s == expected


@pytest.mark.asyncio
async def test_cancellation_observed_after_lock_prevents_tool_start():
    gate = CameraToolGate(monotonic_ns=lambda: 0)
    transport = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await gate.run("body", transport, Spec("canceled"), None,
            interrupted=lambda: asyncio.CancelledError())
    transport.run.assert_not_awaited()

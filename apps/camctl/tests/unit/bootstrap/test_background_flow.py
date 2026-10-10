"""取回后台推进保留同一任务，收场等到实际连接清理结束。"""

import asyncio
from unittest.mock import create_autospec

import pytest

from camctl.bootstrap.background_flow import BackgroundFlow, CombinedLocalWork
from camctl.bootstrap.flows import CaptureFlow

pytestmark = pytest.mark.asyncio


async def test_capture_consumes_success_before_releasing_device_owner():
    release = asyncio.Event()
    async def body(context):
        await release.wait()
    owner = BackgroundFlow(body)
    capture = CaptureFlow(None)
    capture._devices["cam-1"] = owner
    await owner(None)
    release.set()
    await asyncio.sleep(0)
    assert capture.owns_device("cam-1") and capture.required_settlements() == 1
    capture.check_completed()
    assert not capture.owns_device("cam-1") and capture.required_settlements() == 0


async def test_capture_completed_failure_stops_other_owner_and_keeps_actual_cleanup():
    fail, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    failure = ValueError("original capture save failed")
    async def first(context):
        await fail.wait()
        raise failure
    async def second(context):
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            await release.wait()
    capture = CaptureFlow(None)
    capture._devices = {"cam-1": BackgroundFlow(first), "cam-2": BackgroundFlow(second)}
    for owner in capture._devices.values():
        await owner(None)
    fail.set()
    await asyncio.sleep(0)
    with pytest.raises(ValueError) as caught:
        capture.check_completed()
    assert caught.value is failure
    await cleaning.wait()
    assert capture.required_settlements() == 1
    release.set()
    await capture.settle()
    await capture(object())  # 已停止，不再打开连接或派发设备工作。
    assert capture.required_settlements() == 0


async def test_running_flow_returns_to_scheduler_and_does_not_start_twice():
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def body(context):
        calls.append(context)
        started.set()
        await release.wait()
    flow = BackgroundFlow(body)
    context = object()
    await flow(context)
    assert started.is_set() and flow.required_settlements() == 1
    await flow(context)
    assert calls == [context]
    release.set()
    await flow.settle()
    assert flow.required_settlements() == 0


async def test_quick_flow_finishes_without_an_extra_shutdown_responsibility():
    async def body(context):
        return None
    flow = BackgroundFlow(body)
    await flow(object())
    assert flow.required_settlements() == 0


async def test_completed_flow_is_consumed_before_next_task_can_start():
    release = asyncio.Event()
    calls = []
    async def body(context):
        calls.append(context)
        await release.wait()
    flow = BackgroundFlow(body)
    await flow(1)
    release.set()
    await asyncio.sleep(0)
    await flow(2)
    assert calls == [1] and flow.required_settlements() == 0
    await flow(3)
    assert calls == [1, 3]


async def test_original_flow_error_is_delivered_to_the_session():
    release = asyncio.Event()
    failure = ValueError("source facts invalid")
    async def body(context):
        await release.wait()
        raise failure
    flow = BackgroundFlow(body)
    await flow(None)
    release.set()
    await asyncio.sleep(0)
    assert flow.required_settlements() == 1
    with pytest.raises(ValueError) as caught:
        await flow(None)
    assert caught.value is failure
    assert flow.required_settlements() == 0


async def test_stop_waits_for_actual_cleanup_before_releasing_responsibility():
    read, cleaning, release, closed = (asyncio.Event() for _ in range(4))
    async def body(context):
        try:
            await read.wait()
        finally:
            cleaning.set()
            await release.wait()
            closed.set()
    flow = BackgroundFlow(body)
    await flow(None)
    flow.stop_new_work()
    waiter = asyncio.create_task(flow.settle())
    await cleaning.wait()
    assert not waiter.done() and not closed.is_set()
    assert flow.required_settlements() == 1
    release.set()
    await waiter
    assert closed.is_set() and flow.required_settlements() == 0


async def test_canceled_shutdown_waiter_does_not_cancel_actual_work():
    release, finished = asyncio.Event(), asyncio.Event()
    async def body(context):
        await release.wait()
        finished.set()
    flow = BackgroundFlow(body)
    await flow(None)
    waiter = asyncio.create_task(flow.settle())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not finished.is_set() and flow.required_settlements() == 1
    release.set()
    await flow.settle()
    assert finished.is_set() and flow.required_settlements() == 0


async def test_self_stop_preserves_original_error_from_actual_cleanup():
    entered, release = asyncio.Event(), asyncio.Event()
    failure = ValueError("original database save failed")

    async def body(context):
        try:
            await asyncio.Future()
        except asyncio.CancelledError as interrupted:
            entered.set()
            await release.wait()
            raise interrupted from failure

    flow = BackgroundFlow(body)
    await flow(None)
    flow.stop_new_work()
    waiter = asyncio.create_task(flow.settle())
    await entered.wait()
    assert not waiter.done()
    release.set()
    with pytest.raises(ValueError) as caught:
        await waiter
    assert caught.value is failure
    assert flow.required_settlements() == 0


async def test_completed_error_remains_owned_if_shutdown_waiter_is_canceled():
    release = asyncio.Event()
    failure = ValueError("original result failed")

    async def body(context):
        await release.wait()
        raise failure

    flow = BackgroundFlow(body)
    await flow(None)
    waiter = asyncio.create_task(flow.settle())
    await asyncio.sleep(0)
    release.set()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert flow.required_settlements() == 1
    with pytest.raises(ValueError) as caught:
        await flow.settle()
    assert caught.value is failure
    assert flow.required_settlements() == 0


async def test_cancel_without_self_stop_keeps_original_cancellation():
    interrupted = asyncio.CancelledError("original flow canceled")

    async def body(context):
        raise interrupted

    flow = BackgroundFlow(body)
    with pytest.raises(asyncio.CancelledError) as caught:
        await flow(None)
    assert caught.value is interrupted
    assert flow.required_settlements() == 0


async def test_combined_shutdown_settles_all_owners_and_preserves_failure():
    class LocalWork:
        def required_settlements(self): ...
        def stop_new_work(self): ...
        async def settle(self): ...
    read = create_autospec(LocalWork, instance=True, spec_set=True)
    files = create_autospec(LocalWork, instance=True, spec_set=True)
    read.required_settlements.return_value = 2
    files.required_settlements.return_value = 3
    failure = ValueError("read result not saved")
    read.settle.side_effect = failure
    work = CombinedLocalWork((read, files))
    assert work.required_settlements() == 5
    work.stop_new_work()
    with pytest.raises(ValueError) as caught:
        await work.settle()
    assert caught.value is failure
    read.stop_new_work.assert_called_once()
    files.stop_new_work.assert_called_once()
    files.settle.assert_awaited_once()

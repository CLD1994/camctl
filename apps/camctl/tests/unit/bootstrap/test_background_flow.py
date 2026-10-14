"""取回后台推进保留同一任务，收场等到实际连接清理结束。"""

import asyncio
from unittest.mock import create_autospec

import pytest

from camctl.bootstrap.background_flow import BackgroundFlow, CombinedLocalWork
from camctl.bootstrap.flows import CaptureFlow
from camctl.session.service import _RequiredPhaseWaiter

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


class _ControlledLocalOwner:
    """纯内存的本地责任端口，可在实际消费期间阻塞并交付原错误。"""

    def __init__(self, error=None, *, started=None, release=None, finished=None):
        self.error = error
        self.started = started or asyncio.Event()
        self.release = release
        self.finished = finished or asyncio.Event()
        self.settle_calls = 0
        self.active = False
        self.was_cancelled = False
        self.stop_calls = 0

    def required_settlements(self):
        return int(self.active)

    def stop_new_work(self):
        self.stop_calls += 1

    async def settle(self):
        self.settle_calls += 1
        self.active = True
        self.started.set()
        try:
            if self.release is not None:
                await self.release.wait()
        except asyncio.CancelledError:
            self.was_cancelled = True
            raise
        finally:
            self.active = False
            self.finished.set()
        if self.error is not None:
            raise self.error


async def _run_scheduled_callbacks():
    """在已排队任务先运行后再继续，不依赖时间延迟或事件竞争。"""
    resumed = asyncio.Event()
    asyncio.get_running_loop().call_soon(resumed.set)
    await asyncio.wait_for(resumed.wait(), timeout=1)


async def test_combined_internal_control_settles_remaining_owners():
    business_started, business_release = asyncio.Event(), asyncio.Event()
    diagnostic_started, diagnostic_release = asyncio.Event(), asyncio.Event()
    diagnostic_finished = asyncio.Event()
    internal = asyncio.CancelledError("internal file-owner control")
    internal.add_note("original internal note")

    async def business(_context):
        business_started.set()
        await business_release.wait()
        raise internal

    flow = BackgroundFlow(business)
    await flow(None)
    await asyncio.wait_for(business_started.wait(), timeout=1)
    diagnostic_owner = _ControlledLocalOwner(
        internal,
        started=diagnostic_started,
        release=diagnostic_release,
        finished=diagnostic_finished,
    )
    combined = CombinedLocalWork((flow, diagnostic_owner))
    settlement = asyncio.create_task(combined.settle())
    try:
        business_release.set()
        await asyncio.wait_for(diagnostic_started.wait(), timeout=1)
        assert not diagnostic_finished.is_set()
        assert not settlement.done()
        assert combined.required_settlements() == 1

        diagnostic_release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(settlement, timeout=1)
        assert caught.value is internal
        assert diagnostic_finished.is_set()
        assert diagnostic_owner.settle_calls == 1
        assert diagnostic_owner.was_cancelled is False
        assert internal.__notes__ == ["original internal note"]
    finally:
        business_release.set()
        diagnostic_release.set()
        await asyncio.wait_for(
            asyncio.gather(settlement, return_exceptions=True), timeout=1)

    single = ValueError("single local failure")
    with pytest.raises(ValueError) as caught_single:
        await asyncio.wait_for(
            CombinedLocalWork((_ControlledLocalOwner(single),)).settle(),
            timeout=1,
        )
    assert caught_single.value is single

    first, second = ValueError("first local failure"), OSError("second local failure")
    with pytest.raises(ExceptionGroup) as caught_multiple:
        await asyncio.wait_for(
            CombinedLocalWork((
                _ControlledLocalOwner(first),
                _ControlledLocalOwner(second),
            )).settle(),
            timeout=1,
        )
    assert len(caught_multiple.value.exceptions) == 2
    assert caught_multiple.value.exceptions[0] is first
    assert caught_multiple.value.exceptions[1] is second

    class BoundedNotesCancellation(asyncio.CancelledError):
        def __init__(self):
            super().__init__("same source and target")
            self.note_calls = 0

        def add_note(self, message):
            self.note_calls += 1
            if self.note_calls > 3:
                raise AssertionError("self-annotation must remain bounded")
            super().add_note(message)

    same_error = BoundedNotesCancellation()
    same_error.add_note("preexisting note")
    same_error.note_calls = 0
    original_notes = list(same_error.__notes__)
    _RequiredPhaseWaiter.attach_exception_notes(
        same_error, "same actual error", same_error)
    assert same_error.note_calls == 0
    assert same_error.__notes__ == original_notes


async def test_combined_observer_cancellation_waits_same_actual_task():
    first_started, first_release = asyncio.Event(), asyncio.Event()
    second_started, second_release = asyncio.Event(), asyncio.Event()
    internal = asyncio.CancelledError("actual owner control")
    internal.add_note("actual owner note")
    later = ValueError("later owner result")
    later.add_note("later owner note")
    first = _ControlledLocalOwner(
        internal, started=first_started, release=first_release)
    second = _ControlledLocalOwner(
        later, started=second_started, release=second_release)
    combined = CombinedLocalWork((first, second))
    propagated = []

    async def observe_settlement():
        try:
            await combined.settle()
        except asyncio.CancelledError as error:
            propagated.append(error)
            raise

    observer = asyncio.create_task(observe_settlement())
    try:
        await asyncio.wait_for(first_started.wait(), timeout=1)
        observer.cancel("first observer cancellation")
        await _run_scheduled_callbacks()
        assert not observer.done()
        first_release.set()

        # 只有首取消已被组合观察并且第一个 owner 已交付后，第二 owner 才会开始。
        await asyncio.wait_for(second_started.wait(), timeout=1)
        assert not observer.done()
        assert combined.required_settlements() == 1
        assert first.settle_calls == 1 and second.settle_calls == 1
        observer.cancel("second observer cancellation")
        await _run_scheduled_callbacks()
        assert not observer.done()
        assert first.was_cancelled is False and second.was_cancelled is False
        assert first.stop_calls == 0 and second.stop_calls == 0

        second_release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(observer, timeout=1)
        assert caught.value is propagated[0]
        assert caught.value.args == ("first observer cancellation",)
        assert any("actual owner note" in note for note in caught.value.__notes__)
        assert any("later owner note" in note for note in caught.value.__notes__)
        assert first.finished.is_set() and second.finished.is_set()
        assert first.settle_calls == 1 and second.settle_calls == 1
        assert first.was_cancelled is False and second.was_cancelled is False
        assert first.stop_calls == 0 and second.stop_calls == 0
        assert combined.required_settlements() == 0
    finally:
        first_release.set()
        second_release.set()
        await asyncio.wait_for(
            asyncio.gather(observer, return_exceptions=True), timeout=1)


@pytest.mark.parametrize(
    "first_phase_control",
    [False, True],
    ids=["prior-success", "prior-control-error"],
)
async def test_combined_consumes_new_owner_work_after_prior_phase(first_phase_control):
    business_started = (asyncio.Event(), asyncio.Event())
    business_release = (asyncio.Event(), asyncio.Event())
    diagnostics_started = (asyncio.Event(), asyncio.Event())
    diagnostics_release = (asyncio.Event(), asyncio.Event())
    diagnostics_finished = (asyncio.Event(), asyncio.Event())
    prior_control = asyncio.CancelledError("prior phase actual control")
    prior_control.add_note("prior phase control note")
    second_result = ValueError("new phase actual result")
    body_calls = []

    async def business(phase):
        body_calls.append(phase)
        business_started[phase].set()
        await business_release[phase].wait()
        if phase == 0 and first_phase_control:
            raise prior_control
        if phase == 1:
            raise second_result

    class DiagnosticOwner:
        def __init__(self):
            self.settle_calls = 0
            self.active = False

        def required_settlements(self):
            return int(self.active)

        def stop_new_work(self):
            raise AssertionError("组合收场不得在新阶段隐式停止责任")

        async def settle(self):
            phase = self.settle_calls
            self.settle_calls += 1
            self.active = True
            diagnostics_started[phase].set()
            try:
                await diagnostics_release[phase].wait()
            finally:
                self.active = False
                diagnostics_finished[phase].set()

    flow = BackgroundFlow(business)
    diagnostics = DiagnosticOwner()
    combined = CombinedLocalWork((flow, diagnostics))
    first_settlement = None
    second_settlement = None
    try:
        await flow(0)
        await asyncio.wait_for(business_started[0].wait(), timeout=1)
        first_settlement = asyncio.create_task(combined.settle())
        business_release[0].set()
        await asyncio.wait_for(diagnostics_started[0].wait(), timeout=1)
        diagnostics_release[0].set()
        if first_phase_control:
            with pytest.raises(asyncio.CancelledError) as caught_prior:
                await asyncio.wait_for(first_settlement, timeout=1)
            assert caught_prior.value is prior_control
        else:
            await asyncio.wait_for(first_settlement, timeout=1)
        assert flow.required_settlements() == 0
        assert diagnostics.settle_calls == 1
        assert diagnostics_finished[0].is_set()

        # 同一个本地组合进入后续消费阶段；这轮新增业务及诊断责任各自有独立门闩。
        await flow(1)
        await asyncio.wait_for(business_started[1].wait(), timeout=1)
        second_settlement = asyncio.create_task(combined.settle())
        await _run_scheduled_callbacks()
        assert not second_settlement.done()
        assert flow.required_settlements() == 1
        assert diagnostics.settle_calls == 1

        business_release[1].set()
        await asyncio.wait_for(diagnostics_started[1].wait(), timeout=1)
        assert not diagnostics_finished[1].is_set()
        assert not second_settlement.done()
        assert combined.required_settlements() == 1
        diagnostics_release[1].set()

        with pytest.raises(ValueError) as caught_second:
            await asyncio.wait_for(second_settlement, timeout=1)
        assert caught_second.value is second_result
        assert body_calls == [0, 1]
        assert flow.required_settlements() == 0
        assert diagnostics.settle_calls == 2
        assert diagnostics_finished[1].is_set()
        assert combined.required_settlements() == 0
    finally:
        for gate in (*business_release, *diagnostics_release):
            gate.set()
        tasks = [task for task in (first_settlement, second_settlement) if task is not None]
        if tasks:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=1)
        if flow.required_settlements():
            try:
                await asyncio.wait_for(flow.settle(), timeout=1)
            except BaseException:
                # 清理阶段只交付并消费未被组合取回的真实 BackgroundFlow 结果。
                pass

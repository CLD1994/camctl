"""重试间隔时间强制的组件集成测试。

真实 SQLite、尝试事务与受控单调钟组合：核实轮次与录像停止在保存
重试等待后，按本次配置的间隔到时才允许下一次尝试——未到时不提交
新意图、不消耗次数，由推进循环下一轮再判；跨会话恢复的等待按本
次间隔从首次观察重新计时；间隔为零不额外等待；预算耗尽即时交由
意图事务按耗尽收场，不为等待推迟结论。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from camctl.capture.handlers import capture_handler
from camctl.capture.results import FileKind as ResultFileKind
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import DeviceCallResult
from camctl.operations.attempts import AttemptConfig

from .test_capture_contract import _NOW, _entry, _value
from .test_listing_rounds import _FlakyResults, _run_row
from ..support.result_cases import result_case, result_case_for
from .test_natural_wait_time_eligibility import _request_cancel

pytestmark = pytest.mark.asyncio


class _FlakyStopper:
    """停止端口替身：前 failures 次返回设备错误，之后可靠确认。"""

    def __init__(self, failures: int, target: str) -> None:
        self.failures = failures
        self.target = target
        self.calls = 0

    async def stop(self, request) -> DeviceCallResult:
        self.calls += 1
        if self.calls <= self.failures:
            return DeviceCallResult(observations=(), error={"code": "device_error"})
        return DeviceCallResult(
            observations=(
                DeviceObservation(
                    type="stop_confirmed", version=1,
                    data={"activity_id": self.target}),
            ),
            error=None,
        )


class _Clock:
    """受控单调钟：按秒推进读数。"""

    def __init__(self, ns: int = 5_000_000_000) -> None:
        self.ns = ns

    def __call__(self) -> int:
        return self.ns

    def advance_s(self, seconds: Decimal) -> None:
        self.ns += int(Decimal(seconds) * 1_000_000_000)


def _stop_row(owned, action_id: int):
    """停止流程行的当前状态：状态、累计次数与重试等待。"""
    return _value(
        owned,
        "SELECT status, attempts_used, retry_wait_required FROM operation_runs"
        " WHERE responsibility_key = ?",
        f"stop/{action_id}")


async def _timed_world(tmp_path, *, consumer="photo", clock=None, check_interval=Decimal(3)):
    clock = _Clock() if clock is None else clock
    owned, runtime, action_id, handler = await result_case_for().world(tmp_path,
        consumer, prepare_start_only=consumer == "record", assembly_options={
            "monotonic_ns": clock,
            "check_config": AttemptConfig(3, Decimal(10), check_interval),
            "stop_config": AttemptConfig(3, Decimal(10), Decimal(3)),
        })
    if consumer == "record":
        result_case_for().clock.wall = _NOW
    return owned, runtime, action_id, clock


class TestCheckRoundInterval:
    async def test_listing_retry_waits_configured_interval(self, tmp_path: Path) -> None:
        owned, runtime, aid, clock = await _timed_world(tmp_path)
        try:
            flaky = _FlakyResults({aid: (_entry("shot-1", kind=ResultFileKind.PHOTO),)}, failures=1)
            result_case_for().results.delegate = flaky
            handler = capture_handler("camera_take_photo")
            await handler(aid, runtime)
            assert flaky.calls == [aid]
            assert _run_row(owned, aid) == (2, 1, 1)
            clock.advance_s(Decimal(1))
            await handler(aid, runtime)
            assert flaky.calls == [aid]
            assert _run_row(owned, aid) == (2, 1, 1)
            clock.advance_s(Decimal(2))
            await handler(aid, runtime)
            assert flaky.calls == [aid, aid]
            assert _run_row(owned, aid) == (3, 2, 0)
            assert _value(owned, "SELECT status FROM actions WHERE id=?", aid) == (3,)
        finally:
            owned.connection.close()

    async def test_zero_interval_retries_next_round_immediately(self, tmp_path: Path) -> None:
        owned, runtime, aid, clock = await _timed_world(tmp_path, check_interval=Decimal(0))
        try:
            flaky = _FlakyResults({aid: (_entry("shot-1", kind=ResultFileKind.PHOTO),)}, failures=1)
            result_case_for().results.delegate = flaky
            handler = capture_handler("camera_take_photo")
            await handler(aid, runtime)
            await handler(aid, runtime)
            assert flaky.calls == [aid, aid]
            assert _run_row(owned, aid) == (3, 2, 0)
        finally:
            owned.connection.close()

    async def test_exhausted_budget_not_delayed_by_wait(self, tmp_path: Path) -> None:
        owned, runtime, aid, clock = await _timed_world(tmp_path)
        try:
            flaky = _FlakyResults({}, failures=3)
            result_case_for().results.delegate = flaky
            handler = capture_handler("camera_take_photo")
            await handler(aid, runtime)
            clock.advance_s(Decimal(3))
            await handler(aid, runtime)
            clock.advance_s(Decimal(3))
            await handler(aid, runtime)
            assert flaky.calls == [aid, aid, aid]
            await handler(aid, runtime)
            assert flaky.calls == [aid, aid, aid]
            assert _value(owned, "SELECT status FROM actions WHERE id=?", aid) == (4,)
            assert _run_row(owned, aid) == (6, 3, 0)
        finally:
            owned.connection.close()


class TestStopInterval:
    async def test_failed_stop_waits_interval_before_next_attempt(self, tmp_path: Path) -> None:
        owned, runtime, aid, clock = await _timed_world(tmp_path, consumer="record")
        try:
            activity_id, = owned.connection.execute("SELECT id FROM device_activities WHERE action_id=?", (aid,)).fetchone()
            stopper = _FlakyStopper(failures=1, target=str(activity_id))
            result_case_for().stopper.stop.side_effect = stopper.stop
            await _request_cancel(runtime, aid, [result_case_for().clock.wall])
            handler = capture_handler("camera_record")
            await handler(aid, runtime)
            assert stopper.calls == 1
            assert _stop_row(owned, aid) == (2, 1, 1)
            clock.advance_s(Decimal(1))
            await handler(aid, runtime)
            assert stopper.calls == 1
            assert _stop_row(owned, aid) == (2, 1, 1)
            clock.advance_s(Decimal(2))
            await handler(aid, runtime)
            assert stopper.calls == 2
            assert _value(owned, "SELECT status FROM actions WHERE id=?", aid) == (6,)
        finally:
            owned.connection.close()


class TestCrossSessionRecount:
    async def test_new_session_recounts_wait_from_first_observation(self, tmp_path: Path) -> None:
        owned, runtime_a, aid, clock_a = await _timed_world(tmp_path)
        try:
            flaky = _FlakyResults({}, failures=3)
            result_case_for().results.delegate = flaky
            handler = capture_handler("camera_take_photo")
            await handler(aid, runtime_a)
            clock_a.advance_s(Decimal(3))
            await handler(aid, runtime_a)
            assert flaky.calls == [aid, aid]
            clock_b = _Clock()
            runtime_b = await result_case_for().reopen(owned, cold=True,
                assembly_options={"monotonic_ns": clock_b})
            clock_b.advance_s(Decimal(1))
            await handler(aid, runtime_b)
            assert flaky.calls == [aid, aid]
            assert _run_row(owned, aid) == (2, 2, 1)
            clock_b.advance_s(Decimal(3))
            await handler(aid, runtime_b)
            assert flaky.calls == [aid, aid, aid]
        finally:
            owned.connection.close()

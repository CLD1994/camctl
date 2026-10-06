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

from camctl.capture.handlers import CaptureRuntime, capture_handler
from camctl.capture.results import FileKind as ResultFileKind
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.devices.evidence import (
    DeviceObservation,
    EvidenceContract,
    EvidenceRegistry,
)
from camctl.devices.ports import DeviceCallResult
from camctl.operations.attempts import AttemptConfig
from camctl.persistence.repositories.capture import (
    CaptureRepository,
    register_capture_guards,
)
from camctl.persistence.repositories.operations import (
    OperationRepository,
    register_operation_guards,
)
from camctl.persistence.repositories.scheduling import SchedulingRepository
from camctl.persistence.repositories.timelapse import (
    TimelapseRepository,
    register_timelapse_guards,
)
from camctl.scheduling.rules import LaunchWindow

from .test_capture_contract import (
    _NOW,
    _PHOTO,
    _RECORD,
    DriverDouble,
    ResultsDouble,
    _environment,
    _entry,
    _value,
)
from .test_listing_rounds import _FlakyResults, _run_row

register_operation_guards()
register_capture_guards()
register_timelapse_guards()

pytestmark = pytest.mark.asyncio

_EVIDENCE = EvidenceRegistry(
    (
        EvidenceContract(type="operation_returned", version=1, operation="control",
                         fields=frozenset()),
        EvidenceContract(type="photo_taken", version=1, operation="control",
                         fields=frozenset({"activity_id"}), identity_field="activity_id"),
        EvidenceContract(type="start_confirmed", version=1, operation="control",
                         fields=frozenset({"activity_id"}), identity_field="activity_id"),
        EvidenceContract(type="results_returned", version=1, operation="result",
                         fields=frozenset()),
        EvidenceContract(type="stop_returned", version=1, operation="stop",
                         fields=frozenset()),
        EvidenceContract(type="stop_confirmed", version=1, operation="stop",
                         fields=frozenset({"activity_id"}),
                         identity_field="activity_id"),
    )
)


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


def _runtime(owned, *, driver=None, results=None, stopper=None, clock=None,
             check_interval=Decimal("3"),
             stop_interval=Decimal("3")) -> CaptureRuntime:
    return CaptureRuntime(
        owned=owned,
        scheduling=SchedulingRepository(),
        operations=OperationRepository(),
        capture=CaptureRepository(),
        timelapse=TimelapseRepository(),
        driver=driver if driver is not None else DriverDouble(),
        results=results if results is not None else ResultsDouble({}),
        evidence=_EVIDENCE,
        wall_us=lambda: _NOW,
        monotonic_ns=clock if clock is not None else (lambda: 5_000_000_000),
        window_of=lambda action: LaunchWindow(
            scheduled_at=action["scheduled_at"],
            window_end=action["scheduled_at"] + action["max_delay_ms"] * 1000),
        wait_config=lambda params: CaptureWaitConfig(
            target_duration_ms=600_000, driver_margin_ms=0),
        stopper=stopper,
        check_config=AttemptConfig(
            max_attempts=3, timeout_s=Decimal("10"),
            retry_interval_s=check_interval),
        stop_config=AttemptConfig(
            max_attempts=3, timeout_s=Decimal("10"),
            retry_interval_s=stop_interval),
    )


def _stop_row(owned, action_id: int):
    """停止流程行的当前状态：状态、累计次数与重试等待。"""
    return _value(
        owned,
        "SELECT status, attempts_used, retry_wait_required FROM operation_runs"
        " WHERE responsibility_key = ?",
        f"stop/{action_id}")


class TestCheckRoundInterval:
    async def test_listing_retry_waits_configured_interval(
            self, tmp_path: Path) -> None:
        owned = _environment(tmp_path, _PHOTO)
        try:
            flaky = _FlakyResults(
                {11: (_entry("shot-1", kind=ResultFileKind.PHOTO),)},
                failures=1)
            clock = _Clock()
            runtime = _runtime(owned, results=flaky, clock=clock)
            handler = capture_handler("camera_take_photo")
            await handler(11, runtime)
            # 本轮列举失败：保存失败结果与重试等待。
            assert flaky.calls == [11]
            assert _run_row(owned, 11) == (2, 1, 1)
            clock.advance_s(Decimal("1"))
            await handler(11, runtime)
            # 间隔未到：不提交新意图，不消耗核实名额。
            assert flaky.calls == [11]
            assert _run_row(owned, 11) == (2, 1, 1)
            clock.advance_s(Decimal("2"))
            await handler(11, runtime)
            # 累计到整段间隔：下一轮作为新轮次成功核实。
            assert flaky.calls == [11, 11]
            assert _run_row(owned, 11) == (3, 2, 0)
            assert _value(owned, "SELECT status FROM actions WHERE id = 11") == (3,)
        finally:
            owned.connection.close()

    async def test_zero_interval_retries_next_round_immediately(
            self, tmp_path: Path) -> None:
        owned = _environment(tmp_path, _PHOTO)
        try:
            flaky = _FlakyResults(
                {11: (_entry("shot-1", kind=ResultFileKind.PHOTO),)},
                failures=1)
            runtime = _runtime(
                owned, results=flaky, clock=_Clock(),
                check_interval=Decimal("0"))
            handler = capture_handler("camera_take_photo")
            await handler(11, runtime)
            await handler(11, runtime)
            # 间隔为零：满足重试资格后不额外等待。
            assert flaky.calls == [11, 11]
            assert _run_row(owned, 11) == (3, 2, 0)
        finally:
            owned.connection.close()

    async def test_exhausted_budget_not_delayed_by_wait(
            self, tmp_path: Path) -> None:
        owned = _environment(tmp_path, _PHOTO)
        try:
            flaky = _FlakyResults({}, failures=3)
            clock = _Clock()
            runtime = _runtime(owned, results=flaky, clock=clock)
            handler = capture_handler("camera_take_photo")
            await handler(11, runtime)
            clock.advance_s(Decimal("3"))
            await handler(11, runtime)
            clock.advance_s(Decimal("3"))
            await handler(11, runtime)
            # 三轮失败预算耗尽；不推进时钟的第四次推进立即收场。
            assert flaky.calls == [11, 11, 11]
            await handler(11, runtime)
            assert flaky.calls == [11, 11, 11]
            assert _value(owned, "SELECT status FROM actions WHERE id = 11") == (4,)
            assert _run_row(owned, 11) == (6, 3, 0)
        finally:
            owned.connection.close()


class TestStopInterval:
    async def test_failed_stop_waits_interval_before_next_attempt(
            self, tmp_path: Path) -> None:
        owned = _environment(tmp_path, _RECORD)
        try:
            clock = _Clock()
            stopper = _FlakyStopper(failures=1, target="12")
            runtime = _runtime(owned, stopper=stopper, clock=clock)
            handler = capture_handler("camera_record")
            await handler(12, runtime)
            # 启动确认后取消生效：不经计时立即按剩余预算停止。
            owned.connection.execute(
                "UPDATE actions SET cancel_requested = 1 WHERE id = 12")
            owned.connection.commit()
            await handler(12, runtime)
            assert stopper.calls == 1
            assert _stop_row(owned, 12) == (2, 1, 1)
            clock.advance_s(Decimal("1"))
            await handler(12, runtime)
            # 间隔未到：不再次发令，等待由推进循环下一轮再判。
            assert stopper.calls == 1
            assert _stop_row(owned, 12) == (2, 1, 1)
            clock.advance_s(Decimal("2"))
            await handler(12, runtime)
            # 到时：第二次尝试确认停止，取消收场保存终态。
            assert stopper.calls == 2
            assert _value(owned, "SELECT status FROM actions WHERE id = 12") == (6,)
        finally:
            owned.connection.close()


class TestCrossSessionRecount:
    async def test_new_session_recounts_wait_from_first_observation(
            self, tmp_path: Path) -> None:
        owned = _environment(tmp_path, _PHOTO)
        try:
            flaky = _FlakyResults({}, failures=3)
            clock_a = _Clock()
            runtime_a = _runtime(owned, results=flaky, clock=clock_a)
            handler = capture_handler("camera_take_photo")
            await handler(11, runtime_a)
            clock_a.advance_s(Decimal("3"))
            await handler(11, runtime_a)
            assert flaky.calls == [11, 11]
            # 新会话（全新等待记忆）：跨会话按本次间隔从首次观察重新计时。
            clock_b = _Clock()
            runtime_b = _runtime(owned, results=flaky, clock=clock_b)
            clock_b.advance_s(Decimal("1"))
            await handler(11, runtime_b)
            assert flaky.calls == [11, 11]
            assert _run_row(owned, 11) == (2, 2, 1)
            # 首次观察起整段间隔经过后才开新轮次。
            clock_b.advance_s(Decimal("3"))
            await handler(11, runtime_b)
            assert flaky.calls == [11, 11, 11]
        finally:
            owned.connection.close()

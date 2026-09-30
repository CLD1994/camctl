"""S3 受理后时钟资格与受限执行的单元测试。

期望独立来自时钟恢复规格：下界只在检查通过后建立或推进且每次
run 至多一次；读数不可信进入有限收场模式；受理阶段读数不推进
下界。
"""

from __future__ import annotations

import pytest

from camctl.session.clock import (
    ClockCheckInput,
    ClockTrustStatus,
    ExecutionMode,
    check_clock,
    enter_execution,
)

_MIN = 1_735_689_600_000_000  # 2025-01-01 00:00:00 UTC


class FixedClock:
    def __init__(self, readings: list[int]) -> None:
        self._readings = list(readings)

    def utc_micros(self) -> int:
        return self._readings.pop(0)

    def monotonic_ns(self) -> int:
        return 0


def _input(bound: int | None, **overrides) -> ClockCheckInput:
    base = dict(
        lower_bound_micros=bound, min_plausible_micros=_MIN,
        recheck_delay_s=0.0, recheck_count=1,
    )
    base.update(overrides)
    return ClockCheckInput(**base)


class TestCheckClock:
    def test_plausible_reading_without_bound_establishes(self) -> None:
        check = check_clock(_input(None), FixedClock([_MIN + 1000]))
        assert check.trusted
        assert check.new_lower_bound_micros == _MIN + 1000

    def test_strictly_later_reading_advances(self) -> None:
        bound = _MIN + 5000
        check = check_clock(_input(bound), FixedClock([bound + 1]))
        assert check.trusted
        assert check.new_lower_bound_micros == bound + 1

    def test_earlier_or_equal_reading_keeps_bound(self) -> None:
        bound = _MIN + 5000
        for reading in (bound, bound - 1):
            check = check_clock(_input(bound), FixedClock([reading]))
            assert check.trusted
            assert check.new_lower_bound_micros is None

    def test_implausible_reading_is_untrusted_without_update(self) -> None:
        check = check_clock(_input(None), FixedClock([_MIN - 1]))
        assert check.status is ClockTrustStatus.UNTRUSTED
        assert check.new_lower_bound_micros is None


class TestEnterExecution:
    def _context(self, completed: bool = True):
        class Repository:
            def __init__(self) -> None:
                self.calls = 0

            def update_lower_bound(self, check, key, owned):
                self.calls += 1
                from camctl.persistence.models import DbOutcome, DbOutcomeKind

                kind = DbOutcomeKind.COMPLETED if completed else DbOutcomeKind.UNKNOWN
                return DbOutcome(kind=kind)

        repository = Repository()
        return type(
            "Context",
            (),
            {"repository": repository, "operation_key": "0" * 32, "owned": None},
        )(), repository

    @pytest.mark.asyncio
    async def test_untrusted_reading_enters_restricted_mode(self) -> None:
        check = check_clock(_input(None), FixedClock([_MIN - 1]))
        mode = await enter_execution(check, self._context()[0])
        assert mode is ExecutionMode.RESTRICTED

    @pytest.mark.asyncio
    async def test_trusted_first_check_saves_bound_and_enters_normal(self) -> None:
        check = check_clock(_input(None), FixedClock([_MIN + 10]))
        context, repository = self._context()
        mode = await enter_execution(check, context)
        assert mode is ExecutionMode.NORMAL
        assert repository.calls == 1

    @pytest.mark.asyncio
    async def test_no_change_needed_skips_write(self) -> None:
        bound = _MIN + 5000
        check = check_clock(_input(bound), FixedClock([bound]))
        context, repository = self._context()
        mode = await enter_execution(check, context)
        assert mode is ExecutionMode.NORMAL
        assert repository.calls == 0

    @pytest.mark.asyncio
    async def test_bound_save_failure_is_fatal(self) -> None:
        check = check_clock(_input(None), FixedClock([_MIN + 10]))
        mode = await enter_execution(check, self._context(completed=False)[0])
        assert mode is ExecutionMode.FATAL

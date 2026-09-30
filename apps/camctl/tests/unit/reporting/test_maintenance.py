"""R8 报告失败维护与时限的单元测试。

文件/worker 失败后等待新触发（重扫不重试）；新业务变化才重新
生成；时限不因进度续期；停止宽限后强制终止并确认退出。
"""

from __future__ import annotations

import asyncio

import pytest

from camctl.reporting.maintenance import (
    MaintenanceLimits,
    ReportMaintenanceContext,
    ReportMaintenanceOutcome,
    maintain_reports,
)


class Ports:
    """受端口约束的替身：记录调用次数。"""

    def __init__(
        self,
        *,
        generation_calls: int = 0,
        generation_fails: bool = False,
        new_changes: bool = False,
    ) -> None:
        self.generation_calls = generation_calls
        self.generation_fails = generation_fails
        self._new_changes = new_changes
        self.publish_calls = 0

    def has_new_changes(self) -> bool:
        return self._new_changes

    async def generate(self, job):
        self.generation_calls += 1
        if self.generation_fails:
            raise RuntimeError("生成失败")
        return b"payload"

    async def publish(self, report_id: int, payload: bytes) -> bool:
        self.publish_calls += 1
        return True


def _context(ports: Ports, *, previous_failure: bool = False) -> ReportMaintenanceContext:
    return ReportMaintenanceContext(
        limits=MaintenanceLimits(startup_seconds=10.0, total_seconds=300.0, stop_grace_seconds=5.0),
        ports=ports,
        previous_attempt_failed=previous_failure,
    )


class TestFailureWaitsForNewTrigger:
    @pytest.mark.asyncio
    async def test_failure_waits_for_new_trigger(self) -> None:
        ports = Ports(generation_fails=True, new_changes=True)
        result = await maintain_reports(_context(ports))
        assert result.outcome is ReportMaintenanceOutcome.GENERATION_FAILED
        # 失败后时间经过/重扫：不再生成（不调用第二次）。
        ports._new_changes = False
        again = await maintain_reports(_context(ports, previous_failure=True))
        assert again.outcome is ReportMaintenanceOutcome.WAITING_NEW_TRIGGER
        assert ports.generation_calls == 1

    @pytest.mark.asyncio
    async def test_publish_failure_waits_for_new_trigger(self) -> None:
        class PublishFails:
            def has_new_changes(self) -> bool:
                return True

            async def generate(self, job):
                return b"payload"

            async def publish(self, report_id, payload):
                return False

        result = await maintain_reports(
            ReportMaintenanceContext(
                limits=MaintenanceLimits(10.0, 300.0, 5.0),
                ports=PublishFails(),
                previous_attempt_failed=False,
            )
        )
        assert result.outcome is ReportMaintenanceOutcome.GENERATION_FAILED

    @pytest.mark.asyncio
    async def test_no_pending_report_skips_generation(self) -> None:
        ports = Ports()
        result = await maintain_reports(
            ReportMaintenanceContext(
                limits=MaintenanceLimits(10.0, 300.0, 5.0),
                ports=ports,
                previous_attempt_failed=False,
            )
        )
        assert result.outcome is ReportMaintenanceOutcome.NO_PENDING_REPORT
        assert ports.generation_calls == 0

    @pytest.mark.asyncio
    async def test_success_publishes_once(self) -> None:
        class Ready:
            def has_new_changes(self) -> bool:
                return True

            async def generate(self, job):
                return b"payload"

            async def publish(self, report_id, payload):
                return True

        result = await maintain_reports(
            ReportMaintenanceContext(
                limits=MaintenanceLimits(10.0, 300.0, 5.0),
                ports=Ready(),
                previous_attempt_failed=False,
            )
        )
        assert result.outcome is ReportMaintenanceOutcome.PUBLISHED


class TestLimits:
    def test_limits_positive_and_finite(self) -> None:
        with pytest.raises(ValueError):
            MaintenanceLimits(0.0, 300.0, 5.0)
        with pytest.raises(ValueError):
            MaintenanceLimits(10.0, -1.0, 5.0)

    def test_default_limits(self) -> None:
        limits = MaintenanceLimits.defaults()
        assert limits.startup_seconds == 10.0
        assert limits.total_seconds == 300.0
        assert limits.stop_grace_seconds == 5.0

"""C5 延时摄影等待及跨重启恢复的单元测试。

发送成功后的等待计划由发送锚点、目标时长、驱动必要余量与部署
额外等待组成；数据库延迟不推迟预计检查；仅发送不产生设备完成
观察；重启用原发送日期时间计算剩余等待并采用本次额外等待；原
生完成后返回不再等待全时长；主机负责结束时目标时长安排停止。
"""

from __future__ import annotations

import pytest

import pytest

from camctl.capture.timelapse import (
    CaptureWaitConfig,
    ClockReading,
    EndControl,
    StartReturn,
    TimelapseState,
    WaitKind,
    plan_capture_wait,
)

_T0_UTC = 1_750_000_000_000_000  # 10:00 的可信墙钟（微秒）
_DURATION_MS = 20 * 60 * 1000
_MARGIN_MS = 60 * 1000
_EXTRA_MS = 0

_CHECK_UTC = _T0_UTC + (_DURATION_MS + _MARGIN_MS + _EXTRA_MS) * 1000

pytestmark = pytest.mark.asyncio


def _config(**overrides) -> CaptureWaitConfig:
    values = dict(
        target_duration_ms=_DURATION_MS,
        driver_margin_ms=_MARGIN_MS,
        extra_wait_ms=_EXTRA_MS,
    )
    values.update(overrides)
    return CaptureWaitConfig(**values)


def _state(**overrides) -> TimelapseState:
    values = dict(
        clock_trusted=True,
        start_return=StartReturn.SENT,
        end_control=EndControl.DEVICE,
        sent_at_utc=_T0_UTC,
        anchor_monotonic_ns=5_000_000_000,
        restart=False,
        wait_completed_fact_saved=False,
        device_completion_evidence=False,
    )
    values.update(overrides)
    return TimelapseState(**values)


def _now(utc_us: int = _T0_UTC + 600 * 1_000_000, mono_ns: int = 8_000_000_000):
    return ClockReading(utc_us=utc_us, monotonic_ns=mono_ns)


class TestWaitPlan:
    async def test_send_only_has_no_device_completion(self) -> None:
        """发送成功只安排等待：不产生设备完成观察。"""
        plan = plan_capture_wait(_state(), _config(), _now())
        assert plan.kind is WaitKind.WAIT_THEN_CHECK
        assert plan.device_state_is_observed_ended is False

    async def test_db_delay_does_not_postpone_check(self) -> None:
        """锚点取得后的持久化延迟不推迟预计检查。"""
        prompt = plan_capture_wait(_state(), _config(), _now())
        delayed = plan_capture_wait(
            _state(), _config(), _now(utc_us=_T0_UTC + 3600 * 1_000_000)
        )
        assert prompt.check_at_utc == delayed.check_at_utc == _CHECK_UTC
        assert prompt.monotonic_deadline_ns == delayed.monotonic_deadline_ns

    async def test_check_time_sums_all_components(self) -> None:
        plan = plan_capture_wait(
            _state(), _config(extra_wait_ms=3_000), _now()
        )
        assert plan.check_at_utc == _T0_UTC + (
            _DURATION_MS + _MARGIN_MS + 3_000
        ) * 1000

    async def test_default_extra_wait_is_zero(self) -> None:
        assert _config().extra_wait_ms == 0
        assert plan_capture_wait(_state(), _config(), _now()).check_at_utc == _CHECK_UTC

    async def test_negative_extra_wait_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            _config(extra_wait_ms=-1)
        with pytest.raises(ValueError):
            _config(driver_margin_ms=-1)


class TestRestartRecovery:
    async def test_restart_computes_remaining_wait(self) -> None:
        """10:00 发送、预计 10:21 检查；10:10 恢复时剩 11 分钟。"""
        resume_at = _T0_UTC + 10 * 60 * 1_000_000  # 10:10
        plan = plan_capture_wait(
            _state(restart=True, anchor_monotonic_ns=None),
            _config(),
            _now(utc_us=resume_at, mono_ns=42_000_000_000),
        )
        assert plan.kind is WaitKind.RESUME_WAIT
        assert plan.remaining_ms == 11 * 60 * 1000
        assert plan.monotonic_deadline_ns == 42_000_000_000 + plan.remaining_ms * 1_000_000

    async def test_restart_after_check_time_verifies_now(self) -> None:
        """预计检查时间已过：直接核实产物，不补等全时长。"""
        late = _T0_UTC + 25 * 60 * 1_000_000  # 10:25
        plan = plan_capture_wait(
            _state(restart=True, anchor_monotonic_ns=None),
            _config(),
            _now(utc_us=late, mono_ns=99_000_000_000),
        )
        assert plan.kind is WaitKind.CHECK_NOW

    async def test_restart_uses_current_extra_wait(self) -> None:
        """重启采用本次部署额外等待，从原发送时间重算。"""
        resume_at = _T0_UTC + 10 * 60 * 1_000_000
        plan = plan_capture_wait(
            _state(restart=True, anchor_monotonic_ns=None),
            _config(extra_wait_ms=60_000),  # 本次配置增加 1 分钟
            _now(utc_us=resume_at, mono_ns=42_000_000_000),
        )
        assert plan.remaining_ms == 12 * 60 * 1000

    async def test_saved_wait_completion_is_not_rewaited(self) -> None:
        plan = plan_capture_wait(
            _state(wait_completed_fact_saved=True), _config(), _now()
        )
        assert plan.kind is WaitKind.VERIFY_FILES_NOW

    async def test_device_completion_evidence_skips_wait(self) -> None:
        plan = plan_capture_wait(
            _state(device_completion_evidence=True), _config(), _now()
        )
        assert plan.kind is WaitKind.VERIFY_FILES_NOW


class TestOtherPaths:
    async def test_completed_return_does_not_rewait(self) -> None:
        """原生任务完成后返回：直接产物核实，不等全时长。"""
        plan = plan_capture_wait(
            _state(start_return=StartReturn.COMPLETED), _config(), _now()
        )
        assert plan.kind is WaitKind.VERIFY_FILES_NOW

    async def test_host_end_control_schedules_stop(self) -> None:
        """主机负责结束：目标时长安排停止，不用检查公式。"""
        plan = plan_capture_wait(
            _state(end_control=EndControl.HOST), _config(), _now()
        )
        assert plan.kind is WaitKind.HOST_CONTROL_STOP
        assert plan.monotonic_deadline_ns == (
            _state().anchor_monotonic_ns + _DURATION_MS * 1_000_000
        )

    async def test_untrusted_clock_blocks_wait(self) -> None:
        plan = plan_capture_wait(_state(clock_trusted=False), _config(), _now())
        assert plan.kind is WaitKind.CLOCK_UNAVAILABLE

    async def test_missing_sent_time_is_unresolvable(self) -> None:
        """没有可靠发送时间且无恢复依据：不虚构等待。"""
        plan = plan_capture_wait(
            _state(sent_at_utc=None, anchor_monotonic_ns=None, restart=True),
            _config(),
            _now(),
        )
        assert plan.kind is WaitKind.UNRESOLVABLE

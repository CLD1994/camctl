"""C7 有限安全收场与应急最终补记的单元测试。

仅在可靠归属、排他资格、安全重复停止能力及可运行条件成立时执
行有限应急停止；额度先占用不退还，同一会话同一录像唯一流程；
已确认停止不再补发；停止结果与持久化结果分别表达。
"""

from __future__ import annotations

import pytest

from camctl.capture.recovery import (
    EmergencyBudget,
    EmergencyDecision,
    EmergencyFacts,
    EmergencyOutcome,
    EmergencyRecord,
    RecordStatus,
    emergency_eligibility,
    emergency_stop,
)

pytestmark = pytest.mark.asyncio


def _facts(**overrides) -> EmergencyFacts:
    values = dict(
        process_can_handle=True,
        session_fatal_error=True,
        stop_confirmed=False,
        ownership_confirmed=True,
        exclusive_eligibility=True,
        driver_safe_repeat_stop=True,
        config_known=True,
        budget_available=True,
    )
    values.update(overrides)
    return EmergencyFacts(**values)


class TestEligibility:
    async def test_fatal_error_with_conditions_is_eligible(self) -> None:
        decision = emergency_eligibility(_facts())
        assert decision is EmergencyDecision.ELIGIBLE

    async def test_already_confirmed_does_not_repeat_stop(self) -> None:
        decision = emergency_eligibility(_facts(stop_confirmed=True))
        assert decision is EmergencyDecision.ALREADY_CONFIRMED_SKIP

    async def test_process_cannot_handle_keeps_diagnosis(self) -> None:
        decision = emergency_eligibility(_facts(process_can_handle=False))
        assert decision is EmergencyDecision.INELIGIBLE_KEEP_DIAGNOSIS

    async def test_unknown_conditions_do_not_operate_device(self) -> None:
        """执行条件不成立或未知：不依据猜测操作设备。"""
        for overrides in (
            {"ownership_confirmed": False},
            {"exclusive_eligibility": False},
            {"driver_safe_repeat_stop": False},
            {"config_known": False},
        ):
            decision = emergency_eligibility(_facts(**overrides))
            assert decision is EmergencyDecision.INELIGIBLE_KEEP_DIAGNOSIS, overrides

    async def test_ordinary_failures_do_not_gain_eligibility(self) -> None:
        """普通业务失败、报告失败及日志失败不取得应急资格（S-01）。"""
        assert (
            emergency_eligibility(_facts(session_fatal_error=False))
            is EmergencyDecision.INELIGIBLE_KEEP_DIAGNOSIS
        )

    async def test_budget_exhausted_ends_finite_settlement(self) -> None:
        decision = emergency_eligibility(_facts(budget_available=False))
        assert decision is EmergencyDecision.BUDGET_EXHAUSTED_END


class TestBudget:
    async def test_budget_is_taken_before_dispatch_and_not_refunded(self) -> None:
        """发令前占用额度；失败、超时或未知不退还。"""
        budget = EmergencyBudget(max_attempts=3)
        assert budget.take() is True
        budget.report_failure()
        assert budget.attempts_used == 1
        assert budget.take() is True
        budget.report_unknown()
        assert budget.attempts_used == 2
        assert budget.take() is True
        assert budget.take() is False
        assert budget.attempts_used == 3

    async def test_budget_rejects_invalid_limits(self) -> None:
        with pytest.raises(ValueError):
            EmergencyBudget(max_attempts=0)


class _StopPort:
    """停止端口替身：可编排响应并统计调用。"""

    def __init__(self, *, confirmed_at: int | None = None) -> None:
        self.confirmed_at = confirmed_at
        self.calls = 0

    async def stop(self) -> object:
        self.calls += 1
        if self.calls == self.confirmed_at:
            return type("Stop", (), {"confirmed": True})()
        return type("Stop", (), {"confirmed": False, "error": "timeout"})()


class TestEmergencyStop:
    async def test_emergency_recording_does_not_repeat_stop(self) -> None:
        """取得停止确认后结束流程，不补发剩余次数。"""
        port = _StopPort(confirmed_at=2)
        record = await emergency_stop(
            _facts(), EmergencyBudget(max_attempts=3), port
        )
        assert port.calls == 2
        assert record.outcome is EmergencyOutcome.STOPPED
        assert record.attempts_used == 2
        assert record.record_status is RecordStatus.NOT_RECORDED

    async def test_unconfirmed_after_budget_is_honest(self) -> None:
        """额度耗尽仍未确认：如实保留未确认，不无限补发。"""
        port = _StopPort()
        record = await emergency_stop(
            _facts(), EmergencyBudget(max_attempts=2), port
        )
        assert port.calls == 2
        assert record.outcome is EmergencyOutcome.UNCONFIRMED
        assert record.attempts_used == 2

    async def test_not_attempted_when_ineligible(self) -> None:
        """无法执行且未发令：未尝试，不虚构次数。"""
        port = _StopPort()
        record = await emergency_stop(
            _facts(process_can_handle=False), EmergencyBudget(max_attempts=3), port
        )
        assert port.calls == 0
        assert record.outcome is EmergencyOutcome.NOT_ATTEMPTED
        assert record.attempts_used == 0

    async def test_already_confirmed_makes_no_calls(self) -> None:
        observation = {"type": "stop_confirmed", "version": 1,
                       "data": {"activity_id": "1"}}
        port = _StopPort()
        record = await emergency_stop(
            _facts(stop_confirmed=True, stop_observation=observation),
            EmergencyBudget(max_attempts=3), port,
        )
        assert port.calls == 0
        assert record.outcome is EmergencyOutcome.STOPPED
        assert record.attempts_used == 0
        assert record.stop_observation == observation

    async def test_already_confirmed_without_observation_is_rejected(self) -> None:
        """零尝试停止的补记必须保存停止依据；无可保存观察不能跳过。"""
        with pytest.raises(ValueError, match="可靠停止依据"):
            await emergency_stop(
                _facts(stop_confirmed=True), EmergencyBudget(max_attempts=3), _StopPort()
            )

    async def test_record_status_partitions(self) -> None:
        assert RecordStatus.RECORDED.value == "recorded"
        assert RecordStatus.NOT_RECORDED.value == "not_recorded"
        assert RecordStatus.UNKNOWN.value == "unknown"

    @pytest.mark.parametrize("overrides, facts", [
        ({"process_can_handle": False}, ("进程", "处理")),
        ({"session_fatal_error": False}, ("致命", "错误")),
        ({"ownership_confirmed": False}, ("归属", "未确认")),
        ({"exclusive_eligibility": False}, ("排他", "未确认")),
        ({"driver_safe_repeat_stop": False}, ("安全", "停止", "未确认")),
        ({"config_known": False}, ("配置", "未知")),
    ])
    async def test_not_attempted_keeps_actual_missing_condition(
            self, overrides, facts) -> None:
        """不足资格的实际事实进入诊断，不能只保存未尝试分类。"""
        port = _StopPort()
        record = await emergency_stop(
            _facts(**overrides), EmergencyBudget(max_attempts=3), port,
        )
        assert record.outcome is EmergencyOutcome.NOT_ATTEMPTED
        assert record.attempts_used == 0
        assert port.calls == 0
        reason = getattr(record, "reason", None)
        assert isinstance(reason, str) and reason
        assert all(fact in reason for fact in facts)

    async def test_not_attempted_keeps_all_actual_missing_conditions(self) -> None:
        record = await emergency_stop(
            _facts(ownership_confirmed=False, exclusive_eligibility=False,
                   config_known=False),
            EmergencyBudget(max_attempts=3), _StopPort(),
        )
        reason = getattr(record, "reason", None)
        assert isinstance(reason, str) and reason
        assert all(fact in reason for fact in ("归属", "排他", "配置"))

    async def test_unconfirmed_keeps_exhausted_budget_diagnosis(self) -> None:
        """有限次数用完的汇总诊断与每次驱动错误分别表达。"""
        record = await emergency_stop(
            _facts(), EmergencyBudget(max_attempts=2), _StopPort(),
        )
        assert record.outcome is EmergencyOutcome.UNCONFIRMED
        assert record.attempts_used == 2
        reason = getattr(record, "reason", None)
        assert isinstance(reason, str) and reason
        assert "2" in reason and "耗尽" in reason

    async def test_confirmed_stop_does_not_keep_failure_reason(self) -> None:
        record = await emergency_stop(
            _facts(), EmergencyBudget(max_attempts=3), _StopPort(confirmed_at=2),
        )
        assert record.outcome is EmergencyOutcome.STOPPED
        assert getattr(record, "reason", None) is None

    @pytest.mark.parametrize("used, outcome", [
        (0, EmergencyOutcome.NOT_ATTEMPTED),
        (1, EmergencyOutcome.UNCONFIRMED),
    ])
    async def test_unavailable_budget_stops_before_dispatch_and_keeps_actual_count(
            self, used, outcome) -> None:
        budget = EmergencyBudget(max_attempts=3)
        if used:
            assert budget.take() is True
        port = _StopPort()
        record = await emergency_stop(_facts(budget_available=False), budget, port)
        assert port.calls == 0
        assert record.outcome is outcome
        assert record.attempts_used == used
        reason = getattr(record, "reason", None)
        assert isinstance(reason, str) and reason
        assert "预算" in reason or "额度" in reason

    async def test_ineligible_after_attempt_keeps_unconfirmed_and_actual_count(self) -> None:
        budget = EmergencyBudget(max_attempts=3)
        assert budget.take() is True
        port = _StopPort()
        record = await emergency_stop(_facts(ownership_confirmed=False), budget, port)
        assert port.calls == 0
        assert record.outcome is EmergencyOutcome.UNCONFIRMED
        assert record.attempts_used == 1
        reason = getattr(record, "reason", None)
        assert isinstance(reason, str) and reason
        assert "归属" in reason and "未确认" in reason

    async def test_already_confirmed_keeps_attempts_used_before_confirmation(self) -> None:
        budget = EmergencyBudget(max_attempts=3)
        assert budget.take() is True
        port = _StopPort()
        observation = {"type": "stop_confirmed", "version": 1,
                       "data": {"activity_id": "1"}}
        record = await emergency_stop(
            _facts(stop_confirmed=True, stop_observation=observation), budget, port)
        assert port.calls == 0
        assert record.outcome is EmergencyOutcome.STOPPED
        assert record.attempts_used == 1
        assert record.stop_observation == observation
        assert getattr(record, "reason", None) is None

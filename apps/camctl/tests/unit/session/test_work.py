"""S1 统一待处理责任分类的单元测试。

期望独立来自会话协议的待处理工作判定表：未来动作是工作；仅剩
快照、ACK 等待、可延后清理及无主动收场的残留设备事实不是工作；
未知事实必须错误，不能判为无工作。
"""

from __future__ import annotations

import pytest
from decimal import Decimal

from camctl.session.work import (
    FactDimensionError,
    WorkDecisionKind,
    WorkFacts,
    classify_work,
)


def _facts(**overrides) -> WorkFacts:
    base = dict(
        unfinished_actions=0,
        required_settlements=0,
        pending_report_changes=False,
        report_failed_no_new_changes=False,
        residual_device_facts=False,
        deferred_work_cleanup=False,
        waiting_acknowledgement=False,
        snapshot_backlog=False,
    )
    base.update(overrides)
    return WorkFacts(**base)


class TestWorkDimensions:
    def test_future_pending_is_work(self) -> None:
        decision = classify_work(_facts(unfinished_actions=2))
        assert decision.kind is WorkDecisionKind.NEEDS_DRIVER

    def test_pending_action_counts_even_when_future(self) -> None:
        # 未来 scheduled_at 的 pending 动作同样构成待处理工作。
        decision = classify_work(_facts(unfinished_actions=1))
        assert decision.needs_driver is True

    def test_required_settlement_is_work(self) -> None:
        decision = classify_work(_facts(required_settlements=1))
        assert decision.kind is WorkDecisionKind.NEEDS_DRIVER

    def test_pending_report_changes_is_work(self) -> None:
        # 受理拒绝与 ACK 校验错误的报告责任同样是工作。
        decision = classify_work(_facts(pending_report_changes=True))
        assert decision.kind is WorkDecisionKind.NEEDS_DRIVER

    def test_new_diagnostic_report_with_residual_is_work(self) -> None:
        decision = classify_work(
            _facts(residual_device_facts=True, pending_report_changes=True)
        )
        assert decision.kind is WorkDecisionKind.NEEDS_DRIVER

    def test_report_failed_without_new_changes_waits_for_changes(self) -> None:
        decision = classify_work(_facts(report_failed_no_new_changes=True))
        assert decision.kind is WorkDecisionKind.EXIT_REPORT_ERROR

    def test_report_failed_with_new_changes_reprocesses(self) -> None:
        decision = classify_work(
            _facts(report_failed_no_new_changes=True, pending_report_changes=True)
        )
        assert decision.kind is WorkDecisionKind.NEEDS_DRIVER


class TestNonWorkStates:
    def test_snapshot_backlog_only_is_not_work(self) -> None:
        decision = classify_work(_facts(snapshot_backlog=True))
        assert decision.kind is WorkDecisionKind.CAN_EXIT_SUCCESS

    def test_waiting_ack_only_is_not_work(self) -> None:
        decision = classify_work(_facts(waiting_acknowledgement=True))
        assert decision.kind is WorkDecisionKind.CAN_EXIT_SUCCESS

    def test_deferred_cleanup_only_is_not_work(self) -> None:
        decision = classify_work(_facts(deferred_work_cleanup=True))
        assert decision.kind is WorkDecisionKind.CAN_EXIT_SUCCESS

    def test_residual_device_facts_only_is_not_work(self) -> None:
        decision = classify_work(_facts(residual_device_facts=True))
        assert decision.kind is WorkDecisionKind.CAN_EXIT_SUCCESS

    def test_all_quiescent_dimensions_exit_success(self) -> None:
        decision = classify_work(
            _facts(
                residual_device_facts=True,
                deferred_work_cleanup=True,
                waiting_acknowledgement=True,
                snapshot_backlog=True,
            )
        )
        assert decision.kind is WorkDecisionKind.CAN_EXIT_SUCCESS


class TestUnknownFacts:
    def test_unknown_action_count_is_error_not_no_work(self) -> None:
        with pytest.raises(FactDimensionError):
            classify_work(_facts(unfinished_actions=None))

    def test_unknown_settlement_count_is_error(self) -> None:
        with pytest.raises(FactDimensionError):
            classify_work(_facts(required_settlements=None))

    def test_unknown_report_state_is_error(self) -> None:
        with pytest.raises(FactDimensionError):
            classify_work(_facts(pending_report_changes=None))

    def test_negative_counts_rejected(self) -> None:
        with pytest.raises(ValueError):
            _facts(unfinished_actions=-1)


@pytest.mark.parametrize("dimension", ["unfinished_actions", "required_settlements"])
@pytest.mark.parametrize("invalid", [True, False, 0.0, 1.5, "1", Decimal("1"), [], -1])
def test_work_count_requires_actual_nonnegative_integer(dimension, invalid):
    with pytest.raises(FactDimensionError):
        _facts(**{dimension: invalid})


@pytest.mark.parametrize("dimension", [
    "pending_report_changes", "report_failed_no_new_changes", "residual_device_facts",
    "deferred_work_cleanup", "waiting_acknowledgement", "snapshot_backlog",
])
@pytest.mark.parametrize("invalid", [None, 0, 1, "false", [], {}])
def test_work_flag_requires_actual_boolean(dimension, invalid):
    with pytest.raises(FactDimensionError):
        _facts(**{dimension: invalid})

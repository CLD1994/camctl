"""S4 事务内接管判定的单元测试（纯规则）。

期望独立来自 submit 的事务顺序表：提交成功后的接管判定由持久
化工作与接纳探测共同决定；事实未知不输出成功判定。
"""

from __future__ import annotations

import pytest

from camctl.session.handoff import (
    HandoffOutcome,
    SubmitHandoff,
    WorkFactsError,
    decide_handoff,
)
from camctl.session.locks import AdmissionProbe, AdmissionProbeStatus
from camctl.session.work import WorkDecisionKind, WorkFacts, classify_work


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


_CONFLICT = AdmissionProbe(status=AdmissionProbeStatus.CONFLICT)
_FREE = AdmissionProbe(status=AdmissionProbeStatus.ACQUIRED_AND_RELEASED)


class TestDecideHandoff:
    def test_no_pending_work_needs_no_run(self) -> None:
        decision = decide_handoff(classify_work(_facts()), _CONFLICT)
        assert decision.outcome is HandoffOutcome.NO_PENDING_WORK
        assert decision.needs_run is False

    def test_pending_work_with_acceptor_handed_over(self) -> None:
        decision = decide_handoff(classify_work(_facts(unfinished_actions=1)), _CONFLICT)
        assert decision.outcome is HandoffOutcome.ACCEPTOR_PRESENT
        assert decision.needs_run is False

    def test_pending_work_without_acceptor_requests_run(self) -> None:
        decision = decide_handoff(classify_work(_facts(unfinished_actions=1)), _FREE)
        assert decision.outcome is HandoffOutcome.REQUIRES_RUN
        assert decision.needs_run is True

    def test_report_error_with_no_acceptor_requests_run(self) -> None:
        # 报告失败退出保留责任：没有接纳者时仍需后续 run 接续。
        decision = decide_handoff(
            classify_work(_facts(report_failed_no_new_changes=True)), _FREE
        )
        assert decision.outcome is HandoffOutcome.REQUIRES_RUN
        assert decision.needs_run is True

    def test_report_error_with_acceptor_kept_by_acceptor(self) -> None:
        decision = decide_handoff(
            classify_work(_facts(report_failed_no_new_changes=True)), _CONFLICT
        )
        assert decision.outcome is HandoffOutcome.ACCEPTOR_PRESENT
        assert decision.needs_run is False

    def test_unknown_facts_do_not_produce_success_judgment(self) -> None:
        # 分类阶段即拒绝未知事实：整条链不产生成功接管判定。
        from camctl.session.work import FactDimensionError

        with pytest.raises((WorkFactsError, FactDimensionError)):
            decide_handoff(classify_work(_facts(unfinished_actions=None)), _FREE)


def test_submit_with_no_work_skips_admission_probe():
    def forbidden_probe():
        pytest.fail("无工作时不应进行锁操作")

    handoff = SubmitHandoff(lambda connection: _facts(), forbidden_probe)
    assert handoff.needs_run(object()) is False


@pytest.mark.parametrize("facts", [
    _facts(unfinished_actions=1), _facts(required_settlements=1),
    _facts(pending_report_changes=True), _facts(report_failed_no_new_changes=True),
])
@pytest.mark.parametrize("probe, expected", [(_FREE, True), (_CONFLICT, False)])
def test_submit_handoff_classifies_required_work(facts, probe, expected):
    handoff = SubmitHandoff(lambda connection: facts, lambda: probe)
    assert handoff.needs_run(object()) is expected

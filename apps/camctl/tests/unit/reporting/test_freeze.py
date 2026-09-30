"""R2 报告机会决定规则的单元测试。

期望独立来自报告覆盖规则：普通机会从上次累计水位之后的新变化；
完整同步从 0；已有宽报告可满足窄需求；报告 ID 更大不代表覆盖
更宽；没有新变化不产生新报告。
"""

from __future__ import annotations

import pytest

from camctl.reporting.policy import (
    ReportDecisionKind,
    ReportOpportunity,
    decide_report,
)


def _opportunity(
    *,
    kind: str = "normal",
    from_wm: int = 0,
    latest_wm: int = 0,
    acknowledged_wm: int = 0,
    existing_coverages: tuple = (),
) -> ReportOpportunity:
    return ReportOpportunity(
        kind=kind,
        requested_from_wm=from_wm,
        latest_change_wm=latest_wm,
        acknowledged_wm=acknowledged_wm,
        existing_report_coverages=tuple(existing_coverages),
    )


class TestDecideReport:
    def test_new_changes_produce_incremental_report(self) -> None:
        decision = decide_report(
            _opportunity(acknowledged_wm=100, latest_wm=300, from_wm=100)
        )
        assert decision.kind is ReportDecisionKind.GENERATE
        assert decision.from_wm == 100
        assert decision.to_wm == 300

    def test_no_new_changes_skip_report(self) -> None:
        decision = decide_report(
            _opportunity(acknowledged_wm=300, latest_wm=300, from_wm=300)
        )
        assert decision.kind is ReportDecisionKind.SKIP

    def test_full_sync_starts_from_zero(self) -> None:
        decision = decide_report(
            _opportunity(kind="full_sync", latest_wm=500, from_wm=999)
        )
        assert decision.kind is ReportDecisionKind.GENERATE
        assert decision.from_wm == 0
        assert decision.to_wm == 500

    def test_existing_wide_report_satisfies_narrow_need(self) -> None:
        decision = decide_report(
            _opportunity(
                from_wm=100, latest_wm=300,
                existing_coverages=((5, 0, 400),),
            )
        )
        assert decision.kind is ReportDecisionKind.REUSE
        assert decision.reused_report_id == 5

    def test_larger_report_id_with_narrower_coverage_does_not_satisfy(self) -> None:
        decision = decide_report(
            _opportunity(
                from_wm=100, latest_wm=300,
                existing_coverages=((9, 150, 320), (7, 100, 200)),
            )
        )
        assert decision.kind is ReportDecisionKind.GENERATE

    def test_partial_coverage_does_not_satisfy(self) -> None:
        decision = decide_report(
            _opportunity(
                from_wm=100, latest_wm=500,
                existing_coverages=((3, 100, 400),),
            )
        )
        assert decision.kind is ReportDecisionKind.GENERATE

    def test_empty_range_with_zero_ack_is_initial_skip(self) -> None:
        decision = decide_report(_opportunity(latest_wm=0, from_wm=0))
        assert decision.kind is ReportDecisionKind.SKIP

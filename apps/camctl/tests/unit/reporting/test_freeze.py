"""报告范围与已有报告资格；纯规则不访问状态库或交接目录。"""

from __future__ import annotations

import pytest

from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.values import ConsistencyError
from camctl.reporting.ack import AckReport
from camctl.reporting.policy import ReportDecisionKind, ReportOpportunity, decide_report


def _opportunity(ack=100, latest=300, sync_from=None, started=None):
    return ReportOpportunity(HistoryBoundary(10, 200), latest, ack, sync_from, started)


@pytest.mark.parametrize("ack,sync_from,want", [
    (100, None, 100), (100, 0, 0), (100, 20, 20), (20, 80, 20), (20, 20, 20),
])
def test_range_covers_ack_and_earliest_sync(ack, sync_from, want):
    decision = decide_report(_opportunity(ack, sync_from=sync_from,
                                         started=None if sync_from is None else 80))
    assert decision.kind is ReportDecisionKind.GENERATE
    assert (decision.from_wm, decision.to_wm) == (want, 300)


@pytest.mark.parametrize("watermark", [0, 100])
def test_empty_range_without_sync_skips(watermark):
    decision = decide_report(_opportunity(watermark, watermark))
    assert decision.kind is ReportDecisionKind.SKIP
    assert decision.reused_report_id is None


def test_empty_sync_range_still_requires_local_report():
    decision = decide_report(_opportunity(100, 100, 100, 80))
    assert decision.kind is ReportDecisionKind.GENERATE
    assert (decision.from_wm, decision.to_wm) == (100, 100)


@pytest.mark.parametrize("cover_from", [0, 101])
@pytest.mark.parametrize("cover_to", [299, 300])
@pytest.mark.parametrize("frozen", [79, 80])
def test_reuse_requires_entire_range_and_all_sync_history(cover_from, cover_to, frozen):
    decision = decide_report(_opportunity(100, 300, 100, 80),
                             (AckReport(5, cover_from, cover_to, frozen),))
    want_reuse = cover_from == 0 and cover_to == 300 and frozen == 80
    assert decision.kind is (ReportDecisionKind.REUSE if want_reuse else ReportDecisionKind.GENERATE)
    assert decision.reused_report_id == (5 if want_reuse else None)


def test_one_report_must_cover_all_requirements():
    decision = decide_report(_opportunity(), (AckReport(1, 0, 150, 100), AckReport(2, 150, 300, 150)))
    assert decision.kind is ReportDecisionKind.GENERATE


def test_larger_report_id_cannot_replace_history_qualification():
    decision = decide_report(_opportunity(100, 300, 100, 80),
                             (AckReport(99, 0, 300, 79), AckReport(2, 0, 300, 80)))
    assert decision.kind is ReportDecisionKind.REUSE
    assert decision.reused_report_id == 2


@pytest.mark.parametrize("field,bad", [
    ("boundary", None), ("latest_change_wm", True), ("latest_change_wm", "300"),
    ("latest_change_wm", -1), ("latest_change_wm", 2**53), ("acknowledged_wm", False),
    ("acknowledged_wm", 301), ("sync_from_wm", 301), ("sync_from_wm", "100"),
    ("sync_started_boundary_event_id", True), ("sync_started_boundary_event_id", 0),
    ("sync_started_boundary_event_id", 201), ("sync_from_wm", None),
    ("sync_started_boundary_event_id", None),
])
def test_opportunity_rejects_unreliable_or_inconsistent_facts(field, bad):
    values = dict(boundary=HistoryBoundary(10, 200), latest_change_wm=300,
                  acknowledged_wm=100, sync_from_wm=100, sync_started_boundary_event_id=80)
    values[field] = bad
    with pytest.raises(ValueError):
        ReportOpportunity(**values)


@pytest.mark.parametrize("report", [AckReport(1, 0, 301, 100), AckReport(1, 0, 300, 201)])
def test_existing_report_cannot_come_from_after_current_boundary(report):
    with pytest.raises(ConsistencyError):
        decide_report(_opportunity(), (report,))


def test_maximum_watermark_remains_exact():
    decision = decide_report(_opportunity(9007199254740990, 9007199254740991))
    assert (decision.from_wm, decision.to_wm) == (9007199254740990, 9007199254740991)

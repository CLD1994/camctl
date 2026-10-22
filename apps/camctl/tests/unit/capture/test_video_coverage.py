"""实际视频覆盖的纯累计与终止判定。"""

from __future__ import annotations

from decimal import Decimal

import pytest
from camctl.capture import video_coverage


def _coverage():
    return video_coverage


def test_action6_video_targets_use_exact_duration_and_integer_frames() -> None:
    coverage = _coverage()

    assert coverage.target_frames(Decimal("300"), Decimal("2")) == 151
    assert coverage.target_frames(Decimal("301"), Decimal("2")) == 151
    assert coverage.target_frames(Decimal("299.999999999999999999"), Decimal("2")) == 150


def test_duration_coverage_stops_at_the_shortest_whole_segment_prefix() -> None:
    coverage = _coverage()
    target = coverage.CoverageTarget("duration_s", Decimal("60"))
    state = coverage.CoverageAccumulator(target)

    for value in (Decimal("18"), Decimal("27"), Decimal("22")):
        state = coverage.advance_coverage(
            state, coverage.VideoMetric("duration_s", value)
        )

    assert state.confirmed == Decimal("67")
    assert state.confirmed_segments == 3
    assert state.reached is True
    decision = coverage.finish_coverage(
        state, coverage.CoverageFinishFacts(collection_complete=False)
    )
    assert decision.status == "reached"
    assert decision.confirmed == Decimal("67")


def test_exact_target_stops_before_accepting_another_segment() -> None:
    coverage = _coverage()
    state = coverage.CoverageAccumulator(
        coverage.CoverageTarget("duration_s", Decimal("45"))
    )
    state = coverage.advance_coverage(
        state, coverage.VideoMetric("duration_s", Decimal("18"))
    )
    state = coverage.advance_coverage(
        state, coverage.VideoMetric("duration_s", Decimal("27"))
    )

    assert state.reached is True
    with pytest.raises(ValueError):
        coverage.advance_coverage(
            state, coverage.VideoMetric("duration_s", Decimal("22"))
        )


def test_duration_accumulation_preserves_all_decimal_digits() -> None:
    coverage = _coverage()
    state = coverage.CoverageAccumulator(
        coverage.CoverageTarget("duration_s", Decimal("99999999999999999999"))
    )
    state = coverage.advance_coverage(
        state,
        coverage.VideoMetric(
            "duration_s", Decimal("9007199254740993.00000000000000000000000000001")
        ),
    )
    state = coverage.advance_coverage(
        state, coverage.VideoMetric("duration_s", Decimal("0.00000000000000000000000000001"))
    )

    assert state.confirmed == Decimal(
        "9007199254740993.00000000000000000000000000002"
    )


def test_native_timelapse_adds_actual_integer_frames() -> None:
    coverage = _coverage()
    state = coverage.CoverageAccumulator(
        coverage.CoverageTarget("frames", 151)
    )
    for count in (60, 60, 45):
        state = coverage.advance_coverage(
            state, coverage.VideoMetric("frames", count)
        )

    assert state.confirmed == 165
    assert type(state.confirmed) is int
    assert state.confirmed_segments == 3
    assert state.reached is True
    assert coverage.finish_coverage(
        state, coverage.CoverageFinishFacts(collection_complete=False)
    ).status == "reached"


def test_unknown_metric_freezes_prefix_and_cannot_be_skipped() -> None:
    coverage = _coverage()
    state = coverage.CoverageAccumulator(
        coverage.CoverageTarget("duration_s", Decimal("60"))
    )
    state = coverage.advance_coverage(
        state, coverage.VideoMetric("duration_s", Decimal("18"))
    )
    state = coverage.advance_coverage(
        state, coverage.VideoMetric("duration_s", None, "metric_unknown")
    )

    assert state.confirmed == Decimal("18")
    assert state.confirmed_segments == 1
    assert state.reached is False
    assert coverage.finish_coverage(
        state, coverage.CoverageFinishFacts(collection_complete=True)
    ).status == "unconfirmed"
    with pytest.raises(ValueError):
        coverage.advance_coverage(
            state, coverage.VideoMetric("duration_s", Decimal("100"))
        )


def test_unknown_first_segment_leaves_an_empty_unconfirmed_prefix() -> None:
    coverage = _coverage()
    state = coverage.CoverageAccumulator(
        coverage.CoverageTarget("duration_s", Decimal("60"))
    )

    state = coverage.advance_coverage(
        state, coverage.VideoMetric("duration_s", None, "copy_failed")
    )
    decision = coverage.finish_coverage(
        state, coverage.CoverageFinishFacts(collection_complete=False)
    )

    assert decision.status == "unconfirmed"
    assert decision.confirmed == Decimal("0")
    assert decision.confirmed_segments == 0


def test_only_a_complete_reliable_collection_can_be_confirmed_short() -> None:
    coverage = _coverage()
    state = coverage.CoverageAccumulator(
        coverage.CoverageTarget("duration_s", Decimal("60"))
    )
    state = coverage.advance_coverage(
        state, coverage.VideoMetric("duration_s", Decimal("18"))
    )
    state = coverage.advance_coverage(
        state, coverage.VideoMetric("duration_s", Decimal("27"))
    )

    assert coverage.finish_coverage(
        state, coverage.CoverageFinishFacts(collection_complete=False)
    ).status == "unconfirmed"
    decision = coverage.finish_coverage(
        state, coverage.CoverageFinishFacts(collection_complete=True)
    )
    assert decision.status == "confirmed_short"
    assert decision.confirmed == Decimal("45")


@pytest.mark.parametrize(
    ("metric", "value"),
    [
        ("frames", Decimal("12")),
        ("frames", True),
        ("duration_s", 12.5),
    ],
)
def test_coverage_rejects_non_exact_or_wrong_dimension_values(metric, value) -> None:
    coverage = _coverage()

    with pytest.raises(ValueError):
        coverage.VideoMetric(metric, value)


def test_coverage_rejects_mixed_dimensions() -> None:
    coverage = _coverage()
    state = coverage.CoverageAccumulator(
        coverage.CoverageTarget("frames", 151)
    )

    with pytest.raises(ValueError):
        coverage.advance_coverage(
            state, coverage.VideoMetric("duration_s", Decimal("18"))
        )


@pytest.mark.parametrize(
    ("duration_s", "interval_s"),
    [(Decimal("1"), Decimal("0")), (Decimal("-1"), Decimal("2")), (1.5, 2)],
)
def test_target_frames_rejects_invalid_or_inexact_inputs(duration_s, interval_s) -> None:
    coverage = _coverage()

    with pytest.raises(ValueError):
        coverage.target_frames(duration_s, interval_s)

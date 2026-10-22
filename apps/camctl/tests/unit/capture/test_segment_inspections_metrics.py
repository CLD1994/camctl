"""逐段覆盖指标只使用可确认的主视频流事实。"""

from decimal import Decimal

import pytest

from camctl.capture.segment_inspections import _metric_from_probe
from camctl.capture.video_coverage import CoverageMetric, CoverageTarget
from camctl.host_files.media import MediaProbe, MediaStream


@pytest.mark.parametrize(
    ("metric", "expected"),
    [
        (CoverageMetric.DURATION_S, Decimal("12.5")),
        (CoverageMetric.FRAMES, 375),
    ],
)
def test_missing_container_duration_keeps_reliable_main_stream_metric(metric, expected):
    probe = MediaProbe(
        duration_s=None,
        error="missing_duration: 容器时长字段缺失",
        streams=(MediaStream(
            index=0,
            codec_type="video",
            codec_name="h264",
            width=1920,
            height=1080,
            duration_s=Decimal("12.5"),
            attached_pic=False,
            frame_count=375,
            fps=Decimal("30"),
        ),),
    )

    actual = _metric_from_probe(CoverageTarget(metric, 60), probe)

    assert actual.metric is metric
    assert actual.value == expected
    assert actual.reason is None


def test_parse_error_does_not_trust_stream_like_facts():
    probe = MediaProbe(
        duration_s=None,
        error="invalid_structure: 流事实结构非法",
        streams=(MediaStream(
            index=0,
            codec_type="video",
            codec_name="h264",
            width=1920,
            height=1080,
            duration_s=Decimal("12.5"),
            attached_pic=False,
            frame_count=375,
            fps=Decimal("30"),
        ),),
    )

    actual = _metric_from_probe(CoverageTarget(CoverageMetric.DURATION_S, 60), probe)

    assert actual.value is None
    assert actual.reason == "probe_error"


def test_confirmed_no_video_code_is_preserved_for_recovery_classification():
    probe = MediaProbe(
        duration_s=None,
        error="no_video_stream: 容器没有视频流",
        streams=(),
    )

    actual = _metric_from_probe(CoverageTarget(CoverageMetric.DURATION_S, 60), probe)

    assert actual.value is None
    assert actual.reason == "no_video_stream"

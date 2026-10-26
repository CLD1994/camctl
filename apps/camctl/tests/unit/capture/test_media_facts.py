"""媒体逐流指标的持久事实编码与读取。"""

from decimal import Decimal

import pytest

from camctl.capture.media_facts import probe_document, read_probe
from camctl.host_files.media import MediaProbe, MediaStream, MediaToolResult


def test_probe_document_round_trips_actual_video_metrics() -> None:
    original = MediaProbe(
        Decimal("2.5"), None,
        (MediaStream(
            0, "video", "h264", 32, 24, Decimal("2.125"), False,
            frame_count=64, fps=Decimal("29.97002997002997002997002997"),
        ),),
        "mp4",
    )

    decoded = read_probe(probe_document(original))

    assert decoded == original


def test_read_probe_preserves_older_media_fact_documents() -> None:
    legacy = {
        "duration_s": Decimal("3"),
        "error": None,
        "format_name": "mp4",
        "streams": [{
            "index": 0,
            "codec_type": "video",
            "codec_name": "h264",
            "width": 32,
            "height": 24,
            "duration_s": Decimal("3"),
            "attached_pic": False,
        }],
    }

    probe = read_probe(legacy)

    assert probe.streams[0].frame_count is None
    assert probe.streams[0].fps is None
    assert probe.tool_diagnostics is None


@pytest.mark.parametrize("control", [True, False])
def test_probe_document_keeps_only_explicit_tool_control_fact(control) -> None:
    original = MediaProbe(None, "tool_failed: original", tool_diagnostics=
        MediaToolResult(control_stopped=control))

    document = probe_document(original)
    restored = read_probe(document)

    assert document == {"duration_s": None, "error": original.error,
        "format_name": None, "streams": [], "tool_control": control}
    assert restored.tool_diagnostics.control_only is control
    assert restored.tool_diagnostics.outcome is None
    assert restored.tool_diagnostics.exception is None


@pytest.mark.parametrize("control", [None, 0, 1, "true", []])
def test_read_probe_rejects_non_boolean_tool_control(control) -> None:
    document = probe_document(MediaProbe(None, "tool_failed: original"))
    document["tool_control"] = control
    with pytest.raises(ValueError):
        read_probe(document)


@pytest.mark.parametrize("frame_count", [True, -1, Decimal("2"), "2.5"])
def test_read_probe_rejects_invalid_frame_counts(frame_count) -> None:
    document = {
        "duration_s": Decimal("3"),
        "error": None,
        "format_name": "mp4",
        "streams": [{
            "index": 0,
            "codec_type": "video",
            "codec_name": "h264",
            "width": 32,
            "height": 24,
            "duration_s": Decimal("3"),
            "attached_pic": False,
            "frame_count": frame_count,
            "fps": Decimal("30"),
        }],
    }

    with pytest.raises(ValueError):
        read_probe(document)

"""异常分段场景使用可由真实媒体工具读取的 MP4 样例。"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess

import pytest

from _action6_segmented_files import create_segmented_media_manifest


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="真实媒体 fixture 需要 ffmpeg 和 ffprobe",
)
@pytest.mark.parametrize(
    ("metric", "expected"),
    [
        ("duration_s", (18, 27, 22, 40)),
        ("frames", (60, 60, 45, 20)),
    ],
    ids=("ordinary-durations", "native-frame-counts"),
)
def test_action6_media_fixture_contains_real_ordered_segments_and_photo(
    tmp_path, metric, expected,
):
    manifest, contents, facts = create_segmented_media_manifest(
        tmp_path, metric=metric, include_jpeg=True,
    )
    entries = json.loads(manifest.read_text(encoding="utf-8"))
    assert len(entries) == 9  # four MP4, four paired LRF and one JPEG
    assert len(facts) == 4
    assert [row["ordinal"] for row in facts] == [1, 2, 3, 4]
    assert [row[metric] for row in facts] == list(expected)

    for offset, row in enumerate(facts):
        path = row["source"]
        assert contents[row["device_path"]] == path.read_bytes()
        assert row["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        probed = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-count_frames", "-show_entries",
                "stream=codec_type,codec_name,duration,nb_read_frames",
                "-of", "json", str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        stream, = json.loads(probed.stdout)["streams"]
        assert stream["codec_type"] == "video"
        assert stream["codec_name"] == "h264"
        if metric == "duration_s":
            assert float(stream["duration"]) == expected[offset]
        else:
            assert int(stream["nb_read_frames"]) == expected[offset]

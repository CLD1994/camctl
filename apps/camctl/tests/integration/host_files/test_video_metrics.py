"""真实媒体工具从小型本地文件取得主视频实际帧数与帧率。"""

from __future__ import annotations

import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.host_files.media import ProbeRequest, select_main_video_stream
from camctl.host_files.models import BoundDirectories, FilePurpose, FileRef
from media_helpers import probe_media

pytestmark = pytest.mark.asyncio


async def test_real_ffprobe_reads_main_video_metrics_from_complete_local_file(
    tmp_path: Path,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    assert ffmpeg is not None, "该集成门槛需要部署规格提供的 ffmpeg"
    assert ffprobe is not None, "该集成门槛需要部署规格提供的 ffprobe"

    staging = tmp_path / "staging"
    input_dir = staging / "recording-inputs"
    input_dir.mkdir(parents=True)
    input_path = input_dir / "71.mp4"
    subprocess.run(
        [
            ffmpeg, "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=32x24:r=3:d=2",
            "-frames:v", "6", "-an", "-c:v", "mpeg4", str(input_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    roots = BoundDirectories(staging=staging)
    input_ref = FileRef(
        file_id=71,
        purpose=FilePurpose.RECORDING_INPUT,
        relative_path="recording-inputs/71.mp4",
        root=staging,
    )

    probe = await probe_media(
        input_ref, roots, ProbeRequest(ffprobe=ffprobe, count_frames=True)
    )

    assert probe.error is None
    stream = select_main_video_stream(probe)
    assert stream is not None
    assert stream.duration_s == Decimal("2")
    assert stream.frame_count == 6
    assert stream.fps == Decimal("3")
    assert input_path.is_file() and input_path.stat().st_size > 0

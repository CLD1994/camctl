"""Action6 分段安装链使用的显式 ADB 文件与内容样例。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess


REMOTE_DIRECTORY = "/mnt/media_rw/emulated/DCIM"


def create_segmented_file_manifest(
        root: Path, *, include_jpeg: bool) -> tuple[Path, dict[str, bytes]]:
    """Create reverse-listed, unequal-size files across the DJI_001/002 seam.

    These opaque bytes exercise the normal result path, which must not copy
    files for media inspection. The ADB fake executes production-generated
    ShellFileTools scripts against them and the client chain verifies bytes.
    """
    source_dir = root / "action6-segment-fixtures"
    source_dir.mkdir(parents=True)
    segments = (
        ("DJI_001", "DJI_20991231235959_0999_D", 73_111, b"segment-0999\x00"),
        ("DJI_002", "DJI_20000101000000_0001_D", 131_077, b"segment-0001\x00"),
    )
    contents: dict[str, bytes] = {}
    entries = []
    # Reverse the file listing and use reverse datetime order so the production
    # result pipeline must rely on the declared directory/index key.
    for directory, stem, size, marker in reversed(segments):
        for suffix, body in (
                ("LRF", b"preview:" + marker),
                ("MP4", marker)):
            path = f"{REMOTE_DIRECTORY}/{directory}/{stem}.{suffix}"
            content = (body * ((size + len(body) - 1) // len(body)))[:size]
            source = source_dir / f"{directory}-{stem.rsplit('_', 1)[-1]}.{suffix}"
            source.write_bytes(content)
            entries.append({"path": path, "source": str(source)})
            contents[path] = content
    if include_jpeg:
        path = f"{REMOTE_DIRECTORY}/DJI_002/FRAME_0001.JPG"
        content = b"\xff\xd8action6-independent-jpeg\x00" * 997 + b"\xff\xd9"
        source = source_dir / "FRAME_0001.JPG"
        source.write_bytes(content)
        entries.append({"path": path, "source": str(source)})
        contents[path] = content

    manifest = root / "action6-segment-files.json"
    manifest.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    video_paths = [path for path in contents if path.endswith(".MP4")]
    assert len(video_paths) == 2
    assert len({len(contents[path]) for path in video_paths}) == 2
    return manifest, contents


def create_segmented_media_manifest(
        root: Path, *, metric: str, include_jpeg: bool,
        values: tuple[int, ...] | None = None,
) -> tuple[Path, dict[str, bytes], list[dict]]:
    """Build real H.264 members and return their measured fixture facts.

    Ordinary values are whole seconds; native values are exact frame counts.
    The LRF copies are valid media files paired by basename, and the optional
    JPEG is a valid independently generated image. Production still receives
    only the ordinary ADB path/source manifest.
    """
    if metric not in ("duration_s", "frames"):
        raise ValueError("metric must be duration_s or frames")
    values = values or ((18, 27, 22, 40) if metric == "duration_s" else (60, 60, 45, 20))
    if not values or any(not isinstance(value, int) or value <= 0 for value in values):
        raise ValueError("segment values must be positive integers")
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise RuntimeError("real segmented media fixtures require ffmpeg and ffprobe")

    source_dir = root / "action6-real-segment-fixtures"
    source_dir.mkdir(parents=True)
    manifest = root / "action6-segment-media-files.json"
    contents: dict[str, bytes] = {}
    entries = []
    facts: list[dict] = []
    frame_rate = 10 if metric == "duration_s" else 30
    resolution = "480x360" if metric == "duration_s" else "160x120"
    directory_indices = ("DJI_001", "DJI_001", "DJI_002", "DJI_002")
    index_values = (998, 999, 1, 2)

    for offset, value in enumerate(values):
        index = index_values[offset] if len(values) == 4 else offset + 1
        directory = directory_indices[offset] if len(values) == 4 else "DJI_001"
        timestamp = f"2026102312{offset:02d}00"
        stem = f"DJI_{timestamp}_{index:04d}_D"
        device_path = f"{REMOTE_DIRECTORY}/{directory}/{stem}.MP4"
        source = source_dir / f"{directory}-{index:04d}.MP4"
        frame_count = value * frame_rate if metric == "duration_s" else value
        duration = frame_count / frame_rate
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-y", "-f", "lavfi", "-i",
                f"testsrc2=size={resolution}:rate={frame_rate}:duration={duration}",
                "-frames:v", str(frame_count), "-an", "-c:v", "libx264",
                "-preset", "ultrafast", "-crf", "24", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", str(source),
            ],
            check=True,
            capture_output=True,
        )
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-count_frames", "-show_entries",
                "stream=codec_type,codec_name,duration,nb_read_frames",
                "-of", "json", str(source),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        stream, = json.loads(probe.stdout)["streams"]
        if stream.get("codec_name") != "h264":
            raise AssertionError(f"generated segment is not H.264: {source}")
        measured_frames = int(stream["nb_read_frames"])
        measured_duration = float(stream["duration"])
        if (
            measured_frames != frame_count
            or metric == "duration_s" and measured_duration != duration
        ):
            raise AssertionError(
                f"generated fixture metric differs from target: {source}: "
                f"frames={measured_frames}/{frame_count}, "
                f"duration={measured_duration}/{duration}"
            )

        content = source.read_bytes()
        contents[device_path] = content
        entries.append({"path": device_path, "source": str(source)})
        preview_path = device_path.removesuffix(".MP4") + ".LRF"
        preview_source = source_dir / f"{directory}-{index:04d}.LRF"
        shutil.copyfile(source, preview_source)
        contents[preview_path] = preview_source.read_bytes()
        entries.append({"path": preview_path, "source": str(preview_source)})
        facts.append({
            "ordinal": offset + 1,
            "index": index,
            "device_path": device_path,
            "source": source,
            "sha256": hashlib.sha256(content).hexdigest(),
            "duration_s": int(value) if metric == "duration_s" else measured_duration,
            "frames": measured_frames,
        })

    if include_jpeg:
        device_path = f"{REMOTE_DIRECTORY}/DJI_002/FRAME_0001.JPG"
        source = source_dir / "FRAME_0001.JPG"
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-y", "-f", "lavfi", "-i", "color=c=yellow:s=64x64:r=1:d=1",
                "-frames:v", "1", "-c:v", "mjpeg", "-q:v", "3", str(source),
            ],
            check=True,
            capture_output=True,
        )
        contents[device_path] = source.read_bytes()
        entries.append({"path": device_path, "source": str(source)})
    manifest.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return manifest, contents, facts

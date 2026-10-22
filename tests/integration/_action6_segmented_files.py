"""Action6 分段安装链使用的显式 ADB 文件与内容样例。"""

from __future__ import annotations

import json
from pathlib import Path


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

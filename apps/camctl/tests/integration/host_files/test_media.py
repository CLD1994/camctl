"""F6 受管媒体工具与真实子进程的组合集成测试。

可执行替身工具经真实 O3 受管执行验证成功、失败、部分成品与取
消；真实 ffprobe/ffmpeg 存在时补充真实工具用例；未安装工具按处
理错误分类。
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.host_files.media import (
    ProbeRequest,
    RepairRequest,
    probe_media,
    repair_media,
)
from camctl.host_files.models import BoundDirectories, FilePurpose, FileRef

pytestmark = pytest.mark.asyncio

_PROBE_BODY = """
import json, sys
args = sys.argv[1:]
input_path = args[-1]
with open(input_path, "rb") as source:
    size = len(source.read())
print(json.dumps({"format": {"duration": f"{size / 100:.6f}"}}))
"""

_REPAIR_BODY = """
import sys, time
args = sys.argv[1:]
input_path = args[args.index("-i") + 1]
output_path = args[-1]
with open(input_path, "rb") as source:
    data = source.read()
if "--fail" in args:
    with open(output_path, "wb") as target:
        target.write(data[: len(data) // 2])
    sys.exit(1)
if "--sleep" in args:
    time.sleep(5)
with open(output_path, "wb") as target:
    target.write(b"repaired:" + data)
"""


def _tool(tmp_path: Path, name: str, body: str) -> str:
    helper = tmp_path / f"{name}.py"
    helper.write_text(body, encoding="utf-8")
    if sys.platform == "win32":
        launcher = tmp_path / f"{name}.cmd"
        launcher.write_text(
            f'@{sys.executable} "{helper}" %*\n', encoding="utf-8"
        )
        return str(launcher)
    launcher = tmp_path / f"{name}.sh"
    launcher.write_text(
        f'#!/bin/sh\nexec {sys.executable} "{helper}" "$@"\n', encoding="utf-8"
    )
    launcher.chmod(0o755)
    return str(launcher)


def _setup(tmp_path: Path) -> tuple[FileRef, FileRef, BoundDirectories, Path, Path]:
    staging = tmp_path / "staging"
    (staging / "recording-inputs").mkdir(parents=True)
    (staging / "derived").mkdir()
    input_path = staging / "recording-inputs" / "11.mp4"
    input_path.write_bytes(b"x" * 250)
    output_path = staging / "derived" / "12.mp4"
    roots = BoundDirectories(staging=staging)
    input_ref = FileRef(
        file_id=11,
        purpose=FilePurpose.RECORDING_INPUT,
        relative_path="recording-inputs/11.mp4",
        root=staging,
    )
    output_ref = FileRef(
        file_id=12,
        purpose=FilePurpose.REPAIR_OUTPUT,
        relative_path="derived/12.mp4",
        root=staging,
    )
    return input_ref, output_ref, roots, input_path, output_path


async def test_real_subprocess_probe_success(tmp_path: Path) -> None:
    input_ref, _, roots, input_path, _ = _setup(tmp_path)
    tool = _tool(tmp_path, "probe", _PROBE_BODY)
    probe = await probe_media(input_ref, roots, ProbeRequest(ffprobe=tool))
    assert probe.error is None
    assert probe.duration_s == Decimal("2.500000")
    assert input_path.read_bytes() == b"x" * 250


async def test_real_subprocess_repair_success_and_failure(tmp_path: Path) -> None:
    input_ref, output_ref, roots, input_path, output_path = _setup(tmp_path)
    tool = _tool(tmp_path, "repair", _REPAIR_BODY)
    ok = await repair_media(
        input_ref, output_ref, roots, RepairRequest(ffmpeg=tool)
    )
    assert ok.complete is True
    assert ok.error is None
    assert output_path.read_bytes() == b"repaired:" + b"x" * 250
    assert ok.size_bytes == len(b"repaired:") + 250
    assert ok.digest == hashlib.sha256(b"repaired:" + b"x" * 250).hexdigest()
    # 源文件不被媒体调用修改。
    assert input_path.read_bytes() == b"x" * 250

    output_path.unlink()
    failed = await repair_media(
        input_ref, output_ref, roots, RepairRequest(ffmpeg=tool, output_args=("--fail",))
    )
    assert failed.complete is False
    assert failed.exists is True
    assert failed.error is not None
    assert failed.error.startswith("tool_failed")
    assert output_path.read_bytes() == b"x" * 125


async def test_cancel_terminates_media_tool(tmp_path: Path) -> None:
    input_ref, output_ref, roots, _, _ = _setup(tmp_path)
    tool = _tool(tmp_path, "repair", _REPAIR_BODY)

    class CancelAfterDelay:
        def __init__(self) -> None:
            self._event = asyncio.Event()

        async def requested(self) -> None:
            await asyncio.sleep(0.2)
            self._event.set()

    artifact = await repair_media(
        input_ref,
        output_ref,
        roots,
        RepairRequest(ffmpeg=tool, output_args=("--sleep",)),
        stop=CancelAfterDelay(),
    )
    assert artifact.complete is False
    assert artifact.error is not None


async def test_missing_tool_is_classified(tmp_path: Path) -> None:
    input_ref, _, roots, _, _ = _setup(tmp_path)
    probe = await probe_media(
        input_ref, roots, ProbeRequest(ffprobe=str(tmp_path / "no-such-tool.exe"))
    )
    assert probe.error is not None
    assert probe.error.startswith("tool_unavailable")

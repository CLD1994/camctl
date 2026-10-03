"""F6 受管媒体工具与真实子进程的组合集成测试。

可执行替身工具经真实 O3 受管执行验证成功、失败、部分成品与取
消。本文件验证进程和文件协作，不证明真实视频时长或修复正确性。
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import sys
from decimal import Decimal
from pathlib import Path

import pytest

import camctl.host_files.io as file_io
from camctl.host_files.io import DirectorySyncStage
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


async def test_subprocess_numeric_duration_keeps_all_digits(tmp_path: Path) -> None:
    input_ref, _, roots, _, _ = _setup(tmp_path)
    tool = _tool(
        tmp_path, "exact-probe",
        'print(\'{"format":{"duration":1.00000000000000000000000000001e-3}}\')',
    )
    result = await probe_media(input_ref, roots, ProbeRequest(ffprobe=tool))
    assert result.error is None
    assert result.duration_s == Decimal("0.00100000000000000000000000000001")


@pytest.mark.skipif(sys.platform == "win32", reason="需要 POSIX SIGTERM 处理器")
@pytest.mark.parametrize("entry", ["probe", "repair"])
async def test_cancelled_tool_zero_exit_is_not_success(tmp_path: Path, entry: str) -> None:
    input_ref, output_ref, roots, input_path, output_path = _setup(tmp_path)
    tool = _tool(tmp_path, "cancel-zero", """
import signal, sys
from pathlib import Path
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
if "-i" in sys.argv:
    Path(sys.argv[-1]).write_bytes(b"partial output")
else:
    print('{"format":{"duration":"1.25"}}', flush=True)
Path(__file__).with_suffix(".ready").write_text("ready")
while True:
    signal.pause()
""")
    ready = tmp_path / "cancel-zero.ready"

    class StopWhenReady:
        async def requested(self) -> None:
            # 以工具已装好信号处理器并写出内容为条件；期限仅使测试故障能收场。
            deadline = asyncio.get_running_loop().time() + 5
            while not ready.exists() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)

    if entry == "probe":
        result = await probe_media(input_ref, roots, ProbeRequest(ffprobe=tool), stop=StopWhenReady())
        assert result.duration_s is None
    else:
        result = await repair_media(
            input_ref, output_ref, roots, RepairRequest(ffmpeg=tool), stop=StopWhenReady()
        )
        assert result.complete is False
        assert result.exists is True
        assert output_path.read_bytes() == b"partial output"
    assert ready.exists(), "工具未到达可取消阶段"
    assert result.error is not None
    assert result.error.startswith("tool_failed:")
    assert "cancelled" in result.error
    assert "exit=0" in result.error
    assert input_path.read_bytes() == b"x" * 250


@pytest.mark.parametrize("phase", ["file", "directory"])
async def test_repair_real_file_sync_error_keeps_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    input_ref, output_ref, roots, _, output_path = _setup(tmp_path)
    tool = _tool(tmp_path, "repair", _REPAIR_BODY)

    def fail_sync(*args):
        raise OSError(errno.EIO, "sync disk")

    monkeypatch.setattr(file_io, "_DIRECTORY_SYNC_SUPPORTED", True)
    monkeypatch.setattr(file_io, "_fsync" if phase == "file" else "_sync_directory", fail_sync)
    result = await repair_media(input_ref, output_ref, roots, RepairRequest(ffmpeg=tool))
    assert result.exists is True
    assert result.complete is True
    assert result.size_bytes == 259
    assert result.digest == hashlib.sha256(output_path.read_bytes()).hexdigest()
    assert result.file_synced is (phase == "directory")
    assert result.directory is (DirectorySyncStage.FAILED if phase == "directory"
                                else DirectorySyncStage.NOT_ATTEMPTED)
    assert result.error is not None and "sync_failed" in result.error


async def test_repair_keeps_both_real_close_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_ref, output_ref, roots, _, output_path = _setup(tmp_path)
    tool = _tool(tmp_path, "repair", _REPAIR_BODY)

    def close_then_report_error(fd: int) -> None:
        os.close(fd)
        raise OSError(errno.EIO, "close disk")

    monkeypatch.setattr(file_io, "_close", close_then_report_error)
    result = await repair_media(input_ref, output_ref, roots, RepairRequest(ffmpeg=tool))
    assert result.complete is False
    assert result.digest == hashlib.sha256(output_path.read_bytes()).hexdigest()
    assert result.checksum.error is not None
    assert result.synchronization.error is not None
    assert result.file_synced is True
    assert result.directory is DirectorySyncStage.NOT_ATTEMPTED
    assert result.error.count("close_failed") == 2


async def test_repair_output_directory_does_not_qualify(tmp_path: Path) -> None:
    input_ref, output_ref, roots, _, output_path = _setup(tmp_path)
    output_path.mkdir()
    tool = _tool(tmp_path, "repair", "pass")
    result = await repair_media(input_ref, output_ref, roots, RepairRequest(ffmpeg=tool))
    assert result.exists is True
    assert result.complete is False
    assert result.checksum is None
    assert result.synchronization is None
    assert "output_type_mismatch" in result.error


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 目录访问权限")
async def test_repair_tool_and_inspection_errors_are_both_preserved(tmp_path: Path) -> None:
    input_ref, output_ref, roots, _, output_path = _setup(tmp_path)
    tool = _tool(tmp_path, "repair", _REPAIR_BODY)
    output_path.parent.chmod(0o000)
    try:
        result = await repair_media(input_ref, output_ref, roots, RepairRequest(ffmpeg=tool))
    finally:
        output_path.parent.chmod(0o755)
    assert result.exists is None
    assert result.complete is False
    assert "tool_failed" in result.error
    assert "inspect_failed" in result.error

"""F6 受管媒体工具执行的单元测试。

工具退出与成品校验分别确认：工具失败但文件存在不是完成；时长保
持全精度不舍入；非法结构与读取错误单独分类；媒体调用经 O3 受管
执行。工具执行与文件检查经窄注入点替身。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

import camctl.host_files.media as media
from camctl.host_files.io import DirectorySyncStage, HashResult, SyncResult
from camctl.host_files.media import (
    MediaArtifact,
    MediaProbe,
    ProbeRequest,
    RepairRequest,
    probe_media,
    repair_media,
)
from camctl.host_files.models import BoundDirectories, FilePurpose, FileRef
from camctl.operations.process import LocalExit, RawToolOutcome

pytestmark = pytest.mark.asyncio

_ROOT = Path("D:\\state")


class NeverStop:
    async def requested(self) -> None:
        import asyncio

        await asyncio.Future()


class FakeMedia:
    """窄注入点替身：脚本化工具结果与文件事实。"""

    def __init__(self) -> None:
        self.outcome = RawToolOutcome(
            exit=LocalExit(exit_code=0), output=b"{}", error=None, used_grace_s=None
        )
        self.fail_spawn: Exception | None = None
        self.argvs: list[tuple[str, ...]] = []
        self.output_exists = False
        self.output_size = 0
        self.digest = HashResult(digest="a" * 64, size_bytes=1, error=None)
        self.sync = SyncResult(
            file_synced=True, directory=DirectorySyncStage.UNSUPPORTED, error=None
        )

    async def execute(self, spec, *, stop, spawner=None):
        self.argvs.append(spec.argv)
        if self.fail_spawn is not None:
            raise self.fail_spawn
        return self.outcome

    def exists(self, path: Path) -> bool:
        return self.output_exists

    def size(self, path: Path) -> int:
        return self.output_size

    def hash_target(self, ref, roots):
        return self.digest

    def sync_target(self, ref, roots):
        return self.sync


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeMedia:
    fm = FakeMedia()
    monkeypatch.setattr(media, "_execute_tool", fm.execute)
    monkeypatch.setattr(media, "_file_exists", fm.exists)
    monkeypatch.setattr(media, "_file_size", fm.size)
    monkeypatch.setattr(media, "_file_digest", fm.hash_target)
    monkeypatch.setattr(media, "_file_sync", fm.sync_target)
    return fm


def _input_ref() -> FileRef:
    return FileRef(
        file_id=11,
        purpose=FilePurpose.RECORDING_INPUT,
        relative_path="recording-inputs/11.mp4",
        root=_ROOT / "staging",
    )


def _output_ref() -> FileRef:
    return FileRef(
        file_id=12,
        purpose=FilePurpose.REPAIR_OUTPUT,
        relative_path="derived/12.mp4",
        root=_ROOT / "staging",
    )


def _roots() -> BoundDirectories:
    return BoundDirectories(staging=_ROOT / "staging")


async def test_failed_media_output_is_not_complete(fake: FakeMedia) -> None:
    """工具失败但输出文件存在：不是完成，部分成品不作正式产物。"""
    fake.outcome = RawToolOutcome(
        exit=LocalExit(exit_code=1), output=b"", error=None, used_grace_s=None
    )
    fake.output_exists = True
    fake.output_size = 1024
    artifact = await repair_media(
        _input_ref(), _output_ref(), _roots(), RepairRequest()
    )
    assert isinstance(artifact, MediaArtifact)
    assert artifact.exists is True
    assert artifact.complete is False
    assert artifact.error is not None
    assert artifact.error.startswith("tool_failed")


async def test_successful_repair_collects_full_evidence(fake: FakeMedia) -> None:
    fake.output_exists = True
    fake.output_size = 2048
    artifact = await repair_media(
        _input_ref(), _output_ref(), _roots(),
        RepairRequest(output_args=("-c", "copy")),
    )
    assert artifact.complete is True
    assert artifact.exists is True
    assert artifact.size_bytes == 2048
    assert artifact.digest == "a" * 64
    assert artifact.file_synced is True
    assert artifact.error is None
    argv = fake.argvs[0]
    assert argv[0] == "ffmpeg"
    assert "-nostdin" in argv and "-y" in argv
    assert argv.index("-c") < argv.index(str(_ROOT / "staging" / "derived" / "12.mp4"))


async def test_zero_exit_but_missing_output(fake: FakeMedia) -> None:
    fake.output_exists = False
    artifact = await repair_media(
        _input_ref(), _output_ref(), _roots(), RepairRequest()
    )
    assert artifact.complete is False
    assert artifact.exists is False
    assert artifact.error is not None
    assert artifact.error.startswith("output_missing")


async def test_zero_exit_but_empty_output(fake: FakeMedia) -> None:
    fake.output_exists = True
    fake.output_size = 0
    artifact = await repair_media(
        _input_ref(), _output_ref(), _roots(), RepairRequest()
    )
    assert artifact.complete is False
    assert artifact.error is not None
    assert artifact.error.startswith("output_empty")


async def test_hash_failure_keeps_exit_and_sync_evidence(fake: FakeMedia) -> None:
    fake.output_exists = True
    fake.output_size = 10
    fake.digest = HashResult(digest=None, size_bytes=None, error="read_failed: x")
    artifact = await repair_media(
        _input_ref(), _output_ref(), _roots(), RepairRequest()
    )
    assert artifact.complete is False
    assert artifact.error is not None
    assert artifact.error.startswith("hash_failed")
    assert artifact.file_synced is True


async def test_probe_duration_keeps_full_precision(fake: FakeMedia) -> None:
    fake.outcome = RawToolOutcome(
        exit=LocalExit(exit_code=0),
        output=b'{"format": {"duration": "3.016999"}}',
        error=None,
        used_grace_s=None,
    )
    probe = await probe_media(_input_ref(), _roots(), ProbeRequest())
    assert isinstance(probe, MediaProbe)
    assert probe.error is None
    assert probe.duration_s == Decimal("3.016999")
    # 全精度保留：不舍入到目标毫秒。
    assert probe.duration_s != Decimal("3.017")


async def test_probe_invalid_structure(fake: FakeMedia) -> None:
    fake.outcome = RawToolOutcome(
        exit=LocalExit(exit_code=0), output=b"not json", error=None, used_grace_s=None
    )
    probe = await probe_media(_input_ref(), _roots(), ProbeRequest())
    assert probe.duration_s is None
    assert probe.error is not None
    assert probe.error.startswith("invalid_structure")


async def test_probe_missing_duration(fake: FakeMedia) -> None:
    fake.outcome = RawToolOutcome(
        exit=LocalExit(exit_code=0),
        output=b'{"format": {}}',
        error=None,
        used_grace_s=None,
    )
    probe = await probe_media(_input_ref(), _roots(), ProbeRequest())
    assert probe.duration_s is None
    assert probe.error is not None
    assert probe.error.startswith("missing_duration")


async def test_probe_tool_failed(fake: FakeMedia) -> None:
    fake.outcome = RawToolOutcome(
        exit=LocalExit(signal=1), output=b"", error=None, used_grace_s=None
    )
    probe = await probe_media(_input_ref(), _roots(), ProbeRequest())
    assert probe.duration_s is None
    assert probe.error is not None
    assert probe.error.startswith("tool_failed")


async def test_tool_unavailable(fake: FakeMedia) -> None:
    fake.fail_spawn = FileNotFoundError("no ffprobe")
    probe = await probe_media(_input_ref(), _roots(), ProbeRequest())
    assert probe.error is not None
    assert probe.error.startswith("tool_unavailable")
    fake.output_exists = True
    artifact = await repair_media(
        _input_ref(), _output_ref(), _roots(), RepairRequest()
    )
    assert artifact.complete is False
    assert artifact.error is not None
    assert artifact.error.startswith("tool_unavailable")


async def test_probe_argv_uses_managed_prefix(fake: FakeMedia) -> None:
    await probe_media(
        _input_ref(), _roots(), ProbeRequest(ffprobe="C:\\tools\\ffprobe.exe")
    )
    argv = fake.argvs[0]
    assert argv[0] == "C:\\tools\\ffprobe.exe"
    assert "-of" in argv and "json" in argv
    assert argv[-1] == str(_ROOT / "staging" / "recording-inputs" / "11.mp4")

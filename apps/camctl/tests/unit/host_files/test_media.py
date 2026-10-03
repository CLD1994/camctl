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
from camctl.host_files.models import (
    BoundDirectories, FileObservation, FileObservationKind, FilePurpose, FileRef,
)
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
        self.observe_error: OSError | None = None
        self.is_file = True
        self.digest = HashResult(digest="a" * 64, size_bytes=1, error=None)
        self.sync = SyncResult(
            file_synced=True, directory=DirectorySyncStage.UNSUPPORTED, error=None
        )

    async def execute(self, spec, *, stop, spawner=None):
        self.argvs.append(spec.argv)
        if self.fail_spawn is not None:
            raise self.fail_spawn
        return self.outcome

    def observe(self, ref: FileRef, roots: BoundDirectories) -> FileObservation:
        path = roots.staging / ref.relative_path
        if self.observe_error is not None:
            return FileObservation(FileObservationKind.ERROR, path, error=self.observe_error)
        if not self.output_exists:
            return FileObservation(FileObservationKind.MISSING, path)
        return FileObservation(
            FileObservationKind.VALID_OBJECT if self.is_file else FileObservationKind.TYPE_MISMATCH,
            path, is_file=self.is_file, size_bytes=self.output_size if self.is_file else None,
        )

    def hash_target(self, ref, roots):
        return self.digest

    def sync_target(self, ref, roots):
        return self.sync


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeMedia:
    fm = FakeMedia()
    monkeypatch.setattr(media, "_execute_tool", fm.execute)
    monkeypatch.setattr(media, "_file_digest", fm.hash_target)
    monkeypatch.setattr(media, "_file_sync", fm.sync_target)
    monkeypatch.setattr(media, "observe_file", fm.observe)
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
    fake.digest = HashResult(digest="a" * 64, size_bytes=2048, error=None)
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


@pytest.mark.parametrize("entry", ["probe", "repair"])
@pytest.mark.parametrize("reason", [None, "cancelled", "timeout"])
@pytest.mark.parametrize(
    "exit_value", [LocalExit(exit_code=0), LocalExit(exit_code=1), LocalExit(signal=15), None]
)
async def test_tool_completion_requires_exit_and_no_call_error(
    fake: FakeMedia, entry: str, reason: str | None, exit_value: LocalExit | None
) -> None:
    fake.outcome = RawToolOutcome(
        exit=exit_value,
        output=b'{"format":{"duration":"1.25"}}',
        error=reason,
        used_grace_s=Decimal("5") if reason else None,
    )
    fake.output_exists = True
    fake.output_size = 1
    if entry == "probe":
        result = await probe_media(_input_ref(), _roots(), ProbeRequest())
        succeeded = result.duration_s is not None
    else:
        result = await repair_media(_input_ref(), _output_ref(), _roots(), RepairRequest())
        succeeded = result.complete
        assert result.exists is True

    if reason is None and exit_value == LocalExit(exit_code=0):
        assert succeeded is True
        assert result.error is None
    else:
        assert succeeded is False
        assert result.error is not None
        assert result.error.startswith("tool_failed:")
        if reason:
            assert reason in result.error
        if exit_value is None:
            assert "no-exit" in result.error
        elif exit_value.signal is not None:
            assert f"signal={exit_value.signal}" in result.error
        else:
            assert f"exit={exit_value.exit_code}" in result.error


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ('"3.01699900000000000000000000001"', "3.01699900000000000000000000001"),
        ("3.01699900000000000000000000001", "3.01699900000000000000000000001"),
        ("9007199254740993", "9007199254740993"),
        ("1.00000000000000000000000000001e-3", "0.00100000000000000000000000000001"),
        ("0", "0"),
        ('"0.000000"', "0"),
    ],
)
async def test_probe_numeric_representations_are_exact(
    fake: FakeMedia, literal: str, expected: str
) -> None:
    fake.outcome = RawToolOutcome(
        exit=LocalExit(exit_code=0),
        output=('{"format":{"duration":' + literal + '}}').encode(),
        error=None,
        used_grace_s=None,
    )
    result = await probe_media(_input_ref(), _roots(), ProbeRequest())
    assert result.error is None
    assert result.duration_s == Decimal(expected)


@pytest.mark.parametrize(
    "literal",
    [
        "-0.001", '"-1e-100"', '"NaN"', '"sNaN"', '"Infinity"', '"-Infinity"',
        "NaN", "Infinity", "-Infinity", "true", "false", "null", "[]", "{}",
        '"N/A"', '""',
    ],
)
async def test_probe_invalid_duration_is_unknown(fake: FakeMedia, literal: str) -> None:
    fake.outcome = RawToolOutcome(
        exit=LocalExit(exit_code=0),
        output=('{"format":{"duration":' + literal + '}}').encode(),
        error=None,
        used_grace_s=None,
    )
    result = await probe_media(_input_ref(), _roots(), ProbeRequest())
    assert result.duration_s is None
    assert result.error is not None
    assert result.error.startswith("invalid_structure:")


@pytest.mark.parametrize(
    "payload",
    [
        b'{"format":{"duration":1,"duration":2}}',
        b'{"format":{"duration":1},"format":{"duration":2}}',
        b'{"format":{"duration":"\xff"}}',
        b'{"format":{"duration":1},"other":"\\ud800"}',
        b'[]', b'{"format":null}', b'{"format":[]}',
    ],
)
async def test_probe_malformed_output_is_not_partially_used(
    fake: FakeMedia, payload: bytes
) -> None:
    fake.outcome = RawToolOutcome(
        exit=LocalExit(exit_code=0), output=payload, error=None, used_grace_s=None
    )
    result = await probe_media(_input_ref(), _roots(), ProbeRequest())
    assert result.duration_s is None
    assert result.error is not None
    assert result.error.startswith("invalid_structure:")


@pytest.mark.parametrize("hash_fails", [False, True])
@pytest.mark.parametrize(
    "synced",
    [
        SyncResult(False, DirectorySyncStage.NOT_ATTEMPTED, "fsync_failed: disk"),
        SyncResult(True, DirectorySyncStage.FAILED, "directory_sync_failed: disk"),
        SyncResult(True, DirectorySyncStage.SYNCED, "directory_close_failed: disk"),
    ],
)
async def test_repair_keeps_sync_error_and_independent_hash(
    fake: FakeMedia, synced: SyncResult, hash_fails: bool
) -> None:
    fake.output_exists = True
    fake.output_size = 1
    fake.sync = synced
    if hash_fails:
        fake.digest = HashResult(None, None, "read_failed: disk")
    result = await repair_media(_input_ref(), _output_ref(), _roots(), RepairRequest())
    assert result.complete is (not hash_fails)
    assert result.size_bytes == 1
    assert result.file_synced is synced.file_synced
    assert result.directory is synced.directory
    assert result.error is not None and synced.error in result.error
    if hash_fails:
        assert "hash_failed" in result.error


@pytest.mark.parametrize("tool_fails", [False, True])
async def test_output_inspection_error_preserves_unknown_and_tool_error(
    fake: FakeMedia, tool_fails: bool
) -> None:
    fake.observe_error = PermissionError("cannot inspect output")
    if tool_fails:
        fake.outcome = RawToolOutcome(LocalExit(exit_code=1), b"", None, None)
    result = await repair_media(_input_ref(), _output_ref(), _roots(), RepairRequest())
    assert result.exists is None
    assert result.complete is False
    assert result.error is not None and "inspect_failed" in result.error
    if tool_fails:
        assert "tool_failed" in result.error


async def test_non_file_output_does_not_qualify(fake: FakeMedia) -> None:
    fake.output_exists = True
    fake.output_size = 1
    fake.is_file = False
    result = await repair_media(_input_ref(), _output_ref(), _roots(), RepairRequest())
    assert result.exists is True
    assert result.complete is False
    assert result.error is not None and "output_type_mismatch" in result.error


async def test_hash_length_conflict_is_not_complete(fake: FakeMedia) -> None:
    fake.output_exists = True
    fake.output_size = 2
    result = await repair_media(_input_ref(), _output_ref(), _roots(), RepairRequest())
    assert result.size_bytes == 2
    assert result.complete is False
    assert result.error is not None and "size_mismatch" in result.error


async def test_hash_close_failure_preserves_computed_digest(fake: FakeMedia) -> None:
    fake.output_exists = True
    fake.output_size = 1
    fake.digest = HashResult("a" * 64, 1, "close_failed: disk")
    result = await repair_media(_input_ref(), _output_ref(), _roots(), RepairRequest())
    assert result.complete is False
    assert result.digest == "a" * 64
    assert result.file_synced is True
    assert result.error is not None and "close_failed" in result.error


async def test_hash_close_and_size_conflict_are_both_visible(fake: FakeMedia) -> None:
    fake.output_exists = True
    fake.output_size = 2
    fake.digest = HashResult("a" * 64, 1, "close_failed: disk")
    result = await repair_media(_input_ref(), _output_ref(), _roots(), RepairRequest())
    assert result.complete is False
    assert "close_failed" in result.error
    assert "size_mismatch" in result.error

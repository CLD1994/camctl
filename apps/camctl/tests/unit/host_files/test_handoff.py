"""F5 原子交接与撤回事实的单元测试。

移动未发生、已移动、移动未知分别表达；移动成功而目录同步失败保
留已移动事实。同名目标不覆盖，源缺失与目标竞争分别分类；撤回只
作用于 camctl 仍拥有的位置。文件系统调用经窄注入点替身。
"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

import camctl.host_files.handoff as handoff
from camctl.host_files.handoff import (
    HandoffDirectories,
    HandoffError,
    HandoffIdentity,
    PublishResult,
    PublishStage,
    ReadyName,
    WithdrawResult,
    WithdrawStage,
    publish_file,
    withdraw_file,
)
from camctl.host_files.io import DirectorySyncStage
from camctl.host_files.models import BoundDirectories, FilePurpose, FileRef

pytestmark = pytest.mark.asyncio

_ROOT = Path("D:\\state")


class FakeHandoff:
    """交接文件系统的内存替身：记录调用并按脚本注入故障。"""

    def __init__(self) -> None:
        self.files: set[Path] = set()
        self.renames: list[tuple[Path, Path]] = []
        self.links: list[tuple[Path, Path]] = []
        self.unlinks: list[Path] = []
        self.removes: list[Path] = []
        self.synced_directories: list[Path] = []
        self.fail_rename: OSError | None = None
        self.fail_link: OSError | None = None
        self.fail_unlink: OSError | None = None
        self.fail_remove: OSError | None = None
        self.fail_directory_sync: OSError | None = None

    def stat(self, path: Path):
        if path not in self.files:
            raise FileNotFoundError(errno.ENOENT, "missing")
        return path

    def rename(self, source: Path, target: Path) -> None:
        if self.fail_rename is not None:
            raise self.fail_rename
        if target in self.files:
            raise FileExistsError(errno.EEXIST, "exists")
        if source not in self.files:
            raise FileNotFoundError(errno.ENOENT, "missing")
        self.files.discard(source)
        self.files.add(target)
        self.renames.append((source, target))

    def link(self, source: Path, target: Path) -> None:
        if self.fail_link is not None:
            raise self.fail_link
        if target in self.files:
            raise FileExistsError(errno.EEXIST, "exists")
        if source not in self.files:
            raise FileNotFoundError(errno.ENOENT, "missing")
        self.files.add(target)
        self.links.append((source, target))

    def unlink(self, path: Path) -> None:
        if self.fail_unlink is not None:
            raise self.fail_unlink
        self.files.discard(path)
        self.unlinks.append(path)

    def remove(self, path: Path) -> None:
        if self.fail_remove is not None:
            raise self.fail_remove
        if path not in self.files:
            raise FileNotFoundError(errno.ENOENT, "missing")
        self.files.discard(path)
        self.removes.append(path)

    def sync_directory(self, path: Path) -> None:
        if self.fail_directory_sync is not None:
            raise self.fail_directory_sync
        self.synced_directories.append(path)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeHandoff:
    fs = FakeHandoff()
    monkeypatch.setattr(handoff, "_stat", fs.stat)
    monkeypatch.setattr(handoff, "_rename", fs.rename)
    monkeypatch.setattr(handoff, "_link", fs.link)
    monkeypatch.setattr(handoff, "_unlink", fs.unlink)
    monkeypatch.setattr(handoff, "_remove", fs.remove)
    monkeypatch.setattr(handoff, "_sync_directory", fs.sync_directory)
    monkeypatch.setattr(handoff, "_DIRECTORY_SYNC_SUPPORTED", True)
    monkeypatch.setattr(handoff, "_RENAME_WITHOUT_OVERWRITE", True)
    return fs


def _ref() -> FileRef:
    staging = _ROOT / "staging"
    return FileRef(
        file_id=7,
        purpose=FilePurpose.DELIVERY_COPY,
        relative_path="deliveries/7.bin",
        root=staging,
    )


def _directories() -> tuple[BoundDirectories, HandoffDirectories]:
    staging = _ROOT / "staging"
    return (
        BoundDirectories(staging=staging),
        HandoffDirectories(staging=staging, ready=_ROOT / "ready"),
    )


def _source_path() -> Path:
    return _ROOT / "staging" / "deliveries" / "7.bin"


def _target_path() -> Path:
    return _ROOT / "ready" / "7.bin"


async def test_move_success_sync_failure_is_visible(fake: FakeHandoff) -> None:
    """移动成功后目录同步失败：已移动事实可见，durability 未确认。"""
    fake.files.add(_source_path())
    fake.fail_directory_sync = OSError(errno.EIO, "io")
    roots, handoff_dirs = _directories()
    result = await publish_file(_ref(), roots, handoff_dirs, ReadyName("7.bin"))
    assert isinstance(result, PublishResult)
    assert result.stage is PublishStage.MOVED
    assert result.directory is DirectorySyncStage.FAILED
    assert result.error is not None
    assert result.source_removed is True


async def test_successful_publish_syncs_both_directories(fake: FakeHandoff) -> None:
    fake.files.add(_source_path())
    roots, handoff_dirs = _directories()
    result = await publish_file(_ref(), roots, handoff_dirs, ReadyName("7.bin"))
    assert result.stage is PublishStage.MOVED
    assert result.directory is DirectorySyncStage.SYNCED
    assert result.error is None
    assert result.source_removed is True
    assert fake.synced_directories == [
        _ROOT / "staging" / "deliveries",
        _ROOT / "ready",
    ]


async def test_existing_target_is_not_overwritten(fake: FakeHandoff) -> None:
    """同名普通文件不覆盖：目标存在时移动不发生，源保留。"""
    fake.files.add(_source_path())
    fake.files.add(_target_path())
    roots, handoff_dirs = _directories()
    result = await publish_file(_ref(), roots, handoff_dirs, ReadyName("7.bin"))
    assert result.stage is PublishStage.NOT_MOVED
    assert result.error is not None
    assert "target_exists" in result.error
    assert _source_path() in fake.files
    assert fake.renames == []


async def test_missing_source_is_not_target_conflict(fake: FakeHandoff) -> None:
    fake.files.add(_target_path())
    roots, handoff_dirs = _directories()
    result = await publish_file(_ref(), roots, handoff_dirs, ReadyName("7.bin"))
    assert result.stage is PublishStage.NOT_MOVED
    assert "source_missing" in (result.error or "")


async def test_move_race_target_appears(
    fake: FakeHandoff, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目标预检通过后同名出现：移动原语拒绝覆盖，移动未发生。"""
    fake.files.add(_source_path())
    roots, handoff_dirs = _directories()
    original_stat = fake.stat

    def stat_with_late_target(path: Path):
        if path == _target_path():
            # 预检时目标尚不存在，真正移动时已出现。
            fake.files.add(_target_path())
            raise FileNotFoundError(errno.ENOENT, "missing")
        return original_stat(path)

    monkeypatch.setattr(handoff, "_stat", stat_with_late_target)
    result = await publish_file(_ref(), roots, handoff_dirs, ReadyName("7.bin"))
    assert result.stage is PublishStage.NOT_MOVED
    assert "target_exists" in (result.error or "")
    assert _source_path() in fake.files


async def test_interrupted_move_is_unknown(fake: FakeHandoff) -> None:
    fake.files.add(_source_path())
    fake.fail_rename = InterruptedError(errno.EINTR, "interrupted")
    roots, handoff_dirs = _directories()
    result = await publish_file(_ref(), roots, handoff_dirs, ReadyName("7.bin"))
    assert result.stage is PublishStage.UNKNOWN
    assert result.error is not None


async def test_posix_link_path_keeps_source_removal_fact(
    fake: FakeHandoff, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSIX 分支：链接建立后源删除失败仍保留已移动事实。"""
    monkeypatch.setattr(handoff, "_RENAME_WITHOUT_OVERWRITE", False)
    fake.files.add(_source_path())
    fake.fail_unlink = OSError(errno.EACCES, "denied")
    roots, handoff_dirs = _directories()
    result = await publish_file(_ref(), roots, handoff_dirs, ReadyName("7.bin"))
    assert result.stage is PublishStage.MOVED
    assert result.source_removed is False
    assert result.error is not None
    assert "source_remove_failed" in result.error


async def test_posix_link_target_exists_is_not_moved(
    fake: FakeHandoff, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handoff, "_RENAME_WITHOUT_OVERWRITE", False)
    fake.files.add(_source_path())
    fake.files.add(_target_path())
    roots, handoff_dirs = _directories()
    result = await publish_file(_ref(), roots, handoff_dirs, ReadyName("7.bin"))
    assert result.stage is PublishStage.NOT_MOVED
    assert "target_exists" in (result.error or "")


async def test_invalid_ready_name_is_rejected(fake: FakeHandoff) -> None:
    with pytest.raises(HandoffError):
        ReadyName("../escape.bin")
    with pytest.raises(HandoffError):
        ReadyName("sub/7.bin")
    with pytest.raises(HandoffError):
        ReadyName("")


async def test_withdraw_removes_ready_file_only(fake: FakeHandoff) -> None:
    """撤回只作用于 camctl 仍拥有的 ready 位置，不触碰 processing。"""
    ready_dir = _ROOT / "ready"
    processing_dir = _ROOT / "processing"
    fake.files.add(ready_dir / "7.bin")
    fake.files.add(processing_dir / "7.bin")
    result = await withdraw_file(HandoffIdentity(ready_dir=ready_dir, name="7.bin"))
    assert isinstance(result, WithdrawResult)
    assert result.stage is WithdrawStage.WITHDRAWN
    assert (ready_dir / "7.bin") not in fake.files
    assert (processing_dir / "7.bin") in fake.files
    assert fake.removes == [ready_dir / "7.bin"]


async def test_withdraw_missing_file_is_not_present(fake: FakeHandoff) -> None:
    result = await withdraw_file(
        HandoffIdentity(ready_dir=_ROOT / "ready", name="7.bin")
    )
    assert result.stage is WithdrawStage.NOT_PRESENT


async def test_withdraw_failure_and_unknown(fake: FakeHandoff) -> None:
    identity = HandoffIdentity(ready_dir=_ROOT / "ready", name="7.bin")
    fake.files.add(identity.ready_dir / "7.bin")
    fake.fail_remove = OSError(errno.EIO, "io")
    failed = await withdraw_file(identity)
    assert failed.stage is WithdrawStage.FAILED
    assert failed.error is not None

    fake.fail_remove = InterruptedError(errno.EINTR, "interrupted")
    unknown = await withdraw_file(identity)
    assert unknown.stage is WithdrawStage.UNKNOWN

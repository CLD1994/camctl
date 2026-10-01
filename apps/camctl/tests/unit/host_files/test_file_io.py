"""F4 创建、截断、同步与摘要的单元测试。

实际阶段可恢复：截断失败阻断续传，文件与目录同步分阶段表达，
摘要按流式 SHA-256 计算，读取错误不当空文件摘要。文件系统调用
经窄注入点替身，不访问真实文件。
"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

import camctl.host_files.io as file_io
from camctl.host_files.io import (
    DirectorySyncStage,
    FileIoError,
    FileMutationResult,
    HashResult,
    SyncResult,
    prepare_target,
    sync_target,
)
from camctl.host_files.models import BoundDirectories, FilePurpose, FileRef
from camctl.host_files.paths import PathRuleError

_ROOT = Path("D:\\state\\staging")


class FakeFilesystem:
    """窄注入点的内存替身：记录阶段并按脚本注入故障。"""

    def __init__(self, *, initial: dict[str, int] | None = None) -> None:
        self.sizes: dict[str, int] = dict(initial or {})
        self.next_fd = 10
        self.truncate_calls: list[tuple[str, int]] = []
        self.fsync_calls: list[int] = []
        self.directory_syncs: list[str] = []
        self.fail_truncate: OSError | None = None
        self.fail_open_write: OSError | None = None
        self.fail_open_read: OSError | None = None
        self.fail_fsync: OSError | None = None
        self.fail_directory_sync: OSError | None = None
        self.fail_read: OSError | None = None

    def open_for_write(self, path) -> int:
        if self.fail_open_write is not None:
            raise self.fail_open_write
        key = str(path)
        if key not in self.sizes:
            self.sizes[key] = 0
        fd = self.next_fd
        self.next_fd += 1
        return fd

    def open_for_read(self, path) -> int:
        if self.fail_open_read is not None:
            raise self.fail_open_read
        fd = self.next_fd
        self.next_fd += 1
        return fd

    def open_for_sync(self, path) -> int:
        if self.fail_open_read is not None:
            raise self.fail_open_read
        fd = self.next_fd
        self.next_fd += 1
        return fd

    def ftruncate(self, fd: int, length: int) -> None:
        if self.fail_truncate is not None:
            raise self.fail_truncate
        self.truncate_calls.append((fd, length))

    def fsync(self, fd: int) -> None:
        if self.fail_fsync is not None:
            raise self.fail_fsync
        self.fsync_calls.append(fd)

    def sync_directory(self, path) -> None:
        if self.fail_directory_sync is not None:
            raise self.fail_directory_sync
        self.directory_syncs.append(str(path))

    def close(self, fd: int) -> None:
        pass

    def read(self, fd: int, size: int) -> bytes:
        if self.fail_read is not None:
            raise self.fail_read
        return b""


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeFilesystem:
    fs = FakeFilesystem()
    monkeypatch.setattr(file_io, "_open_for_write", fs.open_for_write)
    monkeypatch.setattr(file_io, "_open_for_read", fs.open_for_read)
    monkeypatch.setattr(file_io, "_open_for_sync", fs.open_for_sync)
    monkeypatch.setattr(file_io, "_ftruncate", fs.ftruncate)
    monkeypatch.setattr(file_io, "_fsync", fs.fsync)
    monkeypatch.setattr(file_io, "_sync_directory", fs.sync_directory)
    monkeypatch.setattr(file_io, "_close", fs.close)
    monkeypatch.setattr(file_io, "_read", fs.read)
    return fs


def _ref() -> FileRef:
    return FileRef(
        file_id=7,
        purpose=FilePurpose.DELIVERY_COPY,
        relative_path="deliveries/7.bin",
        root=_ROOT,
    )


def _roots() -> BoundDirectories:
    return BoundDirectories(staging=_ROOT)


def test_truncate_failure_blocks_continue(fake: FakeFilesystem) -> None:
    fake.fail_truncate = OSError(errno.ENOSPC, "no space")
    result = prepare_target(_ref(), _roots(), 1024)
    assert isinstance(result, FileMutationResult)
    assert result.truncated is False
    assert result.can_continue is False
    assert result.error is not None
    assert result.error.startswith("truncate_failed")


def test_open_failure_reports_stage(fake: FakeFilesystem) -> None:
    fake.fail_open_write = OSError(errno.EACCES, "denied")
    result = prepare_target(_ref(), _roots(), 10)
    assert result.created is False
    assert result.truncated is False
    assert result.can_continue is False
    assert result.error is not None and "open" in result.error


def test_prepare_reports_create_and_truncate_stages(fake: FakeFilesystem) -> None:
    result = prepare_target(_ref(), _roots(), 4096)
    assert result.created is True
    assert result.truncated is True
    assert result.can_continue is True
    assert result.error is None
    assert len(fake.truncate_calls) == 1
    assert fake.truncate_calls[0][1] == 4096


def test_file_synced_but_directory_failed_is_staged(
    fake: FakeFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(file_io, "_DIRECTORY_SYNC_SUPPORTED", True)
    fake.fail_directory_sync = OSError(errno.EIO, "io")
    result = sync_target(_ref(), _roots())
    assert isinstance(result, SyncResult)
    assert result.file_synced is True
    assert result.directory is DirectorySyncStage.FAILED
    assert result.error is not None


def test_file_sync_failure_skips_directory(fake: FakeFilesystem) -> None:
    fake.fail_fsync = OSError(errno.EIO, "io")
    result = sync_target(_ref(), _roots())
    assert result.file_synced is False
    assert result.directory is DirectorySyncStage.NOT_ATTEMPTED
    assert result.error is not None
    assert fake.directory_syncs == []


def test_directory_sync_unsupported_platform_is_explicit(
    fake: FakeFilesystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(file_io, "_DIRECTORY_SYNC_SUPPORTED", False)
    result = sync_target(_ref(), _roots())
    assert result.file_synced is True
    assert result.directory is DirectorySyncStage.UNSUPPORTED
    assert result.error is None
    assert fake.directory_syncs == []


def test_directory_sync_success(fake: FakeFilesystem, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(file_io, "_DIRECTORY_SYNC_SUPPORTED", True)
    result = sync_target(_ref(), _roots())
    assert result.file_synced is True
    assert result.directory is DirectorySyncStage.SYNCED
    assert result.error is None


def test_read_error_is_not_empty_file_hash(fake: FakeFilesystem) -> None:
    fake.fail_read = OSError(errno.EIO, "io")
    result = file_io.hash_target(_ref(), _roots())
    assert isinstance(result, HashResult)
    assert result.digest is None
    assert result.size_bytes is None
    assert result.error is not None


def test_hash_open_failure(fake: FakeFilesystem) -> None:
    fake.fail_open_read = OSError(errno.ENOENT, "missing")
    result = file_io.hash_target(_ref(), _roots())
    assert result.digest is None
    assert result.error is not None
    assert result.error.startswith("open_failed")


def test_negative_length_is_rejected(fake: FakeFilesystem) -> None:
    with pytest.raises(FileIoError):
        prepare_target(_ref(), _roots(), -1)


def test_path_rule_violation_propagates(fake: FakeFilesystem) -> None:
    bad = FileRef(
        file_id=7,
        purpose=FilePurpose.DELIVERY_COPY,
        relative_path="deliveries/8.bin",
        root=_ROOT,
    )
    with pytest.raises(PathRuleError):
        prepare_target(bad, _roots(), 10)

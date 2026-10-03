"""F4 文件创建截断同步摘要与真实文件的组合集成测试。

真实文件验证截断尾部、完整摘要与同步阶段；系统调用故障经窄注
入点提供确定错误；异步包装经 F2 文件任务在默认线程池执行。
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import sys
from pathlib import Path

import pytest

import camctl.host_files.io as file_io
from camctl.host_files.io import (
    DirectorySyncStage,
    prepare_target,
    sync_target,
    hash_target,
)
from camctl.host_files.models import BoundDirectories, FilePurpose, FileRef
from camctl.host_files.tasks import FileTask, FileTaskExecutor, FileTaskId
from camctl.session.supervision import Supervisor

pytestmark = pytest.mark.asyncio

_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _setup(tmp_path: Path, file_id: int = 7) -> tuple[FileRef, BoundDirectories, Path]:
    staging = tmp_path / "staging"
    (staging / "deliveries").mkdir(parents=True, exist_ok=True)
    roots = BoundDirectories(staging=staging)
    ref = FileRef(
        file_id=file_id,
        purpose=FilePurpose.DELIVERY_COPY,
        relative_path=f"deliveries/{file_id}.bin",
        root=staging,
    )
    return ref, roots, staging / "deliveries" / f"{file_id}.bin"


async def test_real_truncate_tail_and_create_stages(tmp_path: Path) -> None:
    ref, roots, path = _setup(tmp_path)
    path.write_bytes(b"x" * 100)
    result = prepare_target(ref, roots, 50)
    assert result.created is False
    assert result.truncated is True
    assert result.can_continue is True
    assert path.read_bytes() == b"x" * 50

    fresh_ref, fresh_roots, fresh_path = _setup(tmp_path, file_id=8)
    created = prepare_target(fresh_ref, fresh_roots, 0)
    assert created.created is True
    assert created.truncated is True
    assert fresh_path.read_bytes() == b""


async def test_full_digest_matches_hashlib(tmp_path: Path) -> None:
    ref, roots, path = _setup(tmp_path)
    data = bytes(index % 251 for index in range(8192))
    path.write_bytes(data)
    result = hash_target(ref, roots)
    assert result.error is None
    assert result.digest == hashlib.sha256(data).hexdigest()
    assert result.size_bytes == len(data)

    empty_ref, empty_roots, empty_path = _setup(tmp_path, file_id=9)
    empty_path.write_bytes(b"")
    empty = hash_target(empty_ref, empty_roots)
    assert empty.error is None
    assert empty.digest == _EMPTY_SHA256
    assert empty.size_bytes == 0


async def test_real_sync_and_injected_call_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref, roots, path = _setup(tmp_path)
    path.write_bytes(b"data")

    real = sync_target(ref, roots)
    assert real.file_synced is True
    assert real.error is None
    if sys.platform == "win32":
        assert real.directory is DirectorySyncStage.UNSUPPORTED
    else:
        assert real.directory is DirectorySyncStage.SYNCED

    def fail_fsync(fd: int) -> None:
        raise OSError(errno.EIO, "io")

    monkeypatch.setattr(file_io, "_fsync", fail_fsync)
    failed = sync_target(ref, roots)
    assert failed.file_synced is False
    assert failed.directory is DirectorySyncStage.NOT_ATTEMPTED
    assert failed.error is not None

    monkeypatch.setattr(file_io, "_fsync", os.fsync)
    monkeypatch.setattr(file_io, "_DIRECTORY_SYNC_SUPPORTED", True)

    def fail_directory(directory_path: Path) -> None:
        raise OSError(errno.EIO, "io")

    monkeypatch.setattr(file_io, "_sync_directory", fail_directory)
    staged = sync_target(ref, roots)
    assert staged.file_synced is True
    assert staged.directory is DirectorySyncStage.FAILED
    assert staged.error is not None

    def fail_truncate(fd: int, length: int) -> None:
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr(file_io, "_ftruncate", fail_truncate)
    blocked = prepare_target(ref, roots, 4096)
    assert blocked.truncated is False
    assert blocked.can_continue is False


async def test_async_wrapper_uses_file_tasks(tmp_path: Path) -> None:
    ref, roots, path = _setup(tmp_path)
    data = os.urandom(4096)
    executor = FileTaskExecutor(Supervisor())

    class Owner:
        async def take_over(self, task) -> None:
            raise AssertionError("正常完成不需要接手")

    def write_body(stop) -> str:
        with open(path, "wb") as writer:
            writer.write(data)
        prepared = prepare_target(ref, roots, len(data))
        assert prepared.can_continue is True
        synced = sync_target(ref, roots)
        assert synced.file_synced is True
        return "written"

    def hash_body(stop):
        result = hash_target(ref, roots)
        return result.digest

    first = await executor.run_file_task(
        FileTask(FileTaskId("prepare-7"), 7, "prepare", "copy", write_body), Owner()
    )
    assert first.ran is True and first.value == "written"
    second = await executor.run_file_task(
        FileTask(FileTaskId("hash-7"), 7, "hash", "copy", hash_body), Owner()
    )
    assert second.ran is True
    assert second.value == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("operation", ["prepare", "hash", "sync"])
async def test_close_error_keeps_real_file_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    ref, roots, path = _setup(tmp_path)
    path.write_bytes(b"abcdef")

    def close_then_report_error(fd: int) -> None:
        os.close(fd)
        raise OSError(errno.EIO, "close device")

    monkeypatch.setattr(file_io, "_close", close_then_report_error)
    if operation == "prepare":
        result = prepare_target(ref, roots, 3)
        assert result.created is False
        assert result.truncated is True
        assert result.can_continue is False
        assert path.read_bytes() == b"abc"
    elif operation == "hash":
        result = hash_target(ref, roots)
        assert result.digest == hashlib.sha256(b"abcdef").hexdigest()
        assert result.size_bytes == 6
    else:
        result = sync_target(ref, roots)
        assert result.file_synced is True
        assert result.directory is DirectorySyncStage.NOT_ATTEMPTED
    assert result.error is not None and "close_failed" in result.error


@pytest.mark.skipif(sys.platform == "win32", reason="目录同步需要 POSIX 文件描述符")
async def test_directory_close_error_keeps_completed_directory_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref, roots, path = _setup(tmp_path)
    path.write_bytes(b"data")
    real_close = os.close

    def close_directory_with_error(fd: int) -> None:
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        real_close(fd)
        if is_directory:
            raise OSError(errno.EIO, "close directory")

    monkeypatch.setattr(file_io.os, "close", close_directory_with_error)
    result = sync_target(ref, roots)
    assert result.file_synced is True
    assert result.directory is DirectorySyncStage.SYNCED
    assert result.error is not None and "directory_close_failed" in result.error

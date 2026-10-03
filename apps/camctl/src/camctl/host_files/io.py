"""创建、截断、同步与摘要的实际文件阶段。

目标文件按保存身份定位；创建与截断的实际阶段、文件与目录同步、
流式 SHA-256 摘要分别表达，不把读取错误当空文件摘要。本模块不
重置数据库进度、预算或自动重拷；调用方已给恢复或重拷资格。
"""

from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from camctl.host_files.models import BoundDirectories, FileRef
from camctl.host_files.paths import resolve_file
from camctl.host_files.segments import DEFAULT_CHUNK_SIZE_BYTES

__all__ = [
    "DirectorySyncStage",
    "FileIoError",
    "FileMutationResult",
    "HashResult",
    "SyncResult",
    "hash_target",
    "prepare_target",
    "sync_target",
]

#: Windows 不提供目录同步句柄；目录一致性由文件系统自身保证，
#: 与真实同步失败分开表达。
_DIRECTORY_SYNC_SUPPORTED = sys.platform != "win32"

#: 二进制内容必须显式按二进制打开（Windows 默认文本模式会改写字节）。
_BINARY = getattr(os, "O_BINARY", 0)


class FileIoError(ValueError):
    """文件修改参数错误。"""


class DirectorySyncStage(Enum):
    """目录同步的实际阶段。"""

    SYNCED = "synced"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"
    NOT_ATTEMPTED = "not_attempted"


@dataclass(frozen=True)
class FileMutationResult:
    """一次创建与截断的实际阶段。

    created、truncated 保留实际效果；只有整个准备阶段无错误才允
    许继续。关闭失败不撤销已完成截断，但阻止后续写入。
    """

    created: bool
    truncated: bool
    can_continue: bool
    error: str | None


@dataclass(frozen=True)
class SyncResult:
    """一次同步的实际阶段：文件与涉及目录分别表达。"""

    file_synced: bool
    directory: DirectorySyncStage
    error: str | None


@dataclass(frozen=True)
class HashResult:
    """一次摘要计算结果；未读完则摘要未知，读完后的关闭错误独立保留。"""

    digest: str | None
    size_bytes: int | None
    error: str | None


# 窄注入点：系统调用故障的确定注入位置；仅测试替换。
def _open_for_write(path: Path) -> int:
    return os.open(path, os.O_WRONLY | os.O_CREAT | _BINARY, 0o600)


def _open_for_read(path: Path) -> int:
    return os.open(path, os.O_RDONLY | _BINARY)


def _open_for_sync(path: Path) -> int:
    # Windows 的 fsync 拒绝只读句柄；同步语义是刷写，用写句柄打开，
    # 不带 O_CREAT：缺失是错误，不隐式创建。
    return os.open(path, os.O_WRONLY | _BINARY)


def _ftruncate(fd: int, length: int) -> None:
    os.ftruncate(fd, length)


def _fsync(fd: int) -> None:
    os.fsync(fd)


def _close(fd: int) -> None:
    os.close(fd)


def _read(fd: int, size: int) -> bytes:
    return os.read(fd, size)


def _sync_directory(path: Path) -> tuple[DirectorySyncStage, str | None]:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    stage = DirectorySyncStage.FAILED
    error = None
    try:
        os.fsync(fd)
    except OSError as failure:
        error = _describe("directory_sync_failed", failure)
    else:
        stage = DirectorySyncStage.SYNCED
    finally:
        try:
            os.close(fd)
        except OSError as failure:
            error = _combine_errors(error, _describe("directory_close_failed", failure))
    return stage, error


def _exists(path: Path) -> bool:
    return os.path.exists(path)


def prepare_target(
    ref: FileRef, roots: BoundDirectories, desired_length: int
) -> FileMutationResult:
    """创建或截断目标文件到期望长度。

    打开或截断失败保留实际阶段并阻断继续；成功即目标长度可靠建
    立，内容是否已写与本函数无关。
    """
    host = resolve_file(ref, roots)
    if not isinstance(desired_length, int) or desired_length < 0:
        raise FileIoError(f"期望长度必须是非负整数: {desired_length!r}")
    try:
        existed = _exists(host.path)
        fd = _open_for_write(host.path)
    except OSError as failure:
        return FileMutationResult(
            created=False,
            truncated=False,
            can_continue=False,
            error=_describe("open_failed", failure),
        )
    truncated = False
    error = None
    try:
        try:
            _ftruncate(fd, desired_length)
        except OSError as failure:
            error = _describe("truncate_failed", failure)
        else:
            truncated = True
    finally:
        close_error = _close_result(fd)
    error = _combine_errors(error, close_error)
    return FileMutationResult(
        created=not existed, truncated=truncated, can_continue=error is None, error=error
    )


def sync_target(ref: FileRef, roots: BoundDirectories) -> SyncResult:
    """同步目标文件及涉及目录；目录阶段只在文件同步成功后尝试。"""
    host = resolve_file(ref, roots)
    try:
        fd = _open_for_sync(host.path)
    except OSError as failure:
        return SyncResult(
            file_synced=False,
            directory=DirectorySyncStage.NOT_ATTEMPTED,
            error=_describe("open_failed", failure),
        )
    file_synced = False
    error = None
    try:
        _fsync(fd)
    except OSError as failure:
        error = _describe("fsync_failed", failure)
    else:
        file_synced = True
    finally:
        close_error = _close_result(fd)
    error = _combine_errors(error, close_error)
    if error is not None:
        return SyncResult(
            file_synced=file_synced,
            directory=DirectorySyncStage.NOT_ATTEMPTED,
            error=error,
        )
    if not _DIRECTORY_SYNC_SUPPORTED:
        return SyncResult(
            file_synced=True,
            directory=DirectorySyncStage.UNSUPPORTED,
            error=None,
        )
    try:
        directory, error = _sync_directory(host.path.parent)
    except OSError as failure:
        return SyncResult(
            file_synced=True,
            directory=DirectorySyncStage.FAILED,
            error=_describe("directory_sync_failed", failure),
        )
    return SyncResult(
        file_synced=True, directory=directory, error=error
    )


def hash_target(ref: FileRef, roots: BoundDirectories) -> HashResult:
    """流式计算目标文件 SHA-256 与长度；读取错误不当作空文件摘要。"""
    host = resolve_file(ref, roots)
    try:
        fd = _open_for_read(host.path)
    except OSError as failure:
        return HashResult(
            digest=None, size_bytes=None, error=_describe("open_failed", failure)
        )
    hasher = hashlib.sha256()
    size = 0
    error = None
    try:
        while True:
            try:
                block = _read(fd, DEFAULT_CHUNK_SIZE_BYTES)
            except OSError as failure:
                error = _describe("read_failed", failure)
                break
            if not block:
                break
            hasher.update(block)
            size += len(block)
    finally:
        close_error = _close_result(fd)
    return HashResult(
        digest=hasher.hexdigest() if error is None else None,
        size_bytes=size if error is None else None,
        error=_combine_errors(error, close_error),
    )


def _close_result(fd: int) -> str | None:
    """关闭只尝试一次；错误作为实际结果返回，不覆盖先前阶段。"""
    try:
        _close(fd)
    except OSError as failure:
        return _describe("close_failed", failure)
    return None


def _combine_errors(*errors: str | None) -> str | None:
    return "; ".join(error for error in errors if error is not None) or None


def _describe(kind: str, failure: Exception) -> str:
    return f"{kind}: {type(failure).__name__}: {failure}"

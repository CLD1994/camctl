"""原子文件交接与撤回事实。

staging 内完整准备的文件原子发布到 ready：同名普通文件不覆盖，
移动未发生、已移动、移动未知分别表达；目录同步在移动后尝试，其
失败不撤销已移动事实。撤回只作用于 camctl 仍拥有的 ready 位置，
主程序领取后的对象不可修改。本模块不依赖业务数据库。
"""

from __future__ import annotations

import asyncio
import errno
import os
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from camctl.host_files import io as _io
from camctl.host_files.models import BoundDirectories, FileRef
from camctl.host_files.paths import resolve_file

__all__ = [
    "HandoffDirectories",
    "HandoffError",
    "HandoffIdentity",
    "PublishResult",
    "PublishStage",
    "ReadyName",
    "WithdrawResult",
    "WithdrawStage",
    "publish_file",
    "withdraw_file",
]

#: Windows 的 os.rename 原子且不覆盖已存在目标；POSIX 的 rename 会
#: 覆盖，改用链接加删除保证不覆盖。
_RENAME_WITHOUT_OVERWRITE = sys.platform == "win32"

#: Windows 不提供目录同步句柄；目录一致性由文件系统自身保证。
_DIRECTORY_SYNC_SUPPORTED = sys.platform != "win32"


class HandoffError(ValueError):
    """交接身份或参数错误。"""


class PublishStage(Enum):
    """一次发布的移动结果。"""

    NOT_MOVED = "not_moved"
    MOVED = "moved"
    UNKNOWN = "unknown"


class WithdrawStage(Enum):
    """一次撤回的实际结果。"""

    WITHDRAWN = "withdrawn"
    NOT_PRESENT = "not_present"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ReadyName:
    """ready 目录下的文件名；单段名称，不含路径。"""

    name: str

    def __post_init__(self) -> None:
        if not self.name or "/" in self.name or "\\" in self.name:
            raise HandoffError(f"ready 文件名必须是单段非空名称: {self.name!r}")
        if self.name in (".", ".."):
            raise HandoffError(f"ready 文件名不能是目录引用: {self.name!r}")
        if "\x00" in self.name:
            raise HandoffError("ready 文件名包含非法字符")


@dataclass(frozen=True)
class HandoffDirectories:
    """交接涉及的绑定根目录：staging 与 ready 位于同一文件系统。"""

    staging: Path
    ready: Path


@dataclass(frozen=True)
class HandoffIdentity:
    """撤回对象的身份：ready 目录与文件名。"""

    ready_dir: Path
    name: str

    def __post_init__(self) -> None:
        # 复用 ReadyName 的名称规则。
        ReadyName(self.name)


@dataclass(frozen=True)
class PublishResult:
    """一次发布的实际阶段。

    directory 表达移动后涉及目录的同步阶段；source_removed 只在
    需要链接加删除的平台上区分源删除事实，rename 平台恒为 True。
    """

    stage: PublishStage
    directory: _io.DirectorySyncStage
    source_removed: bool
    error: str | None


@dataclass(frozen=True)
class WithdrawResult:
    """一次撤回的实际结果。"""

    stage: WithdrawStage
    error: str | None


# 窄注入点：系统调用故障的确定注入位置；仅测试替换。
def _stat(path: Path):
    return os.stat(path)


def _rename(source: Path, target: Path) -> None:
    os.rename(source, target)


def _link(source: Path, target: Path) -> None:
    os.link(source, target)


def _unlink(path: Path) -> None:
    os.unlink(path)


def _remove(path: Path) -> None:
    os.remove(path)


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


async def publish_file(
    ref: FileRef,
    roots: BoundDirectories,
    directories: HandoffDirectories,
    target: ReadyName,
) -> PublishResult:
    """把 staging 中完整准备的文件原子发布到 ready。

    先核对完整文件资格并拒绝同名目标，再执行原子移动，随后同步
    涉及目录；同步失败或主程序提前领取不撤销已移动事实。
    """
    return await asyncio.to_thread(
        _publish, ref, roots, directories, target
    )


async def withdraw_file(identity: HandoffIdentity) -> WithdrawResult:
    """撤回 ready 中 camctl 仍拥有的文件；已领取对象不修改。"""
    return await asyncio.to_thread(_withdraw, identity)


def _publish(
    ref: FileRef,
    roots: BoundDirectories,
    directories: HandoffDirectories,
    target: ReadyName,
) -> PublishResult:
    host = resolve_file(ref, roots)
    if directories.staging != roots.staging:
        raise HandoffError(
            f"交接目录与绑定不一致: {directories.staging!s} != {roots.staging!s}"
        )
    target_path = directories.ready / target.name
    try:
        _stat(host.path)
    except FileNotFoundError:
        return _not_moved("source_missing")
    except OSError as failure:
        return _not_moved(_describe("inspect_failed", failure))
    try:
        _stat(target_path)
    except FileNotFoundError:
        pass
    except OSError:
        # 目标可否创建由移动原语裁决，预检错误不阻断。
        pass
    else:
        return _not_moved("target_exists")

    error: str | None = None
    if _RENAME_WITHOUT_OVERWRITE:
        try:
            _rename(host.path, target_path)
        except FileExistsError:
            return _not_moved("target_exists")
        except FileNotFoundError:
            return _not_moved("source_missing")
        except InterruptedError:
            return PublishResult(
                stage=PublishStage.UNKNOWN,
                directory=_io.DirectorySyncStage.NOT_ATTEMPTED,
                source_removed=False,
                error="move_unknown: InterruptedError",
            )
        except OSError as failure:
            return _not_moved(_describe("move_failed", failure))
        source_removed = True
    else:
        try:
            _link(host.path, target_path)
        except FileExistsError:
            return _not_moved("target_exists")
        except FileNotFoundError:
            return _not_moved("source_missing")
        except OSError as failure:
            return _not_moved(_describe("move_failed", failure))
        try:
            _unlink(host.path)
        except OSError as failure:
            # 目标已建立：已移动事实保留，源删除失败单独表达。
            error = _describe("source_remove_failed", failure)
            source_removed = False
        else:
            source_removed = True

    directory, sync_error = _sync_directories(
        host.path.parent, directories.ready
    )
    if error is None:
        error = sync_error
    return PublishResult(
        stage=PublishStage.MOVED,
        directory=directory,
        source_removed=source_removed,
        error=error,
    )


def _withdraw(identity: HandoffIdentity) -> WithdrawResult:
    path = identity.ready_dir / identity.name
    try:
        _remove(path)
    except FileNotFoundError:
        return WithdrawResult(stage=WithdrawStage.NOT_PRESENT, error=None)
    except InterruptedError:
        return WithdrawResult(
            stage=WithdrawStage.UNKNOWN, error="remove_unknown: InterruptedError"
        )
    except OSError as failure:
        return WithdrawResult(
            stage=WithdrawStage.FAILED, error=_describe("remove_failed", failure)
        )
    return WithdrawResult(stage=WithdrawStage.WITHDRAWN, error=None)


def _sync_directories(
    source_directory: Path, ready_directory: Path
) -> tuple[_io.DirectorySyncStage, str | None]:
    if not _DIRECTORY_SYNC_SUPPORTED:
        return _io.DirectorySyncStage.UNSUPPORTED, None
    for directory in (source_directory, ready_directory):
        try:
            _sync_directory(directory)
        except OSError as failure:
            return _io.DirectorySyncStage.FAILED, _describe(
                "directory_sync_failed", failure
            )
    return _io.DirectorySyncStage.SYNCED, None


def _not_moved(reason: str) -> PublishResult:
    return PublishResult(
        stage=PublishStage.NOT_MOVED,
        directory=_io.DirectorySyncStage.NOT_ATTEMPTED,
        source_removed=False,
        error=reason,
    )


def _describe(kind: str, failure: Exception) -> str:
    return f"{kind}: {type(failure).__name__}: {failure}"

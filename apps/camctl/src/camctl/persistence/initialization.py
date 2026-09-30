"""显式状态库初始化与既有库验证。

初始化在目标状态库的稳定会话锁下判定实际状态：可靠不存在时经
准备文件发布完整初始库；已有完整有效的库只做验证和目录准备，
保留全部事实。发布保持"只创建尚不存在的目标"，失败不把准备
文件当作业务库。日常入口不经过本模块创建数据库。
"""

from __future__ import annotations

import enum
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from camctl.bootstrap.resources import resource_bytes
from camctl.persistence.runtime import (
    DbConfig,
    DbOpenMode,
    StateDatabaseError,
    ensure_runtime_library,
    open_existing,
    verify_directory_binding,
)
from camctl.session.locks import SessionLockConflictError, acquire_session, lock_file_paths

_SCHEMA_RESOURCES = (
    "sql/core.sql",
    "sql/workflows.sql",
    "sql/files.sql",
    "sql/operations.sql",
    "sql/reports.sql",
    "sql/history.sql",
)

#: staging 下的固定直接子目录：四种中间文件用途目录与日志副本目录。
#: 定义来源：docs/camctl/database/file-fields.md#中间文件的路径与用途目录
#: 及 docs/architecture/file-handoff.md#配置归属。
_STAGING_SUBDIRS = (
    "deliveries",
    "recording-inputs",
    "processing-temp",
    "derived",
    "logs",
)


def _canonical_binding(path: Path) -> str:
    """目录绑定的规范绝对路径字符串。

    目标部署为 Linux，直接使用绝对路径。Windows 开发环境按
    "/<盘符>/..." 约定可逆映射，使绑定仍满足结构 SQL 对 POSIX
    绝对路径的约束；同一约定用于保存与核对，切换判定不受影响。
    部署到目标机时此映射为恒等（见台账裁决）。
    """
    resolved = path.resolve()
    if os.name != "nt":
        return str(resolved)
    posix = resolved.as_posix()
    return "/" + posix[0].lower() + posix[1:]


class InitOutcome(enum.Enum):
    CREATED = "created"
    VERIFIED_EXISTING = "verified_existing"
    FAILED = "failed"


@dataclass(frozen=True)
class InitResult:
    """一次显式初始化的机器结果；诊断供 stderr 使用。"""

    outcome: InitOutcome
    detail: str
    error: BaseException | None = None


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        # Windows 句柄不支持目录 fsync；目标部署（Linux）保留目录持久化。
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _prepare_directories(state_db: Path, staging: Path, ready: Path, processing: Path) -> None:
    """准备运行目录；已有目录及内容保留，冲突对象不覆盖。"""

    def ensure_directory(target: Path, *, allow_symlink: bool = False) -> None:
        if target.exists():
            if not target.is_dir():
                raise StateDatabaseError(f"路径已被非目录对象占用: {target}")
            if not allow_symlink and target.is_symlink():
                raise StateDatabaseError(f"目录不能是符号链接: {target}")
            return
        target.mkdir(parents=True)

    ensure_directory(state_db.parent, allow_symlink=True)
    ensure_directory(staging)
    for name in _STAGING_SUBDIRS:
        ensure_directory(staging / name)
    ensure_directory(ready)
    ensure_directory(processing)


def _build_initial_database(
    target: Path, staging: str, ready: str, processing: str
) -> None:
    """在准备路径上建立完整初始库并可靠落盘。"""
    connection = sqlite3.connect(target)
    try:
        connection.execute("PRAGMA journal_mode=wal")
        connection.execute("BEGIN IMMEDIATE")
        for name in _SCHEMA_RESOURCES:
            connection.executescript(resource_bytes(name).decode("utf-8"))
        connection.execute(
            "INSERT INTO database_metadata (id, application_id, instance_id, format_version,"
            " staging_path, ready_path, processing_path) VALUES (1, 'camctl', ?, 1, ?, ?, ?)",
            (uuid.uuid4().hex, staging, ready, processing),
        )
        connection.execute(
            "INSERT INTO runtime_state (id, acknowledged_wm) VALUES (1, 0)"
        )
        connection.execute("COMMIT")
    finally:
        connection.close()
    # Windows 的 FlushFileBuffers 需要写句柄；Linux 上 O_RDWR 同样可用。
    descriptor = os.open(target, os.O_RDWR)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def initialize_state(config, state_db: Path) -> InitResult:
    """按配置显式初始化或验证目标状态库。

    全程持有目标库的稳定会话锁；锁冲突按失败报告，不无界等待。
    运行库条件、目录准备与发布顺序遵守初始化落盘契约。
    """
    try:
        ensure_runtime_library()
    except StateDatabaseError as error:
        return InitResult(InitOutcome.FAILED, f"运行库条件不满足: {error}", error)

    staging = Path(config.paths.staging).resolve()
    ready = Path(config.paths.ready).resolve()
    processing = Path(config.paths.processing).resolve()
    state_db = Path(state_db).resolve()

    try:
        # 锁文件位于状态库父目录：先准备该目录，锁内再准备其余目录。
        state_db.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return InitResult(InitOutcome.FAILED, f"状态库父目录准备失败: {error}", error)

    try:
        session_lock, _ = lock_file_paths(state_db)
        lease = acquire_session(session_lock)
    except SessionLockConflictError as error:
        return InitResult(
            InitOutcome.FAILED, f"另一进程持有目标会话锁: {error}", error
        )
    except Exception as error:  # 锁系统的其他故障不能解释为无占用。
        return InitResult(InitOutcome.FAILED, f"会话锁取得失败: {error}", error)
    try:
        binding = (_canonical_binding(staging), _canonical_binding(ready), _canonical_binding(processing))
        if state_db.exists() or state_db.is_symlink():
            # 已有目标：无论内容如何都按已有库验证；无效时保留原文件。
            result = _verify_existing(state_db, *binding)
            if result.outcome is InitOutcome.VERIFIED_EXISTING:
                try:
                    _prepare_directories(state_db, staging, ready, processing)
                except (OSError, StateDatabaseError) as error:
                    return InitResult(
                        InitOutcome.FAILED, f"目录准备失败: {error}", error
                    )
            return result
        try:
            _prepare_directories(state_db, staging, ready, processing)
        except (OSError, StateDatabaseError) as error:
            return InitResult(InitOutcome.FAILED, f"目录准备失败: {error}", error)
        return _create_new(state_db, *binding)
    finally:
        lease.close()


def _verify_existing(
    state_db: Path, staging: str, ready: str, processing: str
) -> InitResult:
    try:
        owned = open_existing(state_db, DbOpenMode.EXISTING_RW, DbConfig())
    except StateDatabaseError as error:
        return InitResult(
            InitOutcome.FAILED, f"已有状态库无效，保留原文件: {error}", error
        )
    try:
        try:
            verify_directory_binding(owned.metadata, staging, ready, processing)
        except StateDatabaseError as error:
            # 绑定不一致时按切换资格判断；资格检查不可靠时拒绝切换。
            return InitResult(
                InitOutcome.FAILED,
                f"目录绑定与配置不一致，保留原绑定: {error}",
                error,
            )
    finally:
        owned.connection.close()
    return InitResult(
        InitOutcome.VERIFIED_EXISTING,
        f"已有状态库验证通过，保留全部记录: {state_db}",
    )


def _create_new(state_db: Path, staging: str, ready: str, processing: str) -> InitResult:
    prepared = state_db.parent / f".{state_db.name}.init-{uuid.uuid4().hex}"
    try:
        _build_initial_database(prepared, staging, ready, processing)
        try:
            # 只创建尚不存在的目标：硬链接发布在目标已存在时原子失败。
            os.link(prepared, state_db)
        except FileExistsError:
            # 另一进程已发布完整库：按已有库重新验证。
            prepared.unlink()
            return _verify_existing(state_db, staging, ready, processing)
        _fsync_directory(state_db.parent)
    except (OSError, sqlite3.Error) as error:
        prepared.unlink(missing_ok=True)
        return InitResult(InitOutcome.FAILED, f"状态库建立失败: {error}", error)
    finally:
        prepared.unlink(missing_ok=True)
    return InitResult(InitOutcome.CREATED, f"已创建完整初始状态库: {state_db}")

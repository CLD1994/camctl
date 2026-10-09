"""显式状态库初始化、既有库验证与目录绑定切换。

初始化在目标状态库的稳定会话锁下判定实际状态：可靠不存在时经
准备文件发布完整初始库；已有完整有效的库只做验证和目录准备，
保留全部事实；绑定与配置不一致且切换资格全部通过时，三路径一
次事务共同保存新绑定。发布保持"只创建尚不存在的目标"，失败不
把准备文件当作业务库。日常入口不经过本模块创建数据库。
"""

from __future__ import annotations

import enum
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from camctl.history.initial_state import runtime_state_values
from camctl.resources import resource_bytes
from camctl.persistence.directory_switch import (
    SwitchCommitError,
    SwitchCommitObservation,
    canonical_to_path,
    check_atomic_move_support,
    check_distinct_roots,
    classify_switch_eligibility,
    load_switch_facts,
    scan_new_directories,
    scan_original_directories,
    switch_directory_binding,
)
from camctl.persistence.runtime import (
    DbConfig,
    DbOpenMode,
    StateDatabaseError,
    canonical_directory_binding,
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
    return canonical_directory_binding(path)


class InitOutcome(enum.Enum):
    CREATED = "created"
    VERIFIED_EXISTING = "verified_existing"
    SWITCHED = "switched"
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
        initial = runtime_state_values()
        columns = ", ".join(initial)
        parameters = ", ".join("?" for _ in initial)
        connection.execute(
            f"INSERT INTO runtime_state ({columns}) VALUES ({parameters})",
            tuple(initial.values()),
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

    # 保留目录对象本身；实际别名在 check_distinct_roots 中核对。
    staging = Path(os.path.abspath(config.paths.staging))
    ready = Path(os.path.abspath(config.paths.ready))
    processing = Path(os.path.abspath(config.paths.processing))
    state_db = Path(state_db).resolve()

    alias = check_distinct_roots((staging, ready, processing))
    if alias is not None:
        return InitResult(InitOutcome.FAILED, f"目录组合不合法: {alias}")
    filesystem = check_atomic_move_support((staging, ready, processing))
    if filesystem is not None:
        return InitResult(InitOutcome.FAILED, f"目录组合不合法: {filesystem}")

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
    roots = (staging, ready, processing)
    try:
        binding = (_canonical_binding(staging), _canonical_binding(ready), _canonical_binding(processing))
        if state_db.exists() or state_db.is_symlink():
            # 已有目标：无论内容如何都按已有库验证；无效时保留原文件。
            return _verify_existing(state_db, roots, binding)
        try:
            _prepare_directories(state_db, *roots)
        except (OSError, StateDatabaseError) as error:
            return InitResult(InitOutcome.FAILED, f"目录准备失败: {error}", error)
        return _create_new(state_db, roots, binding)
    finally:
        lease.close()


def _verify_existing(
    state_db: Path,
    roots: tuple[Path, Path, Path],
    binding: tuple[str, str, str],
) -> InitResult:
    try:
        owned = open_existing(state_db, DbOpenMode.EXISTING_RW, DbConfig())
    except StateDatabaseError as error:
        return InitResult(
            InitOutcome.FAILED, f"已有状态库无效，保留原文件: {error}", error
        )
    try:
        try:
            verify_directory_binding(owned.metadata, *binding)
        except StateDatabaseError:
            # 绑定不一致：显式 init 是第一版唯一切换入口，按切换资格判定。
            return _attempt_directory_switch(state_db, owned, roots, binding)
        try:
            _prepare_directories(state_db, *roots)
        except (OSError, StateDatabaseError) as error:
            return InitResult(InitOutcome.FAILED, f"目录准备失败: {error}", error)
    finally:
        owned.connection.close()
    return InitResult(
        InitOutcome.VERIFIED_EXISTING,
        f"已有状态库验证通过，保留全部记录: {state_db}",
    )


def _attempt_directory_switch(
    state_db: Path,
    owned,
    roots: tuple[Path, Path, Path],
    binding: tuple[str, str, str],
) -> InitResult:
    """绑定不一致时按切换规则处理；任一条件不满足保持原绑定。

    先检查数据库责任资格，再在事务外核对原三目录只剩空目录树、
    新三目录条件与部署组合，准备新目录后经一次元信息事务三路径共
    同保存；数据库身份、历史与对象 ID 保持。切换不生成业务事件，
    不复制、移动或清理任何业务文件。
    """
    old_binding = (
        owned.metadata.staging_path,
        owned.metadata.ready_path,
        owned.metadata.processing_path,
    )
    try:
        old_roots = tuple(canonical_to_path(value) for value in old_binding)
    except StateDatabaseError as error:
        return InitResult(
            InitOutcome.FAILED, f"原绑定无法定位，保留原绑定: {error}", error
        )
    try:
        blocked = classify_switch_eligibility(load_switch_facts(owned.connection))
    except StateDatabaseError as error:
        return InitResult(
            InitOutcome.FAILED, f"切换责任检查不可靠，保留原绑定: {error}", error
        )
    if blocked is not None:
        return InitResult(
            InitOutcome.FAILED,
            f"目录绑定与配置不一致，存在未结束责任，保留原绑定: {blocked}",
        )
    blocked = scan_original_directories(old_roots)
    if blocked is not None:
        return InitResult(
            InitOutcome.FAILED, f"原目录未清空，保留原绑定: {blocked}")
    blocked = scan_new_directories(roots)
    if blocked is not None:
        return InitResult(
            InitOutcome.FAILED, f"新目录条件不满足，保留原绑定: {blocked}")
    alias = check_distinct_roots(roots)
    if alias is not None:
        return InitResult(
            InitOutcome.FAILED, f"新目录组合不合法，保留原绑定: {alias}")
    filesystem = check_atomic_move_support(roots)
    if filesystem is not None:
        return InitResult(
            InitOutcome.FAILED, f"新目录组合不合法，保留原绑定: {filesystem}")
    try:
        _prepare_directories(state_db, *roots)
    except (OSError, StateDatabaseError) as error:
        return InitResult(
            InitOutcome.FAILED, f"新目录准备失败，原绑定仍有效: {error}", error)
    try:
        switch_directory_binding(owned, old_binding, binding)
    except SwitchCommitError as error:
        if error.observation is SwitchCommitObservation.NOT_COMPLETED:
            detail = f"绑定保存未完成，原绑定仍有效: {error}"
        else:
            detail = f"绑定保存结果未能可靠确认，须重新核对完整绑定: {error}"
        return InitResult(
            InitOutcome.FAILED, detail, error)
    return InitResult(InitOutcome.SWITCHED, f"已切换目录绑定: {state_db}")


def _create_new(
    state_db: Path,
    roots: tuple[Path, Path, Path],
    binding: tuple[str, str, str],
) -> InitResult:
    prepared = state_db.parent / f".{state_db.name}.init-{uuid.uuid4().hex}"
    try:
        _build_initial_database(prepared, *binding)
        try:
            # 只创建尚不存在的目标：硬链接发布在目标已存在时原子失败。
            os.link(prepared, state_db)
        except FileExistsError:
            # 另一进程已发布完整库：按已有库重新验证。
            prepared.unlink()
            return _verify_existing(state_db, roots, binding)
        _fsync_directory(state_db.parent)
    except (OSError, sqlite3.Error) as error:
        prepared.unlink(missing_ok=True)
        return InitResult(InitOutcome.FAILED, f"状态库建立失败: {error}", error)
    finally:
        prepared.unlink(missing_ok=True)
    return InitResult(InitOutcome.CREATED, f"已创建完整初始状态库: {state_db}")

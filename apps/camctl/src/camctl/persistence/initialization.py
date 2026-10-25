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
from camctl.host_files.paths import PURPOSE_DIRECTORIES
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
    CURRENT_FORMAT_VERSION,
    canonical_directory_binding,
    ensure_runtime_library,
    open_existing,
    verify_directory_binding,
    _set_and_verify,
    _SCHEMA_RESOURCES,
)
from camctl.session.locks import (
    SessionLockConflictError,
    acquire_session,
    lock_error_facts,
    lock_file_paths,
)

#: 用途子目录从用途与路径的权威映射派生；另建日志目录和拍摄工作父目录。
_STAGING_SUBDIRS = (*PURPOSE_DIRECTORIES.values(), "logs", "capture-work")


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
    secondary_errors: tuple[BaseException, ...] = ()


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
        _set_and_verify(connection, "synchronous", 2)
        journal = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        actual_journal = None if journal is None else journal[0]
        if not isinstance(actual_journal, str) or actual_journal.lower() != "wal":
            raise StateDatabaseError(
                f"journal_mode 核验失败: 期望 'wal'，实际 {actual_journal!r}"
            )
        # executescript 会先提交脚本外已有事务；将 BEGIN 放进合并后的
        # 权威 SQL，使全部结构和两条初始记录保持在同一写事务中。
        schema_sql = "\n".join(
            resource_bytes(name).decode("utf-8") for name in _SCHEMA_RESOURCES
        )
        connection.executescript(f"BEGIN IMMEDIATE;\n{schema_sql}")
        connection.execute(
            "INSERT INTO database_metadata (id, application_id, instance_id, format_version,"
            " staging_path, ready_path, processing_path) VALUES (1, 'camctl', ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, CURRENT_FORMAT_VERSION, staging, ready, processing),
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


def initialize_state(
    config, state_db: Path, *,
    upgrade_software_recovery=False, upgrade_capture_preparation=False,
) -> InitResult:
    """按配置显式初始化或验证目标状态库。

    全程持有目标库的稳定会话锁；锁冲突按失败报告，不无界等待。
    运行库条件、目录准备与发布顺序遵守初始化落盘契约。
    """
    if upgrade_software_recovery and upgrade_capture_preparation:
        return InitResult(InitOutcome.FAILED, "两个格式升级选项必须分别执行")
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
    result: InitResult | None = None
    body_error: BaseException | None = None
    try:
        result = _initialize_under_lease(state_db, roots,
            upgrade_software_recovery=upgrade_software_recovery,
            upgrade_capture_preparation=upgrade_capture_preparation)
    except BaseException as error:
        body_error = error

    release_error: BaseException | None = None
    try:
        lease.close()
    except BaseException as error:
        release_error = error

    if body_error is not None and not isinstance(body_error, Exception):
        if release_error is not None:
            _attach_initialization_exception(
                body_error, "后续会话锁释放错误", release_error,
            )
        raise body_error.with_traceback(body_error.__traceback__)

    if release_error is not None and not isinstance(release_error, Exception):
        if body_error is not None:
            _attach_initialization_exception(
                release_error, "此前初始化异常", body_error,
            )
        if result is not None:
            _attach_initialization_result(release_error, result)
        _attach_initialization_exception(
            release_error, "会话锁释放后续错误", release_error,
        )
        raise release_error.with_traceback(release_error.__traceback__)

    if body_error is not None:
        if release_error is not None:
            _attach_initialization_exception(
                body_error, "后续会话锁释放错误", release_error,
            )
        raise body_error.with_traceback(body_error.__traceback__)

    assert result is not None
    if release_error is None:
        return result

    if result.outcome is InitOutcome.FAILED:
        return InitResult(
            result.outcome,
            result.detail,
            result.error,
            (*result.secondary_errors, release_error),
        )
    return InitResult(
        InitOutcome.FAILED,
        f"{result.detail}；初始化完成后的会话锁释放失败: {release_error}",
        release_error,
        result.secondary_errors,
    )


def _initialize_under_lease(
    state_db: Path,
    roots: tuple[Path, Path, Path],
    *, upgrade_software_recovery=False, upgrade_capture_preparation=False,
) -> InitResult:
    """在已取得的会话锁下完成一次初始化或既有库验证。"""
    binding = tuple(_canonical_binding(root) for root in roots)
    if state_db.exists() or state_db.is_symlink():
        # 已有目标：无论内容如何都按已有库验证；无效时保留原文件。
        return _verify_existing(state_db, roots, binding,
            upgrade_software_recovery=upgrade_software_recovery,
            upgrade_capture_preparation=upgrade_capture_preparation)
    try:
        _prepare_directories(state_db, *roots)
    except (OSError, StateDatabaseError) as error:
        return InitResult(InitOutcome.FAILED, f"目录准备失败: {error}", error)
    return _create_new(state_db, roots, binding,
        upgrade_software_recovery=upgrade_software_recovery,
        upgrade_capture_preparation=upgrade_capture_preparation)


def _attach_initialization_note(target: BaseException, note: str) -> None:
    if note not in tuple(getattr(target, "__notes__", ())):
        target.add_note(note)


def initialization_error_descriptions(
    error: BaseException,
    *,
    include_primary: bool = True,
) -> tuple[str, ...]:
    """按原始错误顺序格式化异常组与锁端口记录的实际错误。"""
    descriptions: list[str] = []
    seen_errors: set[int] = set()
    seen_facts: set[int] = set()

    def describe_error(source: BaseException) -> str:
        facts = lock_error_facts(source)
        if facts is None:
            return f"{type(source).__name__}: {source}"
        return _describe_lock_fact(facts, source)

    def visit_fact(facts, fallback: BaseException, *, include: bool) -> None:
        if id(facts) in seen_facts:
            return
        seen_facts.add(id(facts))
        source = facts.original_error or fallback
        if include and id(source) not in seen_errors:
            seen_errors.add(id(source))
            descriptions.append(_describe_lock_fact(facts, source))
        elif not include:
            seen_errors.add(id(source))
        for secondary in facts.secondary_errors:
            visit_fact(secondary, secondary.original_error or fallback, include=True)

    def visit(source: BaseException, *, include: bool) -> None:
        if isinstance(source, BaseExceptionGroup):
            for child in source.exceptions:
                visit(child, include=include)
        facts = lock_error_facts(source)
        if facts is not None:
            visit_fact(facts, source, include=include)
            return
        if isinstance(source, BaseExceptionGroup):
            return
        if include and id(source) not in seen_errors:
            seen_errors.add(id(source))
            descriptions.append(describe_error(source))
        else:
            seen_errors.add(id(source))

    visit(error, include=include_primary)
    return tuple(descriptions)


def _describe_lock_fact(facts, source: BaseException) -> str:
    return (
        f"{facts.operation.value}: {type(source).__name__}: {source}; "
        f"path={facts.path}; errno={facts.errno}"
    )


def _initialization_error_objects(error: BaseException) -> tuple[BaseException, ...]:
    """返回错误树中的原对象，供有限地复制既有附注。"""
    objects: list[BaseException] = []
    seen: set[int] = set()

    def visit(source: BaseException) -> None:
        if id(source) in seen:
            return
        seen.add(id(source))
        objects.append(source)
        if isinstance(source, BaseExceptionGroup):
            for child in source.exceptions:
                visit(child)
        facts = lock_error_facts(source)
        if facts is None:
            return

        def visit_fact(fact) -> None:
            if fact.original_error is not None:
                visit(fact.original_error)
            for secondary in fact.secondary_errors:
                visit_fact(secondary)

        visit_fact(facts)

    visit(error)
    return tuple(objects)


def _attach_initialization_exception(
    target: BaseException,
    label: str,
    error: BaseException,
) -> None:
    same_source = target is error
    descriptions = initialization_error_descriptions(
        error,
        include_primary=not same_source,
    )
    for index, description in enumerate(descriptions):
        suffix = "" if index == 0 else "后续错误"
        _attach_initialization_note(target, f"{label}{suffix}: {description}")
    existing = set(tuple(getattr(target, "__notes__", ())))
    for source in _initialization_error_objects(error):
        if source is target:
            continue
        for note in tuple(getattr(source, "__notes__", ())):
            attached = f"{label}附注: {note}"
            if attached not in existing:
                target.add_note(attached)
                existing.add(attached)


def _attach_initialization_result(
    target: BaseException,
    result: InitResult,
) -> None:
    _attach_initialization_note(target, f"此前初始化结果: {result.detail}")
    if result.error is not None:
        _attach_initialization_exception(target, "此前初始化原错误", result.error)
    for error in result.secondary_errors:
        _attach_initialization_exception(target, "此前初始化后续错误", error)


def _verify_existing(
    state_db: Path,
    roots: tuple[Path, Path, Path],
    binding: tuple[str, str, str],
    *, upgrade_software_recovery=False, upgrade_capture_preparation=False,
) -> InitResult:
    try:
        owned = open_existing(state_db, DbOpenMode.EXISTING_RW, DbConfig())
    except StateDatabaseError as error:
        return InitResult(
            InitOutcome.FAILED, f"已有状态库无效，保留原文件: {error}", error
        )
    try:
        try:
            # 配置侧按操作系统路径比较；规范绑定文本只在库内保存。
            verify_directory_binding(owned.metadata, *(str(root) for root in roots))
        except StateDatabaseError:
            if upgrade_software_recovery or upgrade_capture_preparation:
                return InitResult(InitOutcome.FAILED, "格式升级不能与目录绑定切换合并；先按原配置升级，再单独切换目录")
            # 绑定不一致：显式 init 是第一版唯一切换入口，按切换资格判定。
            return _attempt_directory_switch(state_db, owned, roots, binding)
        try:
            _prepare_directories(state_db, *roots)
            if upgrade_software_recovery:
                from camctl.persistence.software_recovery_upgrade import upgrade_software_recovery as upgrade
                upgrade(owned, state_db)
            elif upgrade_capture_preparation:
                from camctl.persistence.capture_preparation_upgrade import upgrade_capture_preparation as upgrade
                upgrade(owned, state_db)
        except (OSError, sqlite3.Error, StateDatabaseError) as error:
            return InitResult(InitOutcome.FAILED, f"目录准备或显式升级失败，须核实原库及备份: {error}", error)
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
    *, upgrade_software_recovery=False, upgrade_capture_preparation=False,
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
            return _verify_existing(state_db, roots, binding,
                upgrade_software_recovery=upgrade_software_recovery,
                upgrade_capture_preparation=upgrade_capture_preparation)
        _fsync_directory(state_db.parent)
    except (OSError, sqlite3.Error, StateDatabaseError) as error:
        prepared.unlink(missing_ok=True)
        return InitResult(InitOutcome.FAILED, f"状态库建立失败: {error}", error)
    finally:
        prepared.unlink(missing_ok=True)
    return InitResult(InitOutcome.CREATED, f"已创建完整初始状态库: {state_db}")

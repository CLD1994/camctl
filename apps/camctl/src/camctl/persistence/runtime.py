"""既有状态库的打开与运行条件核验。

只有本模块（及初始化）创建状态库连接；日常使用 mode=rw、
报告使用 mode=ro，禁止隐式建库。连接由所属线程创建、使用和关闭。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any

from camctl.resources import resource_bytes

_RUNTIME_RESOURCE = "runtime/sqlite-runtime.json"
_SCHEMA_RESOURCES = (
    "sql/core.sql",
    "sql/workflows.sql",
    "sql/files.sql",
    "sql/operations.sql",
    "sql/reports.sql",
    "sql/history.sql",
)

#: 程序支持的数据库整体格式；第一版只支持格式 1。
SUPPORTED_FORMAT_VERSIONS = frozenset({1})

_PRAGMA_LIMIT = 2_147_483_647
_INSTANCE_ID = re.compile(r"\A[0-9a-f]{32}\Z")
_CREATE_TABLE = re.compile(r"CREATE\s+TABLE\s+\"?([A-Za-z_][A-Za-z0-9_]*)\"?", re.IGNORECASE)


class StateDatabaseError(RuntimeError):
    """状态库不可用、无效或无法可靠解释。"""


class RuntimeLibraryError(StateDatabaseError):
    """Python 实际链接的 SQLite 不满足统一运行条件。"""


class DatabaseMissingError(StateDatabaseError):
    """状态库文件不存在；日常入口不得自动创建。"""


class DatabaseInvalidError(StateDatabaseError):
    """目标不是有效的 camctl 状态库（目录、非 SQLite、身份或结构非法等）。"""


class DatabaseUnsupportedFormatError(StateDatabaseError):
    """数据库整体格式有效但本程序不支持。"""


class DirectoryBindingError(StateDatabaseError):
    """配置目录与数据库保存的绑定不一致。"""


class DbOpenMode(Enum):
    """打开既有库的权限；没有创建新库的模式。"""

    EXISTING_RW = "rw"
    EXISTING_RO = "ro"


@dataclass(frozen=True)
class DbConfig:
    """状态库连接的运行参数；合法范围由 SQLite 配置专题规定。"""

    busy_timeout_ms: int = 9000
    wal_autocheckpoint_pages: int = 1000

    def __post_init__(self) -> None:
        for name in ("busy_timeout_ms", "wal_autocheckpoint_pages"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= _PRAGMA_LIMIT
            ):
                raise StateDatabaseError(
                    f"{name} 必须是 1～{_PRAGMA_LIMIT} 的整数: {value!r}"
                )


@dataclass(frozen=True)
class DatabaseMetadata:
    """database_metadata 单份记录表达的身份、格式与目录绑定。"""

    application_id: str
    instance_id: str
    format_version: int
    staging_path: str
    ready_path: str
    processing_path: str


@dataclass(frozen=True)
class OwnedConnection:
    """由创建线程持有的状态库连接及其核验过的元信息。"""

    connection: sqlite3.Connection
    metadata: DatabaseMetadata


@lru_cache(maxsize=1)
def _runtime_conditions() -> dict[str, Any]:
    return json.loads(resource_bytes(_RUNTIME_RESOURCE))


def check_sqlite_runtime(version: Any) -> bool:
    """按权威运行条件判断 SQLite 版本是否允许。

    达到主线最低版本，或精确匹配回移修复清单，才通过；
    版本信息缺失、类型或分量非法时检查失败。
    """
    if isinstance(version, bool) or not isinstance(version, tuple):
        return False
    if len(version) != 3 or any(isinstance(part, bool) or not isinstance(part, int) for part in version):
        return False
    conditions = _runtime_conditions()
    mainline = tuple(conditions["sqlite_mainline_minimum"])
    fixed = {tuple(item) for item in conditions["sqlite_fixed_backports"]}
    return version >= mainline or version in fixed


def ensure_runtime_library() -> None:
    """核验当前解释器实际链接的 SQLite 及 Python 版本。"""
    python_minimum = tuple(_runtime_conditions()["python_minimum"])
    if sys.version_info[: len(python_minimum)] < python_minimum:
        raise RuntimeLibraryError(
            f"Python 版本低于要求 {python_minimum}: {sys.version_info[:3]}"
        )
    if not check_sqlite_runtime(sqlite3.sqlite_version_info):
        raise RuntimeLibraryError(
            f"Python 实际链接的 SQLite {sqlite3.sqlite_version} 不满足统一运行条件"
        )


@lru_cache(maxsize=1)
def expected_table_names() -> frozenset[str]:
    """从权威结构 SQL 生成应存在的表名集合，不另行维护清单。"""
    names: set[str] = set()
    for resource in _SCHEMA_RESOURCES:
        text = resource_bytes(resource).decode("utf-8")
        names.update(_CREATE_TABLE.findall(text))
    if not names:
        raise StateDatabaseError("权威结构 SQL 未定义任何表")
    return frozenset(names)


def canonical_directory_binding(path: Path) -> str:
    """按主机路径规则规范绝对绑定文本，不解析软链接目标。"""
    normalized = Path(os.path.abspath(path))
    if os.name != "nt":
        return str(normalized)
    posix = normalized.as_posix()
    return "/" + posix[0].lower() + posix[1:]


def configured_directory_binding(raw: str, field: str) -> str:
    """配置目录先证明绝对且不含 NUL，再规范绑定文本。"""
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise DirectoryBindingError(f"paths.{field} 必须是非空且不含 NUL 的绝对目录路径: {raw!r}")
    path = Path(raw)
    if not path.is_absolute():
        raise DirectoryBindingError(f"paths.{field} 必须是绝对目录路径: {raw!r}")
    return canonical_directory_binding(path)


def verify_directory_binding(
    metadata: DatabaseMetadata,
    staging: str,
    ready: str,
    processing: str,
) -> None:
    """核对本次配置目录与数据库保存的绑定。"""
    actual = (metadata.staging_path, metadata.ready_path, metadata.processing_path)
    configured = (staging, ready, processing)
    changed = [f"{name}: 数据库原路径 {old!r}，本次配置新路径 {new!r}"
               for name, old, new in zip(("staging", "ready", "processing"), actual, configured)
               if old != new]
    if changed:
        raise DirectoryBindingError(
            "配置目录与数据库绑定不一致: " + "; ".join(changed)
        )


def _read_metadata(connection: sqlite3.Connection) -> DatabaseMetadata:
    try:
        row = connection.execute(
            "SELECT application_id, instance_id, format_version, staging_path,"
            " ready_path, processing_path FROM database_metadata WHERE id = 1"
        ).fetchone()
    except sqlite3.Error as error:
        raise DatabaseInvalidError(f"database_metadata 不可读: {error}") from error
    if row is None:
        raise DatabaseInvalidError("database_metadata 单份记录缺失")
    application_id, instance_id, format_version, staging, ready, processing = row
    if application_id != "camctl":
        raise DatabaseInvalidError(f"数据库标识不是 camctl: {application_id!r}")
    if not isinstance(instance_id, str) or not _INSTANCE_ID.match(instance_id):
        raise DatabaseInvalidError(f"数据库实例身份非法: {instance_id!r}")
    if isinstance(format_version, bool) or not isinstance(format_version, int):
        raise DatabaseInvalidError(f"数据库格式版本非法: {format_version!r}")
    if format_version not in SUPPORTED_FORMAT_VERSIONS:
        raise DatabaseUnsupportedFormatError(
            f"数据库整体格式 {format_version} 不受本程序支持"
        )
    return DatabaseMetadata(
        application_id=application_id,
        instance_id=instance_id,
        format_version=format_version,
        staging_path=staging,
        ready_path=ready,
        processing_path=processing,
    )


def _verify_structure(connection: sqlite3.Connection) -> None:
    try:
        present = {
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing = expected_table_names() - present
        if missing:
            raise DatabaseInvalidError(f"状态库缺少必要结构: {sorted(missing)}")
        state = connection.execute(
            "SELECT acknowledged_wm FROM runtime_state WHERE id = 1"
        ).fetchone()
    except sqlite3.Error as error:
        raise DatabaseInvalidError(f"必要结构不可读: {error}") from error
    if state is None:
        raise DatabaseInvalidError("runtime_state 单份记录缺失")


def _set_and_verify(connection: sqlite3.Connection, pragma: str, value: int) -> None:
    connection.execute(f"PRAGMA {pragma} = {value}")
    actual = connection.execute(f"PRAGMA {pragma}").fetchone()[0]
    if actual != value:
        raise StateDatabaseError(f"{pragma} 核验失败: 期望 {value}，实际 {actual}")


def open_existing(path, mode: DbOpenMode, config: DbConfig) -> OwnedConnection:
    """打开并核验既有状态库；不创建、不修复、不隐式升级。

    只能在连接所属线程调用。目录、缺失文件、非 SQLite 内容、
    身份或结构非法分别报对应错误；格式不受支持单独表达。
    """
    ensure_runtime_library()
    if path.is_dir():
        raise DatabaseInvalidError(f"目标路径是目录: {path}")
    if not path.exists():
        raise DatabaseMissingError(f"状态库不存在: {path}")

    uri = f"{path.resolve().as_uri()}?mode={mode.value}"
    try:
        connection = sqlite3.connect(uri, uri=True, isolation_level=None)
    except sqlite3.Error as error:
        raise DatabaseInvalidError(f"状态库打开失败: {error}") from error
    try:
        journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
        if str(journal).lower() != "wal":
            raise DatabaseInvalidError(
                f"状态库不是 WAL 模式: {journal!r}；不执行隐式修复"
            )
        _set_and_verify(connection, "busy_timeout", config.busy_timeout_ms)
        if mode is DbOpenMode.EXISTING_RW:
            _set_and_verify(connection, "synchronous", 2)
            _set_and_verify(connection, "wal_autocheckpoint", config.wal_autocheckpoint_pages)
        metadata = _read_metadata(connection)
        _verify_structure(connection)
    except StateDatabaseError:
        connection.close()
        raise
    except sqlite3.Error as error:
        connection.close()
        raise DatabaseInvalidError(f"状态库校验失败: {error}") from error
    return OwnedConnection(connection=connection, metadata=metadata)

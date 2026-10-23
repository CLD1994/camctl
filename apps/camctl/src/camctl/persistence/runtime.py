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

from camctl.contracts.enums import enum_for
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

#: 整体格式编号及当前格式直接从权威登记取得。
_FORMAT_VERSIONS = enum_for("database_metadata.format_version")
SUPPORTED_FORMAT_VERSIONS = frozenset(int(member) for member in _FORMAT_VERSIONS)
CURRENT_FORMAT_VERSION = max(SUPPORTED_FORMAT_VERSIONS)
HOST_PHOTO_MINIMUM_FORMAT_VERSION = int(_FORMAT_VERSIONS.V2)

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
    """核对本次配置目录与数据库保存的绑定。

    配置侧是操作系统路径形式，先按同一规则规范化再与库内保存的
    绑定文本比较；两侧文本都由 `canonical_directory_binding` 生成。
    """
    actual = (metadata.staging_path, metadata.ready_path, metadata.processing_path)
    configured = (
        canonical_directory_binding(Path(staging)),
        canonical_directory_binding(Path(ready)),
        canonical_directory_binding(Path(processing)),
    )
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


def require_host_photo_format(format_version: int | None) -> None:
    """拒绝在尚不能表达主机拍摄文件用途的整体格式中写入该事实。"""
    if (
        isinstance(format_version, bool)
        or not isinstance(format_version, int)
        or format_version not in SUPPORTED_FORMAT_VERSIONS
        or format_version < HOST_PHOTO_MINIMUM_FORMAT_VERSION
    ):
        raise DatabaseUnsupportedFormatError(
            f"数据库整体格式 {format_version!r} 不支持主机照片存储；"
            f"至少需要格式 {HOST_PHOTO_MINIMUM_FORMAT_VERSION}"
        )


def _known_integer_check_values(sql: str | None, column: str) -> frozenset[int] | None:
    """读取指定已知列声明中的简单整数等值或 IN 检查，不解析通用 SQL。"""
    if not isinstance(sql, str):
        return None
    expression = re.compile(
        rf"\b{re.escape(column)}\s+INTEGER\s+NOT\s+NULL\s+CHECK\s*\(\s*"
        rf"{re.escape(column)}\s*(?:=\s*(?P<equal>[0-9]+)|"
        rf"IN\s*\((?P<values>[0-9,\s]+)\))\s*\)",
        re.IGNORECASE,
    )
    matches = tuple(expression.finditer(sql))
    if len(matches) != 1:
        return None
    match = matches[0]
    if match.group("equal") is not None:
        return frozenset({int(match.group("equal"))})
    raw_values = match.group("values")
    if raw_values is None:
        return None
    parts = tuple(part.strip() for part in raw_values.split(","))
    if not parts or any(not part.isdecimal() for part in parts):
        return None
    values = tuple(int(part) for part in parts)
    if len(values) != len(set(values)):
        return None
    return frozenset(values)


def _verify_format_constraints(
    connection: sqlite3.Connection, format_version: int
) -> None:
    """核对仅涉及整体格式声明的两个已知 CHECK 约束。"""
    metadata_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'database_metadata'"
    ).fetchone()
    files_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'intermediate_files'"
    ).fetchone()
    if metadata_row is None or files_row is None:
        raise DatabaseInvalidError("整体格式所需的已知表结构缺失")

    metadata_values = _known_integer_check_values(metadata_row[0], "format_version")
    purpose_values = _known_integer_check_values(files_row[0], "purpose")
    if metadata_values is None or format_version not in metadata_values:
        raise DatabaseInvalidError(
            "database_metadata.format_version 的 CHECK 不接受其已保存格式"
        )

    from camctl.host_files.paths import HOST_PHOTO_PURPOSES, PURPOSE_DIRECTORIES

    purpose_enum = enum_for("intermediate_files.purpose")
    mapped_purposes = frozenset(
        int(purpose_enum[purpose.name]) for purpose in PURPOSE_DIRECTORIES
    )
    all_purposes = frozenset(int(member) for member in purpose_enum)
    if mapped_purposes != all_purposes:
        raise DatabaseInvalidError(
            "用途目录映射没有覆盖 intermediate_files.purpose 的登记成员"
        )
    host_photo_purposes = frozenset(
        int(purpose_enum[purpose.name]) for purpose in HOST_PHOTO_PURPOSES
    )
    legacy_purposes = mapped_purposes - host_photo_purposes
    if purpose_values is None:
        raise DatabaseInvalidError("intermediate_files.purpose 的 CHECK 不可解释")

    if format_version == int(_FORMAT_VERSIONS.V1):
        allowed_purpose_checks = {legacy_purposes, mapped_purposes}
        allowed_metadata_checks = {
            frozenset({int(_FORMAT_VERSIONS.V1)}),
            SUPPORTED_FORMAT_VERSIONS,
            frozenset({1, 2}),
        }
        if purpose_values not in allowed_purpose_checks or metadata_values not in allowed_metadata_checks:
            raise DatabaseInvalidError(
                "格式 V1 的已知 CHECK 与受支持的旧版或当前结构不匹配"
            )
        return

    if format_version in (int(_FORMAT_VERSIONS.V2), CURRENT_FORMAT_VERSION):
        if (
            purpose_values != mapped_purposes
            or not metadata_values.issubset(SUPPORTED_FORMAT_VERSIONS)
        ):
            raise DatabaseInvalidError(
                "格式 V2 的已知 CHECK 未声明完整用途集合或格式版本"
            )
        return

    raise DatabaseUnsupportedFormatError(
        f"数据库整体格式 {format_version} 没有本程序的结构资格规则"
    )


def _verify_structure(connection: sqlite3.Connection, format_version: int) -> None:
    try:
        present = {
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        expected = expected_table_names()
        from camctl.history.events import business_tables_for_format, load_event_registry
        unavailable = set(load_event_registry()["tables"]) - set(business_tables_for_format(format_version))
        expected = expected - unavailable
        missing = expected - present
        if missing:
            raise DatabaseInvalidError(f"状态库缺少必要结构: {sorted(missing)}")
        _verify_format_constraints(connection, format_version)
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
        _verify_structure(connection, metadata.format_version)
    except StateDatabaseError:
        connection.close()
        raise
    except sqlite3.Error as error:
        connection.close()
        raise DatabaseInvalidError(f"状态库校验失败: {error}") from error
    return OwnedConnection(connection=connection, metadata=metadata)

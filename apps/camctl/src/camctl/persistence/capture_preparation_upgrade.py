"""显式拍摄准备格式升级：核对完整副本后共同发布结构和版本。"""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import uuid

from camctl.persistence.initialization import _fsync_directory
from camctl.persistence.runtime import (
    CURRENT_FORMAT_VERSION,
    DatabaseMetadata,
    DbConfig,
    DbOpenMode,
    OwnedConnection,
    StateDatabaseError,
    _FORMAT_VERSIONS,
    _SCHEMA_RESOURCES,
    open_existing,
)
from camctl.resources import resource_bytes

_BATCH_SIZE = 128


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _one(connection: sqlite3.Connection, sql: str):
    with closing(connection.execute(sql)) as cursor:
        return cursor.fetchone()


def _tables(connection: sqlite3.Connection) -> tuple[str, ...]:
    with closing(connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )) as cursor:
        return tuple(row[0] for row in cursor.fetchall())


def _columns(connection: sqlite3.Connection, table: str) -> tuple[tuple, ...]:
    with closing(connection.execute(f"PRAGMA table_xinfo({_quote(table)})")) as cursor:
        return tuple(cursor.fetchall())


def _integrity(connection: sqlite3.Connection) -> None:
    if _one(connection, "PRAGMA quick_check") != ("ok",):
        raise StateDatabaseError("状态库升级的 SQLite 完整性核验失败")
    if _one(connection, "PRAGMA foreign_key_check") is not None:
        raise StateDatabaseError("状态库升级的外键核验失败")


def _checked_columns(source, target, table: str) -> tuple[tuple, ...]:
    columns = _columns(source, table)
    target_columns = _columns(target, table)
    if tuple((column[1], column[6]) for column in columns) != tuple(
        (column[1], column[6]) for column in target_columns
    ):
        raise StateDatabaseError(f"升级前后的列集合或生成列不一致: {table}")
    return columns


def _row_query(table: str, columns: tuple[tuple, ...], *, writable=False) -> str:
    selected = tuple(column for column in columns if not writable or column[6] == 0)
    primary = sorted((column for column in columns if column[5]), key=lambda column: column[5])
    order = primary or columns
    return (
        f"SELECT {', '.join(_quote(column[1]) for column in selected)} FROM {_quote(table)}"
        f" ORDER BY {', '.join(_quote(column[1]) for column in order)}"
    )


def _with_format(rows, table: str, names: tuple[str, ...], target_format: int):
    if table != "database_metadata":
        return rows
    index = names.index("format_version")
    return [(*row[:index], target_format, *row[index + 1:]) for row in rows]


def _copy_tables(source: sqlite3.Connection, target: sqlite3.Connection) -> None:
    tables = _tables(source)
    if tables != _tables(target):
        raise StateDatabaseError("原状态库的全部表与权威目标结构不一致，停止升级")
    for table in tables:
        columns = _checked_columns(source, target, table)
        names = tuple(column[1] for column in columns if column[6] == 0)
        insert = (
            f"INSERT INTO {_quote(table)} ({', '.join(_quote(name) for name in names)})"
            f" VALUES ({', '.join('?' for _ in names)})"
        )
        with closing(source.execute(_row_query(table, columns, writable=True))) as cursor:
            while rows := cursor.fetchmany(_BATCH_SIZE):
                rows = _with_format(rows, table, names, CURRENT_FORMAT_VERSION)
                with closing(target.executemany(insert, rows)):
                    pass


def _verify_contents(source, target, *, target_format: int) -> None:
    tables = _tables(source)
    if tables != _tables(target):
        raise StateDatabaseError("升级副本没有保留原状态库的全部表")
    for table in tables:
        columns = _checked_columns(source, target, table)
        names = tuple(column[1] for column in columns)
        count = _one(source, f"SELECT COUNT(*) FROM {_quote(table)}")
        if count != _one(target, f"SELECT COUNT(*) FROM {_quote(table)}"):
            raise StateDatabaseError(f"升级副本的记录数量不一致: {table}")
        sql = _row_query(table, columns)
        with closing(source.execute(sql)) as before, closing(target.execute(sql)) as after:
            checked = 0
            while True:
                rows = before.fetchmany(_BATCH_SIZE)
                actual = after.fetchmany(_BATCH_SIZE)
                expected = _with_format(rows, table, names, target_format)
                if expected != actual:
                    raise StateDatabaseError(f"升级副本的原始记录内容不一致: {table}")
                if not rows:
                    break
                checked += len(rows)
            if count != (checked,):
                raise StateDatabaseError(f"升级副本的逐行核对数量不一致: {table}")


def _checkpoint(connection: sqlite3.Connection) -> None:
    if _one(connection, "PRAGMA wal_checkpoint(TRUNCATE)") != (0, 0, 0):
        raise StateDatabaseError("升级发布前未能完成 WAL 检查点")


def _require_closed_wal(path: Path) -> None:
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm")):
        raise StateDatabaseError(f"连接关闭后 WAL 辅助文件仍存在，停止发布: {path}")


def _sync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDWR)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _reserve(path: Path) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)


def _verify_copy(path, original, source, *, target_format: int) -> None:
    verified = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        if verified.metadata != replace(original, format_version=target_format):
            raise StateDatabaseError("升级副本的实例身份、目录绑定或格式不一致")
        _integrity(verified.connection)
        _verify_contents(source, verified.connection, target_format=target_format)
        _checkpoint(verified.connection)
    finally:
        verified.connection.close()
    _require_closed_wal(path)


def _backup(
    source, state_db: Path, original: DatabaseMetadata, *, upgrade_name="capture-preparation",
) -> Path:
    backup = state_db.parent / (
        f"{state_db.name}.before-{upgrade_name}-v{CURRENT_FORMAT_VERSION}"
        f"-{uuid.uuid4().hex}.sqlite"
    )
    try:
        _reserve(backup)
        target = sqlite3.connect(f"{backup.resolve().as_uri()}?mode=rw", uri=True)
        try:
            source.backup(target, pages=_BATCH_SIZE)
            if _one(target, "PRAGMA journal_mode=WAL") != ("wal",):
                raise StateDatabaseError("升级备份的 WAL 模式核验失败")
        finally:
            target.close()
        _verify_copy(backup, original, source, target_format=original.format_version)
        _sync_file(backup)
        _fsync_directory(backup.parent)
    except (OSError, sqlite3.Error, StateDatabaseError) as error:
        raise StateDatabaseError(f"一致备份未完成核验，保留备份文件 {backup}: {error}") from error
    return backup


def _prepare(source, prepared: Path, original: DatabaseMetadata) -> None:
    _reserve(prepared)
    target = sqlite3.connect(f"{prepared.resolve().as_uri()}?mode=rw", uri=True)
    commit_error = None
    try:
        with closing(target.execute("PRAGMA synchronous=FULL")):
            pass
        if _one(target, "PRAGMA synchronous") != (2,):
            raise StateDatabaseError("升级副本的同步设置核验失败")
        if _one(target, "PRAGMA journal_mode=WAL") != ("wal",):
            raise StateDatabaseError("升级副本的 WAL 模式核验失败")
        with closing(target.execute("PRAGMA foreign_keys=ON")):
            pass
        if _one(target, "PRAGMA foreign_keys") != (1,):
            raise StateDatabaseError("升级副本的外键设置核验失败")
        schema = "\n".join(resource_bytes(name).decode("utf-8") for name in _SCHEMA_RESOURCES)
        with closing(target.executescript(f"BEGIN IMMEDIATE;\n{schema}")):
            pass
        _copy_tables(source, target)
        _integrity(target)
        try:
            target.commit()
        except sqlite3.Error as error:
            commit_error = error
    finally:
        # close 先结束未确认事务；重新打开只观察结果，绝不重发复制。
        target.close()
    try:
        _verify_copy(prepared, original, source,
                     target_format=CURRENT_FORMAT_VERSION)
    except (OSError, sqlite3.Error, StateDatabaseError) as error:
        if commit_error is not None:
            raise StateDatabaseError(f"升级副本提交结果未能核实: {error}") from commit_error
        raise
    _sync_file(prepared)
    _fsync_directory(prepared.parent)


def _verify_published(state_db: Path, backup: Path, original: DatabaseMetadata) -> None:
    saved = open_existing(backup, DbOpenMode.EXISTING_RO, DbConfig())
    try:
        _verify_copy(state_db, original, saved.connection,
                     target_format=CURRENT_FORMAT_VERSION)
    finally:
        saved.connection.close()


def upgrade_capture_preparation(owned: OwnedConnection, state_db: Path) -> Path | None:
    """在初始化持有稳定会话锁时，将 V3/V4/V5 的完整事实升级到当前格式。"""
    return _upgrade_full_copy(
        owned, state_db,
        source_versions=(int(_FORMAT_VERSIONS.V3), int(_FORMAT_VERSIONS.V4), int(_FORMAT_VERSIONS.V5)),
        upgrade_name="capture-preparation", description="拍摄准备", minimum="V3",
    )


def _upgrade_full_copy(
    owned: OwnedConnection, state_db: Path, *, source_versions: tuple[int, ...],
    upgrade_name: str, description: str, minimum: str,
) -> Path | None:
    """由显式升级入口复用完整复制、内容核验和一次发布的规则。"""
    source = owned.connection
    original = owned.metadata
    target_format = CURRENT_FORMAT_VERSION
    if original.format_version == target_format:
        _integrity(source)
        return None
    if original.format_version not in source_versions:
        raise StateDatabaseError(f"{description}升级要求先完成格式 {minimum} 的已有升级路径")
    _integrity(source)
    backup = _backup(source, state_db, original, upgrade_name=upgrade_name)
    prepared = state_db.parent / f".{state_db.name}.{upgrade_name}-{uuid.uuid4().hex}"
    try:
        _prepare(source, prepared, original)
        _checkpoint(source)
        source.close()
        # 不删除原 WAL；只有真实关闭全部使用者后才能替换主文件。
        _require_closed_wal(state_db)
        verified = False
        try:
            os.replace(prepared, state_db)
        except OSError as error:
            try:
                _verify_published(state_db, backup, original)
            except (OSError, sqlite3.Error, StateDatabaseError) as verification_error:
                raise StateDatabaseError(f"升级发布结果未能核实: {verification_error}") from error
            verified = True
        _fsync_directory(state_db.parent)
        if not verified:
            _verify_published(state_db, backup, original)
    except (OSError, sqlite3.Error, StateDatabaseError) as error:
        raise StateDatabaseError(f"{description}升级未完成核验；保留原库及一致备份 {backup}: {error}") from error
    return backup

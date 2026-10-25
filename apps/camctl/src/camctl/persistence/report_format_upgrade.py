"""显式扩展报告生成格式资格，数据库整体版本和全部既有事实保持原值。"""
from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3
import uuid

from camctl.persistence.capture_preparation_upgrade import (
    _checkpoint, _integrity, _one, _reserve, _require_closed_wal, _sync_file, _verify_copy,
)
from camctl.persistence.initialization import _fsync_directory
from camctl.persistence.runtime import (
    DbConfig, DbOpenMode, OwnedConnection, StateDatabaseError, open_existing, report_format_versions,
)
from camctl.reporting.models import REPORT_FORMAT_VERSIONS


def _copy(source, path: Path, original):
    _reserve(path)
    with closing(sqlite3.connect(path)) as target:
        source.backup(target, pages=128)
    _verify_copy(path, original, source, target_format=original.format_version)
    _sync_file(path)
    _fsync_directory(path.parent)


def _expand_reports(path: Path):
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("PRAGMA synchronous=FULL")
        sql = _one(connection, "SELECT sql FROM sqlite_master WHERE type='table' AND name='reports'")[0]
        # 资格检查已限定旧版 CHECK；仅替换这一个约束，保留其余原表声明。
        values = ",".join(str(value) for value in sorted(REPORT_FORMAT_VERSIONS))
        sql, count = re.subn(r"CHECK\s*\(\s*format_version\s*=\s*1\s*\)",
                             "CHECK (format_version IN (" + values + "))", sql, count=1, flags=re.I)
        if count != 1:
            raise StateDatabaseError("原报告格式约束无法作精确升级")
        sql, count = re.subn(r"CREATE\s+TABLE\s+reports\b", "CREATE TABLE reports_upgraded", sql, count=1, flags=re.I)
        if count != 1:
            raise StateDatabaseError("原报告表声明无法作精确升级")
        objects = connection.execute("SELECT sql FROM sqlite_master WHERE tbl_name='reports' AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY name").fetchall()
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(sql)
            connection.execute("INSERT INTO reports_upgraded SELECT * FROM reports")
            connection.execute("DROP TABLE reports")
            connection.execute("ALTER TABLE reports_upgraded RENAME TO reports")
            for (definition,) in objects:
                connection.execute(definition)
            connection.commit()
        finally:
            if connection.in_transaction:
                connection.rollback()
        connection.execute("PRAGMA foreign_keys=ON")
        _integrity(connection)
        if report_format_versions(connection) != REPORT_FORMAT_VERSIONS:
            raise StateDatabaseError("升级副本未声明当前报告格式")
        _checkpoint(connection)
    _require_closed_wal(path)


def _verify_published(path, backup, original):
    saved = open_existing(backup, DbOpenMode.EXISTING_RO, DbConfig())
    try:
        _verify_copy(path, original, saved.connection, target_format=original.format_version)
    finally:
        saved.connection.close()
    verified = open_existing(path, DbOpenMode.EXISTING_RO, DbConfig())
    try:
        if report_format_versions(verified.connection) != REPORT_FORMAT_VERSIONS:
            raise StateDatabaseError("发布后的报告格式资格不正确")
    finally:
        verified.connection.close()


def upgrade_report_format(owned: OwnedConnection, state_db: Path) -> Path | None:
    """调用方须持有稳定会话锁；普通打开状态库不会调用本入口。"""
    source, original = owned.connection, owned.metadata
    _integrity(source)
    if report_format_versions(source) == REPORT_FORMAT_VERSIONS:
        return None
    identity = uuid.uuid4().hex
    backup = state_db.parent / f"{state_db.name}.before-report-format-{identity}.sqlite"
    prepared = state_db.parent / f".{state_db.name}.report-format-{identity}"
    try:
        _copy(source, backup, original)
        _copy(source, prepared, original)
        _expand_reports(prepared)
        _verify_copy(prepared, original, source, target_format=original.format_version)
        _sync_file(prepared)
        _fsync_directory(prepared.parent)
        _checkpoint(source)
        source.close()
        _require_closed_wal(state_db)
        try:
            os.replace(prepared, state_db)
        except OSError as error:
            # 替换结果未知时只核对发布结果，不能重新发送替换。
            try:
                _verify_published(state_db, backup, original)
            except (OSError, sqlite3.Error, StateDatabaseError) as verification_error:
                raise StateDatabaseError(f"报告格式升级发布结果未能核实: {verification_error}") from error
        _fsync_directory(state_db.parent)
        _verify_published(state_db, backup, original)
    except (OSError, sqlite3.Error, StateDatabaseError) as error:
        raise StateDatabaseError(f"报告格式升级未完成核验；保留原库及一致备份 {backup}: {error}") from error
    return backup

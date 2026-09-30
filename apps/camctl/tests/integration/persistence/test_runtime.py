"""P1 既有库打开与运行条件核验的组件集成测试。

使用真实 sqlite3 与临时文件；测试准备直接从包内权威 SQL
构造有效库，不调用被测打开入口产生期望。
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

from camctl.bootstrap.resources import resource_bytes
from camctl.persistence.runtime import (
    DatabaseInvalidError,
    DatabaseMissingError,
    DatabaseUnsupportedFormatError,
    DirectoryBindingError,
    DbConfig,
    DbOpenMode,
    check_sqlite_runtime,
    open_existing,
    verify_directory_binding,
)

SCHEMA_RESOURCES = [
    "sql/core.sql",
    "sql/workflows.sql",
    "sql/files.sql",
    "sql/operations.sql",
    "sql/reports.sql",
    "sql/history.sql",
]

DIRECTORIES = ("/srv/camctl/staging", "/srv/camctl/ready", "/srv/camctl/processing")


def _create_valid_database(path: Path, *, format_version: int = 1) -> None:
    """按权威 SQL 构造完整有效的状态库（测试准备，不经过被测入口）。"""
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=wal")
        connection.execute("BEGIN IMMEDIATE")
        for name in SCHEMA_RESOURCES:
            connection.executescript(resource_bytes(name).decode("utf-8"))
        connection.execute(
            "INSERT INTO database_metadata (id, application_id, instance_id, format_version,"
            " staging_path, ready_path, processing_path) VALUES (1, 'camctl', ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, format_version, *DIRECTORIES),
        )
        connection.execute(
            "INSERT INTO runtime_state (id, acknowledged_wm) VALUES (1, 0)"
        )
        connection.commit()
    finally:
        connection.close()


def test_missing_database_is_not_created(tmp_path: Path) -> None:
    target = tmp_path / "state.db"
    with pytest.raises(DatabaseMissingError):
        open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())
    assert not target.exists()
    assert not (tmp_path / "state.db-wal").exists()


def test_directory_path_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(DatabaseInvalidError):
        open_existing(tmp_path, DbOpenMode.EXISTING_RW, DbConfig())


def test_invalid_file_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "state.db"
    target.write_bytes(b"this is not a sqlite database at all")
    with pytest.raises(DatabaseInvalidError):
        open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())


def test_plain_sqlite_without_camctl_identity_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "state.db"
    connection = sqlite3.connect(target)
    connection.execute("CREATE TABLE stray (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    with pytest.raises(DatabaseInvalidError):
        open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())


def test_unsupported_format_version_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "future.db"
    connection = sqlite3.connect(target)
    connection.execute("PRAGMA journal_mode=wal")
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "CREATE TABLE database_metadata (id INTEGER PRIMARY KEY, application_id TEXT,"
        " instance_id TEXT, format_version INTEGER, staging_path TEXT, ready_path TEXT,"
        " processing_path TEXT)"
    )
    connection.execute(
        "INSERT INTO database_metadata VALUES (1, 'camctl', ?, 2, ?, ?, ?)",
        (uuid.uuid4().hex, *DIRECTORIES),
    )
    connection.commit()
    connection.close()
    with pytest.raises(DatabaseUnsupportedFormatError):
        open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())


def test_special_uri_characters_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "st rage #db% 键.db"
    _create_valid_database(target)
    owned = open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        assert owned.metadata.application_id == "camctl"
    finally:
        owned.connection.close()


def test_write_connection_pragmas_are_set_and_verified(tmp_path: Path) -> None:
    target = tmp_path / "state.db"
    _create_valid_database(target)
    config = DbConfig(busy_timeout_ms=1500, wal_autocheckpoint_pages=333)
    owned = open_existing(target, DbOpenMode.EXISTING_RW, config)
    try:
        assert owned.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert owned.connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert owned.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 1500
        assert owned.connection.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 333
        assert owned.metadata.staging_path == DIRECTORIES[0]
        assert owned.metadata.ready_path == DIRECTORIES[1]
        assert owned.metadata.processing_path == DIRECTORIES[2]
        assert len(owned.metadata.instance_id) == 32
    finally:
        owned.connection.close()


def test_readonly_connection_rejects_writes(tmp_path: Path) -> None:
    target = tmp_path / "state.db"
    _create_valid_database(target)
    owned = open_existing(target, DbOpenMode.EXISTING_RO, DbConfig(busy_timeout_ms=1200))
    try:
        assert owned.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 1200
        assert owned.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        with pytest.raises(sqlite3.Error):
            owned.connection.execute("DELETE FROM runtime_state")
    finally:
        owned.connection.close()


def test_directory_binding_verification() -> None:
    metadata_ok, metadata_off = (
        _metadata(staging=s, ready=r, processing=p)
        for s, r, p in (DIRECTORIES, ("/other/staging", *DIRECTORIES[1:]))
    )
    verify_directory_binding(metadata_ok, *DIRECTORIES)
    with pytest.raises(DirectoryBindingError):
        verify_directory_binding(metadata_off, *DIRECTORIES)


def _metadata(staging: str, ready: str, processing: str):
    from camctl.persistence.runtime import DatabaseMetadata

    return DatabaseMetadata(
        application_id="camctl",
        instance_id=uuid.uuid4().hex,
        format_version=1,
        staging_path=staging,
        ready_path=ready,
        processing_path=processing,
    )


class TestSqliteRuntimePartitions:
    """用权威运行条件文件的允许和拒绝分区核对版本判断。"""

    @pytest.mark.parametrize(
        "version",
        [(3, 51, 3), (3, 52, 0), (4, 0, 0), (3, 44, 6), (3, 50, 7)],
    )
    def test_allowed_versions(self, version: tuple[int, int, int]) -> None:
        assert check_sqlite_runtime(version) is True

    @pytest.mark.parametrize(
        "version",
        [
            (3, 51, 2),
            (3, 44, 5),
            (3, 44, 7),
            (3, 50, 6),
            (3, 50, 8),
            (3, 45, 0),
            (3, 36, 0),
        ],
    )
    def test_rejected_versions(self, version: tuple[int, int, int]) -> None:
        assert check_sqlite_runtime(version) is False

    def test_current_interpreter_library_is_allowed(self) -> None:
        assert check_sqlite_runtime(sqlite3.sqlite_version_info) is True

    def test_missing_or_malformed_version_is_rejected(self) -> None:
        assert check_sqlite_runtime(None) is False  # type: ignore[arg-type]
        assert check_sqlite_runtime((3, 51)) is False  # type: ignore[arg-type]

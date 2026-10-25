"""升级只改变整体格式；数据库、文件和发布边界均使用受约束替身。"""

from dataclasses import replace
from pathlib import Path
import re
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.persistence import capture_preparation_upgrade as upgrade
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection, StateDatabaseError


def _cursor(rows=()):
    cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
    remaining = list(rows)

    def fetchmany(size=1):
        batch = remaining[:size]
        del remaining[:size]
        return batch

    cursor.fetchmany.side_effect = fetchmany
    cursor.fetchone.side_effect = lambda: fetchmany()[0] if remaining else None
    cursor.fetchall.side_effect = lambda: fetchmany(len(remaining))
    return cursor


def _column(index, name, *, primary=0, generated=0):
    return (index, name, "INTEGER", 1, None, primary, generated)


class DatabaseRows:
    """在 sqlite3 接口上提供可控记录，不执行 SQL 或访问文件系统。"""

    def __init__(self, tables, columns):
        self.tables = {table: list(rows) for table, rows in tables.items()}
        self.columns = columns
        self.cursors = []
        self.inserts = []
        self.connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
        self.connection.execute.side_effect = self.execute
        self.connection.executemany.side_effect = self.executemany

    def cursor(self, rows=()):
        cursor = _cursor(rows)
        self.cursors.append(cursor)
        return cursor

    def execute(self, sql):
        if sql.startswith("SELECT name FROM sqlite_master"):
            return self.cursor((name,) for name in sorted(self.tables))
        if sql.startswith("PRAGMA table_xinfo"):
            return self.cursor(self.columns[re.search(r'\("(.*)"\)', sql)[1]])
        if sql.startswith("SELECT COUNT(*)"):
            return self.cursor([(len(self.tables[re.search(r'FROM "(.*?)"', sql)[1]]),)])
        if sql.startswith("SELECT "):
            table = re.search(r'FROM "(.*?)"', sql)[1]
            names = re.findall(r'"(.*?)"', sql.split(" FROM ")[0])
            indices = [next(index for index, column in enumerate(self.columns[table])
                            if column[1] == name) for name in names]
            return self.cursor(tuple(row[index] for index in indices) for row in self.tables[table])
        if sql == "PRAGMA quick_check":
            return self.cursor([("ok",)])
        if sql == "PRAGMA foreign_key_check":
            return self.cursor()
        if sql == "PRAGMA wal_checkpoint(TRUNCATE)":
            return self.cursor([(0, 0, 0)])
        raise AssertionError(f"未约定的查询: {sql}")

    def executemany(self, sql, rows):
        table = re.search(r'INSERT INTO "(.*?)"', sql)[1]
        names = re.findall(r'"(.*?)"', sql.split(" VALUES ")[0])[1:]
        rows = list(rows)
        self.inserts.append((table, names, rows))
        for row in rows:
            values = dict(zip(names, row))
            self.tables[table].append(tuple(
                values[column[1]] if not column[6] else values["id"] * 2
                for column in self.columns[table]
            ))
        return self.cursor()


def _rows(*, target=False, version=3):
    columns = {
        "database_metadata": (_column(0, "id", primary=1), _column(1, "format_version")),
        "history_events": (_column(0, "id", primary=1), _column(1, "payload"),
                           _column(2, "derived", generated=3)),
    }
    tables = {"database_metadata": [(1, version)],
              "history_events": [(index, f"记录-{index}", index * 2) for index in range(1, 261)]}
    if target:
        tables = {table: [] for table in tables}
    return DatabaseRows(tables, columns)


def _metadata(version=3):
    return DatabaseMetadata("camctl", "a" * 32, version, "/staging", "/ready", "/processing")


@pytest.mark.parametrize("version", [3, 4])
def test_copy_keeps_all_rows_and_identities_and_changes_only_metadata_format(version):
    source, target = _rows(version=version), _rows(target=True)
    upgrade._copy_tables(source.connection, target.connection)
    assert target.tables == {
        "database_metadata": [(1, 5)],
        "history_events": source.tables["history_events"],
    }
    assert all(len(rows) <= 128 for _, _, rows in target.inserts)
    assert all("derived" not in names for _, names, _ in target.inserts)
    upgrade._verify_contents(source.connection, target.connection, target_format=5)
    assert all(cursor.close.call_count == 1 for cursor in (*source.cursors, *target.cursors))


@pytest.mark.parametrize("changed", ["missing_row", "same_count_changed_history", "generated_value"])
def test_content_verification_rejects_loss_or_changes(changed):
    source, target = _rows(), _rows()
    target.tables["database_metadata"] = [(1, 5)]
    if changed == "missing_row":
        target.tables["history_events"].pop()
    elif changed == "same_count_changed_history":
        target.tables["history_events"][129] = (130, "改写历史", 260)
    else:
        target.tables["history_events"][0] = (1, "记录-1", 999)
    with pytest.raises(StateDatabaseError):
        upgrade._verify_contents(source.connection, target.connection, target_format=5)


def test_original_table_without_authoritative_target_is_not_silently_dropped():
    source, target = _rows(), _rows(target=True)
    source.tables["unmatched_table"] = [(91,)]
    with pytest.raises(StateDatabaseError):
        upgrade._copy_tables(source.connection, target.connection)
    assert target.inserts == []


@pytest.fixture
def publication(monkeypatch):
    source = _rows()
    owned = OwnedConnection(source.connection, _metadata())
    state_db, backup = Path("/state/state.db"), Path("/state/preserved.sqlite")
    events = []
    backup_fn = create_autospec(upgrade._backup, return_value=backup)
    prepare = create_autospec(upgrade._prepare, side_effect=lambda *args: events.append("prepared"))
    verify = create_autospec(upgrade._verify_published, side_effect=lambda *args: events.append("verified"))
    publish = create_autospec(upgrade.os.replace, side_effect=lambda *args: events.append("published"))
    source.connection.close.side_effect = lambda: events.append("closed")
    monkeypatch.setattr(upgrade, "_backup", backup_fn)
    monkeypatch.setattr(upgrade, "_prepare", prepare)
    monkeypatch.setattr(upgrade, "_verify_published", verify)
    monkeypatch.setattr(upgrade, "_fsync_directory", create_autospec(upgrade._fsync_directory))
    monkeypatch.setattr(upgrade.os, "replace", publish)
    monkeypatch.setattr(Path, "exists", create_autospec(Path.exists, return_value=False))
    return owned, state_db, backup, events, backup_fn, prepare, verify, publish


@pytest.mark.parametrize("version", [3, 4])
def test_publish_happens_only_after_complete_preparation_checkpoint_and_actual_close(publication, version):
    owned, state_db, backup, events, _, _, verify, publish = publication
    owned = replace(owned, metadata=_metadata(version))
    assert upgrade.upgrade_capture_preparation(owned, state_db) == backup
    assert events == ["prepared", "closed", "published", "verified"]
    publish.assert_called_once()
    verify.assert_called_once_with(state_db, backup, owned.metadata)


def test_preparation_failure_preserves_source_and_backup_without_publication(publication):
    owned, state_db, backup, _, _, prepare, _, publish = publication
    prepare.side_effect = sqlite3.OperationalError("无法复制记录")
    with pytest.raises(StateDatabaseError, match=str(backup)):
        upgrade.upgrade_capture_preparation(owned, state_db)
    publish.assert_not_called()
    owned.connection.close.assert_not_called()


def test_busy_checkpoint_stops_before_closing_or_publication(publication):
    owned, state_db, backup, _, _, _, _, publish = publication
    original_execute = owned.connection.execute.side_effect
    owned.connection.execute.side_effect = lambda sql: (
        _cursor([(1, 3, 2)]) if sql == "PRAGMA wal_checkpoint(TRUNCATE)" else original_execute(sql)
    )
    with pytest.raises(StateDatabaseError, match=str(backup)):
        upgrade.upgrade_capture_preparation(owned, state_db)
    owned.connection.close.assert_not_called()
    publish.assert_not_called()


def test_remaining_original_wal_stops_publication(publication):
    owned, state_db, backup, _, _, _, _, publish = publication
    Path.exists.return_value = True
    with pytest.raises(StateDatabaseError, match=str(backup)):
        upgrade.upgrade_capture_preparation(owned, state_db)
    owned.connection.close.assert_called_once()
    publish.assert_not_called()


@pytest.mark.parametrize("published", [True, False])
def test_unknown_replace_is_observed_without_republishing(publication, published):
    owned, state_db, _, _, _, prepare, verify, publish = publication
    publish.side_effect = OSError("发布返回未知")
    if not published:
        verify.side_effect = StateDatabaseError("实际仍为原格式")
        with pytest.raises(StateDatabaseError):
            upgrade.upgrade_capture_preparation(owned, state_db)
    else:
        upgrade.upgrade_capture_preparation(owned, state_db)
    publish.assert_called_once()
    prepare.assert_called_once()
    verify.assert_called_once()


def test_repeated_v5_only_checks_the_existing_database(publication):
    owned, state_db, _, _, backup, prepare, _, publish = publication
    owned = replace(owned, metadata=_metadata(5))
    assert upgrade.upgrade_capture_preparation(owned, state_db) is None
    backup.assert_not_called()
    prepare.assert_not_called()
    publish.assert_not_called()
    owned.connection.close.assert_not_called()


@pytest.mark.parametrize("version", [1, 2])
def test_older_formats_require_their_explicit_path(publication, version):
    owned, state_db, _, _, backup, prepare, _, publish = publication
    with pytest.raises(StateDatabaseError):
        upgrade.upgrade_capture_preparation(replace(owned, metadata=_metadata(version)), state_db)
    backup.assert_not_called()
    prepare.assert_not_called()
    publish.assert_not_called()


@pytest.mark.parametrize("committed", [True, False])
def test_unknown_prepared_commit_is_closed_then_observed_without_recopy(monkeypatch, committed):
    target = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    settings = {"PRAGMA synchronous": [(2,)], "PRAGMA journal_mode=WAL": [("wal",)],
                "PRAGMA foreign_keys": [(1,)], "PRAGMA quick_check": [("ok",)]}
    target.execute.side_effect = lambda sql: _cursor(settings.get(sql, []))
    target.executescript.return_value = _cursor()
    target.commit.side_effect = sqlite3.OperationalError("提交返回未知")
    monkeypatch.setattr(upgrade.sqlite3, "connect", create_autospec(sqlite3.connect, return_value=target))
    monkeypatch.setattr(Path, "resolve", create_autospec(Path.resolve, side_effect=lambda path: path))
    monkeypatch.setattr(upgrade, "_reserve", create_autospec(upgrade._reserve))
    monkeypatch.setattr(upgrade, "_sync_file", create_autospec(upgrade._sync_file))
    monkeypatch.setattr(upgrade, "_fsync_directory", create_autospec(upgrade._fsync_directory))
    monkeypatch.setattr(upgrade, "resource_bytes", create_autospec(upgrade.resource_bytes, return_value=b"-- authority"))
    copy = create_autospec(upgrade._copy_tables)
    monkeypatch.setattr(upgrade, "_copy_tables", copy)

    def observe(*args, **kwargs):
        target.close.assert_called_once()
        if not committed:
            raise StateDatabaseError("副本没有完整提交")

    monkeypatch.setattr(upgrade, "_verify_copy", create_autospec(upgrade._verify_copy, side_effect=observe))
    source = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    if committed:
        upgrade._prepare(source, Path("/state/prepared"), _metadata())
    else:
        with pytest.raises(StateDatabaseError):
            upgrade._prepare(source, Path("/state/prepared"), _metadata())
    copy.assert_called_once()
    target.commit.assert_called_once()


@pytest.mark.parametrize("changed", ["instance_id", "staging_path", "ready_path", "processing_path", "format_version"])
def test_copy_verification_rejects_changed_identity_binding_or_format(monkeypatch, changed):
    original = _metadata()
    metadata = replace(original, format_version=5)
    metadata = replace(metadata, **{changed: 3 if changed == "format_version" else "different"})
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    monkeypatch.setattr(upgrade, "open_existing", create_autospec(upgrade.open_existing,
        return_value=OwnedConnection(connection, metadata)))
    with pytest.raises(StateDatabaseError):
        upgrade._verify_copy(Path("/state/prepared"), original,
            create_autospec(sqlite3.Connection, instance=True, spec_set=True), target_format=5)
    connection.close.assert_called_once()


@pytest.mark.parametrize("failure", [None, "sync", "directory_sync"])
def test_backup_is_consistent_closed_verified_and_durable_before_it_is_returned(monkeypatch, failure):
    source = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    target = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    target.execute.return_value = _cursor([("wal",)])
    events = []
    source.backup.side_effect = lambda *args, **kwargs: events.append("backup")
    target.close.side_effect = lambda: events.append("closed")
    monkeypatch.setattr(Path, "resolve", create_autospec(Path.resolve, side_effect=lambda path: path))
    monkeypatch.setattr(upgrade.sqlite3, "connect", create_autospec(sqlite3.connect, return_value=target))
    monkeypatch.setattr(upgrade, "_reserve", create_autospec(upgrade._reserve))
    monkeypatch.setattr(upgrade, "_verify_copy", create_autospec(upgrade._verify_copy,
        side_effect=lambda *args, **kwargs: events.append("verified")))

    def sync(path):
        events.append("file_synced")
        if failure == "sync":
            raise OSError("备份无法同步")

    monkeypatch.setattr(upgrade, "_sync_file", create_autospec(upgrade._sync_file, side_effect=sync))
    def sync_directory(path):
        events.append("directory_synced")
        if failure == "directory_sync":
            raise OSError("备份父目录无法同步")

    monkeypatch.setattr(upgrade, "_fsync_directory", create_autospec(upgrade._fsync_directory,
        side_effect=sync_directory))
    if failure:
        with pytest.raises(StateDatabaseError, match="before-capture-preparation"):
            upgrade._backup(source, Path("/state/state.db"), _metadata())
        assert events == ["backup", "closed", "verified", "file_synced"] + (
            ["directory_synced"] if failure == "directory_sync" else []
        )
    else:
        path = upgrade._backup(source, Path("/state/state.db"), _metadata())
        assert path.parent == Path("/state")
        assert path.name.startswith("state.db.before-capture-preparation-v5-")
        assert events == ["backup", "closed", "verified", "file_synced", "directory_synced"]
    source.backup.assert_called_once_with(target, pages=128)


@pytest.mark.parametrize("broken", ["quick_check", "foreign_key_check"])
def test_copy_integrity_error_closes_connection_and_rejects_the_copy(monkeypatch, broken):
    target = _rows()
    target.tables["database_metadata"] = [(1, 5)]
    original_execute = target.connection.execute.side_effect
    target.connection.execute.side_effect = lambda sql: (
        _cursor([("broken",)]) if sql == f"PRAGMA {broken}" else original_execute(sql)
    )
    monkeypatch.setattr(upgrade, "open_existing", create_autospec(upgrade.open_existing,
        return_value=OwnedConnection(target.connection, _metadata(5))))
    with pytest.raises(StateDatabaseError):
        upgrade._verify_copy(Path("/state/prepared"), _metadata(), _rows().connection, target_format=5)
    target.connection.close.assert_called_once()


def test_directory_sync_after_replace_must_succeed_without_republication(publication):
    owned, state_db, backup, _, _, _, _, publish = publication
    upgrade._fsync_directory.side_effect = OSError("发布目录无法同步")
    with pytest.raises(StateDatabaseError, match=str(backup)):
        upgrade.upgrade_capture_preparation(owned, state_db)
    publish.assert_called_once()

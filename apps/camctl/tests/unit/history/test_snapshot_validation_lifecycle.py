"""正向恢复的S/H校验结构各自独立；真实校验入口使用受约束存储替身。"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec
import sqlite3

import pytest

from camctl.history import bounded, validators
from camctl.history.bounded import SegmentHistoryHandle
from camctl.persistence import row_validation
from camctl.persistence.repositories.history import HistoryRepository
from camctl.persistence.transaction import encode_json_value


@pytest.fixture
def phases(monkeypatch):
    scripts = set()
    connections = []
    def connect(path, *, isolation_level):
        connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
        connections.append((Path(path).name, connection))
        keys, current = {}, []
        def executescript(script):
            if path in scripts:
                raise sqlite3.OperationalError('table plans already exists')
            scripts.add(path)
            return create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
        def execute(sql, parameters=()):
            cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
            if sql.startswith('PRAGMA table_xinfo'):
                cursor.fetchall.return_value = [(0, 'id', 'INTEGER', 1, None, 1, 0),
                    (1, 'name', 'TEXT', 1, None, 0, 0)]
            elif sql.startswith('PRAGMA index_list'):
                cursor.fetchall.return_value = [(0, 'plan_name', 1, 'u', 0)]
            elif sql.startswith('PRAGMA index_xinfo'):
                cursor.fetchall.return_value = [(0, 1, 'name', 0, 'BINARY', 1)]
            elif sql.startswith('INSERT INTO plans '):
                current[:] = parameters
            elif sql.startswith('SELECT "name", CASE'):
                cursor.fetchone.return_value = (current[1], 1)
            elif sql.startswith('SELECT key_0, applies'):
                cursor.fetchone.return_value = keys.get(parameters[0])
            elif sql.startswith('INSERT INTO projection_unique_'):
                identity, name, applies = parameters
                if (name, applies) in keys.values():
                    raise sqlite3.IntegrityError('unique key conflict')
                keys[identity] = (name, applies)
            elif sql.startswith('DELETE FROM projection_unique_'):
                keys.clear()
            elif not sql.startswith(('SAVEPOINT ', 'RELEASE ', 'ROLLBACK TO ',
                                     'CREATE TEMP TABLE ', 'CREATE UNIQUE INDEX ')):
                raise AssertionError(sql)
            return cursor
        connection.executescript.side_effect = executescript
        connection.execute.side_effect = execute
        return connection
    monkeypatch.setattr(row_validation.sqlite3, 'connect', create_autospec(
        sqlite3.connect, side_effect=connect))
    monkeypatch.setattr(row_validation, 'configure_derived_connection', create_autospec(
        row_validation.configure_derived_connection))
    monkeypatch.setattr(row_validation, '_schema_scripts', create_autospec(
        row_validation._schema_scripts, return_value=('CREATE TABLE plans(id INTEGER PRIMARY KEY,name TEXT UNIQUE);',)))
    registry = {'tables': {'plans': {'owner': {'entity': 'plan', 'id': 'id', 'via': []}}}}
    monkeypatch.setattr(row_validation, 'load_event_registry', create_autospec(
        row_validation.load_event_registry, return_value=registry))
    monkeypatch.setattr(bounded, 'load_event_registry', create_autospec(
        bounded.load_event_registry, return_value=registry))
    monkeypatch.setattr(row_validation, 'json_columns', create_autospec(
        row_validation.json_columns, return_value={}))
    monkeypatch.setattr(bounded, 'foreign_key_targets', create_autospec(
        bounded.foreign_key_targets, return_value={}))
    monkeypatch.setattr(validators, '_resolve_owner_spec', create_autospec(
        validators._resolve_owner_spec, return_value=('plan', 9)))
    repository = HistoryRepository.__new__(HistoryRepository)
    repository._report_resources = None
    handle = SegmentHistoryHandle.__new__(SegmentHistoryHandle)
    handle.repository = repository
    handle.seed = SimpleNamespace(snapshot=SimpleNamespace(transaction=object()))
    handle._temporary, handle.batch_size, handle.stats = SimpleNamespace(name='/unused/unit-owned'), 128, {}
    handle._row_owner = create_autospec(handle._row_owner, return_value=('plan', 9))
    handle._db = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    rows = [{'id': 9, 'name': 'same'}]
    def select(sql, parameters):
        assert sql.startswith('SELECT tab,id,body FROM rows')
        cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
        cursor.fetchall.return_value = [('plans', row['id'], encode_json_value(row)) for row in rows] if parameters[:2] == ('', 0) else []
        return cursor
    handle._db.execute.side_effect = select
    return handle, rows, connections


def test_forward_snapshot_and_h_validate_with_independent_schema_paths(phases):
    handle, _, connections = phases
    handle._validate(build_indexes=False)
    handle._validate()
    assert [name for name, _ in connections] == ['snapshot-validation.db', 'validation.db']
    for _, connection in connections:
        connection.executescript.assert_called_once()
        connection.close.assert_called_once_with()


@pytest.mark.parametrize('stage', ['snapshot', 'h'])
def test_stage_failure_closes_its_validator_and_keeps_unique_conflicts(phases, stage):
    handle, rows, connections = phases
    if stage == 'h':
        handle._validate(build_indexes=False)
    rows.append({'id': 10, 'name': 'same'})
    with pytest.raises(row_validation.ProjectionRowValidationError, match='唯一键冲突'):
        handle._validate(build_indexes=stage == 'h')
    for _, connection in connections:
        connection.close.assert_called_once_with()

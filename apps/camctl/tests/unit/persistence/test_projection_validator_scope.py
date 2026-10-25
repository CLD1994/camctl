"""唯一性账本按对象隔离，校验结构及其有限元数据可继续使用。"""

import asyncio
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.persistence.row_validation import (
    ProjectionRowValidationError, ProjectionRowValidator, _UniqueIndex,
)


@pytest.fixture
def validator_storage():
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    entries = {"projection_unique_0": {}, "projection_unique_1": {}}

    def execute(query, parameters=()):
        cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
        if query.startswith("DELETE FROM "):
            entries[query.removeprefix("DELETE FROM ")].clear()
        elif query.startswith("SELECT key_"):
            table = query.split(" FROM ", 1)[1].split()[0]
            cursor.fetchone.return_value = entries[table].get(parameters[0])
        elif query.startswith("INSERT INTO projection_unique_"):
            table = query.split()[2]
            identity, *value = parameters
            if tuple(value) in entries[table].values():
                raise sqlite3.IntegrityError("unique key conflict")
            entries[table][identity] = tuple(value)
        elif not query.startswith(("SAVEPOINT ", "RELEASE ", "ROLLBACK TO ")):
            raise AssertionError(f"未声明的存储操作: {query}")
        return cursor

    connection.execute.side_effect = execute
    validator = ProjectionRowValidator()
    validator._connection = connection
    indexes = tuple(_UniqueIndex(f"unique_{i}", ("key",), "1", name)
                    for i, name in enumerate(entries))
    validator._indexes["test_table"] = indexes
    validator._columns["test_table"] = (("id", "INTEGER", True),)
    validator._index_count = len(indexes)
    return validator, connection, entries, indexes


def _save(validator, indexes, identity, value):
    validator._save_unique_keys("test_table", identity,
        [(index, (value, 1)) for index in indexes])


def test_unique_keys_still_reject_conflicts_across_pages_of_one_object(validator_storage):
    validator, _, _, indexes = validator_storage
    with validator.object_scope():
        _save(validator, indexes, 1, "same-key")
        with pytest.raises(ProjectionRowValidationError):
            _save(validator, indexes, 2, "same-key")


def test_next_object_can_reuse_keys_and_same_identity_with_other_values(validator_storage):
    validator, _, entries, indexes = validator_storage
    with validator.object_scope():
        _save(validator, indexes, 1, "first-key")
    assert all(not rows for rows in entries.values())

    with validator.object_scope():
        _save(validator, indexes, 2, "first-key")
        _save(validator, indexes, 1, "second-key")

    assert all(not rows for rows in entries.values())
    assert validator._indexes["test_table"] is indexes
    assert validator._columns == {"test_table": (("id", "INTEGER", True),)}
    assert validator._index_count == 2


@pytest.mark.parametrize("failure", [RuntimeError("object failed"),
                                    asyncio.CancelledError("cancelled"),
                                    BaseException("control")])
def test_object_failure_clears_keys_and_preserves_original_exception(validator_storage, failure):
    validator, _, entries, indexes = validator_storage
    with pytest.raises(type(failure)) as caught:
        with validator.object_scope():
            _save(validator, indexes, 1, "key")
            raise failure

    assert caught.value is failure
    assert all(not rows for rows in entries.values())
    with validator.object_scope():
        _save(validator, indexes, 1, "different-key")


def test_entering_object_clears_existing_keys(validator_storage):
    validator, _, entries, indexes = validator_storage
    _save(validator, indexes, 1, "stale-key")
    with validator.object_scope():
        assert all(not rows for rows in entries.values())


def test_nested_object_scope_is_rejected_without_clearing_active_keys(validator_storage):
    validator, _, entries, indexes = validator_storage
    with validator.object_scope():
        _save(validator, indexes, 1, "key")
        with pytest.raises(ProjectionRowValidationError):
            with validator.object_scope():
                pass
        assert all(rows for rows in entries.values())
        with pytest.raises(ProjectionRowValidationError):
            _save(validator, indexes, 2, "key")


def test_closed_validator_cannot_start_an_object_scope():
    with pytest.raises(ProjectionRowValidationError):
        with ProjectionRowValidator().object_scope():
            pass


@pytest.mark.parametrize("fail_at", [1, 3])
def test_key_cleanup_error_is_not_hidden_or_treated_as_an_empty_scope(validator_storage, fail_at):
    validator, connection, _, _ = validator_storage
    original_execute = connection.execute.side_effect
    deleted = 0
    failure = sqlite3.OperationalError("key cleanup failed")

    def execute(query, parameters=()):
        nonlocal deleted
        if query.startswith("DELETE FROM "):
            deleted += 1
            if deleted == fail_at:
                raise failure
        return original_execute(query, parameters)

    connection.execute.side_effect = execute
    with pytest.raises(sqlite3.OperationalError) as caught:
        with validator.object_scope():
            pass
    assert caught.value is failure
    assert not validator._object_active

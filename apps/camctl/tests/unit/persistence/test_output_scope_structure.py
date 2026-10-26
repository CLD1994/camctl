"""输出范围格式按权威结构核对操作流程约束，不访问真实数据库。"""

import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.persistence import output_scope_structure as structure


_AUTHORITY = """CREATE TABLE operation_runs (
    kind INTEGER NOT NULL CHECK (kind BETWEEN 1 AND 11),
    responsibility_key TEXT NOT NULL,
    CHECK (kind <> 11 OR responsibility_key = 'scope/' || activity_id)
) STRICT;"""


@pytest.mark.parametrize("change", [None, "kind", "responsibility", "missing"])
def test_scope_structure_requires_the_authoritative_operation_constraints(monkeypatch, change):
    monkeypatch.setattr(structure, "resource_bytes", create_autospec(
        structure.resource_bytes, return_value=_AUTHORITY.encode(),
    ))
    actual = _AUTHORITY.rstrip(";")
    if change == "kind":
        actual = actual.replace("BETWEEN 1 AND 11", "BETWEEN 1 AND 10")
    elif change == "responsibility":
        actual = actual.replace("'scope/'", "'results/'")
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
    cursor.fetchone.return_value = None if change == "missing" else (actual,)
    connection.execute.return_value = cursor
    if change:
        with pytest.raises(ValueError):
            structure.verify_output_scope_structure(connection)
    else:
        structure.verify_output_scope_structure(connection)
    cursor.close.assert_called_once()


def test_scope_structure_ignores_whitespace_comments_and_keyword_case(monkeypatch):
    monkeypatch.setattr(structure, "resource_bytes", create_autospec(
        structure.resource_bytes, return_value=_AUTHORITY.encode(),
    ))
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    connection.execute.return_value.fetchone.return_value = (
        _AUTHORITY.rstrip(";").replace("CREATE TABLE", "create /*结构*/ table"),
    )
    structure.verify_output_scope_structure(connection)

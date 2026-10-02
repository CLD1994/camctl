"""恢复查询在读取成功、没有原责任和读取失败时释放游标。"""

from dataclasses import replace
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository

from .test_read_associations import read_targets, _command
from .test_local_read import local_read
from .test_read_recovery import prepared_read
from ..operations.test_result_reuse import _FaultConnection


class _RecoveryConnection(_FaultConnection):
    def __init__(self, connection, table, fail_fetch):
        super().__init__(connection, "unused")
        self.prefix = f"SELECT id FROM {table} WHERE "
        self.fail_fetch = fail_fetch
        self.cursors = []

    def execute(self, sql, parameters=()):
        cursor = self._connection.execute(sql, parameters)
        if sql.startswith(self.prefix):
            probe = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
            probe.close.side_effect = cursor.close
            probe.fetchall.side_effect = (sqlite3.OperationalError("read failed") if self.fail_fetch else cursor.fetchall)
            self.cursors.append(probe)
            return probe
        return cursor


@pytest.mark.parametrize("table", ["file_copies", "intermediate_files", "operation_runs"])
@pytest.mark.parametrize("fail_fetch", [False, True])
def test_preparation_queries_close_on_success_and_fetch_failure(prepared_read, table, fail_fetch):
    owned, command, original = prepared_read
    before = tuple(owned.connection.iterdump())
    probe = _RecoveryConnection(owned.connection, table, fail_fetch)
    result = OutputsRepository().grant_file(command, new_operation_key(), replace(owned, connection=probe))
    assert probe.cursors
    for cursor in probe.cursors:
        cursor.close.assert_called_once_with()
    if fail_fetch:
        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert isinstance(result.error, sqlite3.OperationalError), result.error
    else:
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value.target_file_id == original.target_file_id
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("table", ["file_copies", "intermediate_files", "operation_runs"])
def test_initial_internal_read_closes_empty_preparation_query(read_targets, table):
    owned = read_targets
    owned.connection.execute("DELETE FROM obtain_items")
    owned.connection.execute("DELETE FROM outputs")
    owned.connection.commit()
    probe = _RecoveryConnection(owned.connection, table, False)
    result = OutputsRepository().grant_file(_command(internal=True), new_operation_key(),
                                           replace(owned, connection=probe))
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert len(probe.cursors) == 1
    probe.cursors[0].close.assert_called_once_with()

"""原保存核实只在本次可靠事务内共享历史读取。"""
import sqlite3
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.capture.segment_finish import FinishSegmentRecording
from camctl.capture.segment_storage import SegmentResultRef
from camctl.contracts.values import new_operation_key
from camctl.persistence.repositories import segment_transactions as storage


@pytest.mark.parametrize("already_open", [False, True])
def test_saved_finish_verification_owns_read_reuse_scope(monkeypatch, already_open):
    from camctl.persistence.read_reuse import reuse_read

    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    connection.in_transaction = already_open
    connection.total_changes = 0

    def execute(sql):
        if sql == "BEGIN":
            connection.in_transaction = True
        elif sql == "ROLLBACK":
            connection.in_transaction = False
        else:
            raise AssertionError(sql)

    connection.execute.side_effect = execute
    loaded = []

    def load():
        loaded.append(len(loaded) + 1)
        return {"proof": loaded[-1]}

    def verify(request, key, reader, batch):
        return tuple(reuse_read(reader.connection, ("proof", 7), load) for _ in range(2))

    monkeypatch.setattr(storage, "_verify_segment_finish", create_autospec(
        storage._verify_segment_finish, side_effect=verify))
    request = FinishSegmentRecording(SegmentResultRef(1, 2, 1), 3, 4, 5)
    reader = SimpleNamespace(connection=connection)
    first = storage.verify_segment_finish(request, new_operation_key(), reader, execution_finished=True)
    second = storage.verify_segment_finish(request, new_operation_key(), reader, execution_finished=True)
    assert first == ({"proof": 1}, {"proof": 1})
    assert second == ({"proof": 2}, {"proof": 2})
    assert loaded == [1, 2]
    assert connection.execute.call_count == (0 if already_open else 4)

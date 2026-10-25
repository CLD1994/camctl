"""原键归属核实从共同端口取得实际原事件。"""
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.history.events import EventEnvelope
from camctl.persistence import fact_reads
from camctl.persistence.repositories import result_file_ownership as ownership
from camctl.persistence.transaction import TransactionError


def test_ownership_verification_reads_original_event_from_common_port(monkeypatch):
    connection = create_autospec(sqlite3.Connection, instance=True)
    transaction = TransactionRange(1, 1, 1)
    event = EventEnvelope(1, 1, ownership._EVENT['id']+1, 1, 0, 1, None, 1, {}, ())
    record = fact_reads.EventRecord((1, 1, 1, 1, 0, 1, None, '{}'), event, transaction)
    read = create_autospec(fact_reads.read_event, return_value=record)
    monkeypatch.setattr(ownership, 'read_event', read)
    monkeypatch.setattr(ownership, 'read_operation_transaction', create_autospec(
        fact_reads.read_operation_transaction, return_value=transaction))
    monkeypatch.setattr(ownership, 'validate_event_structure', create_autospec(ownership.validate_event_structure))
    with pytest.raises(TransactionError, match='其他完整事务'):
        ownership._verify(object(), 'key', connection)
    read.assert_called_once_with(connection, 1)
    connection.execute.assert_not_called()

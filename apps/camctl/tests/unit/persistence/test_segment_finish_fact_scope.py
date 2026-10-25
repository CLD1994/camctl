"""原键核实保留可靠只读事务，并沿用内核事实生命周期。"""
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import call, create_autospec

import pytest

from camctl.capture.segment_finish import FinishSegmentRecording
from camctl.capture.segment_storage import SegmentResultRef
from camctl.contracts.values import new_operation_key
from camctl.persistence.repositories import segment_transactions as storage


@pytest.mark.parametrize("already_open", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_saved_finish_scope_closes_on_result_and_error(monkeypatch, already_open, failure):
    db = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    db.in_transaction = already_open
    transitions = []
    @contextmanager
    def scope(connection, opened):
        assert connection is db
        assert opened is (not already_open)
        transitions.append("enter")
        try:
            yield
        finally:
            transitions.append("exit")
    monkeypatch.setattr(storage, "opened_operation_fact_scope", create_autospec(
        storage.opened_operation_fact_scope, side_effect=scope))
    error = sqlite3.OperationalError("原组读取失败")
    verify = create_autospec(storage._verify_segment_finish,
        side_effect=error if failure else None, return_value=None)
    monkeypatch.setattr(storage, "_verify_segment_finish", verify)
    request = FinishSegmentRecording(SegmentResultRef(1, 2, 1), 3, 4, 5)
    key = new_operation_key()
    if failure:
        with pytest.raises(sqlite3.OperationalError) as caught:
            storage.verify_segment_finish(request, key, SimpleNamespace(connection=db), execution_finished=True)
        assert caught.value is error
    else:
        assert storage.verify_segment_finish(request, key, SimpleNamespace(connection=db), execution_finished=True) is None
    assert transitions == ["enter", "exit"]
    assert db.execute.call_args_list == ([] if already_open else [call("BEGIN"), call("ROLLBACK")])

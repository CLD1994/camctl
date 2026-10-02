"""原操作键读取与真实事件事务、精确解码及整组回滚的组合。"""

from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.contracts.history_values import HistoryBoundary, ReadOrder, ReadScope, TransactionRange
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.persistence.repositories.history import HistoryRepository
from camctl.persistence.transaction import commit_operation, saved_transaction_events

from .test_runtime import _create_valid_database
from .test_transactions import PlanCreateCommand, _open, plan_guards  # noqa: F401


@pytest.fixture
def saved_database(tmp_path, plan_guards):
    _create_valid_database(tmp_path / "state.db")
    owned = _open(tmp_path)

    class PrecisePlans(PlanCreateCommand):
        def plan(self, scope):
            plan = super().plan(scope)
            return replace(plan, events=tuple(replace(event, evidence={
                "observation": {"value": Decimal("0.10000000000000001")},
            }) for event in plan.events))

    key = new_operation_key()
    try:
        for command, operation_key in (
            (PlanCreateCommand((1,)), new_operation_key()),
            (PrecisePlans((2, 3)), key),
            (PlanCreateCommand((4,)), new_operation_key()),
        ):
            receipt = commit_operation(command, operation_key, owned)
            assert receipt.kind == "completed", receipt.error
        yield owned, key
    finally:
        owned.connection.close()


def test_original_key_reads_full_exact_group_after_later_commit(saved_database):
    owned, key = saved_database
    connection = owned.connection
    changes = connection.total_changes
    connection.execute("BEGIN")
    saved = saved_transaction_events(connection, key)
    assert [event["event_id"] for event in saved] == [2, 3]
    assert all(event["transaction"] == TransactionRange(2, 2, 3) for event in saved)
    assert [event["body"]["rows"][0]["id"] for event in saved] == [2, 3]
    assert saved[0]["body"]["evidence"]["observation"]["value"] == Decimal("0.10000000000000001")
    assert connection.in_transaction is True
    assert connection.total_changes == changes
    connection.execute("ROLLBACK")


def test_unknown_key_is_absent_without_creating_history(saved_database):
    owned, _ = saved_database
    changes = owned.connection.total_changes
    assert saved_transaction_events(owned.connection, new_operation_key()) is None
    assert owned.connection.total_changes == changes


@pytest.mark.parametrize("statement,parameters", [
    ("DELETE FROM history_events WHERE id = 3", ()),
    ("UPDATE history_events SET transaction_id = 3 WHERE id = 3", ()),
    ("UPDATE history_transactions SET last_event_id = 5 WHERE id = 2", ()),
    ("UPDATE history_transactions SET last_event_id = 2 WHERE id = 1", ()),
    ("DELETE FROM history_transactions WHERE id = 1", ()),
    ("UPDATE history_events SET body_json = ? WHERE id = 3", ('{"reason":1,"reason":2,"evidence":{},"rows":[]}',)),
    ("UPDATE history_events SET body_json = '{}' WHERE id = 3", ()),
    ("UPDATE history_events SET event_version = 2 WHERE id = 3", ()),
])
def test_invalid_original_group_is_rejected_by_both_read_entries(saved_database, tmp_path, statement, parameters):
    owned, key = saved_database
    # 注入正常保存入口无法形成的坏事实；读取与保存门禁保持原约束。
    owned.connection.execute("PRAGMA ignore_check_constraints = ON")
    try:
        owned.connection.execute(statement, parameters)
    finally:
        owned.connection.execute("PRAGMA ignore_check_constraints = OFF")
    with pytest.raises(ConsistencyError):
        saved_transaction_events(owned.connection, key)
    repository = HistoryRepository(tmp_path / "state.db")
    with pytest.raises(ConsistencyError):
        repository.read_events(ReadScope(ReadOrder.ASCENDING, None, 1, 3, 8, lambda value: value),
                               HistoryBoundary(2, 3))


def test_saved_read_error_rolls_back_new_group_and_projections(saved_database):
    owned, key = saved_database
    connection = owned.connection
    connection.execute("UPDATE history_events SET body_json = '{}' WHERE id = 3")
    before = tuple(connection.iterdump())

    class SaveThenVerify(PlanCreateCommand):
        def plan(self, scope):
            return replace(super().plan(scope), complete_result=lambda connection, result:
                           saved_transaction_events(connection, key))

    receipt = commit_operation(SaveThenVerify((5,)), new_operation_key(), owned)
    assert receipt.kind == "rolled_back"
    assert isinstance(receipt.error, ConsistencyError)
    assert connection.in_transaction is False
    assert tuple(connection.iterdump()) == before

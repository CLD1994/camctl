"""绑定读取结果引用的取消事实，以内存替身隔离全部历史读取。"""

from types import ModuleType, SimpleNamespace
import sys

import pytest

from camctl.contracts import enums
from camctl.contracts.values import ConsistencyError
from camctl.persistence.repositories.binding_read_cancel import binding_cancel_event, saved_binding_read_failure, load_saved_binding_read_failure


@pytest.fixture
def history_stubs(monkeypatch):
    monkeypatch.setattr(enums, "resource_bytes", lambda name: pytest.fail("不能读取真实包资源"))
    monkeypatch.setattr(enums, "load_registry", lambda: {"history_objects": {"action": {"id": 1}}})
    decoding = ModuleType("camctl.history.decoding")
    decoding.decode_event_row = lambda row: SimpleNamespace(event_id=row[0], transaction_id=row[1], occurred_at=row[2])
    events = ModuleType("camctl.history.events")
    events.load_event_registry = lambda: {"events": {"CANCEL_CHANGED": {"id": 25}}}
    transaction = ModuleType("camctl.persistence.transaction")
    transaction.read_transaction_range = lambda connection, identity: SimpleNamespace(first_event_id=10, last_event_id=12)
    for module in (decoding, events, transaction):
        monkeypatch.setitem(sys.modules, module.__name__, module)


class Connection:
    def __init__(self, rows):
        self.rows = rows
        self.args = None
    def execute(self, sql, args):
        self.args = args
        return self
    def fetchall(self):
        return self.rows
    def close(self):
        pass


def test_cancel_event_returns_fixed_reference_and_limits_history(history_stubs):
    connection = Connection([(11, 4, 35)])
    assert binding_cancel_event(connection, 8, 5) == {"event_id": 11, "transaction_id": 4, "occurred_at": 35}
    assert connection.args == (1, 8, 25, 5, 8)


@pytest.mark.parametrize("rows", [[], [(11, 4, 35), (12, 4, 36)], [(13, 4, 35)]])
def test_cancel_event_rejects_missing_duplicate_or_incomplete_history(history_stubs, rows):
    with pytest.raises(ConsistencyError):
        binding_cancel_event(Connection(rows), 8, 5)


@pytest.mark.parametrize("damage", [None, "wrong_copy", "wrong_error", "cancel_selected", "unknown_result"])
def test_retirement_requires_exact_saved_read_result(monkeypatch, damage):
    error = {"code": "device_binding_unavailable", "stage": "execution", "details": {"reason": "missing"}}
    result = {"settlement": {"basis": "observed", "evidence": {"type": "read_returned", "version": 1, "data": {}}}}
    attempt = {"run_id": 3, "result_event_id": 11, "status": 2, "result_json": result, "error_json": None}
    run = {"id": 3, "action_id": 4, "copy_id": 5, "status": 4, "error_json": error}
    evidence = {"error": error, "cancel_event": None, "current_identity": None}
    events = [{"type": 12, "reason": 1, "event_id": 11, "body": {"rows": [{
        "table": "operation_attempts", "id": 6, "after": {"values": {"status": 2, "result_json": result}}}]}},
        {"type": 10, "reason": 3, "body": {"evidence": {"binding_read_finish": evidence}, "rows": [{
            "table": "operation_runs", "id": 3, "after": {"values": {"status": 4, "error_json": error}}}]}}]
    if damage == "wrong_copy":
        run["copy_id"] = 99
    elif damage == "wrong_error":
        evidence["error"] = {"code": "other"}
    elif damage == "cancel_selected":
        evidence["cancel_event"] = {"event_id": 20}
    elif damage == "unknown_result":
        events[0]["reason"] = 3
    transaction = ModuleType("camctl.persistence.transaction")
    transaction.saved_transaction_events = lambda connection, key: events
    transaction.row_facts = lambda connection, table, identity: attempt if table == "operation_attempts" else run
    monkeypatch.setitem(sys.modules, transaction.__name__, transaction)
    if damage is None:
        assert saved_binding_read_failure(object(), "original-key", 4, 5, error)
    else:
        with pytest.raises(ConsistencyError):
            saved_binding_read_failure(object(), "original-key", 4, 5, error)


def test_recovery_loads_original_transaction_identity_and_rejects_different_identity(monkeypatch):
    identity = {"serial": "observed"}
    error = {"code": "device_binding_unavailable", "stage": "execution", "details": {"reason": "physical_identity_mismatch"}}
    result = {"settlement": {"basis": "observed", "evidence": {"type": "read_returned", "version": 1, "data": {}}}}
    attempt = {"run_id": 3, "result_event_id": 11, "status": 2, "result_json": result, "error_json": None}
    run = {"id": 3, "action_id": 4, "copy_id": 5, "status": 4, "error_json": error}
    events = [{"type": 12, "reason": 1, "event_id": 11, "occurred_at": 17, "body": {"rows": [{
        "table": "operation_attempts", "id": 6, "after": {"values": {"status": 2, "result_json": result}}}]}},
        {"type": 10, "reason": 3, "body": {"evidence": {"binding_read_finish": {
            "error": error, "cancel_event": None, "current_identity": identity}}, "rows": [{
            "table": "operation_runs", "id": 3, "after": {"values": {"status": 4, "error_json": error}}}]}}]
    transaction = ModuleType("camctl.persistence.transaction")
    transaction.saved_transaction_events = lambda connection, key: events
    transaction.row_facts = lambda connection, table, identity: attempt if table == "operation_attempts" else run
    monkeypatch.setitem(sys.modules, transaction.__name__, transaction)
    assert load_saved_binding_read_failure(Connection([("original-key",)]), 4, 5, error) == ("original-key", identity, 17)
    with pytest.raises(ConsistencyError):
        saved_binding_read_failure(object(), "original-key", 4, 5, error, {"serial": "different"})
    with pytest.raises(ConsistencyError):
        load_saved_binding_read_failure(Connection([]), 4, 5, error)

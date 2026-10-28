"""绑定读取结果引用的取消事实，以内存替身隔离全部历史读取。"""

from types import ModuleType, SimpleNamespace
import sys

import pytest

from camctl.contracts import enums
from camctl.contracts.values import ConsistencyError
from camctl.persistence.repositories.binding_read_cancel import binding_cancel_event


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

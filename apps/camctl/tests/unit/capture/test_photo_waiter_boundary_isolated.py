"""固定完整事务边界上等待者状态的纯核验。"""
import importlib.util
import sys
from enum import IntEnum
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def api(monkeypatch):
    class Status(IntEnum): RUNNING = 2; SUCCEEDED = 3
    class Selection(IntEnum): PENDING = 1; FIXED = 2
    class Type(IntEnum): OBTAIN_ACTION_OUTPUTS = 5
    class Empty(IntEnum): VALUE = 1
    values = ModuleType("camctl.contracts.values")
    values.ConsistencyError, values.ObjectId, values.new_operation_key = ValueError, int, lambda: "key"
    enums = ModuleType("camctl.contracts.enums")
    enums.enum_for = lambda name: {"actions.status": Status, "obtain_source_selections.status": Selection,
        "actions.type": Type}.get(name, Empty)
    history = ModuleType("camctl.contracts.history_values")
    history.HistoryBoundary = type("HistoryBoundary", (), {})
    io = ModuleType("camctl.host_files.io"); io.HashResult = object; io.hash_target = None
    models = ModuleType("camctl.host_files.models")
    models.BoundDirectories = models.FilePurpose = models.FileRef = object
    tasks = ModuleType("camctl.host_files.tasks")
    tasks.AsyncFileTask = tasks.FileTaskError = tasks.FileTaskExecutor = tasks.FileTaskId = object
    row_history = ModuleType("camctl.persistence.row_history")
    row_history.read_row_values_at_boundary = lambda *args, **kwargs: {"status": kwargs["current_values"]["at_h"]}
    transaction = ModuleType("camctl.persistence.transaction")
    states = {"actions": {"type": 5, "created_event_id": 3, "at_h": 2},
              "obtain_source_selections": {"dependency_id": 4, "at_h": 1}}
    transaction.row_facts = lambda connection, table, identity: states[table]
    transaction.read_transaction_range = lambda *args: None
    discovery = ModuleType("camctl.outputs.read_discovery"); discovery.DEFAULT_BATCH_SIZE = 32
    for stub in (values, enums, history, io, models, tasks, row_history, transaction, discovery):
        monkeypatch.setitem(sys.modules, stub.__name__, stub)
    path = Path(__file__).parents[3] / "src/camctl/outputs/segment_copy_reuse.py"
    name = "camctl.outputs._isolated_photo_waiter_boundary"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    module._current_boundary = lambda connection: SimpleNamespace(txn_id=20, last_event_id=100)
    return module, states


class Cursor:
    def fetchone(self): return (5, 1)
    def close(self): pass


class Connection:
    def execute(self, *args): return Cursor()


@pytest.mark.parametrize("created,action_at_h,selection_at_h,bound", [
    (3, 2, 1, True), (3, 3, 1, False), (3, 2, 2, False), (16, 2, 1, False),
])
def test_h_only_binds_running_waiting_source_selection(api, created, action_at_h, selection_at_h, bound):
    module, states = api
    states["actions"].update(created_event_id=created, at_h=action_at_h)
    states["obtain_source_selections"]["at_h"] = selection_at_h
    boundary = SimpleNamespace(txn_id=9, last_event_id=15)
    actual, selected = module._selection_is_bound_at_boundary(source_action_id=1, obtain_action_id=5,
        selection_id=6, boundary=boundary, owned=SimpleNamespace(connection=Connection()))
    assert actual is boundary and selected is bound

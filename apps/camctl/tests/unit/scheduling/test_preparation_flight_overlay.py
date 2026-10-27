"""同事务资源事实只使用隔离替身和已经验证的前序。"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

from camctl.contracts.values import ConsistencyError


@pytest.fixture
def reader_module(monkeypatch):
    from camctl.contracts import enums
    from camctl import resources

    monkeypatch.setattr(resources, "resource_bytes", lambda *_: pytest.fail("不能读取包资源"))
    monkeypatch.setattr(enums, "resource_bytes", lambda *_: pytest.fail("不能读取包资源"))
    definitions = {"operation_runs.kind": SimpleNamespace(START=1, READ_FILE=3),
                   "operation_attempts.status": SimpleNamespace(RUNNING=1)}
    monkeypatch.setattr(enums, "enum_for", lambda name: definitions[name])
    from camctl.scheduling import resources as scheduling_resources
    monkeypatch.setattr(scheduling_resources, "enum_for", lambda name: definitions[name])
    preparation = ModuleType("camctl.devices.capture_preparation")
    preparation.CAPTURE_ACTION_TYPES = ()
    capture_facts = ModuleType("camctl.persistence.repositories.capture_facts")
    capture_facts.preparation_calls_resolved = lambda *args, **kwargs: True
    transaction = ModuleType("camctl.persistence.transaction")
    transaction.row_facts = lambda connection, table, identity: connection.rows.get((table, identity))
    path = Path(__file__).parents[3] / "src/camctl/persistence/repositories/preparation.py"
    spec = importlib.util.spec_from_file_location("camctl.persistence.repositories._flight_test", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {preparation.__name__: preparation, capture_facts.__name__: capture_facts,
                                  transaction.__name__: transaction}):
        spec.loader.exec_module(module)
        yield module


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def __iter__(self):
        return iter(self.rows)

    def fetchone(self):
        return next(iter(self.rows), None)

    def close(self):
        pass


class Database:
    def __init__(self, rows=()):
        self.rows = {(table, row["id"]): row for table, row in rows}

    def execute(self, sql, parameters):
        attempts = [row for (table, _), row in self.rows.items()
                    if table == "operation_attempts" and row["status"] == 1]
        if "c.slot_device_id=?" in sql or "r.copy_id=?" in sql:
            kind, device = parameters
            found = [(a["id"],) for a in attempts
                     if (r := self.rows.get(("operation_runs", a["run_id"])))
                     and r["kind"] == kind
                     and (c := self.rows.get(("file_copies", r["copy_id"])))
                     and (r["copy_id"] == device if "r.copy_id=?" in sql
                          else c["slot_device_id"] == device)]
        elif "a.device_id=?" in sql or "r.action_id=?" in sql:
            kind, device = parameters
            found = [(a["id"],) for a in attempts
                     if (r := self.rows.get(("operation_runs", a["run_id"])))
                     and r["kind"] == kind
                     and (owner := self.rows.get(("actions", r["action_id"])))
                     and (r["action_id"] == device if "r.action_id=?" in sql
                          else owner["device_id"] == device)]
        else:
            raise AssertionError(sql)
        return Cursor(found)


def change(table, identity, values, *, exists=False):
    return SimpleNamespace(table=table, row_id=identity,
                           before=SimpleNamespace(exists=exists),
                           after=SimpleNamespace(exists=True, values=values))


def accepted(*rows):
    return SimpleNamespace(envelope=SimpleNamespace(rows=rows))


@pytest.mark.parametrize("kind,method,owner_table,owner_id,owner", [
    (1, "device_start_in_flight", "actions", 10, {"device_id": "camera"}),
    (3, "device_read_in_flight", "file_copies", 20, {"slot_device_id": "camera"}),
])
def test_new_intent_and_other_device(reader_module, kind, method, owner_table, owner_id, owner):
    run = {"kind": kind, "action_id": 10, "copy_id": 20}
    events = [accepted(change(owner_table, owner_id, owner), change("operation_runs", 3, run),
                       change("operation_attempts", 5, {"run_id": 3, "status": 1}))]
    reader = reader_module.PreparationReads(Database(), events)
    assert getattr(reader, method)("camera") is True
    assert getattr(reader, method)("other") is False


@pytest.mark.parametrize("kind,method,owner_table,owner", [
    (1, "device_start_in_flight", "actions", {"id": 10, "device_id": "camera"}),
    (3, "device_read_in_flight", "file_copies", {"id": 20, "slot_device_id": "camera"}),
])
def test_prior_result_closes_database_attempt(reader_module, kind, method, owner_table, owner):
    db = Database([(owner_table, owner), ("operation_runs", {"id": 3, "kind": kind,
                   "action_id": 10, "copy_id": 20}),
                   ("operation_attempts", {"id": 5, "run_id": 3, "status": 1})])
    result = accepted(change("operation_attempts", 5, {"status": 2}, exists=True))
    assert getattr(reader_module.PreparationReads(db, [result]), method)("camera") is False
    assert getattr(reader_module.PreparationReads(db), method)("camera") is True


def test_host_read_does_not_occupy_camera(reader_module):
    events = [accepted(change("file_copies", 20, {"slot_device_id": None}),
                       change("operation_runs", 3, {"kind": 3, "copy_id": 20}),
                       change("operation_attempts", 5, {"run_id": 3, "status": 1}))]
    assert not reader_module.PreparationReads(Database(), events).device_read_in_flight("camera")


@pytest.mark.parametrize("rows", [
    [change("operation_attempts", 5, {"run_id": 3, "status": 1})],
    [change("operation_attempts", 5, {"run_id": 3, "status": 1}),
     change("operation_runs", 3, {"kind": 3, "copy_id": 20})],
])
def test_missing_association_is_error(reader_module, rows):
    with pytest.raises(ConsistencyError):
        reader_module.PreparationReads(Database(), [accepted(*rows)]).device_read_in_flight("camera")


def test_result_does_not_hide_another_running_attempt(reader_module):
    db = Database([("actions", {"id": 10, "device_id": "camera"}),
                   ("operation_runs", {"id": 3, "kind": 1, "action_id": 10}),
                   ("operation_attempts", {"id": 5, "run_id": 3, "status": 1}),
                   ("operation_attempts", {"id": 6, "run_id": 3, "status": 1})])
    result = accepted(change("operation_attempts", 5, {"status": 2}, exists=True))
    assert reader_module.PreparationReads(db, [result]).device_start_in_flight("camera")


def test_only_accepted_prefix_is_visible_and_later_result_overlays_intent(reader_module):
    intent = accepted(change("actions", 10, {"device_id": "camera"}),
                      change("operation_runs", 3, {"kind": 1, "action_id": 10}),
                      change("operation_attempts", 5, {"run_id": 3, "status": 1}))
    result = accepted(change("operation_attempts", 5, {"status": 2}, exists=True))
    accepted_prefix = [intent]
    before = reader_module.PreparationReads(Database(), accepted_prefix)
    accepted_prefix.append(result)
    assert before.device_start_in_flight("camera")
    assert not reader_module.PreparationReads(Database(), accepted_prefix).device_start_in_flight("camera")


def test_database_failure_is_not_treated_as_no_call(reader_module):
    class FailingDatabase(Database):
        def execute(self, sql, parameters):
            raise OSError("database unavailable")

    reader = reader_module.PreparationReads(FailingDatabase(), [accepted()])
    with pytest.raises(OSError, match="database unavailable"):
        reader.device_start_in_flight("camera")


@pytest.mark.parametrize("kind,method,owner_table,owner_id,column", [
    (1, "device_start_in_flight", "actions", 10, "device_id"),
    (3, "device_read_in_flight", "file_copies", 20, "slot_device_id"),
])
@pytest.mark.parametrize("old,new", [("camera", "other"), ("other", "camera")])
def test_database_attempt_uses_projected_owner(reader_module, kind, method, owner_table,
                                               owner_id, column, old, new):
    db = Database([(owner_table, {"id": owner_id, column: old}),
                   ("operation_runs", {"id": 3, "kind": kind, "action_id": 10, "copy_id": 20}),
                   ("operation_attempts", {"id": 5, "run_id": 3, "status": 1})])
    event = accepted(change(owner_table, owner_id, {column: new}, exists=True))
    assert getattr(reader_module.PreparationReads(db, [event]), method)("camera") == (new == "camera")


def test_unrelated_finished_attempt_needs_no_run(reader_module):
    event = accepted(change("operation_attempts", 5, {"run_id": 3, "status": 2}))
    assert not reader_module.PreparationReads(Database(), [event]).device_start_in_flight("camera")


def test_unrelated_running_attempt_with_known_run_is_ignored(reader_module):
    event = accepted(change("operation_runs", 3, {"kind": 4}),
                     change("operation_attempts", 5, {"run_id": 3, "status": 1}))
    assert not reader_module.PreparationReads(Database(), [event]).device_start_in_flight("camera")

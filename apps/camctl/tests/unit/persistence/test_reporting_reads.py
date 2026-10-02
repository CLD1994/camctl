"""报告读取函数负责关闭单行及流式查询游标。"""

from enum import IntEnum
from importlib.util import find_spec, module_from_spec
import sqlite3
import sys
from unittest.mock import create_autospec

import pytest

from camctl.contracts import enums
from camctl.contracts.history_values import HistoryBoundary, TransactionRange
from camctl.contracts.values import ConsistencyError
from camctl.reporting.ack import AckReport
from camctl.persistence.transaction import saved_transaction_events
from camctl.history.events import load_event_registry


@pytest.fixture
def repository(monkeypatch):
    # 解释器加载源码前隔离登记读取；独立模型实例及缓存随夹具恢复。
    status = IntEnum("reports.status", {"REGISTERED": 1, "PREPARED": 2})
    authority_registry_reader = enums.load_registry
    monkeypatch.setattr(enums, "enum_for", create_autospec(enums.enum_for, return_value=status))
    monkeypatch.setattr(enums, "load_registry", create_autospec(enums.load_registry,
        return_value={"history_objects": {}}))
    monkeypatch.setattr(enums, "resource_bytes", create_autospec(enums.resource_bytes,
        side_effect=AssertionError("单元测试不能读取真实包资源")))
    spec = find_spec("camctl.reporting.models")
    models = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "camctl.reporting.models", models)
    spec.loader.exec_module(models)
    spec = find_spec("camctl.persistence.repositories.reporting")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "load_event_registry", create_autospec(module.load_event_registry,
        return_value={"events": {"REPORT_CHANGED": {"id": 28, "branches": {"PUBLISH": {"reason": 4}}}}}))
    monkeypatch.setattr(module, "load_enum_registry", create_autospec(authority_registry_reader,
        return_value={"history_objects": {"report": {"id": 6}}}))
    facts = {"id": 7, "frozen_event_id": 0, "from_wm": 0, "to_wm": 0, "format_version": 1,
             "status": 1, "size_bytes": None, "sha256": None, "publication_count": 0,
             "last_published_event_id": None, "last_error_json": None,
             "created_event_id": 1, "last_event_id": 1}
    monkeypatch.setattr(module, "row_facts", create_autospec(module.row_facts, return_value=facts))
    monkeypatch.setattr(module, "validate_report_management", create_autospec(module.validate_report_management,
        return_value=None))
    return module


@pytest.fixture
def database():
    connection = create_autospec(sqlite3.Connection, instance=True)
    data = {"cursors": [], "fault": None, "error": sqlite3.OperationalError("报告读取不可用"),
            "state": (0, None), "definition": (0, 0, 0, 1, 1), "boundary": (1, 2), "boundary_exists": (1,),
            "watermark": (None,), "candidate": (7,), "published": None, "created": (1,), "syncs": []}
    data["first_bytes"] = (3, "a" * 32)
    data["business"] = (1,)

    def execute(sql, parameters=()):
        if sql.startswith("SELECT link.event_id, txn.operation_key"):
            tag = "first_bytes"
        elif sql.startswith("SELECT acknowledged_wm"):
            tag = "state"
        elif sql.startswith("SELECT from_wm"):
            tag = "definition"
        elif sql.startswith("SELECT MAX(change_seq)"):
            tag = "watermark"
        elif sql.startswith("SELECT id FROM reports"):
            tag = "candidate"
        elif sql.startswith("SELECT event.id, event.event_type"):
            tag = "published"
        elif sql.startswith("SELECT created_event_id"):
            tag = "created"
        elif sql.startswith("SELECT id, action_id, from_wm"):
            tag = "syncs"
        elif sql.startswith("SELECT id, last_event_id"):
            tag = "boundary"
        elif sql.startswith("SELECT id FROM history_transactions"):
            tag = "boundary_exists"
        elif sql.startswith("SELECT event.id FROM history_events"):
            tag = "business"
        else:
            raise AssertionError(f"未声明的读取: {sql}")
        if data["fault"] == "execute":
            raise data["error"]
        cursor = create_autospec(sqlite3.Cursor, instance=True)
        data["cursors"].append(cursor)
        cursor.fetchone.return_value = data[tag]
        cursor.__iter__.return_value = iter(data["syncs"])
        if data["fault"] == "fetch" or data["fault"] == tag:
            cursor.fetchone.side_effect = data["error"]
            cursor.__iter__.side_effect = data["error"]
        return cursor

    connection.execute.side_effect = execute
    return connection, data


def _read(repository, connection, entry):
    if entry == "ack_state":
        return repository.read_ack_state(connection)
    if entry == "ack_report":
        return repository.read_ack_report(connection, 7)
    if entry == "opportunity":
        return repository.read_report_opportunity(connection)
    if entry == "covering":
        return repository.read_covering_report(connection, repository.ReportOpportunity(HistoryBoundary(1, 2), 0, 0), 0)
    if entry == "frozen":
        return repository.read_frozen_report(connection, AckReport(7, 0, 0, 2))
    if entry == "management":
        return repository.read_report_management(connection, 7)
    return list(repository.read_outstanding_syncs(connection))


@pytest.mark.parametrize("entry", ["ack_state", "ack_report", "opportunity", "covering", "frozen", "management", "syncs"])
@pytest.mark.parametrize("fault", [None, "fetch", "execute"])
def test_public_report_reads_release_acquired_cursors(repository, database, entry, fault):
    connection, data = database
    data["fault"] = fault
    if fault is not None:
        with pytest.raises(sqlite3.OperationalError) as raised:
            _read(repository, connection, entry)
        assert raised.value is data["error"]
    else:
        value = _read(repository, connection, entry)
        if entry == "ack_state":
            assert value == (0, None, None)
        elif entry in ("ack_report", "covering"):
            assert value == AckReport(7, 0, 0, 0)
        elif entry == "opportunity":
            assert (value.boundary, value.latest_change_wm, value.acknowledged_wm) == (HistoryBoundary(1, 2), 0, 0)
        elif entry == "frozen":
            assert (value.report_id, value.boundary) == (7, HistoryBoundary(1, 2))
        elif entry == "management":
            assert (value["id"], value["status"], value["publication_count"]) == (7, 1, 0)
        else:
            assert value == []
    for cursor in data["cursors"]:
        cursor.close.assert_called_once_with()


@pytest.mark.parametrize("entry,tag,failed", [
    ("ack_state", "state", True), ("ack_report", "definition", False),
    ("covering", "candidate", False), ("frozen", "boundary", True),
])
def test_report_missing_row_releases_cursor(repository, database, entry, tag, failed):
    connection, data = database
    data[tag] = None
    if failed:
        with pytest.raises(ConsistencyError):
            _read(repository, connection, entry)
    else:
        assert _read(repository, connection, entry) is None
    for cursor in data["cursors"]:
        cursor.close.assert_called_once_with()


@pytest.mark.parametrize("fault", [None, "boundary_exists", "created"])
def test_partial_sync_nested_reads_release_cursors(repository, database, fault):
    connection, data = database
    data["syncs"] = [(1, 2, 0, 2, 1, None, None, 2, 7)]
    data["fault"] = fault
    if fault is None:
        value = _read(repository, connection, "syncs")
        assert len(value) == 1
        assert (value[0][0].sync_id, value[0][0].action_id) == (1, 2)
    else:
        with pytest.raises(sqlite3.OperationalError) as raised:
            _read(repository, connection, "syncs")
        assert raised.value is data["error"]
    for cursor in data["cursors"]:
        cursor.close.assert_called_once_with()


@pytest.mark.parametrize("fault", [None, "boundary_exists", "watermark", "business"])
def test_nonzero_report_boundary_reads_release_cursors(repository, database, fault):
    connection, data = database
    data.update(definition=(1, 2, 4, 1, 5), boundary_exists=(2,), watermark=(2,), fault=fault)
    if fault is None:
        assert repository.read_ack_report(connection, 7) == AckReport(7, 1, 2, 4)
    else:
        with pytest.raises(sqlite3.OperationalError) as raised:
            repository.read_ack_report(connection, 7)
        assert raised.value is data["error"]
    for cursor in data["cursors"]:
        cursor.close.assert_called_once_with()


def test_report_opportunity_closes_retained_sync_iterator_on_range_error(repository, database, monkeypatch):
    connection, data = database
    data["syncs"] = [(1, 2, 1, 2, 1, None, None, 1, None)]
    pending = repository.read_outstanding_syncs(connection)
    monkeypatch.setattr(repository, "read_outstanding_syncs", create_autospec(
        repository.read_outstanding_syncs, return_value=pending))
    with pytest.raises(ConsistencyError):
        repository.read_report_opportunity(connection)
    for cursor in data["cursors"]:
        cursor.close.assert_called_once_with()


def test_report_management_closes_published_lookup_before_fact_error(repository, database):
    connection, data = database
    data["published"] = (2, 28, 1, "{}")
    with pytest.raises(ConsistencyError):
        repository.read_report_management(connection, 7)
    for cursor in data["cursors"]:
        cursor.close.assert_called_once_with()


@pytest.fixture
def byte_event(repository, monkeypatch):
    event = {"event_id": 3, "transaction": TransactionRange(2, 3, 3),
             "type": 28, "occurred_at": 1, "reason": 2,
             "body": {"reason": 2, "evidence": {}, "rows": [{
                 "table": "reports", "id": 7,
                 "before": {"exists": True, "values": {"status": 1, "size_bytes": None, "sha256": None}},
                 "after": {"exists": True, "values": {"status": 2, "size_bytes": 6, "sha256": "a" * 64}},
             }]}}
    monkeypatch.setattr(repository, "load_event_registry", create_autospec(load_event_registry,
        return_value={"events": {"REPORT_CHANGED": {"id": 28, "branches": {"PREPARE": {"reason": 2}}}}}))
    reader = create_autospec(saved_transaction_events, return_value=[event])
    monkeypatch.setattr(repository, "saved_transaction_events", reader, raising=False)
    return event, reader


@pytest.mark.parametrize("fault", [None, "fetch", "execute", "group"])
def test_first_report_bytes_preserve_value_exception_and_cursor_ownership(repository, database, byte_event, fault):
    connection, data = database
    _, reader = byte_event
    data["fault"] = fault
    if fault == "group":
        reader.side_effect = data["error"]
    if fault is None:
        assert repository.read_report_bytes_at_boundary(connection, 7, 8) == repository.ReportBytes(6, "a" * 64)
    else:
        with pytest.raises(sqlite3.OperationalError) as raised:
            repository.read_report_bytes_at_boundary(connection, 7, 8)
        assert raised.value is data["error"]
    for cursor in data["cursors"]:
        cursor.close.assert_called_once_with()


def test_first_report_bytes_can_be_unknown_at_original_boundary(repository, database, byte_event):
    connection, data = database
    _, reader = byte_event
    data["first_bytes"] = None
    reader.side_effect = AssertionError("没有来源时不能补造历史")
    assert repository.read_report_bytes_at_boundary(connection, 7, 2) is None
    for cursor in data["cursors"]:
        cursor.close.assert_called_once_with()


@pytest.mark.parametrize("invalid", ["group_missing", "event_id", "phase", "row_id", "creation",
                                    "missing_size", "known_before", "wrong_stage", "after_boundary"])
def test_first_report_bytes_reject_unreliable_source(repository, database, byte_event, invalid):
    connection, data = database
    event, reader = byte_event
    change = event["body"]["rows"][0]
    if invalid == "group_missing":
        reader.return_value = None
    elif invalid == "event_id":
        event["event_id"] = 4
    elif invalid == "phase":
        event["reason"] = 3
    elif invalid == "row_id":
        change["id"] = 8
    elif invalid == "creation":
        change["before"]["exists"] = False
    elif invalid == "missing_size":
        del change["after"]["values"]["size_bytes"]
    elif invalid == "known_before":
        change["before"]["values"].update(size_bytes=6, sha256="a" * 64)
    elif invalid == "wrong_stage":
        change["after"]["values"]["status"] = 3
    else:
        event["transaction"] = TransactionRange(2, 3, 9)
    with pytest.raises(ConsistencyError):
        repository.read_report_bytes_at_boundary(connection, 7, 8)
    for cursor in data["cursors"]:
        cursor.close.assert_called_once_with()

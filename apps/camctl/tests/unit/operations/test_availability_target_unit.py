"""可用性目标的固定事实分类不读取包资源或数据库。"""

from copy import deepcopy
from importlib.util import find_spec, module_from_spec, spec_from_file_location
import sys
from types import ModuleType, SimpleNamespace

import pytest

from camctl import resources
from camctl.contracts import enums


@pytest.fixture
def target_rules(monkeypatch):
    monkeypatch.setattr(resources, "resource_bytes", lambda name: pytest.fail("单元测试不能读取包资源"))
    definitions = ModuleType("camctl.devices.drivers.hasselblad.definitions")
    definitions.DRIVER_ID = "hasselblad_cfv100c"
    monkeypatch.setitem(sys.modules, definitions.__name__, definitions)
    members = {
        "actions.status": SimpleNamespace(FAILED=4),
        "device_activities.activity_state": SimpleNamespace(UNKNOWN=1),
        "device_activities.occupancy_state": SimpleNamespace(HELD=1),
        "device_activities.ownership_mode": SimpleNamespace(TASK_SCOPE=1),
        "device_activities.dispatch_state": SimpleNamespace(MAY_HAVE_DISPATCHED=2, SUCCESS_RETURNED=3),
    }
    monkeypatch.setattr(enums, "enum_for", lambda column: members[column])
    source = find_spec("camctl.persistence.repositories.availability_target")
    spec = spec_from_file_location("camctl.persistence.repositories._isolated_target", source.origin)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def facts(target_rules):
    identity = target_rules.CameraIdentity("CAMERA-1", 10)
    check = {"action_id": 12, "device_id": "hb0", "driver_id": "hasselblad_cfv100c",
             "expected_identity_json": {"serial": "CAMERA-1", "product_id": 10},
             "resource_key": identity.resource_key}
    activity = {"action_id": 7, "activity_state": 1, "occupancy_state": 1,
                "ownership_mode": 1, "dispatch_state": 2, "availability_release_attempt_id": None,
                "expected_identity_json": {"serial": "CAMERA-1", "product_id": 10},
                "resource_key": identity.resource_key}
    action = {"id": 7, "type": 1, "driver_id": "hasselblad_cfv100c", "device_id": "hb0",
              "status": 4, "execution_started": 1, "execution_spec_json": {"output_storage": "host"}}
    return check, activity, action


@pytest.mark.parametrize("dispatch", [2, 3])
def test_old_unknown_photo_can_be_a_target(target_rules, facts, dispatch):
    check, activity, action = facts
    activity["dispatch_state"] = dispatch
    target_rules.require_target_facts(check, activity, action)


@pytest.mark.parametrize("part,field,value", [
    ("action", "status", 2), ("action", "execution_started", 0),
    ("action", "type", 2), ("action", "driver_id", "other"),
    ("action", "device_id", "other"), ("action", "id", 12),
    ("activity", "action_id", 8), ("activity", "activity_state", 2),
    ("activity", "activity_state", 3), ("activity", "occupancy_state", 2),
    ("activity", "ownership_mode", 2), ("activity", "dispatch_state", 1),
    ("activity", "dispatch_state", 4), ("activity", "availability_release_attempt_id", 99),
    ("activity", "expected_identity_json", None),
    ("activity", "expected_identity_json", {"serial": "CAMERA-2", "product_id": 10}),
    ("activity", "expected_identity_json", {"serial": "CAMERA-1", "product_id": 11}),
    ("activity", "resource_key", "other"), ("check", "resource_key", "other"),
    ("action", "id", None), ("check", "action_id", True),
    ("action", "execution_started", True), ("activity", "activity_state", True),
])
def test_inapplicable_target_is_rejected(target_rules, facts, part, field, value):
    check, activity, action = deepcopy(facts)
    {"check": check, "activity": activity, "action": action}[part][field] = value
    with pytest.raises(ValueError):
        target_rules.require_target_facts(check, activity, action)


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.closed = False

    def fetchmany(self, limit):
        return self.rows[:limit]

    def close(self):
        self.closed = True


class _PagedConnection:
    def __init__(self, rows):
        self.rows = rows
        self.cursors = []
        self.calls = []

    def execute(self, sql, args):
        self.calls.append((sql, args))
        after = args[1] if "FROM actions a" in sql else args[2]
        cursor = _Cursor([row for row in self.rows if row[0] > after])
        self.cursors.append(cursor)
        return cursor


def test_physical_held_read_uses_keyset_pages_and_closes_cursor(target_rules):
    connection = _PagedConnection([(1,), (4,), (7,), (9,), (11,)])
    pages = target_rules.iter_held_activity_ids(connection, "resource", batch_size=2)
    assert next(pages) == (1, 4)
    assert connection.cursors[-1].closed
    assert list(pages) == [(7, 9), (11,)]
    assert [call[1] for call in connection.calls] == [
        ("resource", 1, 0, 2), ("resource", 1, 4, 2), ("resource", 1, 9, 2)]
    assert all(cursor.closed for cursor in connection.cursors)


def test_logical_held_read_keeps_action_order_and_does_not_filter_terminal_actions(target_rules):
    connection = _PagedConnection([(3, 10), (5, 8), (9, 12)])
    assert list(target_rules.iter_logical_held_activities(connection, "hb0", batch_size=2)) == [
        ((3, 10), (5, 8)), ((9, 12),)]
    assert [call[1] for call in connection.calls] == [("hb0", 0, 1, 2), ("hb0", 5, 1, 2)]
    assert all("a.status" not in sql for sql, _ in connection.calls)
    assert all(cursor.closed for cursor in connection.cursors)


@pytest.mark.parametrize("batch", [0, 129, True, 1.0])
def test_target_reads_reject_invalid_batch_size(target_rules, batch):
    connection = _PagedConnection([])
    for reader in (target_rules.iter_held_activity_ids, target_rules.iter_logical_held_activities):
        with pytest.raises(ValueError):
            list(reader(connection, "identity", batch_size=batch))
    assert not connection.calls


def test_target_reads_reject_nonincreasing_database_rows(target_rules):
    connection = _PagedConnection([(4,), (4,)])
    with pytest.raises(ValueError):
        list(target_rules.iter_held_activity_ids(connection, "resource", batch_size=2))
    assert connection.cursors[0].closed

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
        "operation_attempts.status": SimpleNamespace(RUNNING=1),
        "operation_runs.status": SimpleNamespace(PENDING=1, ACTIVE=2),
        "operation_runs.kind": SimpleNamespace(QUERY_ACTIVITY=6),
        "operation_runs.query_purpose": SimpleNamespace(START_CONFIRMATION=2),
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
    activity = {"id": 25, "action_id": 7, "activity_state": 1, "occupancy_state": 1,
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


@pytest.mark.parametrize("part,field", [
    ("check", "action_id"), ("check", "device_id"), ("check", "resource_key"),
    ("activity", "id"), ("activity", "availability_release_attempt_id"),
    ("activity", "expected_identity_json"), ("action", "execution_spec_json"),
])
def test_target_requires_original_fields_even_when_their_value_may_be_null(target_rules, facts, part, field):
    check, activity, action = deepcopy(facts)
    del {"check": check, "activity": activity, "action": action}[part][field]
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

    def fetchone(self):
        return self.rows[0] if self.rows else None


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


class _CreationConnection(_PagedConnection):
    def __init__(self, activity_id, fault=None):
        super().__init__([])
        self.activity_id = activity_id
        self.fault = fault

    def execute(self, sql, args):
        self.calls.append((sql, args))
        if "FROM actions a" in sql:
            rows = [(7, self.activity_id)] if args[1] == 0 else []
        elif "FROM device_activities WHERE" in sql:
            rows = [(self.activity_id,)] if args[2] == 0 else []
        elif "FROM operation_attempts" in sql:
            rows = [(1,)] if self.fault in (("photo",) if "json_each" in sql else ("in_flight", "existing_in_flight")) else []
        elif "FROM operation_runs WHERE" in sql:
            rows = [(1,)] if self.fault == "confirmation" else []
        elif "FROM device_availability_targets" in sql:
            rows = [(1,)] if self.fault in ("existing", "existing_in_flight") else []
        else:
            raise AssertionError(sql)
        cursor = _Cursor(rows)
        self.cursors.append(cursor)
        return cursor


@pytest.fixture
def creation_facts(facts, monkeypatch):
    check, activity, action = deepcopy(facts)
    activity["id"] = 25
    rows = {("device_activities", 25): activity, ("actions", 7): action}
    transaction = ModuleType("camctl.persistence.transaction")
    transaction.row_facts = lambda connection, table, identity: rows.get((table, identity))
    monkeypatch.setitem(sys.modules, transaction.__name__, transaction)
    capture_facts = ModuleType("camctl.persistence.repositories.capture_facts")
    capture_facts.load_start_facts = lambda connection, action: SimpleNamespace(
        activity=activity, run={"id": 31, "status": 4}, attempts_used=1, not_started=False, preparation_resolved=True)
    monkeypatch.setitem(sys.modules, capture_facts.__name__, capture_facts)
    return check, activity


def test_creation_targets_read_original_facts(target_rules, creation_facts):
    check, _ = creation_facts
    connection = _CreationConnection(25)
    assert list(target_rules.iter_creation_targets(connection, check, batch_size=2)) == [(25,)]
    assert all(cursor.closed for cursor in connection.cursors)


@pytest.mark.parametrize("fault", ["in_flight", "confirmation", "photo"])
def test_creation_targets_wait_for_original_responsibility(target_rules, creation_facts, fault):
    check, _ = creation_facts
    connection = _CreationConnection(25, fault)
    with pytest.raises(target_rules.AvailabilityTargetBlocked):
        list(target_rules.iter_creation_targets(connection, check, batch_size=2))
    assert all(cursor.closed for cursor in connection.cursors)


def test_creation_targets_identify_the_existing_responsibility(target_rules, creation_facts):
    check, _ = creation_facts
    with pytest.raises(target_rules.AvailabilityTargetReuse) as caught:
        list(target_rules.iter_creation_targets(_CreationConnection(25, "existing"), check, batch_size=2))
    assert caught.value.check_id == 1


def test_existing_check_is_reused_before_classifying_unsettled_original_call(target_rules, creation_facts):
    check, _ = creation_facts
    with pytest.raises(target_rules.AvailabilityTargetReuse) as caught:
        list(target_rules.iter_creation_targets(_CreationConnection(25, "existing_in_flight"), check, batch_size=2))
    assert caught.value.check_id == 1


def test_creation_targets_reject_logical_identity_mismatch(target_rules, creation_facts):
    check, activity = creation_facts
    activity["resource_key"] = "other"
    with pytest.raises(target_rules.AvailabilityTargetIdentityMismatch):
        list(target_rules.iter_creation_targets(_CreationConnection(25), check, batch_size=2))


def test_creation_targets_report_no_old_restriction(target_rules, creation_facts):
    check, _ = creation_facts
    with pytest.raises(target_rules.AvailabilityTargetNotRequired):
        list(target_rules.iter_creation_targets(_PagedConnection([]), check, batch_size=2))


def _target_page(event_id, activity_ids, *, check_id=31, action_id=12):
    rows = tuple(SimpleNamespace(
        table="device_availability_targets", row_id=100 + index,
        before=SimpleNamespace(exists=False),
        after=SimpleNamespace(exists=True, values={"check_id": check_id, "action_id": action_id,
                                                   "activity_id": activity_id}),
    ) for index, activity_id in enumerate(activity_ids))
    return SimpleNamespace(event_id=event_id, transaction_id=2, event_type=39, reason=1, rows=rows)


@pytest.fixture
def target_group(target_rules, monkeypatch):
    monkeypatch.setattr(target_rules, "iter_creation_targets", lambda *args, **kwargs: iter(((4, 7), (9,))))
    monkeypatch.setattr(target_rules, "load_event_registry", lambda: {"events": {
        "AVAILABILITY_CHECK": {"id": 39, "branches": {"CREATE": {"reason": 1}}}}})
    transaction = SimpleNamespace(txn_id=2, first_event_id=1, last_event_id=3)
    return target_rules.AvailabilityCreationTargets(object(), {"id": 31, "action_id": 12}, transaction, batch_size=2)


def test_target_group_seals_only_the_complete_original_pages(target_group):
    for event in (_target_page(2, (4, 7)), _target_page(3, (9,))):
        target_group.prepare(event)
        target_group.seal(SimpleNamespace(envelope=event))
    target_group.finish()


def test_target_group_rejects_partial_target_set(target_group, target_rules):
    event = _target_page(2, (4, 7))
    target_group.prepare(event)
    target_group.seal(SimpleNamespace(envelope=event))
    with pytest.raises(target_rules.AvailabilityTargetStateError):
        target_group.finish()


@pytest.mark.parametrize("event", [
    _target_page(2, (7, 4)), _target_page(2, (4, 8)),
    _target_page(2, (4, 7), check_id=32), _target_page(2, (4, 7), action_id=13),
])
def test_target_group_rejects_forged_page(target_group, target_rules, event):
    with pytest.raises(target_rules.AvailabilityTargetStateError):
        target_group.prepare(event)


def test_target_group_requires_the_same_validated_event(target_group, target_rules):
    target_group.prepare(_target_page(2, (4, 7)))
    with pytest.raises(target_rules.AvailabilityTargetStateError):
        target_group.seal(SimpleNamespace(envelope=_target_page(3, (4, 7))))


def test_target_group_rejects_unsealed_page(target_group, target_rules):
    target_group.prepare(_target_page(2, (4, 7)))
    with pytest.raises(target_rules.AvailabilityTargetStateError):
        target_group.finish()


def test_target_group_rejects_another_event_branch(target_group, target_rules):
    event = _target_page(2, (4, 7))
    event.reason = 2
    with pytest.raises(target_rules.AvailabilityTargetStateError):
        target_group.prepare(event)

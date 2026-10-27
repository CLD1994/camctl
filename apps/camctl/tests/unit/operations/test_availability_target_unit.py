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
        "actions.status": SimpleNamespace(PENDING=1, RUNNING=2, FAILED=4),
        "actions.type": SimpleNamespace(OBTAIN_ACTION_OUTPUTS=4),
        "device_activities.activity_state": SimpleNamespace(UNKNOWN=1),
        "device_activities.occupancy_state": SimpleNamespace(HELD=1, RELEASED=2),
        "device_activities.ownership_mode": SimpleNamespace(TASK_SCOPE=1),
        "device_activities.dispatch_state": SimpleNamespace(MAY_HAVE_DISPATCHED=2, SUCCESS_RETURNED=3),
        "operation_attempts.status": SimpleNamespace(RUNNING=1, SUCCEEDED=2, FAILED=3, UNKNOWN=4),
        "operation_runs.status": SimpleNamespace(PENDING=1, ACTIVE=2, SUCCEEDED=3, FAILED=4, UNCONFIRMED=6),
        "operation_runs.kind": SimpleNamespace(START=1, QUERY_ACTIVITY=6, CHECK_DEVICE_AVAILABILITY=12),
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
        after = args[1] if "FROM actions" in sql else args[2]
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


@pytest.fixture
def creation_module(target_rules, monkeypatch):
    transaction = ModuleType("camctl.persistence.transaction")
    for name in ("BoundedCommandPlan", "CommandPlan", "commit_operation", "event_envelope", "next_row_id",
                 "row_change", "update_change", "row_facts", "read_saved_transaction_page", "read_transaction_range"):
        setattr(transaction, name, object())
    monkeypatch.setitem(sys.modules, transaction.__name__, transaction)
    validators = ModuleType("camctl.history.validators")
    validators.EventContext = object
    validators.register_guard = lambda *args: None
    monkeypatch.setitem(sys.modules, validators.__name__, validators)
    monkeypatch.setitem(sys.modules, "camctl.persistence.repositories.availability_target", target_rules)
    source = find_spec("camctl.persistence.repositories.availability_create")
    spec = spec_from_file_location("camctl.persistence.repositories._isolated_create", source.origin)
    module = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_creation_request_rejects_invalid_budget_type(creation_module):
    from decimal import Decimal
    identity = creation_module.CameraIdentity("CAMERA-1", 10)
    for batch in (True, 0, 129):
        with pytest.raises(ValueError):
            creation_module.CreateAvailabilityCheck(12, identity, 10, Decimal("1"), 10, batch)


def test_creation_group_requires_all_events_before_final_seal(creation_module, monkeypatch):
    from dataclasses import dataclass
    @dataclass(frozen=True)
    class Event:
        event_id: int
        transaction_id: int = 2
        change_seq: int | None = None
        evidence: object = None
        rows: tuple = ()
    events = (Event(1, evidence={"availability_create_request": {}}), Event(2))
    request = SimpleNamespace(batch_size=2, action_id=12)
    monkeypatch.setattr(creation_module, "_request", lambda raw: request)
    monkeypatch.setattr(creation_module, "_head", lambda *args: (None, {"id": 31}, 3, None, 1, 1))
    monkeypatch.setattr(creation_module, "_events", lambda *args: iter(events))
    monkeypatch.setattr(creation_module, "_context", lambda *args: "actual context")
    class Targets:
        def __init__(self, *args, **kwargs):
            self.sealed = False
        def prepare(self, event):
            assert event.event_id == 2
        def seal(self, validated):
            self.sealed = True
        def finish(self):
            assert self.sealed
    monkeypatch.setattr(creation_module, "AvailabilityCreationTargets", Targets)
    transaction = SimpleNamespace(first_event_id=1, last_event_id=2)
    group = creation_module.CreationGroup(object(), events[0], transaction)
    group.bind(events[0], "actual context")
    group.seal(SimpleNamespace(envelope=events[0]))
    with pytest.raises(creation_module.ConsistencyError):
        group.finish()


def test_creation_group_rejects_forged_context(creation_module, monkeypatch):
    from dataclasses import dataclass
    @dataclass(frozen=True)
    class Event:
        event_id: int = 1
        transaction_id: int = 2
        change_seq: int | None = None
        evidence: object = None
    event = Event(evidence={"availability_create_request": {}})
    monkeypatch.setattr(creation_module, "_request", lambda raw: SimpleNamespace(batch_size=2, action_id=12))
    monkeypatch.setattr(creation_module, "_head", lambda *args: (None, {"id": 31}, 3, None, 1, 1))
    monkeypatch.setattr(creation_module, "_events", lambda *args: iter((event,)))
    monkeypatch.setattr(creation_module, "_context", lambda *args: "actual context")
    monkeypatch.setattr(creation_module, "AvailabilityCreationTargets", lambda *args, **kwargs: object())
    group = creation_module.CreationGroup(object(), event, SimpleNamespace(first_event_id=1, last_event_id=2))
    with pytest.raises(creation_module.ConsistencyError):
        group.bind(event, "forged context")


def test_creation_guard_rejects_missing_kernel_group(creation_module):
    with pytest.raises(creation_module.ConsistencyError):
        creation_module.availability_guard(object(), SimpleNamespace(availability_reads=None))


def test_device_trigger_fixes_original_copy_and_camera(creation_module, monkeypatch):
    from decimal import Decimal
    identity = creation_module.CameraIdentity("CAMERA-1", 10)
    request = creation_module.CreateAvailabilityCheck(12, identity, 10, Decimal("1"), 10,
                                                       trigger_copy_id=8)
    rows = {
        ("actions", 12): {"id": 12, "type": 4, "status": 2, "cancel_requested": 0},
        ("file_copies", 8): {"id": 8, "delivery_id": 11, "source_device_file_id": 15, "slot_device_id": None},
        ("deliveries", 11): {"action_id": 12, "output_id": 21},
        ("device_files", 15): {"id": 15, "identity_key": "key", "locator_json": {},
                                "source_action_id": 7, "observer_action_id": 7},
        ("outputs", 21): {"device_file_id": 15, "source_action_id": 7},
        ("actions", 7): {"id": 7, "device_id": "hb0", "driver_id": "hasselblad_cfv100c"},
    }
    monkeypatch.setattr(creation_module, "row_facts", lambda connection, table, row_id: rows.get((table, row_id)))
    monkeypatch.setattr(creation_module, "_one", lambda *args: None)
    monkeypatch.setattr(creation_module, "iter_creation_targets", lambda *args, **kwargs: iter(((25,),)))
    files = ModuleType("camctl.devices.drivers.hasselblad.files")
    fixed = SimpleNamespace(binding=SimpleNamespace(device_id="hb0", driver_id="hasselblad_cfv100c"),
                            serial="CAMERA-1", product_id=10)
    files.CameraFileIdentity = SimpleNamespace(from_source=lambda *args: fixed)
    monkeypatch.setitem(sys.modules, files.__name__, files)
    discovery = ModuleType("camctl.outputs.read_discovery")
    discovery.first_eligible_read = lambda *args: 8
    monkeypatch.setitem(sys.modules, discovery.__name__, discovery)
    preparation = ModuleType("camctl.devices.capture_preparation")
    preparation.CAPTURE_ACTION_TYPES = (1, 2, 3)
    monkeypatch.setitem(sys.modules, preparation.__name__, preparation)
    recovery = ModuleType("camctl.capture.software_recovery")
    recovery.recovery_locked = lambda *args: False
    monkeypatch.setitem(sys.modules, recovery.__name__, recovery)
    resources = ModuleType("camctl.scheduling.resources")
    resources.device_read_in_flight = lambda *args: False
    resources.device_start_in_flight = lambda *args: False
    monkeypatch.setitem(sys.modules, resources.__name__, resources)
    alias = ModuleType("camctl.persistence.repositories.physical_alias")
    alias.physical_alias_conflict = lambda *args: False
    monkeypatch.setitem(sys.modules, alias.__name__, alias)
    connection = _PagedConnection([])
    _, check, count = creation_module._qualify(connection, request, 99)
    assert (check["action_id"], check["trigger_copy_id"], check["device_id"], count) == (12, 8, "hb0", 1)
    fixed.serial = "OTHER"
    with pytest.raises(creation_module.AvailabilityTargetIdentityMismatch):
        creation_module._qualify(connection, request, 99)


@pytest.fixture
def release_facts(target_rules, facts, monkeypatch):
    check, first, action = deepcopy(facts)
    check["id"] = 31
    second = deepcopy(first)
    second["id"], second["action_id"] = 26, 8
    second_action = deepcopy(action)
    second_action["id"] = 8
    rows = {("device_activities", 25): first, ("actions", 7): action,
            ("device_activities", 26): second, ("actions", 8): second_action}
    transaction = ModuleType("camctl.persistence.transaction")
    transaction.row_facts = lambda connection, table, identity: rows.get((table, identity))
    monkeypatch.setitem(sys.modules, transaction.__name__, transaction)
    monkeypatch.setattr(target_rules, "iter_fixed_targets", lambda *args, **kwargs:
                        iter((((101, 12, 25),), ((102, 12, 26),))))
    monkeypatch.setattr(target_rules, "require_original_settled", lambda *args: None)
    return check, first, second


def test_release_skips_targets_already_released_by_another_path(target_rules, release_facts):
    check, first, _ = release_facts
    first["occupancy_state"] = 2
    assert list(target_rules.iter_release_targets(object(), check, batch_size=1)) == [(26,)]


def test_release_rejects_the_whole_batch_before_yielding_when_later_target_is_ineligible(target_rules, release_facts):
    check, _, second = release_facts
    second["activity_state"] = 2
    targets = target_rules.iter_release_targets(object(), check, batch_size=1)
    with pytest.raises(target_rules.AvailabilityTargetBlocked):
        next(targets)


def test_all_targets_previously_released_needs_no_release_page(target_rules, release_facts):
    check, first, second = release_facts
    first["occupancy_state"] = second["occupancy_state"] = 2
    assert list(target_rules.iter_release_targets(object(), check, batch_size=1)) == []


@pytest.fixture
def release_module(creation_module, monkeypatch):
    attempts = ModuleType("camctl.operations.attempts")
    attempts.RunFinish = object
    attempts.RunOutcome = object
    monkeypatch.setitem(sys.modules, attempts.__name__, attempts)
    models = ModuleType("camctl.operations.models")
    models.ErrorValue = object
    monkeypatch.setitem(sys.modules, models.__name__, models)
    observation = ModuleType("camctl.persistence.repositories.availability_observation")
    observation.require_saved_availability = lambda *args: None
    monkeypatch.setitem(sys.modules, observation.__name__, observation)
    source = find_spec("camctl.persistence.repositories.availability_release")
    spec = spec_from_file_location("camctl.persistence.repositories._isolated_release", source.origin)
    module = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def release_group(release_module, monkeypatch):
    from dataclasses import dataclass
    @dataclass(frozen=True)
    class Event:
        event_id: int
        event_type: int
        reason: int
        rows: tuple
        transaction_id: int = 2
        change_seq: int | None = None
    def row(table, identity, after):
        return SimpleNamespace(table=table, row_id=identity, before=SimpleNamespace(exists=True),
                               after=SimpleNamespace(values=after))
    events = (Event(1, 12, 1, (row("operation_attempts", 5, {}),)),
              Event(2, 10, 3, (row("operation_runs", 3, {"status": 3}),)),
              Event(3, 39, 2, (row("device_activities", 25,
                                   {"occupancy_state": 2, "availability_release_attempt_id": 5}),)))
    facts = {("operation_attempts", 5): {"id": 5, "run_id": 3},
             ("operation_runs", 3): {"id": 3, "kind": 12, "status": 2,
                                      "availability_check_id": 31, "action_id": 12,
                                      "attempts_used": 1, "max_attempts_used": 3},
             ("device_availability_checks", 31): {"id": 31, "action_id": 12,
                                                  "trigger_copy_id": None, "device_id": "hb0"}}
    monkeypatch.setattr(release_module, "row_facts", lambda connection, table, identity: facts.get((table, identity)))
    monkeypatch.setattr(release_module, "_no_conflicts", lambda *args: None)
    monkeypatch.setattr(release_module, "iter_release_targets", lambda *args: iter(((25,),)))
    monkeypatch.setattr(release_module, "_release_context", lambda *args: "actual context")
    monkeypatch.setattr(release_module, "load_event_registry", lambda: {"events": {
        "ATTEMPT_RESULT": {"id": 12, "branches": {"SUCCEED": {"reason": 1}}},
        "OPERATION_CONFIGURED": {"id": 10, "branches": {"FINISH": {"reason": 3}}},
        "AVAILABILITY_CHECK": {"id": 39, "branches": {"RELEASE": {"reason": 2}}}}})
    transaction = SimpleNamespace(txn_id=2, first_event_id=1, last_event_id=3)
    return release_module.ReleaseGroup(object(), events[0], transaction), events


def test_release_group_seals_result_finish_and_all_target_pages(release_group):
    group, events = release_group
    for event in events:
        group.bind(event, "actual context")
        group.seal(SimpleNamespace(envelope=event))
    group.finish()


def test_single_pass_event_without_kernel_group_is_rejected(release_module, release_group):
    _, events = release_group
    with pytest.raises(release_module.ConsistencyError):
        release_module.require_result_group(events[0], SimpleNamespace(availability_reads=None))


def test_pass_event_is_accepted_only_while_bound_to_complete_group(release_module, release_group):
    group, events = release_group
    group.bind(events[0], "actual context")
    release_module.require_result_group(events[0], SimpleNamespace(availability_reads=group))
    for event in events:
        if event is not events[0]:
            group.bind(event, "actual context")
        group.seal(SimpleNamespace(envelope=event))
    group.finish()


def test_blocked_pass_group_saves_failure_without_releasing_targets(release_module, release_group, monkeypatch):
    from dataclasses import replace
    _, events = release_group
    def blocked(*args):
        raise release_module.AvailabilityTargetBlocked("busy")
    monkeypatch.setattr(release_module, "_no_conflicts", blocked)
    transaction = SimpleNamespace(txn_id=2, first_event_id=1, last_event_id=2)
    group = release_module.ReleaseGroup(object(), events[0], transaction)
    error = {"code": "device_availability_unconfirmed", "stage": "execution", "details": {
        "device_id": "hb0", "operation_run_id": "3", "attempts_used": 1, "max_attempts": 3}}
    row = SimpleNamespace(table="operation_runs", row_id=3,
                          after=SimpleNamespace(values={"status": 4, "error_json": error}))
    failed = replace(events[1], rows=(row,))
    for event in (events[0], failed):
        group.bind(event, "actual context")
        group.seal(SimpleNamespace(envelope=event))
    group.finish()


def test_blocked_complete_pass_cannot_be_labeled_unconfirmed(release_module, release_group, monkeypatch):
    from dataclasses import replace
    _, events = release_group
    def blocked(*args):
        raise release_module.AvailabilityTargetBlocked("busy")
    monkeypatch.setattr(release_module, "_no_conflicts", blocked)
    group = release_module.ReleaseGroup(object(), events[0],
        SimpleNamespace(txn_id=2, first_event_id=1, last_event_id=2))
    group.bind(events[0], "actual context")
    group.seal(SimpleNamespace(envelope=events[0]))
    row = SimpleNamespace(table="operation_runs", row_id=3,
                          after=SimpleNamespace(values={"status": 6, "error_json": {}}))
    with pytest.raises(release_module.ConsistencyError):
        group.bind(replace(events[1], rows=(row,)), "actual context")


def test_eligible_pass_cannot_be_disguised_as_failed_without_release(release_module, release_group):
    from dataclasses import replace
    group, events = release_group
    group.bind(events[0], "actual context")
    group.seal(SimpleNamespace(envelope=events[0]))
    row = SimpleNamespace(table="operation_runs", row_id=3,
                          after=SimpleNamespace(values={"status": 4, "error_json": {}}))
    with pytest.raises(release_module.ConsistencyError):
        group.bind(replace(events[1], rows=(row,)), "actual context")


def test_late_pass_keeps_an_already_terminal_check_and_original_result(release_module, release_group, monkeypatch):
    _, events = release_group
    original = release_module.row_facts
    monkeypatch.setattr(release_module, "row_facts", lambda connection, table, identity:
                        {**original(connection, table, identity), "status": 4}
                        if table == "operation_runs" else original(connection, table, identity))
    def unexpected(*args):
        pytest.fail("终态后的原结果交付不能等待其他候选")
    monkeypatch.setattr(release_module, "_no_conflicts", unexpected)
    transaction = SimpleNamespace(txn_id=2, first_event_id=1, last_event_id=1)
    group = release_module.ReleaseGroup(object(), events[0], transaction)
    group.bind(events[0], "actual context")
    release_module.require_result_group(events[0], SimpleNamespace(availability_reads=group))
    group.seal(SimpleNamespace(envelope=events[0]))
    group.finish()


@pytest.fixture
def finish_reads(target_rules, monkeypatch):
    source = find_spec("camctl.persistence.repositories.availability_finish")
    spec = spec_from_file_location("camctl.persistence.repositories._isolated_finish", source.origin)
    module = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "load_event_registry", lambda: {"events": {"ATTEMPT_RESULT": {"id": 12}}})
    return module


class _PendingAttemptConnection:
    def execute(self, sql, args):
        return _Cursor([(5,)] if args[1] == 0 else [])


def test_terminal_check_cannot_overtake_the_original_in_flight_attempt(finish_reads):
    reads = finish_reads.AvailabilityFinishReads(_PendingAttemptConnection())
    with pytest.raises(finish_reads.ConsistencyError):
        reads.require_settled(3)


def test_terminal_check_accepts_only_already_validated_original_result(finish_reads):
    row = SimpleNamespace(table="operation_attempts", row_id=5, before=SimpleNamespace(exists=True),
        after=SimpleNamespace(exists=True, values={"status": 3, "result_event_id": 7, "result_json": {}}))
    validated = SimpleNamespace(envelope=SimpleNamespace(event_id=7, event_type=12, rows=(row,)))
    reads = finish_reads.AvailabilityFinishReads(_PendingAttemptConnection(), (validated,))
    reads.require_settled(3)


def test_terminal_check_does_not_accept_unrelated_result_as_settlement(finish_reads):
    row = SimpleNamespace(table="operation_attempts", row_id=6, before=SimpleNamespace(exists=True),
        after=SimpleNamespace(exists=True, values={"status": 3, "result_event_id": 7, "result_json": {}}))
    validated = SimpleNamespace(envelope=SimpleNamespace(event_id=7, event_type=12, rows=(row,)))
    reads = finish_reads.AvailabilityFinishReads(_PendingAttemptConnection(), (validated,))
    with pytest.raises(finish_reads.ConsistencyError):
        reads.require_settled(3)


@pytest.fixture
def isolated_result_guard(target_rules, monkeypatch):
    from enum import Enum
    recovery = ModuleType("camctl.capture.software_recovery")
    recovery.intent_recovery_locked = lambda *args: False
    monkeypatch.setitem(sys.modules, recovery.__name__, recovery)
    validators = ModuleType("camctl.history.validators")
    validators.EventValidationError = type("EventValidationError", (ValueError,), {})
    validators.register_guard = lambda *args: None
    monkeypatch.setitem(sys.modules, validators.__name__, validators)
    attempts = ModuleType("camctl.operations.attempts")
    class Kind(Enum):
        START = "start"
        STOP = "stop"
        CHECK_CAPTURE_RESULTS = "results"
        PREPARE_CAPTURE = "prepare"
        RESOLVE_OUTPUT_SCOPE = "scope"
    class Purpose(Enum):
        START_CONFIRMATION = "start"
        STOP_CONFIRMATION = "stop"
        RESIDUAL_STOP_CONFIRMATION = "residual"
    attempts.OperationKind, attempts.QueryPurpose = Kind, Purpose
    for name in ("AttemptFinish", "ReadResumeDecision", "ReadResumeDisposition", "ReadResumeRequest",
                 "AttemptIntent", "AttemptTarget", "BeginAttemptResult", "BeginDisposition", "FinishAttemptResult",
                 "FinishDisposition", "RunStatus", "StaleRunFinish", "StaleRunFinishResult", "responsibility_key",
                 "seconds_from_json", "ticket_target_id", "ticket_target_for_identity", "operation_responsibility_key"):
        setattr(attempts, name, object())
    monkeypatch.setitem(sys.modules, attempts.__name__, attempts)
    models = ModuleType("camctl.operations.models")
    class AttemptStatus(Enum):
        SUCCEEDED = "succeeded"
        FAILED = "failed"
        UNKNOWN = "unknown"
    class Basis(Enum):
        OBSERVED = "observed"
        NOT_DISPATCHED = "not_dispatched"
    models.AttemptStatus, models.SettlementBasis = AttemptStatus, Basis
    models.AttemptTicket = models.ValidatedOutcome = object
    monkeypatch.setitem(sys.modules, models.__name__, models)
    results = ModuleType("camctl.operations.result_format")
    results.result_document = results.read_result_document = results.error_document = object()
    monkeypatch.setitem(sys.modules, results.__name__, results)
    persistence = ModuleType("camctl.persistence.models")
    persistence.DbOutcome = persistence.DbOutcomeKind = object
    monkeypatch.setitem(sys.modules, persistence.__name__, persistence)
    runtime = ModuleType("camctl.persistence.runtime")
    runtime.OwnedConnection = object
    monkeypatch.setitem(sys.modules, runtime.__name__, runtime)
    history = ModuleType("camctl.persistence.row_history")
    history.read_row_values_at_boundary = object()
    monkeypatch.setitem(sys.modules, history.__name__, history)
    scheduling = ModuleType("camctl.scheduling.resources")
    for name in ("baseline_start_reason", "capture_scope_blocked", "device_read_in_flight", "device_start_in_flight"):
        setattr(scheduling, name, object())
    monkeypatch.setitem(sys.modules, scheduling.__name__, scheduling)
    transaction = ModuleType("camctl.persistence.transaction")
    for name in ("CommandPlan", "commit_operation", "event_envelope", "next_row_id", "row_change", "row_facts",
                 "saved_transaction_events", "SavedEvent", "update_change"):
        setattr(transaction, name, object())
    transaction.TransactionError = ValueError
    monkeypatch.setitem(sys.modules, transaction.__name__, transaction)
    from camctl.contracts import enums
    enum_values = {
        "operation_runs.kind": SimpleNamespace(START=1, STOP=2, STOP_RESIDUAL=8,
            CHECK_DEVICE_AVAILABILITY=12, PREPARE_CAPTURE=10),
        "operation_runs.status": SimpleNamespace(SUCCEEDED=3, FAILED=4, UNCONFIRMED=6),
        "operation_runs.query_purpose": SimpleNamespace(),
        "operation_attempts.status": SimpleNamespace(RUNNING=1, SUCCEEDED=2, FAILED=3, UNKNOWN=4),
        "operation_attempts.effect_state": SimpleNamespace(NO_EFFECT=2, CONFIRMED=3),
    }
    monkeypatch.setattr(enums, "enum_for", lambda column: enum_values[column])
    source = find_spec("camctl.persistence.repositories.operations")
    spec = spec_from_file_location("camctl.persistence.repositories._isolated_operations_result", source.origin)
    module = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("status", [3, 4])
def test_failed_or_unknown_attempt_does_not_require_a_pass_group(isolated_result_guard, status):
    result = {"format_version": 1, "settlement": {"basis": "observed", "evidence": {
        "type": "returned", "version": 1}}, "observations": []}
    row = SimpleNamespace(table="operation_attempts", row_id=5, before=SimpleNamespace(exists=True),
        after=SimpleNamespace(values={"run_id": 3, "status": status, "effect_state": 2,
                                      "result_event_id": 7, "result_json": result, "error_json": {"code": "failed"}}))
    event = SimpleNamespace(event_id=7, rows=(row,))
    context = SimpleNamespace(state_rows={"operation_runs": {3: {"kind": 12}}}, availability_reads=None)
    isolated_result_guard._attempt_result_guard(event, context)


def test_success_result_guard_rejects_a_forged_single_event(isolated_result_guard, release_module, monkeypatch):
    monkeypatch.setitem(sys.modules, "camctl.persistence.repositories.availability_release", release_module)
    result = {"format_version": 1, "settlement": {"basis": "observed", "evidence": {
        "type": "availability_returned", "version": 1}}, "observations": []}
    row = SimpleNamespace(table="operation_attempts", row_id=5, before=SimpleNamespace(exists=True),
        after=SimpleNamespace(values={"run_id": 3, "status": 2, "effect_state": 2,
                                      "result_event_id": 7, "result_json": result, "error_json": None}))
    event = SimpleNamespace(event_id=7, rows=(row,))
    context = SimpleNamespace(state_rows={"operation_runs": {3: {"kind": 12}}}, availability_reads=None)
    with pytest.raises(isolated_result_guard.EventValidationError):
        isolated_result_guard._attempt_result_guard(event, context)


def test_generic_finish_guard_rejects_terminal_check_with_in_flight_attempt(
        isolated_result_guard, finish_reads, monkeypatch):
    monkeypatch.setitem(sys.modules, "camctl.persistence.repositories.availability_finish", finish_reads)
    row = SimpleNamespace(table="operation_runs", row_id=3, before=SimpleNamespace(exists=True),
        after=SimpleNamespace(values={"status": 4, "retry_wait_required": 0, "error_json": {"code": "failed"}}))
    event = SimpleNamespace(rows=(row,))
    context = SimpleNamespace(state_rows={"operation_runs": {3: {"kind": 12}}},
        availability_finish_reads=finish_reads.AvailabilityFinishReads(_PendingAttemptConnection()))
    with pytest.raises(isolated_result_guard.EventValidationError):
        isolated_result_guard._operation_finish_guard(event, context)


def test_generic_finish_guard_accepts_the_validated_prior_result(
        isolated_result_guard, finish_reads, monkeypatch):
    monkeypatch.setitem(sys.modules, "camctl.persistence.repositories.availability_finish", finish_reads)
    attempt = SimpleNamespace(table="operation_attempts", row_id=5, before=SimpleNamespace(exists=True),
        after=SimpleNamespace(exists=True, values={"status": 3, "result_event_id": 7, "result_json": {}}))
    validated = SimpleNamespace(envelope=SimpleNamespace(event_id=7, event_type=12, rows=(attempt,)))
    row = SimpleNamespace(table="operation_runs", row_id=3, before=SimpleNamespace(exists=True),
        after=SimpleNamespace(values={"status": 4, "retry_wait_required": 0, "error_json": {"code": "failed"}}))
    context = SimpleNamespace(state_rows={"operation_runs": {3: {"kind": 12}}},
        availability_finish_reads=finish_reads.AvailabilityFinishReads(_PendingAttemptConnection(), (validated,)))
    isolated_result_guard._operation_finish_guard(SimpleNamespace(rows=(row,)), context)


@pytest.mark.parametrize("kind", ["START", "READ_FILE"])
def test_ordinary_attempt_guard_independently_rejects_in_flight_check(isolated_result_guard, monkeypatch, kind):
    module = isolated_result_guard
    module._RUN_KIND.RESOLVE_OUTPUT_SCOPE = 11
    module._RUN_KIND.CHECK_CAPTURE_RESULTS = 7
    module._RUN_KIND.READ_FILE = 3
    module._ATTEMPT_STATUS.RUNNING = 1
    module._EFFECT_STATE.UNKNOWN = 1
    module.baseline_start_reason = lambda *args: None
    preparation = ModuleType("camctl.persistence.repositories.preparation")
    class Reads:
        def device_read_in_flight(self, device_id):
            return False
        def device_start_in_flight(self, device_id):
            return False
        def availability_in_flight(self, device_id, identity):
            return True
    preparation.PreparationReads = Reads
    monkeypatch.setitem(sys.modules, preparation.__name__, preparation)
    run_kind = 1 if kind == "START" else 3
    run = {"kind": run_kind, "action_id": 10, "activity_id": 20, "copy_id": 30, "attempts_used": 0}
    attempt = {"run_id": 3, "attempt_no": 1, "intent_event_id": 7, "status": 1, "effect_state": 1,
               "result_event_id": None, "result_json": None, "error_json": None,
               "copy_round": None if kind == "START" else 1}
    row_attempt = SimpleNamespace(table="operation_attempts", row_id=5, before=SimpleNamespace(exists=False),
                                  after=SimpleNamespace(values=attempt))
    row_run = SimpleNamespace(table="operation_runs", row_id=3,
                              before=SimpleNamespace(exists=True, values={"attempts_used": 0}),
                              after=SimpleNamespace(values={"attempts_used": 1}))
    event = SimpleNamespace(event_id=7, rows=(row_attempt, row_run), evidence={})
    state = {"operation_runs": {3: run}, "device_activities": {20: {"id": 20}},
             "actions": {10: {"id": 10, "device_id": "camera", "driver_id": "driver-a"},
                         11: {"id": 11, "device_id": "camera", "driver_id": "driver-a"}},
             "file_copies": {30: {"id": 30, "source_device_file_id": 40,
                                   "source_intermediate_file_id": None, "slot_device_id": "camera", "round": 1}},
             "device_files": {40: {"observer_action_id": 11, "source_action_id": 11}}}
    context = SimpleNamespace(state_rows=state, preparation_reads=Reads())
    with pytest.raises(module.EventValidationError, match="设备检查"):
        module._attempt_intent_guard(event, context)


def test_release_group_rejects_partial_transaction(release_module, release_group):
    group, events = release_group
    for event in events[:2]:
        group.bind(event, "actual context")
        group.seal(SimpleNamespace(envelope=event))
    with pytest.raises(release_module.ConsistencyError):
        group.finish()


def test_release_pages_compact_sparse_target_pages(release_module, monkeypatch):
    monkeypatch.setattr(release_module, "iter_release_targets", lambda *args:
                        iter((tuple(range(1, 65)), tuple(range(65, 129)), (129,))))
    pages = list(release_module._release_pages(object(), {}))
    assert pages == [tuple(range(1, 129)), (129,)]


def test_release_group_rejects_foreign_target(release_module, release_group):
    group, events = release_group
    for event in events[:2]:
        group.bind(event, "actual context")
        group.seal(SimpleNamespace(envelope=event))
    events[2].rows[0].row_id = 26
    with pytest.raises(release_module.ConsistencyError):
        group.bind(events[2], "actual context")


def test_resolved_group_ends_without_an_additional_attempt(release_module, monkeypatch):
    from dataclasses import dataclass
    @dataclass(frozen=True)
    class Event:
        event_id: int
        event_type: int
        reason: int
        rows: tuple
        transaction_id: int = 2
        change_seq: int | None = None
    row = SimpleNamespace(table="operation_runs", row_id=3, after=SimpleNamespace(values={"status": 3}))
    event = Event(1, 10, 3, (row,))
    run = {"id": 3, "kind": 12, "status": 2, "availability_check_id": 31, "action_id": 12}
    check = {"id": 31, "action_id": 12}
    monkeypatch.setattr(release_module, "row_facts", lambda conn, table, identity:
                        run if table == "operation_runs" else check)
    monkeypatch.setattr(release_module, "iter_release_targets", lambda *args: iter(()))
    monkeypatch.setattr(release_module, "_resolved_context", lambda *args: "actual context")
    monkeypatch.setattr(release_module, "load_event_registry", lambda: {"events": {
        "OPERATION_CONFIGURED": {"id": 10, "branches": {"FINISH": {"reason": 3}}}}})
    class Connection:
        def execute(self, *args):
            return _Cursor([])
    group = release_module.ResolvedGroup(Connection(), event,
        SimpleNamespace(txn_id=2, first_event_id=1, last_event_id=1))
    group.bind(event, "actual context")
    group.seal(SimpleNamespace(envelope=event))
    group.finish()


def test_blocked_release_still_saves_the_actual_pass_and_failed_conclusion(release_module, monkeypatch):
    from dataclasses import dataclass
    @dataclass(frozen=True)
    class Finish:
        ticket: object
        outcome: object
        run_finish: object
        retry_wait: bool = False
    success = object()
    result = object()
    outcome = SimpleNamespace(outcome=SimpleNamespace(status=success))
    finish = Finish(SimpleNamespace(run_id=3, attempt_id=1), outcome,
                    SimpleNamespace(status="succeeded"))
    run = {"id": 3, "kind": 12, "status": 2, "attempts_used": 1, "max_attempts_used": 3}
    check = {"id": 31, "device_id": "hb0", "trigger_copy_id": None}
    monkeypatch.setattr(release_module, "row_facts", lambda *args: run)
    monkeypatch.setattr(release_module, "_check", lambda *args: check)
    monkeypatch.setattr(release_module, "iter_release_targets", lambda *args: iter(((25,),)))
    def blocked(*args):
        raise release_module.AvailabilityTargetBlocked("busy")
    monkeypatch.setattr(release_module, "_no_conflicts", blocked)
    monkeypatch.setattr(release_module, "RunOutcome", SimpleNamespace(SUCCEEDED="succeeded", FAILED="failed"))
    monkeypatch.setattr(release_module, "RunFinish", lambda status, error: SimpleNamespace(status=status, error=error))
    monkeypatch.setattr(release_module, "ErrorValue", lambda code, stage, details:
                        SimpleNamespace(code=code, stage=stage, details=details))
    monkeypatch.setattr(sys.modules["camctl.operations.models"], "AttemptStatus", SimpleNamespace(SUCCEEDED=success), raising=False)
    capture = ModuleType("camctl.persistence.repositories.capture")
    capture._CompositeScope = lambda *args: object()
    monkeypatch.setitem(sys.modules, capture.__name__, capture)
    operations = ModuleType("camctl.persistence.repositories.operations")
    captured = []
    class Command:
        def __init__(self, actual, key):
            captured.append(actual)
        def plan(self, scope):
            return SimpleNamespace(read_only=False, events=(object(), object()), result=result)
    operations.FinishAttemptCommand = Command
    monkeypatch.setitem(sys.modules, operations.__name__, operations)
    class Connection:
        def execute(self, sql, args):
            return _Cursor([] if "history_transactions" in sql else [(5,)])
    monkeypatch.setattr(release_module, "BoundedCommandPlan", lambda events, context, value, complete:
                        SimpleNamespace(events=events, result=value))
    scope = SimpleNamespace(connection=Connection(), max_event_id=0, allocate=lambda count: None)
    planned = release_module.FinishAvailabilityCommand(finish, "key").plan(scope)
    assert planned.result is result
    assert len(planned.events) == 2
    assert captured[0].outcome is outcome
    assert captured[0].run_finish.status == "failed"
    assert captured[0].run_finish.error.code == "device_availability_unconfirmed"
    assert captured[0].run_finish.error.details == {"device_id": "hb0", "operation_run_id": "3",
                                                     "attempts_used": 1, "max_attempts": 3}


@pytest.fixture
def intent_guard(creation_module, monkeypatch):
    from dataclasses import dataclass
    @dataclass(frozen=True)
    class Event:
        event_id: int = 7
        transaction_id: int = 2
        change_seq: int | None = None
        occurred_at: int = 10
        evidence: object = None
        rows: tuple = ()
    attempt = SimpleNamespace(table="operation_attempts", before=SimpleNamespace(exists=False),
        after=SimpleNamespace(values={"run_id": 3, "attempt_no": 1, "max_attempts_used": 3,
                                      "timeout_s_json": 5, "retry_interval_s_json": 1}))
    run_row = SimpleNamespace(table="operation_runs", row_id=3, before=SimpleNamespace(exists=True),
        after=SimpleNamespace(values={"attempts_used": 1, "max_attempts_used": 3,
                                      "timeout_s_json": 5, "retry_interval_s_json": 1}))
    event = Event(evidence={"availability_intent_request": {}}, rows=(attempt, run_row))
    run = {"id": 3, "kind": 12, "action_id": 12, "status": 1, "availability_check_id": 31,
           "retry_wait_required": 0, "attempts_used": 0}
    check = {"id": 31, "action_id": 12, "target_boundary_event_id": 2}
    transaction = SimpleNamespace(txn_id=2, first_event_id=7, last_event_id=7)
    request = SimpleNamespace(action_id=12, occurred_at=10)
    create = ModuleType("camctl.persistence.repositories.availability_create")
    create._qualify = lambda *args, **kwargs: None
    create._request = lambda raw: request
    monkeypatch.setitem(sys.modules, create.__name__, create)
    source = find_spec("camctl.persistence.repositories.availability_intent")
    spec = spec_from_file_location("camctl.persistence.repositories._isolated_intent", source.origin)
    module = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "row_facts", lambda connection, table, identity: run if table == "operation_runs" else check)
    context = SimpleNamespace(transaction=transaction, state_rows={"operation_runs": {3: run},
                              "device_availability_checks": {31: check}})
    return module, module.AvailabilityIntentReads(object(), event, transaction), event, context, run


def test_first_check_intent_uses_original_facts(intent_guard):
    _, guard, event, context, _ = intent_guard
    guard.verify(event, context, 3)


def test_check_intent_rejects_different_attempt_and_flow_budgets(intent_guard):
    module, guard, event, context, _ = intent_guard
    event.rows[0].after.values["max_attempts_used"] = 4
    with pytest.raises(module.ConsistencyError):
        guard.verify(event, context, 3)


def test_check_intent_waits_until_the_original_interval_is_cleared(intent_guard):
    module, guard, event, context, run = intent_guard
    run["attempts_used"], run["retry_wait_required"] = 1, 1
    event.rows[0].after.values["attempt_no"] = event.rows[1].after.values["attempts_used"] = 2
    with pytest.raises(module.AvailabilityTargetBlocked):
        guard.verify(event, context, 3)


@pytest.mark.parametrize("completed", [True, False])
def test_check_retry_requires_the_last_attempts_completed_interval(intent_guard, monkeypatch, completed):
    module, guard, event, context, run = intent_guard
    run["attempts_used"] = 1
    event.rows[0].after.values["attempt_no"] = event.rows[1].after.values["attempts_used"] = 2
    reads = ModuleType("camctl.persistence.repositories.retry_wait_reads")
    reads.RetryWaitReads = lambda connection: SimpleNamespace(completed=lambda run_id, used: completed)
    monkeypatch.setitem(sys.modules, reads.__name__, reads)
    if completed:
        guard.verify(event, context, 3)
    else:
        with pytest.raises(module.AvailabilityTargetBlocked):
            guard.verify(event, context, 3)


@pytest.fixture
def retry_module(creation_module, monkeypatch):
    transaction = sys.modules["camctl.persistence.transaction"]
    transaction.saved_transaction_events = object()
    source = find_spec("camctl.persistence.repositories.availability_retry")
    spec = spec_from_file_location("camctl.persistence.repositories._isolated_retry", source.origin)
    module = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    result_format = ModuleType("camctl.operations.result_format")
    result_format.read_result_document = lambda *args: None
    monkeypatch.setitem(sys.modules, result_format.__name__, result_format)
    return module


def test_retry_completion_verifies_original_failed_attempt(retry_module, monkeypatch):
    run = {"kind": 12, "attempts_used": 1, "retry_wait_required": 1, "status": 2}
    attempt = {"run_id": 3, "attempt_no": 1, "status": 3, "result_event_id": 10,
               "result_json": {}, "effect_state": 2, "error_json": {}}
    monkeypatch.setattr(retry_module, "row_facts", lambda conn, table, identity:
                        run if table == "operation_runs" else attempt)
    class Connection:
        def execute(self, sql, args):
            return _Cursor([(1, 1)] if "MAX(attempt_no)" in sql else [])
    assert retry_module._verify(Connection(), 3, 5) == (run, attempt)


def test_retry_completion_rejects_another_attempt(retry_module, monkeypatch):
    run = {"kind": 12, "attempts_used": 2, "retry_wait_required": 1, "status": 2}
    attempt = {"run_id": 3, "attempt_no": 1, "status": 3, "result_event_id": 10, "result_json": {}}
    monkeypatch.setattr(retry_module, "row_facts", lambda conn, table, identity:
                        run if table == "operation_runs" else attempt)
    with pytest.raises(retry_module.ConsistencyError):
        retry_module._verify(object(), 3, 5)


def test_dispatch_recheck_uses_original_ticket_and_excludes_its_own_attempt(intent_guard, monkeypatch):
    module, _, _, _, run = intent_guard
    run.update(responsibility_key="availability/31", status=2, attempts_used=1)
    check = {"id": 31, "action_id": 12, "target_boundary_event_id": 2}
    attempt = {"id": 5, "run_id": 3, "attempt_no": 1, "status": 1, "result_event_id": None}
    monkeypatch.setattr(module, "row_facts", lambda connection, table, identity:
                        {"operation_runs": run, "device_availability_checks": check,
                         "operation_attempts": attempt}[table])
    calls = []
    monkeypatch.setattr(module, "_qualify", lambda *args, **kwargs: calls.append(kwargs))
    models = ModuleType("camctl.operations.models")
    models.AttemptTicket = type("AttemptTicket", (), {})
    monkeypatch.setitem(sys.modules, models.__name__, models)
    scheduling = ModuleType("camctl.scheduling.resources")
    scheduling.DispatchRecheck = lambda allowed, reason: SimpleNamespace(allowed=allowed, reason=reason)
    monkeypatch.setitem(sys.modules, scheduling.__name__, scheduling)
    create = sys.modules["camctl.persistence.repositories.availability_create"]
    create.CreateAvailabilityCheck = type("CreateAvailabilityCheck", (), {})
    ticket = models.AttemptTicket()
    ticket.run_id, ticket.attempt_id, ticket.operation = 3, 1, "availability"
    ticket.responsibility_key, ticket.target_id = "availability/31", "31"
    request = create.CreateAvailabilityCheck()
    request.action_id = 12
    class Connection:
        def execute(self, *args):
            return _Cursor([(5,)])
    result = module.recheck_availability_dispatch(Connection(), ticket, request)
    assert result.allowed and calls == [{"existing_check": check, "exclude_attempt_id": 5}]

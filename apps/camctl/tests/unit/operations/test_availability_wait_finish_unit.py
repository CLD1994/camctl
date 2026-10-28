"""检查等待终结分类，替换枚举、连接和活动目标读取。"""

from importlib.util import find_spec, module_from_spec, spec_from_file_location
from types import ModuleType, SimpleNamespace
import sys

import pytest

from camctl import resources
from camctl.contracts import enums


@pytest.fixture
def finish_module(monkeypatch):
    monkeypatch.setattr(resources, "resource_bytes", lambda name: pytest.fail("不能读取真实包资源"))
    monkeypatch.setattr(enums, "resource_bytes", lambda name: pytest.fail("不能读取真实包资源"))
    kinds = {
        "operation_runs.status": SimpleNamespace(PENDING=1, ACTIVE=2, SUCCEEDED=3, FAILED=4,
                                                  CANCELED=5, UNCONFIRMED=6, EXPIRED=7),
        "operation_attempts.status": SimpleNamespace(RUNNING=1, SUCCEEDED=2, FAILED=3, UNKNOWN=4),
        "actions.status": SimpleNamespace(PENDING=1, RUNNING=2, SUCCEEDED=3, FAILED=4, EXPIRED=5, CANCELED=6),
    }
    monkeypatch.setattr(enums, "enum_for", lambda name: kinds[name])
    history = ModuleType("camctl.history.events")
    history.load_event_registry = lambda: {}
    transaction = ModuleType("camctl.persistence.transaction")
    for name in ("CommandPlan", "commit_operation", "event_envelope", "row_facts",
                 "saved_transaction_events", "update_change"):
        setattr(transaction, name, object())
    target = ModuleType("camctl.persistence.repositories.availability_target")
    target.AvailabilityTargetBlocked = type("AvailabilityTargetBlocked", (ValueError,), {})
    target.iter_release_targets = lambda connection, check: iter([(1,)])
    settled = ModuleType("camctl.persistence.repositories.availability_finish")
    settled.AvailabilityFinishReads = lambda connection: SimpleNamespace(require_settled=lambda run_id: None)
    for item in (history, transaction, target, settled):
        monkeypatch.setitem(sys.modules, item.__name__, item)
    source = find_spec("camctl.persistence.repositories.availability_wait_finish")
    spec = spec_from_file_location("camctl.persistence.repositories._isolated_wait_finish", source.origin)
    loaded = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, loaded)
    spec.loader.exec_module(loaded)
    return loaded


class Cursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row

    def close(self):
        pass


@pytest.mark.parametrize("state,canceled,now,used,maximum,last,expected", [
    (2, 1, 10, 2, 2, 3, 5),
    (1, 0, 12, 2, 2, 3, 7),
    (1, 0, 10, 2, 2, 3, 4),
    (1, 0, 10, 2, 1, 4, 6),
])
def test_wait_finish_classifies_cancel_expiry_and_current_budget(
        finish_module, monkeypatch, state, canceled, now, used, maximum, last, expected):
    run = {"id": 3, "status": 2, "attempts_used": used, "retry_wait_required": 1}
    action = {"status": state, "cancel_requested": canceled, "scheduled_at": 10, "max_delay_ms": 0}
    check = {"id": 9, "trigger_copy_id": None, "device_id": "camera"}
    request = finish_module.WaitingFinishRequest(9, now, 20, maximum)
    attempt = {"status": last, "result_event_id": 42, "result_json": {}}
    monkeypatch.setattr(finish_module, "row_facts", lambda connection, table, identity: attempt)
    connection = SimpleNamespace(execute=lambda *args: Cursor((7,)))
    decision, status, error = finish_module._decision(connection, request, check, run, action)
    assert decision is finish_module.WaitingFinishDisposition.FINISHED and status == expected
    if expected in (4, 6):
        assert error["details"] == {"device_id": "camera", "operation_run_id": "3",
                                    "attempts_used": used, "max_attempts": maximum}
    else:
        assert error is None
    assert run["retry_wait_required"] == 1


def test_released_targets_finish_success_through_existing_resolve(finish_module, monkeypatch):
    monkeypatch.setattr(finish_module, "iter_release_targets", lambda *args: iter(()))
    request = finish_module.WaitingFinishRequest(9, 10, 20, 1)
    result = finish_module._decision(object(), request, {"id": 9}, {"id": 3, "status": 2}, {})
    assert result[0] is finish_module.WaitingFinishDisposition.RESOLVED


def test_unsettled_attempt_waits_without_new_result(finish_module, monkeypatch):
    class Reads:
        def __init__(self, connection):
            pass
        def require_settled(self, run_id):
            raise finish_module.ConsistencyError("原结果尚未收场")
    monkeypatch.setattr(finish_module, "AvailabilityFinishReads", Reads)
    request = finish_module.WaitingFinishRequest(9, 10, 20, 1)
    result = finish_module._decision(object(), request, {"id": 9}, {"id": 3, "status": 2}, {})
    assert result[0] is finish_module.WaitingFinishDisposition.WAIT


def test_successful_attempt_is_not_reinterpreted_as_exhausted_failure(finish_module, monkeypatch):
    monkeypatch.setattr(finish_module, "row_facts", lambda *args: {
        "status": 2, "result_event_id": 42, "result_json": {}})
    connection = SimpleNamespace(execute=lambda *args: Cursor((7,)))
    request = finish_module.WaitingFinishRequest(9, 10, 20, 1)
    with pytest.raises(finish_module.ConsistencyError):
        finish_module._decision(connection, request, {"id": 9, "trigger_copy_id": None},
                                {"id": 3, "status": 2, "attempts_used": 1},
                                {"status": 1, "cancel_requested": 0, "scheduled_at": 10, "max_delay_ms": 0})


def test_wait_finish_guard_rejects_proposal_that_disagrees_with_current_facts(finish_module, monkeypatch):
    run = {"id": 3, "error_json": None}
    monkeypatch.setattr(finish_module, "_facts", lambda connection, request: ({}, run, {}))
    monkeypatch.setattr(finish_module, "_decision", lambda *args: (
        finish_module.WaitingFinishDisposition.FINISHED, 5, None))
    row = SimpleNamespace(table="operation_runs", row_id=3, after=SimpleNamespace(values={"status": 4}))
    event = SimpleNamespace(evidence={"availability_wait_finish_request": {
        "check_id": 9, "trusted_wall_now": 10, "occurred_at": 20, "max_attempts": 1}},
        occurred_at=20, event_id=42, rows=(row,))
    context = SimpleNamespace(transaction=SimpleNamespace(first_event_id=42, last_event_id=42),
                              state_rows={"operation_runs": {3: run}})
    with pytest.raises(finish_module.ConsistencyError):
        finish_module.verify_waiting_finish(object(), event, context, 3)


@pytest.fixture
def failure_module(monkeypatch):
    monkeypatch.setattr(resources, "resource_bytes", lambda name: pytest.fail("不能读取真实包资源"))
    monkeypatch.setattr(enums, "resource_bytes", lambda name: pytest.fail("不能读取真实包资源"))
    definitions = {
        "operation_runs.kind": SimpleNamespace(CHECK_DEVICE_AVAILABILITY=12, READ_FILE=4),
        "operation_runs.status": SimpleNamespace(FAILED=4, UNCONFIRMED=6),
        "device_activities.occupancy_state": SimpleNamespace(HELD=1, RELEASED=2),
    }
    monkeypatch.setattr(enums, "enum_for", lambda name: definitions[name])
    errors = ModuleType("camctl.contracts.workflow_errors")
    errors.action_error_id = lambda name: 31
    errors.validate_error_details = lambda name, details: None
    transaction = ModuleType("camctl.persistence.transaction")
    for name in ("CommandPlan", "commit_operation", "event_envelope", "row_facts",
                 "saved_transaction_events", "update_change"):
        setattr(transaction, name, object())
    target = ModuleType("camctl.persistence.repositories.availability_target")
    target.iter_fixed_targets = lambda *args: iter([((1, 10, 20),)])
    for item in (errors, transaction, target):
        monkeypatch.setitem(sys.modules, item.__name__, item)
    source = find_spec("camctl.persistence.repositories.availability_failure")
    spec = spec_from_file_location("camctl.persistence.repositories._isolated_availability_failure", source.origin)
    loaded = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, loaded)
    spec.loader.exec_module(loaded)
    return loaded


@pytest.mark.parametrize("status,occupancy,expected", [(4, 1, True), (6, 1, True),
                                                        (3, 1, False), (5, 1, False), (4, 2, False)])
def test_failure_delivery_requires_terminal_failure_and_unreleased_target(
        failure_module, monkeypatch, status, occupancy, expected):
    check = {"action_id": 10, "trigger_copy_id": 8, "device_id": "camera",
             "driver_id": "hb", "resource_key": "key", "expected_identity_json": {"serial": "s", "product_id": 1}}
    details = {"device_id": "camera", "operation_run_id": "3", "attempts_used": 2, "max_attempts": 1}
    run = {"id": 3, "action_id": 10, "responsibility_key": "availability/9", "status": status,
           "attempts_used": 2, "error_json": {"code": "source_device_unavailable", "stage": "source_read", "details": details}}
    activity = {"action_id": 30, "resource_key": "key", "expected_identity_json": check["expected_identity_json"],
                "occupancy_state": occupancy}
    facts = {"device_availability_checks": check, "operation_runs": run, "device_activities": activity,
             "actions": {"device_id": "camera", "driver_id": "hb"}}
    monkeypatch.setattr(failure_module, "row_facts", lambda connection, table, identity: facts[table])
    cursor = SimpleNamespace(fetchall=lambda: [(3,)], close=lambda: None)
    connection = SimpleNamespace(execute=lambda *args: cursor)
    result = failure_module.failure_basis(connection, 9, 10)
    assert (result is not None) is expected
    if expected:
        assert result[2] == details


@pytest.mark.parametrize("attempt_status,result,eligible", [(2, {}, True), (1, None, False), (3, None, False)])
def test_read_failure_preserves_existing_attempts_and_waits_for_settlement(
        failure_module, monkeypatch, attempt_status, result, eligible):
    outputs = ModuleType("camctl.persistence.repositories.outputs")
    outputs._read_owner_eligible = lambda action: True
    alias = ModuleType("camctl.persistence.repositories.physical_alias")
    alias.source_physical_identity = lambda *args: SimpleNamespace(resource_key="key", serial="s", product_id=1)
    monkeypatch.setitem(sys.modules, outputs.__name__, outputs)
    monkeypatch.setitem(sys.modules, alias.__name__, alias)
    check = {"action_id": 10, "trigger_copy_id": 8, "device_id": "camera", "driver_id": "hb",
             "resource_key": "key", "expected_identity_json": {"serial": "s", "product_id": 1}}
    facts = {
        "device_availability_checks": {9: check},
        "file_copies": {8: {"delivery_id": 11, "source_device_file_id": 12, "target_file_id": 13,
                            "slot_device_id": "camera"}},
        "deliveries": {11: {"id": 11, "action_id": 10, "output_id": 14, "status": 2}},
        "actions": {10: {"id": 10}, 15: {"id": 15, "device_id": "camera", "driver_id": "hb"},
                    16: {"id": 16}},
        "device_files": {12: {"id": 12, "observer_action_id": 15, "source_action_id": 16}},
        "outputs": {14: {"id": 14, "device_file_id": 12, "source_action_id": 16}},
        "intermediate_files": {13: {"id": 13}},
        "operation_runs": {17: {"id": 17, "kind": 4, "action_id": 10, "responsibility_key": "read/8",
                                 "status": 2, "attempts_used": 1}},
        "obtain_items": {18: {"id": 18, "output_id": 14, "selection_id": 19, "source_dependency": 1}},
        "obtain_source_selections": {19: {"id": 19, "dependency_id": 20}},
        "action_dependencies": {20: {"id": 20, "action_id": 10, "depends_on_action_id": 16}},
        "operation_attempts": {21: {"id": 21, "status": attempt_status, "result_json": result}},
    }
    monkeypatch.setattr(failure_module, "row_facts", lambda connection, table, identity: facts[table].get(identity))
    monkeypatch.setattr(failure_module, "failure_basis", lambda *args: (None, None, {"attempts_used": 2}))

    class Rows:
        def __init__(self, rows):
            self.rows = rows
        def fetchall(self):
            return self.rows
        def fetchmany(self, size):
            rows, self.rows = self.rows[:size], self.rows[size:]
            return rows
        def close(self):
            pass

    def execute(sql, params):
        if "operation_runs" in sql:
            return Rows([(17,)])
        if "obtain_items" in sql:
            return Rows([(18,)])
        return Rows([(21,)])

    state, details = failure_module._read_basis(SimpleNamespace(execute=execute), 9, 8)
    assert (details is not None) is eligible
    assert state["operation_runs"][17]["attempts_used"] == 1
    assert state["file_copies"][8]["slot_device_id"] == "camera"


@pytest.mark.parametrize("damage", [None, "missing_run", "wrong_run", "missing_slot"])
def test_read_failure_requires_complete_exact_event_group(failure_module, monkeypatch, damage):
    def change(table, identity, before, after):
        return SimpleNamespace(table=table, row_id=identity, before=SimpleNamespace(values=before),
                               after=SimpleNamespace(values=after))
    monkeypatch.setattr(failure_module, "update_change", change)
    outputs = ModuleType("camctl.persistence.repositories.outputs")
    outputs._COPY_CHANGED_EVENT, outputs._SLOT_REASON = 22, 6
    monkeypatch.setitem(sys.modules, outputs.__name__, outputs)
    details = {"operation_run_id": "3"}
    error = {"code": "source_device_unavailable", "stage": "source_read", "details": details}
    copy = {"id": 8, "slot_device_id": "camera"}
    delivery = {"id": 11, "status": 2, "error_json": None}
    run = {"id": 17, "status": 2, "retry_wait_required": 1, "error_json": None}
    item = {"id": 18}
    state = {"file_copies": {8: copy}, "deliveries": {11: delivery},
             "operation_runs": {17: run}, "obtain_items": {18: item}}
    monkeypatch.setattr(failure_module, "row_facts", lambda *args: {"availability_check_id": 9})
    monkeypatch.setattr(failure_module, "_read_basis", lambda *args: (state, details))
    cursor = SimpleNamespace(fetchall=lambda: [(8,)], close=lambda: None)
    connection = SimpleNamespace(execute=lambda *args: cursor)
    changes = [
        (23, 5, change("deliveries", 11, {"status": 2, "error_json": None}, {"status": 6, "error_json": error})),
        (10, 3, change("operation_runs", 17, {"status": 2, "retry_wait_required": 1, "error_json": None},
                       {"status": 4, "retry_wait_required": 0, "error_json": error})),
        (21, 3, change("obtain_items", 18, {"source_dependency": 1}, {"source_dependency": 0})),
        (22, 6, change("file_copies", 8, {"slot_device_id": "camera"}, {"slot_device_id": None})),
    ]
    events = [SimpleNamespace(event_id=30+i, transaction_id=5, occurred_at=40,
                              event_type=kind, reason=reason, rows=(row,), evidence={})
              for i, (kind, reason, row) in enumerate(changes)]
    if damage == "missing_run":
        del events[1]
    elif damage == "wrong_run":
        events[1].rows[0].row_id = 99
    elif damage == "missing_slot":
        events.pop()
    transaction = SimpleNamespace(first_event_id=30, last_event_id=33, txn_id=5)
    if damage is None:
        failure_module.verify_read_failure_group(connection, events[0], events, transaction)
    else:
        with pytest.raises(failure_module.ConsistencyError):
            failure_module.verify_read_failure_group(connection, events[0], events, transaction)

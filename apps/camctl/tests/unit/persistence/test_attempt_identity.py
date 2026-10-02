"""固定身份校验区分发起者、活动归属和读取父对象，不依赖资源。"""

from copy import deepcopy
from enum import IntEnum
from importlib.util import find_spec, module_from_spec
from unittest.mock import create_autospec

import pytest

from camctl.contracts import enums
from camctl.contracts.values import ConsistencyError
from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.history.validators import EventContext, EventValidationError
from camctl.contracts.history_values import TransactionRange
from camctl.persistence import row_history, transaction


# 每项只给出该行为的协议值，不在夹具中维护完整枚举登记。
_CASES = (
    ("START", 1, None, None, "start/7", "activity", 7),
    ("STOP", 2, None, None, "stop/7", "activity", 7),
    ("CHECK_CAPTURE_RESULTS", 7, None, None, "results/4", "activity", 7),
    ("STOP_RESIDUAL", 8, None, None, "followup/7/4", "activity", 9),
    ("READ_FILE", 3, None, None, "read/3", "delivery", 7),
    ("READ_FILE", 3, None, None, "read/3", "processing", 7),
    ("DELETE_FILE", 4, None, None, "delete/3", "cleanup", 7),
    ("CHECK_FILE_EXISTS", 5, None, None, "exists/3", "cleanup", 7),
    ("QUERY_ACTIVITY", 6, "BEFORE_EXECUTION", 1, "query/preflight/7", "preflight", 7),
    ("QUERY_ACTIVITY", 6, "START_CONFIRMATION", 2, "query/start/7/4", "start", 7),
    ("QUERY_ACTIVITY", 6, "ACTIVITY_OBSERVATION", 3, "query/activity/7/4", "activity", 7),
    ("QUERY_ACTIVITY", 6, "STOP_CONFIRMATION", 4, "query/stop/7/4", "stop", 7),
    ("QUERY_ACTIVITY", 6, "RESIDUAL_STOP_CONFIRMATION", 5, "query/residual/7/4", "residual", 9),
)


_ALL = pytest.mark.parametrize("identity_case", _CASES, indirect=True,
                               ids=lambda case: case[4] + "/" + case[5])


@pytest.fixture
def identity_case(request, monkeypatch):
    name, code, purpose, purpose_code, key, relation, owner_action = request.param
    definitions = {
        "operation_runs.kind": {"START": 1, "STOP": 2, "STOP_RESIDUAL": 8, name: code},
        "operation_runs.status": {"ACTIVE": 2},
        "operation_runs.query_purpose": {"BEFORE_EXECUTION": 1, **({purpose: purpose_code} if purpose else {})},
        "operation_attempts.status": {"RUNNING": 1},
        "operation_attempts.effect_state": {"UNKNOWN": 1},
    }
    monkeypatch.setattr(enums, "enum_for", create_autospec(enums.enum_for,
        side_effect=lambda column: IntEnum(column, definitions[column])))
    reader = create_autospec(enums.resource_bytes,
                             side_effect=AssertionError("纯单元不能读取登记资源"))
    monkeypatch.setattr(enums, "resource_bytes", reader)
    spec = find_spec("camctl.persistence.repositories.operations")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    run = {"id": 21, "action_id": 7, "kind": code, "query_purpose": purpose_code,
           "responsibility_key": key, "delivery_id": None, "activity_id": None,
           "copy_id": None, "cleanup_item_id": None, "session_key": None,
           "status": 2, "attempts_used": 1, "max_attempts_used": 3,
           "timeout_s_json": None, "retry_interval_s_json": None,
           "retry_wait_required": 0, "error_json": None}
    state = {"operation_runs": {21: run}}
    if relation in ("activity", "start", "stop", "residual"):
        run["activity_id"] = 4
        # 只传入固定关联事实，不把可变设备状态纳入身份契约。
        state["device_activities"] = {4: {"id": 4, "action_id": owner_action}}
    elif relation in ("delivery", "processing"):
        run["copy_id"] = 3
        run["delivery_id"] = 5 if relation == "delivery" else None
        state["file_copies"] = {3: {"id": 3, "delivery_id": run["delivery_id"],
                                   "processing_id": 5 if relation == "processing" else None}}
        table = "deliveries" if relation == "delivery" else "recording_processing"
        state[table] = {5: {"id": 5, "action_id": owner_action}}
    elif relation == "cleanup":
        run["cleanup_item_id"] = 3
        state["cleanup_items"] = {3: {"id": 3, "action_id": owner_action}}
    if relation in ("start", "stop", "residual"):
        original = dict(run, id=11, kind={"start": 1, "stop": 2, "residual": 8}[relation],
                        query_purpose=None, responsibility_key={
                            "start": "start/7", "stop": "stop/7", "residual": "followup/7/4"}[relation])
        state["operation_runs"][11] = original
    yield module, run, state, relation
    reader.assert_not_called()


@_ALL
def test_fixed_identity_accepts_its_actual_owner(identity_case):
    module, run, state, _ = identity_case
    module._verify_run_identity(run, state)


@_ALL
@pytest.mark.parametrize("field,value", [
    ("session_key", "a" * 32), ("responsibility_key", "unrelated/21"),
    ("action_id", False), ("kind", False), ("kind", 99),
    ("query_purpose", 99),
])
def test_fixed_identity_rejects_invalid_identity_fields(identity_case, field, value):
    module, run, state, _ = identity_case
    changed = dict(run, **{field: value})
    with pytest.raises(ConsistencyError):
        module._verify_run_identity(changed, state)


@pytest.mark.parametrize("identity_case", [case for case in _CASES if case[5] != "preflight"], indirect=True)
def test_fixed_identity_requires_its_target_and_parent(identity_case):
    module, run, state, relation = identity_case
    missing = deepcopy(state)
    table = {"delivery": "deliveries", "processing": "recording_processing",
             "cleanup": "cleanup_items"}.get(relation, "device_activities")
    missing[table] = {}
    with pytest.raises(ConsistencyError):
        module._verify_run_identity(run, missing)


@pytest.mark.parametrize("identity_case", [case for case in _CASES
    if case[5] not in ("preflight", "residual") and case[0] != "STOP_RESIDUAL"], indirect=True)
def test_fixed_identity_rejects_foreign_owner_except_residual_trigger(identity_case):
    module, run, state, relation = identity_case
    changed = deepcopy(state)
    table = {"delivery": "deliveries", "processing": "recording_processing",
             "cleanup": "cleanup_items"}.get(relation, "device_activities")
    next(iter(changed[table].values()))["action_id"] = 12
    with pytest.raises(ConsistencyError):
        module._verify_run_identity(run, changed)


@pytest.mark.parametrize("original_state", ["missing", "duplicate", "wrong_key", "wrong_target", "wrong_id"])
@pytest.mark.parametrize("identity_case", [case for case in _CASES if case[5] in ("start", "stop", "residual")], indirect=True)
def test_confirmation_requires_one_valid_original_flow(identity_case, original_state):
    module, run, state, relation = identity_case
    changed = deepcopy(state)
    originals = changed["operation_runs"]
    original = originals[11]
    if original_state == "missing":
        originals.pop(11)
    elif original_state == "duplicate":
        originals[12] = dict(original, id=12)
    elif original_state == "wrong_key":
        original["responsibility_key"] = "invalid/7"
    elif original_state == "wrong_target":
        original["copy_id"] = 3
    else:
        original["id"] = 12
    with pytest.raises(ConsistencyError):
        module._verify_run_identity(run, changed)


@_ALL
def test_fixed_identity_rejects_mixed_target_columns(identity_case):
    module, run, state, _ = identity_case
    changed = dict(run, **({"activity_id": 4} if run["copy_id"] is not None else {"copy_id": 3}))
    with pytest.raises(ConsistencyError):
        module._verify_run_identity(changed, state)


@_ALL
def test_fixed_identity_rejects_inapplicable_or_foreign_delivery(identity_case):
    module, run, state, _ = identity_case
    with pytest.raises(ConsistencyError):
        module._verify_run_identity(dict(run, delivery_id=99), state)


@pytest.mark.parametrize("identity_case", [case for case in _CASES if case[0] == "READ_FILE"], indirect=True)
@pytest.mark.parametrize("fault", ["missing_delivery_column", "missing_processing_column", "no_parent", "two_parents",
                                   "no_original_flow", "two_flows", "wrong_copy_id", "wrong_parent_id"])
def test_read_identity_requires_complete_parent_and_unique_physical_flow(identity_case, fault):
    module, run, state, relation = identity_case
    changed = deepcopy(state)
    copy = changed["file_copies"][3]
    if fault.startswith("missing_"):
        copy.pop("delivery_id" if fault == "missing_delivery_column" else "processing_id")
    elif fault == "no_parent":
        copy.update(delivery_id=None, processing_id=None)
    elif fault == "two_parents":
        copy.update(delivery_id=5, processing_id=5)
    elif fault == "no_original_flow":
        changed["operation_runs"] = {}
    elif fault == "two_flows":
        changed["operation_runs"][22] = dict(run, id=22)
    elif fault == "wrong_copy_id":
        copy["id"] = 4
    else:
        changed["deliveries" if relation == "delivery" else "recording_processing"][5]["id"] = 6
    with pytest.raises(ConsistencyError):
        module._verify_run_identity(run, changed)


def _guard_event():
    return EventEnvelope(1, 1, 11, 1, 1, 2, None, 1, {}, (
        RowChange("operation_runs", 21, RowImage(True, {"status": 1}), RowImage(True, {"status": 2})),
    ))


@_ALL
def test_identity_guard_uses_complete_proposal_for_later_targets(identity_case):
    module, _, state, _ = identity_case
    before = {"operation_runs": state["operation_runs"]}
    context = EventContext(TransactionRange(1, 1, 1), {}, before, transaction_rows=state)
    module._operation_identity_guard(_guard_event(), context)


@pytest.mark.parametrize("identity_case", [case for case in _CASES if case[5] != "preflight"], indirect=True)
def test_identity_guard_does_not_replace_explicit_empty_proposal_with_old_targets(identity_case):
    module, _, state, _ = identity_case
    context = EventContext(TransactionRange(1, 1, 1), {}, state, transaction_rows={})
    with pytest.raises(EventValidationError):
        module._operation_identity_guard(_guard_event(), context)


@_ALL
def test_identity_guard_checks_event_facts_when_proposal_is_not_provided(identity_case):
    module, _, state, _ = identity_case
    context = EventContext(TransactionRange(1, 1, 1), {}, state)
    module._operation_identity_guard(_guard_event(), context)


def _read_creation_event(state, table):
    identity = 3 if table == "file_copies" else 21
    after = dict(state[table][identity])
    after.pop("id")
    event_type = 22 if table == "file_copies" else 10
    return EventEnvelope(2, 1, event_type, 1, 1, 2, None, 1, {}, (
        RowChange(table, identity, RowImage(False, {}), RowImage(True, after)),
    ))


@pytest.mark.parametrize("identity_case", [case for case in _CASES if case[0] == "READ_FILE"], indirect=True)
@pytest.mark.parametrize("table", ["file_copies", "operation_runs"])
def test_identity_guard_includes_current_creation_without_complete_proposal(identity_case, table):
    module, _, state, _ = identity_case
    event = _read_creation_event(state, table)
    before = deepcopy(state)
    before[table].pop(event.rows[0].row_id)
    unchanged = deepcopy(before)
    context = EventContext(TransactionRange(1, 1, 2), {}, before)

    module._operation_identity_guard(event, context)

    assert before == unchanged


@pytest.mark.parametrize("identity_case", [case for case in _CASES if case[0] == "READ_FILE"], indirect=True)
@pytest.mark.parametrize("table", ["file_copies", "operation_runs"])
def test_identity_guard_current_creation_requires_parent(identity_case, table):
    module, _, state, relation = identity_case
    event = _read_creation_event(state, table)
    before = deepcopy(state)
    before[table].pop(event.rows[0].row_id)
    before["deliveries" if relation == "delivery" else "recording_processing"] = {}
    context = EventContext(TransactionRange(1, 1, 2), {}, before)

    with pytest.raises(EventValidationError):
        module._operation_identity_guard(event, context)


@pytest.mark.parametrize("identity_case", [case for case in _CASES if case[0] == "READ_FILE"], indirect=True)
@pytest.mark.parametrize("table", ["file_copies", "operation_runs"])
def test_identity_guard_current_creation_does_not_replace_empty_proposal(identity_case, table):
    module, _, state, _ = identity_case
    event = _read_creation_event(state, table)
    before = deepcopy(state)
    before[table].pop(event.rows[0].row_id)
    context = EventContext(TransactionRange(1, 1, 2), {}, before, transaction_rows={})

    with pytest.raises(EventValidationError):
        module._operation_identity_guard(event, context)


@pytest.mark.parametrize("identity_case", [case for case in _CASES if case[0] == "READ_FILE"], indirect=True)
@pytest.mark.parametrize("table", ["file_copies", "operation_runs"])
@pytest.mark.parametrize("fault", ["foreign_parent", "duplicate_read", "missing_other_creation"])
def test_identity_guard_current_creation_rejects_invalid_or_future_relations(identity_case, table, fault):
    module, run, state, relation = identity_case
    event = _read_creation_event(state, table)
    before = deepcopy(state)
    before[table].pop(event.rows[0].row_id)
    if fault == "foreign_parent":
        before["deliveries" if relation == "delivery" else "recording_processing"][5]["action_id"] = 12
    elif fault == "duplicate_read":
        before["operation_runs"][22] = dict(run, id=22)
    else:
        before["operation_runs" if table == "file_copies" else "file_copies"] = {}
    context = EventContext(TransactionRange(1, 1, 2), {}, before)

    with pytest.raises(EventValidationError):
        module._operation_identity_guard(event, context)

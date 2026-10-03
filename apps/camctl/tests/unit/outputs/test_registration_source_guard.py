"""具名 output 守卫按事件当前事实核对三类产物的来源。"""

from copy import deepcopy
from dataclasses import replace

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.history import validators
from camctl.history.reads import ReadCoverage
from camctl.history.validators import EventContext, EventValidationError
from camctl.outputs.catalog import OutputCatalogFacts
from camctl.persistence.repositories.capture import FinishCapture, register_capture_guards
from camctl.persistence.transaction import event_envelope, row_change


def _registration(kind=1):
    states = {
        "actions": {
            1: {"id": 1, "type": 2, "device_id": "cam", "driver_id": "driver", "status": 3},
            2: {"id": 2, "type": 1, "device_id": "cam", "driver_id": "driver", "status": 3},
        },
        "device_files": {11: {"id": 11, "source_action_id": 1, "observer_action_id": 2,
                              "ownership_evidence_json": {}, "role": 3 if kind == 3 else 2,
                              "completion_state": 3}},
        "intermediate_files": {11: {"id": 11, "owner_action_id": 1, "owner_delivery_id": None}},
        "outputs": {9: {"id": 9, "kind": 1, "source_action_id": 1, "device_file_id": 9,
                        "intermediate_file_id": None}},
        "output_origins": {},
    }
    states["device_files"][9] = {**states["device_files"][11], "id": 9, "role": 2,
                                "original_device_file_id": None, "pairing_evidence_json": None}
    states["device_files"][11].update(original_device_file_id=9 if kind == 3 else None,
                                      pairing_evidence_json={} if kind == 3 else None)
    values = {"source_action_id": 1, "kind": kind,
              "device_file_id": None if kind == 2 else 11,
              "intermediate_file_id": 11 if kind == 2 else None}
    rows = (row_change("outputs", 10, values),)
    if kind != 1:
        rows += (row_change("output_origins", 8, {"output_id": 10, "original_output_id": 9}),)
    event = event_envelope(1, 1, 20, kind, rows, 1)
    return event, EventContext(TransactionRange(1, 1, 1), {}, states, read_coverage=ReadCoverage({
        ("output_origins", "output_id"): frozenset({9, 10}),
        ("output_origins", "original_output_id"): frozenset({9}),
    }))


@pytest.fixture
def output_guard(monkeypatch):
    monkeypatch.setattr(validators, "NAMED_GUARDS", dict(validators.NAMED_GUARDS))
    register_capture_guards()
    return validators.NAMED_GUARDS["output"]


@pytest.mark.parametrize("kind,source_type", [(1, 1), (1, 2), (1, 3), (2, 2), (3, 1), (3, 2), (3, 3)])
def test_output_guard_accepts_matching_source(output_guard, kind, source_type):
    event, context = _registration(kind)
    context.state_rows["actions"][1]["type"] = source_type
    output_guard(event, context)


@pytest.mark.parametrize("kind", [1, 2, 3])
@pytest.mark.parametrize("case", ["other_owner", "unknown_owner", "missing_file", "missing_source", "non_capture"])
def test_output_guard_rejects_unproven_source(output_guard, kind, case):
    event, context = _registration(kind)
    table = "intermediate_files" if kind == 2 else "device_files"
    owner = "owner_action_id" if kind == 2 else "source_action_id"
    if case == "missing_file":
        context.state_rows[table].clear()
    elif case == "missing_source":
        del context.state_rows["actions"][1]
    elif case == "non_capture":
        context.state_rows["actions"][1]["type"] = 4
    else:
        context.state_rows[table][11][owner] = 2 if case == "other_owner" else None
    with pytest.raises(EventValidationError):
        output_guard(event, context)


@pytest.mark.parametrize("kind", [1, 3])
@pytest.mark.parametrize("case", [
    "no_evidence", "no_observer", "missing_observer", "non_capture_observer",
    "other_device", "other_driver", "unknown_devices", "unknown_drivers",
])
def test_device_output_guard_requires_original_binding(output_guard, kind, case):
    event, context = _registration(kind)
    file = context.state_rows["device_files"][11]
    observer = context.state_rows["actions"][2]
    if case == "no_evidence":
        file["ownership_evidence_json"] = None
    elif case == "no_observer":
        del file["observer_action_id"]
    elif case == "missing_observer":
        del context.state_rows["actions"][2]
    elif case == "non_capture_observer":
        observer["type"] = 4
    elif case in {"other_device", "other_driver"}:
        observer["device_id" if case == "other_device" else "driver_id"] = "other"
    else:
        column = "device_id" if case == "unknown_devices" else "driver_id"
        observer[column] = context.state_rows["actions"][1][column] = None
    with pytest.raises(EventValidationError):
        output_guard(event, context)


@pytest.mark.parametrize("case", ["present", "unknown"])
def test_intermediate_output_guard_requires_no_delivery_owner(output_guard, case):
    event, context = _registration(2)
    if case == "present":
        context.state_rows["intermediate_files"][11]["owner_delivery_id"] = 9
    else:
        del context.state_rows["intermediate_files"][11]["owner_delivery_id"]
    with pytest.raises(EventValidationError):
        output_guard(event, context)


@pytest.mark.parametrize("kind", [1, 2, 3])
def test_later_source_assignment_cannot_authorize_registration(output_guard, kind):
    event, context = _registration(kind)
    future = deepcopy(context.state_rows)
    table, owner = ("intermediate_files", "owner_action_id") if kind == 2 else ("device_files", "source_action_id")
    context.state_rows[table][11][owner] = None
    with pytest.raises(EventValidationError):
        output_guard(event, replace(context, transaction_rows=future))


@pytest.mark.parametrize("kind", [1, 3])
def test_future_binding_cannot_replace_current_observer_binding(output_guard, kind):
    event, context = _registration(kind)
    future = deepcopy(context.state_rows)
    context.state_rows["actions"][2]["driver_id"] = "other"
    with pytest.raises(EventValidationError):
        output_guard(event, replace(context, transaction_rows=future))


@pytest.mark.parametrize("identity", [0, -1, True, 1.0, "1"])
def test_finish_action_identity_is_not_coerced(identity):
    with pytest.raises(ValueError):
        FinishCapture(identity, (), OutputCatalogFacts(1, True), 1)

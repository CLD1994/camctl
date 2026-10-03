"""正式 output 守卫按完整的当前关系拒绝缺失、错配和重复派生。"""

from copy import deepcopy
from dataclasses import replace

import pytest

from camctl.history.reads import ReadCoverage
from camctl.history.validators import EventValidationError
from camctl.persistence.transaction import row_change

from .test_registration_source_guard import _registration, output_guard


@pytest.mark.parametrize("kind", [2, 3])
@pytest.mark.parametrize("case", [
    "missing_link", "duplicate_link", "foreign_link", "self", "missing_original",
    "derived_target", "foreign_source", "original_has_origin", "same_kind_exists",
    "missing_original_file", "wrong_original_role", "original_file_has_pairing",
    "unknown_original_backend",
])
def test_derived_registration_requires_one_valid_unique_origin(output_guard, kind, case):
    event, context = _registration(kind)
    if case == "missing_link":
        event = replace(event, rows=event.rows[:1])
    elif case == "duplicate_link":
        event = replace(event, rows=(*event.rows, replace(event.rows[1], row_id=18)))
    elif case in {"foreign_link", "self"}:
        values = {"output_id": 99 if case == "foreign_link" else 10,
                  "original_output_id": 10 if case == "self" else 9}
        event = replace(event, rows=(event.rows[0], row_change("output_origins", 8, values)))
    elif case == "missing_original":
        del context.state_rows["outputs"][9]
    elif case == "derived_target":
        context.state_rows["outputs"][9]["kind"] = 3
    elif case == "foreign_source":
        context.state_rows["outputs"][9]["source_action_id"] = 2
    elif case == "original_has_origin":
        context.state_rows["output_origins"][7] = {"output_id": 9, "original_output_id": 5}
    elif case == "missing_original_file":
        del context.state_rows["device_files"][9]
    elif case == "wrong_original_role":
        context.state_rows["device_files"][9]["role"] = 3
    elif case == "original_file_has_pairing":
        context.state_rows["device_files"][9].update(original_device_file_id=8, pairing_evidence_json={})
    elif case == "unknown_original_backend":
        del context.state_rows["outputs"][9]["intermediate_file_id"]
    else:
        _existing_derivative(context, kind)
    with pytest.raises(EventValidationError):
        output_guard(event, context)


def _existing_derivative(context, kind):
    context.state_rows["outputs"][20] = {"kind": kind, "source_action_id": 1}
    context.state_rows["output_origins"][21] = {"output_id": 20, "original_output_id": 9}


@pytest.mark.parametrize("kind", [2, 3])
def test_other_derivative_kind_does_not_block_registration(output_guard, kind):
    event, context = _registration(kind)
    _existing_derivative(context, 3 if kind == 2 else 2)
    output_guard(event, context)


@pytest.mark.parametrize("case", ["unpaired", "no_evidence", "wrong_original"])
def test_preview_origin_must_match_device_pairing(output_guard, case):
    event, context = _registration(3)
    file = context.state_rows["device_files"][11]
    if case == "no_evidence":
        file["pairing_evidence_json"] = None
    else:
        file["original_device_file_id"] = None if case == "unpaired" else 19
    with pytest.raises(EventValidationError):
        output_guard(event, context)


@pytest.mark.parametrize("column,identity", [("output_id", 10), ("output_id", 9), ("original_output_id", 9)])
def test_unknown_relation_range_cannot_be_treated_as_empty(output_guard, column, identity):
    event, context = _registration(3)
    ranges = dict(context.read_coverage.ranges)
    ranges["output_origins", column] -= {identity}
    with pytest.raises(EventValidationError):
        output_guard(event, replace(context, read_coverage=ReadCoverage(ranges)))


def test_original_cannot_register_with_an_origin(output_guard):
    event, context = _registration(1)
    event = replace(event, rows=(*event.rows, row_change("output_origins", 8,
                                                      {"output_id": 10, "original_output_id": 9})))
    with pytest.raises(EventValidationError):
        output_guard(event, context)


@pytest.mark.parametrize("case", ["original_in_future", "duplicate_removed_in_future"])
def test_future_relations_cannot_authorize_current_registration(output_guard, case):
    event, context = _registration(3)
    future = deepcopy(context.state_rows)
    if case == "original_in_future":
        del context.state_rows["outputs"][9]
    else:
        _existing_derivative(context, 3)
    with pytest.raises(EventValidationError):
        output_guard(event, replace(context, transaction_rows=future))


@pytest.mark.parametrize("kind", [2, 3])
def test_same_event_cannot_register_two_derivatives_of_one_kind(output_guard, kind):
    event, context = _registration(kind)
    table = "intermediate_files" if kind == 2 else "device_files"
    column = "intermediate_file_id" if kind == 2 else "device_file_id"
    context.state_rows[table][12] = {**context.state_rows[table][11], "id": 12}
    output = row_change("outputs", 12, {**event.rows[0].after.values, column: 12})
    event = replace(event, rows=(*event.rows, output,
        row_change("output_origins", 18, {"output_id": 12, "original_output_id": 9})))
    ranges = dict(context.read_coverage.ranges)
    ranges["output_origins", "output_id"] |= {12}
    with pytest.raises(EventValidationError):
        output_guard(event, replace(context, read_coverage=ReadCoverage(ranges)))


@pytest.mark.parametrize("kind", [1, 2, 3])
def test_formal_registration_rejects_wrong_file_table(output_guard, kind):
    event, context = _registration(kind)
    row = event.rows[0]
    values = {**row.after.values, "device_file_id": 11 if kind == 2 else None,
              "intermediate_file_id": None if kind == 2 else 11}
    event = replace(event, rows=(row_change("outputs", 10, values), *event.rows[1:]))
    with pytest.raises(EventValidationError):
        output_guard(event, context)

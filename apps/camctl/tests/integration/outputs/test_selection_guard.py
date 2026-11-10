"""正式选择事件以原请求、完整当前事实和保存后的条目次序为准。"""

from copy import deepcopy
from dataclasses import replace

import pytest

from camctl.contracts.values import new_operation_key
from camctl.history.reads import ReadCoverage
from camctl.history.validators import EventValidationError
from ..support.output_planning import EventSequence, capture_operation, validate_static_event as validate_event
from camctl.outputs.sources import SelectionMode
from camctl.persistence.repositories import outputs
from camctl.persistence.repositories.transactions import commit_operation, row_facts

from .test_catalog_integrity import selection_database
from .test_output_family import family_database
from .test_selection_authority import _request, _snapshot, _NOW


def _proposal(owned, mode=SelectionMode.DEFAULT):
    requested = (705, 703, 999, 704) if mode is SelectionMode.EXPLICIT_IDS else ()
    _request(owned.connection, mode, requested)
    snapshot = _snapshot(owned.connection, mode, requested)
    plan = capture_operation(owned, lambda target: outputs.OutputsRepository().fix_selection(
        outputs.FixSelection(61, snapshot, _NOW), new_operation_key(), target))
    return plan.events[0], plan


def _change_item(event, index=0, **values):
    row = event.rows[index + 1]
    changed = replace(row, after=replace(row.after, values={**row.after.values, **values}))
    return replace(event, rows=event.rows[:index + 1] + (changed,) + event.rows[index + 2:])


def _commit_selection(owned, event, proposal, *, rejected=False):
    before = tuple(owned.connection.iterdump())
    receipt = commit_operation(EventSequence((event,), proposal), new_operation_key(), owned)
    assert not owned.connection.in_transaction
    if rejected:
        assert receipt.kind == "rolled_back", receipt.error
        assert isinstance(receipt.error, EventValidationError), receipt.error
        assert "选择条目的完整集合、保存顺序或依据" in str(receipt.error)
        assert tuple(owned.connection.iterdump()) == before
    else:
        assert receipt.kind == "completed", receipt.error
    return receipt


@pytest.mark.parametrize("mode", list(SelectionMode))
def test_registered_guard_accepts_complete_saved_request(selection_database, mode):
    event, context = _proposal(selection_database, mode)
    _commit_selection(selection_database, event, context)


@pytest.mark.parametrize("mode", list(SelectionMode))
def test_event_row_order_does_not_change_persisted_selection_order(selection_database, mode):
    event, context = _proposal(selection_database, mode)
    _commit_selection(selection_database, replace(event, rows=tuple(reversed(event.rows))), context)
    saved = outputs.load_selection(selection_database.connection, 61)
    assert [(item.output_id, item.requested_output_id) for item in saved.items] == {
        SelectionMode.DEFAULT: [(703, None), (705, None)],
        SelectionMode.PREVIEW: [(703, None), (None, None)],
        SelectionMode.EXPLICIT_IDS: [(705, 705), (703, 703), (None, 999), (None, 704)],
    }[mode]


@pytest.mark.parametrize("mode", list(SelectionMode))
@pytest.mark.parametrize("mutation", ["omit", "append", "swap_ids"])
def test_registered_guard_rejects_incomplete_or_reordered_saved_collection(selection_database, mode, mutation):
    event, context = _proposal(selection_database, mode)
    rows = list(event.rows)
    if mutation == "omit":
        rows.pop()
    elif mutation == "append":
        rows.append(replace(rows[1], row_id=99))
        context.owners["obtain_items", 99] = ("action", 30)
    else:
        rows[1], rows[2] = replace(rows[1], row_id=rows[2].row_id), replace(rows[2], row_id=rows[1].row_id)
    _commit_selection(selection_database, replace(event, rows=tuple(rows)), context, rejected=True)


@pytest.mark.parametrize("mode,changes", [
    (SelectionMode.DEFAULT, {"output_id": 702, "basis": 3}),
    (SelectionMode.DEFAULT, {"original_output_id": 705}),
    (SelectionMode.PREVIEW, {"preview_size": 79}),
    (SelectionMode.PREVIEW, {"repaired_size": 81}),
    (SelectionMode.PREVIEW, {"basis": 3, "output_id": 702}),
    (SelectionMode.EXPLICIT_IDS, {"requested_output_id": 701}),
])
def test_registered_guard_rejects_selection_facts_not_derived_from_request(selection_database, mode, changes):
    event, context = _proposal(selection_database, mode)
    _commit_selection(selection_database, _change_item(event, **changes), context, rejected=True)


@pytest.mark.parametrize("table,column,identity", [
    ("outputs", "source_action_id", 11),
    ("outputs", "id", 999),
    ("recording_processing", "action_id", 11),
    ("obtain_items", "selection_id", 61),
    ("output_origins", "original_output_id", 701),
])
def test_registered_guard_rejects_uncovered_reads(selection_database, table, column, identity):
    event, context = _proposal(selection_database, SelectionMode.EXPLICIT_IDS)
    ranges = dict(context.read_coverage.ranges)
    ranges[table, column] = ranges[table, column] - {identity}
    with pytest.raises(EventValidationError):
        validate_event(event, replace(context, read_coverage=ReadCoverage(ranges)))


@pytest.mark.parametrize("table,identity,changes", [
    ("obtain_source_selections", 61, {"status": 2}),
    ("actions", 30, {"type": 7}),
    ("actions", 30, {"source_resolution_state": 1}),
    ("actions", 30, {"resolved_source_plan_id": 2}),
    ("actions", 30, {"execution_spec_json": {"selection_mode": 2}}),
    ("actions", 30, {"input_fields_json": {"params": {"filter": "preview"}}}),
    ("actions", 11, {"type": 7}),
    ("outputs", 703, {"availability": 3}),
    ("device_files", 502, {"original_device_file_id": 505}),
])
def test_registered_guard_uses_current_eligibility_and_selection_facts(selection_database, table, identity, changes):
    event, context = _proposal(selection_database)
    context.state_rows[table][identity].update(changes)
    with pytest.raises(EventValidationError):
        validate_event(event, context)


@pytest.mark.parametrize("status", [1, 2, 4])
def test_current_old_item_prevents_first_fix(selection_database, status):
    event, context = _proposal(selection_database)
    context.state_rows["obtain_items"][99] = {"selection_id": 61, "status": status}
    with pytest.raises(EventValidationError):
        validate_event(event, context)


@pytest.mark.parametrize("changes", [
    {"check_state": 2}, {"repair_state": 3}, {"repair_state": 4},
    {"discard_state": 2}, {"discard_state": 3},
])
def test_unfinished_current_processing_prevents_fix(selection_database, changes):
    event, context = _proposal(selection_database)
    context.state_rows["recording_processing"] = {51: {
        "action_id": 11, "check_state": 1, "repair_state": 2, "discard_state": 1, **changes}}
    with pytest.raises(EventValidationError):
        validate_event(event, context)


@pytest.mark.parametrize("table,identity", [
    ("actions", 30), ("actions", 11), ("obtain_source_selections", 61),
    ("action_dependencies", 41), ("device_files", 501), ("intermediate_files", 801),
])
def test_future_rows_cannot_supply_current_selection_facts(selection_database, table, identity):
    event, context = _proposal(selection_database)
    future = deepcopy(context.state_rows)
    del context.state_rows[table][identity]
    with pytest.raises(EventValidationError):
        validate_event(event, replace(context, transaction_rows=future))


@pytest.mark.parametrize("table,identity,changes", [
    ("actions", 30, {"cancel_requested": 1}),
    ("actions", 30, {"execution_started": 0}),
    ("actions", 11, {"status": 2}),
    ("actions", 11, {"type": True}),
    ("actions", 30, {"resolved_source_plan_id": True}),
    ("actions", 30, {"cancel_requested": False}),
    ("obtain_source_selections", 61, {"status": True}),
    ("obtain_source_selections", 61, {"error_code": 1, "error_details_json": {}}),
    ("action_dependencies", 41, {"depends_on_action_id": True}),
    ("outputs", 704, {"source_action_id": True}),
])
def test_future_eligible_facts_do_not_override_current_ineligibility(selection_database, table, identity, changes):
    event, context = _proposal(selection_database, SelectionMode.EXPLICIT_IDS)
    future = deepcopy(context.state_rows)
    context.state_rows[table][identity].update(changes)
    with pytest.raises(EventValidationError):
        validate_event(event, replace(context, transaction_rows=future))


@pytest.mark.parametrize("column", ["check_state", "repair_state", "discard_state"])
@pytest.mark.parametrize("invalid", [None, True, "1", 99])
def test_invalid_processing_state_is_not_completed(selection_database, column, invalid):
    event, context = _proposal(selection_database)
    context.state_rows["recording_processing"] = {51: {
        "action_id": 11, "check_state": 1, "repair_state": 2, "discard_state": 1, column: invalid}}
    with pytest.raises(EventValidationError):
        validate_event(event, context)


@pytest.mark.parametrize("check,repair,discard", [(1, 2, 1), (3, 5, 1), (4, 6, 4), (5, 7, 5)])
def test_finished_processing_states_permit_selection(selection_database, check, repair, discard):
    event, context = _proposal(selection_database)
    context.state_rows["recording_processing"] = {51: {
        "action_id": 11, "check_state": check, "repair_state": repair, "discard_state": discard}}
    validate_event(event, context)


def test_multiple_current_processing_records_are_not_one_completed_responsibility(selection_database):
    event, context = _proposal(selection_database)
    context.state_rows["recording_processing"] = {
        identity: {"action_id": 11, "check_state": 1, "repair_state": 2, "discard_state": 1}
        for identity in (51, 52)}
    with pytest.raises(EventValidationError):
        validate_event(event, context)


@pytest.mark.parametrize("mode", list(SelectionMode))
def test_reliably_empty_catalog_uses_saved_mode(selection_database, mode):
    connection = selection_database.connection
    connection.execute("DELETE FROM output_origins")
    connection.execute("DELETE FROM outputs WHERE source_action_id=11")
    connection.commit()
    event, context = _proposal(selection_database, mode)
    _commit_selection(selection_database, event, context)
    if mode is SelectionMode.EXPLICIT_IDS:
        assert [row.after.values["error_code"] for row in event.rows[1:]] == [1, 1, 1, 2]
        selection = row_facts(connection, "obtain_source_selections", 61)
        assert selection["error_code"] is None
    else:
        assert len(event.rows) == 1
        assert event.rows[0].after.values["error_code"] == 1

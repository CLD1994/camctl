"""来源选择保存的错误由公共登记直接消费，关联列仍保存整数身份。"""

from contextlib import closing

import pytest

from camctl.contracts.values import new_operation_key
from camctl.contracts.workflow_errors import registered_error_spec, validate_error_details
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import FixSelection, load_selection_facts
from camctl.persistence.repositories.outputs import _source_selection_guard
from camctl.persistence.transaction import event_envelope, row_facts, update_change
from camctl.history.validators import EventValidationError
from camctl.outputs.sources import SelectionMode, select_outputs

from .test_sources import _NOW, _prepared_selection, _fixed_resolution, _seed_output
from .test_member_guard import member_context, read_targets


@pytest.mark.parametrize("scenario,expected", [
    ("not_found", 1), ("mismatch", 2), ("cleaned", 3), ("restricted", 4), ("preview_missing", 8),
])
def test_selection_errors_satisfy_public_schema_after_commit(tmp_path, scenario, expected):
    _, owned, repository, selection_id = _prepared_selection(tmp_path)
    try:
        connection = owned.connection
        if scenario != "not_found":
            _seed_output(connection, 501, 11 if scenario == "mismatch" else 21, 1,
                         availability={"cleaned": 3, "restricted": 2}.get(scenario, 1))
        connection.commit()
        requested = () if scenario == "preview_missing" else (501,)
        snapshot = select_outputs(
            _fixed_resolution((21,)), load_selection_facts(connection, 21, requested_output_ids=requested),
            SelectionMode.PREVIEW if scenario == "preview_missing" else SelectionMode.EXPLICIT_IDS,
            requested_output_ids=requested,
        )
        result = repository.fix_selection(FixSelection(selection_id, snapshot, _NOW), new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        with closing(connection.execute("SELECT id FROM obtain_items WHERE selection_id=?", (selection_id,))) as cursor:
            item_id, = cursor.fetchone()
        item = row_facts(connection, "obtain_items", item_id)
        assert item["error_code"] == expected
        name, _ = registered_error_spec("item_error_ids.obtain_items", expected)
        validate_error_details(name, item["error_details_json"])
        if item["output_id"] is not None:
            assert isinstance(item["output_id"], int)
        selection = row_facts(connection, "obtain_source_selections", selection_id)
        if selection["error_code"] is not None:
            name, _ = registered_error_spec("item_error_ids.obtain_source_selections", selection["error_code"])
            validate_error_details(name, selection["error_details_json"])
    finally:
        owned.connection.close()


def _source_error_event(context, code, details):
    context.state_rows["obtain_source_selections"][31]["status"] = 1
    context.state_rows["obtain_items"].clear()
    row = update_change("obtain_source_selections", 31,
        {"status": 1, "error_code": None, "error_details_json": None},
        {"status": 2, "error_code": code, "error_details_json": details})
    return event_envelope(2, 2, 4, 1, (row,), _NOW)


@pytest.mark.parametrize("details", [
    {"source_action_instance_id": "12"},
    {"source_action_instance_id": "11", "original_output_id": "702"},
    {"source_action_instance_id": "11", "original_output_id": "703"},
    {"source_action_instance_id": "11", "original_output_id": "999"},
])
def test_source_error_must_refer_to_actual_source_and_original(member_context, details):
    member_context.state_rows["outputs"][702] = {"id": 702, "source_action_id": 12, "kind": 1}
    member_context.state_rows["outputs"][703] = {"id": 703, "source_action_id": 11, "kind": 2}
    event = _source_error_event(member_context, 2, details)
    with pytest.raises(EventValidationError):
        _source_selection_guard(event, member_context)


@pytest.mark.parametrize("code,details", [
    (1, {}), (2, {"source_action_instance_id": "11"}),
    (2, {"source_action_instance_id": "11", "original_output_id": "701"}),
])
def test_source_error_accepts_registered_details_for_actual_source(member_context, code, details):
    event = _source_error_event(member_context, code, details)
    _source_selection_guard(event, member_context)

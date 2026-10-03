"""来源选择保存的错误由公共登记直接消费，关联列仍保存整数身份。"""

from contextlib import closing
from dataclasses import replace

import pytest

from camctl.contracts.values import new_operation_key
from camctl.contracts.workflow_errors import registered_error_spec, validate_error_details
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import FixSelection, load_selection_facts
from camctl.persistence.transaction import row_facts
from camctl.history.validators import EventValidationError, validate_event
from camctl.outputs.sources import SelectionMode, select_outputs

from .test_sources import _NOW, _prepared_selection, _fixed_resolution, _seed_output, _set_selection_request
from .test_selection_guard import _proposal, selection_database, family_database


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
        _set_selection_request(connection,
            SelectionMode.PREVIEW if scenario == "preview_missing" else SelectionMode.EXPLICIT_IDS, requested)
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


def _source_error_event(owned, code, details):
    connection = owned.connection
    if code == 1:
        connection.execute("DELETE FROM output_origins")
        connection.execute("DELETE FROM outputs WHERE source_action_id=11")
    else:
        connection.execute("DELETE FROM output_origins WHERE output_id=702")
        connection.execute("DELETE FROM outputs WHERE id=702")
    connection.commit()
    event, context = _proposal(owned, SelectionMode.DEFAULT if code == 1 else SelectionMode.PREVIEW)
    row = event.rows[0]
    assert row.after.values["error_code"] == code
    row = replace(row, after=replace(row.after, values={**row.after.values, "error_details_json": details}))
    return replace(event, rows=(row, *event.rows[1:])), context


@pytest.mark.parametrize("details", [
    {"source_action_instance_id": "12"},
    {"source_action_instance_id": "11", "original_output_id": "704"},
    {"source_action_instance_id": "11", "original_output_id": "703"},
    {"source_action_instance_id": "11", "original_output_id": "999"},
])
def test_source_error_must_refer_to_actual_source_and_original(selection_database, details):
    event, context = _source_error_event(selection_database, 2, details)
    context.state_rows["outputs"][704] = {"id": 704, "source_action_id": 12, "kind": 1}
    with pytest.raises(EventValidationError):
        validate_event(event, context)


@pytest.mark.parametrize("code,details", [
    (1, {}), (2, {"source_action_instance_id": "11"}),
    (2, {"source_action_instance_id": "11", "original_output_id": "701"}),
])
def test_source_error_accepts_registered_details_for_actual_source(selection_database, code, details):
    event, context = _source_error_event(selection_database, code, details)
    validate_event(event, context)

"""来源选择保存的错误由公共登记直接消费，关联列仍保存整数身份。"""

from contextlib import closing
from dataclasses import replace
import json

import pytest

from camctl.contracts.values import new_operation_key
from camctl.contracts.workflow_errors import item_error_id, registered_error_spec, validate_error_details
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories import outputs
from camctl.persistence.repositories.outputs import FixSelection, load_selection_facts
from camctl.persistence.transaction import commit_operation, row_facts
from camctl.history.validators import EventValidationError
from camctl.history.decoding import decode_event_row
from ..support.output_planning import EventSequence, capture_operation, proposal_state
from ..support.output_storage_cases import observed_operation
from camctl.outputs.sources import SelectionMode, select_outputs

from .test_sources import _NOW, _prepared_selection, _fixed_resolution, _seed_output, _set_selection_request
from .test_selection_guard import selection_database, family_database
from .test_selection_authority import _request


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


def _legacy_source_error_event(owned, details):
    connection = owned.connection
    connection.execute("DELETE FROM output_origins WHERE output_id=702")
    connection.execute("DELETE FROM outputs WHERE id=702")
    connection.commit()
    _request(connection, SelectionMode.PREVIEW)
    # 存储格式专项：这里只核对完整业务前态及事件保存，不以夹具的占位历史验证重放。
    # 旧判定在两个原片均无预览时保存来源错误及两个失败项。
    # 可选 original_output_id 按公共错误登记构造，不代表旧生产者曾写出该字段。
    rows = [{
        "table": "obtain_source_selections", "id": 61,
        "before": {"exists": True, "values": {
            "status": 1, "error_code": None, "error_details_json": None}},
        "after": {"exists": True, "values": {
            "status": 2, "error_code": 2, "error_details_json": details}},
    }]
    for row_id, original in enumerate((701, 705), 1):
        rows.append({
            "table": "obtain_items", "id": row_id, "before": {"exists": False},
            "after": {"exists": True, "values": {
                "selection_id": 61, "requested_output_id": None, "output_id": None,
                "basis": 3, "original_output_id": original, "preview_output_id": None,
                "preview_size": None, "repaired_size": None, "status": 4,
                "source_dependency": 0, "delivery_id": None, "error_code": 8,
                "error_details_json": {"source_action_instance_id": "11",
                                       "original_output_id": str(original)},
            }},
        })
    body = {"reason": 1, "evidence": {}, "rows": rows}
    event = decode_event_row((2, 2, 4, 1, _NOW, 1, None, json.dumps(body)))
    owners = {("obtain_source_selections", 61): ("action", 30),
              ("obtain_items", 1): ("action", 30), ("obtain_items", 2): ("action", 30)}
    return event, proposal_state(owned, owners=owners)


def _current_preview_proposal(owned):
    connection = owned.connection
    connection.execute("DELETE FROM output_origins WHERE output_id=702")
    connection.execute("DELETE FROM outputs WHERE id=702")
    connection.commit()
    _request(connection, SelectionMode.PREVIEW)
    facts = load_selection_facts(connection, 11, preview_requested=True)
    snapshot = select_outputs(_fixed_resolution((11,)), facts, SelectionMode.PREVIEW)
    assert snapshot.source_error_code is None
    proposal = capture_operation(owned, lambda actual: outputs.OutputsRepository().fix_selection(
        FixSelection(61, snapshot, _NOW), new_operation_key(), actual))
    event, = proposal.events
    assert event.evidence["preview_validation_version"] == 1
    matching = [row for row in event.rows if row.table == "obtain_items"
                and row.after.values.get("original_output_id") == 701]
    assert len(matching) == 1
    row, = matching
    assert row.after.values["error_code"] == item_error_id("obtain_items", "preview_missing")
    assert row.after.values["error_details_json"] == {
        "source_action_instance_id": "11", "original_output_id": "701"}
    return proposal, event, row


def _assert_preview_control(owned, proposal):
    def commit(actual):
        receipt = commit_operation(EventSequence(proposal.events, proposal), new_operation_key(), actual)
        assert receipt.kind == "completed", receipt.error
        rows = actual.connection.execute(
            "SELECT original_output_id,error_code,error_details_json FROM obtain_items"
            " WHERE selection_id=61 ORDER BY original_output_id").fetchall()
        assert [(identity, code, json.loads(details)) for identity, code, details in rows] == [
            (identity, item_error_id("obtain_items", "preview_missing"),
             {"source_action_instance_id": "11", "original_output_id": str(identity)})
            for identity in (701, 705)]
        assert actual.connection.execute(
            "SELECT status,error_code,error_details_json FROM obtain_source_selections WHERE id=61"
        ).fetchone() == (2, None, None)
        assert not actual.connection.in_transaction
        return receipt
    observed_operation(owned, commit)


@pytest.mark.parametrize("details", [
    {"source_action_instance_id": "12", "original_output_id": "701"},
    {"source_action_instance_id": "11", "original_output_id": "704"},
    {"source_action_instance_id": "11", "original_output_id": "703"},
    {"source_action_instance_id": "11", "original_output_id": "999"},
], ids=["other-source", "other-source-original", "repaired", "missing"])
def test_item_error_must_refer_to_actual_source_and_original(selection_database, details):
    owned = selection_database
    other = outputs.load_output_family(owned.connection, 704)
    assert other.source_action_id == 12 and other.original.output_id == 704
    source = outputs.load_output_family(owned.connection, 703)
    assert source.source_action_id == 11 and source.repaired.output_id == 703
    assert owned.connection.execute("SELECT id FROM outputs WHERE id=999").fetchone() is None
    proposal, event, row = _current_preview_proposal(owned)
    _assert_preview_control(owned, proposal)
    changed_row = replace(row, after=replace(
        row.after, values={**row.after.values, "error_details_json": details}))
    changed = replace(event, rows=tuple(changed_row if item is row else item for item in event.rows))
    before = tuple(owned.connection.iterdump())
    rejected = commit_operation(EventSequence((changed,), proposal), new_operation_key(), owned)
    assert rejected.kind == "rolled_back", rejected.error
    assert isinstance(rejected.error, EventValidationError), rejected.error
    assert "选择条目的完整集合、保存顺序或依据" in str(rejected.error)
    assert not owned.connection.in_transaction
    assert tuple(owned.connection.iterdump()) == before


def test_preview_item_accepts_actual_source_and_original(selection_database):
    proposal, _, _ = _current_preview_proposal(selection_database)
    _assert_preview_control(selection_database, proposal)


def test_no_outputs_accepts_empty_source_error_details(selection_database):
    owned = selection_database
    connection = owned.connection
    connection.execute("DELETE FROM output_origins")
    connection.execute("DELETE FROM outputs WHERE source_action_id=11")
    connection.commit()
    _request(connection, SelectionMode.DEFAULT)
    snapshot = select_outputs(_fixed_resolution((11,)), load_selection_facts(connection, 11),
                              SelectionMode.DEFAULT)
    assert snapshot.source_error_code == item_error_id("obtain_source_selections", "no_outputs")
    result = outputs.OutputsRepository().fix_selection(
        FixSelection(61, snapshot, _NOW), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    selection = row_facts(connection, "obtain_source_selections", 61)
    assert selection["error_code"] == snapshot.source_error_code
    assert selection["error_details_json"] == {}
    assert not connection.in_transaction


@pytest.mark.parametrize("details", [
    {"source_action_instance_id": "11"},
    {"source_action_instance_id": "11", "original_output_id": "701"},
])
def test_legacy_source_error_accepts_registered_details_for_actual_source(selection_database, details):
    event, proposal = _legacy_source_error_event(selection_database, details)
    receipt = commit_operation(EventSequence((event,), proposal), new_operation_key(), selection_database)
    assert receipt.kind == "completed", receipt.error
    saved = outputs.load_selection(selection_database.connection, 61)
    assert saved.source_error_code == 2
    assert [(item.original_output_id, item.error_code) for item in saved.items] == [(701, 8), (705, 8)]
    assert row_facts(selection_database.connection, "obtain_source_selections", 61)["error_details_json"] == details
    version, body = selection_database.connection.execute(
        "SELECT event_version,body_json FROM history_events ORDER BY id DESC LIMIT 1").fetchone()
    body = json.loads(body)
    assert version == 1 and body["evidence"] == {}
    assert len(body["rows"]) == 3
    assert not selection_database.connection.in_transaction


@pytest.mark.parametrize("details", [
    {"source_action_instance_id": "12", "original_output_id": "701"},
    {"source_action_instance_id": "11", "original_output_id": "704"},
    {"source_action_instance_id": "11", "original_output_id": "703"},
    {"source_action_instance_id": "11", "original_output_id": "999"},
], ids=["other-source", "other-source-original", "repaired", "missing"])
def test_legacy_source_error_rejects_invalid_references(selection_database, details):
    owned = selection_database
    other = outputs.load_output_family(owned.connection, 704)
    assert other.source_action_id == 12 and other.original.output_id == 704
    source = outputs.load_output_family(owned.connection, 703)
    assert source.source_action_id == 11 and source.repaired.output_id == 703
    assert owned.connection.execute("SELECT id FROM outputs WHERE id=999").fetchone() is None
    event, proposal = _legacy_source_error_event(owned, details)
    valid_row = event.rows[0]
    valid_row = replace(valid_row, after=replace(valid_row.after, values={
        **valid_row.after.values, "error_details_json": {"source_action_instance_id": "11"}}))
    valid = replace(event, rows=(valid_row, *event.rows[1:]))
    def control(actual):
        result = commit_operation(EventSequence((valid,), proposal), new_operation_key(), actual)
        assert result.kind == "completed", result.error
    observed_operation(owned, control)
    before = tuple(owned.connection.iterdump())
    receipt = commit_operation(EventSequence((event,), proposal), new_operation_key(), owned)
    assert receipt.kind == "rolled_back", receipt.error
    assert isinstance(receipt.error, EventValidationError), receipt.error
    expected = ("来源错误必须指向实际固定来源" if details["source_action_instance_id"] == "12"
                else "来源错误中的原片必须属于实际固定来源")
    assert expected in str(receipt.error)
    assert not owned.connection.in_transaction
    assert tuple(owned.connection.iterdump()) == before

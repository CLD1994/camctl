"""首次选择只能保存原请求与当前可靠事实共同确定的完整结果。"""

from dataclasses import replace
import json
import sqlite3

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.outputs.sources import SelectionMode, SelectionSnapshot, SelectedItem, select_outputs
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories import outputs

from .test_catalog_integrity import CatalogConnection, selection_database
from .test_output_family import family_database
from .test_sources import _NOW, _fixed_resolution
from .test_selection_lookup import LookupConnection
from ..operations.test_result_reuse import _FaultConnection


def _request(connection, mode, ids=()):
    params = {"source": {"action_instance_id": "11"}}
    if mode == SelectionMode.EXPLICIT_IDS:
        params["output_ids"] = [str(identity) for identity in ids]
    elif mode == SelectionMode.PREVIEW:
        params["filter"] = "preview"
    connection.execute("UPDATE actions SET execution_spec_json=?, input_fields_json=? WHERE id=30",
                       (json.dumps({"selection_mode": int(mode)}), json.dumps({"params": params})))
    connection.commit()


def _snapshot(connection, mode=SelectionMode.DEFAULT, ids=()):
    return select_outputs(_fixed_resolution((11,)),
                          outputs.load_selection_facts(connection, 11, requested_output_ids=ids), mode, ids)


def _reject(owned, snapshot):
    before = tuple(owned.connection.iterdump())
    result = outputs.OutputsRepository().fix_selection(
        outputs.FixSelection(61, snapshot, _NOW), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result
    assert tuple(owned.connection.iterdump()) == before
    return result


@pytest.mark.parametrize("mode,ids,selected", [
    (SelectionMode.DEFAULT, (), (703, 705)),
    (SelectionMode.PREVIEW, (), (703,)),
    (SelectionMode.EXPLICIT_IDS, (705, 999, 704, 702), (705, 702)),
])
def test_matching_request_saves_complete_selection(selection_database, mode, ids, selected):
    owned = selection_database
    _request(owned.connection, mode, ids)
    result = outputs.OutputsRepository().fix_selection(
        outputs.FixSelection(61, _snapshot(owned.connection, mode, ids), _NOW), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.snapshot.selected_output_ids == selected
    if ids:
        assert [(item.requested_output_id, item.error_code) for item in result.value.snapshot.items] == [
            (705, None), (999, 1), (704, 2), (702, None)]


def test_default_request_cannot_save_preview_selection(selection_database):
    _request(selection_database.connection, SelectionMode.DEFAULT)
    _reject(selection_database, _snapshot(selection_database.connection, SelectionMode.PREVIEW))


@pytest.mark.parametrize("change", ["omit", "reorder", "boolean_basis", "numeric_fixed"])
def test_default_snapshot_keeps_complete_ordered_typed_facts(selection_database, change):
    _request(selection_database.connection, SelectionMode.DEFAULT)
    snapshot = _snapshot(selection_database.connection)
    if change == "omit":
        snapshot = replace(snapshot, items=snapshot.items[:1])
    elif change == "reorder":
        snapshot = replace(snapshot, items=tuple(reversed(snapshot.items)))
    elif change == "boolean_basis":
        snapshot = replace(snapshot, items=(snapshot.items[0], replace(snapshot.items[1], basis=True)))
    else:
        snapshot = replace(snapshot, is_fixed=1)
    _reject(selection_database, snapshot)


@pytest.mark.parametrize("proposed", [(701,), (705, 701), (701, 702), (701, 705, 702)])
def test_explicit_selection_preserves_exact_requested_list(selection_database, proposed):
    _request(selection_database.connection, SelectionMode.EXPLICIT_IDS, (701, 705))
    _reject(selection_database, _snapshot(selection_database.connection, SelectionMode.EXPLICIT_IDS, proposed))


@pytest.mark.parametrize("change", [
    {"original_output_id": 705}, {"preview_size": 101}, {"repaired_size": 79},
    {"basis": 2}, {"requested_output_id": 703},
])
def test_preview_selection_preserves_comparison_and_identity(selection_database, change):
    _request(selection_database.connection, SelectionMode.PREVIEW)
    snapshot = _snapshot(selection_database.connection, SelectionMode.PREVIEW)
    _reject(selection_database, replace(snapshot, items=(replace(snapshot.items[0], **change), *snapshot.items[1:])))


def test_nonempty_default_catalog_cannot_be_saved_as_no_outputs(selection_database):
    _request(selection_database.connection, SelectionMode.DEFAULT)
    _reject(selection_database, SelectionSnapshot(is_fixed=True, source_error_code=1))


def test_missing_explicit_target_cannot_be_saved_as_source_mismatch(selection_database):
    _request(selection_database.connection, SelectionMode.EXPLICIT_IDS, (999,))
    _reject(selection_database, SelectionSnapshot(is_fixed=True, items=(
        SelectedItem(basis=5, status=4, requested_output_id=999, error_code=2,
                     error_details={"requested_output_id": "999"}),)))


def test_changed_availability_requires_new_proposal(selection_database):
    owned = selection_database
    _request(owned.connection, SelectionMode.DEFAULT)
    snapshot = _snapshot(owned.connection)
    owned.connection.execute("UPDATE outputs SET availability=3, cleanup_status=4 WHERE id=703")
    owned.connection.commit()
    _reject(owned, snapshot)


@pytest.mark.parametrize("sql", [
    "UPDATE actions SET execution_spec_json='{}' WHERE id=30",
    "UPDATE actions SET execution_spec_json='{\"selection_mode\": true}' WHERE id=30",
    "UPDATE actions SET source_resolution_state=1, resolved_source_plan_id=NULL WHERE id=30",
    "UPDATE actions SET resolved_source_plan_id=2 WHERE id=30",
])
def test_invalid_saved_selection_authority_never_uses_proposal_as_fallback(selection_database, sql):
    owned = selection_database
    _request(owned.connection, SelectionMode.DEFAULT)
    snapshot = _snapshot(owned.connection)
    owned.connection.execute(sql)
    owned.connection.commit()
    _reject(owned, snapshot)


@pytest.mark.parametrize("raw", [{}, {"params": {}}, {"params": {"output_ids": []}},
    {"params": {"output_ids": [701]}}, {"params": {"output_ids": ["701", "701"]}}])
def test_invalid_original_explicit_list_cannot_be_replaced_by_snapshot(selection_database, raw):
    owned = selection_database
    _request(owned.connection, SelectionMode.EXPLICIT_IDS, (701,))
    snapshot = _snapshot(owned.connection, SelectionMode.EXPLICIT_IDS, (701,))
    owned.connection.execute("UPDATE actions SET input_fields_json=? WHERE id=30", (json.dumps(raw),))
    owned.connection.commit()
    _reject(owned, snapshot)


@pytest.mark.parametrize("mode,params", [
    (SelectionMode.DEFAULT, {"output_ids": ["701"]}),
    (SelectionMode.PREVIEW, {"output_ids": ["701"]}),
    (SelectionMode.DEFAULT, {"filter": "preview"}),
    (SelectionMode.PREVIEW, {}),
    (SelectionMode.EXPLICIT_IDS, {"output_ids": ["701"], "filter": "default"}),
])
def test_saved_mode_conflicting_with_original_selector_cannot_be_fixed(selection_database, mode, params):
    owned = selection_database
    _request(owned.connection, mode, (701,) if mode == SelectionMode.EXPLICIT_IDS else ())
    snapshot = _snapshot(owned.connection, mode, (701,) if mode == SelectionMode.EXPLICIT_IDS else ())
    owned.connection.execute("UPDATE actions SET input_fields_json=? WHERE id=30",
                             (json.dumps({"params": params}),))
    owned.connection.commit()
    _reject(owned, snapshot)


@pytest.mark.parametrize("status,output_id,requested,basis,error,details", [
    (2, 701, None, 1, None, None),
    (1, None, 999, 5, None, None),
    (4, None, 999, 5, 1, '{"requested_output_id":"999"}'),
])
def test_pending_selection_cannot_append_to_existing_items(
    selection_database, status, output_id, requested, basis, error, details,
):
    owned = selection_database
    _request(owned.connection, SelectionMode.DEFAULT)
    snapshot = _snapshot(owned.connection)
    owned.connection.execute(
        "INSERT INTO obtain_items (id, selection_id, requested_output_id, output_id, basis,"
        " original_output_id, preview_output_id, preview_size, repaired_size, status,"
        " source_dependency, delivery_id, error_code, error_details_json)"
        " VALUES (99, 61, ?, ?, ?, NULL, NULL, NULL, NULL, ?, 0, NULL, ?, ?)",
        (requested, output_id, basis, status, error, details))
    owned.connection.commit()
    result = _reject(owned, snapshot)
    assert isinstance(result.error, ConsistencyError), result.error


@pytest.mark.parametrize("state", ["empty", "present", "execute", "fetch"])
def test_pending_item_query_closes_cursor_and_propagates_failure(selection_database, state):
    owned = selection_database
    _request(owned.connection, SelectionMode.DEFAULT)
    snapshot = _snapshot(owned.connection)
    if state == "present":
        owned.connection.execute("INSERT INTO obtain_items"
            " (id,selection_id,output_id,basis,status,source_dependency) VALUES (99,61,701,1,2,0)")
        owned.connection.commit()
    probe = CatalogConnection(owned.connection, state, "pending_items")
    before = tuple(owned.connection.iterdump())
    result = outputs.OutputsRepository().fix_selection(outputs.FixSelection(61, snapshot, _NOW),
        new_operation_key(), replace(owned, connection=probe))
    assert result.kind is (DbOutcomeKind.COMPLETED if state == "empty" else DbOutcomeKind.ROLLED_BACK)
    if state in ("execute", "fetch"):
        assert result.error is probe.error
    if state == "present":
        assert isinstance(result.error, ConsistencyError), result.error
    if state != "empty":
        assert tuple(owned.connection.iterdump()) == before
    assert len(probe.cursors) == (0 if state == "execute" else 1)
    for cursor in probe.cursors:
        cursor.close.assert_called_once_with()


@pytest.mark.parametrize("stage", ["execute", "fetch"])
@pytest.mark.parametrize("fail_at", [1, 2])
def test_first_fix_lookup_failure_rolls_back_without_partial_selection(selection_database, stage, fail_at):
    owned = selection_database
    ids = tuple(range(1000, 1260))
    _request(owned.connection, SelectionMode.EXPLICIT_IDS, ids)
    snapshot = _snapshot(owned.connection, SelectionMode.EXPLICIT_IDS, ids)
    probe = LookupConnection(owned.connection, fail_at=fail_at, stage=stage)
    before = tuple(owned.connection.iterdump())
    result = outputs.OutputsRepository().fix_selection(outputs.FixSelection(61, snapshot, _NOW),
        new_operation_key(), replace(owned, connection=probe))
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert result.error is probe.error
    assert tuple(owned.connection.iterdump()) == before
    for cursor in probe.cursors:
        cursor.close.assert_called_once_with()


@pytest.mark.parametrize("sql", ["UPDATE obtain_source_selections", "INSERT INTO obtain_items",
    "INSERT INTO history_events", "INSERT INTO entity_event_links"])
def test_first_fix_write_failure_preserves_retryable_original_state(selection_database, sql):
    owned = selection_database
    _request(owned.connection, SelectionMode.DEFAULT)
    command = outputs.FixSelection(61, _snapshot(owned.connection), _NOW)
    repository = outputs.OutputsRepository()
    key = new_operation_key()
    before = tuple(owned.connection.iterdump())
    result = repository.fix_selection(command, key, replace(owned, connection=_FaultConnection(owned.connection, sql)))
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, sqlite3.OperationalError), result.error
    assert tuple(owned.connection.iterdump()) == before
    retried = repository.fix_selection(command, key, owned)
    assert retried.kind is DbOutcomeKind.COMPLETED, retried.error
    assert retried.value.snapshot.selected_output_ids == (703, 705)

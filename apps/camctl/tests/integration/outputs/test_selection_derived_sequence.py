"""派生产物与原片关联共同进入当前事实；选择保留当时的比较依据。"""

from contextlib import closing
from dataclasses import replace
import sqlite3

import pytest

from camctl.contracts.values import new_operation_key
from camctl.history import validators
from camctl.history.events import business_columns
from camctl.history.reads import ReadCoverage
from camctl.history.validators import EventValidationError
from camctl.outputs.sources import SelectionMode
from camctl.persistence.repositories import outputs
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.transaction import commit_operation, event_envelope, row_change, row_facts, update_change

from .test_selection_guard import _proposal, _NOW, selection_database, family_database
from .test_selection_event_sequence import _Sequence
from .test_qualification import _seed_device_file
from ..operations.test_result_reuse import _FaultConnection


# 新产物类型、长度、既有配套产物类型及长度。均属于原片705。
_CASES = {
    "preview_only": (3, 100, None, None),
    "repair_only": (2, 80, None, None),
    "preview_after_repair": (3, 100, 2, 80),
    "smaller_preview_after_repair": (3, 60, 2, 80),
    "repair_after_preview": (2, 80, 3, 100),
    "larger_repair_after_preview": (2, 120, 3, 100),
}


def _file(connection, identity, kind, size, *, pending_repair: bool = False):
    if kind == 3:
        file_id = identity - 400
        _seed_device_file(connection, file_id, 11)
        connection.execute("UPDATE device_files SET role=3, original_device_file_id=505,"
                           " pairing_evidence_json='{}', size_bytes=? WHERE id=?", (size, file_id))
        return "device_files", file_id
    file_id = identity - 100
    connection.execute(
        "INSERT INTO intermediate_files (id, owner_action_id, purpose, relative_path, retention_state,"
        " cleanup_state, size_bytes, sha256, created_event_id, last_event_id, change_count)"
        " VALUES (?, 11, 4, ?, ?, 1, ?, ?, 1, 1, 1)",
        (file_id, f"derived/{file_id}.mp4", 1 if pending_repair else 3, size, "e" * 64))
    if pending_repair:
        connection.execute(
            "INSERT INTO recording_processing (id, action_id, source_device_file_id,"
            " check_state, check_decision, check_basis_json, media_json, repair_state,"
            " repair_basis_json, repair_output_file_id, repair_error_json,"
            " discard_state, discard_error_json)"
            " VALUES (1, 11, NULL, 3, 3, '{}', '{}', 5, '{}', ?, NULL, 1, NULL)",
            (file_id,),
        )
    return "intermediate_files", file_id


def _output(connection, identity, kind, file_id):
    connection.execute(
        "INSERT INTO outputs (id, source_action_id, kind, device_file_id, intermediate_file_id,"
        " original_name, media_type, availability, cleanup_status, cleanup_error_json, media_json,"
        " error_json, created_event_id, last_event_id, change_count)"
        " VALUES (?, 11, ?, ?, ?, 'derived.mp4', 'video/mp4', 1, 1, NULL, '{}', NULL, 1, 1, 1)",
        (identity, kind, file_id if kind == 3 else None, file_id if kind == 2 else None))
    with closing(connection.execute(
        "INSERT INTO output_origins (output_id, original_output_id) VALUES (?, 705) RETURNING id", (identity,)
    )) as cursor:
        return cursor.fetchone()[0]


def _scenario(owned, mode, case):
    connection = owned.connection
    kind, size, previous_kind, previous_size = _CASES[case]
    if previous_kind is not None:
        _, previous_file = _file(connection, 998, previous_kind, previous_size)
        _output(connection, 998, previous_kind, previous_file)
    file_table, file_id = _file(
        connection, 999, kind, size, pending_repair=kind == 2)
    connection.commit()
    initial, current = _proposal(owned, mode)
    current.state_rows["plans"] = {1: row_facts(connection, "plans", 1)}
    reads = outputs._CatalogReads(connection)
    reads.required(file_table, file_id)
    assert reads.origin(999) is None
    ranges = dict(current.read_coverage.ranges)
    for key, identities in reads.read_coverage().ranges.items():
        ranges[key] = ranges.get(key, frozenset()) | identities
    for table, rows in reads.state_rows.items():
        current.state_rows.setdefault(table, {}).update(rows)
    current = replace(current, read_coverage=ReadCoverage(ranges))
    origin_id = _output(connection, 999, kind, file_id)
    if kind == 2:
        # 变化后提案按一致状态预跑：登记后的修复承载文件已提升。
        connection.execute("UPDATE intermediate_files SET retention_state=3 WHERE id=?", (file_id,))
    connection.commit()
    rows = []
    for table, identity in (("outputs", 999), ("output_origins", origin_id)):
        facts = row_facts(connection, table, identity)
        rows.append(row_change(table, identity, {column: facts[column] for column in business_columns(table)}))
        current.owners[table, identity] = ("output", 999)
    changed, future = _proposal(owned, mode)
    current.owners.update(future.owners)
    connection.execute("DELETE FROM output_origins WHERE id=?", (origin_id,))
    connection.execute("DELETE FROM outputs WHERE id=999")
    registration_events = [event_envelope(2, 2, 20, kind, tuple(rows), _NOW)]
    if kind == 2:
        # 登记事件时刻的事实：承载文件未提升、本动作修复成功。
        connection.execute("UPDATE intermediate_files SET retention_state=1 WHERE id=?", (file_id,))
        current.state_rows["intermediate_files"][file_id]["retention_state"] = 1
        current.state_rows.setdefault("recording_processing", {})[1] = row_facts(
            connection, "recording_processing", 1)
        current.owners["intermediate_files", file_id] = ("intermediate_file", file_id)
        registration_events.append(event_envelope(2, 2, 26, 2, (
            update_change("intermediate_files", file_id,
                          {"retention_state": 1}, {"retention_state": 3}),), _NOW))
    connection.commit()
    return initial, changed, tuple(registration_events), current


def _changes_selection(case, mode):
    if mode is SelectionMode.EXPLICIT_IDS:
        return True
    kind, _, previous_kind, _ = _CASES[case]
    if mode is SelectionMode.DEFAULT:
        return kind == 2
    return kind == 3 or previous_kind == 3


@pytest.mark.parametrize("mode", list(SelectionMode))
@pytest.mark.parametrize("case", _CASES)
@pytest.mark.parametrize("registration_first", [False, True])
@pytest.mark.parametrize("select_changed", [False, True])
def test_derived_registration_and_origin_take_effect_together(
    selection_database, monkeypatch, mode, case, registration_first, select_changed,
):
    monkeypatch.setattr(validators, "NAMED_GUARDS", dict(validators.NAMED_GUARDS))
    register_capture_guards()
    initial, changed, registrations, context = _scenario(selection_database, mode, case)
    selection = changed if select_changed else initial
    events = (*registrations, selection) if registration_first else (selection, *registrations)
    before = tuple(selection_database.connection.iterdump())
    seen = []
    original = validators.NAMED_GUARDS["source_selection"]

    def inspect(event, current):
        seen.append((999 in current.state_rows["outputs"], any(
            row["output_id"] == 999 for row in current.state_rows["output_origins"].values())))
        original(event, current)

    monkeypatch.setitem(validators.NAMED_GUARDS, "source_selection", inspect)
    receipt = commit_operation(_Sequence(events, context), new_operation_key(), selection_database)
    assert seen == [(registration_first, registration_first)], receipt.error
    if registration_first != select_changed and _changes_selection(case, mode):
        assert receipt.kind == "rolled_back", receipt.error
        assert isinstance(receipt.error, EventValidationError), receipt.error
        assert tuple(selection_database.connection.iterdump()) == before
    else:
        assert receipt.kind == "completed", receipt.error
        items = outputs._saved_items(selection_database.connection, 61)
        if mode is SelectionMode.EXPLICIT_IDS:
            item = next(item for item in items if item.requested_output_id == 999)
            assert (item.output_id, item.error_code) == ((999, None) if registration_first else (None, 1))
        elif mode is SelectionMode.DEFAULT:
            expected = (999 if registration_first and _CASES[case][0] == 2
                        else 998 if _CASES[case][2] == 2 else 705)
            assert tuple(item.output_id for item in items) == (703, expected)
        else:
            item = next(item for item in items if item.original_output_id == 705)
            before_id = 998 if _CASES[case][2] == 3 else None
            after_id = {
                "preview_only": 999, "repair_only": None, "preview_after_repair": 998,
                "smaller_preview_after_repair": 999, "repair_after_preview": 999,
                "larger_repair_after_preview": 998,
            }[case]
            assert item.output_id == (after_id if registration_first else before_id)
            if registration_first and _CASES[case][2] is not None:
                preview_size, repaired_size = {
                    "preview_after_repair": (100, 80), "smaller_preview_after_repair": (60, 80),
                    "repair_after_preview": (100, 80), "larger_repair_after_preview": (100, 120),
                }[case]
                assert (item.preview_size, item.repaired_size) == (preview_size, repaired_size)


@pytest.mark.parametrize("kind", ["preview_only", "repair_only"])
@pytest.mark.parametrize("failure", ["INSERT INTO output_origins", "INSERT INTO obtain_items"])
def test_derived_registration_and_selection_rollback_together(selection_database, monkeypatch, kind, failure):
    monkeypatch.setattr(validators, "NAMED_GUARDS", dict(validators.NAMED_GUARDS))
    register_capture_guards()
    _, selection, registrations, context = _scenario(selection_database, SelectionMode.EXPLICIT_IDS, kind)
    before = tuple(selection_database.connection.iterdump())
    target = replace(selection_database, connection=_FaultConnection(selection_database.connection, failure))
    receipt = commit_operation(_Sequence((*registrations, selection), context), new_operation_key(), target)
    assert receipt.kind == "rolled_back", receipt.error
    assert isinstance(receipt.error, sqlite3.OperationalError), receipt.error
    assert tuple(selection_database.connection.iterdump()) == before

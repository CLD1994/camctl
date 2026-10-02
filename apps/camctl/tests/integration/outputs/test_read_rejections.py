"""新取回按可靠清理及文件事实保存拒绝，暂未确认只等待。"""

from dataclasses import replace
from contextlib import closing
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.contracts.workflow_errors import registered_error_spec, validate_error_details
from camctl.outputs.qualification import QualificationOutcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.transaction import row_facts

from .test_read_associations import read_targets, _command
from .test_local_read import local_read
from .test_qualification import _seed_action
from ..operations.test_result_reuse import _FaultConnection


@pytest.fixture(params=["device", "host"])
def target(request):
    if request.param == "host":
        owned, command = request.getfixturevalue("local_read")
    else:
        owned, command = request.getfixturevalue("read_targets"), _command()
    owned.connection.execute("UPDATE actions SET status=3 WHERE id=11")
    owned.connection.commit()
    return owned, command


def _availability(connection, command, value, cleanup=1):
    connection.execute(
        "UPDATE outputs SET availability=?, cleanup_status=?, cleanup_error_json=?, error_json=? WHERE id=701",
        (value, cleanup, '{"code":"delete_unconfirmed"}' if cleanup == 5 else None,
         '{"code":"file_unconfirmed"}' if value in (4, 5) else None),
    )
    if command.source_device_file_id is not None:
        presence = {1: 2, 2: 2, 3: 3, 4: 3, 5: 1}[value]
        connection.execute("UPDATE device_files SET presence_state=? WHERE id=501", (presence,))


def _cleanup(connection, status, restriction, identity=1):
    action_id = 40 + identity
    _seed_action(connection, action_id, 1, action_type=5)
    ended = status in (4, 5, 6)
    has_error = status == 5 or (status == 6 and restriction == 4)
    connection.execute(
        "INSERT INTO cleanup_items (id, action_id, requested_output_id, output_id, status,"
        " restriction_state, outcome, final_event_id, error_code, error_details_json)"
        " VALUES (?, ?, 701, 701, ?, ?, ?, ?, ?, ?)",
        (identity, action_id, status, restriction, 1 if status == 4 else None,
         1 if ended else None, 6 if has_error else None,
         '{"output_id":"701"}' if has_error else None),
    )


@pytest.mark.parametrize("status,restriction,availability,cleanup,code", [
    (2, 2, 2, 2, 4), (3, 2, 2, 3, 4), (3, 4, 2, 3, 4),
    (5, 2, 2, 5, 4), (5, 4, 2, 5, 4), (6, 4, 2, 5, 4),
    (4, 4, 3, 4, 3),
])
def test_effective_cleanup_facts_determine_final_rejection(target, status, restriction, availability, cleanup, code):
    owned, command = target
    _cleanup(owned.connection, status, restriction)
    _availability(owned.connection, command, availability, cleanup)
    owned.connection.commit()
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is QualificationOutcome.REJECTED_FINAL
    item = row_facts(owned.connection, "obtain_items", 101)
    assert item["status"] == 4
    assert item["error_code"] == code
    expected = {"output_id": "701"}
    if code == 3:
        expected["availability"] = "cleaned"
    assert item["error_details_json"] == expected
    name, _ = registered_error_spec("item_error_ids.obtain_items", code)
    validate_error_details(name, item["error_details_json"])
    assert item["delivery_id"] is None
    assert item["source_dependency"] == 0


def test_missing_file_saves_actual_availability(target):
    owned, command = target
    _availability(owned.connection, command, 4)
    owned.connection.commit()
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is QualificationOutcome.REJECTED_FINAL
    item = row_facts(owned.connection, "obtain_items", 101)
    assert item["error_code"] == 3
    assert item["error_details_json"] == {"output_id": "701", "availability": "missing"}
    validate_error_details("output_unavailable", item["error_details_json"])


def test_unknown_file_waits_without_final_error_or_new_records(target):
    owned, command = target
    _availability(owned.connection, command, 5)
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is QualificationOutcome.REJECTED
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("status,restriction,cleanup", [(1, 1, 1), (5, 1, 1), (6, 1, 1), (6, 3, 6)])
def test_unestablished_or_released_restriction_does_not_reject(target, status, restriction, cleanup):
    owned, command = target
    _cleanup(owned.connection, status, restriction)
    _availability(owned.connection, command, 1, cleanup)
    owned.connection.commit()
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is QualificationOutcome.GRANTED


@pytest.mark.parametrize("facts,availability,cleanup,code", [
    ([(5, 4), (6, 3)], 2, 5, 4),
    ([(2, 2), (3, 4)], 2, 3, 4),
    ([(4, 4), (2, 2)], 3, 4, 3),
])
def test_all_effective_restrictions_contribute_to_priority(target, facts, availability, cleanup, code):
    owned, command = target
    for index, (status, restriction) in enumerate(facts, 1):
        _cleanup(owned.connection, status, restriction, index)
    _availability(owned.connection, command, availability, cleanup)
    owned.connection.commit()
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert row_facts(owned.connection, "obtain_items", 101)["error_code"] == code


@pytest.mark.parametrize("fact,availability,cleanup", [
    (None, 2, 2), (None, 3, 4), ((2, 2), 1, 1),
    ((4, 4), 2, 2), ((2, 2), 2, 3),
])
def test_inconsistent_cleanup_projection_rolls_back(target, fact, availability, cleanup):
    owned, command = target
    if fact is not None:
        _cleanup(owned.connection, *fact)
    _availability(owned.connection, command, availability, cleanup)
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ConsistencyError), result.error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("availability,presence", [(1, 1), (1, 3), (4, 2), (5, 2)])
def test_device_presence_must_agree_with_unrestricted_output(read_targets, availability, presence):
    owned, command = read_targets, _command()
    owned.connection.execute("UPDATE actions SET status=3 WHERE id=11")
    _availability(owned.connection, command, availability)
    owned.connection.execute("UPDATE device_files SET presence_state=? WHERE id=501", (presence,))
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ConsistencyError), result.error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("prefix", ["SELECT COUNT(*)", "UPDATE obtain_items SET"])
def test_rejection_query_or_write_failure_preserves_all_original_facts(target, prefix):
    owned, command = target
    _cleanup(owned.connection, 5, 4)
    _availability(owned.connection, command, 2, 5)
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    fault = replace(owned, connection=_FaultConnection(owned.connection, prefix))
    result = OutputsRepository().grant_file(command, new_operation_key(), fault)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, sqlite3.OperationalError), result.error
    assert tuple(owned.connection.iterdump()) == before


class _RestrictionConnection(_FaultConnection):
    def __init__(self, connection, fail_fetch=False):
        super().__init__(connection, "unused")
        self.fail_fetch = fail_fetch
        self.query = None
        self.cursor = None

    def execute(self, sql, parameters=()):
        cursor = self._connection.execute(sql, parameters)
        if sql.startswith("SELECT COUNT(*)") and "cleanup_items" in sql:
            self.query = sql, parameters
            self.cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
            self.cursor.close.side_effect = cursor.close
            self.cursor.fetchone.side_effect = (sqlite3.OperationalError("restriction read failed")
                                               if self.fail_fetch else cursor.fetchone)
            return self.cursor
        return cursor


@pytest.mark.parametrize("restricted", [False, True])
@pytest.mark.parametrize("fail_fetch", [False, True])
def test_cleanup_query_closes_cursor_on_empty_result_success_and_read_failure(target, restricted, fail_fetch):
    owned, command = target
    if restricted:
        _cleanup(owned.connection, 5, 4)
        _availability(owned.connection, command, 2, 5)
        owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    probe = _RestrictionConnection(owned.connection, fail_fetch)
    result = OutputsRepository().grant_file(command, new_operation_key(), replace(owned, connection=probe))
    probe.cursor.close.assert_called_once_with()
    if fail_fetch:
        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert isinstance(result.error, sqlite3.OperationalError)
        assert tuple(owned.connection.iterdump()) == before
    else:
        assert result.kind is DbOutcomeKind.COMPLETED, result.error


def test_cleanup_query_searches_current_output_index(target):
    owned, command = target
    probe = _RestrictionConnection(owned.connection)
    result = OutputsRepository().grant_file(command, new_operation_key(), replace(owned, connection=probe))
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    sql, parameters = probe.query
    with closing(owned.connection.execute("EXPLAIN QUERY PLAN " + sql, parameters)) as cursor:
        plan = cursor.fetchall()
    assert any("SEARCH cleanup_items" in row[3] and "output_id=?" in row[3] for row in plan), plan


@pytest.mark.parametrize("completion,presence,expected", [
    (1, 2, QualificationOutcome.REJECTED), (2, 2, QualificationOutcome.REJECTED),
    (3, 1, QualificationOutcome.REJECTED), (4, 2, QualificationOutcome.REJECTED_FINAL),
    (4, 1, QualificationOutcome.REJECTED_FINAL), (3, 3, QualificationOutcome.REJECTED_FINAL),
])
def test_internal_source_distinguishes_pending_and_final_confirmation(read_targets, completion, presence, expected):
    owned = read_targets
    owned.connection.execute("DELETE FROM obtain_items")
    owned.connection.execute("DELETE FROM outputs")
    owned.connection.execute(
        "UPDATE device_files SET completion_state=?, presence_state=?, size_bytes=?,"
        " completion_evidence_json=?, last_error_json=? WHERE id=501",
        (completion, presence, 4096 if completion == 3 else None,
         '{}' if completion in (3, 4) else None,
         '{"reason":"confirmation_exhausted"}' if completion == 4 else None),
    )
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().grant_file(_command(internal=True), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is expected
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("completion", [1, 2, 4])
@pytest.mark.parametrize("canceled", [False, True])
def test_formal_device_output_requires_immutable_completion_before_status(read_targets, completion, canceled):
    owned = read_targets
    owned.connection.execute("UPDATE actions SET status=3 WHERE id=11")
    owned.connection.execute("UPDATE actions SET cancel_requested=? WHERE id=31", (int(canceled),))
    owned.connection.execute(
        "UPDATE device_files SET completion_state=?, size_bytes=NULL, completion_evidence_json=NULL,"
        " last_error_json=? WHERE id=501",
        (completion, '{"reason":"confirmation_exhausted"}' if completion == 4 else None),
    )
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().grant_file(_command(), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ConsistencyError), result.error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("availability,cleanup,status", [(2, 2, 2), (3, 4, 4)])
def test_restriction_or_completed_cleanup_precedes_later_presence_unknown(read_targets, availability, cleanup, status):
    owned, command = read_targets, _command()
    owned.connection.execute("UPDATE actions SET status=3 WHERE id=11")
    _cleanup(owned.connection, status, 4 if status == 4 else 2)
    _availability(owned.connection, command, availability, cleanup)
    owned.connection.execute("UPDATE device_files SET presence_state=1 WHERE id=501")
    owned.connection.commit()
    result = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is QualificationOutcome.REJECTED_FINAL
    assert row_facts(owned.connection, "obtain_items", 101)["error_code"] == (3 if status == 4 else 4)


def test_saved_rejection_does_not_reopen_after_restriction_is_released(target):
    owned, command = target
    _cleanup(owned.connection, 2, 2)
    _availability(owned.connection, command, 2, 2)
    owned.connection.commit()
    first = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    owned.connection.execute("UPDATE cleanup_items SET status=6, restriction_state=3, final_event_id=1 WHERE id=1")
    _availability(owned.connection, command, 1, 6)
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    later = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert later.kind is DbOutcomeKind.COMPLETED, later.error
    assert later.value.outcome is QualificationOutcome.REJECTED_FINAL
    assert tuple(owned.connection.iterdump()) == before

"""原操作键绑定首次选择的完整输入和响应，不受后续条目进度替换。"""

from dataclasses import replace
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.outputs.sources import SelectionMode, SelectionSnapshot
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories import outputs
from camctl.persistence.transaction import (
    CommandPlan, TransactionError, commit_operation, event_envelope, row_facts, update_change,
)

from .test_selection_authority import _request, _snapshot, _NOW, selection_database, family_database
from ..operations.test_result_reuse import _FaultConnection


def _save(owned, mode=SelectionMode.DEFAULT, ids=()):
    _request(owned.connection, mode, ids)
    command = outputs.FixSelection(61, _snapshot(owned.connection, mode, ids), _NOW)
    key = new_operation_key()
    result = outputs.OutputsRepository().fix_selection(command, key, owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    return command, key


def _reuse(owned, command, key):
    before = tuple(owned.connection.iterdump())
    result = outputs.OutputsRepository().fix_selection(command, key, owned)
    assert tuple(owned.connection.iterdump()) == before
    return result


@pytest.mark.parametrize("change", ["selection", "time", "fixed", "omit", "order", "source_error",
    "output", "requested", "basis", "boolean_basis", "original", "preview", "preview_size",
    "repaired_size", "status", "error", "details"])
def test_same_key_rejects_changed_original_input(selection_database, change):
    owned = selection_database
    command, key = _save(owned, SelectionMode.PREVIEW)
    snapshot = command.snapshot
    if change == "selection":
        command = replace(command, selection_id=999)
    elif change == "time":
        command = replace(command, occurred_at=_NOW + 1)
    elif change in ("fixed", "omit", "order", "source_error"):
        replacement = {
            "fixed": {"is_fixed": 1}, "omit": {"items": snapshot.items[:1]},
            "order": {"items": tuple(reversed(snapshot.items))}, "source_error": {"source_error_code": 1},
        }[change]
        command = replace(command, snapshot=replace(snapshot, **replacement))
    else:
        replacement = {
            "output": {"output_id": 702}, "requested": {"requested_output_id": 703},
            "basis": {"basis": 1}, "boolean_basis": {"basis": True},
            "original": {"original_output_id": 705}, "preview": {"preview_output_id": 705},
            "preview_size": {"preview_size": 101}, "repaired_size": {"repaired_size": 81},
            "status": {"status": 4}, "error": {"error_code": 3},
            "details": {"error_details": {"output_id": "703", "availability": "missing"}},
        }[change]
        command = replace(command, snapshot=replace(snapshot,
            items=(replace(snapshot.items[0], **replacement), *snapshot.items[1:])))
    result = _reuse(owned, command, key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result
    assert isinstance(result.error, TransactionError), result.error


class _Advance:
    def __init__(self, reason):
        self.reason = reason

    def plan(self, scope):
        identities = {"plans": (1,), "actions": (11, 30), "action_dependencies": (41,),
            "obtain_source_selections": (61,), "obtain_items": (1,), "outputs": (705,)}
        state = {table: {identity: row_facts(scope.connection, table, identity) for identity in ids}
                 for table, ids in identities.items()}
        after = ({"status": 2, "output_id": 705} if self.reason == 4 else {
            "status": 4, "error_code": 3,
            "error_details_json": {"output_id": "705", "availability": "missing"},
        })
        item = state["obtain_items"][1]
        allocation = scope.allocate(1)
        event = event_envelope(allocation.first_event_id, allocation.txn_id, 21, self.reason,
            (update_change("obtain_items", 1, {name: item[name] for name in after}, after),), _NOW + 1)
        return CommandPlan((event,), {("obtain_items", 1): ("action", 30)}, state)


@pytest.mark.parametrize("reason", [2, 4])
def test_original_response_survives_real_member_progress(selection_database, reason, monkeypatch):
    owned = selection_database
    if reason == 4:
        owned.connection.execute("UPDATE outputs SET availability=5, error_json='{}' WHERE id=705")
        owned.connection.commit()
    command, key = _save(owned, SelectionMode.EXPLICIT_IDS, (705,))
    owned.connection.execute("UPDATE outputs SET availability=?, error_json=? WHERE id=705",
                             (1 if reason == 4 else 4, None if reason == 4 else '{}'))
    owned.connection.commit()
    receipt = commit_operation(_Advance(reason), new_operation_key(), owned)
    assert receipt.kind == "completed", receipt.error
    monkeypatch.setattr(outputs, "select_outputs", create_autospec(outputs.select_outputs,
        side_effect=AssertionError("原键恢复不得采用当前目录重选")))
    same = _reuse(owned, command, key)
    assert same.kind is DbOutcomeKind.COMPLETED, same.error
    original = same.value.snapshot.items[0]
    assert (original.status, original.output_id, original.error_code) == (
        (1, None, None) if reason == 4 else (2, 705, None))
    current = _reuse(owned, replace(command, snapshot=SelectionSnapshot(False)), new_operation_key())
    assert current.kind is DbOutcomeKind.COMPLETED, current.error
    assert (current.value.snapshot.items[0].status, current.value.snapshot.items[0].output_id) == (
        (2, 705) if reason == 4 else (4, 705))


@pytest.mark.parametrize("sql", [
    "UPDATE obtain_source_selections SET status=1 WHERE id=61",
    "DELETE FROM obtain_items WHERE id=1",
    "UPDATE obtain_items SET id=999 WHERE id=1",
    "UPDATE obtain_items SET basis=2, preview_output_id=NULL, preview_size=NULL, repaired_size=NULL WHERE id=1",
    "UPDATE obtain_items SET output_id=702 WHERE id=1",
    "UPDATE obtain_items SET original_output_id=705 WHERE id=1",
    "UPDATE obtain_items SET preview_size=101 WHERE id=1",
    "DELETE FROM outputs WHERE id=703",
])
def test_current_fixed_facts_must_preserve_original_members(selection_database, sql):
    owned = selection_database
    command, key = _save(owned, SelectionMode.PREVIEW)
    owned.connection.execute(sql)
    owned.connection.commit()
    result = _reuse(owned, command, key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result
    assert isinstance(result.error, ConsistencyError), result.error


def test_committed_selection_with_lost_receipt_recovers_original_response(selection_database):
    owned = selection_database
    _request(owned.connection, SelectionMode.DEFAULT)
    command = outputs.FixSelection(61, _snapshot(owned.connection), _NOW)
    key = new_operation_key()

    class LostReceipt(_FaultConnection):
        def execute(self, sql, parameters=()):
            cursor = self._connection.execute(sql, parameters)
            if sql == "COMMIT":
                cursor.close()
                raise sqlite3.OperationalError("提交完成后回执丢失")
            return cursor

    unknown = outputs.OutputsRepository().fix_selection(
        command, key, replace(owned, connection=LostReceipt(owned.connection, "unused")))
    assert unknown.kind is DbOutcomeKind.UNKNOWN, unknown.error
    recovered = _reuse(owned, command, key)
    assert recovered.kind is DbOutcomeKind.COMPLETED, recovered.error
    assert recovered.value.snapshot.selected_output_ids == (703, 705)

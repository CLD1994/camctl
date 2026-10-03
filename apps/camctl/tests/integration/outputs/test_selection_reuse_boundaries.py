"""选择原键的事务身份、完整成员、故障恢复与游标责任。"""

from contextlib import closing
from dataclasses import replace
from decimal import Decimal
import json
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.history import validators
from camctl.outputs.sources import SelectionMode
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories import outputs
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.operations import register_operation_guards
from camctl.outputs.qualification import QualificationOutcome
from camctl.persistence.transaction import TransactionError, commit_operation

from .test_selection_reuse import _save, _reuse, _Advance, selection_database, family_database
from .test_selection_authority import _request, _snapshot, _NOW
from .test_selection_event_sequence import _scenarios, _Sequence
from .test_sources import _seed_output
from .test_read_associations import _command as _read_command
from ..operations.test_result_reuse import _FaultConnection


def test_key_cannot_address_another_existing_selection(selection_database):
    owned = selection_database
    command, key = _save(owned)
    owned.connection.execute("INSERT INTO action_dependencies VALUES (42, 30, 12)")
    owned.connection.execute("INSERT INTO obtain_source_selections VALUES (62, 42, 1, NULL, NULL)")
    owned.connection.commit()
    result = _reuse(owned, replace(command, selection_id=62), key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, TransactionError), result.error


def test_member_progress_key_is_not_a_selection_key(selection_database):
    owned = selection_database
    command, _ = _save(owned, SelectionMode.EXPLICIT_IDS, (705,))
    key = new_operation_key()
    receipt = commit_operation(_Advance(2), key, owned)
    assert receipt.kind == "completed", receipt.error
    result = _reuse(owned, command, key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, TransactionError), result.error


def test_original_selection_response_survives_real_delivery_creation(selection_database):
    owned = selection_database
    command, key = _save(owned, SelectionMode.EXPLICIT_IDS, (705,))
    register_operation_guards()
    candidate = replace(_read_command(), action_id=30, item_id=1, output_id=705, source_device_file_id=505)
    granted = outputs.OutputsRepository().grant_file(candidate, new_operation_key(), owned)
    assert granted.kind is DbOutcomeKind.COMPLETED, granted.error
    assert granted.value.outcome is QualificationOutcome.GRANTED
    result = _reuse(owned, command, key)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert (result.value.snapshot.items[0].status, result.value.snapshot.items[0].output_id) == (2, 705)
    with closing(owned.connection.execute("SELECT status, source_dependency, delivery_id FROM obtain_items WHERE id=1")) as cursor:
        assert cursor.fetchone() == (3, 1, granted.value.delivery_id)


def test_cleanup_branch_cannot_supply_original_selection_identity(selection_database):
    owned = selection_database
    command, key = _save(owned)
    # 注入可解释的另一个 TARGETS_FIXED 分支，验证按分支辨别原命令。
    body = {"reason": 2, "evidence": {}, "rows": [{"table": "actions", "id": 30,
        "before": {"exists": True, "values": {"target_selection_state": 1}},
        "after": {"exists": True, "values": {"target_selection_state": 2}}}]}
    owned.connection.execute("UPDATE history_events SET body_json=? WHERE id=2", (json.dumps(body),))
    owned.connection.commit()
    result = _reuse(owned, command, key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, TransactionError), result.error


def test_combined_transaction_is_not_the_original_fix_command(selection_database, monkeypatch):
    owned = selection_database
    monkeypatch.setattr(validators, "NAMED_GUARDS", dict(validators.NAMED_GUARDS))
    register_capture_guards()
    initial, _, observation, context = _scenarios(owned, SelectionMode.DEFAULT, "availability")
    command = outputs.FixSelection(61, _snapshot(owned.connection), _NOW)
    key = new_operation_key()
    receipt = commit_operation(_Sequence((initial, observation), context), key, owned)
    assert receipt.kind == "completed", receipt.error
    result = _reuse(owned, command, key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, TransactionError), result.error


@pytest.mark.parametrize("empty", [False, True])
def test_body_order_and_mathematically_equal_numbers_preserve_original_response(selection_database, empty):
    owned = selection_database
    if empty:
        owned.connection.execute("DELETE FROM output_origins")
        owned.connection.execute("DELETE FROM outputs WHERE source_action_id=11")
        owned.connection.commit()
    command, key = _save(owned, SelectionMode.PREVIEW)
    with closing(owned.connection.execute("SELECT body_json FROM history_events WHERE id=2")) as cursor:
        body = json.loads(cursor.fetchone()[0])
    body["rows"].reverse()
    owned.connection.execute("UPDATE history_events SET body_json=? WHERE id=2", (json.dumps(body),))
    owned.connection.commit()
    if not empty:
        first, *rest = command.snapshot.items
        command = replace(command, snapshot=replace(command.snapshot,
            items=(replace(first, preview_size=Decimal("100.00"), repaired_size=Decimal("80.0")), *rest)))
    result = _reuse(owned, command, key)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.snapshot.selected_output_ids == (() if empty else (703,))
    assert result.value.snapshot.source_error_code == (1 if empty else None)


@pytest.mark.parametrize("sql", [
    "UPDATE actions SET cancel_requested=1 WHERE id=30",
    "UPDATE actions SET status=3 WHERE id=30",
    "UPDATE outputs SET availability=4, error_json='{}' WHERE id=703",
    "UPDATE intermediate_files SET size_bytes=200 WHERE id=801",
])
def test_original_key_does_not_rejudge_current_eligibility_or_comparison(selection_database, sql):
    owned = selection_database
    command, key = _save(owned, SelectionMode.PREVIEW)
    owned.connection.execute(sql)
    owned.connection.commit()
    result = _reuse(owned, command, key)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    first = result.value.snapshot.items[0]
    assert (first.output_id, first.preview_size, first.repaired_size) == (703, 100, 80)


@pytest.mark.parametrize("fault", ["extra_member", "source_error", "failed_item", "missing_original", "missing_preview"])
def test_original_key_rejects_changed_fixed_collection(selection_database, fault):
    owned = selection_database
    command, key = _save(owned, SelectionMode.PREVIEW)
    sql = {
        "extra_member": "INSERT INTO obtain_items SELECT 999, selection_id, 704, 704, 5, NULL, NULL, NULL, NULL, 2, 0, NULL, NULL, NULL FROM obtain_items WHERE id=1",
        "source_error": "UPDATE obtain_source_selections SET error_code=1, error_details_json='{}' WHERE id=61",
        "failed_item": "UPDATE obtain_items SET error_details_json='{}' WHERE id=2",
        "missing_original": "DELETE FROM outputs WHERE id=701",
        "missing_preview": "DELETE FROM outputs WHERE id=702",
    }[fault]
    owned.connection.execute(sql)
    owned.connection.commit()
    result = _reuse(owned, command, key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ConsistencyError), result.error


def test_rolled_back_first_fix_can_retry_original_key(selection_database):
    owned = selection_database
    _request(owned.connection, SelectionMode.DEFAULT)
    command = outputs.FixSelection(61, _snapshot(owned.connection), _NOW)
    key = new_operation_key()
    before = tuple(owned.connection.iterdump())
    failed = outputs.OutputsRepository().fix_selection(command, key,
        replace(owned, connection=_FaultConnection(owned.connection, "INSERT INTO obtain_items")))
    assert failed.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(failed.error, sqlite3.OperationalError)
    assert tuple(owned.connection.iterdump()) == before
    result = outputs.OutputsRepository().fix_selection(command, key, owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.snapshot.selected_output_ids == (703, 705)


@pytest.mark.parametrize("state", ["unresolved", "canceled", "source_mismatch"])
def test_known_requested_output_cannot_disappear_without_actual_association(selection_database, state):
    owned = selection_database
    identity = 704 if state == "source_mismatch" else 705
    if state != "source_mismatch":
        owned.connection.execute("UPDATE outputs SET availability=5, error_json='{}' WHERE id=705")
        owned.connection.commit()
    command, key = _save(owned, SelectionMode.EXPLICIT_IDS, (identity,))
    assert command.snapshot.items[0].output_id is None
    # 当前投影表示原未决项已经取消；成员身份和实际空关联均保持。
    if state == "canceled":
        owned.connection.execute("UPDATE obtain_items SET status=5 WHERE id=1")
    owned.connection.execute("DELETE FROM outputs WHERE id=?", (identity,))
    owned.connection.commit()
    result = _reuse(owned, command, key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ConsistencyError), result.error


@pytest.mark.parametrize("registered_later", [False, True])
def test_original_not_found_does_not_require_or_adopt_later_output(selection_database, registered_later):
    owned = selection_database
    command, key = _save(owned, SelectionMode.EXPLICIT_IDS, (999,))
    if registered_later:
        _seed_output(owned.connection, 999, 11, 1)
        owned.connection.commit()
    result = _reuse(owned, command, key)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    item, = result.value.snapshot.items
    assert (item.requested_output_id, item.output_id, item.status, item.error_code) == (999, None, 4, 1)


class _ReadProbe(_FaultConnection):
    def __init__(self, connection, prefix, fail):
        super().__init__(connection, "unused")
        self.prefix, self.fail = prefix, fail
        self.cursors = []
        self.error = sqlite3.OperationalError("原选择读取失败")

    def execute(self, sql, parameters=()):
        match = sql.startswith(self.prefix)
        if match and self.fail == "execute":
            raise self.error
        cursor = self._connection.execute(sql, parameters)
        if not match:
            return cursor
        probe = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
        probe.description = cursor.description
        probe.close.side_effect = cursor.close
        probe.fetchone.side_effect = self.error if self.fail == "fetch" else cursor.fetchone
        self.cursors.append(probe)
        return probe


@pytest.mark.parametrize("prefix", ["SELECT * FROM obtain_source_selections", "SELECT id FROM obtain_items WHERE",
                                    "SELECT * FROM obtain_items", "SELECT 1 FROM outputs"])
@pytest.mark.parametrize("fail", [None, "execute", "fetch"])
def test_recovery_reads_release_cursors_and_preserve_failure(selection_database, prefix, fail):
    owned = selection_database
    command, key = _save(owned)
    probe = _ReadProbe(owned.connection, prefix, fail)
    before = tuple(owned.connection.iterdump())
    result = outputs.OutputsRepository().fix_selection(command, key, replace(owned, connection=probe))
    if fail:
        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert result.error is probe.error
    else:
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value.snapshot.selected_output_ids == (703, 705)
    for cursor in probe.cursors:
        cursor.close.assert_called_once_with()
    assert tuple(owned.connection.iterdump()) == before
    assert not owned.connection.in_transaction

"""清理目标固定与清理成员守卫的直接事件测试。

真实事件登记与守卫组合：TARGETS_FIXED.CLEANUP 的动作类型与目标集
合一致（精确=全部原请求、范围=固定来源产物）；CLEANUP_CHANGED 的
成员归属、身份保持、直接终态事务边界、终态最终事件与成功依据、
唯一删除处理者。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.contracts.values import new_operation_key
from camctl.history.validators import EventContext, EventValidationError, validate_event
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import (
    event_envelope,
    row_change,
    row_facts,
    update_change,
)

from ..persistence.test_runtime import _create_valid_database

register_outputs_guards()
register_capture_guards()

_NOW = 1_750_000_000_000_000


def _environment(tmp_path: Path, *, explicit: bool = True):
    target = tmp_path / "state.db"
    _create_valid_database(target)
    owned = open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "INSERT INTO history_transactions (id, operation_key, first_event_id, last_event_id)"
        " VALUES (1, ?, 1, 1)", ("f" * 32,))
    connection.execute(
        "INSERT INTO history_events (id, transaction_id, event_type, event_version,"
        " occurred_at, clock_status, change_seq, body_json)"
        " VALUES (1, 1, 2, 1, ?, 2, NULL, ?)",
        (_NOW, json.dumps({"reason": 1, "evidence": {}, "rows": []})))
    connection.execute(
        "INSERT INTO plans (id, request_id, name, created_at, status,"
        " created_event_id, last_event_id, change_count)"
        " VALUES (1, 4242, 'seed', ?, 1, 1, 1, 1)", (_NOW,))
    connection.execute(
        "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
        " scheduled_at, group_name, input_fields_json, effective_params_json,"
        " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
        " cancel_requested, error_code, error_details_json, first_window_observed_at,"
        " expiration_reason, source_resolution_state, resolved_source_plan_id,"
        " target_selection_state, created_event_id, last_event_id, change_count)"
        " VALUES (11, 1, 0, 'rec', 2, 'cam-1', ?, NULL, '{}', '{}', 'camctl-adb',"
        " 1000, '{}', 3, 1, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, 1, 1)",
        (_NOW,))
    params = json.dumps(
        {"params": {"output_ids": [701]}} if explicit else {"params": {}})
    connection.execute(
        "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
        " scheduled_at, group_name, input_fields_json, effective_params_json,"
        " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
        " cancel_requested, error_code, error_details_json, first_window_observed_at,"
        " expiration_reason, source_resolution_state, resolved_source_plan_id,"
        " target_selection_state, created_event_id, last_event_id, change_count)"
        " VALUES (30, 1, 1, 'clean', 5, NULL, ?, NULL, ?, NULL, NULL, NULL, '{}',"
        " 2, 1, 0, NULL, NULL, NULL, NULL, 2, 1, 1, 1, 1, 1)", (_NOW, params))
    connection.execute(
        "INSERT INTO action_dependencies (id, action_id, depends_on_action_id)"
        " VALUES (41, 30, 11)")
    connection.execute(
        "INSERT INTO outputs (id, source_action_id, kind, device_file_id,"
        " availability, cleanup_status, media_json, created_event_id,"
        " last_event_id, change_count) VALUES (701, 11, 1, 501, 1, 1, '{}', 1, 1, 1)")
    connection.execute(
        "INSERT INTO device_files (id, observer_action_id, source_action_id,"
        " identity_key, locator_json, ownership_evidence_json, role,"
        " presence_state, completion_state, completion_evidence_json,"
        " checksum_support, size_bytes, created_event_id, last_event_id,"
        " change_count)"
        " VALUES (501, 11, 11, 'file-0501', '{}', '{}', 2, 2, 3, '{}', 2, 10,"
        " 1, 1, 1)")
    connection.execute(
        "INSERT INTO cleanup_items (id, action_id, requested_output_id, status,"
        " restriction_state) VALUES (90, 30, 701, 1, 1)")
    connection.commit()
    return owned


def _state(owned, tables=("actions", "outputs", "device_files",
                          "action_dependencies", "cleanup_items")):
    return {table: {row[0]: row_facts(owned.connection, table, row[0])
                    for row in owned.connection.execute(
                        f"SELECT id FROM {table}").fetchall()}
            for table in tables}


def _context(owned, state, owners, event_id: int = 2):
    return EventContext(
        TransactionRange(event_id, event_id, event_id), owners, state)


def _envelope(event_id: int, txn_id: int, event_type: int, reason: int, rows, now):
    """公开投影变化的事件须携带 change_seq（内核按报告目标分配）。"""
    from dataclasses import replace

    return replace(event_envelope(
        event_id, txn_id, event_type, reason, rows, now), change_seq=event_id)


def _fix_rows(owned, requested=(701,), *, action_type_ok=True):
    action_before = row_facts(owned.connection, "actions", 30)
    rows = (update_change("actions", 30,
                          {"target_selection_state": 1},
                          {"target_selection_state": 2}),)
    owners = {("actions", 30): ("action", 30)}
    for index, identity in enumerate(requested):
        item_id = 91 + index
        values = {
            "action_id": 30,
            "requested_output_id": identity,
            "output_id": None,
            "status": 1,
            "restriction_state": 1,
            "outcome": None,
            "final_event_id": None,
            "error_code": None,
            "error_details_json": None,
        }
        rows += (row_change("cleanup_items", item_id, values),)
        owners[("cleanup_items", item_id)] = ("action", 30)
    return rows, owners


class TestTargetSetGuard:
    def test_explicit_fix_accepts_matching_request_set(self, tmp_path: Path):
        owned = _environment(tmp_path)
        rows, owners = _fix_rows(owned)
        event = _envelope(2, 2, 4, 2, rows, _NOW)
        validate_event(event, _context(owned, _state(owned), owners))

    @pytest.mark.parametrize("requested", [(999,), (701, 999), ()])
    def test_explicit_fix_rejects_mismatched_sets(self, tmp_path, requested):
        owned = _environment(tmp_path)
        rows, owners = _fix_rows(owned, requested)
        event = _envelope(2, 2, 4, 2, rows, _NOW)
        with pytest.raises(EventValidationError):
            validate_event(event, _context(owned, _state(owned), owners))

    def test_initial_item_values_must_be_unresolved(self, tmp_path):
        owned = _environment(tmp_path)
        rows, owners = _fix_rows(owned)
        broken = tuple(
            row if row.table != "cleanup_items"
            else type(row)(  # 构造非法初始值：限制已建立。
                table=row.table, row_id=row.row_id, before=row.before,
                after=type(row.after)(
                    exists=True,
                    values={**row.after.values, "restriction_state": 2}))
            for row in rows)
        event = _envelope(2, 2, 4, 2, broken, _NOW)
        with pytest.raises(EventValidationError):
            validate_event(event, _context(owned, _state(owned), owners))

    def test_range_fix_checks_source_membership(self, tmp_path):
        owned = _environment(tmp_path, explicit=False)
        rows, owners = _fix_rows(owned, (701,))
        event = _envelope(2, 2, 4, 2, rows, _NOW)
        validate_event(event, _context(owned, _state(owned), owners))
        other, other_owners = _fix_rows(owned, (999,))
        with pytest.raises(EventValidationError):
            validate_event(
                _envelope(3, 3, 4, 2, other, _NOW),
                _context(owned, _state(owned), other_owners, event_id=3))

    def test_fail_branch_creates_no_members(self, tmp_path):
        owned = _environment(tmp_path)
        rows = (update_change("actions", 30,
                              {"target_selection_state": 1, "status": 2,
                               "error_code": None, "error_details_json": None},
                              {"target_selection_state": 3, "status": 4,
                               "error_code": 22,
                               "error_details_json": {}}),)
        event = _envelope(2, 2, 4, 4, rows, _NOW)
        validate_event(event, _context(
            owned, _state(owned), {("actions", 30): ("action", 30)}))
        with_members, owners = _fix_rows(owned)
        with pytest.raises(EventValidationError):
            validate_event(_envelope(3, 3, 4, 4, rows + with_members[1:], _NOW),
                           _context(owned, _state(owned), owners, event_id=3))


class TestCleanupMemberGuard:
    def _item_state(self, owned, **overrides):
        state = _state(owned)
        facts = dict(state["cleanup_items"][90])
        facts.update(overrides)
        state["cleanup_items"][90] = facts
        return state

    def test_progress_requires_fixed_target_set(self, tmp_path):
        owned = _environment(tmp_path)
        progress = update_change("cleanup_items", 90,
                                 {"status": 2, "restriction_state": 2},
                                 {"status": 3, "restriction_state": 2})
        event = _envelope(2, 2, 24, 2, (progress,), _NOW)
        owners = {("cleanup_items", 90): ("action", 30)}
        with pytest.raises(EventValidationError):
            validate_event(event, _context(
                owned, self._item_state(owned, status=2, restriction_state=2), owners))
        fixed = self._item_state(owned, status=2, restriction_state=2)
        fixed["actions"][30]["target_selection_state"] = 2
        validate_event(event, _context(owned, fixed, owners))

    def test_restrict_confirms_output_once_from_null(self, tmp_path):
        owned = _environment(tmp_path)
        restrict = update_change(
            "cleanup_items", 90,
            {"output_id": None, "status": 1, "restriction_state": 1},
            {"output_id": 701, "status": 2, "restriction_state": 2})
        owners = {("cleanup_items", 90): ("action", 30)}
        state = self._item_state(owned)
        state["actions"][30]["target_selection_state"] = 2
        validate_event(_envelope(2, 2, 24, 1, (restrict,), _NOW),
                       _context(owned, state, owners))
        reconfirm = update_change(
            "cleanup_items", 90,
            {"output_id": 701, "status": 1, "restriction_state": 1},
            {"output_id": 702, "status": 2, "restriction_state": 2})
        with pytest.raises(EventValidationError):
            validate_event(_envelope(3, 3, 24, 1, (reconfirm,), _NOW),
                           _context(owned, state, owners, event_id=3))

    def test_terminal_member_requires_final_event_and_basis(self, tmp_path):
        owned = _environment(tmp_path)
        state = self._item_state(owned, status=2, restriction_state=2)
        state["actions"][30]["target_selection_state"] = 2
        owners = {("cleanup_items", 90): ("action", 30)}
        succeed = update_change(
            "cleanup_items", 90,
            {"status": 2, "restriction_state": 2, "outcome": None,
             "final_event_id": None, "error_code": None,
             "error_details_json": None},
            {"status": 4, "restriction_state": 4, "outcome": 1,
             "final_event_id": 2, "error_code": None,
             "error_details_json": None})
        # 无删除调用或缺席事实：成功依据不足。
        with pytest.raises(EventValidationError):
            validate_event(_envelope(2, 2, 24, 3, (succeed,), _NOW),
                           _context(owned, state, owners))
        # 已有完成的删除调用：依据成立。
        state["operation_runs"] = {60: {
            "id": 60, "cleanup_item_id": 90, "kind": 4}}
        state["operation_attempts"] = {61: {
            "id": 61, "run_id": 60, "status": 2, "effect_state": 3}}
        validate_event(_envelope(2, 2, 24, 3, (succeed,), _NOW),
                       _context(owned, state, owners))
        wrong_event = update_change(
            "cleanup_items", 90,
            {"status": 2, "restriction_state": 2, "outcome": None,
             "final_event_id": None, "error_code": None,
             "error_details_json": None},
            {"status": 4, "restriction_state": 4, "outcome": 1,
             "final_event_id": 3, "error_code": None,
             "error_details_json": None})
        with pytest.raises(EventValidationError):
            validate_event(_envelope(4, 4, 24, 3, (wrong_event,), _NOW),
                           _context(owned, state, owners, event_id=4))

    def test_absence_basis_accepts_device_file_absent(self, tmp_path):
        owned = _environment(tmp_path)
        state = self._item_state(owned, status=2, restriction_state=2)
        state["actions"][30]["target_selection_state"] = 2
        state["cleanup_items"][90]["output_id"] = 701
        state["device_files"][501]["presence_state"] = 3
        owners = {("cleanup_items", 90): ("action", 30)}
        confirmed = update_change(
            "cleanup_items", 90,
            {"status": 2, "restriction_state": 2, "outcome": None,
             "final_event_id": None, "error_code": None,
             "error_details_json": None},
            {"status": 4, "restriction_state": 4, "outcome": 3,
             "final_event_id": 2, "error_code": None,
             "error_details_json": None})
        validate_event(_envelope(2, 2, 24, 3, (confirmed,), _NOW),
                       _context(owned, state, owners))

    def test_single_deleting_handler_per_output(self, tmp_path):
        owned = _environment(tmp_path)
        state = self._item_state(owned, status=2, restriction_state=2)
        state["actions"][30]["target_selection_state"] = 2
        state["cleanup_items"][90]["output_id"] = 701
        # 另一动作的同产物项已在删除中。
        state["cleanup_items"][95] = {
            "id": 95, "action_id": 31, "requested_output_id": 701,
            "output_id": 701, "status": 3, "restriction_state": 2}
        owners = {("cleanup_items", 90): ("action", 30)}
        progress = update_change("cleanup_items", 90,
                                 {"status": 2, "restriction_state": 2},
                                 {"status": 3, "restriction_state": 2})
        with pytest.raises(EventValidationError):
            validate_event(_envelope(2, 2, 24, 2, (progress,), _NOW),
                           _context(owned, state, owners))

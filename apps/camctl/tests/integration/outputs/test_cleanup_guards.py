"""清理目标固定与清理成员守卫的直接事件测试。

真实事件登记与守卫组合：TARGETS_FIXED.CLEANUP 的动作类型与目标集
合一致（精确=全部原请求、范围=固定来源产物）；CLEANUP_CHANGED 的
成员归属、身份保持、直接终态事务边界、终态最终事件与成功依据、
唯一删除处理者。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from camctl.capture.files import file_identity_key
from camctl.contracts.history_values import TransactionRange
from camctl.contracts.values import new_operation_key
from camctl.history.validators import EventContext, EventValidationError, validate_event
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.repositories import outputs
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import (
    commit_operation,
    event_envelope,
    row_change,
    row_facts,
    update_change,
)

from ..persistence.test_runtime import _create_valid_database
from ..support.output_planning import EventSequence, proposal_state
from ..support.output_storage_cases import capture_scoped_operation

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
        {"params": {"output_ids": ["701"]}} if explicit else {"params": {}})
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
        " VALUES (501, 11, 11, ?, '{}', '{}', 2, 2, 3, '{}', 2, 10,"
        " 1, 1, 1)",
        (file_identity_key(*connection.execute(
            "SELECT device_id, driver_id FROM actions WHERE id = 11").fetchone(),
            "file-0501"),))
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


def _commit_proposal(owned, proposal, events, *, rejection=None):
    before = tuple(owned.connection.iterdump())
    receipt = commit_operation(EventSequence(events, proposal), new_operation_key(), owned)
    assert not owned.connection.in_transaction
    if rejection is None:
        assert receipt.kind == "completed", receipt.error
    else:
        assert receipt.kind == "rolled_back", receipt.error
        assert isinstance(receipt.error, EventValidationError), receipt.error
        assert rejection in str(receipt.error)
        assert tuple(owned.connection.iterdump()) == before
    return receipt


class TestTargetSetGuard:
    @staticmethod
    def _fix_proposal(owned, requested=(701,)):
        from camctl.outputs.cleanup_flow import FixCleanupTargets

        connection = owned.connection
        connection.execute("DELETE FROM cleanup_items")
        connection.execute("UPDATE actions SET input_fields_json=? WHERE id=30",
                           (json.dumps({"params": {"output_ids": [str(i) for i in requested]}}),))
        connection.commit()
        return capture_scoped_operation(
            owned, outputs, lambda target: outputs.OutputsRepository().fix_cleanup_targets(
                FixCleanupTargets(30, _NOW), new_operation_key(), target))

    def test_explicit_fix_accepts_matching_request_set(self, tmp_path: Path):
        owned = _environment(tmp_path)
        proposal = self._fix_proposal(owned)
        _commit_proposal(owned, proposal, proposal.events)
        assert owned.connection.execute(
            "SELECT requested_output_id, output_id FROM cleanup_items WHERE action_id=30"
        ).fetchall() == [(701, None)]
        owned.connection.close()

    @pytest.mark.parametrize("requested", [(999,), (701, 999), ()])
    def test_explicit_fix_rejects_mismatched_sets(self, tmp_path, requested):
        owned = _environment(tmp_path)
        proposal = self._fix_proposal(owned, requested if requested == (701, 999) else (701,))
        event = proposal.events[0]
        if requested == (701, 999):
            # 提案包含两个成员，而本次实际原请求只有 701。
            owned.connection.execute("UPDATE actions SET input_fields_json=? WHERE id=30",
                                     (json.dumps({"params": {"output_ids": ["701"]}}),))
            owned.connection.commit()
            proposal = proposal_state(owned, owners=proposal.owners)
        elif requested:
            row = event.rows[1]
            changed = replace(row, after=replace(row.after, values={
                **row.after.values, "requested_output_id": requested[0]}))
            event = replace(event, rows=(event.rows[0], changed))
        else:
            event = replace(event, rows=(event.rows[0],))
        _commit_proposal(owned, proposal, (event,), rejection=(
            "清理目标固定必须创建清理成员" if not requested
            else "精确清理成员与原请求及机内照片配对不一致"))
        owned.connection.close()

    def test_initial_item_values_must_be_unresolved(self, tmp_path):
        owned = _environment(tmp_path)
        proposal = self._fix_proposal(owned)
        event = proposal.events[0]
        row = event.rows[1]
        broken = replace(row, after=replace(row.after, values={
            **row.after.values, "restriction_state": 2}))
        _commit_proposal(owned, proposal, (replace(event, rows=(event.rows[0], broken)),),
                         rejection="restriction_state=2 不在允许范围")
        owned.connection.close()

    def test_range_fix_checks_source_membership(self, tmp_path):
        from camctl.outputs.cleanup_flow import FixCleanupTargets

        owned = TestFixCleanupTargetsCommand._scope_environment(tmp_path)
        TestFixCleanupTargetsCommand._processing_completed(owned)
        proposal = capture_scoped_operation(
            owned, outputs, lambda target: outputs.OutputsRepository().fix_cleanup_targets(
                FixCleanupTargets(30, _NOW), new_operation_key(), target))
        event = proposal.events[0]
        row = event.rows[1]
        other = replace(row, after=replace(row.after, values={
            **row.after.values, "requested_output_id": 999}))
        _commit_proposal(owned, proposal, (replace(event, rows=(event.rows[0], other)),),
                         rejection="范围清理成员不属于本动作固定来源")
        _commit_proposal(owned, proposal, proposal.events)
        assert owned.connection.execute(
            "SELECT requested_output_id FROM cleanup_items WHERE action_id=30"
        ).fetchall() == [(701,)]
        owned.connection.close()

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
        from camctl.outputs.cleanup_flow import ProgressCleanupItem, RestrictCleanupItem
        from camctl.persistence.models import DbOutcomeKind

        owned = _environment(tmp_path)
        connection = owned.connection
        connection.execute("UPDATE actions SET target_selection_state=2 WHERE id=30")
        connection.commit()
        repository = outputs.OutputsRepository()
        restricted = repository.restrict_cleanup_item(
            RestrictCleanupItem(90, _NOW), new_operation_key(), owned)
        assert restricted.kind is DbOutcomeKind.COMPLETED, restricted.error
        proposal = capture_scoped_operation(
            owned, outputs, lambda target: repository.progress_cleanup_item(
                ProgressCleanupItem(90, _NOW), new_operation_key(), target))
        connection.execute("UPDATE actions SET target_selection_state=1 WHERE id=30")
        connection.commit()
        current = proposal_state(owned, owners=proposal.owners)
        _commit_proposal(owned, current, proposal.events,
                         rejection="清理成员推进要求目标集合已固定")
        connection.execute("UPDATE actions SET target_selection_state=2 WHERE id=30")
        connection.commit()
        _commit_proposal(owned, proposal, proposal.events)
        assert connection.execute("SELECT status FROM cleanup_items WHERE id=90").fetchone() == (3,)
        connection.close()

    def test_restrict_confirms_output_once_from_null(self, tmp_path):
        from camctl.outputs.cleanup_flow import RestrictCleanupItem

        owned = _environment(tmp_path)
        owned.connection.execute("UPDATE actions SET target_selection_state=2 WHERE id=30")
        owned.connection.commit()
        proposal = capture_scoped_operation(
            owned, outputs, lambda target: outputs.OutputsRepository().restrict_cleanup_item(
                RestrictCleanupItem(90, _NOW), new_operation_key(), target))
        event = proposal.events[0]
        row = event.rows[0]
        owned.connection.execute("UPDATE cleanup_items SET output_id=701 WHERE id=90")
        owned.connection.commit()
        reconfirm = replace(row,
            before=replace(row.before, values={**row.before.values, "output_id": 701}),
            after=replace(row.after, values={**row.after.values, "output_id": 702}))
        _commit_proposal(owned, proposal,
                         (replace(event, rows=(reconfirm,)), *proposal.events[1:]),
                         rejection="output_id'] 已赋值，不能再次更改")
        owned.connection.execute("UPDATE cleanup_items SET output_id=NULL WHERE id=90")
        owned.connection.commit()
        _commit_proposal(owned, proposal, proposal.events)
        assert owned.connection.execute(
            "SELECT output_id, status, restriction_state FROM cleanup_items WHERE id=90"
        ).fetchone() == (701, 2, 2)
        owned.connection.close()

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
        from camctl.outputs.cleanup_flow import (
            CleanupOutcomeChoice, FinishCleanupItem, RestrictCleanupItem)
        from camctl.persistence.models import DbOutcomeKind

        owned = _environment(tmp_path)
        connection = owned.connection
        connection.execute("UPDATE actions SET target_selection_state=2 WHERE id=30")
        connection.commit()
        repository = outputs.OutputsRepository()
        restricted = repository.restrict_cleanup_item(
            RestrictCleanupItem(90, _NOW), new_operation_key(), owned)
        assert restricted.kind is DbOutcomeKind.COMPLETED, restricted.error
        connection.execute("UPDATE device_files SET presence_state=3 WHERE id=501")
        connection.commit()
        proposal = capture_scoped_operation(
            owned, outputs, lambda target: repository.finish_cleanup_item(
                FinishCleanupItem(90, CleanupOutcomeChoice.ABSENCE_CONFIRMED, _NOW),
                new_operation_key(), target))
        _commit_proposal(owned, proposal, proposal.events)
        assert connection.execute(
            "SELECT status, outcome FROM cleanup_items WHERE id=90"
        ).fetchone() == (4, 3)
        connection.close()

    def test_single_deleting_handler_per_output(self, tmp_path):
        from camctl.outputs.cleanup_flow import ProgressCleanupItem, RestrictCleanupItem
        from camctl.persistence.models import DbOutcomeKind

        owned = _environment(tmp_path)
        connection = owned.connection
        connection.execute("UPDATE actions SET target_selection_state=2 WHERE id=30")
        connection.commit()
        repository = outputs.OutputsRepository()
        restricted = repository.restrict_cleanup_item(
            RestrictCleanupItem(90, _NOW), new_operation_key(), owned)
        assert restricted.kind is DbOutcomeKind.COMPLETED, restricted.error
        proposal = capture_scoped_operation(
            owned, outputs, lambda target: repository.progress_cleanup_item(
                ProgressCleanupItem(90, _NOW), new_operation_key(), target))
        # 模拟另一动作先取得同一产物的删除处理权。
        connection.execute(
            "INSERT INTO actions (id, plan_id, input_index, name, type, scheduled_at,"
            " input_fields_json, execution_spec_json, status, execution_started,"
            " cancel_requested, source_resolution_state, resolved_source_plan_id,"
            " target_selection_state, created_event_id, last_event_id, change_count)"
            " VALUES (31, 1, 2, 'clean2', 5, ?, '{}', '{}', 2, 1, 0, 2, 1, 2, 1, 1, 1)",
            (_NOW,))
        connection.execute(
            "INSERT INTO cleanup_items (id, action_id, requested_output_id, output_id,"
            " status, restriction_state) VALUES (95, 31, 701, 701, 3, 2)")
        connection.commit()
        current = proposal_state(owned, owners=proposal.owners)
        _commit_proposal(owned, current, proposal.events,
                         rejection="共同来源仍有读取依赖或其他删除处理者")
        connection.execute("DELETE FROM cleanup_items WHERE id=95")
        connection.execute("DELETE FROM actions WHERE id=31")
        connection.commit()
        _commit_proposal(owned, proposal, proposal.events)
        assert connection.execute("SELECT status FROM cleanup_items WHERE id=90").fetchone() == (3,)
        connection.close()


class TestFixCleanupTargetsCommand:
    @staticmethod
    def _environment(tmp_path):
        owned = _environment(tmp_path)
        # 守卫用例预置的成员不参与命令流程。
        owned.connection.execute("DELETE FROM cleanup_items")
        owned.connection.commit()
        return owned

    def _repository(self):
        from camctl.persistence.repositories.outputs import OutputsRepository

        register_capture_guards()
        register_outputs_guards()
        return OutputsRepository()

    def test_explicit_fix_creates_members_for_resolvable_targets(self, tmp_path):
        from camctl.contracts.values import new_operation_key
        from camctl.persistence.models import DbOutcomeKind

        owned = self._environment(tmp_path)
        repository = self._repository()
        from camctl.outputs.cleanup_flow import (
            CleanupTargetsDisposition, FixCleanupTargets)

        outcome = repository.fix_cleanup_targets(
            FixCleanupTargets(30, _NOW), new_operation_key(), owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert outcome.value.disposition is CleanupTargetsDisposition.SAVED
        row = owned.connection.execute(
            "SELECT requested_output_id, output_id, status, restriction_state"
            " FROM cleanup_items WHERE action_id = 30").fetchall()
        assert row == [(701, None, 1, 1)]
        assert owned.connection.execute(
            "SELECT target_selection_state FROM actions WHERE id = 30"
        ).fetchone() == (2,)

    def test_missing_targets_become_terminal_and_all_missing_fails_action(
            self, tmp_path):
        from camctl.contracts.values import new_operation_key
        from camctl.persistence.models import DbOutcomeKind

        owned = self._environment(tmp_path)
        owned.connection.execute(
            "UPDATE actions SET input_fields_json = ? WHERE id = 30",
            (json.dumps({"params": {"output_ids": ["701", "999"]}}),))
        owned.connection.commit()
        repository = self._repository()
        from camctl.outputs.cleanup_flow import FixCleanupTargets

        outcome = repository.fix_cleanup_targets(
            FixCleanupTargets(30, _NOW), new_operation_key(), owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        rows = owned.connection.execute(
            "SELECT requested_output_id, status, restriction_state, outcome,"
            " final_event_id, error_code, error_details_json"
            " FROM cleanup_items WHERE action_id = 30 ORDER BY id").fetchall()
        assert len(rows) == 2
        resolvable, missing = rows
        assert resolvable[1:4] == (1, 1, None)
        assert missing[1] == 5
        assert missing[3] is None
        assert missing[5] == 1
        assert json.loads(missing[6]) == {"requested_output_id": "999"}
        # 有可删除成员：动作保持执行中。
        assert owned.connection.execute(
            "SELECT status, target_selection_state FROM actions WHERE id = 30"
        ).fetchone() == (2, 2)

    def test_all_missing_fails_action_with_registered_error(self, tmp_path):
        from camctl.contracts.values import new_operation_key
        from camctl.persistence.models import DbOutcomeKind

        owned = self._environment(tmp_path)
        owned.connection.execute(
            "UPDATE actions SET input_fields_json = ? WHERE id = 30",
            (json.dumps({"params": {"output_ids": ["999"]}}),))
        owned.connection.commit()
        repository = self._repository()
        from camctl.outputs.cleanup_flow import FixCleanupTargets

        outcome = repository.fix_cleanup_targets(
            FixCleanupTargets(30, _NOW), new_operation_key(), owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        # 全部不可解析：成员直接终态；动作失败终态由流程汇总保存。
        assert owned.connection.execute(
            "SELECT status, target_selection_state FROM actions WHERE id = 30"
        ).fetchone() == (2, 2)
        assert owned.connection.execute(
            "SELECT status, error_code FROM cleanup_items"
            " WHERE action_id = 30").fetchone() == (5, 1)

    def test_original_key_resend_restores_first_response(self, tmp_path):
        from camctl.contracts.values import new_operation_key
        from camctl.persistence.models import DbOutcomeKind

        owned = self._environment(tmp_path)
        repository = self._repository()
        from camctl.outputs.cleanup_flow import (
            CleanupTargetsDisposition, FixCleanupTargets)

        key = new_operation_key()
        first = repository.fix_cleanup_targets(
            FixCleanupTargets(30, _NOW), key, owned)
        assert first.value.disposition is CleanupTargetsDisposition.SAVED
        resend = repository.fix_cleanup_targets(
            FixCleanupTargets(30, _NOW), key, owned)
        assert resend.kind is DbOutcomeKind.COMPLETED, resend.error
        assert resend.value.disposition is CleanupTargetsDisposition.ALREADY
        assert resend.value.item_ids == first.value.item_ids
        late = repository.fix_cleanup_targets(
            FixCleanupTargets(30, _NOW + 1_000_000), key, owned)
        assert late.kind is DbOutcomeKind.ROLLED_BACK, late.error

    def test_already_fixed_returns_members_without_new_history(self, tmp_path):
        from camctl.contracts.values import new_operation_key
        from camctl.persistence.models import DbOutcomeKind

        owned = self._environment(tmp_path)
        repository = self._repository()
        from camctl.outputs.cleanup_flow import (
            CleanupTargetsDisposition, FixCleanupTargets)

        first = repository.fix_cleanup_targets(
            FixCleanupTargets(30, _NOW), new_operation_key(), owned)
        before = tuple(owned.connection.execute(
            "SELECT id, body_json FROM history_events ORDER BY id").fetchall())
        again = repository.fix_cleanup_targets(
            FixCleanupTargets(30, _NOW + 5), new_operation_key(), owned)
        assert again.kind is DbOutcomeKind.COMPLETED, again.error
        assert again.value.disposition is CleanupTargetsDisposition.ALREADY
        assert again.value.item_ids == first.value.item_ids
        assert tuple(owned.connection.execute(
            "SELECT id, body_json FROM history_events ORDER BY id").fetchall()
        ) == before

    @staticmethod
    def _scope_environment(tmp_path):
        """范围清理动作：params.source 请求，固定来源为动作 11。"""
        owned = _environment(tmp_path, explicit=False)
        owned.connection.execute("DELETE FROM cleanup_items")
        owned.connection.execute(
            "UPDATE actions SET input_fields_json = ? WHERE id = 30",
            (json.dumps({"params": {"source": {"action_instance_id": "11"}}}),))
        owned.connection.commit()
        return owned

    @staticmethod
    def _processing_completed(owned) -> None:
        owned.connection.execute(
            "INSERT INTO recording_processing (id, action_id, source_device_file_id,"
            " check_state, check_decision, check_basis_json, media_json, repair_state,"
            " repair_basis_json, repair_output_file_id, repair_error_json, discard_state,"
            " discard_error_json)"
            " VALUES (51, 11, 501, 1, 1, NULL, '{}', 1, NULL, NULL, NULL, 4, NULL)")
        owned.connection.commit()

    def _fix(self, repository, owned):
        from camctl.outputs.cleanup_flow import FixCleanupTargets

        return repository.fix_cleanup_targets(
            FixCleanupTargets(30, _NOW), new_operation_key(), owned)

    def test_scope_fix_waits_until_source_terminal(self, tmp_path):
        from camctl.persistence.models import DbOutcomeKind
        from camctl.outputs.cleanup_flow import CleanupTargetsDisposition

        owned = self._scope_environment(tmp_path)
        owned.connection.execute("UPDATE actions SET status = 2 WHERE id = 11")
        owned.connection.commit()
        outcome = self._fix(self._repository(), owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert outcome.value.disposition is CleanupTargetsDisposition.WAITING
        assert outcome.value.item_ids == ()
        # 只读等待：不创建成员，也不写新历史。
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM cleanup_items WHERE action_id = 30"
        ).fetchone() == (0,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM history_events").fetchone() == (1,)
        assert owned.connection.execute(
            "SELECT target_selection_state FROM actions WHERE id = 30"
        ).fetchone() == (1,)

    def test_scope_fix_waits_until_source_processing_completed(self, tmp_path):
        from camctl.persistence.models import DbOutcomeKind
        from camctl.outputs.cleanup_flow import CleanupTargetsDisposition

        owned = self._scope_environment(tmp_path)
        self._processing_completed(owned)
        owned.connection.execute(
            "UPDATE recording_processing SET discard_state = 2 WHERE id = 51")
        owned.connection.commit()
        outcome = self._fix(self._repository(), owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert outcome.value.disposition is CleanupTargetsDisposition.WAITING
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM cleanup_items WHERE action_id = 30"
        ).fetchone() == (0,)

    def test_scope_fix_enumerates_outputs_of_fixed_sources(self, tmp_path):
        from camctl.persistence.models import DbOutcomeKind
        from camctl.outputs.cleanup_flow import CleanupTargetsDisposition

        owned = self._scope_environment(tmp_path)
        # 同来源第二产物与外来源产物：只枚举本动作固定来源的产物。
        owned.connection.execute(
            "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
            " scheduled_at, group_name, input_fields_json, effective_params_json,"
            " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
            " cancel_requested, error_code, error_details_json, first_window_observed_at,"
            " expiration_reason, source_resolution_state, resolved_source_plan_id,"
            " target_selection_state, created_event_id, last_event_id, change_count)"
            " VALUES (12, 1, 2, 'other', 2, 'cam-1', ?, NULL, '{}', '{}', 'camctl-adb',"
            " 1000, '{}', 3, 1, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, 1, 1)",
            (_NOW,))
        for output_id, file_id, source_id in ((702, 502, 11), (703, 503, 12)):
            owned.connection.execute(
                "INSERT INTO device_files (id, observer_action_id, source_action_id,"
                " identity_key, locator_json, ownership_evidence_json, role,"
                " presence_state, completion_state, completion_evidence_json,"
                " checksum_support, size_bytes, created_event_id, last_event_id,"
                " change_count)"
                " VALUES (?, 11, ?, ?, '{}', '{}', 2, 2, 3, '{}', 2, 10, 1, 1, 1)",
                (file_id, source_id, file_identity_key(*owned.connection.execute(
                    "SELECT device_id, driver_id FROM actions WHERE id=?", (source_id,)
                ).fetchone(), f"file-{file_id}")))
            owned.connection.execute(
                "INSERT INTO outputs (id, source_action_id, kind, device_file_id,"
                " availability, cleanup_status, media_json, created_event_id,"
                " last_event_id, change_count)"
                f" VALUES ({output_id}, {source_id}, 1, {file_id}, 1, 1, '{{}}',"
                " 1, 1, 1)")
        self._processing_completed(owned)
        outcome = self._fix(self._repository(), owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert outcome.value.disposition is CleanupTargetsDisposition.SAVED
        rows = owned.connection.execute(
            "SELECT requested_output_id, output_id, status, restriction_state"
            " FROM cleanup_items WHERE action_id = 30 ORDER BY id").fetchall()
        assert rows == [(701, None, 1, 1), (702, None, 1, 1)]
        assert owned.connection.execute(
            "SELECT target_selection_state FROM actions WHERE id = 30"
        ).fetchone() == (2,)


class TestFinishCleanupAction:
    @staticmethod
    def _environment(tmp_path, *, items, cancel_requested=0):
        """目标已固定的清理动作与给定状态的成员；第二产物支撑混合结果。"""
        owned = _environment(tmp_path)
        owned.connection.execute("DELETE FROM cleanup_items")
        owned.connection.execute(
            "UPDATE actions SET target_selection_state = 2,"
            " cancel_requested = ? WHERE id = 30", (cancel_requested,))
        owned.connection.execute(
            "INSERT INTO device_files (id, observer_action_id, source_action_id,"
            " identity_key, locator_json, ownership_evidence_json, role,"
            " presence_state, completion_state, completion_evidence_json,"
            " checksum_support, size_bytes, created_event_id, last_event_id,"
            " change_count)"
            " VALUES (502, 11, 11, 'file-0502', '{}', '{}', 2, 2, 3, '{}',"
            " 2, 10, 1, 1, 1)")
        owned.connection.execute(
            "INSERT INTO outputs (id, source_action_id, kind, device_file_id,"
            " availability, cleanup_status, media_json, created_event_id,"
            " last_event_id, change_count)"
            " VALUES (702, 11, 1, 502, 1, 1, '{}', 1, 1, 1)")
        for values in items:
            owned.connection.execute(
                "INSERT INTO cleanup_items (id, action_id, requested_output_id,"
                " output_id, status, restriction_state, outcome, final_event_id,"
                " error_code, error_details_json)"
                " VALUES (?, 30, ?, ?, ?, ?, ?, ?, ?, ?)", values)
        owned.connection.commit()
        return owned

    def _repository(self):
        from camctl.persistence.repositories.outputs import OutputsRepository

        register_capture_guards()
        register_outputs_guards()
        return OutputsRepository()

    def _finish(self, repository, owned, occurred_at=_NOW, key=None):
        from camctl.contracts.values import new_operation_key
        from camctl.outputs.cleanup_flow import FinishCleanupAction

        return repository.finish_cleanup_action(
            FinishCleanupAction(30, occurred_at),
            key or new_operation_key(), owned)

    def test_failed_member_fails_action_with_registered_error(self, tmp_path):
        from camctl.persistence.models import DbOutcomeKind

        owned = self._environment(tmp_path, items=[
            (91, 701, 701, 4, 4, 1, 1, None, None),
            (92, 702, 702, 5, 4, None, 1, 3,
             '{"output_id":"702","max_attempts":1,"attempts_used":1}'),
        ])
        outcome = self._finish(self._repository(), owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert owned.connection.execute(
            "SELECT status, error_code, error_details_json FROM actions"
            " WHERE id = 30").fetchone() == (4, 22, "{}")
        # 兄弟动作（来源 11）已终态：父计划同事务完成。
        assert owned.connection.execute(
            "SELECT status FROM plans WHERE id = 1").fetchone() == (3,)

    def test_all_succeeded_completes_action(self, tmp_path):
        from camctl.persistence.models import DbOutcomeKind

        owned = self._environment(tmp_path, items=[
            (91, 701, 701, 4, 4, 1, 1, None, None),
        ])
        outcome = self._finish(self._repository(), owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert owned.connection.execute(
            "SELECT status, error_code FROM actions WHERE id = 30"
        ).fetchone() == (3, None)

    def test_unfinished_member_rejects_finish(self, tmp_path):
        from camctl.persistence.models import DbOutcomeKind

        owned = self._environment(tmp_path, items=[
            (91, 701, 701, 2, 2, None, None, None, None),
        ])
        outcome = self._finish(self._repository(), owned)
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id = 30").fetchone() == (2,)

    def test_cancel_requested_rejects_finish(self, tmp_path):
        from camctl.persistence.models import DbOutcomeKind

        owned = self._environment(tmp_path, cancel_requested=1, items=[
            (91, 701, 701, 4, 4, 1, 1, None, None),
        ])
        outcome = self._finish(self._repository(), owned)
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id = 30").fetchone() == (2,)

    def test_original_key_resend_restores_first_response(self, tmp_path):
        from camctl.contracts.values import new_operation_key
        from camctl.persistence.models import DbOutcomeKind
        from camctl.outputs.cleanup_flow import CleanupActionDisposition

        owned = self._environment(tmp_path, items=[
            (91, 701, 701, 4, 4, 1, 1, None, None),
        ])
        repository = self._repository()
        key = new_operation_key()
        first = self._finish(repository, owned, key=key)
        assert first.value.disposition is CleanupActionDisposition.SAVED
        resend = self._finish(repository, owned, key=key)
        assert resend.kind is DbOutcomeKind.COMPLETED, resend.error
        assert resend.value.disposition is CleanupActionDisposition.ALREADY
        assert resend.value.action_status == first.value.action_status
        late = self._finish(repository, owned, occurred_at=_NOW + 1_000_000, key=key)
        assert late.kind is DbOutcomeKind.ROLLED_BACK

    def test_terminal_new_key_recovers_without_new_history(self, tmp_path):
        from camctl.persistence.models import DbOutcomeKind
        from camctl.outputs.cleanup_flow import CleanupActionDisposition

        owned = self._environment(tmp_path, items=[
            (91, 701, 701, 4, 4, 1, 1, None, None),
        ])
        repository = self._repository()
        first = self._finish(repository, owned)
        assert first.value.disposition is CleanupActionDisposition.SAVED
        before = tuple(owned.connection.execute(
            "SELECT id FROM history_events ORDER BY id").fetchall())
        again = self._finish(repository, owned, occurred_at=_NOW + 5)
        assert again.kind is DbOutcomeKind.COMPLETED, again.error
        assert again.value.disposition is CleanupActionDisposition.ALREADY
        assert tuple(owned.connection.execute(
            "SELECT id FROM history_events ORDER BY id").fetchall()) == before

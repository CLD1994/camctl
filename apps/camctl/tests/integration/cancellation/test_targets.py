"""N1 完整取消目标解析与固定集合的组件集成测试。

真实 SQLite 寻址四种入口、自动预览关联去重与自身包含检查；固定集
合事务原子保存依据与初始取消效果，目标解析失败以登记错误结束动作
且不创建任何成员；原键重送与终态新键不产生部分取消。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from camctl.cancellation.models import (
    CancelTarget,
    CancelTargetError,
    ResolvedTargets,
    TargetFacts,
)
from camctl.cancellation.targets import (
    missing_target_error,
    prepare_cancel_set,
    resolve_cancel_target,
)
from camctl.contracts.values import new_operation_key
from camctl.persistence.repositories.cancellation import (
    register_cancellation_guards,
)
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..persistence.test_runtime import _create_valid_database

register_capture_guards()
register_outputs_guards()
register_cancellation_guards()

_NOW = 1_750_000_000_000_000


@pytest.fixture
def pipeline(tmp_path: Path):
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
    for plan_id, request_id in ((1, 4242), (2, 4243)):
        connection.execute(
            "INSERT INTO plans (id, request_id, name, created_at, status,"
            " created_event_id, last_event_id, change_count)"
            " VALUES (?, ?, 'seed', ?, 1, 1, 1, 1)", (plan_id, request_id, _NOW))
    # 11、12：计划 1 的拍摄（12 已终态）；21：计划 1 的自动预览取回；
    # 13：计划 2 的拍摄；50：计划 2 承载的取消动作。
    for action_id, plan_id, index, name, kind, group, status in (
            (11, 1, 0, "rec-a", 2, "g", 2),
            (12, 1, 1, "rec-b", 2, "h", 3),
            (21, 1, 2, "preview-a", 4, None, 2),
            (13, 2, 0, "rec-c", 2, "g", 3),
    ):
        capture = kind in (1, 2, 3)
        connection.execute(
            "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
            " scheduled_at, group_name, input_fields_json, effective_params_json,"
            " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
            " cancel_requested, error_code, error_details_json,"
            " first_window_observed_at, expiration_reason, source_resolution_state,"
            " resolved_source_plan_id, target_selection_state, created_event_id,"
            " last_event_id, change_count)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?, '{}', ?, 1, 0,"
            " NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, 1, 1)",
            (action_id, plan_id, index, name, kind,
             "cam-1" if capture else None, _NOW, group,
             "{}" if capture else None,
             "camctl-adb" if capture else None,
             1000 if capture else None, status))
    connection.execute(
        "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
        " scheduled_at, group_name, input_fields_json, effective_params_json,"
        " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
        " cancel_requested, error_code, error_details_json, first_window_observed_at,"
        " expiration_reason, source_resolution_state, resolved_source_plan_id,"
        " target_selection_state, created_event_id, last_event_id, change_count)"
        " VALUES (50, 2, 1, 'cancel', 6, NULL, ?, NULL, ?, NULL, NULL, NULL, '{}',"
        " 2, 1, 0, NULL, NULL, NULL, NULL, NULL, NULL, 1, 1, 1, 1)",
        (_NOW, json.dumps({"params": {"target": {"plan_instance_id": "1"}}})))
    connection.execute(
        "INSERT INTO auto_preview_links (id, obtain_action_id, source_action_id,"
        " preview_support, parameter_type, is_valid)"
        " VALUES (61, 21, 11, 1, 'timed', 1)")
    connection.commit()
    yield owned
    owned.connection.close()


def _value(owned, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


class TestRealStoreResolution:
    def test_four_entries_resolve_against_real_store(self, pipeline):
        from camctl.persistence.repositories.cancellation import SqliteCancelLookup

        owned = pipeline
        lookup = SqliteCancelLookup(owned.connection)
        assert resolve_cancel_target(
            CancelTarget(action_instance_id=11), lookup).action_ids == (11,)
        assert resolve_cancel_target(
            CancelTarget(plan_instance_id=1), lookup).action_ids == (11, 12, 21)
        assert resolve_cancel_target(
            CancelTarget(plan_instance_id=1, group="g"),
            lookup).action_ids == (11,)
        assert resolve_cancel_target(
            CancelTarget(request_id="4242"), lookup).action_ids == (11, 12, 21)
        for target in (CancelTarget(action_instance_id=99),
                       CancelTarget(plan_instance_id=9),
                       CancelTarget(plan_instance_id=9, group="g"),
                       CancelTarget(request_id="9999")):
            assert resolve_cancel_target(target, lookup).missing, target

    def test_auto_candidates_come_from_saved_links(self, pipeline):
        from camctl.persistence.repositories.cancellation import (
            SqliteCancelLookup, sqlite_auto_candidates)

        owned = pipeline
        lookup = SqliteCancelLookup(owned.connection)
        assert lookup.action_exists(21)
        assert sqlite_auto_candidates(
            owned.connection, (11, 12, 21)) == ((11, 21),)


class TestFixCancelTargets:
    def _resolved(self, owned):
        from camctl.persistence.repositories.cancellation import (
            SqliteCancelLookup, sqlite_auto_candidates)

        lookup = SqliteCancelLookup(owned.connection)
        ids = resolve_cancel_target(
            CancelTarget(plan_instance_id=1), lookup).action_ids
        terminal = {
            row[0] for row in owned.connection.execute(
                "SELECT id FROM actions WHERE status IN (3, 4, 5, 6)")}
        direct = tuple(
            TargetFacts(action_id=identity, terminal=identity in terminal,
                        may_cancel=True)
            for identity in ids)
        return ResolvedTargets(
            direct=direct,
            auto_candidates=sqlite_auto_candidates(owned.connection, ids))

    def _fix(self, owned, resolved, occurred_at=_NOW, key=None):
        from camctl.cancellation.models import FixedCancelSet
        from camctl.persistence.repositories.cancellation import (
            CancellationRepository, FixCancelTargets)

        fixed = prepare_cancel_set(50, resolved)
        assert isinstance(fixed, FixedCancelSet), fixed
        return CancellationRepository().fix_cancel_targets(
            FixCancelTargets(50, fixed, occurred_at),
            key or new_operation_key(), owned)

    def test_fix_saves_full_set_with_basis_and_effect(self, pipeline):
        from camctl.persistence.models import DbOutcomeKind

        owned = pipeline
        outcome = self._fix(pipeline, self._resolved(owned))
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        rows = owned.connection.execute(
            "SELECT target_action_id, selection_basis, status,"
            " cancellation_effect, outcome, error_code"
            " FROM cancel_items WHERE action_id = 50 ORDER BY target_action_id"
        ).fetchall()
        assert rows == [(11, 1, 1, 1, None, None),
                        (12, 1, 1, 3, None, None),
                        (21, 3, 1, 1, None, None)]
        assert _value(
            owned, "SELECT target_selection_state FROM actions WHERE id = 50"
        ) == (2,)
        event = _value(
            owned, "SELECT event_type, body_json FROM history_events"
            " WHERE id = 2")
        assert event[0] == 4 and json.loads(event[1])["reason"] == 3

    def test_self_scope_fails_action_without_any_item(self, pipeline):
        from camctl.persistence.models import DbOutcomeKind
        from camctl.persistence.repositories.cancellation import (
            CancellationRepository, FailCancelTargets)

        owned = pipeline
        from camctl.persistence.repositories.cancellation import (
            SqliteCancelLookup)

        resolved = self._resolved(owned)
        # 计划 2 入口包含取消动作自身：完整集合自包含检查失败。
        ids = resolve_cancel_target(
            CancelTarget(plan_instance_id=2),
            SqliteCancelLookup(owned.connection)).action_ids
        terminal = {
            row[0] for row in owned.connection.execute(
                "SELECT id FROM actions WHERE status IN (3, 4, 5, 6)")}
        self_scope = ResolvedTargets(
            direct=tuple(
                TargetFacts(identity, identity in terminal, True)
                for identity in ids),
            auto_candidates=())
        outcome = prepare_cancel_set(50, self_scope)
        assert isinstance(outcome, CancelTargetError)
        failed = CancellationRepository().fail_cancel_targets(
            FailCancelTargets(50, outcome, _NOW), new_operation_key(), owned)
        assert failed.kind is DbOutcomeKind.COMPLETED, failed.error
        row = _value(
            owned, "SELECT status, error_code, error_details_json,"
            " target_selection_state FROM actions WHERE id = 50")
        assert row[0] == 4 and row[1] == 24 and row[3] == 3
        assert json.loads(row[2]) == {"action_instance_id": "50"}
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM cancel_items").fetchone() == (0,)

    def test_missing_target_fails_action_with_registered_error(self, pipeline):
        from camctl.persistence.models import DbOutcomeKind
        from camctl.persistence.repositories.cancellation import (
            CancellationRepository, FailCancelTargets)

        owned = pipeline
        error = missing_target_error(CancelTarget(plan_instance_id=9))
        failed = CancellationRepository().fail_cancel_targets(
            FailCancelTargets(50, error, _NOW), new_operation_key(), owned)
        assert failed.kind is DbOutcomeKind.COMPLETED, failed.error
        row = _value(
            owned, "SELECT status, error_code, error_details_json"
            " FROM actions WHERE id = 50")
        assert row[0] == 4 and row[1] == 25
        assert json.loads(row[2]) == {"target": {"plan_instance_id": "9"}}
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM cancel_items").fetchone() == (0,)

    def test_resend_and_recovery_produce_no_partial_cancel(self, pipeline):
        from camctl.persistence.models import DbOutcomeKind
        from camctl.persistence.repositories.cancellation import (
            CancelTargetsDisposition,)

        owned = pipeline
        key = new_operation_key()
        first = self._fix(pipeline, self._resolved(owned), key=key)
        assert first.value.disposition is CancelTargetsDisposition.SAVED
        before = tuple(owned.connection.execute(
            "SELECT id FROM history_events ORDER BY id").fetchall())
        resend = self._fix(pipeline, self._resolved(owned), key=key)
        assert resend.kind is DbOutcomeKind.COMPLETED, resend.error
        assert resend.value.disposition is CancelTargetsDisposition.ALREADY
        assert resend.value.item_ids == first.value.item_ids
        late = self._fix(
            pipeline, self._resolved(owned), occurred_at=_NOW + 1_000_000,
            key=key)
        assert late.kind is DbOutcomeKind.ROLLED_BACK
        again = self._fix(
            pipeline, self._resolved(owned), occurred_at=_NOW + 5)
        assert again.kind is DbOutcomeKind.COMPLETED, again.error
        assert again.value.disposition is CancelTargetsDisposition.ALREADY
        assert tuple(owned.connection.execute(
            "SELECT id FROM history_events ORDER BY id").fetchall()) == before

    def test_empty_set_is_rejected_without_partial_save(self, pipeline):
        from camctl.cancellation.models import FixedCancelSet
        from camctl.persistence.models import DbOutcomeKind
        from camctl.persistence.repositories.cancellation import (
            CancellationRepository, FixCancelTargets)

        owned = pipeline
        outcome = CancellationRepository().fix_cancel_targets(
            FixCancelTargets(50, FixedCancelSet(), _NOW),
            new_operation_key(), owned)
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM cancel_items").fetchone() == (0,)
        assert _value(
            owned, "SELECT status, target_selection_state FROM actions"
            " WHERE id = 50") == (2, 1)


class TestCancelTargetsGuard:
    """登记名 cancel 守卫的直接事件正反例。"""

    @staticmethod
    def _state(owned):
        from camctl.persistence.transaction import row_facts

        tables = ("actions", "cancel_items", "auto_preview_links")
        return {
            table: {row[0]: row_facts(owned.connection, table, row[0])
                    for row in owned.connection.execute(
                        f"SELECT id FROM {table}").fetchall()}
            for table in tables}

    def _cancel_event(self, owned, members, *, action_facts=None):
        from dataclasses import replace

        from camctl.persistence.transaction import (
            event_envelope, row_change, update_change)

        rows = (update_change("actions", 50,
                              {"target_selection_state": 1},
                              {"target_selection_state": 2}),)
        owners = {("actions", 50): ("action", 50)}
        for index, values in enumerate(members):
            item_id = 91 + index
            full = {
                "action_id": 50,
                "target_action_id": values["target"],
                "selection_basis": values["basis"],
                "status": values.get("status", 1),
                "cancellation_effect": values.get("effect", 1),
                "outcome": None,
                "error_code": None,
                "error_details_json": None,
            }
            rows += (row_change("cancel_items", item_id, full),)
            owners[("cancel_items", item_id)] = ("action", 50)
        state = self._state(owned)
        state["cancel_items"] = {
            91 + index: dict(
                {"action_id": 50, "outcome": None, "error_code": None,
                 "error_details_json": None}, id=91 + index, **values)
            for index, values in enumerate(members)}
        if action_facts:
            state["actions"][50].update(action_facts)
        event = replace(
            event_envelope(2, 2, 4, 3, rows, _NOW), change_seq=2)
        return event, state, owners

    def _validate(self, owned, event, state, owners):
        from camctl.contracts.history_values import TransactionRange
        from camctl.history.validators import EventContext, validate_event

        validate_event(event, EventContext(
            TransactionRange(2, 2, 2), owners, state))

    def test_linked_member_requires_valid_auto_link(self, pipeline):
        owned = pipeline
        members = [{"target": 11, "basis": 1}, {"target": 21, "basis": 2}]
        event, state, owners = self._cancel_event(owned, members)
        # 有效自动关联存在：通过。
        self._validate(owned, event, state, owners)
        broken, broken_state, broken_owners = self._cancel_event(
            owned, members)
        broken_state["auto_preview_links"] = {}
        from camctl.history.validators import EventValidationError

        import pytest as _pytest
        with _pytest.raises(EventValidationError):
            self._validate(owned, broken, broken_state, broken_owners)

    def test_initial_values_must_be_pending_without_result(self, pipeline):
        from camctl.history.validators import EventValidationError

        import pytest as _pytest
        owned = pipeline
        for bad in ({"target": 11, "basis": 1, "status": 2},
                    {"target": 11, "basis": 1, "effect": 2}):
            event, state, owners = self._cancel_event(owned, [bad])
            with _pytest.raises(EventValidationError):
                self._validate(owned, event, state, owners)

    def test_fail_branch_rejects_members_and_foreign_error(self, pipeline):
        from dataclasses import replace

        from camctl.history.validators import EventValidationError
        from camctl.persistence.transaction import (
            event_envelope, row_change, update_change)

        import pytest as _pytest
        owned = pipeline
        state = self._state(owned)
        owners = {("actions", 50): ("action", 50)}
        fail = (update_change(
            "actions", 50,
            {"target_selection_state": 1, "status": 2,
             "error_code": None, "error_details_json": None},
            {"target_selection_state": 3, "status": 4, "error_code": 25,
             "error_details_json": {"target": {}}}),)
        event = replace(event_envelope(2, 2, 4, 4, fail, _NOW), change_seq=2)
        self._validate(owned, event, state, owners)
        with_member = fail + (row_change("cancel_items", 91, {
            "action_id": 50, "target_action_id": 11, "selection_basis": 1,
            "status": 1, "cancellation_effect": 1, "outcome": None,
            "error_code": None, "error_details_json": None}),)
        event_with = replace(
            event_envelope(2, 2, 4, 4, with_member, _NOW), change_seq=2)
        with _pytest.raises(EventValidationError):
            self._validate(
                owned, event_with,
                {**state, "cancel_items": {
                    91: {"id": 91, "action_id": 50, "target_action_id": 11,
                         "selection_basis": 1, "status": 1,
                         "cancellation_effect": 1, "outcome": None,
                         "error_code": None, "error_details_json": None}}},
                {**owners, ("cancel_items", 91): ("action", 50)})

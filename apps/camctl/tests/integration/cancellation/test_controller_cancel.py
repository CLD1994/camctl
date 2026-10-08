"""N4 取消动作自身被取消的组件集成测试。

真实仓储验证后一个取消的等待范围不扩大（C2 只等待 C1，A 的必要收
场独立继续）、发起者自身收场（未结束项转 CANCELED 并保留取消效果、
自身以 canceled 结束）、各边界中断后的幂等重入，以及 C2 同时包含
C1 与 A 时 A 复用原取消流程。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from camctl.cancellation.models import (
    StopWaitCancelItems,
    CancelTarget,
    FixedCancelSet,
    FixedTarget,
    ResolvedTargets,
    SelectionBasis,
    TargetFacts,
    CancellationEffect,
)
from camctl.cancellation.rules import summarize_cancel
from camctl.cancellation.service import ApplyCancel, apply_cancel
from camctl.cancellation.targets import prepare_cancel_set, resolve_cancel_target
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.cancellation import (
    CancellationRepository,
    SqliteCancelLookup,
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
    """A=11 执行中录像；C1=50 已对 A 生效取消（项 91 处理中）。"""
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
    # A：执行中录像，取消已生效（C1 施加）。
    connection.execute(
        "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
        " scheduled_at, group_name, input_fields_json, effective_params_json,"
        " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
        " cancel_requested, error_code, error_details_json, first_window_observed_at,"
        " expiration_reason, source_resolution_state, resolved_source_plan_id,"
        " target_selection_state, created_event_id, last_event_id, change_count)"
        " VALUES (11, 1, 0, 'rec-a', 2, 'cam-1', ?, NULL, '{}', '{}',"
        " 'camctl-adb', 1000, '{}', 2, 1, 1, NULL, NULL, NULL, NULL, NULL, NULL,"
        " NULL, 1, 1, 1)", (_NOW,))
    for action_id, plan_id, index, name, target_state in (
            (50, 2, 1, "cancel-a", 2), (60, 2, 2, "cancel-b", 1)):
        connection.execute(
            "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
            " scheduled_at, group_name, input_fields_json, effective_params_json,"
            " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
            " cancel_requested, error_code, error_details_json,"
            " first_window_observed_at, expiration_reason, source_resolution_state,"
            " resolved_source_plan_id, target_selection_state, created_event_id,"
            " last_event_id, change_count)"
            " VALUES (?, ?, ?, ?, 6, NULL, ?, NULL, ?, NULL, NULL, NULL, '{}',"
            " 2, 1, 0, NULL, NULL, NULL, NULL, NULL, NULL, ?, 1, 1, 1)",
            (action_id, plan_id, index, name, _NOW,
             json.dumps({"params": {"target": {"action_instance_id": "11"}}}),
             target_state))
    # C1 对 A 的取消已生效：项 91 处理中、效果已施加。
    connection.execute(
        "INSERT INTO cancel_items (id, action_id, target_action_id,"
        " selection_basis, status, cancellation_effect)"
        " VALUES (91, 50, 11, 1, 2, 2)")
    from ..scheduling.test_resources import _seed_activity

    _seed_activity(connection, 11, dispatch_state=3)
    connection.commit()
    yield owned
    owned.connection.close()


def _value(owned, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


def _fix_c2(owned, targets):
    from camctl.cancellation.models import FixCancelTargets

    outcome = CancellationRepository().fix_cancel_targets(
        FixCancelTargets(60, FixedCancelSet(targets=tuple(targets)), _NOW),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    return outcome.value.item_ids


class _Settlement:
    def __init__(self, outcomes) -> None:
        from camctl.cancellation.ports import SettlementOutcome

        self._defaults = {
            11: SettlementOutcome(complete=False),  # A 的停止仍在途
            50: SettlementOutcome(complete=True),   # C1 自身取消处理完成
        }
        self.overrides = dict(outcomes or {})
        self.calls: list[int] = []

    async def settle(self, target_action_id: int):
        from camctl.cancellation.ports import SettlementOutcome

        self.calls.append(target_action_id)
        return self.overrides.get(
            target_action_id,
            self._defaults.get(target_action_id,
                               SettlementOutcome(complete=True)))


def _runtime(owned, settlement):
    from camctl.cancellation.service import CancellationRuntime

    return CancellationRuntime(
        owned=owned, repository=CancellationRepository(),
        settlement=settlement, occurred_at=lambda: _NOW + 10)


class TestWaitScope:
    def test_second_cancel_does_not_expand_scope(self, pipeline):
        owned = pipeline
        resolution = resolve_cancel_target(
            CancelTarget(action_instance_id=50),
            SqliteCancelLookup(owned.connection))
        assert resolution.action_ids == (50,)
        fixed = prepare_cancel_set(60, ResolvedTargets(
            direct=(TargetFacts(action_id=50, terminal=False, may_cancel=True),),
            auto_candidates=()))
        assert isinstance(fixed, FixedCancelSet)
        _fix_c2(owned, fixed.targets)
        # C2 的成员只有 C1；A 的取消事实仍归 C1 名下。
        assert _value(
            owned, "SELECT target_action_id FROM cancel_items"
            " WHERE action_id = 60") == (50,)
        assert _value(
            owned, "SELECT action_id, cancellation_effect FROM cancel_items"
            " WHERE target_action_id = 11") == (50, 2)


class TestOriginSettlement:
    @pytest.mark.asyncio
    async def test_c1_canceled_c2_succeeded_a_continues(self, pipeline):
        """C1 canceled、C2 succeeded 与 A 仍停止可同时成立。"""
        from camctl.cancellation.service import (
            OriginSettle, settle_origin_cancel)

        owned = pipeline
        # C2 施加对 C1 的取消并等待其自身取消处理结束。
        _fix_c2(owned, (FixedTarget(
            action_id=50, basis=SelectionBasis.DIRECT,
            cancellation_effect=CancellationEffect.NOT_APPLIED),))
        progress = await apply_cancel(
            ApplyCancel(origin_action_id=60, item_ids=(92,)),
            _runtime(owned, _Settlement({})))
        assert [item.status for item in progress.items] == [3]
        assert _value(
            owned, "SELECT cancel_requested FROM actions WHERE id = 50") == (1,)
        # C1 完成自身收场：停止等待、未结束项转取消并保留效果。
        outcome = await settle_origin_cancel(
            OriginSettle(origin_action_id=50), _runtime(owned, _Settlement({})))
        assert outcome.value.action_status == 6, outcome
        # A 的取消事实保持，A 仍在原停止流程中。
        assert _value(
            owned, "SELECT status, cancel_requested FROM actions"
            " WHERE id = 11") == (2, 1)
        assert _value(
            owned, "SELECT status, cancellation_effect FROM cancel_items"
            " WHERE id = 91") == (5, 2)
        assert _value(
            owned, "SELECT status FROM actions WHERE id = 50") == (6,)

    @pytest.mark.asyncio
    async def test_interrupt_boundaries_reenter_idempotently(self, pipeline):
        """停止等待提交后、最终结果提交前中断：重入幂等完成。"""
        from camctl.cancellation.service import OriginSettle, settle_origin_cancel

        owned = pipeline
        owned.connection.execute(
            "UPDATE actions SET cancel_requested = 1 WHERE id = 50")
        owned.connection.commit()
        repository = CancellationRepository()
        # 边界一：先停止等待。
        stopped = repository.stop_wait_cancel_items(
            StopWaitCancelItems(50, _NOW), new_operation_key(), owned)
        assert stopped.kind is DbOutcomeKind.COMPLETED, stopped.error
        assert _value(
            owned, "SELECT status FROM cancel_items WHERE id = 91") == (5,)
        # 边界二：重入停止等待为幂等只读。
        before = tuple(owned.connection.execute(
            "SELECT id FROM history_events ORDER BY id").fetchall())
        again = repository.stop_wait_cancel_items(
            StopWaitCancelItems(50, _NOW), new_operation_key(), owned)
        assert again.kind is DbOutcomeKind.COMPLETED, again.error
        assert tuple(owned.connection.execute(
            "SELECT id FROM history_events ORDER BY id").fetchall()) == before
        # 边界三：最终结果提交后重入恢复。
        outcome = await settle_origin_cancel(
            OriginSettle(origin_action_id=50), _runtime(owned, _Settlement({})))
        assert outcome.value.action_status == 6
        settled = tuple(owned.connection.execute(
            "SELECT id FROM history_events ORDER BY id").fetchall())
        resumed = await settle_origin_cancel(
            OriginSettle(origin_action_id=50), _runtime(owned, _Settlement({})))
        assert resumed.value.action_status == 6
        assert tuple(owned.connection.execute(
            "SELECT id FROM history_events ORDER BY id").fetchall()) == settled

    @pytest.mark.asyncio
    async def test_c2_with_both_reuses_original_flow(self, pipeline):
        """C2 同时包含 C1 与 A：分别等待，A 复用原取消流程。"""
        owned = pipeline
        from camctl.cancellation.service import (
            OriginSettle, settle_origin_cancel)

        # C1 先以 canceled 结束。
        owned.connection.execute(
            "UPDATE actions SET cancel_requested = 1 WHERE id = 50")
        owned.connection.commit()
        await settle_origin_cancel(
            OriginSettle(origin_action_id=50), _runtime(owned, _Settlement({})))
        _fix_c2(owned, (
            FixedTarget(action_id=11, basis=SelectionBasis.DIRECT,
                        cancellation_effect=CancellationEffect.NOT_APPLIED),
            FixedTarget(action_id=50, basis=SelectionBasis.DIRECT,
                        cancellation_effect=CancellationEffect.NOT_APPLIED),
        ))
        progress = await apply_cancel(
            ApplyCancel(origin_action_id=60, item_ids=(92, 93)),
            _runtime(owned, _Settlement({})))
        by_target = {item.target_action_id: item for item in progress.items}
        # C1 已终态：按既有终态成功。
        assert by_target[50].status == 3
        # A 的取消已由 C1 生效：C2 复用原责任并继续等待 A 的收场。
        assert by_target[11].status == 2
        assert summarize_cancel(progress).status.value == "running"
        assert _value(
            owned, "SELECT status, cancellation_effect FROM cancel_items"
            " WHERE target_action_id = 11 AND action_id = 60") == (2, 2)
        # A 的原取消事实不被改写。
        assert _value(
            owned, "SELECT status, cancellation_effect FROM cancel_items"
            " WHERE id = 91") == (5, 2)

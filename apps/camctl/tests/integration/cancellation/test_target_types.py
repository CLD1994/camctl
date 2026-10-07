"""N5 目标类型取消组合的组件集成测试。

真实端口按目标类型分派：终态取回仍撤回 ready 交付（撤回事实单独
更新，processing 不删除）；清理动作已删项不阻止其他项取消；拍摄目
标等待执行链停止；报告动作按同步责任分类且共享生成不在范围；取消
动作目标转发发起者收场。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from camctl.cancellation.models import ApplyCancelTarget, CancelApplyMode
from camctl.cancellation.service import ApplyCancel, apply_cancel
from camctl.cancellation.settlement import TargetSettlement
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.cancellation import (
    CancellationRepository,
    register_cancellation_guards,
)
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.outputs import (
    OutputsRepository,
    register_outputs_guards,
)
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
    # 取回动作 21（已成功）带一份已发布交付 301。
    for action_id, kind, status, started in (
            (11, 2, 2, 1),      # 录像 A：执行中（等待执行链停止）
            (12, 2, 3, 1),      # 录像 B：已终态
            (21, 4, 3, 1),      # 取回：已成功
            (30, 5, 2, 1),      # 清理：执行中
            (40, 7, 2, 1),      # 报告：执行中
            (50, 6, 2, 1),      # 取消动作（承载者）
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
            " VALUES (?, 1, ?, ?, ?, ?, ?, NULL, '{}', ?, ?, ?, '{}', ?, 1,"
            " 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, 1, 1)",
            (action_id, action_id - 10, f"act-{action_id}", kind,
             "cam-1" if capture else None, _NOW,
             "{}" if capture else None,
             "camctl-adb" if capture else None,
             1000 if capture else None, status))
    connection.execute(
        "INSERT INTO device_files (id, observer_action_id, source_action_id,"
        " identity_key, locator_json, ownership_evidence_json, role,"
        " presence_state, completion_state, completion_evidence_json,"
        " checksum_support, size_bytes, created_event_id, last_event_id,"
        " change_count) VALUES (501, 11, 11, 'file-0501', '{}', '{}', 2, 2, 3,"
        " '{}', 2, 10, 1, 1, 1)")
    connection.execute(
        "INSERT INTO outputs (id, source_action_id, kind, device_file_id,"
        " availability, cleanup_status, media_json, created_event_id,"
        " last_event_id, change_count) VALUES (701, 11, 1, 501, 2, 2, '{}',"
        " 1, 1, 1)")
    connection.execute(
        "INSERT INTO deliveries (id, action_id, output_id, file_name,"
        " display_name, status, publication_intent_event_id, published_event_id,"
        " error_json, withdrawal_state, withdrawal_error_json,"
        " created_event_id, last_event_id, change_count)"
        " VALUES (301, 21, 701, '301.mp4', 'd', 5, 1, 1, NULL, 1, NULL,"
        " 1, 1, 1)")
    # 取消与清理动作的目标集合均已固定。
    connection.execute(
        "UPDATE actions SET target_selection_state = 2 WHERE id IN (50, 30)")
    # 取消动作 50 已固定集合：91→取回 21、92→清理 30、93→录像 11、
    # 94→已终态录像 12、95→报告 40。
    for item_id, target_id in ((91, 21), (92, 30), (93, 11), (94, 12), (95, 40)):
        connection.execute(
            "INSERT INTO cancel_items (id, action_id, target_action_id,"
            " selection_basis, status, cancellation_effect)"
            " VALUES (?, 50, ?, 1, 1, 1)", (item_id, target_id))
    # 清理动作 30 的成员：701 已删（成功）、702 待删除。
    connection.execute(
        "INSERT INTO device_files (id, observer_action_id, source_action_id,"
        " identity_key, locator_json, ownership_evidence_json, role,"
        " presence_state, completion_state, completion_evidence_json,"
        " checksum_support, size_bytes, created_event_id, last_event_id,"
        " change_count) VALUES (502, 11, 11, 'file-0502', '{}', '{}', 2, 2, 3,"
        " '{}', 2, 10, 1, 1, 1)")
    connection.execute(
        "INSERT INTO outputs (id, source_action_id, kind, device_file_id,"
        " availability, cleanup_status, media_json, created_event_id,"
        " last_event_id, change_count) VALUES (702, 11, 1, 502, 2, 2, '{}',"
        " 1, 1, 1)")
    connection.execute(
        "INSERT INTO cleanup_items (id, action_id, requested_output_id, output_id,"
        " status, restriction_state, outcome, final_event_id)"
        " VALUES (81, 30, 701, 701, 4, 4, 1, 1)")
    connection.execute(
        "INSERT INTO cleanup_items (id, action_id, requested_output_id, output_id,"
        " status, restriction_state) VALUES (82, 30, 702, 702, 2, 2)")
    connection.commit()
    yield owned
    owned.connection.close()


def _value(owned, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


def _settlement(owned, positions):
    return TargetSettlement(
        owned=owned,
        outputs=OutputsRepository(),
        cancellations=CancellationRepository(),
        withdrawal_positions=lambda delivery_id: positions.get(
            delivery_id, "unknown"),
        occurred_at=lambda: _NOW + 10)


def _runtime(owned, settlement):
    from camctl.cancellation.service import CancellationRuntime

    return CancellationRuntime(
        owned=owned, repository=CancellationRepository(),
        settlement=settlement, occurred_at=lambda: _NOW + 10)


@pytest.mark.asyncio
class TestObtainWithdrawal:
    async def test_terminal_obtain_still_withdraws_ready(self, pipeline):
        """原取回已成功而 ready 可撤：终态保持，撤回事实单独更新。"""
        owned = pipeline
        settlement = _settlement(owned, {301: "ready"})
        progress = await apply_cancel(
            ApplyCancel(origin_action_id=50, item_ids=(91,)),
            _runtime(owned, settlement))
        assert progress.items[0].status == 3, progress
        # 取回终态保持成功；交付与撤回明细单独更新。
        assert _value(
            owned, "SELECT status FROM actions WHERE id = 21") == (3,)
        assert _value(
            owned, "SELECT status, withdrawal_state FROM deliveries"
            " WHERE id = 301") == (8, 3)
        assert _value(
            owned, "SELECT status FROM cancel_delivery_items"
            " WHERE delivery_id = 301") == (2,)
        # 取消项按既有终态成功。
        assert _value(
            owned, "SELECT status, outcome FROM cancel_items"
            " WHERE id = 91") == (3, 2)

    async def test_processing_delivery_is_not_retractable(self, pipeline):
        """processing 中的交付不可撤回且不删除；取消仍可成功。"""
        owned = pipeline
        settlement = _settlement(owned, {301: "processing"})
        progress = await apply_cancel(
            ApplyCancel(origin_action_id=50, item_ids=(91,)),
            _runtime(owned, settlement))
        assert progress.items[0].status == 3, progress
        assert _value(
            owned, "SELECT status, withdrawal_state FROM deliveries"
            " WHERE id = 301") == (5, 4)
        assert _value(
            owned, "SELECT status FROM cancel_delivery_items"
            " WHERE delivery_id = 301") == (3,)

    async def test_unknown_position_keeps_waiting(self, pipeline):
        owned = pipeline
        settlement = _settlement(owned, {})
        progress = await apply_cancel(
            ApplyCancel(origin_action_id=50, item_ids=(91,)),
            _runtime(owned, settlement))
        assert progress.items[0].status == 2, progress
        assert _value(
            owned, "SELECT withdrawal_state FROM deliveries"
            " WHERE id = 301") == (2,)


@pytest.mark.asyncio
class TestCleanupSettlement:
    async def test_deleted_item_does_not_block_others(self, pipeline):
        """清理动作目标：已删项保持，待删除项取消后动作终态化为取消。"""
        owned = pipeline
        owned.connection.execute(
            "UPDATE actions SET cancel_requested = 1 WHERE id = 30")
        owned.connection.execute(
            "UPDATE cancel_items SET status = 2, cancellation_effect = 2"
            " WHERE id = 92")
        owned.connection.commit()
        settlement = _settlement(owned, {})
        result = await settlement.settle(30)
        assert result.complete and not result.failed, result
        assert _value(
            owned, "SELECT status FROM cleanup_items WHERE id = 81") == (4,)
        assert _value(
            owned, "SELECT status, restriction_state FROM cleanup_items"
            " WHERE id = 82") == (6, 3)
        # 成员全部终态后结算把目标动作终态化为取消。
        assert _value(
            owned, "SELECT status FROM actions WHERE id = 30") == (6,)
        # 已终态目标重入结算：按既有事实返回，不重复登记。
        again = await settlement.settle(30)
        assert again.complete and not again.failed, again


@pytest.mark.asyncio
class TestCaptureAndReportSettlement:
    async def test_running_capture_waits_for_execution_chain(self, pipeline):
        owned = pipeline
        settlement = _settlement(owned, {})
        assert (await settlement.settle(11)).complete is False
        # 已终态拍摄：等待结束，本次有限处理完成。
        assert (await settlement.settle(12)).complete is True

    async def test_report_action_settles_without_shared_generation(
            self, pipeline):
        """报告动作目标：本次责任分类完成，共享生成不在目标范围。"""
        owned = pipeline
        settlement = _settlement(owned, {})
        result = await settlement.settle(40)
        assert result.complete and not result.failed, result


@pytest.mark.asyncio
class TestCancelTaskSettlementDelegation:
    async def test_cancel_action_target_delegates_to_origin_settle(
            self, pipeline):
        """取消动作目标经真实端口完成发起者收场。"""
        from camctl.cancellation.models import FinishCancelAction

        owned = pipeline
        owned.connection.execute(
            "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
            " scheduled_at, group_name, input_fields_json, effective_params_json,"
            " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
            " cancel_requested, error_code, error_details_json,"
            " first_window_observed_at, expiration_reason, source_resolution_state,"
            " resolved_source_plan_id, target_selection_state, created_event_id,"
            " last_event_id, change_count)"
            " VALUES (60, 2, 1, 'cancel-b', 6, NULL, ?, NULL, ?, NULL, NULL, NULL,"
            " '{}', 2, 1, 0, NULL, NULL, NULL, NULL, NULL, NULL, 1, 1, 1, 1)",
            (_NOW, json.dumps({"params": {"target": {"action_instance_id": "50"}}})))
        owned.connection.commit()
        # C2 经真实流程固定并施加对 C1 的取消，端口转发发起者收场。
        from camctl.cancellation.models import FixedCancelSet, FixedTarget
        from camctl.cancellation.models import CancellationEffect, SelectionBasis
        from camctl.persistence.repositories.cancellation import (
            CancellationRepository as Repository)
        from camctl.contracts.values import new_operation_key as key
        from camctl.cancellation.models import FixCancelTargets

        fixed = Repository().fix_cancel_targets(
            FixCancelTargets(60, FixedCancelSet(targets=(
                FixedTarget(action_id=50, basis=SelectionBasis.DIRECT,
                            cancellation_effect=(
                                CancellationEffect.NOT_APPLIED)),)), _NOW),
            key(), owned)
        assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
        settlement = _settlement(owned, {})
        progress = await apply_cancel(
            ApplyCancel(origin_action_id=60, item_ids=fixed.value.item_ids),
            _runtime(owned, settlement))
        assert [item.status for item in progress.items] == [3]
        assert _value(
            owned, "SELECT status FROM actions WHERE id = 50") == (6,)
        assert _value(
            owned, "SELECT status FROM cancel_items WHERE action_id = 60"
        ) == (3,)

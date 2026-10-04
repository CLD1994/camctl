"""N3 取消生效与逐目标独立收场的组件集成测试。

真实仓储保存目标取消生效（CANCEL_CHANGED.APPLY 按资格分区）、逐项
最终结果（RESULT）与动作汇总终态；服务编排消费 N1 固定集合与 N2 资
格，停止收场经端口替身推进：标记已保存但停止在途仍 running，任一失
败不放弃其他有限处理，迟到结果与重启后汇总保持。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from camctl.cancellation.models import (
    ApplyCancelTarget,
    CancelApplyMode,
    CancelOutcomeChoice,
    FinishCancelAction,
    RecordCancelResult,
)
from camctl.cancellation.ports import SettlementOutcome
from camctl.cancellation.service import ApplyCancel, apply_cancel
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.cancellation import (
    CancellationRepository,
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
    # 11：执行中录像（已启动）；12：未启动录像；13：已终态录像。
    for action_id, index, name, status in (
            (11, 0, "rec-a", 2), (12, 1, "rec-b", 1), (13, 2, "rec-c", 3)):
        connection.execute(
            "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
            " scheduled_at, group_name, input_fields_json, effective_params_json,"
            " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
            " cancel_requested, error_code, error_details_json,"
            " first_window_observed_at, expiration_reason, source_resolution_state,"
            " resolved_source_plan_id, target_selection_state, created_event_id,"
            " last_event_id, change_count)"
            " VALUES (?, 1, ?, ?, 2, 'cam-1', ?, NULL, '{}', '{}',"
            " 'camctl-adb', 1000, '{}', ?, ?, 0, NULL, NULL, NULL, NULL, NULL,"
            " NULL, NULL, 1, 1, 1)",
            (action_id, index, name, _NOW, status,
             0 if status == 1 else 1))
    connection.execute(
        "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
        " scheduled_at, group_name, input_fields_json, effective_params_json,"
        " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
        " cancel_requested, error_code, error_details_json, first_window_observed_at,"
        " expiration_reason, source_resolution_state, resolved_source_plan_id,"
        " target_selection_state, created_event_id, last_event_id, change_count)"
        " VALUES (50, 2, 1, 'cancel', 6, NULL, ?, NULL, ?, NULL, NULL, NULL, '{}',"
        " 2, 1, 0, NULL, NULL, NULL, NULL, NULL, NULL, 2, 1, 1, 1)",
        (_NOW, json.dumps({"params": {"target": {"plan_instance_id": "1"}}})))
    for item_id, target_id in ((91, 11), (92, 12), (93, 13)):
        connection.execute(
            "INSERT INTO cancel_items (id, action_id, target_action_id,"
            " selection_basis, status, cancellation_effect)"
            " VALUES (?, 50, ?, 1, 1, 1)", (item_id, target_id))
    connection.commit()
    yield owned
    owned.connection.close()


def _value(owned, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


class SettlementDouble:
    """目标收场端口替身：按目标返回完成/失败/在途。"""

    def __init__(self, outcomes) -> None:
        self.outcomes = dict(outcomes)
        self.calls: list[int] = []

    async def settle(self, target_action_id: int):
        self.calls.append(target_action_id)
        return self.outcomes.get(
            target_action_id, SettlementOutcome(complete=True))


def _runtime(owned, settlement):
    from camctl.cancellation.service import CancellationRuntime

    return CancellationRuntime(
        owned=owned, repository=CancellationRepository(),
        settlement=settlement, occurred_at=lambda: _NOW + 10)


class TestApplyTransactions:
    def test_pre_start_target_cancels_immediately(self, pipeline):
        owned = pipeline
        outcome = CancellationRepository().apply_cancel_target(
            ApplyCancelTarget(92, CancelApplyMode.PRE_START, _NOW),
            new_operation_key(), owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert _value(
            owned, "SELECT status, cancellation_effect, outcome"
            " FROM cancel_items WHERE id = 92") == (3, 2, 1)
        assert _value(
            owned, "SELECT status, cancel_requested FROM actions"
            " WHERE id = 12") == (6, 1)

    def test_terminal_target_succeeds_without_rewriting(self, pipeline):
        owned = pipeline
        outcome = CancellationRepository().apply_cancel_target(
            ApplyCancelTarget(93, CancelApplyMode.TERMINAL, _NOW),
            new_operation_key(), owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert _value(
            owned, "SELECT status, cancellation_effect, outcome"
            " FROM cancel_items WHERE id = 93") == (3, 3, 2)
        assert _value(
            owned, "SELECT status, cancel_requested FROM actions"
            " WHERE id = 13") == (3, 0)

    def test_with_stop_marks_target_and_waits(self, pipeline):
        owned = pipeline
        outcome = CancellationRepository().apply_cancel_target(
            ApplyCancelTarget(91, CancelApplyMode.WITH_STOP, _NOW),
            new_operation_key(), owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        # 标记已保存、项进入处理中：不是完成。
        assert _value(
            owned, "SELECT status, cancellation_effect, outcome"
            " FROM cancel_items WHERE id = 91") == (2, 2, None)
        assert _value(
            owned, "SELECT status, cancel_requested FROM actions"
            " WHERE id = 11") == (2, 1)

    def test_rejected_target_fails_without_touching_target(self, pipeline):
        owned = pipeline
        outcome = CancellationRepository().record_cancel_result(
            RecordCancelResult(
                91, _NOW, code="task_cancel_unsupported",
                details={"action_instance_id": "11"}),
            new_operation_key(), owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert _value(
            owned, "SELECT status, cancellation_effect, error_code"
            " FROM cancel_items WHERE id = 91") == (4, 1, 1)
        assert _value(
            owned, "SELECT status, cancel_requested FROM actions"
            " WHERE id = 11") == (2, 0)

    def test_apply_is_idempotent_and_recoverable(self, pipeline):
        owned = pipeline
        repository = CancellationRepository()
        key = new_operation_key()
        first = repository.apply_cancel_target(
            ApplyCancelTarget(91, CancelApplyMode.WITH_STOP, _NOW), key, owned)
        assert first.value.disposition.value == "saved"
        before = tuple(owned.connection.execute(
            "SELECT id FROM history_events ORDER BY id").fetchall())
        resend = repository.apply_cancel_target(
            ApplyCancelTarget(91, CancelApplyMode.WITH_STOP, _NOW), key, owned)
        assert resend.value.disposition.value == "already"
        again = repository.apply_cancel_target(
            ApplyCancelTarget(91, CancelApplyMode.WITH_STOP, _NOW + 5),
            new_operation_key(), owned)
        assert again.value.disposition.value == "already"
        assert tuple(owned.connection.execute(
            "SELECT id FROM history_events ORDER BY id").fetchall()) == before


class TestFinishCancelAction:
    def _finish(self, owned, occurred_at=_NOW, key=None):
        return CancellationRepository().finish_cancel_action(
            FinishCancelAction(50, occurred_at),
            key or new_operation_key(), owned)

    def test_pending_items_reject_finish(self, pipeline):
        owned = pipeline
        outcome = self._finish(owned)
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        assert _value(
            owned, "SELECT status FROM actions WHERE id = 50") == (2,)

    def test_any_failure_fails_action_with_machine_error(self, pipeline):
        owned = pipeline
        repository = CancellationRepository()
        repository.apply_cancel_target(
            ApplyCancelTarget(92, CancelApplyMode.PRE_START, _NOW),
            new_operation_key(), owned)
        repository.apply_cancel_target(
            ApplyCancelTarget(93, CancelApplyMode.TERMINAL, _NOW),
            new_operation_key(), owned)
        repository.record_cancel_result(
            RecordCancelResult(
                91, _NOW, code="task_cancel_unsupported",
                details={"action_instance_id": "11"}),
            new_operation_key(), owned)
        outcome = self._finish(owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert _value(
            owned, "SELECT status, error_code, error_details_json"
            " FROM actions WHERE id = 50") == (4, 23, "{}")

    def test_all_succeeded_completes_action_and_plan(self, pipeline):
        owned = pipeline
        repository = CancellationRepository()
        for item_id, mode in ((91, CancelApplyMode.WITH_STOP),
                              (92, CancelApplyMode.PRE_START),
                              (93, CancelApplyMode.TERMINAL)):
            repository.apply_cancel_target(
                ApplyCancelTarget(item_id, mode, _NOW),
                new_operation_key(), owned)
        repository.record_cancel_result(
            RecordCancelResult(91, _NOW, outcome=CancelOutcomeChoice.CANCELED),
            new_operation_key(), owned)
        outcome = self._finish(owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert _value(
            owned, "SELECT status, error_code FROM actions WHERE id = 50"
        ) == (3, None)
        assert _value(
            owned, "SELECT status FROM plans WHERE id = 2") == (3,)

    def test_terminal_new_key_recovers_without_new_history(self, pipeline):
        owned = pipeline
        repository = CancellationRepository()
        for item_id, mode in ((91, CancelApplyMode.WITH_STOP),
                              (92, CancelApplyMode.PRE_START),
                              (93, CancelApplyMode.TERMINAL)):
            repository.apply_cancel_target(
                ApplyCancelTarget(item_id, mode, _NOW),
                new_operation_key(), owned)
        repository.record_cancel_result(
            RecordCancelResult(91, _NOW, outcome=CancelOutcomeChoice.CANCELED),
            new_operation_key(), owned)
        key = new_operation_key()
        first = self._finish(owned, key=key)
        before = tuple(owned.connection.execute(
            "SELECT id FROM history_events ORDER BY id").fetchall())
        resend = self._finish(owned, key=key)
        assert resend.value.disposition.value == "already"
        again = self._finish(owned, occurred_at=_NOW + 5)
        assert again.value.disposition.value == "already"
        assert tuple(owned.connection.execute(
            "SELECT id FROM history_events ORDER BY id").fetchall()) == before


class TestApplyCancelService:
    @pytest.mark.asyncio
    async def test_cancel_mark_is_not_settlement(self, pipeline):
        """集成命名用例：标记提交而停止仍在途，汇总保持 running。"""
        from camctl.cancellation.rules import summarize_cancel
        from camctl.cancellation.models import CancellationStatus

        owned = pipeline
        settlement = SettlementDouble({11: SettlementOutcome(complete=False)})
        progress = await apply_cancel(
            ApplyCancel(origin_action_id=50, item_ids=(91, 92, 93)),
            _runtime(owned, settlement))
        assert summarize_cancel(progress).status \
            is CancellationStatus.RUNNING
        # 11 的停止收场已发起且只发起一次。
        assert settlement.calls == [11]
        assert _value(
            owned, "SELECT status FROM cancel_items WHERE id = 91") == (2,)

    @pytest.mark.asyncio
    async def test_failure_does_not_abandon_other_items(self, pipeline):
        owned = pipeline
        settlement = SettlementDouble(
            {11: SettlementOutcome(complete=True, failed=True)})
        progress = await apply_cancel(
            ApplyCancel(origin_action_id=50, item_ids=(91, 92, 93)),
            _runtime(owned, settlement))
        by_target = {item.target_action_id: item for item in progress.items}
        assert by_target[11].status == 4
        assert by_target[12].status == 3
        assert by_target[13].status == 3
        assert _value(
            owned, "SELECT error_code FROM cancel_items WHERE id = 91"
        ) == (2,)

    @pytest.mark.asyncio
    async def test_late_result_enables_restart_summary(self, pipeline):
        """迟到结果：生效轮次后补充结果，重启侧汇总与终态保存成立。"""
        from camctl.cancellation.rules import summarize_cancel
        from camctl.cancellation.models import CancellationStatus

        owned = pipeline
        settlement = SettlementDouble({11: SettlementOutcome(complete=False)})
        await apply_cancel(
            ApplyCancel(origin_action_id=50, item_ids=(91, 92, 93)),
            _runtime(owned, settlement))
        # 重启后新服务以新键处理迟到结果并汇总。
        late = SettlementDouble({11: SettlementOutcome(complete=True)})
        progress = await apply_cancel(
            ApplyCancel(origin_action_id=50, item_ids=(91, 92, 93)),
            _runtime(owned, late))
        assert summarize_cancel(progress).status \
            is CancellationStatus.SUCCEEDED
        finish = CancellationRepository().finish_cancel_action(
            FinishCancelAction(50, _NOW + 20), new_operation_key(), owned)
        assert finish.kind is DbOutcomeKind.COMPLETED, finish.error
        assert _value(
            owned, "SELECT status FROM actions WHERE id = 50") == (3,)

"""N6 所有目标入口与恢复验收的组件集成测试。

四类寻址入口（动作、组、计划、请求）对同一事实矩阵产生各自独立
推导的逐项结果；已拒绝任务后来自然结束不改写旧取消失败，同一取
消的事务重送不重复效果；固定边界后继续收场，旧历史事实字节保持；
全部有限处理结束后取消动作成功结束，不等待设备或客户端。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from camctl.cancellation.models import (
    CancelApplyMode,
    CancelTarget,
    FixedCancelSet,
    FixedTarget,
    CancellationEffect,
    SelectionBasis,
)
from camctl.cancellation.rules import (
    decide_cancel_eligibility,
    load_eligibility_facts,
)
from camctl.cancellation.service import ApplyCancel, apply_cancel
from camctl.cancellation.settlement import TargetSettlement
from camctl.cancellation.targets import (
    prepare_cancel_set,
    resolve_cancel_target,
)
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.cancellation import (
    CancellationRepository,
    SqliteCancelLookup,
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
_TICK = _NOW + 10

#: 计划 1 的目标事实：A=11 执行中且无停止能力、B=12 未启动、
#: C=13 已终态、P=21 已成功取回（关联 A 的自动预览，ready 交付）。
_A, _B, _C, _P = 11, 12, 13, 21
#: 四个取消动作：动作入口、组入口、计划入口、请求入口。
_ACTION_CANCEL, _GROUP_CANCEL, _PLAN_CANCEL, _REQUEST_CANCEL = 70, 71, 72, 73


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
    for action_id, kind, group, status in (
            (_A, 2, "g", 2), (_B, 2, "g", 1), (_C, 3, None, 3),
            (_P, 4, None, 3)):
        capture = kind in (1, 2, 3)
        connection.execute(
            "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
            " scheduled_at, group_name, input_fields_json, effective_params_json,"
            " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
            " cancel_requested, error_code, error_details_json,"
            " first_window_observed_at, expiration_reason, source_resolution_state,"
            " resolved_source_plan_id, target_selection_state, created_event_id,"
            " last_event_id, change_count)"
            " VALUES (?, 1, ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?, ?, ?, ?, 0,"
            " NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, 1, 1)",
            (action_id, action_id - 10, f"act-{action_id}", kind,
             "cam-1" if capture else None, _NOW, group,
             "{}" if capture else None,
             "camctl-adb" if capture else None,
             1000 if capture else None,
             json.dumps({"end_control": 1, "stop_supported": True})
             if kind == 3 else "{}",
             status, 0 if status == 1 else 1))
    # A 的设备活动：已启动且不支持停止；B 未建档。
    connection.execute(
        "INSERT INTO device_activities (id, action_id, task_key, task_locator_json,"
        " state_query_supported, stop_supported, safe_repeat_stop,"
        " start_return_meaning, completion_mode, ownership_mode, output_scope_json,"
        " baseline_state, baseline_first_event_id, baseline_last_event_id,"
        " dispatch_state, activity_state, occupancy_state, sent_at, started_at,"
        " result_wait_margin_ms, extra_wait_ms_used, expected_check_at,"
        " wait_completed_event_id, capture_json, control_elapsed_ns,"
        " completion_basis, completion_evidence_json, result_set_state,"
        " last_error_json)"
        " VALUES (1, ?, ?, NULL, 1, 0, 0, 1, 1, 1, '{}', 1, NULL, NULL, 3, 1,"
        " 1, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, NULL)",
        (_A, f"{_A:032x}"))
    # P 的已发布交付（ready）与 A 的自动预览关联。
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
        " last_event_id, change_count) VALUES (701, 11, 1, 501, 1, 1, '{}',"
        " 1, 1, 1)")
    connection.execute(
        "INSERT INTO deliveries (id, action_id, output_id, file_name,"
        " display_name, status, publication_intent_event_id, published_event_id,"
        " error_json, withdrawal_state, withdrawal_error_json,"
        " created_event_id, last_event_id, change_count)"
        " VALUES (301, 21, 701, '301.mp4', 'd', 5, 1, 1, NULL, 1, NULL, 1, 1, 1)")
    connection.execute(
        "INSERT INTO auto_preview_links (id, obtain_action_id, source_action_id,"
        " preview_support, parameter_type, is_valid)"
        " VALUES (61, 21, 11, 1, 'timed', 1)")
    # 四个取消动作（计划 2 承载），目标均为 PENDING。
    for action_id, index, params in (
            (_ACTION_CANCEL, 1, {"action_instance_id": str(_A)}),
            (_GROUP_CANCEL, 2, {"plan_instance_id": "1", "group": "g"}),
            (_PLAN_CANCEL, 3, {"plan_instance_id": "1"}),
            (_REQUEST_CANCEL, 4, {"request_id": "4242"})):
        connection.execute(
            "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
            " scheduled_at, group_name, input_fields_json, effective_params_json,"
            " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
            " cancel_requested, error_code, error_details_json,"
            " first_window_observed_at, expiration_reason, source_resolution_state,"
            " resolved_source_plan_id, target_selection_state, created_event_id,"
            " last_event_id, change_count)"
            " VALUES (?, 2, ?, ?, 6, NULL, ?, NULL, ?, NULL, NULL, NULL, '{}',"
            " 2, 1, 0, NULL, NULL, NULL, NULL, NULL, NULL, 1, 1, 1, 1)",
            (action_id, index, f"cancel-{action_id}", _NOW,
             json.dumps({"params": {"target": params}})))
    connection.commit()
    yield owned
    owned.connection.close()


def _value(owned, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


def _settlement(owned):
    from unittest.mock import create_autospec

    from camctl.contracts.enums import enum_for
    from camctl.host_files.handoff import (
        HandoffIdentity, WithdrawResult, WithdrawStage, withdraw_file,
    )
    from camctl.outputs.handoff import AdvanceWithdrawal, WithdrawalChoice

    outputs = OutputsRepository()
    states = enum_for("deliveries.withdrawal_state")
    # 本矩阵只控制实际 ready 删除；撤回请求、结果与等待明细仍由
    # 真实仓储保存。实际文件与归属协作见 test_target_types。
    remove_ready = create_autospec(
        withdraw_file, spec_set=True,
        return_value=WithdrawResult(WithdrawStage.WITHDRAWN, None))

    def save(delivery_id, choice):
        result = outputs.advance_withdrawal(
            AdvanceWithdrawal(delivery_id, choice, _TICK),
            new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error

    async def execute_withdrawal(delivery_id):
        name, state = _value(
            owned, "SELECT file_name,withdrawal_state FROM deliveries WHERE id=?",
            delivery_id)
        if state == int(states.NOT_REQUESTED):
            save(delivery_id, WithdrawalChoice.REQUESTED)
            state = int(states.PENDING)
        if state == int(states.PENDING):
            actual = await remove_ready(
                HandoffIdentity(Path(owned.metadata.ready_path), name))
            assert actual.stage is WithdrawStage.WITHDRAWN
        else:
            # 后到的请求采用原可靠结果，不再次执行 ready 删除。
            assert state == int(states.WITHDRAWN), state
        save(delivery_id, WithdrawalChoice.WITHDRAWN)
        return True

    return TargetSettlement(
        owned=owned, outputs=outputs,
        cancellations=CancellationRepository(),
        withdrawal_positions=lambda delivery_id: "ready",
        occurred_at=lambda: _TICK,
        withdrawal_execute=execute_withdrawal)


def _runtime(owned):
    from camctl.cancellation.service import CancellationRuntime

    return CancellationRuntime(
        owned=owned, repository=CancellationRepository(),
        settlement=_settlement(owned), occurred_at=lambda: _TICK)


def _eligibility(owned, target_id):
    return decide_cancel_eligibility(
        load_eligibility_facts(owned.connection, target_id))


def _expected_results(owned, direct_ids):
    """按目标事实独立推导逐项结果（不读取取消流程的任何状态）。"""
    from camctl.cancellation.rules import CancelEligibility

    expected = {}
    for target_id in direct_ids:
        eligibility = _eligibility(owned, target_id)
        if eligibility is CancelEligibility.TERMINAL:
            expected[target_id] = ("succeeded", 2)
        elif eligibility is CancelEligibility.ALLOW_PRE_START:
            expected[target_id] = ("succeeded", 1)
        elif eligibility is CancelEligibility.REJECT_UNSUPPORTED:
            expected[target_id] = ("failed", 1)
        else:
            raise AssertionError(f"矩阵目标应无其他分区: {target_id}")
    return expected


async def _run_cancel(owned, cancel_action_id, target):
    """真实链：寻址→自包含→固定→生效→收场→汇总→终态。"""
    from camctl.cancellation.models import FixCancelTargets, ResolvedTargets
    from camctl.persistence.repositories.cancellation import (
        sqlite_auto_candidates)

    repository = CancellationRepository()
    resolution = resolve_cancel_target(
        target, SqliteCancelLookup(owned.connection))
    assert not resolution.missing and resolution.lookup_failed is None
    facts = _facts_of(owned, resolution.action_ids)
    fixed = prepare_cancel_set(cancel_action_id, ResolvedTargets(
        direct=facts,
        auto_candidates=sqlite_auto_candidates(
            owned.connection, resolution.action_ids)))
    assert hasattr(fixed, "targets"), fixed
    saved = repository.fix_cancel_targets(
        FixCancelTargets(cancel_action_id, fixed, _TICK),
        new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    progress = await apply_cancel(
        ApplyCancel(origin_action_id=cancel_action_id,
                    item_ids=saved.value.item_ids),
        _runtime(owned))
    from camctl.cancellation.models import FinishCancelAction
    from camctl.cancellation.rules import summarize_cancel
    summary = summarize_cancel(progress)
    finish = repository.finish_cancel_action(
        FinishCancelAction(cancel_action_id, _TICK),
        new_operation_key(), owned)
    assert finish.kind is DbOutcomeKind.COMPLETED, finish.error
    return progress, summary, finish.value


def _facts_of(owned, action_ids):
    from camctl.cancellation.models import TargetFacts
    from camctl.cancellation.rules import may_apply_cancel

    terminal = {
        row[0] for row in owned.connection.execute(
            "SELECT id FROM actions WHERE status IN (3, 4, 5, 6)")}
    return tuple(
        TargetFacts(
            action_id=identity, terminal=identity in terminal,
            may_cancel=may_apply_cancel(_eligibility(owned, identity)))
        for identity in action_ids)


def _item_results(owned, cancel_action_id):
    rows = owned.connection.execute(
        "SELECT c.target_action_id, c.status, c.outcome, c.error_code"
        " FROM cancel_items c WHERE c.action_id = ? ORDER BY c.target_action_id",
        (cancel_action_id,)).fetchall()
    return {
        row[0]: (("succeeded", row[2]) if row[1] == 3
                 else ("failed", row[3]))
        for row in rows}


class TestEntryMatrix:
    @pytest.mark.asyncio
    async def test_four_entries_match_independent_results(self, pipeline):
        owned = pipeline
        cases = {
            _ACTION_CANCEL: (CancelTarget(action_instance_id=_A), [_A]),
            _GROUP_CANCEL: (CancelTarget(plan_instance_id=1, group="g"),
                            [_A, _B]),
            _PLAN_CANCEL: (CancelTarget(plan_instance_id=1),
                           [_A, _B, _C, _P]),
            _REQUEST_CANCEL: (CancelTarget(request_id="4242"),
                              [_A, _B, _C, _P]),
        }
        results = {}
        for cancel_id, (target, direct_ids) in cases.items():
            # 期望值在每个入口施加取消前按目标事实独立推导；前序
            # 入口的取消会改变目标状态，后续入口按演进后事实推导。
            expected = _expected_results(owned, direct_ids)
            _, summary, finished = await _run_cancel(owned, cancel_id, target)
            assert summary.status.value == "failed", (cancel_id, summary.status)
            assert finished.action_status == 4
            results[cancel_id] = _item_results(owned, cancel_id)
            assert results[cancel_id] == expected, \
                (cancel_id, results[cancel_id], expected)
        # 计划与请求入口覆盖同一集合，逐项结果一致。
        assert results[_PLAN_CANCEL] == results[_REQUEST_CANCEL]

    @pytest.mark.asyncio
    async def test_cancel_results_survive_restart(self, pipeline):
        """已拒绝任务后来自然结束不改旧取消失败；重送不重复效果。"""
        owned = pipeline
        _, _, finished = await _run_cancel(
            owned, _PLAN_CANCEL, CancelTarget(plan_instance_id=1))
        assert finished.action_status == 4
        # 边界 H：固定已保存事实的字节。
        boundary = tuple(owned.connection.execute(
            "SELECT id, body_json FROM history_events ORDER BY id").fetchall())
        rejected = _value(
            owned, "SELECT status, error_code FROM cancel_items"
            " WHERE action_id = ? AND target_action_id = ?",
            _PLAN_CANCEL, _A)
        assert rejected == (4, 1)
        # 已拒绝的 A 后来自然结束：旧取消失败不被改写。
        owned.connection.execute(
            "UPDATE actions SET status = 3 WHERE id = ?", (_A,))
        owned.connection.commit()
        from camctl.cancellation.models import (
            CancelOutcomeChoice, RecordCancelResult)

        rewrite = CancellationRepository().record_cancel_result(
            RecordCancelResult(
                _value(owned, "SELECT id FROM cancel_items WHERE action_id = ?"
                       " AND target_action_id = ?", _PLAN_CANCEL, _A)[0],
                _TICK, outcome=CancelOutcomeChoice.CANCELED),
            new_operation_key(), owned)
        assert rewrite.value.disposition.value == "already", rewrite
        assert _value(
            owned, "SELECT status, error_code FROM cancel_items"
            " WHERE action_id = ? AND target_action_id = ?",
            _PLAN_CANCEL, _A) == (4, 1)
        # 同一取消的事务重送不重复效果；旧历史事实字节保持。
        from camctl.cancellation.models import FixCancelTargets

        repository = CancellationRepository()
        resend = repository.fix_cancel_targets(
            FixCancelTargets(_PLAN_CANCEL, FixedCancelSet(), _TICK),
            new_operation_key(), owned)
        assert resend.value.disposition.value == "already", resend
        after = tuple(owned.connection.execute(
            "SELECT id, body_json FROM history_events ORDER BY id").fetchall())
        assert after[:len(boundary)] == boundary

    @pytest.mark.asyncio
    async def test_finished_cancel_does_not_wait_for_device(self, pipeline):
        """全部有限处理结束后取消动作结束，不等待设备或客户端。"""
        owned = pipeline
        _, summary, finished = await _run_cancel(
            owned, _GROUP_CANCEL, CancelTarget(plan_instance_id=1, group="g"))
        # 组入口：A 拒绝、B 未启动取消——本次处理全部结束。
        assert finished.action_status == 4
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM cancel_items WHERE action_id = ?"
            " AND status IN (1, 2)", (_GROUP_CANCEL,)).fetchone() == (0,)

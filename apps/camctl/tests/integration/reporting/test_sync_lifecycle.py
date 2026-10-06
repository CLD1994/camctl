"""R6 同步消费者生命周期的组件集成测试。

同步实际开始与动作进入运行同一事务（起点确定、固定开始边界、
起点不存在时的动作失败），计划首次开始与动作成功后的计划完成
也保存在同一事务内；本地报告满足与动作成功共同保存（含
ACK 先结束后的补记）、取消消费者覆盖未开始、运行与终态动作；
共享报告生成不受取消影响；重试、重启与同一请求重送沿用原记录。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.repositories.acceptance import (
    register_acceptance_guards,
)
from camctl.persistence.repositories.cancellation import (
    register_cancellation_guards,
)
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.reporting.policy import (
    cancel_sync,
    record_local_report,
    register_report_guards,
    register_sync_guard,
    start_sync,
)
from camctl.reporting.models import SyncMode

from ..persistence.test_runtime import _create_valid_database

register_report_guards()
register_sync_guard()
register_outputs_guards()
register_capture_guards()
register_acceptance_guards()
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
    connection.execute(
        "INSERT INTO plans (id, request_id, name, created_at, status,"
        " created_event_id, last_event_id, change_count)"
        " VALUES (1, 4242, 'seed', ?, 1, 1, 1, 1)", (_NOW,))
    for action_id, index, kind, status, canceled in (
            (40, 0, 7, 1, 0),   # 报告动作：待执行
            (41, 1, 7, 1, 0),   # 报告动作：待执行（增量起点用）
            (42, 2, 7, 6, 1),   # 报告动作：执行前已被取消
            (11, 3, 2, 1, 0)):  # 非报告动作
        capture = kind in (1, 2, 3)
        connection.execute(
            "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
            " scheduled_at, group_name, input_fields_json, effective_params_json,"
            " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
            " cancel_requested, error_code, error_details_json,"
            " first_window_observed_at, expiration_reason, source_resolution_state,"
            " resolved_source_plan_id, target_selection_state, created_event_id,"
            " last_event_id, change_count)"
            " VALUES (?, 1, ?, ?, ?, ?, ?, NULL, '{}', ?, ?, ?, '{}', ?, 0,"
            " ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, 1, 1)",
            (action_id, index, f"act-{action_id}", kind,
             "cam-1" if capture else None, _NOW,
             "{}" if capture else None,
             "camctl-adb" if capture else None,
             1000 if capture else None, status, canceled))
    # 已发布报告 1（to_wm=30）：增量同步的固定起点。
    connection.execute(
        "INSERT INTO reports (id, frozen_event_id, from_wm, to_wm, format_version,"
        " status, size_bytes, sha256, publication_count, last_published_event_id,"
        " last_error_json, created_event_id, last_event_id)"
        " VALUES (1, 0, 0, 30, 1, 4, 10, '"
        + "0" * 64 + "', 1, 1, NULL, 1, 1)")
    # 本地完成用的报告 2（已发布）。
    connection.execute(
        "INSERT INTO reports (id, frozen_event_id, from_wm, to_wm, format_version,"
        " status, size_bytes, sha256, publication_count, last_published_event_id,"
        " last_error_json, created_event_id, last_event_id)"
        " VALUES (2, 0, 0, 40, 1, 4, 10, '"
        + "1" * 64 + "', 1, 1, NULL, 1, 1)")
    connection.commit()
    yield owned
    owned.connection.close()


def _value(owned, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


def _apply_cancel_request(owned, action_id: int) -> None:
    """以已生效取消请求安排场景；真实链路由取消模块同事务写入。"""
    owned.connection.execute(
        "UPDATE actions SET cancel_requested = 1 WHERE id = ?", (action_id,))
    owned.connection.commit()


def _ack_end_sync(owned, action_id: int) -> None:
    """以合格 ACK 结束安排场景；真实链路由受理事务写入。"""
    row = _value(owned, "SELECT id FROM state_syncs WHERE action_id = ?",
                 action_id)
    owned.connection.execute(
        "UPDATE state_syncs SET status = 2, ack_report_id = 1, ended_event_id = 4"
        " WHERE id = ?", (row[0],))
    owned.connection.commit()


class TestStartSync:
    def test_full_sync_starts_within_action_start_transaction(self, pipeline):
        owned = pipeline
        outcome = start_sync(
            new_operation_key(), owned, action_id=40, mode=SyncMode.FULL,
            occurred_at=_NOW)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert outcome.value.disposition.value == "saved"
        row = _value(
            owned, "SELECT mode, after_report_id, from_wm,"
            " started_boundary_event_id, status FROM state_syncs"
            " WHERE action_id = 40")
        assert row[0] == 1 and row[1] is None and row[2] == 0
        assert row[3] == 4 and row[4] == 1  # 开始边界=本事务末位事件
        # 动作进入运行；计划首次开始；三个事件同属一个事务。
        assert _value(
            owned, "SELECT status, execution_started, cancel_requested"
            " FROM actions WHERE id = 40") == (2, 1, 0)
        assert _value(owned, "SELECT status FROM plans WHERE id = 1") == (2,)
        events = owned.connection.execute(
            "SELECT id, transaction_id, event_type FROM history_events"
            " WHERE id >= 2 ORDER BY id").fetchall()
        assert [(r[0], r[2]) for r in events] == [(2, 5), (3, 9), (4, 29)]
        assert len({r[1] for r in events}) == 1

    def test_start_is_idempotent_for_retry_and_resend(self, pipeline):
        owned = pipeline
        key = new_operation_key()
        first = start_sync(key, owned, action_id=40, mode=SyncMode.FULL,
                           occurred_at=_NOW)
        assert first.value.disposition.value == "saved"
        again = start_sync(key, owned, action_id=40, mode=SyncMode.FULL,
                           occurred_at=_NOW)
        assert again.value.disposition.value == "already"
        before = tuple(owned.connection.execute(
            "SELECT id FROM history_events ORDER BY id").fetchall())
        late = start_sync(new_operation_key(), owned, action_id=40,
                          mode=SyncMode.FULL, occurred_at=_NOW + 5)
        assert late.value.disposition.value == "already"
        assert tuple(owned.connection.execute(
            "SELECT id FROM history_events ORDER BY id").fetchall()) == before

    def test_incremental_start_uses_after_report_watermark(self, pipeline):
        owned = pipeline
        outcome = start_sync(
            new_operation_key(), owned, action_id=41,
            mode=SyncMode.INCREMENTAL, after_report_id=1, occurred_at=_NOW)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        row = _value(
            owned, "SELECT mode, after_report_id, from_wm"
            " FROM state_syncs WHERE action_id = 41")
        assert row == (2, 1, 30)

    def test_missing_anchor_fails_action_without_creating_sync(self, pipeline):
        owned = pipeline
        outcome = start_sync(
            new_operation_key(), owned, action_id=41,
            mode=SyncMode.INCREMENTAL, after_report_id=99, occurred_at=_NOW)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert outcome.value.disposition.value == "failed"
        assert outcome.value.sync_id is None
        # 动作先进入运行再保存执行失败；不建立同步责任。
        row = _value(
            owned, "SELECT status, execution_started, error_code,"
            " error_details_json FROM actions WHERE id = 41")
        assert row[0] == 4 and row[1] == 1 and row[2] == 26
        assert json.loads(row[3]) == {"after_report_id": "99"}
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM state_syncs WHERE action_id = 41"
        ).fetchone() == (0,)
        events = [(r[0], r[1]) for r in owned.connection.execute(
            "SELECT id, event_type FROM history_events WHERE id >= 2"
            " ORDER BY id").fetchall()]
        assert events == [(2, 5), (3, 9), (4, 8)]
        # 动作失败后计划仍处于执行中（其余动作尚未开始）。
        assert _value(owned, "SELECT status FROM plans WHERE id = 1") == (2,)
        # 恢复：失败不改写其他报告动作的正常开始。
        recovery = start_sync(
            new_operation_key(), owned, action_id=40, mode=SyncMode.FULL,
            occurred_at=_NOW + 5)
        assert recovery.kind is DbOutcomeKind.COMPLETED, recovery.error
        assert recovery.value.disposition.value == "saved"

    def test_non_report_action_is_rejected(self, pipeline):
        owned = pipeline
        outcome = start_sync(
            new_operation_key(), owned, action_id=11, mode=SyncMode.FULL,
            occurred_at=_NOW)
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        assert _value(owned, "SELECT status FROM actions WHERE id = 11") == (1,)

    def test_canceled_before_start_does_not_start(self, pipeline):
        owned = pipeline
        outcome = start_sync(
            new_operation_key(), owned, action_id=42, mode=SyncMode.FULL,
            occurred_at=_NOW)
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM state_syncs").fetchone() == (0,)
        assert _value(owned, "SELECT status FROM actions WHERE id = 42") == (6,)


class TestLocalReport:
    def test_local_report_saved_with_action_success(self, pipeline):
        owned = pipeline
        start_sync(new_operation_key(), owned, action_id=40,
                   mode=SyncMode.FULL, occurred_at=_NOW)
        outcome = record_local_report(
            new_operation_key(), owned, action_id=40, local_report_id=2,
            occurred_at=_NOW)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert _value(
            owned, "SELECT local_report_id, status FROM state_syncs"
            " WHERE action_id = 40") == (2, 1)
        assert _value(owned, "SELECT status FROM actions WHERE id = 40") == (3,)
        events = [(r[0], r[1]) for r in owned.connection.execute(
            "SELECT id, event_type FROM history_events WHERE id >= 5"
            " ORDER BY id").fetchall()]
        assert events == [(5, 29), (6, 8)]
        transactions = {r[0] for r in owned.connection.execute(
            "SELECT transaction_id FROM history_events WHERE id IN (5, 6)"
        ).fetchall()}
        assert len(transactions) == 1
        # 其余动作尚未终态：计划保持执行中，本事务无计划状态事件。
        assert _value(owned, "SELECT status FROM plans WHERE id = 1") == (2,)
        # 同一请求重送：本地完成与成功事实原样恢复。
        again = record_local_report(
            new_operation_key(), owned, action_id=40, local_report_id=2,
            occurred_at=_NOW)
        assert again.value.disposition.value == "already"

    def test_final_action_success_completes_plan(self, pipeline):
        owned = pipeline
        # 其余动作先到达终态；真实链路由各自的事务写入。
        # 报告动作执行前失败（spec 未确定）；拍摄动作执行前取消。
        owned.connection.execute(
            "UPDATE actions SET status = 4, error_code = 1,"
            " error_details_json = '{}', execution_spec_json = NULL"
            " WHERE id = 41")
        owned.connection.execute(
            "UPDATE actions SET status = 6, cancel_requested = 1"
            " WHERE id = 11")
        owned.connection.commit()
        start_sync(new_operation_key(), owned, action_id=40,
                   mode=SyncMode.FULL, occurred_at=_NOW)
        outcome = record_local_report(
            new_operation_key(), owned, action_id=40, local_report_id=2,
            occurred_at=_NOW)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert _value(owned, "SELECT status FROM actions WHERE id = 40") == (3,)
        # 最后一个动作成功：计划完成与动作成功同一事务保存。
        assert _value(owned, "SELECT status FROM plans WHERE id = 1") == (3,)
        events = [(r[0], r[1]) for r in owned.connection.execute(
            "SELECT id, event_type FROM history_events WHERE id >= 5"
            " ORDER BY id").fetchall()]
        assert events == [(5, 29), (6, 8), (7, 9)]
        transactions = {r[0] for r in owned.connection.execute(
            "SELECT transaction_id FROM history_events WHERE id IN (5, 6, 7)"
        ).fetchall()}
        assert len(transactions) == 1

    def test_local_report_after_ack_end(self, pipeline):
        owned = pipeline
        start_sync(new_operation_key(), owned, action_id=40,
                   mode=SyncMode.FULL, occurred_at=_NOW)
        _ack_end_sync(owned, 40)
        outcome = record_local_report(
            new_operation_key(), owned, action_id=40, local_report_id=2,
            occurred_at=_NOW)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert _value(
            owned, "SELECT local_report_id, status FROM state_syncs"
            " WHERE action_id = 40") == (2, 2)
        assert _value(owned, "SELECT status FROM actions WHERE id = 40") == (3,)

    def test_conflicting_local_report_is_rejected(self, pipeline):
        owned = pipeline
        start_sync(new_operation_key(), owned, action_id=40,
                   mode=SyncMode.FULL, occurred_at=_NOW)
        record_local_report(
            new_operation_key(), owned, action_id=40, local_report_id=2,
            occurred_at=_NOW)
        conflict = record_local_report(
            new_operation_key(), owned, action_id=40, local_report_id=1,
            occurred_at=_NOW)
        assert conflict.kind is DbOutcomeKind.ROLLED_BACK
        assert _value(
            owned, "SELECT local_report_id FROM state_syncs"
            " WHERE action_id = 40") == (2,)


class TestCancelSync:
    def test_cancel_ends_running_action_sync_only(self, pipeline):
        owned = pipeline
        start_sync(new_operation_key(), owned, action_id=41,
                   mode=SyncMode.INCREMENTAL, after_report_id=1,
                   occurred_at=_NOW)
        start_sync(new_operation_key(), owned, action_id=40,
                   mode=SyncMode.FULL, occurred_at=_NOW)
        _apply_cancel_request(owned, 40)
        outcome = cancel_sync(
            new_operation_key(), owned, action_id=40, occurred_at=_NOW)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        row = _value(
            owned, "SELECT status, ended_event_id FROM state_syncs"
            " WHERE action_id = 40")
        assert row[0] == 3
        assert row[1] == 7  # 结束依据是取消事件自身
        # 其他同步继续；共享生成（reports）不受取消影响。
        assert _value(
            owned, "SELECT status FROM state_syncs WHERE action_id = 41"
        ) == (1,)
        assert _value(owned, "SELECT COUNT(*) FROM reports") == (2,)

    def test_cancel_requires_applied_cancel_request(self, pipeline):
        owned = pipeline
        start_sync(new_operation_key(), owned, action_id=40,
                   mode=SyncMode.FULL, occurred_at=_NOW)
        outcome = cancel_sync(
            new_operation_key(), owned, action_id=40, occurred_at=_NOW)
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        assert _value(
            owned, "SELECT status FROM state_syncs WHERE action_id = 40"
        ) == (1,)

    def test_succeeded_action_preserves_sync_for_ack(self, pipeline):
        owned = pipeline
        start_sync(new_operation_key(), owned, action_id=40,
                   mode=SyncMode.FULL, occurred_at=_NOW)
        record_local_report(
            new_operation_key(), owned, action_id=40, local_report_id=2,
            occurred_at=_NOW)
        outcome = cancel_sync(
            new_operation_key(), owned, action_id=40, occurred_at=_NOW)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert outcome.value.disposition.value == "kept"
        # 成功后的取消不结束责任：继续等待合格 ACK。
        assert _value(
            owned, "SELECT status, local_report_id FROM state_syncs"
            " WHERE action_id = 40") == (1, 2)
        assert _value(owned, "SELECT status FROM actions WHERE id = 40") == (3,)

    def test_unstarted_action_has_nothing_to_end(self, pipeline):
        owned = pipeline
        outcome = cancel_sync(
            new_operation_key(), owned, action_id=42, occurred_at=_NOW)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert outcome.value.disposition.value == "already"
        assert outcome.value.sync_id is None
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM state_syncs").fetchone() == (0,)

    def test_ended_sync_stays_ended(self, pipeline):
        owned = pipeline
        start_sync(new_operation_key(), owned, action_id=40,
                   mode=SyncMode.FULL, occurred_at=_NOW)
        _apply_cancel_request(owned, 40)
        first = cancel_sync(
            new_operation_key(), owned, action_id=40, occurred_at=_NOW)
        assert first.value.disposition.value == "saved"
        again = cancel_sync(
            new_operation_key(), owned, action_id=40, occurred_at=_NOW)
        assert again.value.disposition.value == "already"

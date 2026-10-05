"""S1 生产查询的真实责任维度组合测试。

空库、受理、报告冻结／发布／失败、同步本地完成、ACK、未收尾操
作流程、可延后清理残留与终态动作残留设备事实逐阶段核对
query_work_facts 的每个维度与分类结果；报告失败等待新触发与出
现新变化的两分区以真实报告状态区分。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from camctl.acceptance.input import parse_input, read_input
from camctl.acceptance.service import AcceptanceContext, CommandMode, accept_input
from camctl.bootstrap.application import query_work_facts
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import (
    AcceptanceRepository,
    register_acceptance_guards,
)
from camctl.persistence.repositories.cancellation import (
    register_cancellation_guards,
)
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.host_files.handoff import PublishResult, PublishStage
from camctl.host_files.io import DirectorySyncStage
from camctl.reporting.models import ReportBytes, SyncMode
from camctl.reporting.policy import (
    publish_report,
    record_local_report,
    record_report_bytes,
    record_report_failure,
    record_report_publish_intent,
    register_report_guards,
    register_sync_guard,
    start_sync,
)

from ..acceptance.test_acceptance import Catalog
from ..acceptance.test_atomicity import _process
from ..persistence.test_runtime import _create_valid_database

register_acceptance_guards()
register_report_guards()
register_sync_guard()
register_outputs_guards()
register_capture_guards()
register_cancellation_guards()

_NOW = 1_750_000_000_000_000


def _plan_body(request_id: str) -> dict:
    return {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": f"plan-{request_id}",
        "actions": [{
            "name": "sync", "type": "report_status",
            "scheduled_at": "2026-01-15 09:00:00",
            "params": {"scope": "full"},
        }],
    }


class _Reader:
    def read(self, path):
        return Path(path).read_bytes()


def _submit(owned, tmp_path: Path, request_id: str) -> int:
    target = tmp_path / f"plan-{request_id}.json"
    target.write_text(json.dumps(_plan_body(request_id)), encoding="utf-8")

    async def scenario() -> int:
        result = await accept_input(
            parse_input(await read_input(str(target), _Reader())),
            AcceptanceContext(
                mode=CommandMode.RUN, catalog=Catalog(),
                repository=AcceptanceRepository(),
                clock=type("C", (), {"utc_micros": staticmethod(lambda: _NOW)})(),
            ),
            new_operation_key(), owned,
        )
        assert result.plan_id is not None
        return result.plan_id

    return asyncio.run(scenario())


def _freeze(owned):
    from camctl.reporting.policy import ReportingRepository

    outcome = ReportingRepository().freeze_report(
        new_operation_key(), owned, occurred_at=_NOW)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    return outcome.value.report


def _publish(owned, report_id: int, payload: bytes) -> None:
    saved = record_report_bytes(
        new_operation_key(), owned, report_id,
        ReportBytes(len(payload), hashlib.sha256(payload).hexdigest()))
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    intent = record_report_publish_intent(new_operation_key(), owned, report_id)
    assert intent.kind is DbOutcomeKind.COMPLETED, intent.error
    published = publish_report(new_operation_key(), owned, report_id,
                               PublishResult(PublishStage.MOVED,
                                             DirectorySyncStage.SYNCED, True, None))
    assert published.kind is DbOutcomeKind.COMPLETED, published.error


@pytest.fixture
def owned(tmp_path: Path):
    _create_valid_database(tmp_path / "state.db")
    handle = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
    yield handle
    handle.connection.close()


class TestReportDimensions:
    def test_empty_database_has_no_work(self, owned):
        facts = query_work_facts(owned.connection)
        assert facts.unfinished_actions == 0
        assert facts.required_settlements == 0
        assert facts.pending_report_changes is False
        assert facts.report_failed_no_new_changes is False
        assert facts.waiting_acknowledgement is False
        assert facts.deferred_work_cleanup is False
        assert facts.residual_device_facts is False

    def test_future_pending_action_and_unreported_changes_need_driver(self, owned, tmp_path):
        _submit(owned, tmp_path, "1")
        facts = query_work_facts(owned.connection)
        # 未来 scheduled_at 的 pending 动作仍是工作；受理变化尚未报告。
        assert facts.unfinished_actions == 1
        assert facts.pending_report_changes is True

    def test_rejected_input_diagnostic_is_report_responsibility(self, owned, tmp_path):
        target = tmp_path / "broken.json"
        target.write_text(json.dumps({
            "request_id": "7", "created_at": "2026-01-15 08:00:00",
            "actions": [{"name": "bad", "type": "camera_record",
                         "device_id": "missing-device"}],
        }), encoding="utf-8")

        async def scenario():
            await accept_input(
                parse_input(await read_input(str(target), _Reader())),
                AcceptanceContext(
                    mode=CommandMode.RUN, catalog=Catalog(),
                    repository=AcceptanceRepository(),
                    clock=type("C", (), {"utc_micros": staticmethod(lambda: _NOW)})(),
                ),
                new_operation_key(), owned)

        asyncio.run(scenario())
        facts = query_work_facts(owned.connection)
        # 受理拒绝是应报告的业务结果：无动作但报告责任存在。
        assert facts.unfinished_actions == 0
        assert facts.pending_report_changes is True

    def test_registered_report_is_in_flight_local_responsibility(self, owned, tmp_path):
        _submit(owned, tmp_path, "1")
        report = _freeze(owned)
        facts = query_work_facts(owned.connection)
        # 冻结后生成前：本地报告职责未完成，仍是待处理变化。
        assert facts.pending_report_changes is True
        assert facts.report_failed_no_new_changes is False

        _publish(owned, report.report_id, b'{"report_id":"1"}\n')
        facts = query_work_facts(owned.connection)
        # 已发布覆盖全部变化：本地职责完成，仅等待 ACK。
        assert facts.pending_report_changes is False
        assert facts.waiting_acknowledgement is True

    def test_failed_attempt_without_new_changes_waits_for_trigger(self, owned, tmp_path):
        _submit(owned, tmp_path, "1")
        report = _freeze(owned)
        _publish(owned, report.report_id, b'{"report_id":"1"}\n')
        # 已发布边界之后的新变化触发下一轮报告。
        _submit(owned, tmp_path, "2")
        second = _freeze(owned)
        failure = record_report_failure(
            new_operation_key(), owned, second.report_id,
            {"code": "write_failed", "stage": "write"})
        assert failure.kind is DbOutcomeKind.COMPLETED, failure.error
        facts = query_work_facts(owned.connection)
        # 最新尝试失败且无新变化：责任保留，等待新触发，不构成新变化。
        assert facts.pending_report_changes is False
        assert facts.report_failed_no_new_changes is True

        _submit(owned, tmp_path, "3")
        facts = query_work_facts(owned.connection)
        # 新变化出现后重新成为待处理工作。
        assert facts.pending_report_changes is True

    def test_published_after_failure_recovers_responsibility(self, owned, tmp_path):
        _submit(owned, tmp_path, "1")
        first = _freeze(owned)
        failure = record_report_failure(
            new_operation_key(), owned, first.report_id,
            {"code": "write_failed", "stage": "write"})
        assert failure.kind is DbOutcomeKind.COMPLETED, failure.error
        _publish(owned, first.report_id, b'{"report_id":"1"}\n')
        facts = query_work_facts(owned.connection)
        # 同一报告后来发布成功：失败不再构成等待新触发的唯一残留。
        assert facts.pending_report_changes is False
        assert facts.report_failed_no_new_changes is False
        assert facts.waiting_acknowledgement is True

    def test_ack_advances_watermark_and_clears_waiting(self, owned, tmp_path):
        _submit(owned, tmp_path, "1")
        report = _freeze(owned)
        _publish(owned, report.report_id, b'{"report_id":"1"}\n')
        outcome = _process({"request_id": "1", "last_report_id": str(report.report_id)},
                           owned.connection)
        assert outcome.kind is DbOutcomeKind.COMPLETED
        facts = query_work_facts(owned.connection)
        # 动作仍未执行，但报告侧仅剩已确认。
        assert facts.unfinished_actions == 1
        assert facts.pending_report_changes is False
        assert facts.waiting_acknowledgement is False

    def test_running_report_action_is_not_ordinary_unfinished(self, owned, tmp_path):
        _submit(owned, tmp_path, "1")
        started = start_sync(
            new_operation_key(), owned, action_id=1, mode=SyncMode.FULL,
            occurred_at=_NOW)
        assert started.kind is DbOutcomeKind.COMPLETED, started.error
        facts = query_work_facts(owned.connection)
        # 运行中的报告动作由报告维度推进：等待本地报告处理的同步
        # 不作为普通未完成动作无限延长会话；其开始构成待报告变化。
        assert facts.unfinished_actions == 0
        assert facts.pending_report_changes is True

    def test_published_covering_report_with_unsaved_local_is_pending(self, owned, tmp_path):
        _submit(owned, tmp_path, "1")
        started = start_sync(
            new_operation_key(), owned, action_id=1, mode=SyncMode.FULL,
            occurred_at=_NOW)
        assert started.kind is DbOutcomeKind.COMPLETED, started.error
        report = _freeze(owned)
        _publish(owned, report.report_id, b'{"report_id":"1"}\n')
        facts = query_work_facts(owned.connection)
        # 覆盖同步开始的报告已发布但本地完成尚未保存：本地报告职
        # 责仍开放，不能按已完成退出。
        assert facts.pending_report_changes is True
        settled = record_local_report(
            new_operation_key(), owned, action_id=1,
            local_report_id=report.report_id, occurred_at=_NOW)
        assert settled.kind is DbOutcomeKind.COMPLETED, settled.error
        facts = query_work_facts(owned.connection)
        # 本地保存与动作成功本身是新的待报告变化：下一份报告覆盖
        # 后本地职责才全部完成。
        assert facts.pending_report_changes is True
        followup = _freeze(owned)
        _publish(owned, followup.report_id, b'{"report_id":"2"}\n')
        facts = query_work_facts(owned.connection)
        assert facts.pending_report_changes is False
        assert facts.unfinished_actions == 0


class TestSettlementDimensions:
    def test_unfinished_operation_flow_counts_as_settlement(self, owned, tmp_path):
        _submit(owned, tmp_path, "1")
        connection = owned.connection
        connection.execute(
            "INSERT INTO operation_runs (id, action_id, delivery_id, kind,"
            " query_purpose, responsibility_key, activity_id, copy_id,"
            " cleanup_item_id, session_key, status, attempts_used,"
            " max_attempts_used, timeout_s_json, retry_interval_s_json,"
            " retry_wait_required, error_json)"
            " VALUES (31, (SELECT id FROM actions LIMIT 1), NULL, 6, 1,"
            " 'query/preflight/1', NULL, NULL, NULL, NULL, 1, 0, 1,"
            " '30', '10', 0, NULL)")
        connection.commit()
        facts = query_work_facts(connection)
        assert facts.required_settlements == 1

    def test_deferred_intermediate_cleanup_is_not_work(self, owned, tmp_path):
        _submit(owned, tmp_path, "1")
        connection = owned.connection
        connection.execute(
            "INSERT INTO intermediate_files (id, owner_action_id,"
            " owner_delivery_id, purpose, relative_path, retention_state,"
            " cleanup_state, size_bytes, sha256, last_error_json,"
            " created_event_id, last_event_id, change_count)"
            " VALUES (51, (SELECT id FROM actions LIMIT 1), NULL, 2,"
            " 'copies/51.part', 2, 2, NULL, NULL, NULL,"
            " (SELECT MAX(id) FROM history_events),"
            " (SELECT MAX(id) FROM history_events), 1)")
        connection.commit()
        facts = query_work_facts(connection)
        # 可延后清理残留：不是待处理工作，但如实呈现。
        assert facts.deferred_work_cleanup is True
        assert facts.required_settlements == 0

    def test_residual_device_fact_of_terminal_action_is_not_work(self, owned, tmp_path):
        plan_id = _submit(owned, tmp_path, "1")
        connection = owned.connection
        connection.execute(
            "INSERT INTO device_activities (id, action_id, task_key,"
            " task_locator_json, state_query_supported, stop_supported,"
            " safe_repeat_stop, start_return_meaning, completion_mode,"
            " ownership_mode, output_scope_json, baseline_state,"
            " baseline_first_event_id, baseline_last_event_id, dispatch_state,"
            " activity_state, occupancy_state, sent_at, started_at,"
            " result_wait_margin_ms, extra_wait_ms_used, expected_check_at,"
            " wait_completed_event_id, capture_json, control_elapsed_ns,"
            " completion_basis, completion_evidence_json, result_set_state,"
            " result_check_json, last_error_json)"
            " VALUES (61, (SELECT id FROM actions LIMIT 1), ? , NULL, 0, 0, 0,"
            " 1, 1, 1, '{}', 1, NULL, NULL, 2, 2, 1, NULL, NULL, NULL, NULL,"
            " NULL, NULL, NULL, NULL, NULL, NULL, 1, NULL, NULL)",
            ("a" * 32,))
        connection.commit()
        # 动作仍运行：先确认未完成数量，再置终态观察残留维度。
        assert query_work_facts(connection).unfinished_actions == 1
        connection.execute(
            "UPDATE actions SET status = 4, execution_started = 1,"
            " error_code = 20, error_details_json = '{}'"
            " WHERE id = (SELECT id FROM actions LIMIT 1)")
        connection.commit()
        facts = query_work_facts(connection)
        assert facts.residual_device_facts is True
        assert facts.unfinished_actions == 0
        assert plan_id is not None


class TestPendingDeadline:
    """会话等待截止按待执行动作的下一生命周期时刻计算。"""

    @staticmethod
    def _context(at: int):
        return type(
            "Context", (),
            {"clock": type(
                "Clock", (),
                {"utc_micros": staticmethod(lambda: at)})()})()

    @staticmethod
    def _submit_capture(owned, tmp_path: Path, request_id: str) -> None:
        body = {
            "request_id": request_id,
            "created_at": "2026-01-15 08:00:00",
            "name": f"plan-{request_id}",
            "actions": [{
                "name": "shoot", "type": "camera_take_photo",
                "device_id": "cam-1",
                "scheduled_at": "2026-01-15 09:00:00",
                "params": {"type": "single_shot"},
                "policy": {"max_delay_ms": 1000},
            }],
        }
        target = tmp_path / f"capture-{request_id}.json"
        target.write_text(json.dumps(body), encoding="utf-8")

        async def scenario() -> None:
            await accept_input(
                parse_input(await read_input(str(target), _Reader())),
                AcceptanceContext(
                    mode=CommandMode.RUN, catalog=Catalog(),
                    repository=AcceptanceRepository(),
                    clock=type(
                        "C", (), {"utc_micros": staticmethod(lambda: _NOW)})(),
                ),
                new_operation_key(), owned,
            )

        asyncio.run(scenario())

    def test_capture_deadline_wakes_at_schedule_then_window_end(
        self, owned, tmp_path,
    ):
        from camctl.contracts.values import to_utc_micros
        from camctl.session.service import _pending_deadline_seconds

        self._submit_capture(owned, tmp_path, "50")
        scheduled = to_utc_micros("2026-01-15 09:00:00")
        window_end = scheduled + 1_000 * 1000
        # 计划时间之前：等待到计划时间。
        assert _pending_deadline_seconds(
            self._context(scheduled - 5_000_000), owned) == 5.0
        # 窗口之内：等待到窗口结束（过期判定时刻），不再按已过的
        # 计划时间立即唤醒。
        assert _pending_deadline_seconds(
            self._context(scheduled), owned) == 1.0
        assert _pending_deadline_seconds(
            self._context(window_end - 500_000), owned) == 0.5
        # 窗口结束之后：立即唤醒去过期。
        assert _pending_deadline_seconds(
            self._context(window_end + 1), owned) == 0.0

    def test_no_pending_actions_has_no_deadline(self, owned):
        from camctl.session.service import _pending_deadline_seconds

        assert _pending_deadline_seconds(self._context(_NOW), owned) is None

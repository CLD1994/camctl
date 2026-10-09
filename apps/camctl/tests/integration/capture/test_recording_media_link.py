"""录像尾段媒体链接线的组件集成测试。

真实停止确认后的录像处理器组合真实处理事务与媒体链编排：控制完
成固定无需检查决定后按控制依据登记成功；计时证据不足已固定需要
检查的处理行经媒体端口推进拷贝、检查与修复，判定装载已保存的检
查时长与修复成品，原片与修复成品同事务登记；媒体端口未装配或处
理未终局时保持等待，不提前判成功。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.capture.handlers import CaptureRuntime, capture_handler
from camctl.capture.media import MediaPolicy
from camctl.capture.media_flow import DriverReadSessions, MediaFlow
from camctl.capture.processing import (
    CheckBasis,
    CheckDecisionChoice,
    CheckDecisionSave,
    CheckReason,
)
from camctl.contracts.values import new_operation_key
from camctl.contracts.workflow_errors import registered_error
from camctl.devices.read_session import SourceFile
from camctl.host_files.models import BoundDirectories
from camctl.host_files.tasks import FileTaskId, FileTaskResult
from camctl.host_files.media import MediaProbe
from camctl.outputs.copy import SourceDigest, SourceDigestReader
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import (
    CaptureRepository,
    register_capture_guards,
)
from camctl.persistence.repositories.operations import (
    OperationRepository,
    register_operation_guards,
)
from camctl.persistence.repositories.scheduling import SchedulingRepository
from camctl.persistence.repositories.timelapse import (
    TimelapseRepository,
    register_timelapse_guards,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.scheduling.rules import LaunchWindow

from ..persistence.test_runtime import _create_valid_database
from .test_capture_contract import DriverDouble, ResultsDouble, _PAGE_EVIDENCE, _entry
from .test_media_flow import ReadDriverDouble, _Digest, _Stream

register_operation_guards()
register_capture_guards()
register_timelapse_guards()

pytestmark = pytest.mark.asyncio

_NOW = 1_750_000_000_000_000
_CONTENT = b"0123456789"


class _RepairTools:
    """工具替身：probe 返回固定时长，repair 返回完整成品。"""

    def __init__(self, duration_s: str) -> None:
        self.duration_s = duration_s
        self.calls: list[str] = []

    async def probe(self, input) -> FileTaskResult:
        self.calls.append("probe")
        return FileTaskResult(
            task_id=FileTaskId("probe"), ran=True,
            value=MediaProbe(duration_s=Decimal(self.duration_s), error=None))

    async def repair(self, input, output, *, trim_s) -> FileTaskResult:
        self.calls.append("repair")
        return FileTaskResult(
            task_id=FileTaskId("repair"), ran=True,
            value=SimpleNamespace(
                complete=True, size_bytes=len(_CONTENT), digest="a" * 64))


def _media(owned, tools, staging: Path) -> MediaFlow:
    return MediaFlow(
        owned=owned,
        roots=BoundDirectories(staging=staging),
        sessions=DriverReadSessions(owned, ReadDriverDouble(_CONTENT), ticket=None),
        tools=tools,
        policy=MediaPolicy(repair_margin_s=Decimal("2")),
        occurred_at=lambda: _NOW + 1_000_000,
        digest_supported=True,
        digest=_Digest(_CONTENT),
    )


def _runtime(owned, files, media=None) -> CaptureRuntime:
    runtime = CaptureRuntime(
        owned=owned,
        scheduling=SchedulingRepository(),
        operations=OperationRepository(),
        capture=CaptureRepository(),
        timelapse=TimelapseRepository(),
        driver=DriverDouble(),
        results=ResultsDouble(files),
        evidence=_PAGE_EVIDENCE,
        wall_us=lambda: _NOW,
        monotonic_ns=lambda: 5_000_000_000,
        window_of=lambda action: LaunchWindow(
            scheduled_at=action["scheduled_at"],
            window_end=action["scheduled_at"] + action["max_delay_ms"] * 1000),
        wait_config=lambda params: None,
    )
    runtime.media = media
    return runtime


def _environment(tmp_path: Path, *, check_decision: int,
                 check_basis: str | None) -> tuple:
    """启动与停止都已可靠确认的录像：处理行决定按变体固定。"""
    target = tmp_path / "state.db"
    _create_valid_database(target)
    owned = open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "INSERT INTO history_transactions (id, operation_key, first_event_id, last_event_id)"
        " VALUES (1, ?, 1, 1)",
        ("f" * 32,),
    )
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
        " VALUES (1, 1, 0, 'rec', 2, 'cam-1', ?, NULL, '{}', '{}',"
        " 'camctl-adb', 1000, '{\"target_duration_ms\": 60000}', 2, 1, 0, NULL,"
        " NULL, NULL, NULL, NULL, NULL, NULL, 1, 1, 1)", (_NOW,))
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
        " VALUES (1, 1, ?, NULL, 1, 1, 1, 2, 1, 1, '{}', 1, NULL, NULL, 3, 2, 1,"
        " ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, NULL)",
        (f"{1:032x}", _NOW, _NOW))
    connection.execute(
        "INSERT INTO operation_runs (id, action_id, delivery_id, kind, query_purpose,"
        " responsibility_key, activity_id, copy_id, cleanup_item_id, session_key,"
        " status, attempts_used, max_attempts_used, timeout_s_json,"
        " retry_interval_s_json, retry_wait_required, error_json)"
        " VALUES (20, 1, NULL, 1, NULL, 'start/1', 1, NULL, NULL, NULL, 3, 1, 3,"
        " '10', '1', 0, NULL)")
    connection.execute(
        "INSERT INTO operation_attempts (id, run_id, attempt_no, status,"
        " intent_event_id, result_event_id, max_attempts_used, effect_state,"
        " result_json) VALUES (21, 20, 1, 2, 1, 1, 3, 3, '{}')")
    connection.execute(
        "INSERT INTO operation_runs (id, action_id, delivery_id, kind, query_purpose,"
        " responsibility_key, activity_id, copy_id, cleanup_item_id, session_key,"
        " status, attempts_used, max_attempts_used, timeout_s_json,"
        " retry_interval_s_json, retry_wait_required, error_json)"
        " VALUES (30, 1, NULL, 2, NULL, 'stop/1', 1, NULL, NULL, NULL, 3, 1, 3,"
        " '10', '1', 0, NULL)")
    connection.execute(
        "INSERT INTO operation_attempts (id, run_id, attempt_no, status,"
        " intent_event_id, result_event_id, max_attempts_used, effect_state,"
        " result_json) VALUES (31, 30, 1, 2, 1, 1, 3, 3, '{}')")
    connection.execute(
        "INSERT INTO recording_processing (id, action_id, source_device_file_id,"
        " check_state, check_decision, check_basis_json, media_json, repair_state,"
        " repair_basis_json, repair_output_file_id, repair_error_json, discard_state,"
        " discard_error_json)"
        " VALUES (41, 1, NULL, 1, ?, ?, '{}', 1, NULL, NULL, NULL, 1, NULL)",
        (check_decision, check_basis))
    connection.commit()
    staging = tmp_path / "staging"
    for name in ("recording-inputs", "derived"):
        (staging / name).mkdir(parents=True)
    return owned, staging


def _value(owned, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


_INSUFFICIENT = '{"reason": 2, "target_duration_ms": 60000}'


class TestControlCompleteDecision:
    async def test_control_complete_saves_not_needed_and_succeeds(
            self, tmp_path: Path) -> None:
        owned, staging = _environment(tmp_path, check_decision=1, check_basis=None)
        try:
            runtime = _runtime(owned, {1: (_entry("clip-1", size=10),)})
            await capture_handler("camera_record")(1, runtime)
            # 控制完成即成功依据：终态与产物照常，处理决定固定为无需检查。
            assert _value(owned, "SELECT status FROM actions WHERE id = 1")[0] == 3
            assert _value(
                owned, "SELECT COUNT(*) FROM outputs WHERE source_action_id = 1"
                ) == (1,)
            decision = _value(
                owned,
                "SELECT check_decision, json_extract(check_basis_json, '$.reason'),"
                " json_extract(check_basis_json, '$.target_duration_ms')"
                " FROM recording_processing WHERE id = 41")
            assert decision == (2, 1, 60000)
        finally:
            owned.connection.close()


class TestRequiredMediaChain:
    async def test_check_runs_and_repair_registers_repaired_output(
            self, tmp_path: Path) -> None:
        owned, staging = _environment(
            tmp_path, check_decision=3, check_basis=_INSUFFICIENT)
        try:
            tools = _RepairTools("200")
            runtime = _runtime(
                owned, {1: (_entry("clip-1", size=10),)},
                media=_media(owned, tools, staging))
            await capture_handler("camera_record")(1, runtime)
            # 计时证据不足：拷贝、检查与修复一次推进到终局；多录超门槛
            # 的修复成品与原片同事务登记为正式产物。
            assert tools.calls == ["probe", "repair"]
            # 列举确认在场，读取绑定首次声明源端摘要能力。
            assert _value(
                owned, "SELECT presence_state, checksum_support"
                " FROM device_files WHERE id = 1") == (2, 2)
            assert _value(owned, "SELECT status FROM actions WHERE id = 1")[0] == 3
            processing = _value(
                owned,
                "SELECT check_state, repair_state, repair_output_file_id"
                " FROM recording_processing WHERE id = 41")
            assert processing[0] == 3
            assert processing[1] == 5
            assert processing[2] is not None
            kinds = owned.connection.execute(
                "SELECT kind FROM outputs WHERE source_action_id = 1"
                " ORDER BY kind").fetchall()
            assert kinds == [(1,), (2,)]
            assert _value(
                owned,
                "SELECT retention_state FROM intermediate_files WHERE id = ?",
                processing[2]) == (3,)
        finally:
            owned.connection.close()

    async def test_short_duration_fails_and_keeps_original(
            self, tmp_path: Path) -> None:
        owned, staging = _environment(
            tmp_path, check_decision=3, check_basis=_INSUFFICIENT)
        try:
            tools = _RepairTools("55")
            runtime = _runtime(
                owned, {1: (_entry("clip-1", size=10),)},
                media=_media(owned, tools, staging))
            await capture_handler("camera_record")(1, runtime)
            # 检查时长不足目标：按 recording_too_short 失败，原片保留。
            assert tools.calls == ["probe"]
            failure = _value(
                owned, "SELECT status, error_code FROM actions WHERE id = 1")
            assert failure == (4, registered_error("recording_too_short")
                               ["action_error_id"])
            assert _value(
                owned, "SELECT COUNT(*) FROM outputs WHERE source_action_id = 1"
                ) == (1,)
            assert _value(
                owned, "SELECT COUNT(*) FROM outputs WHERE kind = 2") == (0,)
        finally:
            owned.connection.close()

    async def test_missing_media_port_keeps_action_running(
            self, tmp_path: Path) -> None:
        owned, staging = _environment(
            tmp_path, check_decision=3, check_basis=_INSUFFICIENT)
        try:
            runtime = _runtime(owned, {1: (_entry("clip-1", size=10),)})
            await capture_handler("camera_record")(1, runtime)
            # 媒体端口未装配：检查不能执行也不猜测结果，保持执行中等待。
            assert _value(owned, "SELECT status FROM actions WHERE id = 1")[0] == 2
            assert _value(owned, "SELECT COUNT(*) FROM outputs") == (0,)
            assert _value(
                owned, "SELECT check_state FROM recording_processing"
                " WHERE id = 41") == (1,)
        finally:
            owned.connection.close()

    async def test_missing_source_fails_source_unavailable(
            self, tmp_path: Path) -> None:
        owned, staging = _environment(
            tmp_path, check_decision=3, check_basis=_INSUFFICIENT)
        try:
            tools = _RepairTools("61")
            runtime = _runtime(
                owned, {}, media=_media(owned, tools, staging))
            await capture_handler("camera_record")(1, runtime)
            # 需要检查但没有可归属的原片：无可用输入，按有限失败收场。
            failure = _value(
                owned,
                "SELECT status, error_code,"
                " json_extract(error_details_json, '$.reason')"
                " FROM actions WHERE id = 1")
            assert failure == (4, registered_error("recording_processing_failed")
                               ["action_error_id"], "source_unavailable")
            assert tools.calls == []
        finally:
            owned.connection.close()

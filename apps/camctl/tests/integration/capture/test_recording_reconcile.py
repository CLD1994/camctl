"""跨会话录像对账的组件集成测试。

启动已在既往会话可靠确认（锚点随会话失效）的录像组合真实停止责
任与媒体链：恢复会话以已保存的启动墙钟与当前可信墙钟对账，目标
时长已满足即停止并按恢复控制完成登记成功；尚未满足保持运行等待
剩余时长，不提前停止；恢复停止确认的控制耗时超过门槛时保存异常
多录修复决定，媒体链不经检查直接修复，修复成品与原片同事务登记。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.capture.handlers import CaptureRuntime, capture_handler
from camctl.capture.media import MediaPolicy
from camctl.capture.media_flow import DriverReadSessions, MediaFlow
from camctl.contracts.values import new_operation_key
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult
from camctl.devices.evidence import DeviceObservation
from camctl.host_files.models import BoundDirectories
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
from .test_capture_contract import DriverDouble, ResultsDouble, _entry
from .test_media_flow import ReadDriverDouble, _Digest
from .test_recording_media_link import _RepairTools, _CONTENT

register_operation_guards()
register_capture_guards()
register_timelapse_guards()

pytestmark = pytest.mark.asyncio

_NOW = 1_750_000_000_000_000

#: 共享契约替身补停止链证据后的登记。
_EVIDENCE = EvidenceRegistry(
    (
        EvidenceContract(type="operation_returned", version=1, operation="control",
                         fields=frozenset()),
        EvidenceContract(type="start_confirmed", version=1, operation="control",
                         fields=frozenset({"activity_id"}), identity_field="activity_id"),
        EvidenceContract(type="results_returned", version=1, operation="result",
                         fields=frozenset()),
        EvidenceContract(type="stop_returned", version=1, operation="stop",
                         fields=frozenset()),
        EvidenceContract(type="stop_confirmed", version=1, operation="stop",
                         fields=frozenset({"activity_id"}),
                         identity_field="activity_id"),
    )
)


class _StopDouble:
    """停止端口替身：可靠确认停止。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def stop(self, request) -> DeviceCallResult:
        self.calls.append(request.operation)
        return DeviceCallResult(
            observations=(
                DeviceObservation(
                    type="stop_confirmed", version=1,
                    data={"activity_id": "1"}),
            ),
            error=None,
        )


def _runtime(owned, files, *, stopper, media=None,
             wall_us=lambda: _NOW) -> CaptureRuntime:
    runtime = CaptureRuntime(
        owned=owned,
        scheduling=SchedulingRepository(),
        operations=OperationRepository(),
        capture=CaptureRepository(),
        timelapse=TimelapseRepository(),
        driver=DriverDouble(),
        results=ResultsDouble(files),
        evidence=_EVIDENCE,
        wall_us=wall_us,
        monotonic_ns=lambda: 5_000_000_000,
        window_of=lambda action: LaunchWindow(
            scheduled_at=action["scheduled_at"],
            window_end=action["scheduled_at"] + action["max_delay_ms"] * 1000),
        wait_config=lambda params: None,
        stopper=stopper,
    )
    runtime.media = media
    return runtime


def _media(owned, tools, staging: Path) -> MediaFlow:
    return MediaFlow(
        owned=owned,
        roots=BoundDirectories(staging=staging),
        sessions=DriverReadSessions(owned, ReadDriverDouble(_CONTENT), ticket=None),
        tools=tools,
        policy=MediaPolicy(repair_margin_s=Decimal("10")),
        occurred_at=lambda: _NOW + 1_000_000,
        digest_supported=True,
        digest=_Digest(_CONTENT),
    )


def _environment(tmp_path: Path, *, started_at_offset_s: int) -> tuple:
    """既往会话已确认启动的录像：无本会话锚点，未停止。

    started_at_offset_s 是启动确认墙钟相对当前可信墙钟的回退秒数；
    处理行保持未判定，等待恢复对账后的决定建立。
    """
    target = tmp_path / "state.db"
    _create_valid_database(target)
    owned = open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())
    connection = owned.connection
    started_at = _NOW - started_at_offset_s * 1_000_000
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
        (started_at, json.dumps({"reason": 1, "evidence": {}, "rows": []})))
    connection.execute(
        "INSERT INTO plans (id, request_id, name, created_at, status,"
        " created_event_id, last_event_id, change_count)"
        " VALUES (1, 4242, 'seed', ?, 1, 1, 1, 1)", (started_at,))
    connection.execute(
        "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
        " scheduled_at, group_name, input_fields_json, effective_params_json,"
        " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
        " cancel_requested, error_code, error_details_json, first_window_observed_at,"
        " expiration_reason, source_resolution_state, resolved_source_plan_id,"
        " target_selection_state, created_event_id, last_event_id, change_count)"
        " VALUES (1, 1, 0, 'rec', 2, 'cam-1', ?, NULL, '{}', '{}',"
        " 'camctl-adb', 1000, '{\"target_duration_ms\": 60000}', 2, 1, 0, NULL,"
        " NULL, NULL, NULL, NULL, NULL, NULL, 1, 1, 1)", (started_at,))
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
        (f"{1:032x}", started_at, started_at))
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
        "INSERT INTO recording_processing (id, action_id, source_device_file_id,"
        " check_state, check_decision, check_basis_json, media_json, repair_state,"
        " repair_basis_json, repair_output_file_id, repair_error_json, discard_state,"
        " discard_error_json)"
        " VALUES (41, 1, NULL, 1, 1, NULL, '{}', 1, NULL, NULL, NULL, 1, NULL)")
    connection.commit()
    staging = tmp_path / "staging"
    for name in ("recording-inputs", "derived"):
        (staging / name).mkdir(parents=True)
    return owned, staging


def _value(owned, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


class TestCrossSessionReconciliation:
    async def test_satisfied_timing_stops_and_succeeds(
            self, tmp_path: Path) -> None:
        owned, staging = _environment(tmp_path, started_at_offset_s=65)
        try:
            stopper = _StopDouble()
            runtime = _runtime(
                owned, {1: (_entry("clip-1", size=10),)}, stopper=stopper)
            await capture_handler("camera_record")(1, runtime)
            # 可信计时证明 65 秒已满足目标且未超门槛：立即停止，恢复
            # 控制完成即成功依据。
            assert stopper.calls == ["stop_recording"]
            assert _value(owned, "SELECT status FROM actions WHERE id = 1")[0] == 3
            assert _value(
                owned, "SELECT COUNT(*) FROM outputs WHERE source_action_id = 1"
                ) == (1,)
            decision = _value(
                owned,
                "SELECT check_decision, json_extract(check_basis_json, '$.reason')"
                " FROM recording_processing WHERE id = 41")
            assert decision == (2, 1)
            # 恢复对账入口的释放组合：停止成功后活动收场并释放占
            # 用，同设备下一动作不再被该活动阻挡（O-04）。
            assert _value(
                owned, "SELECT activity_state, occupancy_state"
                " FROM device_activities WHERE id = 1") == (3, 2)
        finally:
            owned.connection.close()

    async def test_unsatisfied_timing_keeps_waiting_then_recovers(
            self, tmp_path: Path) -> None:
        owned, staging = _environment(tmp_path, started_at_offset_s=30)
        try:
            stopper = _StopDouble()
            runtime = _runtime(
                owned, {1: (_entry("clip-1", size=10),)}, stopper=stopper)
            await capture_handler("camera_record")(1, runtime)
            # 尚未满足目标时长：不停止、不定决定，保持执行中等待。
            assert stopper.calls == []
            assert _value(owned, "SELECT status FROM actions WHERE id = 1")[0] == 2
            assert _value(
                owned, "SELECT check_decision FROM recording_processing"
                " WHERE id = 41") == (1,)
            assert _value(
                owned, "SELECT COUNT(*) FROM operation_runs"
                " WHERE responsibility_key = 'stop/1'") == (0,)
            # 剩余时长经过后（新会话、更晚的可信墙钟）对账满足并停止；
            # 恰好达到门槛含等号，不触发修复。
            stopper = _StopDouble()
            later = _runtime(
                owned, {1: (_entry("clip-1", size=10),)}, stopper=stopper,
                wall_us=lambda: _NOW + 40_000_000)
            await capture_handler("camera_record")(1, later)
            assert stopper.calls == ["stop_recording"]
            assert _value(owned, "SELECT status FROM actions WHERE id = 1")[0] == 3
            decision = _value(
                owned,
                "SELECT check_decision, json_extract(check_basis_json, '$.reason')"
                " FROM recording_processing WHERE id = 41")
            assert decision == (2, 1)
        finally:
            owned.connection.close()

    async def test_excess_duration_triggers_repair_without_check(
            self, tmp_path: Path) -> None:
        owned, staging = _environment(tmp_path, started_at_offset_s=120)
        try:
            tools = _RepairTools("200")
            stopper = _StopDouble()
            runtime = _runtime(
                owned, {1: (_entry("clip-1", size=10),)}, stopper=stopper,
                media=_media(owned, tools, staging))
            await capture_handler("camera_record")(1, runtime)
            # 恢复停止确认的控制耗时 120 秒超过目标加余量（70 秒）：
            # 保存异常多录修复决定，不经检查直接修复，成品与原片同
            # 事务登记。
            assert stopper.calls == ["stop_recording"]
            assert tools.calls == ["repair"]
            processing = _value(
                owned,
                "SELECT check_decision, json_extract(check_basis_json, '$.reason'),"
                " repair_state, json_extract(repair_basis_json, '$.reason'),"
                " repair_output_file_id"
                " FROM recording_processing WHERE id = 41")
            assert processing[0] == 2
            assert processing[1] == 3
            assert processing[2] == 5
            assert processing[3] == 2
            assert processing[4] is not None
            kinds = owned.connection.execute(
                "SELECT kind FROM outputs WHERE source_action_id = 1"
                " ORDER BY kind").fetchall()
            assert kinds == [(1,), (2,)]
            assert _value(owned, "SELECT status FROM actions WHERE id = 1")[0] == 3
        finally:
            owned.connection.close()

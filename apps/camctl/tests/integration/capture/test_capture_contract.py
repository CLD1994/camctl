"""C9 三种能力处理器执行链的组件集成测试。

真实 SQLite、授予/尝试/观察/终态事务与契约替身组合：照片、录像、
延时摄影各按处理器推进——授予启动机会、设备契约调用、结果列举观
察登记为设备文件、C6 集合核实并在可判定时保存终态与正式产物。
覆盖正常路径、调用失败保留文件与中断后再次推进的恢复分区。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from camctl.capture.handlers import CaptureRuntime, capture_handler
from camctl.capture.recording import RecordingState
from camctl.capture.results import FileKind as ResultFileKind
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult
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

from ..persistence.test_runtime import _create_valid_database
from ..scheduling.test_resources import _seed_activity

register_operation_guards()
register_capture_guards()
register_timelapse_guards()

_NOW = 1_750_000_000_000_000

_EVIDENCE = EvidenceRegistry(
    (
        EvidenceContract(type="operation_returned", version=1, operation="control",
                         fields=frozenset()),
        EvidenceContract(type="photo_taken", version=1, operation="control",
                         fields=frozenset({"activity_id"}), identity_field="activity_id"),
        EvidenceContract(type="start_confirmed", version=1, operation="control",
                         fields=frozenset({"activity_id"}), identity_field="activity_id"),
        EvidenceContract(type="timelapse_sent", version=1, operation="control",
                         fields=frozenset({"activity_id"}), identity_field="activity_id"),
    )
)


class DriverDouble:
    """契约替身：按操作返回编排的观察与可选错误。"""

    def __init__(self, error=None) -> None:
        self.error = error
        self.calls: list[str] = []

    #: 各操作的观察契约与操作目标（动作身份）。
    _OBSERVATIONS = {
        "take_photo": ("photo_taken", "11"),
        "start_recording": ("start_confirmed", "12"),
        "start_timelapse": ("timelapse_sent", "13"),
    }

    async def control(self, request) -> DeviceCallResult:
        self.calls.append(request.operation)
        observation_type, target = self._OBSERVATIONS[request.operation]
        return DeviceCallResult(
            observations=(
                DeviceObservation(
                    type=observation_type, version=1,
                    data={"activity_id": target}),
            ),
            error=self.error,
        )


class ResultsDouble:
    """结果列举替身：返回编排的候选产物文件。"""

    def __init__(self, files_by_action: dict[int, tuple]) -> None:
        self.files_by_action = files_by_action

    async def list_files(self, action_id: int) -> tuple:
        return self.files_by_action.get(action_id, ())


def _seed_stopped_recording(connection, action_id: int) -> None:
    """真实停止流程行与确认停止尝试，供生产中段装载器读取。"""
    connection.execute(
        "INSERT INTO operation_runs (id, action_id, delivery_id, kind, query_purpose,"
        " responsibility_key, activity_id, copy_id, cleanup_item_id, session_key,"
        " status, attempts_used, max_attempts_used, timeout_s_json,"
        " retry_interval_s_json, retry_wait_required, error_json)"
        " VALUES (30, ?, NULL, 2, NULL, 'stop/12', 12, NULL, NULL, NULL, 1, 1, 3,"
        " '10', '1', 0, NULL)", (action_id,))
    connection.execute(
        "INSERT INTO operation_attempts (id, run_id, attempt_no, status,"
        " intent_event_id, result_event_id, max_attempts_used, effect_state,"
        " result_json) VALUES (40, 30, 1, 2, 1, 1, 3, 3, '{}')")


def _entry(identity: str, *, size: int = 4096,
           kind: ResultFileKind = ResultFileKind.VIDEO) -> object:
    from camctl.capture.handlers import ObservedFile

    return ObservedFile(
        identity=identity,
        locator={"path": f"/DCIM/{identity}"},
        evidence={"listing": identity},
        complete=True,
        size_bytes=size,
        kind=kind,
        original_name=f"{identity}.mp4",
        media_type="video/mp4",
    )


# 只种子本测试需要的动作；同计划更早动作会阻塞启动授予。
def _environment(tmp_path: Path, actions):
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
    for action_id, action_type in actions:
        _seed_action(connection, action_id, action_type)
        _seed_activity(connection, action_id)
    connection.commit()
    return owned


_PHOTO = ((11, 1),)
_RECORD = ((12, 2),)
_TIMELAPSE = ((13, 3),)


def _seed_action(connection, action_id: int, action_type: int) -> None:
    params = "{}" if action_type != 2 else '{"target_duration_s": 60}'
    connection.execute(
        "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
        " scheduled_at, group_name, input_fields_json, effective_params_json,"
        " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
        " cancel_requested, error_code, error_details_json, first_window_observed_at,"
        " expiration_reason, source_resolution_state, resolved_source_plan_id,"
        " target_selection_state, created_event_id, last_event_id, change_count)"
        " VALUES (?, 1, ?, ?, ?, 'cam-1', ?, NULL, '{}', ?, 'camctl-adb', 1000,"
        " '{}', 2, 1, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, 1, 1)",
        (action_id, action_id - 11, f"act-{action_id}", action_type, _NOW, params),
    )


def _seed_processing(connection, action_id: int) -> None:
    """正常录像的处理责任：无需检查（控制完成即成功依据）。"""
    connection.execute(
        "INSERT INTO recording_processing (id, action_id, source_device_file_id,"
        " check_state, check_decision, check_basis_json, media_json, repair_state,"
        " repair_basis_json, repair_output_file_id, repair_error_json, discard_state,"
        " discard_error_json)"
        " VALUES (?, ?, NULL, 1, 2, '{}', '{}', 1, NULL, NULL, NULL, 1, NULL)",
        (action_id + 40, action_id),
    )


def _runtime(owned, *, driver=None, files=None, wall=None,
             recording_state=None) -> CaptureRuntime:
    from camctl.scheduling.rules import LaunchWindow

    return CaptureRuntime(
        owned=owned,
        scheduling=SchedulingRepository(),
        operations=OperationRepository(),
        capture=CaptureRepository(),
        timelapse=TimelapseRepository(),
        driver=driver if driver is not None else DriverDouble(),
        results=ResultsDouble(files or {}),
        evidence=_EVIDENCE,
        wall_us=lambda: wall if wall is not None else _NOW,
        monotonic_ns=lambda: 5_000_000_000,
        window_of=lambda action: LaunchWindow(
            scheduled_at=action["scheduled_at"],
            window_end=action["scheduled_at"] + action["max_delay_ms"] * 1000),
        wait_config=lambda params: CaptureWaitConfig(
            target_duration_ms=600_000, driver_margin_ms=0),
        recording_state=recording_state,
    )


def _value(owned, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


pytestmark = pytest.mark.asyncio


class TestPhotoHandler:
    async def test_normal_path_registers_output_and_succeeds(self, tmp_path: Path):
        owned = _environment(tmp_path, _PHOTO)
        try:
            runtime = _runtime(owned, files={11: (_entry("shot-1", kind=ResultFileKind.PHOTO),)})
            await capture_handler("camera_take_photo")(11, runtime)
            assert _value(owned, "SELECT status FROM actions WHERE id = 11") == (3,)
            output = _value(
                owned,
                "SELECT o.kind, o.device_file_id, f.completion_state, f.size_bytes,"
                " f.source_action_id FROM outputs o JOIN device_files f"
                " ON f.id = o.device_file_id WHERE o.source_action_id = 11")
            assert output == (1, 1, 3, 4096, 11)
            assert _value(
                owned, "SELECT COUNT(*) FROM operation_attempts") == (1,)
        finally:
            owned.connection.close()

    async def test_failed_call_keeps_files_and_fails_action(self, tmp_path: Path):
        owned = _environment(tmp_path, _PHOTO)
        try:
            runtime = _runtime(
                owned, driver=DriverDouble(error={"code": "device_error"}),
                files={11: (_entry("shot-1", kind=ResultFileKind.PHOTO),)})
            await capture_handler("camera_take_photo")(11, runtime)
            assert _value(owned, "SELECT status FROM actions WHERE id = 11") == (4,)
            row = _value(
                owned, "SELECT error_code, error_details_json FROM actions WHERE id = 11")
            assert row[0] == 13
            # 失败仍登记完整且归属明确的文件，不删除。
            assert _value(
                owned, "SELECT completion_state FROM device_files"
                " WHERE observer_action_id = 11") == (3,)
            assert _value(owned, "SELECT COUNT(*) FROM outputs") == (1,)
        finally:
            owned.connection.close()

    async def test_incomplete_listing_waits_then_recovers(self, tmp_path: Path):
        owned = _environment(tmp_path, _PHOTO)
        try:
            runtime = _runtime(owned, files={})
            await capture_handler("camera_take_photo")(11, runtime)
            assert _value(owned, "SELECT status FROM actions WHERE id = 11") == (2,)
            assert _value(owned, "SELECT COUNT(*) FROM operation_attempts") == (1,)
            # 结果尚未列举：第二次推进不重复调用，仅消费迟到的结果。
            runtime.results.files_by_action[11] = (
                _entry("shot-1", kind=ResultFileKind.PHOTO),)
            await capture_handler("camera_take_photo")(11, runtime)
            assert _value(owned, "SELECT status FROM actions WHERE id = 11") == (3,)
            assert _value(owned, "SELECT COUNT(*) FROM operation_attempts") == (1,)
            # 终态后再次推进不产生新事实。
            await capture_handler("camera_take_photo")(11, runtime)
            assert _value(owned, "SELECT COUNT(*) FROM outputs") == (1,)
        finally:
            owned.connection.close()


class TestRecordHandler:
    async def test_start_then_stopped_tail_finishes(self, tmp_path: Path):
        owned = _environment(tmp_path, _RECORD)
        try:
            runtime = _runtime(owned, files={})
            # 第一次推进：尚未启动，只发起启动调用。
            await capture_handler("camera_record")(12, runtime)
            assert _value(
                owned, "SELECT COUNT(*) FROM operation_attempts"
                " a JOIN operation_runs r ON a.run_id = r.id"
                " WHERE r.responsibility_key = 'start/12'") == (1,)
            assert _value(owned, "SELECT status FROM actions WHERE id = 12") == (2,)
            # 停止与处理责任保存后，第二次推进经生产装载器走尾段。
            _seed_processing(owned.connection, 12)
            _seed_stopped_recording(owned.connection, 12)
            owned.connection.commit()
            runtime.results.files_by_action[12] = (_entry("clip-1"),)
            await capture_handler("camera_record")(12, runtime)
            assert _value(owned, "SELECT status FROM actions WHERE id = 12") == (3,)
            output = _value(
                owned, "SELECT kind, device_file_id FROM outputs"
                " WHERE source_action_id = 12")
            assert output == (1, 1)
        finally:
            owned.connection.close()

    async def test_processing_pending_keeps_action_running(self, tmp_path: Path):
        owned = _environment(tmp_path, _RECORD)
        try:
            runtime = _runtime(owned, files={12: (_entry("clip-1"),)})
            await capture_handler("camera_record")(12, runtime)
            # 启动调用先行保存；处理责任未建立，终态等待媒体链。
            assert _value(owned, "SELECT status FROM actions WHERE id = 12") == (2,)
            assert _value(owned, "SELECT COUNT(*) FROM outputs") == (0,)
        finally:
            owned.connection.close()


class TestTimelapseHandler:
    async def test_send_wait_then_finish(self, tmp_path: Path):
        owned = _environment(tmp_path, _TIMELAPSE)
        try:
            runtime = _runtime(owned, files={13: (_entry("sequence-1"),)})
            # 第一次推进：发送并安排等待。
            await capture_handler("camera_timelapse")(13, runtime)
            assert _value(
                owned, "SELECT expected_check_at FROM device_activities WHERE id = 13"
            ) == (_NOW + 600_000_000,)
            assert _value(owned, "SELECT status FROM actions WHERE id = 13") == (2,)
            # 到达预计检查时间后再次推进：核实并保存终态。
            late = _runtime(owned, wall=_NOW + 700_000_000,
                            files={13: (_entry("sequence-1"),)})
            late.driver.calls = list(runtime.driver.calls)
            await capture_handler("camera_timelapse")(13, late)
            assert _value(owned, "SELECT status FROM actions WHERE id = 13") == (3,)
            assert _value(owned, "SELECT COUNT(*) FROM outputs") == (1,)
        finally:
            owned.connection.close()


class TestDispatchLoop:
    async def test_dispatch_routes_all_capabilities_per_plan(self, tmp_path: Path):
        owned = _environment(tmp_path, ((11, 1), (12, 2), (13, 3)))
        # 各能力单独计划，避免同计划更早动作阻塞授予。
        for action_id in (12, 13):
            owned.connection.execute(
                "UPDATE actions SET plan_id = ? WHERE id = ?",
                (action_id - 10, action_id))
            owned.connection.execute(
                "INSERT INTO plans (id, request_id, name, created_at, status,"
                " created_event_id, last_event_id, change_count)"
                " VALUES (?, ?, 'p', ?, 1, 1, 1, 1)",
                (action_id - 10, 9000 + action_id, _NOW))
        owned.connection.commit()
        from camctl.capture.dispatch import dispatch_ready, ready_capture_actions

        runtime = _runtime(owned, files={
            11: (_entry("shot-1", kind=ResultFileKind.PHOTO),)})
        results = await dispatch_ready(
            runtime, ready_capture_actions(owned.connection, _NOW))
        assert [item[0] for item in results] == [11, 12, 13]
        assert all(item[1].phase == "dispatched" for item in results)
        # 照片一次推进即终态；录像与延时仍按各自阶段推进。
        assert _value(owned, "SELECT status FROM actions WHERE id = 11") == (3,)


class TestInterruptionRecovery:
    async def test_fault_at_file_registration_rolls_back_and_recovers(
            self, tmp_path: Path):
        from dataclasses import replace

        from ..operations.test_result_reuse import _FaultConnection

        owned = _environment(tmp_path, _PHOTO)
        runtime = _runtime(
            owned, files={11: (_entry("shot-1", kind=ResultFileKind.PHOTO),)})
        runtime.owned = replace(
            owned, connection=_FaultConnection(owned.connection, "INSERT INTO device_files"))
        try:
            await capture_handler("camera_take_photo")(11, runtime)
        except Exception:
            pass  # 文件登记失败的注入故障；此处只核对回滚与未终态。
        assert _value(owned, "SELECT status FROM actions WHERE id = 11") == (2,)
        before_attempts = _value(
            owned, "SELECT COUNT(*) FROM operation_attempts")[0]
        recovery = _runtime(
            owned, files={11: (_entry("shot-1", kind=ResultFileKind.PHOTO),)})
        await capture_handler("camera_take_photo")(11, recovery)
        assert _value(owned, "SELECT status FROM actions WHERE id = 11") == (3,)
        # 重入不重复调用设备，也不重复登记产物。
        assert _value(owned, "SELECT COUNT(*) FROM operation_attempts")[0] == before_attempts
        assert _value(owned, "SELECT COUNT(*) FROM outputs") == (1,)

    async def test_history_bytes_stable_after_terminal_redispatch(
            self, tmp_path: Path):
        from camctl.capture.dispatch import dispatch_ready

        owned = _environment(tmp_path, _PHOTO)
        runtime = _runtime(
            owned, files={11: (_entry("shot-1", kind=ResultFileKind.PHOTO),)})
        await dispatch_ready(runtime, [
            __import__("camctl.scheduling.service", fromlist=["ActionDescriptor"])
            .ActionDescriptor(action_id=11, action_type="camera_take_photo", ready=True)])
        assert _value(owned, "SELECT status FROM actions WHERE id = 11") == (3,)
        before = tuple(owned.connection.execute(
            "SELECT id, body_json FROM history_events ORDER BY id").fetchall())
        await dispatch_ready(runtime, [
            __import__("camctl.scheduling.service", fromlist=["ActionDescriptor"])
            .ActionDescriptor(action_id=11, action_type="camera_take_photo", ready=True)])
        after = tuple(owned.connection.execute(
            "SELECT id, body_json FROM history_events ORDER BY id").fetchall())
        assert after == before

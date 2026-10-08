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
        EvidenceContract(type="results_returned", version=1, operation="result",
                         fields=frozenset()),
        EvidenceContract(type="stop_returned", version=1, operation="stop",
                         fields=frozenset()),
        EvidenceContract(type="stop_confirmed", version=1, operation="stop",
                         fields=frozenset({"activity_id"}), identity_field="activity_id"),
    )
)


class StopDouble:
    """停止端口替身：返回编排身份的确认观察。"""

    def __init__(self, identity: str = "12") -> None:
        self._identity = identity

    async def stop(self, request) -> DeviceCallResult:
        return DeviceCallResult(
            observations=(
                DeviceObservation(
                    type="stop_confirmed", version=1,
                    data={"activity_id": self._identity}),
            ),
            error=None,
        )


class DriverDouble:
    """契约替身：按操作返回编排的观察与可选错误。"""

    def __init__(self, error=None, identity_override=None) -> None:
        self.error = error
        self.identity_override = identity_override
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
                    data={"activity_id": self.identity_override or target}),
            ),
            error=self.error,
        )


class ResultsDouble:
    """结果列举替身：返回编排的候选产物文件。"""

    def __init__(self, files_by_action: dict[int, tuple]) -> None:
        self.files_by_action = files_by_action
        self.calls: list[int] = []

    async def list_files(self, action_id: int) -> tuple:
        self.calls.append(action_id)
        return self.files_by_action.get(action_id, ())


def _seed_stopped_recording(connection, action_id: int) -> None:
    """真实停止流程行与确认停止尝试，供生产中段装载器读取。"""
    connection.execute(
        "INSERT INTO operation_runs (id, action_id, delivery_id, kind, query_purpose,"
        " responsibility_key, activity_id, copy_id, cleanup_item_id, session_key,"
        " status, attempts_used, max_attempts_used, timeout_s_json,"
        " retry_interval_s_json, retry_wait_required, error_json)"
        " VALUES (30, ?, NULL, 2, NULL, 'stop/12', 12, NULL, NULL, NULL, 3, 1, 3,"
        " '10', '1', 0, NULL)", (action_id,))
    connection.execute(
        "INSERT INTO operation_attempts (id, run_id, attempt_no, status,"
        " intent_event_id, result_event_id, max_attempts_used, effect_state,"
        " result_json) VALUES (40, 30, 1, 2, 1, 1, 3, 3, '{}')")


def _seed_open_start(connection, action_id: int, *,
                     attempt_status: int = 1) -> None:
    """调用发出后结果未保存的启动责任：流程执行中、尝试在途。

    对应进程中断或保存被拒后重入的状态：动作运行中、活动可能已
    派发、启动尝试没有可采纳的结束结果；活动不保存发送时间，表
    达"可能派发但无可靠发送时间"的恢复前提。
    """
    connection.execute(
        "INSERT INTO operation_runs (id, action_id, delivery_id, kind, query_purpose,"
        " responsibility_key, activity_id, copy_id, cleanup_item_id, session_key,"
        " status, attempts_used, max_attempts_used, timeout_s_json,"
        " retry_interval_s_json, retry_wait_required, error_json)"
        f" VALUES (50, ?, NULL, 1, NULL, 'start/{action_id}', {action_id},"
        " NULL, NULL, NULL, 2, 1, 1, '30', '1', 0, NULL)", (action_id,))
    connection.execute(
        "INSERT INTO operation_attempts (id, run_id, attempt_no, status,"
        " intent_event_id, result_event_id, max_attempts_used, effect_state,"
        " result_json) VALUES (51, 50, 1, ?, 1, NULL, 1, 1, NULL)",
        (attempt_status,))
    connection.execute(
        "UPDATE device_activities SET dispatch_state = 2 WHERE id = ?",
        (action_id,))


def _entry(identity: str, *, size: int = 4096,
           kind: ResultFileKind = ResultFileKind.VIDEO,
           complete: bool = True) -> object:
    from camctl.capture.handlers import ObservedFile

    return ObservedFile(
        identity=identity,
        locator={"path": f"/DCIM/{identity}"},
        evidence={"listing": identity},
        complete=complete,
        size_bytes=size if complete else None,
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
        if action_type == 3:
            # 发送后等待的延时任务固定时间与产物完成方式，判定从
            # UNDETERMINED 开始。
            connection.execute(
                "UPDATE device_activities SET completion_mode = 2,"
                " completion_basis = 1 WHERE id = ?", (action_id,))
        elif action_type == 1:
            # 照片活动创建时同样从 UNDETERMINED 开始（operation-
            # fields.md#设备活动字段）；录像的采集判定列保持为空。
            connection.execute(
                "UPDATE device_activities SET completion_basis = 1"
                " WHERE id = ?", (action_id,))
    connection.commit()
    return owned


_PHOTO = ((11, 1),)
_RECORD = ((12, 2),)
_TIMELAPSE = ((13, 3),)


def _seed_action(connection, action_id: int, action_type: int) -> None:
    """录像种子按受理约定保存执行定义（目标时长毫秒）。"""
    spec = '{"target_duration_ms": 60000}' if action_type == 2 else '{}'
    connection.execute(
        "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
        " scheduled_at, group_name, input_fields_json, effective_params_json,"
        " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
        " cancel_requested, error_code, error_details_json, first_window_observed_at,"
        " expiration_reason, source_resolution_state, resolved_source_plan_id,"
        " target_selection_state, created_event_id, last_event_id, change_count)"
        " VALUES (?, 1, ?, ?, ?, 'cam-1', ?, NULL, '{}', '{}',"
        " 'camctl-adb', 1000, ?, 2, 1, 0, NULL, NULL, NULL, NULL, NULL, NULL,"
        " NULL, 1, 1, 1)",
        (action_id, action_id - 11, f"act-{action_id}", action_type, _NOW, spec),
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
             recording_state=None, results=None,
             listing_cache=None, check_config=None,
             stop_config=None) -> CaptureRuntime:
    from camctl.scheduling.rules import LaunchWindow

    return CaptureRuntime(
        owned=owned,
        scheduling=SchedulingRepository(),
        operations=OperationRepository(),
        capture=CaptureRepository(),
        timelapse=TimelapseRepository(),
        driver=driver if driver is not None else DriverDouble(),
        results=results if results is not None else ResultsDouble(files or {}),
        evidence=_EVIDENCE,
        wall_us=lambda: wall if wall is not None else _NOW,
        monotonic_ns=lambda: 5_000_000_000,
        window_of=lambda action: LaunchWindow(
            scheduled_at=action["scheduled_at"],
            window_end=action["scheduled_at"] + action["max_delay_ms"] * 1000),
        wait_config=lambda params: CaptureWaitConfig(
            target_duration_ms=600_000, driver_margin_ms=0),
        recording_state=recording_state,
        listing_cache=listing_cache,
        **({"check_config": check_config} if check_config is not None else {}),
        **({"stop_config": stop_config} if stop_config is not None else {}),
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
            # 启动调用与首轮结果核实各占一次尝试。
            assert _value(
                owned, "SELECT COUNT(*) FROM operation_attempts") == (2,)
            # 成功链收场活动：调用成功返回即结束证据，占用同链释放。
            activity = _value(
                owned, "SELECT activity_state, occupancy_state, dispatch_state"
                " FROM device_activities WHERE id = 11")
            assert activity == (3, 2, 3)
            # 再次推进幂等：活动已收场，不产生新的释放事件。
            await capture_handler("camera_take_photo")(11, runtime)
            assert _value(
                owned, "SELECT COUNT(*) FROM history_events WHERE event_type = 13"
                " AND json_extract(body_json, '$.reason') = 3") == (1,)
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
            # 启动调用与首轮核实（可靠返回但产物暂不齐备）各占一次尝试。
            assert _value(owned, "SELECT COUNT(*) FROM operation_attempts") == (2,)
            # 产物暂不齐备：第二次推进不重复调用设备，可靠列举轮次已
            # 收场核实责任，迟到结果经直接列举消费。
            runtime.results.files_by_action[11] = (
                _entry("shot-1", kind=ResultFileKind.PHOTO),)
            await capture_handler("camera_take_photo")(11, runtime)
            assert _value(owned, "SELECT status FROM actions WHERE id = 11") == (3,)
            assert _value(owned, "SELECT COUNT(*) FROM operation_attempts") == (2,)
            # 终态后再次推进不产生新事实。
            await capture_handler("camera_take_photo")(11, runtime)
            assert _value(owned, "SELECT COUNT(*) FROM outputs") == (1,)
        finally:
            owned.connection.close()

    async def test_invalid_observation_settles_start_and_fails_action(
            self, tmp_path: Path):
        owned = _environment(tmp_path, _PHOTO)
        try:
            # 驱动确认观察的身份与操作目标（活动 11）不符：结果不可
            # 采纳，调用按失败收场，不遗留执行中的启动流程。
            runtime = _runtime(
                owned, driver=DriverDouble(identity_override="99"),
                files={11: (_entry("shot-1", kind=ResultFileKind.PHOTO),)})
            await capture_handler("camera_take_photo")(11, runtime)
            start_run = _value(
                owned, "SELECT status, error_json FROM operation_runs"
                " WHERE responsibility_key = 'start/11'")
            assert start_run[0] == 4
            assert start_run[1] is not None
            attempt = _value(
                owned, "SELECT a.status FROM operation_attempts a"
                " JOIN operation_runs r ON a.run_id = r.id"
                " WHERE r.responsibility_key = 'start/11'")
            assert attempt == (3,)
            # 坏结果按调用失败进入决策：保留完整文件并失败动作。
            assert _value(owned, "SELECT status FROM actions WHERE id = 11") == (4,)
            assert _value(owned, "SELECT COUNT(*) FROM outputs") == (1,)
        finally:
            owned.connection.close()

    async def test_open_start_with_outputs_recovers_success(self, tmp_path: Path):
        owned = _environment(tmp_path, _PHOTO)
        try:
            _seed_open_start(owned.connection, 11)
            owned.connection.commit()
            runtime = _runtime(owned, files={11: (
                _entry("shot-1", kind=ResultFileKind.PHOTO),)})
            # 在途启动尝试没有可采纳的响应结果：不折叠为失败，由
            # 产物核实证明完成。
            await capture_handler("camera_take_photo")(11, runtime)
            assert _value(owned, "SELECT status FROM actions WHERE id = 11") == (3,)
            assert _value(owned, "SELECT COUNT(*) FROM outputs") == (1,)
            # 动作终态后启动流程伴随收场，会话收尾计数可归零。
            assert _value(
                owned, "SELECT status FROM operation_runs"
                " WHERE responsibility_key = 'start/11'") == (3,)
        finally:
            owned.connection.close()

    async def test_open_start_without_outputs_keeps_verifying(
            self, tmp_path: Path):
        owned = _environment(tmp_path, _PHOTO)
        try:
            _seed_open_start(owned.connection, 11)
            owned.connection.commit()
            runtime = _runtime(owned, files={})
            await capture_handler("camera_take_photo")(11, runtime)
            # 结果未知且暂无产物：保持运行等待核实轮次，不折叠失败。
            assert _value(owned, "SELECT status FROM actions WHERE id = 11") == (2,)
            start_run = _value(
                owned, "SELECT status FROM operation_runs"
                " WHERE responsibility_key = 'start/11'")
            assert start_run == (2,)
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

    async def test_invalid_stop_observation_settles_stop_attempt(
            self, tmp_path: Path):
        owned = _environment(tmp_path, _RECORD)
        try:
            # 真实启动后到达停止阶段：驱动停止确认观察的身份与操作
            # 目标（活动 12）不符时，结果不可采纳，停止尝试按调用失
            # 败收场，不遗留执行中的停止流程与在途尝试。
            runtime = _runtime(owned, files={})
            await capture_handler("camera_record")(12, runtime)
            from camctl.capture.handlers import _stop_call
            runtime.stopper = StopDouble(identity="99")
            step = await _stop_call(runtime, runtime.action(12))
            assert step.phase == "call_failed"
            stop_run = _value(
                owned, "SELECT status, error_json FROM operation_runs"
                " WHERE responsibility_key = 'stop/12'")
            assert stop_run[0] == 4
            assert stop_run[1] is not None
            attempt = _value(
                owned, "SELECT a.status FROM operation_attempts a"
                " JOIN operation_runs r ON a.run_id = r.id"
                " WHERE r.responsibility_key = 'stop/12'")
            assert attempt == (3,)
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

    async def test_open_start_without_sent_at_fails_unconfirmed(
            self, tmp_path: Path):
        owned = _environment(tmp_path, _RECORD)
        try:
            _seed_open_start(owned.connection, 12)
            owned.connection.commit()
            runtime = _runtime(owned, files={})
            # 启动调用可能在途且无可靠发送时间：无法核实原任务，
            # 按无法确认失败收场；不重复启动、不补造时间。
            await capture_handler("camera_record")(12, runtime)
            assert _value(owned, "SELECT status FROM actions WHERE id = 12") == (4,)
            row = _value(
                owned, "SELECT error_code, error_details_json"
                " FROM actions WHERE id = 12")
            assert row[0] == 12
            assert json.loads(row[1])["reason"] == "start_unknown"
            # 启动流程伴随收场为无法确认；活动占用保持未知，不释放。
            assert _value(
                owned, "SELECT status FROM operation_runs"
                " WHERE responsibility_key = 'start/12'") == (6,)
            activity = _value(
                owned, "SELECT occupancy_state FROM device_activities"
                " WHERE id = 12")
            assert activity == (1,)
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

    async def test_open_start_without_sent_at_fails_unconfirmed(
            self, tmp_path: Path):
        owned = _environment(tmp_path, _TIMELAPSE)
        try:
            _seed_open_start(owned.connection, 13)
            owned.connection.commit()
            runtime = _runtime(owned, files={})
            # 等待锚点依赖可靠发送时间：可能派发但没有发送时间的
            # 启动无法核实原任务，按无法确认失败收场；不重复启动、
            # 不补造时间。
            await capture_handler("camera_timelapse")(13, runtime)
            assert _value(owned, "SELECT status FROM actions WHERE id = 13") == (4,)
            row = _value(
                owned, "SELECT error_code, error_details_json"
                " FROM actions WHERE id = 13")
            assert row[0] == 12
            assert json.loads(row[1])["reason"] == "start_unknown"
            # 启动流程伴随收场为无法确认；活动占用保持未知，不释放。
            assert _value(
                owned, "SELECT status FROM operation_runs"
                " WHERE responsibility_key = 'start/13'") == (6,)
            activity = _value(
                owned, "SELECT occupancy_state FROM device_activities"
                " WHERE id = 13")
            assert activity == (1,)
        finally:
            owned.connection.close()


class TestDispatchLoop:
    @pytest.mark.parametrize("action_id,action_type", [(11, 1), (12, 2), (13, 3)])
    @pytest.mark.parametrize("canceled", [False, True])
    async def test_unstarted_action_finishes_without_device_work(
            self, tmp_path, action_id, action_type, canceled):
        from camctl.capture.dispatch import dispatch_ready, ready_capture_actions

        owned = _environment(tmp_path, ((action_id, action_type),))
        try:
            if canceled:
                owned.connection.execute(
                    "UPDATE actions SET cancel_requested = 1 WHERE id = ?", (action_id,))
                owned.connection.commit()
            driver = DriverDouble()
            files = ResultsDouble({})
            runtime = _runtime(owned, driver=driver, results=files, wall=_NOW + 1_000_001)
            outcomes = await dispatch_ready(
                runtime, ready_capture_actions(owned.connection, _NOW + 1_000_001))
            assert len(outcomes) == 1
            assert not isinstance(outcomes[0][1], BaseException), outcomes
            assert _value(owned, "SELECT status, execution_started, cancel_requested"
                          " FROM actions WHERE id = ?", action_id) == (
                              6 if canceled else 5, 1, int(canceled))
            assert _value(owned, "SELECT dispatch_state, activity_state, occupancy_state"
                          " FROM device_activities WHERE action_id = ?", action_id) == (1, 1, 2)
            assert _value(owned, "SELECT COUNT(*) FROM operation_attempts") == (0,)
            assert driver.calls == []
            assert files.calls == []
        finally:
            owned.connection.close()

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

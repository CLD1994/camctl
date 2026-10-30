"""C9 三种能力处理器执行链的组件集成测试。

真实 SQLite、授予/尝试/观察/终态事务与契约替身组合：照片、录像、
延时摄影各按处理器推进——授予启动机会、设备契约调用、结果列举观
察登记为设备文件、C6 集合核实并在可判定时保存终态与正式产物。
覆盖正常路径、调用失败保留文件与中断后再次推进的恢复分区。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from ..support.result_cases import result_case, result_case_for

from camctl.capture.handlers import CaptureRuntime, ListedResult, capture_handler
from camctl.capture.result_inputs import RESULT_PAGE_CONTRACT, page_from_outcome
from camctl.capture.recording import RecordingState
from camctl.capture.results import FileKind as ResultFileKind
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.contracts.enums import load_registry as load_enum_registry
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult
from camctl.operations.models import (
    AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue,
    Settlement, SettlementBasis,
)
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

_CONTRACTS = (
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
        EvidenceContract(type="result_files_listed", version=1, operation="result",
                         fields=frozenset({"activity_id", "entries"}), identity_field="activity_id"),
        EvidenceContract(type="stop_returned", version=1, operation="stop",
                         fields=frozenset()),
        EvidenceContract(type="stop_confirmed", version=1, operation="stop",
                         fields=frozenset({"activity_id"}), identity_field="activity_id"),
)
_EVIDENCE = EvidenceRegistry(_CONTRACTS)
_PAGE_EVIDENCE = EvidenceRegistry((*_CONTRACTS, RESULT_PAGE_CONTRACT))


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
    """受 v2 约束的任务范围替身，明确提供编排的全部文件集合。"""

    def __init__(self, files_by_action: dict[int, tuple], *, set_finalized=True) -> None:
        self.files_by_action = files_by_action
        self.calls: list[int] = []
        self.set_finalized = set_finalized

    async def list_page(self, ticket, *, cursor, timeout_s, output_scope=None,
                        completion_context=None, video_result_mode=None, reading_context=None):
        if cursor is not None:
            raise ValueError("替身单批完整集合没有后续游标")
        listed = await self.list_round(ticket, timeout_s=timeout_s)
        observations = []
        for observation in listed.outcome.observations:
            if observation.type == "result_files_listed":
                observations.append(DeviceObservation(observation.type, 2, {
                    **observation.data, "cursor": None, "next_cursor": None,
                    "set_finalized": self.set_finalized, "completion_evidence": None}))
            else:
                observations.append(observation)
        actual = replace(listed.outcome, observations=tuple(observations))
        return page_from_outcome(ticket, actual, cursor=cursor)

    async def list_files(self, action_id: int) -> tuple:
        self.calls.append(action_id)
        return self.files_by_action.get(action_id, ())

    @staticmethod
    def serialized_entries(entries):
        return [{"identity": entry.identity, "locator": dict(entry.locator),
                 "complete": entry.complete, "size_bytes": entry.size_bytes,
                 "kind": entry.kind.value, "original_name": entry.original_name,
                 "media_type": entry.media_type, "paired_identity": entry.paired_identity,
                 **({"format_id": entry.format_id} if entry.format_id is not None else {})}
                for entry in entries]

    async def list_round(self, ticket, *, timeout_s) -> ListedResult:
        try:
            entries = await self.list_files(int(ticket.target_id))
        except RuntimeError:
            return ListedResult((), CallOutcome(
                status=AttemptStatus.FAILED, error=ErrorValue("device_error", "device"),
                effect=EffectState.UNKNOWN, settlement=Settlement(
                    SettlementBasis.OBSERVED, EvidenceValue("results_returned", 1, {}))))
        files = self.serialized_entries(entries)
        return ListedResult(entries, CallOutcome(
            status=AttemptStatus.SUCCEEDED, error=None, effect=EffectState.CONFIRMED,
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("results_returned", 1, {})),
            observations=(DeviceObservation("result_files_listed", 1,
                {"activity_id": ticket.target_id, "entries": files}),)))


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
        " VALUES (1, 1, 1, 1, ?, 2, 1, ?)",
        (_NOW, json.dumps({"reason": 1, "evidence": {}, "rows": [{
            "table": "plans", "id": 1,
            "before": {"exists": False, "values": {}},
            "after": {"exists": True, "values": {
                "request_id": 4242, "name": "seed", "created_at": _NOW, "status": 1}},
        }]})))
    connection.execute(
        "INSERT INTO plans (id, request_id, name, created_at, status,"
        " created_event_id, last_event_id, change_count)"
        " VALUES (1, 4242, 'seed', ?, 1, 1, 1, 1)", (_NOW,))
    plan_type = load_enum_registry()["history_objects"]["plan"]["id"]
    connection.execute(
        "INSERT INTO entity_event_links (entity_type,entity_id,event_id,change_count)"
        " VALUES (?,1,1,1)", (plan_type,))
    connection.execute(
        "INSERT INTO report_entity_changes (entity_type,entity_id,event_id,change_seq)"
        " VALUES (?,1,1,1)", (plan_type,))
    connection.execute(
        "INSERT INTO entity_snapshot_progress (entity_type,entity_id,current_change_count,"
        " snapshot_change_count,latest_snapshot_id) VALUES (?,1,1,0,NULL)", (plan_type,))
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
    """按动作类型保存有效的输入事实与固定执行定义。"""
    input_fields = (
        {"params": {"type": "legacy_photo"},
         "policy": {"max_delay_ms": 1000}}
        if action_type == 1 else {}
    )
    effective_params = {"type": "legacy_photo"} if action_type == 1 else {}
    spec = '{}'
    if action_type == 2:
        spec = json.dumps({"video_result_mode": "single_file", "target_duration_ms": 60000})
    if action_type == 3:
        spec = json.dumps({"duration_based": True, "wait_after_send": True,
            "end_control": 1, "stop_supported": True, "start_return_meaning": 1,
            "completion_mode": 2, "target_duration_ms": 600000, "result_wait_margin_ms": 0,
            "video_result_mode": "single_file"})
    connection.execute(
        "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
        " scheduled_at, group_name, input_fields_json, effective_params_json,"
        " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
        " cancel_requested, error_code, error_details_json, first_window_observed_at,"
        " expiration_reason, source_resolution_state, resolved_source_plan_id,"
        " target_selection_state, created_event_id, last_event_id, change_count)"
        " VALUES (?, 1, ?, ?, ?, 'cam-1', ?, NULL, ?, ?,"
        " 'camctl-adb', 1000, ?, 2, 1, 0, NULL, NULL, NULL, NULL, NULL, NULL,"
        " NULL, 1, 1, 1)",
        (action_id, action_id - 11, f"act-{action_id}", action_type, _NOW,
         json.dumps(input_fields), json.dumps(effective_params), spec),
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


async def _runtime(owned, *, driver=None, files=None, wall=None,
                   results=None, check_config=None, stop_config=None,
                   evidence=None, directory=None, **options):
    from ..support.capture import capture_runtime

    result_port = results if results is not None else ResultsDouble(files or {})
    runtime = await capture_runtime(
        owned,
        driver=driver if driver is not None else DriverDouble(),
        results=result_port,
        evidence=evidence if evidence is not None else (
            _PAGE_EVIDENCE if isinstance(result_port, ResultsDouble) else _EVIDENCE),
        directory=directory,
        wall_us=options.pop("wall_us", lambda: wall if wall is not None else _NOW),
        monotonic_ns=options.pop("monotonic_ns", lambda: 5_000_000_000),
        check_config=check_config,
        stop_config=stop_config,
        **options,
    )
    return runtime


def _value(owned, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


pytestmark = pytest.mark.asyncio


async def _handler_world(tmp_path, consumer):
    from decimal import Decimal
    from camctl.operations.attempts import AttemptConfig
    return await result_case_for().world(tmp_path, consumer, start=False,
        assembly_options={"check_config": AttemptConfig(3, Decimal(10), Decimal(3))})


async def _interrupted_start_world(tmp_path, consumer, monkeypatch, *, qualified=False):
    from camctl.capture.recovery import RecoveryBoundary
    from camctl.persistence.models import DbOutcomeKind
    case = result_case_for()
    contract = EvidenceContract("adb_foreground_recovery", 1, "control", frozenset())
    owned, runtime, aid, handler = await case.world(tmp_path, consumer, start=False,
        assembly_options={"control_recovery": qualified},
        evidence_contracts=(contract,) if qualified else ())
    class StartIntentSaved(Exception):
        pass
    original = SchedulingRepository.grant_start
    def after_saved(repository, request, key, current):
        receipt = original(repository, request, key, current)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        assert receipt.value.ticket is not None
        return replace(receipt, handoff_error=StartIntentSaved())
    with monkeypatch.context() as patch:
        patch.setattr(SchedulingRepository, "grant_start", after_saved)
        with pytest.raises(StartIntentSaved):
            await capture_handler(handler)(aid, runtime)
    horizon, = owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()
    runtime = await case.reopen(owned, cold=True, assembly_options={
        "recovery_boundary": RecoveryBoundary.HOST_LOCAL_SETTLED if qualified else RecoveryBoundary.UNCONFIRMED,
        "recovery_max_event_id": lambda: horizon,
    })
    return owned, runtime, aid, handler


class TestPhotoHandler:
    async def test_normal_path_registers_output_and_succeeds(self, tmp_path: Path):
        owned, runtime, aid, handler = await _handler_world(tmp_path, "photo")
        result_case_for().results.delegate = ResultsDouble({aid: (_entry("shot-1", kind=ResultFileKind.PHOTO),)})
        try:
            await capture_handler(handler)(aid, runtime)
            assert _value(owned, "SELECT status FROM actions WHERE id=?", aid) == (3,)
            output = _value(owned,
                "SELECT o.kind,o.device_file_id,f.completion_state,f.size_bytes,f.source_action_id"
                " FROM outputs o JOIN device_files f ON f.id=o.device_file_id WHERE o.source_action_id=?", aid)
            assert output == (1, 1, 3, 4096, aid)
            assert _value(owned, "SELECT COUNT(*) FROM operation_attempts") == (2,)
            activity = _value(owned, "SELECT activity_state,occupancy_state,dispatch_state FROM device_activities WHERE action_id=?", aid)
            assert activity == (3, 2, 3)
            await capture_handler(handler)(aid, runtime)
            assert _value(owned, "SELECT COUNT(*) FROM history_events WHERE event_type=13 AND json_extract(body_json,'$.reason')=3") == (1,)
        finally:
            owned.connection.close()


    async def test_failed_call_keeps_files_and_fails_action(self, tmp_path: Path):
        owned, runtime, aid, handler = await _handler_world(tmp_path, "photo")
        case = result_case_for()
        actual = case.driver.control.return_value.outcome
        case.driver.control.return_value = DeviceCallResult.from_outcome(replace(actual,
            status=AttemptStatus.FAILED, error=ErrorValue("device_error", "device")))
        case.results.delegate = ResultsDouble({aid: (_entry("shot-1", kind=ResultFileKind.PHOTO),)})
        try:
            await capture_handler(handler)(aid, runtime)
            assert _value(owned, "SELECT status FROM actions WHERE id=?", aid) == (4,)
            row = _value(owned, "SELECT error_code,error_details_json FROM actions WHERE id=?", aid)
            assert row[0] == 13
            assert _value(owned, "SELECT completion_state FROM device_files WHERE observer_action_id=?", aid) == (3,)
            assert _value(owned, "SELECT COUNT(*) FROM outputs") == (1,)
        finally:
            owned.connection.close()


    async def test_incomplete_listing_waits_then_recovers(self, tmp_path: Path):
        owned, runtime, aid, handler = await _handler_world(tmp_path, "photo")
        results = ResultsDouble({}, set_finalized=False)
        case = result_case_for()
        case.results.delegate = results
        try:
            await capture_handler(handler)(aid, runtime)
            assert _value(owned, "SELECT status FROM actions WHERE id=?", aid) == (2,)
            assert _value(owned, "SELECT COUNT(*) FROM operation_attempts") == (2,)
            results.files_by_action[aid] = (_entry("shot-1", kind=ResultFileKind.PHOTO),)
            await capture_handler(handler)(aid, runtime)
            assert _value(owned, "SELECT status FROM actions WHERE id=?", aid) == (2,)
            assert _value(owned, "SELECT COUNT(*) FROM operation_attempts") == (2,)
            case.clock.monotonic += 3_000_000_000
            results.set_finalized = True
            await capture_handler(handler)(aid, runtime)
            assert _value(owned, "SELECT status FROM actions WHERE id=?", aid) == (3,)
            assert _value(owned, "SELECT COUNT(*) FROM operation_attempts") == (3,)
            await capture_handler(handler)(aid, runtime)
            assert _value(owned, "SELECT COUNT(*) FROM outputs") == (1,)
        finally:
            owned.connection.close()


    async def test_invalid_observation_settles_start_and_fails_action(self, tmp_path: Path):
        owned, runtime, aid, handler = await _handler_world(tmp_path, "photo")
        case = result_case_for()
        actual = case.driver.control.return_value.outcome
        observation, = actual.observations
        case.driver.control.return_value = DeviceCallResult.from_outcome(replace(actual,
            observations=(replace(observation, data={"activity_id": "99"}),)))
        case.results.delegate = ResultsDouble({aid: (_entry("shot-1", kind=ResultFileKind.PHOTO),)})
        try:
            await capture_handler(handler)(aid, runtime)
            start_run = _value(owned, "SELECT status,error_json FROM operation_runs WHERE responsibility_key=?", f"start/{aid}")
            assert start_run[0] == 4
            assert start_run[1] is not None
            attempt = _value(owned, "SELECT a.status FROM operation_attempts a JOIN operation_runs r ON a.run_id=r.id WHERE r.responsibility_key=?", f"start/{aid}")
            assert attempt == (3,)
            assert _value(owned, "SELECT status FROM actions WHERE id=?", aid) == (4,)
            assert _value(owned, "SELECT COUNT(*) FROM outputs") == (1,)
        finally:
            owned.connection.close()


    async def test_open_start_with_outputs_recovers_success(self, tmp_path: Path, monkeypatch):
        owned, runtime, aid, handler = await _interrupted_start_world(tmp_path, "photo", monkeypatch)
        result_case_for().results.delegate = ResultsDouble({aid: (_entry("shot-1", kind=ResultFileKind.PHOTO),)})
        try:
            await capture_handler(handler)(aid, runtime)
            assert _value(owned, "SELECT status FROM actions WHERE id=?", aid) == (3,)
            assert _value(owned, "SELECT COUNT(*) FROM outputs") == (1,)
            assert _value(owned, "SELECT status FROM operation_runs WHERE responsibility_key=?", f"start/{aid}") == (3,)
        finally:
            owned.connection.close()


    async def test_open_start_without_outputs_keeps_verifying(self, tmp_path: Path, monkeypatch):
        owned, runtime, aid, handler = await _interrupted_start_world(tmp_path, "photo", monkeypatch)
        result_case_for().results.delegate = ResultsDouble({}, set_finalized=False)
        try:
            await capture_handler(handler)(aid, runtime)
            assert _value(owned, "SELECT status FROM actions WHERE id=?", aid) == (2,)
            start_run = _value(owned, "SELECT status FROM operation_runs WHERE responsibility_key=?", f"start/{aid}")
            assert start_run == (2,)
        finally:
            owned.connection.close()



class TestRecordHandler:
    async def test_start_then_stopped_tail_finishes(self, tmp_path: Path):
        owned, runtime, aid, handler = await _handler_world(tmp_path, "record")
        case = result_case_for()
        try:
            await capture_handler(handler)(aid, runtime)
            assert _value(owned, "SELECT COUNT(*) FROM operation_attempts a JOIN operation_runs r ON a.run_id=r.id WHERE r.responsibility_key=?", f"start/{aid}") == (1,)
            assert _value(owned, "SELECT status FROM actions WHERE id=?", aid) == (2,)
            case.clock.wall = _NOW + 60_000_000
            case.clock.monotonic = 65_000_000_000
            case.results.delegate = ResultsDouble({aid: (_entry("clip-1"),)})
            await capture_handler(handler)(aid, runtime)
            assert _value(owned, "SELECT status FROM actions WHERE id=?", aid) == (3,)
            output = _value(owned, "SELECT kind,device_file_id FROM outputs WHERE source_action_id=?", aid)
            assert output == (1, 1)
        finally:
            owned.connection.close()


    async def test_invalid_stop_observation_settles_stop_attempt(self, tmp_path: Path):
        owned, runtime, aid, handler = await _handler_world(tmp_path, "record")
        case = result_case_for()
        try:
            await capture_handler(handler)(aid, runtime)
            case.clock.wall = _NOW + 60_000_000
            case.clock.monotonic = 65_000_000_000
            actual = case.stopper.stop.return_value.outcome
            observation, = actual.observations
            case.stopper.stop.return_value = DeviceCallResult.from_outcome(replace(actual,
                observations=(replace(observation, data={"activity_id": "99"}),)))
            await capture_handler(handler)(aid, runtime)
            stop_run = _value(owned, "SELECT status,error_json FROM operation_runs WHERE responsibility_key=?", f"stop/{aid}")
            assert json.loads(stop_run[1])["code"] == "invalid_device_result"
            assert stop_run[0] == 4
            assert stop_run[1] is not None
            attempt = _value(owned, "SELECT a.status FROM operation_attempts a JOIN operation_runs r ON a.run_id=r.id WHERE r.responsibility_key=?", f"stop/{aid}")
            assert attempt == (3,)
        finally:
            owned.connection.close()


    async def test_processing_pending_keeps_action_running(self, tmp_path: Path):
        owned, runtime, aid, handler = await _handler_world(tmp_path, "record")
        result_case_for().results.delegate = ResultsDouble({aid: (_entry("clip-1"),)})
        try:
            await capture_handler(handler)(aid, runtime)
            assert _value(owned, "SELECT status FROM actions WHERE id=?", aid) == (2,)
            assert _value(owned, "SELECT COUNT(*) FROM outputs") == (0,)
        finally:
            owned.connection.close()


    async def test_open_start_without_sent_at_fails_unconfirmed(self, tmp_path: Path, monkeypatch):
        owned, runtime, aid, handler = await _interrupted_start_world(tmp_path, "record", monkeypatch, qualified=True)
        try:
            assert _value(owned, "SELECT COUNT(*) FROM operation_attempts") == (1,)
            await capture_handler(handler)(aid, runtime)
            assert _value(owned, "SELECT status FROM actions WHERE id=?", aid) == (4,)
            row = _value(owned, "SELECT error_code,error_details_json FROM actions WHERE id=?", aid)
            assert row[0] == 12
            assert json.loads(row[1])["reason"] == "start_unknown"
            assert _value(owned, "SELECT status FROM operation_runs WHERE responsibility_key=?", f"start/{aid}") == (6,)
            activity = _value(owned, "SELECT occupancy_state FROM device_activities WHERE action_id=?", aid)
            assert activity == (1,)
            assert result_case_for().driver.control.await_count == 0
        finally:
            owned.connection.close()



class TestTimelapseHandler:
    async def test_send_wait_then_finish(self, tmp_path: Path):
        owned, runtime, aid, handler = await _handler_world(tmp_path, "timelapse")
        case = result_case_for()
        case.results.delegate = ResultsDouble({aid: (_entry("sequence-1"),)})
        try:
            await capture_handler(handler)(aid, runtime)
            assert _value(owned, "SELECT expected_check_at FROM device_activities WHERE action_id=?", aid) == (_NOW + 600_000_000,)
            assert _value(owned, "SELECT status FROM actions WHERE id=?", aid) == (2,)
            case.clock.wall = _NOW + 700_000_000
            late = await case.reopen(owned, cold=True)
            await capture_handler(handler)(aid, late)
            assert _value(owned, "SELECT status FROM actions WHERE id=?", aid) == (3,)
            assert _value(owned, "SELECT COUNT(*) FROM outputs") == (1,)
        finally:
            owned.connection.close()


    async def test_open_start_without_sent_at_fails_unconfirmed(self, tmp_path: Path, monkeypatch):
        owned, runtime, aid, handler = await _interrupted_start_world(tmp_path, "timelapse", monkeypatch, qualified=True)
        try:
            await capture_handler(handler)(aid, runtime)
            assert _value(owned, "SELECT status FROM actions WHERE id=?", aid) == (4,)
            row = _value(owned, "SELECT error_code,error_details_json FROM actions WHERE id=?", aid)
            assert row[0] == 12
            assert json.loads(row[1])["reason"] == "start_unknown"
            assert _value(owned, "SELECT status FROM operation_runs WHERE responsibility_key=?", f"start/{aid}") == (6,)
            activity = _value(owned, "SELECT occupancy_state FROM device_activities WHERE action_id=?", aid)
            assert activity == (1,)
        finally:
            owned.connection.close()



class TestDispatchLoop:
    @pytest.mark.parametrize("action_id,action_type", [(11, 1), (12, 2), (13, 3)])
    @pytest.mark.parametrize("canceled", [False, True])
    async def test_unstarted_action_finishes_without_device_work(self, tmp_path, action_id, action_type, canceled):
        from .test_natural_wait_time_eligibility import _request_cancel
        consumer, handler = {1: ("photo", "camera_take_photo"), 2: ("record", "camera_record"),
                             3: ("timelapse", "camera_timelapse")}[action_type]
        case = result_case_for()
        owned, runtime, aid, _ = await case.world(tmp_path, consumer, start=False, max_delay_ms=1000)
        files = ResultsDouble({})
        case.results.delegate = files
        try:
            if canceled:
                await _request_cancel(runtime, aid, [case.clock.wall])
            case.clock.wall = _NOW + 1_000_001
            await capture_handler(handler)(aid, runtime)
            assert _value(owned, "SELECT COUNT(*) FROM actions WHERE type=?", action_type) == (1,)
            assert not await runtime.database.read(lambda current: current.connection.in_transaction)
            assert _value(owned, "SELECT status,execution_started,cancel_requested FROM actions WHERE id=?", aid) == (6 if canceled else 5, 1, int(canceled))
            assert _value(owned, "SELECT dispatch_state,activity_state,occupancy_state FROM device_activities WHERE action_id=?", aid) == (1, 1, 2)
            assert _value(owned, "SELECT COUNT(*) FROM operation_attempts") == (0,)
            assert case.driver.control.await_count == 0
            assert files.calls == []
        finally:
            owned.connection.close()


    async def test_dispatch_routes_all_capabilities_per_plan(self, tmp_path: Path):
        from datetime import datetime, timezone
        from types import SimpleNamespace
        from camctl.bootstrap.flows import capture_flow
        from camctl.contracts.pages import Page
        from camctl.devices.directory import DirectoryRead
        from .result_consumer_fixtures import ResultCatalog, returned
        from .test_baseline_prepare import Directory

        class Catalog(ResultCatalog):
            def device_exists(self, device_id):
                return device_id in ("cam-1", "cam-2", "cam-3")
            def driver_id(self, device_id):
                return "camctl-adb" if self.device_exists(device_id) else None
            def parameter_definition(self, device_id, action_type, parameter_type):
                return super().parameter_definition("cam-1", action_type, parameter_type)
        instant = datetime.fromtimestamp(_NOW // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        extra = [{"name": name, "type": action_type, "device_id": device_id,
                  "scheduled_at": instant, "params": {"type": parameter_type}, "policy": {"max_delay_ms": 5000}}
                 for name, action_type, device_id, parameter_type in (
                     ("录像", "camera_record", "cam-2", "video"),
                     ("延时", "camera_timelapse", "cam-3", "timelapse"))]
        case = result_case_for()
        owned, runtime, aid, _ = await case.world(tmp_path, "photo", start=False, catalog=Catalog(),
            assembly_options={
                "devices": {name: {"driver": "camctl-adb"} for name in ("cam-1", "cam-2", "cam-3")},
                "directory": Directory([lambda request: DirectoryRead(None, None, Page((), None)) for _ in range(3)]),
            })
        from camctl.acceptance.input import ParsedInput
        from camctl.acceptance.service import AcceptanceContext, CommandMode, accept_input
        from camctl.contracts.values import new_operation_key
        from camctl.persistence.repositories.acceptance import AcceptanceRepository
        for request_id, action in enumerate(extra, 2):
            accepted = await accept_input(ParsedInput(f"capability-{request_id}.json", {
                "request_id": str(request_id), "created_at": instant,
                "name": action["name"], "actions": [action]}),
                AcceptanceContext(CommandMode.RUN, Catalog(), AcceptanceRepository(),
                    clock=SimpleNamespace(utc_micros=case.clock.wall_us)),
                new_operation_key(), runtime.database)
            assert accepted.plan_id is not None, accepted
        async def control(request):
            observation = {"take_photo": "photo_taken", "start_recording": "start_confirmed",
                           "start_timelapse": "timelapse_sent"}[request.operation]
            return DeviceCallResult.from_outcome(returned("operation", observation, int(request.ticket.target_id)))
        case.driver.control.side_effect = control
        case.results.delegate = ResultsDouble({aid: (_entry("shot-1", kind=ResultFileKind.PHOTO),)})
        flow = capture_flow(case.factory)
        context = SimpleNamespace(database=runtime.database, clock=SimpleNamespace(utc_micros=case.clock.wall_us))
        try:
            await flow(context)
            await flow.settle()
            flow.check_completed()
            assert sorted((call.args[0].operation, call.args[0].binding.device_id) for call in case.driver.control.await_args_list) == [
                ("start_recording", "cam-2"), ("start_timelapse", "cam-3"), ("take_photo", "cam-1")]
            assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE kind=1 ORDER BY action_id").fetchall() == [(1,), (1,), (1,)]
            assert _value(owned, "SELECT status FROM actions WHERE id=?", aid) == (3,)
        finally:
            try:
                flow.stop_new_work()
                await flow.settle()
                flow.check_completed()
            finally:
                owned.connection.close()



class TestInterruptionRecovery:
    async def test_fault_at_file_registration_rolls_back_and_recovers(
            self, tmp_path: Path, result_case, capture_resources):
        from ..support.capture import capture_runtime
        from camctl.contracts.values import ConsistencyError
        from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
        from camctl.persistence.transaction import saved_transaction_events
        from .test_result_consumer_saves import _actual, _assert_actual_saved
        from .test_result_file_recovery import FileObservationSpy, _fresh_runtime, _result_driver

        owned, runtime, action_id, handler = await result_case.world(
            tmp_path, "photo", result_page_contract=False)
        actual = _actual(action_id, with_files=True, complete=True)
        actual.observations[0].data["entries"][0]["kind"] = "photo"
        results_driver = _result_driver(actual)
        spy = FileObservationSpy("projection", failures=2)
        runtime.capture = spy
        original_wall = result_case.clock.wall
        original_monotonic = result_case.clock.monotonic
        path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
        metadata = owned.metadata
        try:
            with pytest.raises(ConsistencyError):
                await capture_handler(handler)(action_id, runtime)
            assert len(spy.requests) == 2 and spy.requests[1] == spy.requests[0]
            request, file_key = spy.requests[0]
            assert request.occurred_at == original_wall and request.locator == {"path": "/DCIM/original.mp4"}
            assert saved_transaction_events(owned.connection, file_key) is None
            assert _value(owned, "SELECT COUNT(*) FROM device_files") == (0,)
            assert _value(owned, "SELECT status FROM actions WHERE id=?", action_id) == (2,)
            before_attempts = owned.connection.execute(
                "SELECT id,run_id,attempt_no FROM operation_attempts ORDER BY id").fetchall()
            assert len(before_attempts) == 2
            original_result = before_attempts[-1]

            owned.connection.close()
            owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
            assert owned.metadata == metadata
            recovery = (await _fresh_runtime(owned, runtime))
            recovery.capture = spy
            result_case.clock.wall = original_wall + 5_000_000

            await capture_handler(handler)(action_id, recovery)
            assert _value(owned, "SELECT status FROM actions WHERE id=?", action_id) == (2,)
            _assert_actual_saved(owned, action_id, actual)
            assert owned.connection.execute(
                "SELECT id,run_id,attempt_no FROM operation_attempts ORDER BY id").fetchall() == before_attempts
            assert len(spy.requests) == 3 and all(value == (request, file_key) for value in spy.requests)
            # 原读取错误和无集合保证的输入不能证明成功；下一轮取得
            # 明确的完整集合，再使用原文件事实收尾。
            recovered_file = replace(_entry("original", kind=ResultFileKind.PHOTO, size=41),
                                     locator={"path": "/DCIM/original.mp4"})
            result_case.results.delegate = ResultsDouble({action_id: (recovered_file,)})
            result_case.clock.monotonic = original_monotonic + 5_000_000_000
            await result_case.close()
            database = await capture_resources.restart_database(owned)
            recovery = await capture_runtime(database, driver=result_case.driver,
                evidence=_PAGE_EVIDENCE, results=result_case.results,
                wall_us=result_case.clock.wall_us, monotonic_ns=result_case.clock.monotonic_ns,
                check_config=result_case.options['check_config'])
            await capture_handler(handler)(action_id, recovery)
            result_case.clock.monotonic += 3_000_000_000
            await capture_handler(handler)(action_id, recovery)
            assert _value(owned, "SELECT status FROM actions WHERE id=?", action_id) == (3,)
            attempts = owned.connection.execute(
                "SELECT id,run_id,attempt_no FROM operation_attempts ORDER BY id").fetchall()
            assert attempts[:-1] == before_attempts
            assert attempts[-1][1:] == (original_result[1], original_result[2] + 1)
            saved_file = saved_transaction_events(owned.connection, file_key)
            assert saved_file is not None and saved_file[0]["occurred_at"] == original_wall
            assert owned.connection.execute('SELECT e.occurred_at FROM operation_attempts t'
                ' JOIN history_events e ON e.id=t.result_event_id WHERE t.id=?',
                (original_result[0],)).fetchone() == (original_wall,)
            _assert_actual_saved(owned, action_id, actual, attempts_used=2)
            results_driver.list_results.assert_awaited_once()
            result_case.driver.control.assert_awaited_once()
            assert _value(owned, "SELECT source_action_id,presence_state,completion_state,size_bytes"
                          " FROM device_files") == (action_id, 2, 3, 41)
            assert _value(owned, "SELECT COUNT(*) FROM outputs") == (1,)
        finally:
            owned.connection.close()

    async def test_history_bytes_stable_after_terminal_redispatch(self, tmp_path: Path):
        owned, runtime, aid, handler = await _handler_world(tmp_path, "photo")
        result_case_for().results.delegate = ResultsDouble({aid: (_entry("shot-1", kind=ResultFileKind.PHOTO),)})
        try:
            await capture_handler(handler)(aid, runtime)
            assert _value(owned, "SELECT status FROM actions WHERE id=?", aid) == (3,)
            before = tuple(owned.connection.execute("SELECT id,body_json FROM history_events ORDER BY id").fetchall())
            await capture_handler(handler)(aid, runtime)
            after = tuple(owned.connection.execute("SELECT id,body_json FROM history_events ORDER BY id").fetchall())
            assert after == before
        finally:
            owned.connection.close()

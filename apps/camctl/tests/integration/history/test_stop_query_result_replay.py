"""停止与查询结果事务的历史三路径核验。

同一结果事务前后的完整历史分别经直接回放、事务前快照正向和当
前投影逆向恢复动作、停止／查询流程与原活动的全部行，与各边界当
时的实际投影一致；事务前的恢复不含后来的可靠观察，事务后的恢复
携带新的可靠事实。世界经真实初始化、受理与 run 会话构建，被恢复
的行都有完整历史。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.flows import cancel_flow, capture_flow
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.capture.handlers import CaptureRuntime, SessionRecordingState
from camctl.capture.result_inputs import RESULT_FILES_CONTRACT, RESULT_PAGE_CONTRACT
from camctl.capture.results import FileKind as ResultFileKind
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.contracts.history_values import HistoryBoundary, INITIAL_BOUNDARY
from camctl.devices.evidence import (
    DeviceObservation, EvidenceContract, EvidenceRegistry,
)
from camctl.devices.ports import DeviceCallResult
from camctl.history.decoding import decode_event_row
from camctl.history.events import branch_of, event_type_name
from camctl.history.replay import EntityImage, RestoreSeed, apply_reverse, restore
from camctl.history.validators import ValidatedEvent
from camctl.operations.attempts import RetryWaitGate
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.repositories.cancellation import register_cancellation_guards
from camctl.persistence.repositories.capture import (
    CaptureRepository, register_capture_guards,
)
from camctl.persistence.repositories.operations import (
    OperationRepository, register_operation_guards,
)
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.repositories.scheduling import SchedulingRepository
from camctl.persistence.repositories.timelapse import (
    TimelapseRepository, register_timelapse_guards,
)
from camctl.persistence.repositories.history import HistoryRepository
from camctl.reporting.policy import register_report_guards, register_sync_guard
from camctl.scheduling.rules import LaunchWindow

from ..bootstrap.test_recording_stop import (
    _RecordCatalog, _await_query, _cancel, _config_for, _future_schedule,
    _record_plan, _submit_plan,
)
from ..bootstrap.test_residual_winddown import (
    _ControlDouble, _StopDouble, _WinddownCatalog, _exhaust_original_stop,
    _photo_action as _winddown_photo, _plan as _winddown_plan,
    _record_action as _winddown_record, _run_session as _winddown_session,
    _winddown_factory,
)
from ..bootstrap.test_run_dispatch import _ActivityDriver
from ..capture.test_capture_contract import ResultsDouble, _entry

pytestmark = pytest.mark.asyncio

register_operation_guards()
register_capture_guards()
register_timelapse_guards()
register_report_guards()
register_sync_guard()
register_outputs_guards()
register_cancellation_guards()

_REPLAY_EVIDENCE = EvidenceRegistry((
    EvidenceContract("operation_returned", 1, "control", frozenset()),
    EvidenceContract("start_confirmed", 1, "control", frozenset({"activity_id"}),
                     identity_field="activity_id"),
    EvidenceContract("stop_returned", 1, "stop", frozenset()),
    EvidenceContract("stop_confirmed", 1, "stop", frozenset({"activity_id"}),
                     identity_field="activity_id"),
    EvidenceContract("query_returned", 1, "query", frozenset()),
    EvidenceContract("activity_status", 1, "query", frozenset({"activity_id"}),
                     identity_field="activity_id"),
    RESULT_FILES_CONTRACT,
    RESULT_PAGE_CONTRACT,
))

_META = {"id", "created_event_id", "last_event_id", "change_count"}


class BlockingStop:
    """停止端口替身：在途暂停供测试读取事务前状态。

    confirmed 为真时返回可靠停止确认观察，否则只表达命令已发送。
    """

    def __init__(self, *, confirmed: bool) -> None:
        self.confirmed = confirmed
        self.calls: list[str] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def stop(self, request) -> DeviceCallResult:
        self.calls.append(request.operation)
        self.entered.set()
        await self.release.wait()
        if not self.confirmed:
            return DeviceCallResult(observations=(), error=None)
        return DeviceCallResult(
            observations=(DeviceObservation(
                type="stop_confirmed", version=1, data={"activity_id": "1"}),),
            error=None,
        )


class BlockingIdleQuery:
    """查询端口替身：在途暂停后可靠确认设备空闲。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def query_state(self, request) -> DeviceCallResult:
        self.calls.append(request.operation)
        self.entered.set()
        await self.release.wait()
        return DeviceCallResult(observations=(), error=None)


class PreflightThenBlockingIdleQuery:
    """查询端口替身：执行前检查报告残留活动，确认查询在途暂停后空闲。

    第一次调用（执行前检查）立即返回活动 1 仍在录的观察；第二次
    调用（残留停止核实）阻塞至放行后返回可靠空闲。
    """

    def __init__(self) -> None:
        self.calls: list[object] = []
        self.confirm_entered = asyncio.Event()
        self.release = asyncio.Event()

    async def query_state(self, request) -> DeviceCallResult:
        self.calls.append(request)
        if len(self.calls) == 1:
            return DeviceCallResult(
                observations=(DeviceObservation(
                    type="activity_status", version=1,
                    data={"activity_id": "1"}),),
                error=None)
        self.confirm_entered.set()
        await self.release.wait()
        return DeviceCallResult(observations=(), error=None)


def _replay_factory(driver, stopper, results, clock, *, state_query=None):
    """会话共享锚点表与重试等待门槛：推进循环每轮重建运行时。"""
    anchors: dict[int, tuple[int, int]] = {}
    retry_gate = RetryWaitGate()

    def build(owned, device_id: str) -> CaptureRuntime:
        runtime = CaptureRuntime(
            owned=owned,
            scheduling=SchedulingRepository(),
            operations=OperationRepository(),
            capture=CaptureRepository(),
            timelapse=TimelapseRepository(),
            driver=driver,
            results=results,
            evidence=_REPLAY_EVIDENCE,
            wall_us=lambda: int(time.time() * 1_000_000),
            monotonic_ns=lambda: clock["ns"],
            window_of=lambda action: LaunchWindow(
                scheduled_at=action["scheduled_at"],
                window_end=action["scheduled_at"] + action["max_delay_ms"] * 1000),
            wait_config=lambda params: CaptureWaitConfig(
                target_duration_ms=600_000, driver_margin_ms=0),
            stopper=stopper,
            retry_gate=retry_gate,
            state_query=state_query,
        )
        runtime.recording_state = SessionRecordingState(runtime, anchors)
        return runtime

    return build


async def _replay_session(tmp_path, name, *, stopper, state_query=None):
    """真实初始化、受理与 run 会话世界；全部行经正式事务产生。"""
    home = tmp_path / name
    home.mkdir()
    cfg = _config_for(home)
    assert initialize_state(
        cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
    await _submit_plan(
        tmp_path, cfg, _RecordCatalog(), _record_plan("1", _future_schedule(2)))
    db = Path(cfg.paths.state_db)
    deps = build_runtime(CommandMode.RUN, cfg, catalog=_RecordCatalog())
    driver = _ActivityDriver()
    clock = {"ns": time.monotonic_ns()}
    task = asyncio.create_task(execute_command(
        deps, None,
        flows={
            "scheduling": capture_flow(_replay_factory(
                driver, stopper, ResultsDouble({}, set_finalized=False), clock,
                state_query=state_query)),
            "cancel": cancel_flow(ready=Path(cfg.paths.ready),
                                  processing=Path(cfg.paths.processing)),
        },
        poll_interval_s=0.1,
    ))
    return db, deps, task, clock


def _boundary_at(connection) -> HistoryBoundary:
    row = connection.execute(
        "SELECT id, last_event_id FROM history_transactions"
        " WHERE id = (SELECT MAX(id) FROM history_transactions)").fetchone()
    return HistoryBoundary(row[0], row[1])


def _business_rows(rows) -> dict:
    """剔除行元数据列，只保留与回放可比的业务列。"""
    return {key: {name: value for name, value in facts.items()
                  if name not in _META}
            for key, facts in rows.items()}


def _current_rows(db: Path, connection, entity_id: int) -> dict:
    """经权威归属枚举读取动作对象的全部当前行（读事务内调用）。"""
    return _business_rows(HistoryRepository(db).restore_within(
        connection, "action", entity_id, _boundary_at(connection)))


def _snapshot_at(db: Path, entity_id: int):
    """读事务内同时读取当前历史边界与行投影，保证二者一致。"""
    connection = sqlite3.connect(db)
    try:
        connection.execute("BEGIN")
        boundary = _boundary_at(connection)
        rows = _current_rows(db, connection, entity_id)
        connection.execute("COMMIT")
        return boundary, rows
    finally:
        connection.close()


def _events_of(connection, entity_type: int, entity_id: int):
    rows = connection.execute(
        "SELECT h.id, h.transaction_id, h.event_type, h.event_version,"
        " h.occurred_at, h.clock_status, h.change_seq, h.body_json"
        " FROM history_events h"
        " JOIN entity_event_links l ON l.event_id=h.id"
        " WHERE l.entity_type=? AND l.entity_id=? ORDER BY h.id",
        (entity_type, entity_id)).fetchall()
    reference = (entity_type, entity_id)
    events = []
    for raw in rows:
        envelope = decode_event_row(raw)
        events.append(ValidatedEvent(
            envelope=envelope,
            event_name=event_type_name(envelope.event_type),
            branch_name=branch_of(envelope.event_type, envelope.reason)[0],
            references=(reference,),
            row_owners={(row.table, row.row_id): reference
                        for row in envelope.rows},
        ))
    return events


def _run_facts(rows, responsibility: str):
    for (table, row_id), facts in rows.items():
        if (table == "operation_runs"
                and facts["responsibility_key"] == responsibility):
            return row_id, facts
    raise AssertionError(f"缺少流程行: {responsibility}")


def _attempt_facts(rows, run_id: int) -> dict:
    found = [facts for (table, _), facts in rows.items()
             if table == "operation_attempts" and facts["run_id"] == run_id]
    assert len(found) == 1, f"流程 {run_id} 应恰有一个尝试"
    return found[0]


def _assert_three_paths(db: Path, entity_id: int, checkpoints) -> None:
    """各检查点三路径恢复一致：直接回放、前一快照正向、当前投影逆向。"""
    connection = sqlite3.connect(db)
    try:
        connection.execute("BEGIN")
        events = _events_of(connection, 1, entity_id)
        latest = _boundary_at(connection)
        current_rows = _current_rows(db, connection, entity_id)
        connection.execute("COMMIT")
    finally:
        connection.close()
    for index, (boundary, rows) in enumerate(checkpoints):
        upto = [event for event in events
                if event.envelope.event_id <= boundary.last_event_id]
        initial = restore(RestoreSeed(
            EntityImage(1, entity_id, False, {}, 0, 0), INITIAL_BOUNDARY),
            upto, boundary)
        assert initial.rows == rows, f"直接回放与检查点 {index} 投影不一致"
        if index > 0:
            previous_boundary, previous_rows = checkpoints[index - 1]
            forward = restore(RestoreSeed(EntityImage(
                1, entity_id, True, previous_rows,
                previous_boundary.last_event_id, initial.change_count),
                previous_boundary), upto, boundary)
            assert forward.rows == rows, f"快照正向与检查点 {index} 投影不一致"
        current = EntityImage(
            1, entity_id, True, current_rows, latest.last_event_id,
            initial.change_count)
        remaining = [event for event in events
                     if boundary.last_event_id < event.envelope.event_id
                     <= latest.last_event_id]
        for event in reversed(remaining):
            current = apply_reverse(current, event)
        assert current.rows == rows, f"投影逆向与检查点 {index} 投影不一致"


async def test_stop_result_transaction_three_paths(tmp_path):
    """停止确认事务：三路径恢复一致，事务前无停止观察。"""
    stopper = BlockingStop(confirmed=True)
    db, deps, task, clock = await _replay_session(
        tmp_path, "stop-replay", stopper=stopper)
    try:
        await _await_query(
            db, "SELECT activity_state FROM device_activities WHERE id = 1", (2,))
        base = _snapshot_at(db, 1)
        clock["ns"] += 6_000_000_001
        await asyncio.wait_for(stopper.entered.wait(), 10)
        # 停止调用在途：意图已提交、结果未保存，即事务前状态。
        before_boundary, before = _snapshot_at(db, 1)
        stop_run, stop_before = _run_facts(before, "stop/1")
        assert stop_before["status"] == 2
        assert _attempt_facts(before, stop_run)["status"] == 1
        assert before[("device_activities", 1)]["activity_state"] == 2

        stopper.release.set()
        await _await_query(
            db, "SELECT status, attempts_used FROM operation_runs"
            " WHERE responsibility_key = 'stop/1'", (3, 1))
        after_boundary, after = _snapshot_at(db, 1)
        stop_run, stop_after = _run_facts(after, "stop/1")
        attempt = _attempt_facts(after, stop_run)
        assert (attempt["status"], attempt["effect_state"]) == (2, 3)
        assert attempt["result_json"] is not None
        assert after[("device_activities", 1)]["activity_state"] == 3

        _assert_three_paths(db, 1, [
            base, (before_boundary, before), (after_boundary, after)])
    finally:
        await _cancel(task)
    close_runtime(deps)


async def test_query_conclude_transaction_three_paths(tmp_path):
    """停止核实查询收场事务：三路径恢复一致，事务前仍在等待确认。"""
    stopper = BlockingStop(confirmed=False)
    query = BlockingIdleQuery()
    db, deps, task, clock = await _replay_session(
        tmp_path, "query-replay", stopper=stopper, state_query=query)
    try:
        await _await_query(
            db, "SELECT activity_state FROM device_activities WHERE id = 1", (2,))
        base = _snapshot_at(db, 1)
        clock["ns"] += 6_000_000_001
        await asyncio.wait_for(stopper.entered.wait(), 10)
        stopper.release.set()
        # 停止发送未确认后，停止核实查询接管并在途暂停。
        await asyncio.wait_for(query.entered.wait(), 10)
        before_boundary, before = _snapshot_at(db, 1)
        stop_run, stop_before = _run_facts(before, "stop/1")
        assert stop_before["status"] == 2
        assert _attempt_facts(before, stop_run)["status"] == 2
        confirmation_run, confirmation = _run_facts(before, "query/stop/1/1")
        assert confirmation["status"] == 2
        confirmation_attempt = _attempt_facts(before, confirmation_run)
        assert confirmation_attempt["status"] == 1
        assert confirmation_attempt["result_json"] is None

        query.release.set()
        await _await_query(
            db, "SELECT status FROM operation_runs"
            " WHERE responsibility_key = 'stop/1'", (3,))
        after_boundary, after = _snapshot_at(db, 1)
        assert _run_facts(after, "stop/1")[1]["status"] == 3
        concluded_run, concluded = _run_facts(after, "query/stop/1/1")
        assert concluded["status"] == 3
        assert _attempt_facts(after, concluded_run)["result_json"] is not None
        assert after[("device_activities", 1)]["activity_state"] == 3

        _assert_three_paths(db, 1, [
            base, (before_boundary, before), (after_boundary, after)])
    finally:
        await _cancel(task)
    close_runtime(deps)


async def test_residual_query_conclude_three_paths(tmp_path):
    """残留确认查询收场事务：双实体三路径恢复，归属与原动作终态保持。

    原录像动作 1 停止预算耗尽后失败终态化而活动占用保留；触发照片
    动作 2 经执行前检查发现残留，建立独立收场流程停止未获确认，再
    由确认查询可靠空闲后同事务收场。原动作行与触发动作行分别回放，
    事务前的恢复不含空闲确认与活动结束，事务后二者齐备且原动作终
    态保持、收场责任仍归属触发动作。
    """
    home = tmp_path / "residual-replay"
    home.mkdir()
    cfg = _config_for(home)
    assert initialize_state(
        cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
    await _submit_plan(
        tmp_path, cfg, _WinddownCatalog(),
        _winddown_plan("1",
                       _winddown_record("residue", _future_schedule(2)),
                       _winddown_photo("followup", _future_schedule(3))))
    db = Path(cfg.paths.state_db)
    deps = build_runtime(CommandMode.RUN, cfg, catalog=_WinddownCatalog())
    driver = _ControlDouble()
    stopper = _StopDouble(failures=3, unconfirmed=1)
    query = PreflightThenBlockingIdleQuery()
    clock = {"ns": time.monotonic_ns()}
    files = {2: (_entry("photo-1", kind=ResultFileKind.PHOTO),)}
    task = _winddown_session(
        deps, cfg, _winddown_factory(driver, stopper, query, clock, files))
    try:
        await _exhaust_original_stop(db, clock)
        original_base = _snapshot_at(db, 1)
        trigger_base = _snapshot_at(db, 2)
        # 收场首停发出未确认后，确认查询接管并在途暂停。
        await asyncio.wait_for(query.confirm_entered.wait(), 15)
        original_before_boundary, original_before = _snapshot_at(db, 1)
        trigger_before_boundary, trigger_before = _snapshot_at(db, 2)
        # A／B 归属：停止流程沿原活动归属原动作 1 的历史，责任键与
        # action_id 仍指向触发动作 2；核实查询行归触发动作。
        winddown_run, winddown_before = _run_facts(
            original_before, "followup/2/1")
        assert winddown_before["status"] == 2
        assert winddown_before["action_id"] == 2
        assert _attempt_facts(original_before, winddown_run)["status"] == 2
        confirmation_run, confirmation_before = _run_facts(
            trigger_before, "query/residual/2/1")
        assert confirmation_before["status"] == 2
        assert confirmation_before["action_id"] == 2
        confirmation_attempt = _attempt_facts(trigger_before, confirmation_run)
        assert confirmation_attempt["status"] == 1
        assert confirmation_attempt["result_json"] is None
        assert original_before[("device_activities", 1)]["activity_state"] == 2
        assert original_before[("actions", 1)]["status"] == 4

        query.release.set()
        await _await_query(
            db, "SELECT activity_state, occupancy_state"
            " FROM device_activities WHERE id = 1", (3, 2))
        original_after_boundary, original_after = _snapshot_at(db, 1)
        trigger_after_boundary, trigger_after = _snapshot_at(db, 2)
        _, winddown_after = _run_facts(original_after, "followup/2/1")
        assert winddown_after["status"] == 3
        assert winddown_after["action_id"] == 2
        _, confirmation_after = _run_facts(trigger_after, "query/residual/2/1")
        assert confirmation_after["status"] == 3
        assert _attempt_facts(
            trigger_after, confirmation_run)["result_json"] is not None
        assert original_after[("device_activities", 1)]["activity_state"] == 3
        # 原动作终态保持：收场不改变已保存的失败终态。
        assert original_after[("actions", 1)]["status"] == 4

        _assert_three_paths(db, 1, [
            original_base,
            (original_before_boundary, original_before),
            (original_after_boundary, original_after)])
        _assert_three_paths(db, 2, [
            trigger_base,
            (trigger_before_boundary, trigger_before),
            (trigger_after_boundary, trigger_after)])
    finally:
        await _cancel(task)
    close_runtime(deps)

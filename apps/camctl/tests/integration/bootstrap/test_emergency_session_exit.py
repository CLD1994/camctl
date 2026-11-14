"""会话致命错误收场的应急停止接线：真实发现、停止、补记与诊断。

按[正在录像时的应急停止](../../../../../architecture/session-errors.md)
验证默认会话错误出口：非时钟致命错误时对会话锚点内仍可确认的录
像有限尝试停止，逐个补记 kind=9 流程并把诊断并入原错误。
"""

import asyncio
from decimal import Decimal
import json
import os
from pathlib import Path
import threading

import pytest

from camctl.capture.emergency_flow import EmergencySettlement
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.evidence import DeviceObservation, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult, DriverDeclaration
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..support.capture import capture_database
from ..support.session import open_session_database


pytestmark = pytest.mark.asyncio

_NOW = 1_750_000_000_000_000
_SESSION = "b" * 32


class _StaticClock:
    def __init__(self, micros: int) -> None:
        self._micros = micros

    def utc_micros(self) -> int:
        return self._micros


class _StopPort:
    """受 StopDriver 契约约束的替身：按脚本返回真实 DeviceCallResult。"""

    def __init__(self, declaration: DriverDeclaration, results) -> None:
        self.declaration = declaration
        self._results = list(results)
        self.calls = []

    async def stop(self, request):
        self.calls.append(request)
        index = min(len(self.calls), len(self._results)) - 1
        return self._results[index]


def _confirmed() -> DeviceCallResult:
    return DeviceCallResult(
        observations=(DeviceObservation(
            "stop_confirmed", 1, {"activity_id": "1"}),),
        error=None)


def _timeout() -> DeviceCallResult:
    return DeviceCallResult(
        observations=(),
        error={"code": "stop_timeout", "stage": "transport"})


def _declaration(**overrides) -> DriverDeclaration:
    values = dict(control_supported=False, stop_supported=True, query_supported=False,
                  result_supported=False, read_supported=False, digest_supported=False,
                  delete_supported=False)
    values.update(overrides)
    return DriverDeclaration(**values)


def _registry(stop_port) -> DriverRegistry:
    return DriverRegistry((DriverEntry(
        "camctl-adb", stop_port, stop_port.declaration,
        EvidenceRegistry(()), DriverStatus.SOFTWARE_CONTRACT_VERIFIED),))


async def _recording_world(tmp_path: Path):
    """真实受理、授予 START 并保存启动确认，建立在途录像。"""
    from camctl.capture.models import ActivityObservationSave
    from camctl.contracts.values import new_operation_key
    from camctl.devices.bindings import DeviceBinding
    from camctl.devices.ports import ControlRequest
    from camctl.operations.attempts import AttemptConfig, AttemptFinish, RunFinish, RunOutcome
    from camctl.operations.validation import validate_outcome
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.capture import CaptureRepository
    from camctl.persistence.repositories.scheduling import GrantOutcome, GrantRequest, SchedulingRepository
    from camctl.scheduling.rules import LaunchWindow
    from ..capture.result_consumer_fixtures import RESULT_EVIDENCE, consumer_world, returned
    from ..support.database import database_path

    async def no_runtime(owned, *, driver):
        return None

    owned, _, action_id, _ = await consumer_world(
        tmp_path, "record", start=False, runtime_factory=no_runtime)
    assert action_id == 1
    grant = SchedulingRepository().grant_start(GrantRequest(
        "cam-1", action_id, LaunchWindow(_NOW, _NOW + 5_000_000), _NOW,
        AttemptConfig(2, Decimal("10")), _NOW), new_operation_key(), owned)
    assert grant.kind is DbOutcomeKind.COMPLETED, grant.error
    assert grant.value.outcome is GrantOutcome.GRANTED
    ticket = grant.value.ticket
    assert ticket is not None

    class StartPort:
        def __init__(self):
            self.declaration = _declaration(control_supported=True)
            self.calls = []

        async def control(self, request):
            self.calls.append(request)
            return DeviceCallResult.from_outcome(
                returned("operation", "start_confirmed", grant.value.activity_id))

    driver = StartPort()
    response = await driver.control(ControlRequest(
        "start_recording", DeviceBinding("cam-1", "camctl-adb"), {}, ticket, Decimal("10")))
    assert len(driver.calls) == 1
    result = CaptureRepository().finish_start_result(
        AttemptFinish(ticket, validate_outcome(ticket, response.outcome, RESULT_EVIDENCE),
                      _NOW, run_finish=RunFinish(RunOutcome.SUCCEEDED)),
        ActivityObservationSave(action_id, _NOW, sent_at=_NOW, started_at=_NOW,
                                dispatch_state=3, activity_state=2),
        new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert owned.connection.execute(
        "SELECT dispatch_state, activity_state, stop_supported, safe_repeat_stop, started_at"
        " FROM device_activities WHERE action_id = 1").fetchone() == (3, 2, 1, 1, _NOW)
    target = database_path(owned)
    return target, owned


async def _stop_recording(owned):
    """执行停止端口并按正式结果事务保存活动结束。"""
    from camctl.contracts.values import new_operation_key
    from camctl.devices.bindings import DeviceBinding
    from camctl.devices.ports import ControlRequest
    from camctl.operations.attempts import (
        AttemptConfig, AttemptFinish, AttemptIntent, AttemptTarget, OperationKind,
        RunFinish, RunOutcome,
    )
    from camctl.operations.validation import validate_outcome
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.capture import CaptureRepository
    from camctl.persistence.repositories.operations import OperationRepository
    from ..capture.result_consumer_fixtures import RESULT_EVIDENCE, returned

    intent = OperationRepository().begin_attempt(AttemptIntent(
        operation="stop", action_id=1, kind=OperationKind.STOP,
        target=AttemptTarget(activity_id=1), query_purpose=None,
        config=AttemptConfig(2, Decimal("10")), occurred_at=_NOW),
        new_operation_key(), owned)
    assert intent.kind is DbOutcomeKind.COMPLETED, intent.error
    ticket = intent.value.ticket
    assert ticket is not None
    stop = _StopPort(_declaration(), (
        DeviceCallResult.from_outcome(returned("stop", "stop_confirmed", 1)),))
    response = await stop.stop(ControlRequest(
        "stop_recording", DeviceBinding("cam-1", "camctl-adb"), {}, ticket, Decimal("10")))
    assert len(stop.calls) == 1
    saved = CaptureRepository().finish_capture_stop(AttemptFinish(
        ticket, validate_outcome(ticket, response.outcome, RESULT_EVIDENCE), _NOW,
        run_finish=RunFinish(RunOutcome.SUCCEEDED)), new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    assert owned.connection.execute(
        "SELECT activity_state FROM device_activities WHERE id = 1").fetchone() == (3,)


async def _settlement(target: Path, registry, *, anchors=None, maximum: int = 2, session=False):
    return EmergencySettlement(
        session_key=_SESSION,
        anchors=anchors if anchors is not None else {1: (_NOW, _NOW + 1)},
        stop_config={"cam-1": {
            "max_attempts": maximum,
            "timeout_s": Decimal("10"),
            "retry_interval_s": Decimal("0"),
        }},
        registry=registry,
        database=None if session else await capture_database(target),
        wall_us=lambda: _NOW,
    )



async def _settle_in_session(context, settlement):
    settlement.database = context.database
    return await settlement.settle()

def _run_row(owned):
    return owned.connection.execute(
        "SELECT kind, status, attempts_used, max_attempts_used, error_json,"
        " session_key FROM operation_runs WHERE kind = 9").fetchone()


async def test_fatal_exit_stops_recording_once_and_records_stopped(tmp_path):
    target, owned = await _recording_world(tmp_path)
    stop = _StopPort(_declaration(), (_confirmed(),))
    try:
        entries = await (await _settlement(target, _registry(stop))).settle()

        assert len(stop.calls) == 1
        request = stop.calls[0]
        assert request.operation == "stop"
        assert request.binding.device_id == "cam-1"
        assert request.timeout_s == Decimal("10")
        run = _run_row(owned)
        assert run[:4] == (9, 3, 1, 2)
        assert run[4] is None
        assert run[5] == _SESSION
        attempt, = owned.connection.execute(
            "SELECT status, effect_state, result_json, error_json"
            " FROM operation_attempts WHERE run_id ="
            " (SELECT id FROM operation_runs WHERE kind = 9)").fetchall()
        assert attempt[0] == 2 and attempt[1] == 3
        document = json.loads(attempt[2])
        assert document["observations"][0]["data"] == {"activity_id": "1"}
        assert entries == [{
            "action_instance_id": "1", "device_id": "cam-1",
            "max_attempts": 2, "attempts_used": 1,
            "outcome": "stopped", "record_status": "recorded",
        }]
    finally:
        owned.connection.close()


async def test_budget_exhaustion_records_unconfirmed_with_each_attempt(tmp_path):
    target, owned = await _recording_world(tmp_path)
    stop = _StopPort(_declaration(), (_timeout(), _timeout()))
    try:
        entries = await (await _settlement(target, _registry(stop))).settle()

        assert len(stop.calls) == 2
        run = _run_row(owned)
        assert run[:4] == (9, 6, 2, 2)
        assert json.loads(run[4]) == {
            "code": "emergency_stop_unconfirmed", "stage": "emergency",
            "details": {"activity_id": "1", "reason": (
                "本次应急停止额度已耗尽（2/2 次），仍未确认停止")}}
        attempts = owned.connection.execute(
            "SELECT status, effect_state, error_json FROM operation_attempts"
            " WHERE run_id = (SELECT id FROM operation_runs WHERE kind = 9)"
            " ORDER BY attempt_no").fetchall()
        assert [(row[0], row[1]) for row in attempts] == [(3, 1), (3, 1)]
        assert all(json.loads(row[2]) == {
            "code": "stop_timeout", "stage": "device", "details": {}}
            for row in attempts)
        assert entries[0]["outcome"] == "unconfirmed"
        assert entries[0]["record_status"] == "recorded"
        assert entries[0]["attempts_used"] == 2
    finally:
        owned.connection.close()


async def test_ended_activity_and_repeated_settlement_do_not_stop_again(tmp_path):
    target, owned = await _recording_world(tmp_path)
    stop = _StopPort(_declaration(), (_confirmed(), _confirmed()))
    settlement = await _settlement(target, _registry(stop))
    try:
        await settlement.settle()
        assert len(stop.calls) == 1

        # 同一会话再次收场：已补记的目标不再进入停止范围，也不重复补记。
        again = EmergencySettlement(
            session_key=_SESSION,
            anchors={1: (_NOW, _NOW + 1)},
            stop_config=settlement.stop_config,
            registry=_registry(stop),
            database=settlement.database,
            wall_us=settlement.wall_us,
        )
        assert await again.settle() == []
        assert len(stop.calls) == 1
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_runs WHERE kind = 9").fetchone() == (1,)

        # 已可靠确认停止（活动 ENDED）的录像不是应急目标。
        await _stop_recording(owned)
        third = EmergencySettlement(
            session_key="c" * 32,
            anchors={1: (_NOW, _NOW + 1)},
            stop_config=settlement.stop_config,
            registry=_registry(stop),
            database=settlement.database,
            wall_us=settlement.wall_us,
        )
        assert await third.settle() == []
        assert len(stop.calls) == 1
    finally:
        owned.connection.close()


async def test_run_session_merges_emergency_diagnostics_into_fatal_error(tmp_path):
    from camctl.acceptance.service import CommandMode
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from camctl.persistence.repositories.session import SessionRepository
    from camctl.session.clock import ClockCheckInput
    from camctl.session.locks import (
        SessionLockConflictError, acquire_admission, acquire_session,
        lock_file_paths,
    )
    from camctl.session.outcome import SessionOutcome
    from camctl.session.service import StateDbFailure, SessionContext, SessionPaths, run_session

    target, owned = await _recording_world(tmp_path)
    owned.connection.close()
    stop = _StopPort(_declaration(), (_confirmed(),))
    settlement = await _settlement(target, _registry(stop), session=True)
    session_lock, admission_lock = lock_file_paths(target)

    async def fatal_flow(context):
        raise StateDbFailure("读取流程事实失败: 数据库已锁定")

    context = SessionContext(
        mode=CommandMode.RUN,
        catalog=None,
        clock=_StaticClock(_NOW),
        open_database=lambda: open_session_database(target),
        acceptance_repository=AcceptanceRepository(),
        session_repository=SessionRepository(),
        paths=SessionPaths(session_lock=session_lock, admission_lock=admission_lock),
        clock_policy=lambda connection: ClockCheckInput(
            lower_bound_micros=None, min_plausible_micros=_NOW - 1,
            recheck_delay_s=0.0, recheck_count=1),
        acquire_session=lambda: acquire_session(session_lock),
        acquire_admission=lambda: acquire_admission(admission_lock),
        facts_query=lambda connection: None,
        flows={"capture": fatal_flow},
        emergency_settlement=lambda: _settle_in_session(context, settlement),
    )
    outcome = await run_session(context, None)

    assert isinstance(outcome, SessionOutcome)
    assert not outcome.succeeded and outcome.reason == "state_db_error"
    stops = outcome.details["emergency_stops"]
    assert [entry["outcome"] for entry in stops] == ["stopped"]
    assert stops[0]["record_status"] == "recorded"
    assert len(stop.calls) == 1
    check = open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        assert check.connection.execute(
            "SELECT status, attempts_used FROM operation_runs WHERE kind = 9"
        ).fetchone() == (3, 1)
    finally:
        check.connection.close()


async def test_cancel_during_emergency_stop_settles_before_logs_and_unlock(
        tmp_path: Path, monkeypatch) -> None:
    """父取消不取消已合格的真实 EMG，保存及日志收场仍在会话锁内。"""
    from camctl.bootstrap.background_flow import CombinedLocalWork
    from camctl.acceptance.service import CommandMode
    from camctl.logging_runtime.copies import RunFileOwnership
    from camctl.logging_runtime.ownership import LoggingOwner
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from camctl.persistence.repositories.session import SessionRepository
    from camctl.session.clock import ClockCheckInput
    from camctl.session.locks import (
        SessionLockConflictError, acquire_admission, acquire_session,
        lock_file_paths,
    )
    from camctl.session.service import SessionContext, SessionPaths, StateDbFailure, run_session
    from camctl.session.supervision import Supervisor

    timeout = 10
    target, seed = await _recording_world(tmp_path)
    seed.connection.close()
    loop = asyncio.get_running_loop()
    logs = tmp_path / "logs"
    logs.mkdir()
    scan_started = asyncio.Event()
    release_scan = threading.Event()
    original_listdir = os.listdir

    def blocked_listdir(path=None):
        if isinstance(path, (str, bytes, os.PathLike)) and os.fsdecode(path) == os.fspath(logs):
            loop.call_soon_threadsafe(scan_started.set)
            if not release_scan.wait(timeout=timeout):
                raise TimeoutError("startup sweep test gate was not released")
        return original_listdir(path)

    monkeypatch.setattr(os, "listdir", blocked_listdir)
    logging_owner = LoggingOwner(
        logs_dir=logs, ownership=RunFileOwnership(), supervisor=Supervisor(),
        owner_settled=True, diagnose=lambda message: asyncio.sleep(0))
    startup_stopped = asyncio.Event()
    original_cancel_sweep = logging_owner.cancel_startup_sweep

    def observed_cancel_sweep() -> None:
        original_cancel_sweep()
        startup_stopped.set()

    monkeypatch.setattr(logging_owner, "cancel_startup_sweep", observed_cancel_sweep)
    log_settle_started = asyncio.Event()
    log_settle_finished = asyncio.Event()
    original_log_settle = logging_owner.settle

    async def observed_log_settle() -> None:
        log_settle_started.set()
        await original_log_settle()
        log_settle_finished.set()

    monkeypatch.setattr(logging_owner, "settle", observed_log_settle)

    stop_started = asyncio.Event()
    release_stop = asyncio.Event()

    class GatedStopPort(_StopPort):
        async def stop(self, request):
            self.calls.append(request)
            stop_started.set()
            await asyncio.wait_for(release_stop.wait(), timeout=timeout)
            return _confirmed()

    stop = GatedStopPort(_declaration(), ())
    settlement = await _settlement(target, _registry(stop), session=True)
    database_close = []

    async def open_tracked_database():
        database = await open_session_database(target)
        original_close = database.close

        async def observed_close():
            database_close.append(("begin", log_settle_finished.is_set()))
            await original_close()
            database_close.append(("end", log_settle_finished.is_set()))

        database.close = observed_close
        return database

    local_stopped = asyncio.Event()

    class LocalOwner:
        def __init__(self) -> None:
            self.stop_calls = 0

        def required_settlements(self) -> int:
            return 0

        def stop_new_work(self) -> None:
            self.stop_calls += 1
            local_stopped.set()

        async def settle(self) -> None:
            return None

    local_owner = LocalOwner()
    session_lock, admission_lock = lock_file_paths(target)

    async def fatal_flow(_context) -> None:
        raise StateDbFailure("读取流程事实失败: 数据库已锁定")

    context = SessionContext(
        mode=CommandMode.RUN, catalog=None, clock=_StaticClock(_NOW),
        open_database=open_tracked_database,
        acceptance_repository=AcceptanceRepository(),
        session_repository=SessionRepository(),
        paths=SessionPaths(session_lock=session_lock, admission_lock=admission_lock),
        clock_policy=lambda connection: ClockCheckInput(
            lower_bound_micros=None, min_plausible_micros=_NOW - 1,
            recheck_delay_s=0.0, recheck_count=1),
        acquire_session=lambda: acquire_session(session_lock),
        acquire_admission=lambda: acquire_admission(admission_lock),
        facts_query=lambda connection: None,
        flows={"capture": fatal_flow},
        on_session_locked=logging_owner.start_session,
        logging_owner=logging_owner,
        local_work=CombinedLocalWork((local_owner, logging_owner)),
        emergency_settlement=lambda: _settle_in_session(context, settlement),
    )
    run_task = asyncio.create_task(run_session(context, None))
    try:
        await asyncio.wait_for(scan_started.wait(), timeout=timeout)
        await asyncio.wait_for(stop_started.wait(), timeout=timeout)

        run_task.cancel("parent cancel during emergency STOP")
        await asyncio.wait_for(local_stopped.wait(), timeout=timeout)
        await asyncio.wait_for(startup_stopped.wait(), timeout=timeout)
        assert local_owner.stop_calls == 1
        assert len(stop.calls) == 1
        assert not log_settle_started.is_set()
        assert not run_task.done()
        unexpected_lease = None
        try:
            try:
                unexpected_lease = acquire_session(session_lock)
            except SessionLockConflictError:
                pass
            else:
                pytest.fail("父取消后的必要 EMG 收场期间会话锁已提前释放")
        finally:
            if unexpected_lease is not None:
                unexpected_lease.close()

        run_task.cancel("repeated parent cancel")
        await asyncio.sleep(0)
        assert local_owner.stop_calls == 1
        assert len(stop.calls) == 1
        assert not log_settle_started.is_set()

        release_stop.set()
        await asyncio.wait_for(log_settle_started.wait(), timeout=timeout)
        saved = open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            assert _run_row(saved)[1:4] == (3, 1, 2)
        finally:
            saved.connection.close()
        # 共享业务 worker 要等日志收场结束后关闭，不能在等待期间提前关闭。
        assert database_close == []
        assert await context.database.read(
            lambda current: _run_row(current)[1:4], description="核对共享数据库中的应急结果"
        ) == (3, 1, 2)
        assert len(stop.calls) == 1
        assert not run_task.done()
        release_scan.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await asyncio.wait_for(run_task, timeout=timeout)
        assert cancelled.value.args == ("parent cancel during emergency STOP",)
        assert database_close == [("begin", True), ("end", True)]
        released_lease = acquire_session(session_lock)
        released_lease.close()
    finally:
        release_stop.set()
        release_scan.set()
        if not run_task.done():
            run_task.cancel("emergency test cleanup")
            await asyncio.wait_for(
                asyncio.gather(run_task, return_exceptions=True), timeout=timeout)


@pytest.mark.parametrize("parent_cancel", [False, True])
async def test_internal_emergency_control_error_still_settles_logs(
        tmp_path: Path, monkeypatch, parent_cancel: bool) -> None:
    """EMG 内部 CancelledError 不跳过日志收场，也不替换首父取消。"""
    from camctl.bootstrap.background_flow import CombinedLocalWork
    from camctl.acceptance.service import CommandMode
    from camctl.logging_runtime.copies import RunFileOwnership
    from camctl.logging_runtime.ownership import LoggingOwner
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from camctl.persistence.repositories.session import SessionRepository
    from camctl.session.clock import ClockCheckInput
    from camctl.session.locks import (
        SessionLockConflictError, acquire_admission, acquire_session,
        lock_file_paths,
    )
    from camctl.session.service import SessionContext, SessionPaths, StateDbFailure, run_session
    from camctl.session.supervision import Supervisor

    timeout = 10
    target, seed = await _recording_world(tmp_path)
    seed.connection.close()
    logs = tmp_path / "logs"
    logs.mkdir()
    diagnostic_started = asyncio.Event()
    release_diagnostic = asyncio.Event()
    diagnostic_messages: list[str] = []
    scan_thread_ids: list[int] = []
    original_listdir = os.listdir

    def failed_listdir(path=None):
        if isinstance(path, (str, bytes, os.PathLike)) and os.fsdecode(path) == os.fspath(logs):
            scan_thread_ids.append(threading.get_ident())
            raise OSError("controlled startup scan failure")
        return original_listdir(path)

    async def gated_diagnose(message: str) -> None:
        diagnostic_messages.append(message)
        diagnostic_started.set()
        await asyncio.wait_for(release_diagnostic.wait(), timeout=timeout)

    monkeypatch.setattr(os, "listdir", failed_listdir)
    logging_owner = LoggingOwner(
        logs_dir=logs, ownership=RunFileOwnership(), supervisor=Supervisor(),
        owner_settled=True, diagnose=gated_diagnose)
    log_settle_started = asyncio.Event()
    original_log_settle = logging_owner.settle

    async def observed_log_settle() -> None:
        log_settle_started.set()
        await original_log_settle()

    logging_owner.settle = observed_log_settle
    stop_started = asyncio.Event()
    release_stop = asyncio.Event()
    internal_control = asyncio.CancelledError("internal emergency control")

    class ControlStopPort(_StopPort):
        async def stop(self, request):
            self.calls.append(request)
            stop_started.set()
            await asyncio.wait_for(release_stop.wait(), timeout=timeout)
            raise internal_control

    stop = ControlStopPort(_declaration(), ())
    settlement = await _settlement(target, _registry(stop), session=True)
    session_lock, admission_lock = lock_file_paths(target)

    async def fatal_flow(_context) -> None:
        raise StateDbFailure("读取流程事实失败")

    context = SessionContext(
        mode=CommandMode.RUN, catalog=None, clock=_StaticClock(_NOW),
        open_database=lambda: open_session_database(target),
        acceptance_repository=AcceptanceRepository(),
        session_repository=SessionRepository(),
        paths=SessionPaths(session_lock=session_lock, admission_lock=admission_lock),
        clock_policy=lambda connection: ClockCheckInput(
            lower_bound_micros=None, min_plausible_micros=_NOW - 1,
            recheck_delay_s=0.0, recheck_count=1),
        acquire_session=lambda: acquire_session(session_lock),
        acquire_admission=lambda: acquire_admission(admission_lock),
        facts_query=lambda connection: None,
        flows={"capture": fatal_flow},
        on_session_locked=logging_owner.start_session,
        logging_owner=logging_owner,
        local_work=CombinedLocalWork((logging_owner,)),
        emergency_settlement=lambda: _settle_in_session(context, settlement),
    )
    run_task = asyncio.create_task(run_session(context, None))
    try:
        await asyncio.wait_for(diagnostic_started.wait(), timeout=timeout)
        await asyncio.wait_for(stop_started.wait(), timeout=timeout)
        assert scan_thread_ids and scan_thread_ids[0] != threading.get_ident()
        assert "controlled startup scan failure" in diagnostic_messages[0]
        if parent_cancel:
            run_task.cancel("parent before internal EMG control")
            await asyncio.sleep(0)
            assert not run_task.done()
        release_stop.set()
        await asyncio.wait_for(log_settle_started.wait(), timeout=timeout)
        unexpected_lease = None
        try:
            try:
                unexpected_lease = acquire_session(session_lock)
            except SessionLockConflictError:
                pass
            else:
                pytest.fail("启动日志诊断阻塞期间会话锁已提前释放")
        finally:
            if unexpected_lease is not None:
                unexpected_lease.close()
        assert not run_task.done()
        release_diagnostic.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await asyncio.wait_for(run_task, timeout=timeout)
        if parent_cancel:
            assert raised.value.args == ("parent before internal EMG control",)
            assert any("internal emergency control" in note
                       for note in getattr(raised.value, "__notes__", ()))
        else:
            assert raised.value is internal_control
        assert len(stop.calls) == 1
    finally:
        release_stop.set()
        release_diagnostic.set()
        if not run_task.done():
            run_task.cancel("internal EMG test cleanup")
            await asyncio.wait_for(
                asyncio.gather(run_task, return_exceptions=True), timeout=timeout)


@pytest.mark.parametrize("parent_cancel", [False, True])
async def test_initial_restricted_internal_control_stays_separate_from_parent(
        tmp_path: Path, monkeypatch, parent_cancel: bool) -> None:
    """区分受限阶段内部取消与父取消，并保留内部异常附注。"""
    from camctl.acceptance.service import CommandMode
    from camctl.bootstrap.background_flow import CombinedLocalWork
    from camctl.logging_runtime.copies import RunFileOwnership
    from camctl.logging_runtime.ownership import LoggingOwner
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from camctl.persistence.repositories.session import SessionRepository
    from camctl.session.clock import ClockCheckInput
    from camctl.session.locks import acquire_admission, acquire_session, lock_file_paths
    from camctl.session.service import SessionContext, SessionPaths, run_session
    from camctl.session.supervision import Supervisor

    timeout = 10
    target, seed = await _recording_world(tmp_path)
    seed.connection.close()
    logs = tmp_path / "logs"
    logs.mkdir()
    diagnostic_started = asyncio.Event()
    release_diagnostic = asyncio.Event()
    diagnostic_messages: list[str] = []
    scan_thread_ids: list[int] = []
    original_listdir = os.listdir

    def failed_listdir(path=None):
        if isinstance(path, (str, bytes, os.PathLike)) and os.fsdecode(path) == os.fspath(logs):
            scan_thread_ids.append(threading.get_ident())
            raise OSError("controlled restricted startup scan failure")
        return original_listdir(path)

    async def gated_diagnose(message: str) -> None:
        diagnostic_messages.append(message)
        diagnostic_started.set()
        await asyncio.wait_for(release_diagnostic.wait(), timeout=timeout)

    monkeypatch.setattr(os, "listdir", failed_listdir)
    logging_owner = LoggingOwner(
        logs_dir=logs, ownership=RunFileOwnership(), supervisor=Supervisor(),
        owner_settled=True, diagnose=gated_diagnose)
    startup_cancel_calls: list[str] = []
    original_cancel_sweep = logging_owner.cancel_startup_sweep

    def observed_cancel_sweep() -> None:
        startup_cancel_calls.append("cancel")
        original_cancel_sweep()

    monkeypatch.setattr(logging_owner, "cancel_startup_sweep", observed_cancel_sweep)
    log_settle_started = asyncio.Event()
    original_log_settle = logging_owner.settle

    async def observed_log_settle() -> None:
        log_settle_started.set()
        await original_log_settle()

    logging_owner.settle = observed_log_settle
    session_lock, admission_lock = lock_file_paths(target)
    internal_control = asyncio.CancelledError("internal restricted control")
    internal_control.add_note("restricted phase saved secondary cleanup fact")
    restricted_started = asyncio.Event()

    async def restricted_control(_context) -> None:
        restricted_started.set()
        raise internal_control

    context = SessionContext(
        mode=CommandMode.RUN, catalog=None, clock=_StaticClock(_NOW),
        open_database=lambda: open_session_database(target),
        acceptance_repository=AcceptanceRepository(),
        session_repository=SessionRepository(),
        paths=SessionPaths(session_lock=session_lock, admission_lock=admission_lock),
        clock_policy=lambda connection: ClockCheckInput(
            lower_bound_micros=_NOW + 1, min_plausible_micros=_NOW + 1,
            recheck_delay_s=0.0, recheck_count=1),
        acquire_session=lambda: acquire_session(session_lock),
        acquire_admission=lambda: acquire_admission(admission_lock),
        facts_query=lambda connection: None,
        restricted_flows={"stop": restricted_control},
        on_session_locked=logging_owner.start_session,
        logging_owner=logging_owner,
        local_work=CombinedLocalWork((logging_owner,)),
    )
    run_task = asyncio.create_task(run_session(context, None))
    try:
        await asyncio.wait_for(restricted_started.wait(), timeout=timeout)
        await asyncio.wait_for(diagnostic_started.wait(), timeout=timeout)
        await asyncio.wait_for(log_settle_started.wait(), timeout=timeout)
        assert scan_thread_ids and scan_thread_ids[0] != threading.get_ident()
        assert "controlled restricted startup scan failure" in diagnostic_messages[0]
        assert startup_cancel_calls == []
        assert not run_task.done()

        if parent_cancel:
            run_task.cancel("actual parent cancellation during diagnostics")
            await asyncio.sleep(0)
            assert startup_cancel_calls == ["cancel"]
            assert not run_task.done()
            run_task.cancel("repeated parent cancellation")
            await asyncio.sleep(0)
            assert startup_cancel_calls == ["cancel"]

        release_diagnostic.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await asyncio.wait_for(run_task, timeout=timeout)
        if parent_cancel:
            assert raised.value.args == (
                "actual parent cancellation during diagnostics",)
            notes = getattr(raised.value, "__notes__", ())
            assert any("internal restricted control" in note for note in notes)
            assert any("restricted phase saved secondary cleanup fact" in note
                       for note in notes)
        else:
            assert raised.value is internal_control
            assert getattr(raised.value, "__notes__", ()) == [
                "restricted phase saved secondary cleanup fact"]
            assert startup_cancel_calls == []
    finally:
        release_diagnostic.set()
        if not run_task.done():
            run_task.cancel("initial restricted control test cleanup")
            await asyncio.wait_for(
                asyncio.gather(run_task, return_exceptions=True), timeout=timeout)

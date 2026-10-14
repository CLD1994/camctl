"""会话致命错误收场的应急停止接线：真实发现、停止、补记与诊断。

按[正在录像时的应急停止](../../../../../architecture/session-errors.md)
验证默认会话错误出口：非时钟致命错误时对会话锚点内仍可确认的录
像有限尝试停止，逐个补记 kind=9 流程并把诊断并入原错误。
"""

import asyncio
from decimal import Decimal
import json
from pathlib import Path
import threading

import pytest

from camctl.capture.emergency_flow import EmergencySettlement
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.evidence import DeviceObservation, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult, DriverDeclaration
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..persistence.test_runtime import _create_valid_database
from ..scheduling.test_resources import _seed_activity, _seed_plan, _seed_record_action

register_capture_guards()

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


def _world(tmp_path: Path, *, activity_state: int = 2, safe_repeat: bool = True):
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
    _seed_plan(connection, 1)
    _seed_record_action(connection, 1, 1)
    _seed_activity(connection, 1, dispatch_state=3, activity_state=activity_state)
    if not safe_repeat:
        connection.execute("UPDATE device_activities SET safe_repeat_stop = 0")
    connection.commit()
    return target, owned


def _settlement(target: Path, registry, *, anchors=None, maximum: int = 2):
    return EmergencySettlement(
        session_key=_SESSION,
        anchors=anchors if anchors is not None else {1: (_NOW, _NOW + 1)},
        stop_config={"cam-1": {
            "max_attempts": maximum,
            "timeout_s": Decimal("10"),
            "retry_interval_s": Decimal("0"),
        }},
        registry=registry,
        open_owned=lambda: open_existing(target, DbOpenMode.EXISTING_RW, DbConfig()),
        wall_us=lambda: _NOW,
    )


def _run_row(owned):
    return owned.connection.execute(
        "SELECT kind, status, attempts_used, max_attempts_used, error_json,"
        " session_key FROM operation_runs WHERE kind = 9").fetchone()


async def test_fatal_exit_stops_recording_once_and_records_stopped(tmp_path):
    target, owned = _world(tmp_path)
    stop = _StopPort(_declaration(), (_confirmed(),))
    try:
        entries = await _settlement(target, _registry(stop)).settle()

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
    target, owned = _world(tmp_path)
    stop = _StopPort(_declaration(), (_timeout(), _timeout()))
    try:
        entries = await _settlement(target, _registry(stop)).settle()

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


async def test_unsafe_repeat_target_records_not_attempted_without_stop(tmp_path):
    target, owned = _world(tmp_path, safe_repeat=False)
    stop = _StopPort(_declaration(), (_confirmed(),))
    try:
        entries = await _settlement(target, _registry(stop)).settle()

        assert stop.calls == []
        run = _run_row(owned)
        assert run[:4] == (9, 4, 0, 2)
        assert json.loads(run[4])["code"] == "emergency_not_attempted"
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts WHERE run_id ="
            " (SELECT id FROM operation_runs WHERE kind = 9)").fetchone() == (0,)
        assert entries[0]["outcome"] == "not_attempted"
        assert entries[0]["record_status"] == "recorded"
        assert entries[0]["attempts_used"] == 0
    finally:
        owned.connection.close()


async def test_ended_activity_and_repeated_settlement_do_not_stop_again(tmp_path):
    target, owned = _world(tmp_path)
    stop = _StopPort(_declaration(), (_confirmed(), _confirmed()))
    settlement = _settlement(target, _registry(stop))
    try:
        await settlement.settle()
        assert len(stop.calls) == 1

        # 同一会话再次收场：已补记的目标不再进入停止范围，也不重复补记。
        again = EmergencySettlement(
            session_key=_SESSION,
            anchors={1: (_NOW, _NOW + 1)},
            stop_config=settlement.stop_config,
            registry=_registry(stop),
            open_owned=settlement.open_owned,
            wall_us=settlement.wall_us,
        )
        assert await again.settle() == []
        assert len(stop.calls) == 1
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_runs WHERE kind = 9").fetchone() == (1,)

        # 已可靠确认停止（活动 ENDED）的录像不是应急目标。
        owned.connection.execute(
            "UPDATE device_activities SET activity_state = 3")
        owned.connection.commit()
        third = EmergencySettlement(
            session_key="c" * 32,
            anchors={1: (_NOW, _NOW + 1)},
            stop_config=settlement.stop_config,
            registry=_registry(stop),
            open_owned=settlement.open_owned,
            wall_us=settlement.wall_us,
        )
        assert await third.settle() == []
        assert len(stop.calls) == 1
    finally:
        owned.connection.close()


async def test_unreadable_state_keeps_diagnosis_without_device_calls(tmp_path):
    target, owned = _world(tmp_path)
    owned.connection.close()
    stop = _StopPort(_declaration(), (_confirmed(),))

    def _broken_open():
        raise RuntimeError("database disk image is malformed")

    settlement = EmergencySettlement(
        session_key=_SESSION, anchors={1: (_NOW, _NOW + 1)},
        stop_config={"cam-1": {"max_attempts": 2}},
        registry=_registry(stop), open_owned=_broken_open,
        wall_us=lambda: _NOW)
    entries = await settlement.settle()

    assert stop.calls == []
    assert len(entries) == 1
    assert entries[0]["outcome"] == "not_attempted"
    assert entries[0]["record_status"] == "not_recorded"
    assert "目标身份读取失败" in entries[0]["error"]


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

    target, owned = _world(tmp_path)
    owned.connection.close()
    stop = _StopPort(_declaration(), (_confirmed(),))
    settlement = _settlement(target, _registry(stop))
    session_lock, admission_lock = lock_file_paths(target)

    async def fatal_flow(context):
        raise StateDbFailure("读取流程事实失败: 数据库已锁定")

    context = SessionContext(
        mode=CommandMode.RUN,
        catalog=None,
        clock=_StaticClock(_NOW),
        open_connection=lambda: open_existing(
            target, DbOpenMode.EXISTING_RW, DbConfig()),
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
        emergency_settlement=settlement.settle,
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
    from camctl.logging_runtime import copies
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
    target, seed = _world(tmp_path)
    seed.connection.close()
    loop = asyncio.get_running_loop()
    logs = tmp_path / "logs"
    logs.mkdir()
    scan_started = asyncio.Event()
    release_scan = threading.Event()
    original_listdir = copies._listdir

    def blocked_listdir(path):
        loop.call_soon_threadsafe(scan_started.set)
        if not release_scan.wait(timeout=timeout):
            raise TimeoutError("startup sweep test gate was not released")
        return original_listdir(path)

    monkeypatch.setattr(copies, "_listdir", blocked_listdir)
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
    original_log_settle = logging_owner.settle

    async def observed_log_settle() -> None:
        log_settle_started.set()
        await original_log_settle()

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
    settlement = _settlement(target, _registry(stop))
    close_counts: list[int] = []
    original_open = settlement.open_owned

    class ConnectionCloseSpy:
        def __init__(self, connection) -> None:
            self._connection = connection

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def close(self) -> None:
            close_counts.append(len(close_counts) + 1)
            self._connection.close()

    def open_owned():
        owned = original_open()
        return type(owned)(
            connection=ConnectionCloseSpy(owned.connection),
            metadata=owned.metadata,
        )

    settlement.open_owned = open_owned
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
        open_connection=lambda: open_existing(
            target, DbOpenMode.EXISTING_RW, DbConfig()),
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
        emergency_settlement=settlement.settle,
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
        assert close_counts == [1, 2]
        assert len(stop.calls) == 1
        assert not run_task.done()
        release_scan.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await asyncio.wait_for(run_task, timeout=timeout)
        assert cancelled.value.args == ("parent cancel during emergency STOP",)
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
    from camctl.logging_runtime import copies
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
    target, seed = _world(tmp_path)
    seed.connection.close()
    logs = tmp_path / "logs"
    logs.mkdir()
    diagnostic_started = asyncio.Event()
    release_diagnostic = asyncio.Event()
    diagnostic_messages: list[str] = []
    scan_thread_ids: list[int] = []
    def failed_listdir(_path):
        scan_thread_ids.append(threading.get_ident())
        raise OSError("controlled startup scan failure")

    async def gated_diagnose(message: str) -> None:
        diagnostic_messages.append(message)
        diagnostic_started.set()
        await asyncio.wait_for(release_diagnostic.wait(), timeout=timeout)

    monkeypatch.setattr(copies, "_listdir", failed_listdir)
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
    settlement = _settlement(target, _registry(stop))
    session_lock, admission_lock = lock_file_paths(target)

    async def fatal_flow(_context) -> None:
        raise StateDbFailure("读取流程事实失败")

    context = SessionContext(
        mode=CommandMode.RUN, catalog=None, clock=_StaticClock(_NOW),
        open_connection=lambda: open_existing(
            target, DbOpenMode.EXISTING_RW, DbConfig()),
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
        emergency_settlement=settlement.settle,
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
    from camctl.logging_runtime import copies
    from camctl.logging_runtime.copies import RunFileOwnership
    from camctl.logging_runtime.ownership import LoggingOwner
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from camctl.persistence.repositories.session import SessionRepository
    from camctl.session.clock import ClockCheckInput
    from camctl.session.locks import acquire_admission, acquire_session, lock_file_paths
    from camctl.session.service import SessionContext, SessionPaths, run_session
    from camctl.session.supervision import Supervisor

    timeout = 10
    target, seed = _world(tmp_path)
    seed.connection.close()
    logs = tmp_path / "logs"
    logs.mkdir()
    diagnostic_started = asyncio.Event()
    release_diagnostic = asyncio.Event()
    diagnostic_messages: list[str] = []
    scan_thread_ids: list[int] = []

    def failed_listdir(_path):
        scan_thread_ids.append(threading.get_ident())
        raise OSError("controlled restricted startup scan failure")

    async def gated_diagnose(message: str) -> None:
        diagnostic_messages.append(message)
        diagnostic_started.set()
        await asyncio.wait_for(release_diagnostic.wait(), timeout=timeout)

    monkeypatch.setattr(copies, "_listdir", failed_listdir)
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
        open_connection=lambda: open_existing(
            target, DbOpenMode.EXISTING_RW, DbConfig()),
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

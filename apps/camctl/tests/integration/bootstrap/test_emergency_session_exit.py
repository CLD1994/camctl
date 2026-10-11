"""会话致命错误收场的应急停止接线：真实发现、停止、补记与诊断。

按[正在录像时的应急停止](../../../../../architecture/session-errors.md)
验证默认会话错误出口：非时钟致命错误时对会话锚点内仍可确认的录
像有限尝试停止，逐个补记 kind=9 流程并把诊断并入原错误。
"""

from decimal import Decimal
import json
from pathlib import Path

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
    from camctl.session.locks import acquire_admission, acquire_session, lock_file_paths
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

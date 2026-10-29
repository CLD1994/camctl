"""C1实际调用保存与 QUERY 公开仓储的原输入身份。"""
from dataclasses import replace
from decimal import Decimal
import json
import sqlite3

import pytest

from camctl.capture.handlers import capture_handler
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import ControlRequest, DeviceBinding, DeviceCallResult
from camctl.operations.attempts import (AttemptConfig, AttemptIntent, AttemptTarget,
    AttemptFinish, BeginDisposition, OperationKind, QueryPurpose, StaleRunFinish, RunOutcome, RunFinish)
from camctl.operations.models import (AttemptStatus, CallInfo, CallOutcome, EffectState,
    ErrorValue, EvidenceValue, Settlement, SettlementBasis)
from camctl.operations.validation import validate_outcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.capture.models import ActivityObservationSave
from ..support.call_cases import call_case, CallPort, SavedCallBoundary
from ..support.capture import capture_runtime
from .test_capture_contract import _NOW, ResultsDouble
from .test_recording_start_runtime import StartDriver, _result

pytestmark = pytest.mark.asyncio
_RETURNED_AT = _NOW + 2_000_000
_CALL_EVIDENCE = EvidenceRegistry((
    EvidenceContract("operation_returned", 1, "control", frozenset(), max_diagnostic_calls=2),
    EvidenceContract("start_confirmed", 1, "control", frozenset({"activity_id"}), identity_field="activity_id"),
    EvidenceContract("stop_returned", 1, "stop", frozenset(), max_diagnostic_calls=2),
    EvidenceContract("stop_confirmed", 1, "stop", frozenset({"activity_id"}), identity_field="activity_id"),
    EvidenceContract("query_returned", 1, "query", frozenset()),
    EvidenceContract("activity_status", 1, "query", frozenset({"activity_id"}), identity_field="activity_id"),
    EvidenceContract("adb_foreground_assumption", 1, "query", frozenset({"terminate_grace_s"})),
))


def _outcome(operation, *, assumed=False, confirmed=False, activity_id="12"):
    return CallOutcome(
        status=AttemptStatus.FAILED,
        error=ErrorValue("transport_timeout", "transport", {"timeout_s": Decimal("0.125")}),
        effect=EffectState.CONFIRMED if confirmed else EffectState.UNKNOWN,
        settlement=Settlement(
            SettlementBasis.ASSUMED if assumed else SettlementBasis.OBSERVED,
            EvidenceValue("adb_foreground_assumption" if assumed else f"{operation}_returned",
                          1, {"terminate_grace_s": Decimal("0.25")} if assumed else {})),
        observations=(DeviceObservation(
            "stop_confirmed" if operation == "stop" else "activity_status",
            1, {"activity_id": activity_id}),) if confirmed else (),
        call_info=CallInfo(local_signal=9) if assumed else CallInfo(local_exit_code=0),
    )


class ReturnedCall:
    """设备边界提供完整结果；调用后的库错误不使本替身重新执行。"""

    def __init__(self, outcome, *, returned=None):
        self.outcome, self.returned = outcome, returned
        self.calls = []

    def _return(self, request):
        self.calls.append(request)
        if self.returned is not None:
            self.returned()
        return DeviceCallResult.from_outcome(self.outcome)

    async def stop(self, request):
        return self._return(request)

    async def query_state(self, request):
        return self._return(request)


def _assert_actual_saved(owned, ticket, *, assumed, confirmed, occurred_at, activity_id="12"):
    row = owned.connection.execute(
        "SELECT a.status, a.effect_state, a.result_json, a.error_json, e.occurred_at"
        " FROM operation_attempts a JOIN history_events e ON e.id=a.result_event_id"
        " WHERE a.run_id=? AND a.attempt_no=?", (ticket.run_id, ticket.attempt_id)).fetchone()
    assert row is not None
    assert row[:2] == (3, 3 if confirmed else 1)
    result = json.loads(row[2])
    assert result["settlement"] == {
        "basis": "assumed" if assumed else "observed",
        "evidence": {"type": "adb_foreground_assumption" if assumed else f"{ticket.operation}_returned",
                     "version": 1, "data": {"terminate_grace_s": 0.25} if assumed else {}}}
    assert result["call_info"] == {
        "local_exit": {"signal": 9} if assumed else {"exit_code": 0}}
    assert result["observations"] == ([{
        "type": "stop_confirmed" if ticket.operation == "stop" else "activity_status",
        "version": 1, "data": {"activity_id": activity_id}}] if confirmed else [])
    assert json.loads(row[3]) == {
        "code": "transport_timeout", "stage": "transport", "details": {"timeout_s": 0.125}}
    assert row[4] == occurred_at
    assert owned.connection.execute(
        "SELECT attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (1,)


def _diagnosed_outcome(activity_id):
    from camctl.operations.tool_diagnostics import ToolDiagnostics, ToolCallOutput, ToolStream, ToolStreamState
    diagnostics = ToolDiagnostics(2, (ToolCallOutput(1, "stop", ToolStream(
        ToolStreamState.COMPLETE, b"\x00\xff"), ToolStream(ToolStreamState.COMPLETE, b"")),))
    return replace(_outcome("stop", confirmed=True, activity_id=activity_id), diagnostics=diagnostics)


async def _call_world(case, tmp_path, *, now=_NOW, control=None):
    def start(request):
        result = _result(confirmed=True) if control is None else control
        return replace(result, observations=tuple(replace(value,
            data={**value.data, "activity_id": request.ticket.target_id}) for value in result.observations))
    evidence = _CALL_EVIDENCE
    return await case.world(tmp_path, evidence=evidence, control=CallPort(start),
        stop=CallPort(lambda request: _diagnosed_outcome(request.ticket.target_id)),
        query=CallPort(_outcome("query", assumed=True)), now=now)


async def _call_started(case, tmp_path, *, now=_NOW):
    runtime = await _call_world(case, tmp_path, now=now)
    case.boundary = "control"
    with pytest.raises(SavedCallBoundary):
        await capture_handler("camera_record")(case.action_id, runtime)
    case.boundary = None
    return await case.view()


@pytest.mark.parametrize("mode", ["read", "projection", "commit_before", "commit_after"])
async def test_stop_return_is_resaved_with_original_key_without_second_device_call(tmp_path, call_case, mode):
    case = call_case
    runtime = await _call_started(case, tmp_path)
    before_result = []
    case.stop.returned = lambda: before_result.append(tuple(case.owned.connection.iterdump()))
    case.clock.wall = _RETURNED_AT
    case.clock.monotonic = 67_000_000_000
    case.boundary = "stop"
    case.fault.arm(case.stop, mode)
    with pytest.raises(SavedCallBoundary):
        await capture_handler("camera_record")(case.action_id, runtime)
    request, = case.stop.calls
    assert request.timeout_s == Decimal("0.125") and case.fault.injected
    _assert_actual_saved(case.owned, request.ticket, assumed=False, confirmed=True,
        occurred_at=_RETURNED_AT, activity_id=request.ticket.target_id)
    assert len(set(case.fault.result_keys)) == 1
    result_json, = case.owned.connection.execute(
        "SELECT result_json FROM operation_attempts WHERE run_id=?", (request.ticket.run_id,)).fetchone()
    assert json.loads(result_json)["diagnostics"]["calls"][0]["stdout"]["hex"] == "00ff"
    if mode in ("projection", "commit_before"):
        assert case.fault.rolled_back_images == before_result


async def _started(owned):
    """既有外部普通录像夹具沿正式工厂取得START，不改运行时配置字段。"""
    runtime = await capture_runtime(owned, driver=StartDriver(_result(confirmed=True)),
        evidence=_CALL_EVIDENCE, results=ResultsDouble({}), wall_us=lambda: _NOW,
        monotonic_ns=lambda: 7_000_000_000,
        start_config=AttemptConfig(3, Decimal("0.75"), Decimal("1")),
        stop_config=AttemptConfig(1, Decimal("0.125"), Decimal("1")),
        query_config=AttemptConfig(2, Decimal("0.5"), Decimal("1")))
    await capture_handler("camera_record")(12, runtime)
    return runtime


def _diagnosed_stop(runtime):
    """原调用形态由既有消费者使用；证据数量已在工厂登记中声明。"""
    return _diagnosed_outcome("12")


def _for_ticket(case, ticket):
    return [(finish, key) for finish, key in case.saves if finish.ticket == ticket]


async def test_stop_pending_actual_result_precedes_unknown_recovery_without_boundary(tmp_path, call_case):
    case = call_case
    runtime = await _call_started(case, tmp_path)
    case.clock.wall, case.clock.monotonic = _RETURNED_AT, 67_000_000_000
    case.boundary = "stop"
    case.fault.arm(case.stop, "persistent")
    with pytest.raises(ConsistencyError):
        await capture_handler("camera_record")(case.action_id, runtime)
    request, = case.stop.calls
    assert case.owned.connection.execute("SELECT status,result_event_id FROM operation_attempts WHERE run_id=?",
        (request.ticket.run_id,)).fetchone() == (1, None)
    original = _for_ticket(case, request.ticket)
    assert len(original) == 2 and original[0] == original[1]
    case.fault.enabled = False
    case.clock.wall += 5_000_000
    resumed = await case.restart()
    with pytest.raises(SavedCallBoundary):
        await capture_handler("camera_record")(case.action_id, resumed)
    _assert_actual_saved(case.owned, request.ticket, assumed=False, confirmed=True,
        occurred_at=_RETURNED_AT, activity_id=request.ticket.target_id)
    assert len(case.stop.calls) == 1 and len(set(case.fault.result_keys)) == 1
    assert all(item == original[0] for item in _for_ticket(case, request.ticket))


async def _write(case, method, command, key):
    return await case.database.write(lambda owned: method(command, key, owned), key=key)


async def _query_storage(case, tmp_path, purpose):
    """原 START 未确认后的查询仓储关联，不声明当前业务派发资格。"""
    assert purpose is QueryPurpose.START_CONFIRMATION
    runtime = await _call_world(case, tmp_path, control=_result())
    case.boundary = "control"
    with pytest.raises(SavedCallBoundary):
        await capture_handler("camera_record")(case.action_id, runtime)
    case.boundary = None
    intent = AttemptIntent("query", case.action_id, OperationKind.QUERY_ACTIVITY,
        AttemptTarget(activity_id=case.activity_id), purpose,
        AttemptConfig(2, Decimal("0.125"), Decimal("1")), _NOW)
    saved = await _write(case, OperationRepository().begin_attempt, intent, new_operation_key())
    assert saved.kind is DbOutcomeKind.COMPLETED and saved.value.disposition is BeginDisposition.GRANTED, saved.error
    return saved.value.ticket


async def _query_finish(case, ticket):
    response = await case.query.query_state(ControlRequest("query_state", DeviceBinding(
        "cam-1", "camctl-adb"), {}, ticket=ticket, timeout_s=Decimal("0.125")))
    return AttemptFinish(ticket, validate_outcome(ticket, response.outcome, _CALL_EVIDENCE),
        _RETURNED_AT, retry_wait=True)


@pytest.mark.parametrize("purpose", [QueryPurpose.START_CONFIRMATION])
async def test_query_original_finish_is_exact_after_commit_before_fault(tmp_path, call_case, purpose):
    case = call_case
    ticket = await _query_storage(case, tmp_path, purpose)
    case.fault.arm(case.query, "commit_before")
    finish = await _query_finish(case, ticket)
    key = new_operation_key()
    failed = await _write(case, OperationRepository().finish_attempt, finish, key)
    assert failed.kind is DbOutcomeKind.UNKNOWN and case.fault.injected
    saved = await _write(case, OperationRepository().finish_attempt, finish, key)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    _assert_actual_saved(case.owned, ticket, assumed=True, confirmed=False, occurred_at=_RETURNED_AT)
    assert len(case.query.calls) == 1 and set(case.fault.result_keys) == {str(key)}


@pytest.mark.parametrize("purpose", [QueryPurpose.START_CONFIRMATION])
async def test_query_original_finish_is_exact_after_unknown_store_reopens(tmp_path, call_case, purpose):
    case = call_case
    ticket = await _query_storage(case, tmp_path, purpose)
    case.fault.arm(case.query, "persistent")
    finish = await _query_finish(case, ticket)
    key = new_operation_key()
    unknown = await _write(case, OperationRepository().finish_attempt, finish, key)
    assert unknown.kind is DbOutcomeKind.UNKNOWN and case.fault.injected
    assert case.owned.connection.execute("SELECT status,result_event_id FROM operation_attempts WHERE run_id=?",
        (ticket.run_id,)).fetchone() == (1, None)
    case.fault.enabled = False
    case.clock.wall = _RETURNED_AT + 5_000_000
    await case.restart()
    saved = await _write(case, OperationRepository().finish_attempt, finish, key)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    _assert_actual_saved(case.owned, ticket, assumed=True, confirmed=False, occurred_at=_RETURNED_AT)
    assert len(case.query.calls) == 1 and set(case.fault.result_keys) == {str(key)}


_PUBLIC_QUERY_PURPOSES = [purpose for purpose in QueryPurpose
    if purpose is not QueryPurpose.START_CONFIRMATION]


async def _query_consumer(case, tmp_path, purpose):
    """由真实启动、停止和后续动作进入对应的 C1 查询消费者。"""
    from camctl.persistence.repositories.scheduling import StartActionRequest, SchedulingRepository
    if purpose is QueryPurpose.ACTIVITY_OBSERVATION:
        await _call_started(case, tmp_path, now=_NOW - 60_000_000)
        runtime = await case.restart(cold=True)
    else:
        runtime = await _call_started(case, tmp_path,
            now=_NOW - 60_000_000 if purpose is QueryPurpose.STOP_CONFIRMATION else _NOW)
        case.clock.monotonic = 67_000_000_000
        case.boundary = "stop"
        case.stop.outcome = (_outcome("stop") if purpose in (
            QueryPurpose.BEFORE_EXECUTION, QueryPurpose.RESIDUAL_STOP_CONFIRMATION)
            else replace(_outcome("stop"), status=AttemptStatus.SUCCEEDED, error=None))
        with pytest.raises(SavedCallBoundary):
            await capture_handler("camera_record")(case.action_id, runtime)
        case.boundary = None
        if purpose in (QueryPurpose.BEFORE_EXECUTION, QueryPurpose.RESIDUAL_STOP_CONFIRMATION):
            await capture_handler("camera_record")(case.action_id, await case.view())
            assert case.owned.connection.execute("SELECT status FROM actions WHERE id=?",
                (case.action_id,)).fetchone() == (4,)
            original_activity = case.activity_id
            case.action_id = await case.accept(tmp_path, occurred_at=_NOW)
            started = SchedulingRepository().start_action(
                StartActionRequest(case.action_id, _NOW, _NOW), new_operation_key(), case.owned)
            assert started.kind is DbOutcomeKind.COMPLETED, started.error
            if purpose is QueryPurpose.RESIDUAL_STOP_CONFIRMATION:
                case.query.outcome = replace(_outcome("query", confirmed=True,
                    activity_id=str(original_activity)), status=AttemptStatus.SUCCEEDED, error=None)
                case.stop.outcome = replace(_outcome("stop"), status=AttemptStatus.SUCCEEDED, error=None)
                case.boundary = "stop"
                with pytest.raises(SavedCallBoundary):
                    await capture_handler("camera_record")(case.action_id, await case.view())
                assert len(case.query.calls) == 1 and len(case.stop.calls) == 2
        elif purpose is QueryPurpose.STOP_CONFIRMATION:
            # 原 STOP 仅有一次额度；冷会话先核对原录像，再进入原停止核实。
            # 保留原额度，通过正式工厂丢弃旧单调锚点，不改设备活动能力。
            await case.restart(cold=True)
    case.query.outcome = _outcome("query", assumed=True)
    case.boundary = "query"
    if purpose is QueryPurpose.STOP_CONFIRMATION:
        responsibility = f"query/stop/{case.action_id}/{case.activity_id}"
        def query(request):
            if request.ticket.responsibility_key != responsibility:
                return replace(_outcome("query", confirmed=True, activity_id=str(case.activity_id)),
                    status=AttemptStatus.SUCCEEDED, error=None)
            return _outcome("query", assumed=True)
        case.query.outcome = query
        case.boundary = responsibility
    case.clock.wall = _RETURNED_AT
    return await case.view()


@pytest.mark.parametrize("purpose", _PUBLIC_QUERY_PURPOSES)
@pytest.mark.parametrize("mode", ["commit_before", "persistent"])
async def test_public_query_return_keeps_original_application_after_save_fault(tmp_path, call_case, purpose, mode):
    case = call_case
    runtime = await _query_consumer(case, tmp_path, purpose)
    calls_before = len(case.query.calls)
    responsibility = (case.boundary if purpose is QueryPurpose.STOP_CONFIRMATION else None)
    case.fault.arm(case.query, mode, responsibility=responsibility)
    with pytest.raises(SavedCallBoundary if mode == "commit_before" else ConsistencyError):
        await capture_handler("camera_record")(case.action_id, runtime)
    request = case.query.calls[-1]
    ticket = request.ticket
    call_count = calls_before + (2 if purpose is QueryPurpose.STOP_CONFIRMATION else 1)
    assert len(case.query.calls) == call_count and case.fault.injected
    assert request.timeout_s == Decimal("0.5")
    assert ticket.target_id == (None if purpose is QueryPurpose.BEFORE_EXECUTION else str(case.activity_id))
    assert case.owned.connection.execute("SELECT action_id,activity_id,query_purpose FROM operation_runs WHERE id=?",
        (ticket.run_id,)).fetchone() == (case.action_id,
            None if purpose is QueryPurpose.BEFORE_EXECUTION else case.activity_id,
            int(enum_for("operation_runs.query_purpose")[purpose.name]))
    original = _for_ticket(case, ticket)
    assert len(original) == 2 and original[0] == original[1]
    if mode == "persistent":
        assert case.owned.connection.execute("SELECT status,result_event_id FROM operation_attempts WHERE run_id=?",
            (ticket.run_id,)).fetchone() == (1, None)
        case.fault.enabled = False
        case.clock.wall += 5_000_000
        with pytest.raises(SavedCallBoundary):
            await capture_handler("camera_record")(case.action_id, await case.restart())
    _assert_actual_saved(case.owned, ticket, assumed=True, confirmed=False, occurred_at=_RETURNED_AT)
    assert len(case.query.calls) == call_count
    assert all(finish == original[0][0] and key == original[0][1]
        and finish.outcome.outcome is case.query.actual[-1]
        and finish.outcome.outcome.error is case.query.actual[-1].error
        for finish, key in _for_ticket(case, ticket))
    assert set(case.fault.result_keys) == {str(original[0][1])}


@pytest.mark.parametrize("operation", ["control"])
async def test_start_return_is_held_before_first_action_read(tmp_path, call_case, operation):
    case = call_case
    actual = _result(confirmed=True, error=ErrorValue("transport_timeout", "transport"))
    runtime = await _call_world(case, tmp_path, control=actual)
    case.clock.wall, case.clock.monotonic = _RETURNED_AT, 8_000_000_000
    case.boundary = "control"
    case.fault.arm(case.control, "action_read")
    with pytest.raises((sqlite3.Error, ConsistencyError)):
        await capture_handler("camera_record")(case.action_id, runtime)
    request, = case.control.calls
    assert case.owned.connection.execute("SELECT status,result_event_id FROM operation_attempts WHERE run_id=?",
        (request.ticket.run_id,)).fetchone() == (1, None)
    case.fault.enabled = False
    case.clock.wall += 5_000_000
    case.clock.monotonic = 13_000_000_000
    with pytest.raises(SavedCallBoundary):
        await capture_handler("camera_record")(case.action_id, await case.view())
    assert case.owned.connection.execute("SELECT started_at FROM device_activities WHERE action_id=?",
        (case.action_id,)).fetchone() == (_RETURNED_AT,)
    assert len(case.control.calls) == 1 and len(set(case.fault.result_keys)) == 1
    saved = _for_ticket(case, request.ticket)
    assert saved and all(finish.outcome.outcome is case.control.actual[0]
        and finish.outcome.outcome.error is case.control.actual[0].error
        and finish.occurred_at == _RETURNED_AT for finish, _ in saved)
    case.boundary = "stop"
    case.clock.monotonic = 67_999_999_999
    await capture_handler("camera_record")(case.action_id, await case.view())
    assert case.stop.calls == []
    case.clock.monotonic = 68_000_000_000
    with pytest.raises(SavedCallBoundary):
        await capture_handler("camera_record")(case.action_id, await case.view())
    assert len(case.stop.calls) == 1 and len(case.control.calls) == 1


async def test_query_confirmation_writes_original_time_and_start_facts(tmp_path, call_case):
    case = call_case
    ticket = await _query_storage(case, tmp_path, QueryPurpose.START_CONFIRMATION)
    case.query.outcome = _outcome("query", confirmed=True, activity_id=ticket.target_id)
    finish = replace(await _query_finish(case, ticket), retry_wait=False,
        run_finish=RunFinish(RunOutcome.SUCCEEDED))
    observation = ActivityObservationSave(case.action_id, _RETURNED_AT, started_at=_RETURNED_AT, activity_state=2)
    key = new_operation_key()
    saved = await case.database.write(lambda owned: CaptureRepository().finish_start_result(
        finish, observation, key, owned, start_finish=StaleRunFinish(
            (f"start/{case.action_id}",), RunOutcome.SUCCEEDED, _RETURNED_AT)), key=key)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    assert case.owned.connection.execute("SELECT started_at FROM device_activities WHERE id=?",
        (case.activity_id,)).fetchone() == (_RETURNED_AT,)
    _assert_actual_saved(case.owned, ticket, assumed=False, confirmed=True,
        occurred_at=_RETURNED_AT, activity_id=ticket.target_id)
    assert case.owned.connection.execute("SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
        (key,)).fetchone() == (1,)
    assert len(case.query.calls) == 1 and case.query.calls[0].ticket == ticket


async def test_encoding_failure_keeps_original_bytes_ticket_key_and_time(tmp_path, monkeypatch, call_case):
    from camctl.operations import result_format
    from camctl.operations.tool_diagnostics import ResultEncodingError
    case = call_case
    runtime = await _call_started(case, tmp_path)
    case.clock.wall, case.clock.monotonic = _RETURNED_AT, 67_000_000_000
    case.boundary = "stop"
    case.fault.arm(case.stop, "observe")
    encode, count = result_format.encode_diagnostics, 0
    def failing(value):
        nonlocal count
        count += 1
        if count == 1:
            raise ResultEncodingError("诊断编码注入失败")
        return encode(value)
    monkeypatch.setattr(result_format, "encode_diagnostics", failing)
    with pytest.raises(ConsistencyError):
        await capture_handler("camera_record")(case.action_id, runtime)
    request, = case.stop.calls
    original, key = _for_ticket(case, request.ticket)[0]
    assert count == 1 and original.outcome.outcome.diagnostics.calls[0].stdout.data == b"\x00\xff"
    assert original.outcome.outcome is case.stop.actual[0]
    assert original.outcome.outcome.error is case.stop.actual[0].error
    assert original.occurred_at == _RETURNED_AT
    case.clock.wall += 99
    with pytest.raises(SavedCallBoundary):
        await capture_handler("camera_record")(case.action_id, await case.view())
    assert len(case.stop.calls) == 1 and set(case.fault.result_keys) == {str(key)}
    row = case.owned.connection.execute("SELECT a.result_json,e.occurred_at,t.operation_key"
        " FROM operation_attempts a JOIN history_events e ON e.id=a.result_event_id"
        " JOIN history_transactions t ON t.id=e.transaction_id WHERE a.run_id=?", (request.ticket.run_id,)).fetchone()
    assert json.loads(row[0])["diagnostics"]["calls"][0]["stdout"]["hex"] == "00ff"
    assert row[1:] == (_RETURNED_AT, str(key))


@pytest.mark.parametrize("operation", ["control", "stop"])
async def test_diagnostic_validation_failure_holds_original_at_both_capture_exits(tmp_path, monkeypatch, call_case, operation):
    from camctl.operations import tool_diagnostics
    case = call_case
    if operation == "control":
        actual = replace(_result(confirmed=True, error=ErrorValue("transport_timeout", "transport")),
            diagnostics=_diagnosed_outcome("1").diagnostics)
        runtime = await _call_world(case, tmp_path, control=actual)
    else:
        runtime = await _call_started(case, tmp_path)
    port = case.control if operation == "control" else case.stop
    case.boundary = operation
    case.clock.wall, case.clock.monotonic = _RETURNED_AT, (8_000_000_000 if operation == "control" else 67_000_000_000)
    case.fault.arm(port, "observe")
    original, count = tool_diagnostics.validate_diagnostics, 0
    def temporary(value):
        nonlocal count
        count += 1
        if count == 1:
            raise ValueError("原合法诊断的一次临时校验故障")
        return original(value)
    monkeypatch.setattr(tool_diagnostics, "validate_diagnostics", temporary)
    with pytest.raises(ConsistencyError, match="诊断校验失败"):
        await capture_handler("camera_record")(case.action_id, runtime)
    request, = port.calls
    assert count == 1 and port.actual[0].diagnostics.calls[0].stdout.data == b"\x00\xff"
    assert case.owned.connection.execute("SELECT status,result_event_id FROM operation_attempts WHERE run_id=?",
        (request.ticket.run_id,)).fetchone() == (1, None)
    case.clock.wall += 5_000_000
    with pytest.raises(SavedCallBoundary):
        await capture_handler("camera_record")(case.action_id, await case.view())
    assert len(port.calls) == 1 and len(set(case.fault.result_keys)) == 1
    saves = _for_ticket(case, request.ticket)
    assert saves and all(finish.outcome.outcome is port.actual[0]
        and finish.outcome.outcome.error is port.actual[0].error
        for finish, _ in saves)
    result, occurred_at, saved_key = case.owned.connection.execute(
        "SELECT a.result_json,e.occurred_at,t.operation_key FROM operation_attempts a"
        " JOIN history_events e ON e.id=a.result_event_id JOIN history_transactions t ON t.id=e.transaction_id"
        " WHERE a.run_id=?", (request.ticket.run_id,)).fetchone()
    assert json.loads(result)["diagnostics"]["calls"][0]["stdout"]["hex"] == "00ff"
    assert occurred_at == _RETURNED_AT and saved_key == case.fault.result_keys[0]


async def test_general_invalid_stop_observation_still_uses_existing_rejection(tmp_path, call_case):
    case = call_case
    runtime = await _call_started(case, tmp_path)
    actual = _outcome("stop", confirmed=True, activity_id=str(case.activity_id))
    case.stop.outcome = replace(actual, observations=(replace(actual.observations[0], type="unregistered"),))
    case.clock.wall, case.clock.monotonic = _RETURNED_AT, 67_000_000_000
    await capture_handler("camera_record")(case.action_id, runtime)
    error, = case.owned.connection.execute("SELECT a.error_json FROM operation_attempts a"
        " JOIN operation_runs r ON r.id=a.run_id WHERE r.responsibility_key=?", (f"stop/{case.action_id}",)).fetchone()
    assert json.loads(error)["code"] == "invalid_device_result" and len(case.stop.calls) == 1
    assert case.owned.connection.execute("SELECT status,result_event_id IS NOT NULL FROM operation_attempts"
        " WHERE run_id=?", (case.stop.calls[0].ticket.run_id,)).fetchone() == (3, 1)

"""普通 STOP 与五种 QUERY 的原结果、原时刻和原结果事务身份。

STOP 通过实际处理器返回；QUERY 在合法原流程下验证公共 finish 的
保存责任，尚不代替各用途的真实业务消费者验收。
"""

from dataclasses import replace
from decimal import Decimal
import json
import sqlite3

import pytest

from camctl.capture.handlers import _settle_stop_exhausted, _stop_call, capture_handler
from camctl.capture.residual import ResidualCandidate, _stop_residual
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import ControlRequest, DeviceBinding, DeviceCallResult
from camctl.operations.attempts import (
    AttemptConfig, AttemptIntent, AttemptTarget, BeginDisposition, OperationKind,
    QueryPurpose,
)
from camctl.operations.models import (
    AttemptStatus, CallInfo, CallOutcome, EffectState, ErrorValue, EvidenceValue,
    Settlement, SettlementBasis,
)
from camctl.persistence.models import DbOutcomeKind

from .test_capture_contract import _NOW, _seed_action
from .test_recording_start_runtime import StartDriver, configured, _configured, _result, _world
from .test_recording_start_save_boundary import ResultWriteFault
from ..scheduling.test_resources import _seed_activity

pytestmark = pytest.mark.asyncio

_RETURNED_AT = _NOW + 2_000_000
_CALL_EVIDENCE = EvidenceRegistry((
    EvidenceContract("operation_returned", 1, "control", frozenset()),
    EvidenceContract("start_confirmed", 1, "control", frozenset({"activity_id"}),
                     identity_field="activity_id"),
    EvidenceContract("stop_returned", 1, "stop", frozenset()),
    EvidenceContract("stop_confirmed", 1, "stop", frozenset({"activity_id"}),
                     identity_field="activity_id"),
    EvidenceContract("query_returned", 1, "query", frozenset()),
    EvidenceContract("activity_status", 1, "query", frozenset({"activity_id"}),
                     identity_field="activity_id"),
    EvidenceContract("adb_foreground_assumption", 1, "query",
                     frozenset({"terminate_grace_s"})),
))


def _outcome(operation, *, assumed=False, confirmed=False):
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
            1, {"activity_id": "12"}),) if confirmed else (),
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


class CallWriteFault(ResultWriteFault):
    """只在实际调用返回后的尝试投影或 COMMIT 边界注入错误。"""

    @property
    def enabled(self):
        return self.state["enabled"]

    @enabled.setter
    def enabled(self, value):
        self.state["enabled"] = value

    def execute(self, statement, params=()):
        returned = bool(self.driver.calls)
        if (returned and self.armed and self.mode == "read"
                and statement.startswith("SELECT") and "FROM operation_runs" in statement):
            self.armed = False
            raise sqlite3.OperationalError("实际调用返回后的原流程读取失败")
        if (self.state["enabled"] and returned and self.mode == "persistent"
                and statement == "COMMIT"):
            raise sqlite3.OperationalError("原结果提交暂时不能可靠核实")
        if (returned and self.armed and self.mode == "projection"
                and statement.startswith("UPDATE operation_attempts SET")):
            self.armed = False
            raise sqlite3.OperationalError("原结果的尝试投影写入失败")
        return super().execute(statement, params)


async def _started(owned):
    runtime = _configured(owned, StartDriver(_result(confirmed=True)))
    runtime.evidence = _CALL_EVIDENCE
    await capture_handler("camera_record")(12, runtime)
    runtime.stop_config = AttemptConfig(1, Decimal("0.125"), Decimal("1"))
    return runtime


def _assert_actual_saved(owned, ticket, *, assumed, confirmed, occurred_at):
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
        "version": 1, "data": {"activity_id": "12"}}] if confirmed else [])
    assert json.loads(row[3]) == {
        "code": "transport_timeout", "stage": "transport", "details": {"timeout_s": 0.125}}
    assert row[4] == occurred_at
    assert owned.connection.execute(
        "SELECT attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (1,)


@pytest.mark.parametrize("mode", ["read", "projection", "commit_before", "commit_after"])
async def test_stop_return_is_resaved_with_original_key_without_second_device_call(tmp_path, mode, configured, monkeypatch):
    owned = _world(tmp_path)
    try:
        before_result = []
        stopper = ReturnedCall(None, returned=lambda: before_result.append(
            tuple(owned.connection.iterdump())))
        state = {"armed": True, "enabled": True, "result_keys": [], "rolled_back_images": []}
        def wrap(connection):
            return replace(connection, connection=CallWriteFault(
                connection.connection, stopper, mode, state))
        runtime = await configured(owned, StartDriver(_result(confirmed=True)), connection_wrap=wrap)
        runtime.evidence = _CALL_EVIDENCE
        await capture_handler("camera_record")(12, runtime)
        runtime.stop_config = AttemptConfig(1, Decimal("0.125"), Decimal("1"))
        stopper.outcome = _diagnosed_stop(runtime)
        saved_keys = []
        save = runtime.capture.finish_capture_stop
        def observe(finish, key, connection):
            saved_keys.append(key)
            return save(finish, key, connection)
        monkeypatch.setattr(runtime.capture, "finish_capture_stop", observe)
        runtime.stopper = stopper
        runtime.wall_us = lambda: _RETURNED_AT
        runtime.monotonic_ns = lambda: 68_000_000_000
        await capture_handler("camera_record")(12, runtime)
        request, = stopper.calls
        assert request.timeout_s == Decimal("0.125")
        _assert_actual_saved(owned, request.ticket, assumed=False, confirmed=True,
                             occurred_at=_RETURNED_AT)
        assert saved_keys and len(set(saved_keys)) == 1
        key, = owned.connection.execute(
            "SELECT t.operation_key FROM operation_attempts a"
            " JOIN history_events e ON e.id=a.result_event_id"
            " JOIN history_transactions t ON t.id=e.transaction_id WHERE a.run_id=?",
            (request.ticket.run_id,)).fetchone()
        assert key == str(saved_keys[0])
        result_json, = owned.connection.execute("SELECT result_json FROM operation_attempts WHERE run_id=?", (request.ticket.run_id,)).fetchone()
        assert json.loads(result_json)["diagnostics"]["calls"][0]["stdout"]["hex"] == "00ff"
        if mode in ("projection", "commit_before"):
            assert len(before_result) == 1
            assert state["rolled_back_images"][0] == before_result[0]
    finally:
        owned.connection.close()


async def test_stop_pending_actual_result_precedes_unknown_recovery_without_boundary(tmp_path, configured, monkeypatch):
    owned = _world(tmp_path)
    try:
        stopper = ReturnedCall(None)
        state = {"armed": True, "enabled": True, "result_keys": [], "rolled_back_images": []}
        def wrap(connection):
            return replace(connection, connection=CallWriteFault(
                connection.connection, stopper, "persistent", state))
        runtime = await configured(owned, StartDriver(_result(confirmed=True)), connection_wrap=wrap)
        runtime.evidence = _CALL_EVIDENCE
        await capture_handler("camera_record")(12, runtime)
        runtime.stop_config = AttemptConfig(1, Decimal("0.125"), Decimal("1"))
        stopper.outcome = _diagnosed_stop(runtime)
        saved_keys = []
        save = runtime.capture.finish_capture_stop
        def observe(finish, key, connection):
            saved_keys.append(key)
            return save(finish, key, connection)
        monkeypatch.setattr(runtime.capture, "finish_capture_stop", observe)
        runtime.stopper = stopper
        runtime.wall_us = lambda: _RETURNED_AT
        runtime.monotonic_ns = lambda: 68_000_000_000
        with pytest.raises(ConsistencyError):
            await capture_handler("camera_record")(12, runtime)
        request, = stopper.calls
        assert owned.connection.execute(
            "SELECT status, result_event_id FROM operation_attempts"
            " WHERE run_id=?", (request.ticket.run_id,)).fetchone() == (1, None)
        state["enabled"] = False
        runtime.wall_us = lambda: _RETURNED_AT + 5_000_000
        await capture_handler("camera_record")(12, runtime)
        _assert_actual_saved(owned, request.ticket, assumed=False, confirmed=True,
                             occurred_at=_RETURNED_AT)
        assert len(stopper.calls) == 1
        assert len(saved_keys) == 3 and len(set(saved_keys)) == 1
        key, = owned.connection.execute(
            "SELECT t.operation_key FROM operation_attempts a"
            " JOIN history_events e ON e.id=a.result_event_id"
            " JOIN history_transactions t ON t.id=e.transaction_id WHERE a.run_id=?",
            (request.ticket.run_id,)).fetchone()
        assert key == str(saved_keys[0])
    finally:
        owned.connection.close()


async def _query_context(owned, purpose):
    """用真实原结果形成五种用途的固定关联，不制造外部活动。"""
    if purpose is QueryPurpose.BEFORE_EXECUTION:
        runtime = _configured(owned, StartDriver(_result()))
    elif purpose is QueryPurpose.START_CONFIRMATION:
        runtime = _configured(owned, StartDriver(_result()))
        await capture_handler("camera_record")(12, runtime)
    else:
        runtime = await _started(owned)
    runtime.evidence = _CALL_EVIDENCE
    action_id, activity_id = 12, None if purpose is QueryPurpose.BEFORE_EXECUTION else 12
    if purpose in (QueryPurpose.STOP_CONFIRMATION, QueryPurpose.RESIDUAL_STOP_CONFIRMATION):
        runtime.stopper = ReturnedCall(_outcome("stop"))
        await _stop_call(runtime, runtime.action(12))
    if purpose is QueryPurpose.RESIDUAL_STOP_CONFIRMATION:
        _settle_stop_exhausted(runtime, runtime.action(12))
        assert runtime.action(12)["status"] == 4
        _seed_action(owned.connection, 13, 2)
        _seed_activity(owned.connection, 13)
        owned.connection.execute(
            "UPDATE actions SET input_fields_json=?, effective_params_json=?,"
            " max_delay_ms=60000 WHERE id=13",
            (json.dumps({"params": {"type": "ordinary", "duration_s": 60},
                         "policy": {"max_delay_ms": 60000}}),
             json.dumps({"type": "ordinary", "duration_s": 60})))
        await _stop_residual(runtime, 13, ResidualCandidate(12, 12, 1, 1))
        action_id = 13
    begin = runtime.operations.begin_attempt(AttemptIntent(
        operation="query", action_id=action_id, kind=OperationKind.QUERY_ACTIVITY,
        target=AttemptTarget(activity_id=activity_id), query_purpose=purpose,
        config=AttemptConfig(2, Decimal("0.125"), Decimal("1")), occurred_at=_NOW),
        new_operation_key(), owned)
    assert begin.kind is DbOutcomeKind.COMPLETED, begin.error
    assert begin.value.disposition is BeginDisposition.GRANTED
    return runtime, begin.value.ticket


@pytest.mark.parametrize("purpose", list(QueryPurpose))
async def test_query_return_uses_original_key_after_commit_before_error(tmp_path, purpose):
    owned = _world(tmp_path)
    try:
        runtime, ticket = await _query_context(owned, purpose)
        query = ReturnedCall(_outcome("query", assumed=True))
        proxy = CallWriteFault(owned.connection, query, "commit_before")
        runtime.owned = replace(owned, connection=proxy)
        response = await query.query_state(ControlRequest(
            "query_state", DeviceBinding("cam-1", "camctl-adb"), {},
            ticket=ticket, timeout_s=Decimal("0.125")))
        runtime.finish(ticket, response.outcome, occurred_at=_RETURNED_AT, retry_wait=True)
        _assert_actual_saved(owned, ticket, assumed=True, confirmed=False,
                             occurred_at=_RETURNED_AT)
        assert len(query.calls) == 1
        assert len(set(proxy.result_keys)) == 1
    finally:
        owned.connection.close()


@pytest.mark.parametrize("purpose", list(QueryPurpose))
async def test_query_pending_actual_result_precedes_unknown_recovery_without_boundary(tmp_path, purpose):
    owned = _world(tmp_path)
    try:
        runtime, ticket = await _query_context(owned, purpose)
        query = ReturnedCall(_outcome("query", assumed=True))
        proxy = CallWriteFault(owned.connection, query, "persistent")
        runtime.owned = replace(owned, connection=proxy)
        response = await query.query_state(ControlRequest(
            "query_state", DeviceBinding("cam-1", "camctl-adb"), {},
            ticket=ticket, timeout_s=Decimal("0.125")))
        with pytest.raises(ConsistencyError):
            runtime.finish(ticket, response.outcome, occurred_at=_RETURNED_AT, retry_wait=True)
        proxy.enabled = False
        runtime.wall_us = lambda: _RETURNED_AT + 5_000_000
        assert runtime.recover_attempt(ticket)
        _assert_actual_saved(owned, ticket, assumed=True, confirmed=False,
                             occurred_at=_RETURNED_AT)
        assert len(query.calls) == 1
        assert len(set(proxy.result_keys)) == 1
    finally:
        owned.connection.close()


@pytest.mark.parametrize("operation", ["control", "query"])
async def test_start_return_is_held_before_first_action_read(tmp_path, operation, configured, monkeypatch):
    """第一次派生读取失败不能丢失START或启动核实实际返回。"""
    owned = _world(tmp_path)
    try:
        actual = (_result(confirmed=True, error=ErrorValue("transport_timeout", "transport"))
                  if operation == "control" else _outcome("query", confirmed=True))
        driver = StartDriver(actual if operation == "control" else _result())
        returned = driver
        if operation == "query":
            returned = ReturnedCall(actual)

        class ActionReadFault(ResultWriteFault):
            def execute(self, statement, params=()):
                if (self.state["enabled"] and self.driver.calls and statement.startswith("SELECT")
                        and "FROM actions" in statement):
                    raise sqlite3.OperationalError("调用返回后的动作资格暂不能读取")
                return super().execute(statement, params)

        state = {"armed": True, "enabled": True, "result_keys": [], "rolled_back_images": []}
        def wrap(connection):
            return replace(connection, connection=ActionReadFault(
                connection.connection, returned, "read", state))
        runtime = await configured(owned, driver, connection_wrap=wrap)
        runtime.evidence = _CALL_EVIDENCE
        if operation == "query":
            await capture_handler("camera_record")(12, runtime)
            runtime.state_query = returned
        saved_requests = []
        save = runtime.capture.finish_start_result
        def observe(finish, observation, key, connection, **kwargs):
            saved_requests.append((finish, observation, key, kwargs))
            return save(finish, observation, key, connection, **kwargs)
        monkeypatch.setattr(runtime.capture, "finish_start_result", observe)
        runtime.wall_us = lambda: _RETURNED_AT
        runtime.monotonic_ns = lambda: 8_000_000_000
        with pytest.raises((sqlite3.Error, ConsistencyError)):
            await capture_handler("camera_record")(12, runtime)
        ticket = returned.calls[0].ticket
        assert owned.connection.execute(
            "SELECT status,result_event_id FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone() == (1, None)
        state["enabled"] = False
        runtime.wall_us = lambda: _RETURNED_AT + 5_000_000
        runtime.monotonic_ns = lambda: 13_000_000_000
        await capture_handler("camera_record")(12, runtime)
        assert len(saved_requests) == 1
        finish, _, original_key, _ = saved_requests[0]
        assert finish.ticket == ticket and finish.outcome.outcome == actual
        assert finish.occurred_at == _RETURNED_AT
        assert owned.connection.execute(
            "SELECT started_at FROM device_activities WHERE action_id=12"
        ).fetchone() == (_RETURNED_AT,)
        assert (await runtime.recording_state.recording_state(12)).stop_target_ns == 68_000_000_000
        assert len(returned.calls) == 1
        assert set(state["result_keys"]) == {str(original_key)}
    finally:
        owned.connection.close()


def _diagnosed_stop(runtime):
    from camctl.operations.tool_diagnostics import ToolDiagnostics, ToolCallOutput, ToolStream, ToolStreamState
    value = ToolDiagnostics(2, (ToolCallOutput(1, "stop", ToolStream(ToolStreamState.COMPLETE, b"\x00\xff"), ToolStream(ToolStreamState.COMPLETE, b"")),))
    runtime.evidence = runtime.evidence.with_contract(replace(runtime.evidence.contract("stop_returned", 1), max_diagnostic_calls=2))
    return replace(_outcome("stop", confirmed=True), diagnostics=value)


async def test_encoding_failure_keeps_original_bytes_ticket_key_and_time(tmp_path, monkeypatch, configured):
    from camctl.operations import result_format
    from camctl.operations.tool_diagnostics import ResultEncodingError
    owned = _world(tmp_path)
    try:
        runtime = await configured(owned, StartDriver(_result(confirmed=True)))
        runtime.evidence = _CALL_EVIDENCE
        await capture_handler("camera_record")(12, runtime)
        runtime.stop_config = AttemptConfig(1, Decimal("0.125"), Decimal("1"))
        actual = _diagnosed_stop(runtime)
        stopper = ReturnedCall(actual)
        runtime.stopper = stopper
        runtime.wall_us = lambda: _RETURNED_AT
        runtime.monotonic_ns = lambda: 68_000_000_000
        encode = result_format.encode_diagnostics
        count = 0
        def failing(value):
            nonlocal count
            count += 1
            if count == 1:
                raise ResultEncodingError("诊断编码注入失败")
            return encode(value)
        monkeypatch.setattr(result_format, "encode_diagnostics", failing)
        with pytest.raises(ConsistencyError):
            await capture_handler("camera_record")(12, runtime)
        assert count == 1
        ticket = stopper.calls[0].ticket
        requests = []
        save = runtime.capture.finish_capture_stop
        def observe(finish, key, connection):
            requests.append((finish, key))
            return save(finish, key, connection)
        monkeypatch.setattr(runtime.capture, "finish_capture_stop", observe)
        assert actual.diagnostics.calls[0].stdout.data == b"\x00\xff"
        runtime.wall_us = lambda: _RETURNED_AT + 99
        await capture_handler("camera_record")(12, runtime)
        finish, original_key = requests[0]
        assert finish.outcome.outcome == actual and finish.occurred_at == _RETURNED_AT
        assert len(stopper.calls) == 1
        row = owned.connection.execute("SELECT a.result_json,e.occurred_at,t.operation_key FROM operation_attempts a JOIN history_events e ON e.id=a.result_event_id JOIN history_transactions t ON e.id BETWEEN t.first_event_id AND t.last_event_id WHERE a.run_id=?", (ticket.run_id,)).fetchone()
        assert json.loads(row[0])["diagnostics"]["calls"][0]["stdout"]["hex"] == "00ff"
        assert row[1] == _RETURNED_AT
        assert row[2] == str(original_key)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("operation", ["control", "stop"])
async def test_diagnostic_validation_failure_holds_original_at_both_capture_exits(tmp_path, monkeypatch, operation, configured):
    """录像 hold 与 STOP finish 在校验器故障前均持有原失败和字节。"""
    from camctl.operations import tool_diagnostics
    owned = _world(tmp_path)
    try:
        runtime = await configured(owned, StartDriver(_result(confirmed=True)))
        runtime.evidence = _CALL_EVIDENCE
        if operation == "stop":
            await capture_handler("camera_record")(12, runtime)
            runtime.stop_config = AttemptConfig(1, Decimal("0.125"), Decimal("1"))
        diagnosed = _diagnosed_stop(runtime)
        actual = (replace(_result(confirmed=True, error=ErrorValue("transport_timeout", "transport")),
                          diagnostics=diagnosed.diagnostics) if operation == "control" else diagnosed)
        if operation == "control":
            runtime.evidence = runtime.evidence.with_contract(replace(
                runtime.evidence.contract("operation_returned", 1), max_diagnostic_calls=2))
            returned = StartDriver(actual)
            runtime.driver = returned
        else:
            returned = ReturnedCall(actual)
            runtime.stopper = returned
        original = tool_diagnostics.validate_diagnostics
        count = 0
        def temporary(value):
            nonlocal count
            count += 1
            if count == 1:
                raise ValueError("原合法诊断的一次临时校验故障")
            return original(value)
        monkeypatch.setattr(tool_diagnostics, "validate_diagnostics", temporary)
        runtime.wall_us = lambda: _RETURNED_AT
        returned_ns = 8_000_000_000 if operation == "control" else 68_000_000_000
        runtime.monotonic_ns = lambda: returned_ns
        with pytest.raises(ConsistencyError, match="诊断校验失败"):
            await capture_handler("camera_record")(12, runtime)
        assert count == 1
        ticket = returned.calls[0].ticket
        requests = []
        method = "finish_start_result" if operation == "control" else "finish_capture_stop"
        save = getattr(runtime.capture, method)
        def observe(*args, **kwargs):
            requests.append((args, kwargs))
            return save(*args, **kwargs)
        monkeypatch.setattr(runtime.capture, method, observe)
        assert actual.diagnostics.calls[0].stdout.data == b"\x00\xff"
        await capture_handler("camera_record")(12, runtime)
        args, _ = requests[0]
        finish = args[0]
        key = args[2] if operation == "control" else args[1]
        assert finish.outcome.outcome == actual and finish.occurred_at == _RETURNED_AT
        assert len(returned.calls) == 1
        saved, occurred_at, saved_key = owned.connection.execute(
            "SELECT a.result_json,e.occurred_at,t.operation_key FROM operation_attempts a"
            " JOIN history_events e ON e.id=a.result_event_id"
            " JOIN history_transactions t ON e.id BETWEEN t.first_event_id AND t.last_event_id WHERE a.run_id=?",
            (ticket.run_id,)).fetchone()
        assert json.loads(saved)["diagnostics"]["calls"][0]["stdout"]["hex"] == "00ff"
        assert occurred_at == _RETURNED_AT
        assert saved_key == str(key)
    finally:
        owned.connection.close()


async def test_general_invalid_stop_observation_still_uses_existing_rejection(tmp_path, configured):
    owned = _world(tmp_path)
    try:
        runtime = await configured(owned, StartDriver(_result(confirmed=True)))
        runtime.evidence = _CALL_EVIDENCE
        await capture_handler("camera_record")(12, runtime)
        runtime.stop_config = AttemptConfig(1, Decimal("0.125"), Decimal("1"))
        actual = _outcome("stop", confirmed=True)
        actual = replace(actual, observations=(replace(actual.observations[0], type="unregistered"),))
        stopper = ReturnedCall(actual)
        runtime.stopper = stopper
        runtime.monotonic_ns = lambda: 68_000_000_000
        await capture_handler("camera_record")(12, runtime)
        error, = owned.connection.execute(
            "SELECT a.error_json FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
            " WHERE r.responsibility_key='stop/12'").fetchone()
        assert json.loads(error)["code"] == "invalid_device_result"
        assert len(stopper.calls) == 1
    finally:
        owned.connection.close()

"""停止端口确认同时携带调用错误时，沿各调用方的停止事实保存。

可靠停止观察与调用错误并存：尝试保存 FAILED 与 CONFIRMED 效果
及完整错误依据，停止责任按停止事实完成，活动结束与条件释放同
事务保存，不追加停止或无必要查询。调用方沿返回的确认阶段按各
自规则继续。
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from camctl.capture.handlers import (
    SessionRecordingState, advance_winddown, capture_handler,
)
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import DeviceCallResult
from camctl.operations.attempts import AttemptConfig, RunOutcome
from camctl.operations.models import (
    AttemptStatus, CallInfo, CallOutcome, EffectState, ErrorValue, EvidenceValue,
    Settlement, SettlementBasis,
)
from camctl.persistence.transaction import row_facts

from .test_call_result_save import _CALL_EVIDENCE, _started
from .test_capture_contract import _NOW, _environment, _runtime
from .test_recording_start_runtime import (
    _ANCHOR, StartDriver, _configured, _result, _world,
)

pytestmark = pytest.mark.asyncio

_STOP_ERROR = {
    "code": "transport_timeout", "stage": "transport",
    "details": {"response_received": True},
}


def _stop_confirmed_with_error(activity_id: str) -> DeviceCallResult:
    """可靠停止确认同时携带完整调用错误的正式结果。"""
    return DeviceCallResult.from_outcome(CallOutcome(
        status=AttemptStatus.FAILED,
        error=ErrorValue(_STOP_ERROR["code"], _STOP_ERROR["stage"],
                         _STOP_ERROR["details"]),
        effect=EffectState.CONFIRMED,
        settlement=Settlement(
            SettlementBasis.OBSERVED, EvidenceValue("stop_returned", 1, {})),
        observations=(DeviceObservation("stop_confirmed", 1,
                                        {"activity_id": activity_id}),),
        call_info=CallInfo(local_exit_code=0),
    ))


class StopWithError:
    """停止端口替身：返回确认同时带错误的完整结果。"""

    def __init__(self, activity_id: str = "12") -> None:
        self.activity_id = activity_id
        self.calls = []

    async def stop(self, request):
        self.calls.append(request.operation)
        return _stop_confirmed_with_error(self.activity_id)


def _stop_run(owned):
    return owned.connection.execute(
        "SELECT id, status, attempts_used FROM operation_runs"
        " WHERE responsibility_key = 'stop/12'").fetchone()


def _stop_attempt(owned):
    run_id = _stop_run(owned)[0]
    return owned.connection.execute(
        "SELECT status, effect_state, error_json, result_json"
        " FROM operation_attempts WHERE run_id = ?", (run_id,)).fetchone()


def _activity_id(owned, action_id=12):
    return owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id = ?",
        (action_id,)).fetchone()[0]


def _assert_stop_fact(owned, action_id=12):
    """确认带错误的停止按停止事实完成：FAILED 尝试保留完整依据。

    停止流程以 SUCCEEDED 收场（run status 3，预算一次用尽），
    尝试保持 FAILED（3）与 CONFIRMED（3）效果及原错误。
    """
    run_id, run_status, used = _stop_run(owned)
    assert (run_status, used) == (3, 1)
    status, effect, error_json, result_json = _stop_attempt(owned)
    assert (status, effect) == (3, 3)
    assert json.loads(error_json) == _STOP_ERROR
    assert json.loads(result_json)["settlement"] == {
        "basis": "observed",
        "evidence": {"type": "stop_returned", "version": 1, "data": {}}}
    activity = row_facts(owned.connection, "device_activities",
                         _activity_id(owned, action_id))
    assert activity["activity_state"] == 3
    return run_id, activity["id"]


def _assert_settle_transactions(owned, run_id, activity_id):
    """停止结果、流程结束与活动观察必须同事务提交。"""
    attempt_ids = {row[0] for row in owned.connection.execute(
        "SELECT id FROM operation_attempts WHERE run_id = ?", (run_id,))}
    transactions = {"run": set(), "attempt": set(), "activity": set()}
    for transaction_id, body in owned.connection.execute(
            "SELECT transaction_id, body_json FROM history_events ORDER BY id"):
        for row in json.loads(body)["rows"]:
            values = row["after"]["values"]
            if row["table"] == "operation_runs" and row["id"] == run_id \
                    and values.get("status") == 3:
                transactions["run"].add(transaction_id)
            if row["table"] == "operation_attempts" and row["id"] in attempt_ids \
                    and values.get("effect_state") == 3:
                transactions["attempt"].add(transaction_id)
            if row["table"] == "device_activities" and row["id"] == activity_id \
                    and values.get("activity_state") == 3:
                transactions["activity"].add(transaction_id)
    assert len(transactions["run"]) == 1
    assert transactions["attempt"] <= transactions["run"]
    assert transactions["activity"] <= transactions["run"]


async def test_normal_record_stop_confirmed_with_error_completes_stop_fact(tmp_path):
    """正常录像路径：确认带错误时停止责任按停止事实完成。"""
    owned = _world(tmp_path)
    try:
        runtime = _configured(owned, StartDriver(_result(confirmed=True)))
        runtime.evidence = _CALL_EVIDENCE
        await _started_driver_ready(owned, runtime)
        stopper = StopWithError()
        runtime.stopper = stopper
        await capture_handler("camera_record")(12, runtime)

        # 可靠停止事实被录像状态机识别：不重新停止、不补发。
        assert stopper.calls == ["stop_recording"]
        assert runtime.recording_state.recording_state(12).stop_confirmed
        run_id, activity_id = _assert_stop_fact(owned)
        # 普通录像停止后释放占用。
        assert row_facts(owned.connection, "device_activities",
                         activity_id)["occupancy_state"] == 2
        _assert_settle_transactions(owned, run_id, activity_id)
    finally:
        owned.connection.close()


async def _started_driver_ready(owned, runtime):
    """真实确认启动后到达停止时点；沿用 _started 的世界与预算。"""
    await capture_handler("camera_record")(12, runtime)
    runtime.stop_config = AttemptConfig(1, Decimal("0.125"), Decimal("1"))
    runtime.wall_us = lambda: _NOW + 61_000_000
    runtime.monotonic_ns = lambda: _ANCHOR + 61_000_000_000
    return runtime


async def test_reconcile_entry_stop_confirmed_with_error_completes_stop_fact(tmp_path):
    """跨会话对账路径：计时满足即停止，确认带错误不影响收场。"""
    owned = _world(tmp_path)
    try:
        runtime = _configured(owned, StartDriver(_result(confirmed=True)))
        runtime.evidence = _CALL_EVIDENCE
        await _started_driver_ready(owned, runtime)
        stopper = StopWithError()
        runtime.stopper = stopper
        # 新会话不继承单调锚点：进入按已保存启动墙钟的对账。
        runtime.recording_state = SessionRecordingState(runtime)
        await capture_handler("camera_record")(12, runtime)

        assert stopper.calls == ["stop_recording"]
        run_id, activity_id = _assert_stop_fact(owned)
        assert row_facts(owned.connection, "device_activities",
                         activity_id)["occupancy_state"] == 2
        _assert_settle_transactions(owned, run_id, activity_id)
    finally:
        owned.connection.close()


async def test_winddown_stop_confirmed_with_error_completes_stop_fact(tmp_path):
    """保守收场路径：放弃计时停止，确认带错误时按停止事实收场。"""
    owned = _world(tmp_path)
    try:
        runtime = _configured(owned, StartDriver(_result(confirmed=True)))
        runtime.evidence = _CALL_EVIDENCE
        await _started_driver_ready(owned, runtime)
        stopper = StopWithError()
        runtime.stopper = stopper
        runtime.recording_state = SessionRecordingState(runtime)
        outcome = await advance_winddown(
            12, runtime, {}, wait_cap_s=Decimal("0"), sleep=_no_sleep)

        assert outcome.phase == "progress_deferred"
        assert stopper.calls == ["stop_recording"]
        run_id, activity_id = _assert_stop_fact(owned)
        # 保守会话只保存等待阶段；活动已结束且不提前释放占用。
        assert row_facts(owned.connection, "device_activities",
                         activity_id)["occupancy_state"] == 2
        _assert_settle_transactions(owned, run_id, activity_id)
    finally:
        owned.connection.close()


async def _no_sleep(_seconds):
    raise AssertionError("等待上限为零，不应再等待计时或重试间隔")


async def test_canceled_capture_stop_confirmed_with_error_completes_stop_fact(tmp_path):
    """取消收场路径：可靠停止带错误仍按停止事实收取消终态。"""
    from camctl.cancellation.models import CancelApplyMode

    from .test_cancel_late_start import _cancel

    owned = _environment(tmp_path, ((12, 2),))
    try:
        config = AttemptConfig(1, Decimal("10"), Decimal("1"))
        runtime = _runtime(owned, stop_config=config)
        ticket, reason = runtime.grant(runtime.action(12))
        assert ticket is not None, reason
        _cancel(owned, 12)
        _cancel(owned, 12, origin_id=60, mode=CancelApplyMode.ALREADY)
        stopper = StopWithError()
        runtime.stopper = stopper
        # 启动仍在途时取消不能派发停止；迟到启动结果未知后收场。
        await capture_handler("camera_record")(12, runtime)
        assert stopper.calls == []
        runtime.finish(ticket, CallOutcome(
            status=AttemptStatus.UNKNOWN,
            error=ErrorValue("result_unconfirmed", "device"),
            effect=EffectState.UNKNOWN,
            settlement=Settlement(
                SettlementBasis.OBSERVED, EvidenceValue("operation_returned", 1, {})),
            observations=()), end_run=RunOutcome.UNCONFIRMED,
            run_error=ErrorValue("result_unconfirmed", "device"))
        resumed = _runtime(owned, stop_config=config)
        resumed.evidence = _CALL_EVIDENCE
        resumed.stopper = stopper
        await capture_handler("camera_record")(12, resumed)

        assert stopper.calls == ["stop_recording"]
        run_id, activity_id = _assert_stop_fact(owned)
        # 取消按停止事实收终态并释放占用，不再依赖启动确认。
        action = owned.connection.execute(
            "SELECT status, cancel_requested FROM actions WHERE id = 12").fetchone()
        assert action == (6, 1)
        assert row_facts(owned.connection, "device_activities",
                         activity_id)["occupancy_state"] == 2
        _assert_settle_transactions(owned, run_id, activity_id)
        before = tuple(owned.connection.iterdump())
        await capture_handler("camera_record")(12, resumed)
        assert tuple(owned.connection.iterdump()) == before
        assert stopper.calls == ["stop_recording"]
    finally:
        owned.connection.close()


async def test_host_timer_stop_confirmed_with_error_completes_stop_fact(tmp_path):
    """主机到期停止路径：stop_timelapse 确认带错误时同事务保存控制时长。"""
    from .test_timelapse_host_timer import world as host_world

    owned, runtime, action_id, handler, _results, clock = await host_world(tmp_path)
    try:
        stopper = StopWithError("1")
        runtime.stopper = stopper
        await capture_handler(handler)(action_id, runtime)
        clock[0] = 15_000_000_000
        await capture_handler(handler)(action_id, runtime)

        assert stopper.calls == ["stop_timelapse"]
        run = owned.connection.execute(
            "SELECT id, status, attempts_used FROM operation_runs"
            " WHERE responsibility_key = ?", (f"stop/{action_id}",)).fetchone()
        assert run[1:] == (3, 1)
        attempt = owned.connection.execute(
            "SELECT status, effect_state, error_json FROM operation_attempts"
            " WHERE run_id = ?", (run[0],)).fetchone()
        assert attempt[:2] == (3, 3)
        assert json.loads(attempt[2]) == _STOP_ERROR
        activity = row_facts(owned.connection, "device_activities",
                             _activity_id(owned, action_id))
        assert (activity["activity_state"], activity["occupancy_state"],
                activity["control_elapsed_ns"]) == (3, 2, 10_000_000_000)
        stop_event, stop_txn = owned.connection.execute(
            "SELECT h.id, h.transaction_id FROM history_events h"
            " JOIN operation_attempts a ON a.result_event_id = h.id"
            " WHERE a.run_id = ?", (run[0],)).fetchone()
        ends = [(txn, body) for txn, body in owned.connection.execute(
                "SELECT transaction_id, body_json FROM history_events"
                " WHERE event_type = 13")]
        ended = [entry for entry in ends if any(
            row["after"]["values"].get("activity_state") == 3
            for row in json.loads(entry[1])["rows"]
            if row["table"] == "device_activities")]
        assert len(ended) == 1 and ended[0][0] == stop_txn
        observation = json.loads(ended[0][1])["evidence"]["observation"]
        assert observation == {
            "stop_result_event_id": stop_event,
            "control_elapsed_ns": 10_000_000_000}
    finally:
        owned.connection.close()



async def test_residual_stop_confirmed_with_error_settles_in_one_transaction(tmp_path):
    """残留停止分区：确认带错误时结果、流程结束与活动收场同事务。

    原录像停止预算耗尽后，后续动作建立的残留收场流程取得可靠停
    止确认：尝试保存 FAILED/CONFIRMED 及完整错误，流程按停止事实
    成功，活动 ENDED 与条件释放和上述事实共享同一事务身份。
    """
    import json as _json

    from camctl.capture.handlers import _settle_stop_exhausted, _stop_call
    from camctl.capture.residual import ResidualCandidate, _stop_residual
    from camctl.devices.ports import DeviceCallResult
    from camctl.operations.models import AttemptStatus, CallOutcome, EffectState

    from .test_call_result_save import ReturnedCall, _outcome
    from .test_capture_contract import _seed_action
    from ..scheduling.test_resources import _seed_activity

    owned = _world(tmp_path)
    try:
        runtime = await _started(owned)
        # 原录像停止未确认且预算耗尽：活动保持执行中与占用。
        runtime.stopper = ReturnedCall(_outcome("stop"))
        await _stop_call(runtime, runtime.action(12))
        _settle_stop_exhausted(runtime, runtime.action(12))
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id = 12").fetchone() == (4,)
        activity = row_facts(owned.connection, "device_activities",
                             _activity_id(owned))
        assert (activity["activity_state"], activity["occupancy_state"]) == (2, 1)
        _seed_action(owned.connection, 13, 2)
        _seed_activity(owned.connection, 13)
        stopper = StopWithError()
        runtime.stopper = stopper
        await _stop_residual(runtime, 13, ResidualCandidate(12, 12, 1, 1))

        assert stopper.calls == ["stop_recording"]
        run = owned.connection.execute(
            "SELECT id, status, attempts_used FROM operation_runs"
            " WHERE responsibility_key = 'followup/13/12'").fetchone()
        assert run[1:] == (3, 1)
        attempt = owned.connection.execute(
            "SELECT status, effect_state, error_json FROM operation_attempts"
            " WHERE run_id = ?", (run[0],)).fetchone()
        assert attempt[:2] == (3, 3)
        assert _json.loads(attempt[2]) == _STOP_ERROR
        ended = row_facts(owned.connection, "device_activities",
                          _activity_id(owned))
        assert (ended["activity_state"], ended["occupancy_state"]) == (3, 2)
        # 尝试结果、流程结束、活动 ENDED 与释放必须同事务。
        attempt_id = owned.connection.execute(
            "SELECT id FROM operation_attempts WHERE run_id = ?", (run[0],)).fetchone()[0]
        transactions = {"attempt": set(), "run": set(), "ended": set(),
                        "released": set()}
        for transaction_id, body in owned.connection.execute(
                "SELECT transaction_id, body_json FROM history_events ORDER BY id"):
            for row in _json.loads(body)["rows"]:
                values = row["after"]["values"]
                if row["table"] == "operation_attempts" and row["id"] == attempt_id \
                        and values.get("effect_state") == 3:
                    transactions["attempt"].add(transaction_id)
                if row["table"] == "operation_runs" and row["id"] == run[0] \
                        and values.get("status") == 3:
                    transactions["run"].add(transaction_id)
                if row["table"] == "device_activities" and row["id"] == ended["id"]:
                    if values.get("activity_state") == 3:
                        transactions["ended"].add(transaction_id)
                    if values.get("occupancy_state") == 2:
                        transactions["released"].add(transaction_id)
        assert len(transactions["attempt"]) == 1
        assert transactions["run"] <= transactions["attempt"]
        assert transactions["ended"] <= transactions["attempt"]
        assert transactions["released"] <= transactions["attempt"]
    finally:
        owned.connection.close()


class StopNoEffect:
    """停止端口替身：可靠声明停止命令没有效果。"""

    def __init__(self) -> None:
        self.calls = []

    async def stop(self, request):
        self.calls.append(request.operation)
        return DeviceCallResult.from_outcome(CallOutcome(
            status=AttemptStatus.FAILED,
            error=ErrorValue("device_not_recording", "execution"),
            effect=EffectState.NO_EFFECT,
            settlement=Settlement(
                SettlementBasis.OBSERVED, EvidenceValue("stop_returned", 1, {})),
            observations=(),
            call_info=CallInfo(local_exit_code=0),
        ))


async def test_stop_no_effect_keeps_activity_and_waits_for_retry(tmp_path):
    """无效果分区：可靠无效果的停止不结束活动，沿原预算等待重试。"""
    owned = _world(tmp_path)
    try:
        runtime = _configured(owned, StartDriver(_result(confirmed=True)))
        runtime.evidence = _CALL_EVIDENCE
        await _started_driver_ready(owned, runtime)
        stopper = StopNoEffect()
        runtime.stopper = stopper
        await capture_handler("camera_record")(12, runtime)

        assert stopper.calls == ["stop_recording"]
        run = owned.connection.execute(
            "SELECT status, attempts_used, retry_wait_required"
            " FROM operation_runs WHERE responsibility_key = 'stop/12'").fetchone()
        assert run == (2, 1, 1)
        attempt = _stop_attempt(owned)
        assert attempt[:2] == (3, 2)
        # 没有可靠停止事实：活动保持执行中且占用。
        activity = row_facts(owned.connection, "device_activities",
                             _activity_id(owned))
        assert (activity["activity_state"], activity["occupancy_state"]) == (2, 1)
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id = 12").fetchone() == (2,)
        # 重试间隔未到时重入不补发停止。
        await capture_handler("camera_record")(12, runtime)
        assert stopper.calls == ["stop_recording"]
    finally:
        owned.connection.close()


async def test_cancel_during_in_flight_stop_waits_for_actual_result(tmp_path):
    """在途取消分区：停止调用未返回前取消不能提前结束活动。

    实际返回后按返回事实收场：可靠停止同时带错误时活动结束与条
    件释放同事务，动作按取消规则收终态。
    """
    import asyncio

    from .test_cancel_late_start import _cancel

    owned = _world(tmp_path)
    try:
        runtime = _configured(owned, StartDriver(_result(confirmed=True)))
        runtime.evidence = _CALL_EVIDENCE
        await _started_driver_ready(owned, runtime)
        entered, released = asyncio.Event(), asyncio.Event()

        class PendingStopWithError(StopWithError):
            async def stop(self, request):
                self.calls.append(request.operation)
                entered.set()
                await released.wait()
                return _stop_confirmed_with_error(self.activity_id)

        stopper = PendingStopWithError()
        runtime.stopper = stopper
        pending = asyncio.create_task(
            capture_handler("camera_record")(12, runtime))
        await asyncio.wait_for(entered.wait(), 1)
        # 取消生效时停止在途：不派发新停止，不提前结束活动。
        _cancel(owned, 12)
        await capture_handler("camera_record")(12, runtime)
        assert stopper.calls == ["stop_recording"]
        activity = row_facts(owned.connection, "device_activities",
                             _activity_id(owned))
        assert (activity["activity_state"], activity["occupancy_state"]) == (2, 1)

        released.set()
        await asyncio.wait_for(pending, 1)
        # 实际返回按停止事实保存并收场，重入按取消终态收尾。
        await capture_handler("camera_record")(12, runtime)
        assert stopper.calls == ["stop_recording"]
        run_id, activity_id = _assert_stop_fact(owned)
        assert row_facts(owned.connection, "device_activities",
                         activity_id)["occupancy_state"] == 2
        assert owned.connection.execute(
            "SELECT status, cancel_requested FROM actions WHERE id = 12"
        ).fetchone() == (6, 1)
        _assert_settle_transactions(owned, run_id, activity_id)
    finally:
        owned.connection.close()

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
    advance_winddown, capture_handler,
)
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import DeviceCallResult
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import (
    AttemptStatus, CallInfo, CallOutcome, EffectState, ErrorValue, EvidenceValue,
    Settlement, SettlementBasis,
)
from camctl.persistence.transaction import row_facts

from .test_capture_contract import _NOW
from .test_recording_start_runtime import (
    _ANCHOR, StartDriver, _result,
)

from .recording_start_fixtures import start_case
from ..support.result_cases import result_case

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


def _stop_run(owned, action_id=12):
    return owned.connection.execute(
        "SELECT id, status, attempts_used FROM operation_runs"
        " WHERE responsibility_key = ?", (f"stop/{action_id}",)).fetchone()


def _stop_attempt(owned, action_id=12):
    run_id = _stop_run(owned, action_id)[0]
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
    run_id, run_status, used = _stop_run(owned, action_id)
    assert (run_status, used) == (3, 1)
    status, effect, error_json, result_json = _stop_attempt(owned, action_id)
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
    assert transactions["attempt"] == transactions["run"]
    assert transactions["activity"] == transactions["run"]


async def test_normal_record_stop_confirmed_with_error_completes_stop_fact(tmp_path, start_case):
    """正常录像路径：确认带错误时停止责任按停止事实完成。"""
    owned, runtime = await _confirmed_world(tmp_path, start_case)
    try:
        stopper = StopWithError("1")
        start_case.stopper.delegate = stopper
        await capture_handler("camera_record")(1, runtime)

        # 可靠停止事实被录像状态机识别：不重新停止、不补发。
        assert stopper.calls == ["stop_recording"]
        assert _stop_run(owned, 1)[1] == 3
        run_id, activity_id = _assert_stop_fact(owned, 1)
        # 普通录像停止后释放占用。
        assert row_facts(owned.connection, "device_activities",
                         activity_id)["occupancy_state"] == 2
        _assert_settle_transactions(owned, run_id, activity_id)
    finally:
        owned.connection.close()


async def _confirmed_world(tmp_path, case, *, cold=False, query=None):
    owned = await case.world(tmp_path)
    driver = StartDriver(_result(confirmed=True, activity_id='1'))
    options = dict(stop_config=AttemptConfig(1, Decimal('.125'), Decimal('1')), query=query)
    runtime = await case.runtime(owned, driver, **options)
    await capture_handler('camera_record')(case.aid, runtime)
    case.wall[0] = _NOW + 61_000_000
    case.mono[0] = _ANCHOR + 61_000_000_000
    if cold:
        runtime = await case.reopen(owned, driver, wall=case.wall, mono=case.mono, **options)
    return owned, runtime


async def test_reconcile_entry_stop_confirmed_with_error_completes_stop_fact(tmp_path, start_case):
    """跨会话对账路径：计时满足即停止，确认带错误不影响收场。"""
    owned, runtime = await _confirmed_world(tmp_path, start_case, cold=True)
    try:
        stopper = StopWithError("1")
        start_case.stopper.delegate = stopper
        # 新会话不继承单调锚点：进入按已保存启动墙钟的对账。
        await capture_handler("camera_record")(1, runtime)

        assert stopper.calls == ["stop_recording"]
        run_id, activity_id = _assert_stop_fact(owned, 1)
        assert row_facts(owned.connection, "device_activities",
                         activity_id)["occupancy_state"] == 2
        _assert_settle_transactions(owned, run_id, activity_id)
    finally:
        owned.connection.close()


async def test_winddown_stop_confirmed_with_error_completes_stop_fact(tmp_path, start_case):
    """保守收场路径：放弃计时停止，确认带错误时按停止事实收场。"""
    owned, runtime = await _confirmed_world(tmp_path, start_case, cold=True)
    try:
        stopper = StopWithError("1")
        start_case.stopper.delegate = stopper
        outcome = await advance_winddown(
            start_case.aid, runtime, {}, wait_cap_s=Decimal("0"), sleep=_no_sleep)

        assert outcome.phase == "progress_deferred"
        assert stopper.calls == ["stop_recording"]
        run_id, activity_id = _assert_stop_fact(owned, 1)
        # 保守会话只保存等待阶段；活动已结束且不提前释放占用。
        assert row_facts(owned.connection, "device_activities",
                         activity_id)["occupancy_state"] == 2
        _assert_settle_transactions(owned, run_id, activity_id)
    finally:
        owned.connection.close()


async def _no_sleep(_seconds):
    raise AssertionError("等待上限为零，不应再等待计时或重试间隔")


async def test_canceled_capture_stop_confirmed_with_error_completes_stop_fact(tmp_path, start_case):
    """取消收场路径：实际START仍在途时不停止，迟结果保存后可靠停止。"""
    import asyncio
    from .test_natural_wait_time_eligibility import _request_cancel
    owned = await start_case.world(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    pending = None
    outcome = CallOutcome(status=AttemptStatus.UNKNOWN,
        error=ErrorValue('result_unconfirmed', 'device'), effect=EffectState.UNKNOWN,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue('operation_returned', 1, {})))
    class HeldStart(StartDriver):
        async def control(self, request):
            self.calls.append(request)
            entered.set()
            await release.wait()
            return DeviceCallResult.from_outcome(self.outcome)
    driver = HeldStart(outcome)
    stopper = StopWithError('1')
    start_case.stopper.delegate = stopper
    try:
        config = AttemptConfig(1, Decimal('10'), Decimal('1'))
        runtime = await start_case.runtime(owned, driver, stop_config=config)
        pending = asyncio.create_task(capture_handler('camera_record')(start_case.aid, runtime))
        await entered.wait()
        assert driver.calls[0].ticket is not None
        await _request_cancel(runtime, start_case.aid, start_case.wall)
        await _request_second_cancel(runtime, start_case.aid, start_case.wall)
        await capture_handler('camera_record')(start_case.aid, runtime)
        assert stopper.calls == []
        release.set()
        await pending
        resumed = await start_case.reopen(owned, driver, stop_config=config)
        await capture_handler('camera_record')(start_case.aid, resumed)
        assert stopper.calls == ['stop_recording']
        run_id, activity_id = _assert_stop_fact(owned, start_case.aid)
        action = owned.connection.execute('SELECT status,cancel_requested FROM actions WHERE id=1').fetchone()
        assert action == (6, 1)
        assert row_facts(owned.connection, 'device_activities', activity_id)['occupancy_state'] == 2
        _assert_settle_transactions(owned, run_id, activity_id)
        before = tuple(owned.connection.iterdump())
        await capture_handler('camera_record')(start_case.aid, resumed)
        assert tuple(owned.connection.iterdump()) == before
        assert stopper.calls == ['stop_recording']
    finally:
        release.set()
        if pending is not None and not pending.done():
            await pending
        owned.connection.close()


async def _request_second_cancel(runtime, action_id, wall):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import accept_input, AcceptanceContext, CommandMode
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from .result_consumer_fixtures import ResultCatalog
    from .test_natural_wait_time_eligibility import _progress_cancel
    instant = datetime.fromtimestamp(wall[0] // 1_000_000, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    accepted = await accept_input(ParsedInput('cancel-again.json', {
        'request_id': '3', 'created_at': instant, 'name': '再次取消', 'actions': [
            {'name': '取消', 'type': 'cancel_task', 'params': {'target': {'action_instance_id': str(action_id)}}}]}),
        AcceptanceContext(CommandMode.RUN, ResultCatalog(), AcceptanceRepository(),
            clock=SimpleNamespace(utc_micros=lambda: wall[0])), new_operation_key(), runtime.database)
    assert accepted.plan_id is not None, accepted
    await _progress_cancel(runtime, wall)


async def test_host_timer_stop_confirmed_with_error_completes_stop_fact(tmp_path, result_case):
    """主机到期停止路径：stop_timelapse 确认带错误时同事务保存控制时长。"""
    from .test_timelapse_host_timer import HostCatalog
    from .test_result_device_completion import device_pages, _COMPLETION
    owned, runtime, action_id, handler = await result_case.world(tmp_path, 'timelapse',
        independent_activity=True, start=False, catalog=HostCatalog(), evidence_contracts=(_COMPLETION,))
    device_pages(runtime)
    try:
        stopper = StopWithError("1")
        result_case.stopper.stop.side_effect = stopper.stop
        await capture_handler(handler)(action_id, runtime)
        result_case.clock.monotonic = 15_000_000_000
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


async def test_residual_stop_confirmed_with_error_settles_in_one_transaction(tmp_path, start_case):
    """残留停止分区：确认带错误时结果、流程结束与活动收场同事务。

    原录像停止预算耗尽后，后续动作建立的残留收场流程取得可靠停
    止确认：尝试保存 FAILED/CONFIRMED 及完整错误，流程按停止事实
    成功，活动 ENDED 与条件释放和上述事实共享同一事务身份。
    """
    import json as _json

    from .test_call_result_save import ReturnedCall, _outcome
    from .test_recording_queries import RecordingQuery

    owned, runtime = await _confirmed_world(tmp_path, start_case, query=RecordingQuery('1'))
    try:
        start_case.stopper.delegate = ReturnedCall(_outcome('stop'))
        await capture_handler('camera_record')(start_case.aid, runtime)
        await capture_handler('camera_record')(start_case.aid, runtime)
        assert owned.connection.execute('SELECT status FROM actions WHERE id=1').fetchone() == (4,)
        activity = row_facts(owned.connection, 'device_activities', _activity_id(owned, 1))
        assert (activity['activity_state'], activity['occupancy_state']) == (2, 1)
        followup = await _accept_followup(runtime, start_case.wall)
        stopper = StopWithError('1')
        start_case.stopper.delegate = stopper
        await capture_handler('camera_record')(followup, runtime)

        assert stopper.calls == ["stop_recording"]
        run = owned.connection.execute(
            "SELECT id, status, attempts_used FROM operation_runs"
            " WHERE responsibility_key = 'followup/2/1'").fetchone()
        assert run[1:] == (3, 1)
        attempt = owned.connection.execute(
            "SELECT status, effect_state, error_json FROM operation_attempts"
            " WHERE run_id = ?", (run[0],)).fetchone()
        assert attempt[:2] == (3, 3)
        assert _json.loads(attempt[2]) == _STOP_ERROR
        ended = row_facts(owned.connection, "device_activities",
                          _activity_id(owned, 1))
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
        assert transactions["run"] == transactions["attempt"]
        assert transactions["ended"] == transactions["attempt"]
        assert transactions["released"] == transactions["attempt"]
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


async def test_stop_no_effect_keeps_activity_and_waits_for_retry(tmp_path, start_case):
    """无效果分区：可靠无效果的停止不结束活动，沿原预算等待重试。"""
    owned, runtime = await _confirmed_world(tmp_path, start_case)
    try:
        stopper = StopNoEffect()
        start_case.stopper.delegate = stopper
        await capture_handler("camera_record")(1, runtime)

        assert stopper.calls == ["stop_recording"]
        run = owned.connection.execute(
            "SELECT status, attempts_used, retry_wait_required"
            " FROM operation_runs WHERE responsibility_key = 'stop/1'").fetchone()
        assert run == (2, 1, 1)
        attempt = _stop_attempt(owned, 1)
        assert attempt[:2] == (3, 2)
        # 没有可靠停止事实：活动保持执行中且占用。
        activity = row_facts(owned.connection, "device_activities",
                             _activity_id(owned, 1))
        assert (activity["activity_state"], activity["occupancy_state"]) == (2, 1)
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id = 1").fetchone() == (2,)
        # 重试间隔未到时重入不补发停止。
        await capture_handler("camera_record")(1, runtime)
        assert stopper.calls == ["stop_recording"]
    finally:
        owned.connection.close()


async def test_cancel_during_in_flight_stop_waits_for_actual_result(tmp_path, start_case):
    """在途取消分区：停止调用未返回前取消不能提前结束活动。

    实际返回后按返回事实收场：可靠停止同时带错误时活动结束与条
    件释放同事务，动作按取消规则收终态。
    """
    import asyncio

    from .test_natural_wait_time_eligibility import _request_cancel

    owned, runtime = await _confirmed_world(tmp_path, start_case)
    entered, released = asyncio.Event(), asyncio.Event()
    pending = None
    try:

        class PendingStopWithError(StopWithError):
            async def stop(self, request):
                self.calls.append(request.operation)
                entered.set()
                await released.wait()
                return _stop_confirmed_with_error(self.activity_id)

        stopper = PendingStopWithError("1")
        start_case.stopper.delegate = stopper
        pending = asyncio.create_task(
            capture_handler("camera_record")(1, runtime))
        await asyncio.wait_for(entered.wait(), 1)
        # 取消生效时停止在途：不派发新停止，不提前结束活动。
        await _request_cancel(runtime, start_case.aid, start_case.wall)
        await capture_handler("camera_record")(1, runtime)
        assert stopper.calls == ["stop_recording"]
        activity = row_facts(owned.connection, "device_activities",
                             _activity_id(owned, 1))
        assert (activity["activity_state"], activity["occupancy_state"]) == (2, 1)

        released.set()
        await asyncio.wait_for(pending, 1)
        # 实际返回按停止事实保存并收场，重入按取消终态收尾。
        await capture_handler("camera_record")(1, runtime)
        assert stopper.calls == ["stop_recording"]
        run_id, activity_id = _assert_stop_fact(owned, 1)
        assert row_facts(owned.connection, "device_activities",
                         activity_id)["occupancy_state"] == 2
        assert owned.connection.execute(
            "SELECT status, cancel_requested FROM actions WHERE id = 1"
        ).fetchone() == (6, 1)
        _assert_settle_transactions(owned, run_id, activity_id)
    finally:
        released.set()
        if pending is not None and not pending.done():
            await pending
        owned.connection.close()


async def _accept_followup(runtime, wall):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import accept_input, AcceptanceContext, CommandMode
    from camctl.persistence.repositories.acceptance import AcceptanceRepository
    from camctl.contracts.values import new_operation_key
    from .result_consumer_fixtures import ResultCatalog
    instant = datetime.fromtimestamp(wall[0] // 1_000_000, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    accepted = await accept_input(ParsedInput('followup.json', {'request_id': '2',
        'created_at': instant, 'name': '后续录像', 'actions': [{'name': '拍摄', 'type': 'camera_record',
            'device_id': 'cam-1', 'scheduled_at': instant, 'params': {'type': 'video'},
            'policy': {'max_delay_ms': 60000}}]}),
        AcceptanceContext(CommandMode.RUN, ResultCatalog(), AcceptanceRepository(),
            clock=SimpleNamespace(utc_micros=lambda: wall[0])), new_operation_key(), runtime.database)
    assert accepted.plan_id is not None, accepted
    identity, = await runtime.database.read(lambda current: current.connection.execute(
        'SELECT id FROM actions WHERE plan_id=?', (accepted.plan_id,)).fetchone())
    return identity

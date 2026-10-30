"""录像活动与停止查询通过正式处理器、原仓储和冷会话恢复核验。

查询端口只提供正式 CallOutcome；各用途的责任、证据、有限预算与
取消收场分别沿实际持久事实推进。
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from camctl.capture.handlers import capture_handler
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import DeviceCallResult
from camctl.operations.attempts import (
    AttemptConfig, OperationKind, QueryPurpose,
)
from camctl.operations.models import (
    AttemptStatus, CallInfo, CallOutcome, EffectState, ErrorValue, EvidenceValue,
    Settlement, SettlementBasis,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.transaction import row_facts

from .test_recording_start_runtime import (
    _ANCHOR, _NOW, StartDriver, _result,
)

from .recording_start_fixtures import start_case

pytestmark = pytest.mark.asyncio


class SentStop:
    """停止端口替身：命令已发送但没有取得停止确认。"""

    def __init__(self) -> None:
        self.calls = []

    async def stop(self, request):
        self.calls.append(request.operation)
        return DeviceCallResult.from_outcome(CallOutcome(
            status=AttemptStatus.SUCCEEDED,
            error=None,
            effect=EffectState.UNKNOWN,
            settlement=Settlement(
                SettlementBasis.OBSERVED, EvidenceValue("stop_returned", 1, {})),
            observations=(),
            call_info=CallInfo(local_exit_code=0),
        ))


class IdleQuery:
    """查询端点替身：可靠确认设备当前没有进行中的活动。"""

    def __init__(self) -> None:
        self.calls = []

    async def query_state(self, request):
        self.calls.append(request)
        return DeviceCallResult.from_outcome(CallOutcome(
            status=AttemptStatus.SUCCEEDED,
            error=None,
            effect=EffectState.NO_EFFECT,
            settlement=Settlement(
                SettlementBasis.OBSERVED, EvidenceValue("query_returned", 1, {})),
            observations=(),
            call_info=CallInfo(local_exit_code=0),
        ))


class RecordingQuery:
    """查询端点替身：可靠观察到指定活动仍在录制。"""

    def __init__(self, activity_id: str) -> None:
        self.activity_id = activity_id
        self.calls = []

    async def query_state(self, request):
        self.calls.append(request)
        return DeviceCallResult.from_outcome(CallOutcome(
            status=AttemptStatus.SUCCEEDED,
            error=None,
            effect=EffectState.CONFIRMED,
            settlement=Settlement(
                SettlementBasis.OBSERVED, EvidenceValue("query_returned", 1, {})),
            observations=(DeviceObservation(
                "activity_status", 1, {"activity_id": self.activity_id}),),
            call_info=CallInfo(local_exit_code=0),
        ))


class FailedQuery:
    """查询端点替身：调用失败，没有可靠观察。"""

    def __init__(self) -> None:
        self.calls = []

    async def query_state(self, request):
        self.calls.append(request)
        return DeviceCallResult.from_outcome(CallOutcome(
            status=AttemptStatus.FAILED,
            error=ErrorValue("transport_timeout", "transport"),
            effect=EffectState.UNKNOWN,
            settlement=Settlement(
                SettlementBasis.OBSERVED, EvidenceValue("query_returned", 1, {})),
            observations=(),
            call_info=CallInfo(local_exit_code=0),
        ))


def _activity_id(owned, action_id=12):
    return owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id = ?",
        (action_id,)).fetchone()[0]


def _run(owned, key):
    return owned.connection.execute(
        "SELECT id, status, attempts_used FROM operation_runs"
        " WHERE responsibility_key = ?", (key,)).fetchone()


class QueryScript:
    def __init__(self):
        self.delegate = None

    async def query_state(self, request):
        assert self.delegate is not None, '用例须在实际QUERY前提供端口结果'
        return await self.delegate.query_state(request)


async def _public_ready(tmp_path, case, *, stopper, query_maximum=2, stop_maximum=3, cold=False, send_stop=True):
    owned = await case.world(tmp_path)
    script = QueryScript()
    driver = StartDriver(_result(confirmed=True, activity_id='1'))
    case.stopper.delegate = stopper
    options = dict(query=script,
        stop_config=AttemptConfig(stop_maximum, Decimal('.125'), Decimal('1')),
        query_config=AttemptConfig(query_maximum, Decimal('.5'), Decimal('1')))
    runtime = await case.runtime(owned, driver, **options)
    await capture_handler('camera_record')(case.aid, runtime)
    case.wall[0] = _NOW + 61_000_000
    case.mono[0] = _ANCHOR + 61_000_000_000
    if cold:
        runtime = await case.reopen(owned, driver, wall=case.wall, mono=case.mono, **options)
    elif send_stop:
        await capture_handler('camera_record')(case.aid, runtime)
    return owned, runtime, script


async def test_unconfirmed_stop_confirmed_by_idle_query_without_new_stop(tmp_path, start_case):
    """停止核实消费者：未确认停止由空闲观察收场，零重发停止。

    停止命令已发送但没有确认时，重试等待到达后先用原停止核实责
    任查询：可靠空闲即按停止事实结束查询与原停止流程，活动结束
    与条件释放同事务，不再追加停止调用。
    """
    stopper = SentStop()
    owned, runtime, script = await _public_ready(tmp_path, start_case, stopper=stopper, query_maximum=2)
    try:
        stop_run = _run(owned, "stop/1")
        assert stop_run[1:] == (2, 1)
        # 重试等待已到，本会话装配查询端点。
        start_case.wall[0] = _NOW + 63_000_000
        start_case.mono[0] = _ANCHOR + 63_000_000_000
        query = IdleQuery()
        script.delegate = query
        await capture_handler("camera_record")(1, runtime)

        assert stopper.calls == ["stop_recording"]
        assert len(query.calls) == 1
        assert query.calls[0].operation == "query"
        stop_confirmation = _run(owned, f"query/stop/1/{_activity_id(owned, 1)}")
        assert stop_confirmation[1:] == (3, 1)
        assert _run(owned, "stop/1")[1] == 3
        activity = row_facts(owned.connection, "device_activities",
                             _activity_id(owned, 1))
        assert (activity["activity_state"], activity["occupancy_state"]) == (3, 2)
        # 查询确认与活动收场同事务。
        transactions = set()
        settle_txn = None
        for transaction_id, body in owned.connection.execute(
                "SELECT transaction_id, body_json FROM history_events ORDER BY id"):
            for row in json.loads(body)["rows"]:
                values = row["after"]["values"]
                if row["table"] == "operation_runs" and row["id"] == stop_confirmation[0] \
                        and values.get("status") == 3:
                    transactions.add(("query_run", transaction_id))
                if row["table"] == "operation_runs" and row["id"] == stop_run[0] \
                        and values.get("status") == 3:
                    transactions.add(("stop_run", transaction_id))
                if row["table"] == "device_activities" and row["id"] == activity["id"]:
                    if values.get("activity_state") == 3:
                        transactions.add(("ended", transaction_id))
                    if values.get("occupancy_state") == 2:
                        transactions.add(("released", transaction_id))
        marks = {name for name, _ in transactions}
        ids = {txn for _, txn in transactions}
        assert marks == {"query_run", "stop_run", "ended", "released"}
        assert len(ids) == 1
    finally:
        owned.connection.close()


async def test_reconcile_confirms_continuity_before_stop(tmp_path, start_case):
    """活动核实消费者：墙钟满足后先确认归属连续，再执行停止。

    跨会话对账进入停止前，经 `query/activity` 责任确认当前仍是同
    一活动在录；归属确认后停止按原预算执行，确认观察随尝试保存。
    """
    stopper = SentStop()
    owned, runtime, script = await _public_ready(tmp_path, start_case, stopper=stopper, query_maximum=2, stop_maximum=1, cold=True)
    try:
        query = RecordingQuery(str(_activity_id(owned, 1)))
        script.delegate = query
        await capture_handler("camera_record")(1, runtime)

        assert len(query.calls) == 1
        observation = _run(owned, f"query/activity/1/{_activity_id(owned, 1)}")
        assert observation[1:] == (3, 1)
        result = owned.connection.execute(
            "SELECT result_json FROM operation_attempts WHERE run_id = ?",
            (observation[0],)).fetchone()
        assert json.loads(result[0])["observations"] == [
            {"type": "activity_status", "version": 1,
             "data": {"activity_id": str(_activity_id(owned, 1))}}]
        # 归属确认后停止执行且未确认：沿原停止预算保留。
        assert stopper.calls == ["stop_recording"]
        assert _run(owned, "stop/1")[1:] == (2, 1)
        activity = row_facts(owned.connection, "device_activities",
                             _activity_id(owned, 1))
        assert (activity["activity_state"], activity["occupancy_state"]) == (2, 1)
    finally:
        owned.connection.close()


async def test_reconcile_idle_query_concludes_activity_without_stop(tmp_path, start_case):
    """活动核实消费者：空闲观察证明活动已结束，不重新执行停止。"""
    stopper = SentStop()
    owned, runtime, script = await _public_ready(tmp_path, start_case, stopper=stopper, query_maximum=2, stop_maximum=1, cold=True)
    try:
        query = IdleQuery()
        script.delegate = query
        await capture_handler("camera_record")(1, runtime)

        assert stopper.calls == []
        assert len(query.calls) == 1
        observation = _run(owned, f"query/activity/1/{_activity_id(owned, 1)}")
        assert observation[1:] == (3, 1)
        # 空闲确认后录像状态机识别停止已完成，重入不再停止或查询。
        assert owned.connection.execute("SELECT activity_state FROM device_activities WHERE action_id=1").fetchone() == (3,)
        before = tuple(owned.connection.iterdump())
        await capture_handler("camera_record")(1, runtime)
        assert tuple(owned.connection.iterdump()) == before
        assert stopper.calls == [] and len(query.calls) == 1
    finally:
        owned.connection.close()


async def test_reconcile_failed_query_waits_without_stop(tmp_path, start_case):
    """活动核实消费者：归属不明时不进入墙钟成功分支。

    查询调用失败时归属与连续性未取得，只保存错误与重试等待；间
    隔到达后重新核实，不凭计时对账直接停止。
    """
    stopper = SentStop()
    owned, runtime, script = await _public_ready(tmp_path, start_case, stopper=stopper, query_maximum=2, stop_maximum=1, cold=True)
    try:
        query = FailedQuery()
        script.delegate = query
        await capture_handler("camera_record")(1, runtime)

        assert stopper.calls == []
        observation = _run(owned, f"query/activity/1/{_activity_id(owned, 1)}")
        assert observation[1:] == (2, 1)
        activity = row_facts(owned.connection, "device_activities",
                             _activity_id(owned, 1))
        assert (activity["activity_state"], activity["occupancy_state"]) == (2, 1)
        # 重试间隔未到：重入不查询也不停止。
        await capture_handler("camera_record")(1, runtime)
        assert len(query.calls) == 1 and stopper.calls == []
        # 间隔到达后重新核实，仍不停止。
        start_case.wall[0] = _NOW + 63_000_000
        start_case.mono[0] = _ANCHOR + 63_000_000_000
        await capture_handler("camera_record")(1, runtime)
        assert len(query.calls) == 2 and stopper.calls == []
    finally:
        owned.connection.close()


class StillRecordingWithErrorQuery:
    """查询端点替身：可靠观察到本活动仍在录制，同时携带调用错误。"""

    def __init__(self, activity_id: str) -> None:
        self.activity_id = activity_id
        self.calls = []

    async def query_state(self, request):
        self.calls.append(request)
        return DeviceCallResult.from_outcome(CallOutcome(
            status=AttemptStatus.FAILED,
            error=ErrorValue("transport_timeout", "transport"),
            effect=EffectState.CONFIRMED,
            settlement=Settlement(
                SettlementBasis.OBSERVED, EvidenceValue("query_returned", 1, {})),
            observations=(DeviceObservation(
                "activity_status", 1, {"activity_id": self.activity_id}),),
            call_info=CallInfo(local_exit_code=0),
        ))


async def test_unconfirmed_stop_unreliable_query_keeps_retrying_stop(tmp_path, start_case):
    """停止核实矩阵：调用失败没有可靠观察时保存事实，停止继续。"""
    stopper = SentStop()
    owned, runtime, script = await _public_ready(tmp_path, start_case, stopper=stopper, query_maximum=3)
    try:
        query = FailedQuery()
        script.delegate = query
        start_case.wall[0] = _NOW + 63_000_000
        start_case.mono[0] = _ANCHOR + 63_000_000_000
        await capture_handler("camera_record")(1, runtime)

        # 无可靠观察：停止核实继续，停止沿原预算同轮重试。
        confirmation = _run(owned, f"query/stop/1/{_activity_id(owned, 1)}")
        assert confirmation[1:] == (2, 1)
        assert stopper.calls == ["stop_recording", "stop_recording"]
        activity = row_facts(owned.connection, "device_activities",
                             _activity_id(owned, 1))
        assert (activity["activity_state"], activity["occupancy_state"]) == (2, 1)
    finally:
        owned.connection.close()


async def test_stop_confirmation_confirmed_with_error_saves_observation(tmp_path, start_case):
    """停止核实矩阵：仍在录确认与调用错误并存时完整保存。"""
    stopper = SentStop()
    owned, runtime, script = await _public_ready(tmp_path, start_case, stopper=stopper, query_maximum=3)
    try:
        query = StillRecordingWithErrorQuery(str(_activity_id(owned, 1)))
        script.delegate = query
        start_case.wall[0] = _NOW + 63_000_000
        start_case.mono[0] = _ANCHOR + 63_000_000_000
        await capture_handler("camera_record")(1, runtime)

        confirmation = _run(owned, f"query/stop/1/{_activity_id(owned, 1)}")
        assert confirmation[1:] == (2, 1)
        attempt = owned.connection.execute(
            "SELECT status, effect_state, result_json, error_json"
            " FROM operation_attempts WHERE run_id = ?",
            (confirmation[0],)).fetchone()
        assert attempt[:2] == (3, 3)
        saved = json.loads(attempt[2])
        assert saved["observations"] == [
            {"type": "activity_status", "version": 1,
             "data": {"activity_id": str(_activity_id(owned, 1))}}]
        assert json.loads(attempt[3]) == {
            "code": "transport_timeout", "stage": "transport", "details": {}}
        # 仍在录：活动不结束，停止沿原预算继续重试。
        assert stopper.calls == ["stop_recording", "stop_recording"]
        activity = row_facts(owned.connection, "device_activities",
                             _activity_id(owned, 1))
        assert (activity["activity_state"], activity["occupancy_state"]) == (2, 1)
    finally:
        owned.connection.close()


async def test_stop_confirmation_budget_exhausted_unconfirmed_back_to_stop(tmp_path, start_case):
    """停止核实矩阵：持续在录耗尽查询预算按 UNCONFIRMED，停止重试。"""
    stopper = SentStop()
    owned, runtime, script = await _public_ready(tmp_path, start_case, stopper=stopper, query_maximum=1)
    try:
        query = RecordingQuery(str(_activity_id(owned, 1)))
        script.delegate = query
        wall = [_NOW + 63_000_000]
        start_case.wall = wall
        mono = [_ANCHOR + 63_000_000_000]
        start_case.mono = mono
        await capture_handler("camera_record")(1, runtime)
        # 仍在录观察保存后停止链同轮重试。
        assert len(query.calls) == 1
        assert stopper.calls == ["stop_recording", "stop_recording"]
        confirmation = _run(owned, f"query/stop/1/{_activity_id(owned, 1)}")
        assert confirmation[1:] == (2, 1)

        # 间隔到达：预算已尽，查询按 UNCONFIRMED 收场并交回停止链。
        wall[0] = _NOW + 65_000_000
        mono[0] = _ANCHOR + 65_000_000_000
        await capture_handler("camera_record")(1, runtime)
        assert _run(owned, f"query/stop/1/{_activity_id(owned, 1)}")[1:] == (6, 1)
        assert len(query.calls) == 1
        assert stopper.calls == ["stop_recording"] * 3
        assert _run(owned, "stop/1")[1:] == (2, 3)
    finally:
        owned.connection.close()


async def test_canceled_capture_uses_stop_confirmation_without_new_stop(tmp_path, start_case):
    """取消链矩阵：取消后停止核实仍驱动收场，取消终态同事实成立。"""
    from .test_natural_wait_time_eligibility import _request_cancel

    stopper = SentStop()
    owned, runtime, script = await _public_ready(tmp_path, start_case, stopper=stopper, query_maximum=2)
    try:
        await _request_cancel(runtime, start_case.aid, start_case.wall)
        query = IdleQuery()
        script.delegate = query
        start_case.wall[0] = _NOW + 63_000_000
        start_case.mono[0] = _ANCHOR + 63_000_000_000
        await capture_handler("camera_record")(1, runtime)
        # 收场后重入按已保存取消事实收终态。
        await capture_handler("camera_record")(1, runtime)

        # 取消生效：不经计时立即停止；未确认后由停止核实收场。
        assert stopper.calls == ["stop_recording"]
        assert len(query.calls) == 1
        confirmation = _run(owned, f"query/stop/1/{_activity_id(owned, 1)}")
        assert confirmation[1:] == (3, 1)
        activity = row_facts(owned.connection, "device_activities",
                             _activity_id(owned, 1))
        assert (activity["activity_state"], activity["occupancy_state"]) == (3, 2)
        assert owned.connection.execute(
            "SELECT status, cancel_requested FROM actions WHERE id = 1"
        ).fetchone() == (6, 1)
    finally:
        owned.connection.close()


async def test_orphan_stop_confirmation_settled_after_stop_succeeds(tmp_path, start_case):
    """孤儿收场：停止链自行成功后停止核实按可靠依据收 SUCCEEDED。"""
    stopper = SentStop()
    owned, runtime, script = await _public_ready(tmp_path, start_case, stopper=stopper, query_maximum=3)
    try:
        query = FailedQuery()
        script.delegate = query
        start_case.wall[0] = _NOW + 63_000_000
        start_case.mono[0] = _ANCHOR + 63_000_000_000
        await capture_handler("camera_record")(1, runtime)
        # 查询失败短路后，间隔到达的重试停止确认成功，核实成孤儿。
        start_case.stopper.delegate = StopConfirmed("1")
        start_case.wall[0] = _NOW + 65_000_000
        start_case.mono[0] = _ANCHOR + 65_000_000_000
        await capture_handler("camera_record")(1, runtime)
        assert _run(owned, "stop/1")[1] == 3
        confirmation = _run(owned, f"query/stop/1/{_activity_id(owned, 1)}")
        assert confirmation[1:] == (2, 1)

        from camctl.capture.residual import residual_flow
        from types import SimpleNamespace
        await residual_flow(start_case.factory)(SimpleNamespace(database=runtime.database,
            clock=SimpleNamespace(utc_micros=lambda: start_case.wall[0])))
        assert _run(owned, f"query/stop/1/{_activity_id(owned, 1)}")[1] == 3
    finally:
        owned.connection.close()


async def test_orphan_activity_observation_settled_after_cancel(tmp_path, start_case):
    """孤儿收场：取消终态后活动核实责任按 CANCELED 收场。"""
    from .test_natural_wait_time_eligibility import _request_cancel

    stopper = SentStop()
    owned, runtime, script = await _public_ready(tmp_path, start_case, stopper=stopper, query_maximum=2, stop_maximum=1, cold=True)
    try:
        query = FailedQuery()
        script.delegate = query
        await capture_handler("camera_record")(1, runtime)
        observation = _run(owned, f"query/activity/1/{_activity_id(owned, 1)}")
        assert observation[1:] == (2, 1)
        # 取消终态后活动核实不再承担核实责任。
        await _request_cancel(runtime, start_case.aid, start_case.wall)
        from camctl.persistence.repositories.capture import CaptureRepository, FinishCanceledCapture
        from camctl.contracts.values import new_operation_key
        key = new_operation_key()
        saved = await runtime.database.write(lambda current: CaptureRepository().finish_canceled_capture(
            FinishCanceledCapture(start_case.aid, start_case.wall[0]), key, current), key=key)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error

        assert _run(owned, 'query/activity/1/1')[1:] == (2, 1)
        from camctl.capture.residual import residual_flow
        from types import SimpleNamespace
        await residual_flow(start_case.factory)(SimpleNamespace(database=runtime.database,
            clock=SimpleNamespace(utc_micros=lambda: start_case.wall[0])))
        assert _run(owned, f"query/activity/1/{_activity_id(owned, 1)}")[1] == 5
    finally:
        owned.connection.close()


class StopConfirmed:
    """停止端口替身：可靠确认停止。"""

    def __init__(self, activity_id="12") -> None:
        self.calls = []
        self.activity_id = activity_id

    async def stop(self, request):
        from camctl.devices.evidence import EvidenceContract  # noqa: F401
        self.calls.append(request.operation)
        return DeviceCallResult.from_outcome(CallOutcome(
            status=AttemptStatus.SUCCEEDED,
            error=None,
            effect=EffectState.CONFIRMED,
            settlement=Settlement(
                SettlementBasis.OBSERVED, EvidenceValue("stop_returned", 1, {})),
            observations=(DeviceObservation(
                "stop_confirmed", 1, {"activity_id": self.activity_id}),),
            call_info=CallInfo(local_exit_code=0),
        ))


async def test_query_attempts_independent_and_same_key_across_reentry(tmp_path, start_case):
    """用途独立与重入不换键：各用途责任、次数与重试互不串用。

    活动核实的归属确认与停止核实的持续在录查询并行推进：责任键
    不同、次数分别累计；同一用途的第二次查询沿用原流程行与递增
    的尝试号，不新建责任。
    """
    stopper = SentStop()
    owned, runtime, script = await _public_ready(tmp_path, start_case, stopper=stopper, query_maximum=2, cold=True)
    try:
        query = RecordingQuery(str(_activity_id(owned, 1)))
        script.delegate = query
        # 对账归属确认（活动核实第一次也是唯一一次）后停止未确认。
        await capture_handler("camera_record")(1, runtime)
        activity_run = _run(owned, f"query/activity/1/{_activity_id(owned, 1)}")
        assert activity_run[1:] == (3, 1)
        # 停止核实第一次查询仍在录；间隔到达后第二次查询。
        start_case.wall[0] = _NOW + 63_000_000
        start_case.mono[0] = _ANCHOR + 63_000_000_000
        await capture_handler("camera_record")(1, runtime)
        stop_confirmation = _run(owned, f"query/stop/1/{_activity_id(owned, 1)}")
        assert stop_confirmation[1:] == (2, 1)
        start_case.wall[0] = _NOW + 65_000_000
        start_case.mono[0] = _ANCHOR + 65_000_000_000
        await capture_handler("camera_record")(1, runtime)

        # 活动核实终态保持一次；停止核实两次同流程行，尝试号递增。
        assert _run(owned, f"query/activity/1/{_activity_id(owned, 1)}") == activity_run
        final = _run(owned, f"query/stop/1/{_activity_id(owned, 1)}")
        assert final[0] == stop_confirmation[0]
        assert final[1:] == (2, 2)
        attempt_nos = [row[0] for row in owned.connection.execute(
            "SELECT attempt_no FROM operation_attempts WHERE run_id = ?"
            " ORDER BY attempt_no", (final[0],))]
        assert attempt_nos == [1, 2]
        activity_calls = [call for call in query.calls
                          if call.ticket.responsibility_key.endswith(
                              f"query/activity/1/{_activity_id(owned, 1)}")]
        stop_calls = [call for call in query.calls
                      if call.ticket.responsibility_key.endswith(
                          f"query/stop/1/{_activity_id(owned, 1)}")]
        assert [call.ticket.attempt_id for call in activity_calls] == [1]
        assert [call.ticket.attempt_id for call in stop_calls] == [1, 2]
        assert {call.ticket.run_id for call in stop_calls} == {final[0]}
        # 次数独立：活动核实 1 次、停止核实 2 次，各自上限独立保存。
        assert owned.connection.execute(
            "SELECT r.attempts_used, r.max_attempts_used FROM operation_runs r"
            " WHERE r.id = ?", (activity_run[0],)).fetchone() == (1, 2)
        assert owned.connection.execute(
            "SELECT r.attempts_used, r.max_attempts_used FROM operation_runs r"
            " WHERE r.id = ?", (final[0],)).fetchone() == (2, 2)
    finally:
        owned.connection.close()


async def test_running_query_attempt_recovered_by_new_session(tmp_path, start_case, monkeypatch):
    """实际QUERY意图中断后，残留流程沿正式声明恢复原尝试并保持预算。"""
    from dataclasses import replace
    from types import SimpleNamespace
    from camctl.capture.recovery import RecoveryBoundary
    from camctl.capture.residual import residual_flow
    from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
    from camctl.devices.evidence import EvidenceContract
    from camctl.devices.ports import DriverDeclaration
    from camctl.operations.attempts import BeginDisposition
    from camctl.persistence.repositories.operations import OperationRepository

    stopper = SentStop()
    owned, runtime, script = await _public_ready(tmp_path, start_case, stopper=stopper, query_maximum=2)
    try:
        assert _run(owned, 'stop/1')[1:] == (2, 1)
        start_case.wall[0] = _NOW + 63_000_000
        start_case.mono[0] = _ANCHOR + 63_000_000_000
        attempts = []
        begin = OperationRepository.begin_attempt
        class QueryIntentSaved(Exception):
            pass
        def after_intent(repository, request, key, current):
            receipt = begin(repository, request, key, current)
            if request.kind is OperationKind.QUERY_ACTIVITY and request.query_purpose is QueryPurpose.STOP_CONFIRMATION:
                assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
                assert receipt.value.disposition is BeginDisposition.GRANTED
                attempts.append(receipt.value.ticket)
                return replace(receipt, handoff_error=QueryIntentSaved())
            return receipt
        with monkeypatch.context() as patch:
            patch.setattr(OperationRepository, 'begin_attempt', after_intent)
            with pytest.raises(QueryIntentSaved):
                await capture_handler('camera_record')(start_case.aid, runtime)
        ticket, = attempts
        assert owned.connection.execute('SELECT status FROM operation_attempts WHERE run_id=?',
            (ticket.run_id,)).fetchone() == (1,)
        horizon, = owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()
        idle, active = IdleQuery(), RecordingQuery('1')
        class CurrentQuery:
            async def query_state(self, request):
                port = idle if request.ticket.responsibility_key.startswith('query/stop/') else active
                return await port.query_state(request)
        query = CurrentQuery()
        driver = start_case.options['driver']
        evidence = start_case.options['evidence'].with_contract(EvidenceContract(
            'adb_foreground_recovery', 1, 'query', frozenset()))
        registry = DriverRegistry((DriverEntry('camctl-adb', SimpleNamespace(
            control=driver.control, stop=start_case.stopper.stop, query_state=query.query_state),
            DriverDeclaration(control_supported=True, stop_supported=True, query_supported=True,
                result_supported=False, read_supported=False, digest_supported=False, delete_supported=False,
                adb_foreground_recovery_operations=frozenset({'query'})), evidence,
            DriverStatus.SOFTWARE_CONTRACT_VERIFIED),))
        start_case.wall[0] = _NOW + 65_000_000
        start_case.mono[0] = _ANCHOR + 65_000_000_000
        resumed = await start_case.reopen(owned, driver, wall=start_case.wall, mono=start_case.mono,
            query=query, stop_config=AttemptConfig(3, Decimal('.125'), Decimal('1')),
            query_config=AttemptConfig(2, Decimal('.5'), Decimal('1')),
            assembly_options={'drivers': registry, 'evidence': evidence,
                'recovery_boundary': RecoveryBoundary.HOST_LOCAL_SETTLED,
                'recovery_max_event_id': lambda: horizon})
        await residual_flow(start_case.factory)(SimpleNamespace(database=resumed.database,
            clock=SimpleNamespace(utc_micros=lambda: start_case.wall[0])))
        attempt = owned.connection.execute(
            'SELECT status,effect_state,result_json,error_json FROM operation_attempts WHERE run_id=?',
            (ticket.run_id,)).fetchone()
        assert attempt[:2] == (4, 1)
        saved = json.loads(attempt[2])
        assert saved['settlement'] == {'basis': 'assumed',
            'evidence': {'type': 'adb_foreground_recovery', 'version': 1, 'data': {}}}
        assert json.loads(attempt[3]) == {'code': 'result_not_saved', 'stage': 'recovery', 'details': {}}
        await capture_handler('camera_record')(start_case.aid, resumed)
        assert idle.calls == []
        start_case.mono[0] = _ANCHOR + 69_000_000_000
        await capture_handler('camera_record')(start_case.aid, resumed)
        confirmation = _run(owned, 'query/stop/1/1')
        assert confirmation[0] == ticket.run_id
        assert confirmation[1:] == (3, 2)
        assert len(idle.calls) == 1
        assert idle.calls[0].ticket.attempt_id == 2
        assert stopper.calls == ['stop_recording']
        activity = row_facts(owned.connection, 'device_activities', _activity_id(owned, 1))
        assert (activity['activity_state'], activity['occupancy_state']) == (3, 2)
        assert len(active.calls) == 1
    finally:
        owned.connection.close()

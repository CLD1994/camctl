"""五种查询用途的真实消费者矩阵：责任、证据与收场分区。

每次状态查询按固定用途、目标和责任键保存；可靠观察与其他操作
已保存的证据足以满足判断时直接结束责任，不为流程形式追加查询。
本文件覆盖普通录像侧的启动核实、活动核实与普通停止核实消费者，
文件内测试全部使用真实处理器、仓储与守卫，查询端点只提供正式
CallOutcome。
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from camctl.capture.handlers import (
    SessionRecordingState, capture_handler,
)
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import DeviceCallResult
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import (
    AttemptStatus, CallInfo, CallOutcome, EffectState, ErrorValue, EvidenceValue,
    Settlement, SettlementBasis,
)
from camctl.persistence.transaction import row_facts

from .test_call_result_save import _CALL_EVIDENCE, _started
from .test_recording_start_runtime import (
    _ANCHOR, _NOW, StartDriver, _configured, _result, _world,
)

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


async def _ready(owned, runtime, *, stopper, extra_seconds=0):
    """真实确认启动后到达停止时点并发送一次未确认停止。"""
    runtime.evidence = _CALL_EVIDENCE
    await capture_handler("camera_record")(12, runtime)
    runtime.stop_config = AttemptConfig(3, Decimal("0.125"), Decimal("1"))
    runtime.stopper = stopper
    seconds = 61 + extra_seconds
    runtime.wall_us = lambda: _NOW + seconds * 1_000_000
    runtime.monotonic_ns = lambda: _ANCHOR + seconds * 1_000_000_000
    await capture_handler("camera_record")(12, runtime)
    return runtime


def _activity_id(owned, action_id=12):
    return owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id = ?",
        (action_id,)).fetchone()[0]


def _run(owned, key):
    return owned.connection.execute(
        "SELECT id, status, attempts_used FROM operation_runs"
        " WHERE responsibility_key = ?", (key,)).fetchone()


async def test_unconfirmed_stop_confirmed_by_idle_query_without_new_stop(tmp_path):
    """停止核实消费者：未确认停止由空闲观察收场，零重发停止。

    停止命令已发送但没有确认时，重试等待到达后先用原停止核实责
    任查询：可靠空闲即按停止事实结束查询与原停止流程，活动结束
    与条件释放同事务，不再追加停止调用。
    """
    owned = _world(tmp_path)
    try:
        runtime = _configured(owned, StartDriver(_result(confirmed=True)))
        stopper = SentStop()
        await _ready(owned, runtime, stopper=stopper)
        stop_run = _run(owned, "stop/12")
        assert stop_run[1:] == (2, 1)
        # 重试等待已到，本会话装配查询端点。
        runtime.wall_us = lambda: _NOW + 63_000_000
        runtime.monotonic_ns = lambda: _ANCHOR + 63_000_000_000
        query = IdleQuery()
        runtime.state_query = query
        await capture_handler("camera_record")(12, runtime)

        assert stopper.calls == ["stop_recording"]
        assert len(query.calls) == 1
        assert query.calls[0].operation == "query"
        stop_confirmation = _run(owned, f"query/stop/12/{_activity_id(owned)}")
        assert stop_confirmation[1:] == (3, 1)
        assert _run(owned, "stop/12")[1] == 3
        activity = row_facts(owned.connection, "device_activities",
                             _activity_id(owned))
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


async def test_reconcile_confirms_continuity_before_stop(tmp_path):
    """活动核实消费者：墙钟满足后先确认归属连续，再执行停止。

    跨会话对账进入停止前，经 `query/activity` 责任确认当前仍是同
    一活动在录；归属确认后停止按原预算执行，确认观察随尝试保存。
    """
    owned = _world(tmp_path)
    try:
        runtime = _configured(owned, StartDriver(_result(confirmed=True)))
        runtime.evidence = _CALL_EVIDENCE
        await capture_handler("camera_record")(12, runtime)
        runtime.stop_config = AttemptConfig(1, Decimal("0.125"), Decimal("1"))
        stopper = SentStop()
        runtime.stopper = stopper
        runtime.wall_us = lambda: _NOW + 61_000_000
        runtime.monotonic_ns = lambda: _ANCHOR + 61_000_000_000
        # 新会话不继承单调锚点：进入按已保存启动墙钟的对账。
        runtime.recording_state = SessionRecordingState(runtime)
        query = RecordingQuery(str(_activity_id(owned)))
        runtime.state_query = query
        await capture_handler("camera_record")(12, runtime)

        assert len(query.calls) == 1
        observation = _run(owned, f"query/activity/12/{_activity_id(owned)}")
        assert observation[1:] == (3, 1)
        result = owned.connection.execute(
            "SELECT result_json FROM operation_attempts WHERE run_id = ?",
            (observation[0],)).fetchone()
        assert json.loads(result[0])["observations"] == [
            {"type": "activity_status", "version": 1,
             "data": {"activity_id": str(_activity_id(owned))}}]
        # 归属确认后停止执行且未确认：沿原停止预算保留。
        assert stopper.calls == ["stop_recording"]
        assert _run(owned, "stop/12")[1:] == (2, 1)
        activity = row_facts(owned.connection, "device_activities",
                             _activity_id(owned))
        assert (activity["activity_state"], activity["occupancy_state"]) == (2, 1)
    finally:
        owned.connection.close()


async def test_reconcile_idle_query_concludes_activity_without_stop(tmp_path):
    """活动核实消费者：空闲观察证明活动已结束，不重新执行停止。"""
    owned = _world(tmp_path)
    try:
        runtime = _configured(owned, StartDriver(_result(confirmed=True)))
        runtime.evidence = _CALL_EVIDENCE
        await capture_handler("camera_record")(12, runtime)
        runtime.stop_config = AttemptConfig(1, Decimal("0.125"), Decimal("1"))
        stopper = SentStop()
        runtime.stopper = stopper
        runtime.wall_us = lambda: _NOW + 61_000_000
        runtime.monotonic_ns = lambda: _ANCHOR + 61_000_000_000
        runtime.recording_state = SessionRecordingState(runtime)
        query = IdleQuery()
        runtime.state_query = query
        await capture_handler("camera_record")(12, runtime)

        assert stopper.calls == []
        assert len(query.calls) == 1
        observation = _run(owned, f"query/activity/12/{_activity_id(owned)}")
        assert observation[1:] == (3, 1)
        # 空闲确认后录像状态机识别停止已完成，重入不再停止或查询。
        assert runtime.recording_state.recording_state(12).stop_confirmed
        before = tuple(owned.connection.iterdump())
        await capture_handler("camera_record")(12, runtime)
        assert tuple(owned.connection.iterdump()) == before
        assert stopper.calls == [] and len(query.calls) == 1
    finally:
        owned.connection.close()


async def test_reconcile_failed_query_waits_without_stop(tmp_path):
    """活动核实消费者：归属不明时不进入墙钟成功分支。

    查询调用失败时归属与连续性未取得，只保存错误与重试等待；间
    隔到达后重新核实，不凭计时对账直接停止。
    """
    owned = _world(tmp_path)
    try:
        runtime = _configured(owned, StartDriver(_result(confirmed=True)))
        runtime.evidence = _CALL_EVIDENCE
        await capture_handler("camera_record")(12, runtime)
        runtime.stop_config = AttemptConfig(1, Decimal("0.125"), Decimal("1"))
        stopper = SentStop()
        runtime.stopper = stopper
        wall = [_NOW + 61_000_000]
        runtime.wall_us = lambda: wall[0]
        runtime.monotonic_ns = lambda: _ANCHOR + 61_000_000_000
        runtime.recording_state = SessionRecordingState(runtime)
        query = FailedQuery()
        runtime.state_query = query
        await capture_handler("camera_record")(12, runtime)

        assert stopper.calls == []
        observation = _run(owned, f"query/activity/12/{_activity_id(owned)}")
        assert observation[1:] == (2, 1)
        activity = row_facts(owned.connection, "device_activities",
                             _activity_id(owned))
        assert (activity["activity_state"], activity["occupancy_state"]) == (2, 1)
        # 重试间隔未到：重入不查询也不停止。
        await capture_handler("camera_record")(12, runtime)
        assert len(query.calls) == 1 and stopper.calls == []
        # 间隔到达后重新核实，仍不停止。
        wall[0] = _NOW + 63_000_000
        runtime.monotonic_ns = lambda: _ANCHOR + 63_000_000_000
        await capture_handler("camera_record")(12, runtime)
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


async def test_unconfirmed_stop_unreliable_query_keeps_retrying_stop(tmp_path):
    """停止核实矩阵：调用失败没有可靠观察时保存事实，停止继续。"""
    owned = _world(tmp_path)
    try:
        runtime = _configured(owned, StartDriver(_result(confirmed=True)))
        stopper = SentStop()
        await _ready(owned, runtime, stopper=stopper)
        query = FailedQuery()
        runtime.state_query = query
        runtime.query_config = AttemptConfig(3, Decimal("0.5"), Decimal("1"))
        runtime.wall_us = lambda: _NOW + 63_000_000
        runtime.monotonic_ns = lambda: _ANCHOR + 63_000_000_000
        await capture_handler("camera_record")(12, runtime)

        # 无可靠观察：停止核实继续，停止沿原预算同轮重试。
        confirmation = _run(owned, f"query/stop/12/{_activity_id(owned)}")
        assert confirmation[1:] == (2, 1)
        assert stopper.calls == ["stop_recording", "stop_recording"]
        activity = row_facts(owned.connection, "device_activities",
                             _activity_id(owned))
        assert (activity["activity_state"], activity["occupancy_state"]) == (2, 1)
    finally:
        owned.connection.close()


async def test_stop_confirmation_confirmed_with_error_saves_observation(tmp_path):
    """停止核实矩阵：仍在录确认与调用错误并存时完整保存。"""
    owned = _world(tmp_path)
    try:
        runtime = _configured(owned, StartDriver(_result(confirmed=True)))
        stopper = SentStop()
        await _ready(owned, runtime, stopper=stopper)
        query = StillRecordingWithErrorQuery(str(_activity_id(owned)))
        runtime.state_query = query
        runtime.query_config = AttemptConfig(3, Decimal("0.5"), Decimal("1"))
        runtime.wall_us = lambda: _NOW + 63_000_000
        runtime.monotonic_ns = lambda: _ANCHOR + 63_000_000_000
        await capture_handler("camera_record")(12, runtime)

        confirmation = _run(owned, f"query/stop/12/{_activity_id(owned)}")
        assert confirmation[1:] == (2, 1)
        attempt = owned.connection.execute(
            "SELECT status, effect_state, result_json, error_json"
            " FROM operation_attempts WHERE run_id = ?",
            (confirmation[0],)).fetchone()
        assert attempt[:2] == (3, 3)
        saved = json.loads(attempt[2])
        assert saved["observations"] == [
            {"type": "activity_status", "version": 1,
             "data": {"activity_id": str(_activity_id(owned))}}]
        assert json.loads(attempt[3]) == {
            "code": "transport_timeout", "stage": "transport", "details": {}}
        # 仍在录：活动不结束，停止沿原预算继续重试。
        assert stopper.calls == ["stop_recording", "stop_recording"]
        activity = row_facts(owned.connection, "device_activities",
                             _activity_id(owned))
        assert (activity["activity_state"], activity["occupancy_state"]) == (2, 1)
    finally:
        owned.connection.close()


async def test_stop_confirmation_budget_exhausted_unconfirmed_back_to_stop(tmp_path):
    """停止核实矩阵：持续在录耗尽查询预算按 UNCONFIRMED，停止重试。"""
    owned = _world(tmp_path)
    try:
        runtime = _configured(owned, StartDriver(_result(confirmed=True)))
        stopper = SentStop()
        await _ready(owned, runtime, stopper=stopper)
        runtime.query_config = AttemptConfig(1, Decimal("0.5"), Decimal("1"))
        runtime.state_query = RecordingQuery(str(_activity_id(owned)))
        wall = [_NOW + 63_000_000]
        runtime.wall_us = lambda: wall[0]
        mono = [_ANCHOR + 63_000_000_000]
        runtime.monotonic_ns = lambda: mono[0]
        await capture_handler("camera_record")(12, runtime)
        # 仍在录观察保存后停止链同轮重试。
        assert len(runtime.state_query.calls) == 1
        assert stopper.calls == ["stop_recording", "stop_recording"]
        confirmation = _run(owned, f"query/stop/12/{_activity_id(owned)}")
        assert confirmation[1:] == (2, 1)

        # 间隔到达：预算已尽，查询按 UNCONFIRMED 收场并交回停止链。
        wall[0] = _NOW + 65_000_000
        mono[0] = _ANCHOR + 65_000_000_000
        await capture_handler("camera_record")(12, runtime)
        assert _run(owned, f"query/stop/12/{_activity_id(owned)}")[1:] == (6, 1)
        assert len(runtime.state_query.calls) == 1
        assert stopper.calls == ["stop_recording"] * 3
        assert _run(owned, "stop/12")[1:] == (2, 3)
    finally:
        owned.connection.close()


async def test_canceled_capture_uses_stop_confirmation_without_new_stop(tmp_path):
    """取消链矩阵：取消后停止核实仍驱动收场，取消终态同事实成立。"""
    from .test_cancel_late_start import _cancel

    owned = _world(tmp_path)
    try:
        runtime = _configured(owned, StartDriver(_result(confirmed=True)))
        stopper = SentStop()
        await _ready(owned, runtime, stopper=stopper)
        _cancel(owned, 12)
        query = IdleQuery()
        runtime.state_query = query
        runtime.wall_us = lambda: _NOW + 63_000_000
        runtime.monotonic_ns = lambda: _ANCHOR + 63_000_000_000
        await capture_handler("camera_record")(12, runtime)
        # 收场后重入按已保存取消事实收终态。
        await capture_handler("camera_record")(12, runtime)

        # 取消生效：不经计时立即停止；未确认后由停止核实收场。
        assert stopper.calls == ["stop_recording"]
        assert len(query.calls) == 1
        confirmation = _run(owned, f"query/stop/12/{_activity_id(owned)}")
        assert confirmation[1:] == (3, 1)
        activity = row_facts(owned.connection, "device_activities",
                             _activity_id(owned))
        assert (activity["activity_state"], activity["occupancy_state"]) == (3, 2)
        assert owned.connection.execute(
            "SELECT status, cancel_requested FROM actions WHERE id = 12"
        ).fetchone() == (6, 1)
    finally:
        owned.connection.close()

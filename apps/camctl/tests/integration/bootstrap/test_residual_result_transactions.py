"""残留收场查询的事务闭合：查询结果、残留停止与活动收场同事务。

执行前检查与残留停止核实是后续动作对历史残留的两种查询责任。
本文件用真实处理器、仓储与守卫验证：可靠空闲确认把查询尝试结
果、查询流程成功、原残留停止流程结束及活动结束观察与条件释放
并入同一事务身份；其他分区沿各自责任保存事实。
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from camctl.capture.handlers import (
    _settle_stop_exhausted, _stop_call, capture_handler,
)
from camctl.capture.residual import ResidualCandidate, _stop_residual
from camctl.devices.ports import DeviceCallResult
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import (
    AttemptStatus, CallInfo, CallOutcome, EffectState, ErrorValue, EvidenceValue,
    Settlement, SettlementBasis,
)
from camctl.persistence.transaction import row_facts

from ..capture.test_call_result_save import (
    _CALL_EVIDENCE, ReturnedCall, _outcome, _started,
)
from ..capture.test_capture_contract import _seed_action
from ..capture.test_recording_queries import (
    FailedQuery, IdleQuery, RecordingQuery, SentStop,
)
from ..capture.test_recording_start_runtime import _ANCHOR, _NOW, _world
from ..scheduling.test_resources import _seed_activity

pytestmark = pytest.mark.asyncio


def _run(owned, key):
    return owned.connection.execute(
        "SELECT id, status, attempts_used FROM operation_runs"
        " WHERE responsibility_key = ?", (key,)).fetchone()


def _activity_id(owned, action_id=12):
    return owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id = ?",
        (action_id,)).fetchone()[0]


async def _residual_sent(tmp_path, *, stopper=None):
    """原录像停止预算耗尽后由后续动作发送一次未确认残留停止。"""
    from camctl.capture.handlers import capture_handler as handler

    owned = _world(tmp_path)
    runtime = await _started(owned)
    runtime.stopper = ReturnedCall(_outcome("stop"))
    await _stop_call(runtime, runtime.action(12))
    _settle_stop_exhausted(runtime, runtime.action(12))
    assert owned.connection.execute(
        "SELECT status FROM actions WHERE id = 12").fetchone() == (4,)
    _seed_action(owned.connection, 13, 2)
    _seed_activity(owned.connection, 13)
    sent = SentStop() if stopper is None else stopper
    runtime.stopper = sent
    runtime.query_config = AttemptConfig(2, Decimal("0.5"), Decimal("1"))
    await _stop_residual(runtime, 13, ResidualCandidate(12, 12, 1, 1))
    winddown = _run(owned, "followup/13/12")
    assert winddown[1:] == (2, 1)
    return owned, runtime, sent


async def test_residual_confirm_query_settles_in_one_transaction(tmp_path):
    """残留停止核实：空闲确认的查询、残留停止与活动收场同事务。"""
    owned, runtime, stopper = await _residual_sent(tmp_path)
    try:
        from camctl.capture.residual import _advance_winddown

        query = IdleQuery()
        runtime.state_query = query
        # 残留停止未确认：推进先经确认查询核实。
        winddown_row = _run(owned, "followup/13/12")
        await _advance_winddown(
            runtime, ResidualCandidate(12, 12, 1, 1),
            (winddown_row[0], 13, _activity_id(owned)))

        assert stopper.calls == ["stop_recording"]
        assert len(query.calls) == 1
        assert query.calls[0].operation == "query"
        # 绑定取目标活动所属动作 A 保存的设备绑定；查询责任与历史
        # owner 沿触发动作 B，两者分开保存。
        from camctl.devices.bindings import DeviceBinding

        assert query.calls[0].binding == DeviceBinding("cam-1", "camctl-adb")
        assert query.calls[0].ticket.responsibility_key == (
            f"query/residual/13/{_activity_id(owned)}")
        assert owned.connection.execute(
            "SELECT action_id FROM operation_runs WHERE id = ?",
            (_run(owned, f"query/residual/13/{_activity_id(owned)}")[0],)).fetchone() == (13,)
        assert owned.connection.execute(
            "SELECT action_id FROM device_activities WHERE id = ?",
            (_activity_id(owned),)).fetchone() == (12,)
        confirmation = _run(owned, f"query/residual/13/{_activity_id(owned)}")
        assert confirmation[1:] == (3, 1)
        winddown = _run(owned, "followup/13/12")
        assert winddown[1] == 3
        activity = row_facts(owned.connection, "device_activities",
                             _activity_id(owned))
        assert (activity["activity_state"], activity["occupancy_state"]) == (3, 2)
        # 查询结果、残留停止结束、活动结束与释放同事务。
        transactions = set()
        for transaction_id, body in owned.connection.execute(
                "SELECT transaction_id, body_json FROM history_events ORDER BY id"):
            for row in json.loads(body)["rows"]:
                values = row["after"]["values"]
                if row["table"] == "operation_runs" and row["id"] == confirmation[0] \
                        and values.get("status") == 3:
                    transactions.add(("query", transaction_id))
                if row["table"] == "operation_runs" and row["id"] == winddown[0] \
                        and values.get("status") == 3:
                    transactions.add(("residual_stop", transaction_id))
                if row["table"] == "device_activities" and row["id"] == activity["id"]:
                    if values.get("activity_state") == 3:
                        transactions.add(("ended", transaction_id))
                    if values.get("occupancy_state") == 2:
                        transactions.add(("released", transaction_id))
        marks = {name for name, _ in transactions}
        ids = {txn for _, txn in transactions}
        assert marks == {"query", "residual_stop", "ended", "released"}
        assert len(ids) == 1
    finally:
        owned.connection.close()


async def test_residual_confirm_query_still_recording_continues_stop(tmp_path):
    """残留停止核实：仍在录时保存观察并继续停止，活动不收场。"""
    owned, runtime, stopper = await _residual_sent(tmp_path)
    try:
        from camctl.capture.residual import _advance_winddown

        query = RecordingQuery(str(_activity_id(owned)))
        runtime.state_query = query
        runtime.monotonic_ns = lambda: _ANCHOR + 63_000_000_000
        runtime.wall_us = lambda: _NOW + 63_000_000
        winddown_row = _run(owned, "followup/13/12")
        flow = (winddown_row[0], 13, _activity_id(owned))
        candidate = ResidualCandidate(12, 12, 1, 1)
        await _advance_winddown(runtime, candidate, flow)
        # 仍在录：确认查询保存观察，核实责任按间隔继续。
        confirmation = _run(owned, f"query/residual/13/{_activity_id(owned)}")
        assert confirmation[1:] == (2, 1)
        # 第二次查询仍在录；预算耗尽后按 UNCONFIRMED 交回停止链。
        runtime.monotonic_ns = lambda: _ANCHOR + 65_000_000_000
        runtime.wall_us = lambda: _NOW + 65_000_000
        await _advance_winddown(runtime, candidate, flow)
        assert len(query.calls) == 2
        assert _run(owned, f"query/residual/13/{_activity_id(owned)}")[1:] == (2, 2)
        runtime.monotonic_ns = lambda: _ANCHOR + 67_000_000_000
        runtime.wall_us = lambda: _NOW + 67_000_000
        await _advance_winddown(runtime, candidate, flow)
        assert len(query.calls) == 2
        assert _run(owned, f"query/residual/13/{_activity_id(owned)}")[1:] == (6, 2)
        assert stopper.calls == ["stop_recording", "stop_recording"]
        assert _run(owned, "followup/13/12")[1:] == (2, 2)
        activity = row_facts(owned.connection, "device_activities",
                             _activity_id(owned))
        assert (activity["activity_state"], activity["occupancy_state"]) == (2, 1)
    finally:
        owned.connection.close()


async def test_residual_confirm_query_failure_continues_stop_budget(tmp_path):
    """残留停止核实：查询调用失败不反复查询，直接继续停止。"""
    owned, runtime, stopper = await _residual_sent(tmp_path)
    try:
        from camctl.capture.residual import _advance_winddown

        query = FailedQuery()
        runtime.state_query = query
        runtime.monotonic_ns = lambda: _ANCHOR + 63_000_000_000
        runtime.wall_us = lambda: _NOW + 63_000_000
        winddown_row = _run(owned, "followup/13/12")
        await _advance_winddown(
            runtime, ResidualCandidate(12, 12, 1, 1),
            (winddown_row[0], 13, _activity_id(owned)))
        await _advance_winddown(
            runtime, ResidualCandidate(12, 12, 1, 1),
            (winddown_row[0], 13, _activity_id(owned)))

        # 查询失败一次后不再查询：第二次残留停止执行。
        assert len(query.calls) == 1
        assert stopper.calls == ["stop_recording", "stop_recording"]
        confirmation = _run(owned, f"query/residual/13/{_activity_id(owned)}")
        assert confirmation[1:] == (2, 1)
        assert _run(owned, "followup/13/12")[1:] == (2, 2)
    finally:
        owned.connection.close()

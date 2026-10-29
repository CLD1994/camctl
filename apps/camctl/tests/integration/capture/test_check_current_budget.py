"""产物核实责任跨运行采用本次上限，原尝试与终态保持。"""

from decimal import Decimal
from pathlib import Path

import pytest

from camctl.capture.handlers import capture_handler
from camctl.operations.attempts import AttemptConfig
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..support.result_cases import result_case
from .test_capture_contract import ResultsDouble

pytestmark = pytest.mark.asyncio


def _result_run(owned, activity_id):
    return owned.connection.execute(
        "SELECT id,status,attempts_used,retry_wait_required FROM operation_runs"
        " WHERE responsibility_key=?", (f"results/{activity_id}",)).fetchone()


def _attempts(owned, run_id):
    return owned.connection.execute(
        "SELECT attempt_no,status,result_json,error_json FROM operation_attempts"
        " WHERE run_id=? ORDER BY attempt_no", (run_id,)).fetchall()


@pytest.mark.parametrize("new_limit,additional", [(5, 3), (1, 0)], ids=["higher", "lower"])
async def test_check_budget_change_adds_only_remaining_attempts(tmp_path, result_case, new_limit, additional):
    """第一运行已用 2 次后换限：仅补足剩余名额或零新尝试，原尝试保持。

    集合未确定的重试等待不关闭原流程；本次运行采用新上限，旧尝试
    的结果、时刻与累计次数不被改写。已收场的 UNCONFIRMED 不因后续
    运行重开。读取、重拷、启动、停止、删除与查询责任的同形矩阵由
    各自专项覆盖（见运行配置计划 RC3 记录）。
    """
    owned, runtime, action_id, handler = await result_case.world(tmp_path, "photo",
        assembly_options={"check_config": AttemptConfig(2, Decimal("1.25"), Decimal(0))})
    activity_id, = owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
    first = ResultsDouble({activity_id: ()}, set_finalized=False)
    result_case.results.delegate = first
    advance = capture_handler(handler)
    try:
        await advance(action_id, runtime)
        await advance(action_id, runtime)
        run_id, status, used, wait = _result_run(owned, activity_id)
        assert (status, used, wait) == (2, 2, 1)
        assert first.calls == [activity_id, activity_id]
        original_attempts = _attempts(owned, run_id)
        original_history = owned.connection.execute(
            "SELECT id FROM history_events ORDER BY id").fetchall()
        db_path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
        owned.connection.close()

        reopened = open_existing(db_path, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            resumed = await result_case.reopen(reopened, cold=True,
                assembly_options={"check_config": AttemptConfig(new_limit, Decimal("1.25"), Decimal(0))})
            second = ResultsDouble({activity_id: ()}, set_finalized=False)
            result_case.results.delegate = second
            for _ in range(additional + 1):
                await advance(action_id, resumed)

            _, status, used, wait = _result_run(reopened, activity_id)
            assert (status, used, wait) == (6, 2 + additional, 0)
            assert second.calls == [activity_id] * additional
            assert _attempts(reopened, run_id)[:2] == original_attempts
            assert reopened.connection.execute(
                "SELECT id FROM history_events ORDER BY id"
            ).fetchall()[:len(original_history)] == original_history

            # 已收场的核实流程不因再次运行或更高上限重开。
            again = await result_case.reopen(reopened, cold=True,
                assembly_options={"check_config": AttemptConfig(9, Decimal("1.25"), Decimal(0))})
            third = ResultsDouble({activity_id: ()}, set_finalized=False)
            result_case.results.delegate = third
            await advance(action_id, again)
            assert third.calls == []
            assert _result_run(reopened, activity_id)[1:] == (6, 2 + additional, 0)
        finally:
            reopened.connection.close()
    finally:
        owned.connection.close()

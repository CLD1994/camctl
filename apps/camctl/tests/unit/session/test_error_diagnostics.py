"""原读取和关闭诊断经过后台消费、会话结果与收场仍完整交付。"""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest

from camctl.bootstrap.background_flow import BackgroundFlow, CombinedLocalWork
from camctl.cli import encode_session_result
from camctl.operations.error_diagnostics import DiagnosticError
from camctl.session.service import (
    _RequiredPhaseWaiter,
    _merge_cleanup_errors,
    run_session,
)

from .test_business_database_session import world

pytestmark = pytest.mark.asyncio


def failure(message="原读取失败"):
    error = DiagnosticError(message, diagnostic_details={
        "stderr_hex": "00ff80" * 100, "transport_error": "原始传输详情",
    })
    try:
        raise error from OSError(5, "底层失败", "/source")
    except DiagnosticError:
        return error


def read_and_close_failure():
    error = ExceptionGroup("读取与会话关闭均失败", [
        failure(), *[failure(f"关闭失败-{index}") for index in range(32)],
    ])
    error.add_note("原异常组附注")
    return error


@pytest.mark.parametrize("delivery", ["check_completed", "flow"])
@pytest.mark.parametrize("grouped", [False, True])
async def test_background_failure_is_complete_in_final_machine_result(delivery, grouped):
    context, database, _, _, _, _ = world()
    original = read_and_close_failure() if grouped else failure()
    release = asyncio.Event()

    async def body(_):
        if delivery == "check_completed":
            await release.wait()
        raise original

    owner = BackgroundFlow(body)
    context.local_work = CombinedLocalWork((owner,))
    context.flows = {"obtain": owner}
    if delivery == "check_completed":
        await owner(context)
        release.set()
        await asyncio.sleep(0)

    outcome = await run_session(context, None)

    assert outcome.reason == "state_db_error"
    prefix = "后台流程异常: " if delivery == "check_completed" else "流程 obtain 异常: "
    assert outcome.details["error"] == prefix + str(original)
    diagnostic = outcome.details["exception"]
    if grouped:
        assert diagnostic["notes"] == ["原异常组附注"]
        assert len(diagnostic["exceptions"]) == 33
        diagnostic = diagnostic["exceptions"][0]
    assert diagnostic["diagnostic_details"] == original_leaf(original).diagnostic_details
    assert diagnostic["traceback"]
    assert diagnostic["cause"]["errno"] == 5
    encoded = encode_session_result(outcome).decode("utf-8")
    assert "00ff80" * 100 in encoded
    if grouped:
        assert "关闭失败-31" in encoded
    database.executor.close.assert_awaited_once()


def original_leaf(error):
    return error.exceptions[0] if isinstance(error, BaseExceptionGroup) else error


@pytest.mark.parametrize("grouped", [False, True])
async def test_actual_local_settlement_failure_preserves_group_and_leaf_diagnostics(grouped):
    context, database, _, _, _, _ = world()
    original = read_and_close_failure() if grouped else failure()
    context.local_work = type("LocalWork", (), {
        "required_settlements": Mock(return_value=0),
        "stop_new_work": Mock(),
        "check_completed": Mock(),
        "settle": AsyncMock(side_effect=[original, None]),
    })()

    outcome = await run_session(context, None)

    assert outcome.reason == "state_db_error"
    assert outcome.details["stage"] == "shutdown"
    assert outcome.details["message"] == "DiagnosticError: 原读取失败"
    diagnostic = outcome.details["exception"]
    if grouped:
        assert diagnostic["type"] == "builtins.ExceptionGroup"
        assert len(diagnostic["exceptions"]) == 33
        assert diagnostic["notes"] == ["原异常组附注"]
        assert len(outcome.details["secondary_errors"]) == 32
        diagnostic = diagnostic["exceptions"][0]
    assert diagnostic["diagnostic_details"] == original_leaf(original).diagnostic_details
    database.executor.close.assert_awaited_once()


async def test_cleanup_filter_retains_original_group_while_summary_skips_observed_leaf():
    first, second = failure("已经交付"), failure("本次收场")
    original = ExceptionGroup("完整分组", [first, second])
    original.add_note("组级证据")

    outcome, control = _merge_cleanup_errors(
        None, None, [(original, "shutdown", "state_db_error")], [first],
    )

    assert control is None
    assert outcome.details["message"] == "DiagnosticError: 本次收场"
    assert "secondary_errors" not in outcome.details
    assert outcome.details["exception"]["notes"] == ["组级证据"]
    assert [child["message"] for child in outcome.details["exception"]["exceptions"]] == [
        "已经交付", "本次收场",
    ]


@pytest.mark.parametrize("delivery", ["check_completed", "flow"])
async def test_group_containing_cancel_preserves_control_identity_and_cleanup_details(delivery):
    context, database, _, _, _, _ = world()
    cancelled, close_error = asyncio.CancelledError("原读取取消"), failure("源关闭失败")
    original = BaseExceptionGroup("读取与关闭均失败", [cancelled, close_error])
    cleanup_error = failure("数据库收场失败")
    database.close = AsyncMock(side_effect=cleanup_error)
    release = asyncio.Event()

    async def body(_):
        if delivery == "check_completed":
            await release.wait()
        raise original

    owner = BackgroundFlow(body)
    context.local_work = CombinedLocalWork((owner,))
    context.flows = {"obtain": owner}
    if delivery == "check_completed":
        await owner(context)
        release.set()
        await asyncio.sleep(0)

    with pytest.raises(BaseExceptionGroup) as caught:
        await run_session(context, None)

    assert caught.value is original
    assert caught.value.exceptions == (cancelled, close_error)
    assert not isinstance(caught.value, Exception)
    diagnostic_notes = [json.loads(note.split("诊断: ", 1)[1])
                        for note in original.__notes__ if "诊断: " in note]
    assert any(note["diagnostic_details"] == cleanup_error.diagnostic_details
               for note in diagnostic_notes)
    database.close.assert_awaited_once()


async def test_required_phase_note_preserves_entire_secondary_group():
    target = asyncio.CancelledError("保留原控制异常")
    original = read_and_close_failure()

    _RequiredPhaseWaiter.attach_exception_notes(target, "实际收场", original)

    diagnostics = [json.loads(note.split("诊断: ", 1)[1])
                   for note in target.__notes__ if "诊断: " in note]
    assert any(len(note.get("exceptions", [])) == 33
               and note["exceptions"][-1]["diagnostic_details"] == failure().diagnostic_details
               and note["notes"] == ["原异常组附注"]
               for note in diagnostics)

"""受理服务与实际结果模型共同交付业务诊断和数据库原错误。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import (
    AcceptanceContext, AcceptanceResult, AcceptanceStateError, AckDisposition,
    CommandMode, PlanDisposition, accept_input,
)
from camctl.contracts.values import new_operation_key
from camctl.logging_runtime.diagnostics import error_diagnostics_context, flush_diagnostics
from camctl.persistence.models import DbOutcome, DbOutcomeKind, _attach_db_outcome, db_outcome_for

pytestmark = pytest.mark.asyncio


def context():
    return AcceptanceContext(CommandMode.SUBMIT, None, None)


@pytest.mark.parametrize("kind", [DbOutcomeKind.NOT_EXECUTED, DbOutcomeKind.ROLLED_BACK, DbOutcomeKind.UNKNOWN])
async def test_failed_admission_preserves_primary_and_secondary_database_errors(kind):
    primary = OSError(5, "保存输入失败", "/state")
    secondary = RuntimeError("原连接收场失败")
    actual = DbOutcome(kind, error=primary, secondary_errors=(secondary,))
    database = SimpleNamespace(write=AsyncMock(return_value=actual))
    with pytest.raises(AcceptanceStateError) as caught:
        await accept_input(ParsedInput("/input", {}), context(), new_operation_key(), database)
    assert caught.value.__cause__.exceptions == (primary, secondary)
    assert db_outcome_for(caught.value) is actual
    assert caught.value.outcome_kind is kind
    assert database.write.await_count == 1


async def test_failed_admission_keeps_original_control_object_and_actual_result():
    control = asyncio.CancelledError("原输入取消")
    secondary = OSError(5, "原回滚失败", "/state")
    actual = DbOutcome(DbOutcomeKind.UNKNOWN, control=control, secondary_errors=(secondary,))
    database = SimpleNamespace(write=AsyncMock(return_value=actual))
    with pytest.raises(asyncio.CancelledError) as caught:
        await accept_input(ParsedInput("/input", {}), context(), new_operation_key(), database)
    assert caught.value is control
    assert db_outcome_for(control) is actual
    assert any("原回滚失败" in note for note in control.__notes__)


@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_completed_result_diagnostics_survive_observer_or_control_failure(error_type):
    result = AcceptanceResult(PlanDisposition.REJECTED, None, AckDisposition.NOT_PROCESSED, 0,
        diagnostics=({"error": {"message": "原业务拒绝"}, "context": {"action_id": 7}},))
    failure = error_type("提交后的实际交付错误")
    actual = DbOutcome(DbOutcomeKind.COMPLETED, value=result)
    _attach_db_outcome(failure, actual)
    database = SimpleNamespace(write=AsyncMock(side_effect=failure))
    diagnostics = []
    with error_diagnostics_context(None, diagnostics.append):
        with pytest.raises(error_type) as caught:
            await accept_input(ParsedInput("/input", {}), context(), new_operation_key(), database)
        await flush_diagnostics()
    assert caught.value is failure
    assert db_outcome_for(failure) is actual
    assert database.write.await_count == 1
    assert len(diagnostics) == 1
    payload = json.loads(diagnostics[0])
    assert payload["context"]["action_id"] == 7
    assert payload["context"]["input_path"] == "/input"
    assert "原业务拒绝" in diagnostics[0]

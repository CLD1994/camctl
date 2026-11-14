"""受理服务与实际结果模型共同交付业务诊断和数据库原错误。"""

import asyncio
import json
from dataclasses import replace

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import (
    AcceptanceContext, AcceptanceResult, AckDisposition,
    CommandMode, PlanDisposition, accept_input,
)
from camctl.contracts.values import new_operation_key
from camctl.logging_runtime.diagnostics import error_diagnostics_context, flush_diagnostics
from camctl.persistence.models import DbOutcome, DbOutcomeKind, db_outcome_for

from ..support.database import business_database, create_test_database

pytestmark = pytest.mark.asyncio


def context():
    return AcceptanceContext(CommandMode.SUBMIT, None, None)


@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_completed_result_diagnostics_survive_observer_or_control_failure(error_type, tmp_path):
    result = AcceptanceResult(PlanDisposition.REJECTED, None, AckDisposition.NOT_PROCESSED, 0,
        diagnostics=({"error": {"message": "原业务拒绝"}, "context": {"action_id": 7}},))
    failure = error_type("提交后的实际交付错误")
    actual = DbOutcome(DbOutcomeKind.COMPLETED, value=result,
        **({"control": failure} if error_type is asyncio.CancelledError else {"observer_error": failure}))
    calls = []

    class ResultPort:
        def process_input(self, command, key, owned):
            calls.append((command, key))
            return actual

    path = tmp_path / "state.db"
    create_test_database(path)
    diagnostics = []
    async with business_database(path) as database:
        with error_diagnostics_context(None, diagnostics.append):
            with pytest.raises(error_type) as caught:
                await accept_input(ParsedInput("/input", {}), replace(context(), repository=ResultPort()),
                    new_operation_key(), database)
            await flush_diagnostics()
    assert caught.value is failure
    assert db_outcome_for(failure) is actual
    assert len(calls) == 1
    assert len(diagnostics) == 1
    payload = json.loads(diagnostics[0])
    assert payload["context"]["action_id"] == 7
    assert payload["context"]["input_path"] == "/input"
    assert "原业务拒绝" in diagnostics[0]

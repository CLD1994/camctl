"""输入受理日志区分可靠提交与异常，保留原业务结果。"""

import json
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import (
    AcceptanceContext, AcceptanceRepository, AcceptanceResult, AcceptanceStateError,
    AckDisposition, CommandMode, PlanDisposition, accept_input,
)
from camctl.contracts.values import OperationKey
from camctl.logging_runtime.events import event_logging
from camctl.logging_runtime.service import LogChannel
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from support.business_database import inline_business_database


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [DbOutcomeKind.COMPLETED, DbOutcomeKind.ROLLED_BACK])
async def test_input_stage_preserves_result_and_reports_only_confirmed_commit(kind):
    result = AcceptanceResult(PlanDisposition.REJECTED, None,
                              AckDisposition.NOT_PROCESSED, 0)
    repository = create_autospec(AcceptanceRepository, instance=True, spec_set=True)
    repository.process_input.return_value = DbOutcome(kind, value=result)
    context = AcceptanceContext(CommandMode.SUBMIT, None, repository)
    metadata = DatabaseMetadata("camctl", "a" * 32, 1, "/staging", "/ready", "/processing")
    database = inline_business_database(OwnedConnection(
        create_autospec(sqlite3.Connection, instance=True, spec_set=True), metadata))
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    with event_logging(channel):
        if kind is DbOutcomeKind.COMPLETED:
            assert await accept_input(ParsedInput("/plan", {}), context,
                                      OperationKey("b" * 32), database) is result
        else:
            with pytest.raises(AcceptanceStateError):
                await accept_input(ParsedInput("/plan", {}), context,
                                   OperationKey("b" * 32), database)
    records = [json.loads(call.args[0].message) for call in channel.slog.call_args_list]
    assert [record["event"] for record in records] == (
        ["started", "committed", "returned"] if kind is DbOutcomeKind.COMPLETED
        else ["started", "raised"])
    assert all(record["level"] == "INFO" and record["stage"] == "acceptance"
               and record["database_id"] == metadata.instance_id
               and record["input_path"] == "/plan" for record in records)
    if kind is DbOutcomeKind.COMPLETED:
        assert records[1]["plan_disposition"] == "rejected"
        assert "plan_id" not in records[1]

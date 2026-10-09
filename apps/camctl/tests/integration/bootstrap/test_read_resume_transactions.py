"""同一未结束 READ 的配置采用及完整原键核实。"""

from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.contracts.values import new_operation_key
from camctl.operations.attempts import AttemptConfig, ReadResumeRequest, seconds_from_json
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository

from .test_output_binding_changes import _NOW, environment
from .test_output_binding_transactions import pending_read
from .test_output_read_recovery import _start_unended_read

pytestmark = pytest.mark.asyncio


def _request(owned, command):
    return ReadResumeRequest(_start_unended_read(owned, command.copy_id),
        AttemptConfig(5, Decimal("1.25"), Decimal("0.5")), _NOW)


async def test_resume_read_adopts_run_and_attempt_configuration_without_new_attempt(pending_read):
    _cfg, owned, command = pending_read
    request = _request(owned, command)
    attempts_before = owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone()[0]

    result = OperationRepository().resume_read(request, new_operation_key(), owned)

    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    run = owned.connection.execute(
        "SELECT status,attempts_used,max_attempts_used,timeout_s_json,retry_interval_s_json"
        " FROM operation_runs WHERE id=?", (request.ticket.run_id,)).fetchone()
    assert run[:3] == (2, 2, 5)
    assert (seconds_from_json(run[3]), seconds_from_json(run[4])) == (Decimal("1.25"), Decimal("0.5"))
    attempt = owned.connection.execute(
        "SELECT status,result_json,max_attempts_used,timeout_s_json,retry_interval_s_json"
        " FROM operation_attempts WHERE run_id=? AND attempt_no=?",
        (request.ticket.run_id, request.ticket.attempt_id)).fetchone()
    assert attempt[:3] == (1, None, 5)
    assert (seconds_from_json(attempt[3]), seconds_from_json(attempt[4])) == (Decimal("1.25"), Decimal("0.5"))
    assert owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone()[0] == attempts_before


@pytest.mark.parametrize("changed", ["maximum", "idle", "interval", "run", "attempt", "copy", "responsibility"])
async def test_resume_read_original_key_rejects_changed_complete_input(pending_read, changed):
    _cfg, owned, command = pending_read
    request = _request(owned, command)
    repository, key = OperationRepository(), new_operation_key()
    first = repository.resume_read(request, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    before = tuple(owned.connection.iterdump())
    values = {
        "maximum": replace(request, config=replace(request.config, max_attempts=6)),
        "idle": replace(request, config=replace(request.config, timeout_s=Decimal("2.75"))),
        "interval": replace(request, config=replace(request.config, retry_interval_s=Decimal("0.25"))),
        "run": replace(request, ticket=replace(request.ticket, run_id=request.ticket.run_id + 1)),
        "attempt": replace(request, ticket=replace(request.ticket, attempt_id=request.ticket.attempt_id + 1)),
        "copy": replace(request, ticket=replace(request.ticket, target_id=str(command.copy_id + 1))),
        "responsibility": replace(request, ticket=replace(request.ticket, responsibility_key=f"read/{command.copy_id + 1}")),
    }

    refused = repository.resume_read(values[changed], key, owned)

    assert refused.kind is DbOutcomeKind.ROLLED_BACK
    assert tuple(owned.connection.iterdump()) == before
    repeated = repository.resume_read(request, key, owned)
    assert repeated.kind is DbOutcomeKind.COMPLETED, repeated.error
    assert tuple(owned.connection.iterdump()) == before

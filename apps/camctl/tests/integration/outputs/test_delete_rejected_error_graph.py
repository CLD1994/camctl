"""公开删除及默认轮次在保存未完成时保留原数据库和监督错误图。"""

import asyncio
from contextlib import asynccontextmanager
import json
from unittest.mock import patch

from camctl.bootstrap.cleanup_assembly import HostArtifacts

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.host_files.tasks import FileTaskExecutor
from camctl.logging_runtime.diagnostics import diagnostic_scope, error_diagnostics_context
from camctl.operations.error_diagnostics import exception_diagnostic
from camctl.outputs.cleanup_flow import (
    CleanupSupervisionDiagnostic, CleanupSupervisionFault, LocalArtifactTaskFailure,
    advance_cleanup, delete_source_file,
)
from camctl.persistence.models import DbOutcome, DbOutcomeKind, db_outcome_for
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.session.supervision import Supervisor

from support.repository_ports import repository_port
from .test_cleanup_delete_save_recovery import _prepared, _configured
from ..bootstrap.test_output_binding_changes import environment
from ..support.recording_consumer_cases import recording_cases  # noqa: F401
from ..support.repaired_cleanup_cases import repaired_cleanup  # noqa: F401

pytestmark = pytest.mark.asyncio


class _SavePort(OutputsRepository):
    def __init__(self, outcome=None):
        self.outcome = outcome
        self.calls = []

    def finish_cleanup_attempt(self, request, key, owned):
        self.calls.append((request, key))
        if self.outcome is not None:
            return self.outcome
        return super().finish_cleanup_attempt(request, key, owned)


@pytest.mark.parametrize('kind', [DbOutcomeKind.NOT_EXECUTED, DbOutcomeKind.ROLLED_BACK, DbOutcomeKind.UNKNOWN])
async def test_original_delete_rejection_carries_complete_database_outcome(environment, kind):
    owned, _, runtime, driver, item_id = await _prepared(environment)
    error = ValueError('original delete save failure')
    error.__cause__ = OSError(5, 'delete save IO')
    outcome = DbOutcome(kind, error=error, secondary_errors=(RuntimeError('save close failure'),))
    port = _SavePort(outcome)
    with repository_port(OutputsRepository, port, ("finish_cleanup_attempt",),
            matches=lambda name, args, kwargs: args[0].ticket.target_id == str(item_id)):
        step = await delete_source_file(runtime, item_id)
        assert db_outcome_for(step.exception) is outcome
        with pytest.raises(ConsistencyError) as caught:
            await advance_cleanup(runtime)
    assert db_outcome_for(caught.value) is outcome
    assert db_outcome_for(caught.value).error is error
    assert db_outcome_for(caught.value).secondary_errors == outcome.secondary_errors
    assert port.calls == [port.calls[0], port.calls[0]]
    assert len(driver.deletes) == 1
    assert owned.connection.execute('SELECT result_json FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.cleanup_item_id=?',
        (item_id,)).fetchone() == (None,)


async def test_delete_save_control_keeps_original_identity_and_actual_result(environment):
    owned, _, runtime, driver, item_id = await _prepared(environment)
    control = asyncio.CancelledError('original worker cancellation')
    outcome = DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError(5, 'save IO failure'), control=control)
    port = _SavePort(outcome)
    with repository_port(OutputsRepository, port, ("finish_cleanup_attempt",),
            matches=lambda name, args, kwargs: args[0].ticket.target_id == str(item_id)):
        for _ in range(2):
            with pytest.raises(asyncio.CancelledError) as caught:
                await delete_source_file(runtime, item_id)
            assert caught.value is control
            assert db_outcome_for(control) is outcome
    assert port.calls == [port.calls[0], port.calls[0]]
    assert len(driver.deletes) == 1
    assert owned.connection.execute('SELECT result_json FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.cleanup_item_id=?',
        (item_id,)).fetchone() == (None,)


@asynccontextmanager
async def _supervised_world(repaired_cleanup, outcome=None):
    environment, _, item_id, _, _, target, content, _ = repaired_cleanup
    _, owned, _, driver = environment
    assert target.read_bytes() == content
    supervisor = Supervisor()
    runtime = await _configured(environment, owned, driver, file_executor=FileTaskExecutor(supervisor))
    original = ValueError('original runner supervision failure')
    original.__cause__ = OSError(13, 'original runner OS failure', '/staging/source')
    diagnostic = CleanupSupervisionDiagnostic((
        CleanupSupervisionFault('runner', None, None, None, (), type(original).__name__, str(original)),
        CleanupSupervisionFault('runner.cause', None, None, None, (), 'PermissionError', str(original.__cause__)),
    ), original)
    actual_delete = HostArtifacts.delete
    calls = []
    async def supervised_delete(host, request):
        result = await actual_delete(host, request)
        if request.cleanup_item_id != item_id:
            return result
        calls.append((request, result))
        raise LocalArtifactTaskFailure(result, diagnostic) from original
    saves = _SavePort(outcome)
    try:
        with patch.object(HostArtifacts, "delete", supervised_delete), \
                repository_port(OutputsRepository, saves, ("finish_cleanup_attempt",),
                    matches=lambda name, args, kwargs: args[0].ticket.target_id == str(item_id)):
            yield owned, runtime, item_id, original, saves, calls
    finally:
        await supervisor.drain_required()


async def test_original_supervision_result_replay_logs_one_complete_graph(repaired_cleanup):
    async with _supervised_world(repaired_cleanup) as (owned, runtime, item_id, original, saves, calls):
        records = []
        with error_diagnostics_context(None, records.append):
            async with diagnostic_scope():
                with pytest.raises(ConsistencyError) as delivered:
                    await delete_source_file(runtime, item_id)
                assert delivered.value.__cause__ is original
                assert (await delete_source_file(runtime, item_id)).phase == 'succeeded'
                assert (await delete_source_file(runtime, item_id)).phase == 'already_terminal'
        assert len(records) == 1
        assert 'original runner supervision failure' in records[0]
        assert '"errno":13' in records[0]
        assert json.loads(records[0])['context']['cleanup_item_id'] == item_id
        assert len(calls) == len(saves.calls) == 1
        assert owned.connection.execute('SELECT status FROM cleanup_items WHERE id=?', (item_id,)).fetchone() == (4,)


async def test_database_control_and_supervision_error_keep_original_control(repaired_cleanup):
    control = asyncio.CancelledError('original database control')
    original_cause = OSError(5, 'original save failure')
    control.__cause__ = original_cause
    outcome = DbOutcome(DbOutcomeKind.UNKNOWN, error=original_cause, control=control)
    async with _supervised_world(repaired_cleanup, outcome) as (_, runtime, item_id, original, saves, calls):
        with pytest.raises(asyncio.CancelledError) as caught:
            await delete_source_file(runtime, item_id)
        assert caught.value is control and control.__cause__ is original_cause
        assert db_outcome_for(control) is outcome
        document = json.dumps(exception_diagnostic(control), ensure_ascii=False)
        assert 'original runner supervision failure' in document and 'original runner OS failure' in document
        assert 'errno' in document
        assert len(calls) == len(saves.calls) == 1

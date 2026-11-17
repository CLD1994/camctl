"""默认本地恢复继续持有的实际停止观察，不重新打开设备。"""

from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.capture.input_copy import InputPhase, InputStep
from camctl.capture.media_flow import resume_prepared_internal_reads
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.devices.read_session import ReadEnd
from camctl.operations.attempts import FinishAttemptResult, FinishDisposition, RunOutcome, RunStatus
from camctl.operations.models import AttemptStatus, AttemptTicket, EffectState, SettlementBasis
from camctl.outputs.read_attempts import ReadContinuation, save_read_result, stopped_read_result
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbOutcome, DbOutcomeKind, DbReadError
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.runtime import OwnedConnection


@pytest.mark.asyncio
@pytest.mark.parametrize("canceled", [False, True])
async def test_default_resume_uses_original_stopped_read_after_cancel_fact_query_failed(monkeypatch, canceled):
    from camctl.capture import media_flow

    database = create_autospec(BusinessDatabase, instance=True)
    operations = create_autospec(OperationRepository, instance=True)
    operations.finish_attempt.return_value = DbOutcome(kind=DbOutcomeKind.COMPLETED,
        value=FinishAttemptResult(FinishDisposition.SAVED, AttemptStatus.UNKNOWN, RunStatus.CANCELED))
    monkeypatch.setattr(media_flow, "OperationRepository", lambda: operations)
    pending_results, continuing = {}, {}
    runtime = SimpleNamespace(database=database, pending_read_results=pending_results,
        continuing_read_tickets=continuing)
    ticket = AttemptTicket(1, "read", "9", "read/9", 28)
    step = InputStep(InputPhase.SEGMENT_FAILED, copy_id=9,
        read_end=ReadEnd(stopped=True, bytes_read=10, error="stopped"), stop_requested=True)
    evidence = EvidenceRegistry((EvidenceContract("read_returned", 1, "read", frozenset()),))
    original = stopped_read_result(runtime, ticket, step, evidence, 123)
    failure = DbReadError("cancel fact read unavailable")
    database.read.side_effect = failure
    with pytest.raises(DbReadError) as raised:
        await save_read_result(runtime, original)
    assert raised.value is failure
    assert pending_results[(28, 1)] is original
    assert not continuing
    operations.finish_attempt.assert_not_called()

    database.read.side_effect = [(int(canceled), 20, 2), (1, None)]
    owned = create_autospec(OwnedConnection, instance=True)
    async def write(callback, **kwargs):
        assert kwargs["key"] == original.key
        return callback(owned)
    database.write.side_effect = write
    await resume_prepared_internal_reads(database, pending_read_results=pending_results,
        pending_read_business={}, pending_read_ends={}, continuing_read_tickets=continuing,
        pending_segment_inspection_results={})
    assert not pending_results
    if canceled:
        operations.finish_attempt.assert_called_once()
        finish, key, actual_owned = operations.finish_attempt.call_args.args
        assert finish.ticket == ticket and actual_owned is owned
        assert finish.occurred_at == 123
        assert key == original.key
        assert finish.run_finish.status is RunOutcome.CANCELED
        assert finish.outcome.outcome.status is AttemptStatus.UNKNOWN
        assert finish.outcome.outcome.effect is EffectState.UNKNOWN
        assert finish.outcome.outcome.error.code == "read_stopped"
        assert finish.outcome.outcome.settlement.basis is SettlementBasis.OBSERVED
        assert finish.outcome.outcome.settlement.evidence.type == "read_returned"
        database.write.assert_awaited_once()
        assert not continuing
    else:
        assert continuing == {9: ReadContinuation(ticket, False, step, 123, original.key, evidence)}
        database.write.assert_not_awaited()
        operations.finish_attempt.assert_not_called()

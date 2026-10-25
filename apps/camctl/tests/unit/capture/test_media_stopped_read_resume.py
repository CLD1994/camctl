"""默认本地恢复继续持有的实际停止观察，不重新打开设备。"""

from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.capture.input_copy import InputPhase, InputStep
from camctl.capture.media_flow import resume_prepared_internal_reads
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.devices.read_session import ReadEnd
from camctl.operations.models import AttemptTicket
from camctl.outputs.read_attempts import save_read_result, stopped_read_result
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
    operations.finish_attempt.return_value = DbOutcome(kind=DbOutcomeKind.COMPLETED)
    monkeypatch.setattr(media_flow, "OperationRepository", lambda: operations)
    pending_results, continuing = {}, {}
    runtime = SimpleNamespace(database=database, pending_read_results=pending_results,
        continuing_read_tickets=continuing)
    ticket = AttemptTicket(1, "read", "9", "read", 28)
    step = InputStep(InputPhase.SEGMENT_FAILED, copy_id=9,
        read_end=ReadEnd(stopped=True, bytes_read=10, error="stopped"), stop_requested=True)
    evidence = EvidenceRegistry((EvidenceContract("read_returned", 1, "read", frozenset()),))
    original = stopped_read_result(runtime, ticket, step, evidence, 123)
    database.read.side_effect = DbReadError("cancel fact read unavailable")
    with pytest.raises(DbReadError):
        await save_read_result(runtime, original)
    assert pending_results[(28, 1)] is original

    database.read.side_effect = [(int(canceled), 20), (1, None)]
    owned = create_autospec(OwnedConnection, instance=True)
    async def write(callback, **kwargs):
        return callback(owned)
    database.write.side_effect = write
    await resume_prepared_internal_reads(database, pending_read_results=pending_results,
        pending_read_business={}, pending_read_ends={}, continuing_read_tickets=continuing)
    assert not pending_results
    if canceled:
        finish, key, _ = operations.finish_attempt.call_args.args
        assert finish.occurred_at == 123
        assert key == original.key
        assert not continuing
    else:
        assert continuing == {9: ticket}
        operations.finish_attempt.assert_not_called()

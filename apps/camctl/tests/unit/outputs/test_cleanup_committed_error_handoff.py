"""清理保存的实际完成回执先交接，再传播端口原异常。"""
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.operations.attempts import (
    AttemptConfig, AttemptIntent, AttemptTarget, BeginAttemptResult, BeginDisposition,
    StaleRunFinishResult, OperationKind,
)
from camctl.operations.models import AttemptStatus, AttemptTicket
from camctl.outputs import cleanup_flow as flow
from camctl.outputs.cleanup_ownership import save_cleanup_request
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbOutcome, DbOutcomeKind, _attach_db_outcome, db_outcome_for
from .test_cleanup_query_save_responsibility import (
    _ITEM_ID, _TICKET, _actual_result, _runtime, _saved, original_config_reader,
)
from .test_cleanup_delete_save_responsibility import _pending

pytestmark = pytest.mark.asyncio


class WorkerControl(BaseException):
    """worker交付的原控制异常，不终止pytest事件循环。"""


@pytest.mark.parametrize("stage", ["request", "intent", "delete", "query", "member", "companion"])
@pytest.mark.parametrize("kind", [DbOutcomeKind.COMPLETED, DbOutcomeKind.UNKNOWN])
@pytest.mark.parametrize("error_field", ["observer_error", "control", "handoff_error"])
async def test_cleanup_actual_receipt_is_applied_before_original_error(monkeypatch, stage, kind, error_field):
    runtime = _runtime(_actual_result(False), _saved(AttemptStatus.SUCCEEDED))
    database = create_autospec(BusinessDatabase, instance=True)
    database.metadata = runtime.database.metadata
    database.read.side_effect = lambda callback, **kwargs: callback(runtime._test_owned)
    runtime.database = database
    error = WorkerControl("worker control") if error_field == "control" else RuntimeError(error_field)
    member_saved = flow.CleanupItemSaved(flow.CleanupItemDisposition.SAVED, _ITEM_ID)
    ticket = _TICKET
    pending = None
    if stage == "request":
        request = flow.FinishCleanupItem(_ITEM_ID, flow.CleanupOutcomeChoice.DELETED, 123)
        operation = lambda: save_cleanup_request(runtime, "finish_cleanup_item", request)
        value = member_saved
    elif stage == "intent":
        monkeypatch.setattr(flow, "verify_cleanup_transaction", create_autospec(flow.verify_cleanup_transaction, return_value=(object(),)))
        request = AttemptIntent("query", 11, OperationKind.CHECK_FILE_EXISTS,
            AttemptTarget(cleanup_item_id=_ITEM_ID), None, AttemptConfig(3), 123)
        operation = lambda: flow._begin_cleanup_intent(runtime, request, None)
        value = BeginAttemptResult(BeginDisposition.GRANTED, ticket)
    elif stage == "delete":
        ticket = AttemptTicket(1, "delete", "91", "delete/91", 7)
        pending = _pending(runtime, ticket)
        runtime.pending_deletes[_ITEM_ID] = pending
        operation = lambda: flow._save_delete_return(runtime, pending)
        value = _saved(AttemptStatus.FAILED).value
    elif stage == "query":
        pending = flow.PendingCleanupQuery(database.metadata.instance_id, _ITEM_ID,
            flow.CleanupQueryConsumer.BEFORE_DELETE, ticket, _actual_result(False), runtime.evidence,
            123, 300, "a" * 32)
        runtime.pending_queries[(7, 2)] = pending
        runtime.pending_intents[_ITEM_ID] = flow.PendingCleanupIntent(
            database.metadata.instance_id, _ITEM_ID,
            AttemptIntent("query", 11, OperationKind.CHECK_FILE_EXISTS,
                AttemptTarget(cleanup_item_id=_ITEM_ID), None, AttemptConfig(3), 123),
            "c" * 32, pending.consumer, ticket=ticket, dispatched=True)
        operation = lambda: flow._save_query_return(runtime, pending)
        value = _saved(AttemptStatus.SUCCEEDED).value
    elif stage == "member":
        pending = flow.PendingCleanupQuery(database.metadata.instance_id, _ITEM_ID,
            flow.CleanupQueryConsumer.BEFORE_DELETE, ticket, _actual_result(False), runtime.evidence,
            123, 300, "a" * 32, phase=flow.CleanupQueryPhase.SAVED,
            member=flow.PendingCleanupMember(flow.FinishCleanupItem(_ITEM_ID,
                flow.CleanupOutcomeChoice.ABSENCE_CONFIRMED, 123), "b" * 32))
        runtime.pending_queries[(7, 2)] = pending
        operation = lambda: flow._consume_saved_query(runtime, pending)
        value = member_saved
    else:
        from unittest.mock import AsyncMock
        from camctl.operations.attempts import RunOutcome
        monkeypatch.setattr(flow, "_companion_member_result", AsyncMock(return_value=(RunOutcome.SUCCEEDED, None)))
        operation = lambda: flow._settle_companion_runs(runtime, _ITEM_ID)
        value = StaleRunFinishResult((7,))
    if error_field == "handoff_error":
        value = None
    actual = DbOutcome(kind, value=value, **{error_field: error})
    _attach_db_outcome(error, actual)
    database.write.side_effect = error
    with pytest.raises(type(error)) as caught:
        await operation()
    assert caught.value is error
    key = database.write.call_args.kwargs["key"]
    assert db_outcome_for(error) is actual
    if kind is DbOutcomeKind.COMPLETED and error_field == "handoff_error":
        assert isinstance(error.__cause__, ConsistencyError)
    completed = kind is DbOutcomeKind.COMPLETED and value is not None
    if stage == "request":
        assert bool(runtime.pending_saves) is not completed
        if not completed:
            original = runtime.pending_saves[("finish_cleanup_item", _ITEM_ID)]
            assert original.request is request and original.key == key
    elif stage == "intent":
        original = runtime.pending_intents[_ITEM_ID]
        assert original.ticket is (ticket if completed else None)
        assert original.last_kind is (kind if completed else None)
        assert original.request is request and original.key == key and not original.dispatched
    elif stage == "delete":
        assert pending.saved is completed
        assert pending.key == key and runtime.pending_deletes[_ITEM_ID] is pending
    elif stage == "query":
        assert pending.phase is (flow.CleanupQueryPhase.SAVED if completed else flow.CleanupQueryPhase.PREPARED)
        assert pending.key == key and runtime.pending_queries[(7, 2)] is pending
        assert (_ITEM_ID in runtime.pending_intents) is not completed
    elif stage == "member":
        assert pending.member.saved is completed
        assert pending.member.key == key and runtime.pending_queries[(7, 2)] is pending
    else:
        assert bool(runtime.pending_companions) is not completed
        if completed:
            assert "delete/91" not in runtime.retry_gate.anchors and "exists/91" not in runtime.retry_gate.anchors
        else:
            assert runtime.pending_companions[_ITEM_ID].key == key
    runtime.driver.query_file.assert_not_awaited()

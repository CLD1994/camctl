"""DELETE 保存责任拒绝不属于原成员的票据，兼容错误保持完整。"""

from dataclasses import replace
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult
from camctl.operations.models import AttemptStatus, AttemptTicket
from camctl.outputs.cleanup_flow import PendingCleanupDelete, _save_delete_return
from camctl.persistence.repositories.outputs import OutputsRepository
from .test_cleanup_query_save_responsibility import _runtime, _saved, _set_database, original_config_reader


def _pending(runtime, ticket):
    return PendingCleanupDelete(runtime._test_owned.metadata.instance_id, 91, ticket,
        DeviceCallResult((DeviceObservation("file_absent", 1, {"cleanup_item_id": "91"}),),
            {"code": "local_io_failed", "stage": "unlink", "details": {"path": "file", "errno": 5}}),
        EvidenceRegistry((
            EvidenceContract("file_absent", 1, "delete", frozenset({"cleanup_item_id"}), identity_field="cleanup_item_id"),
            EvidenceContract("delete_returned", 1, "delete", frozenset()),)),
        1_750_000_000_123_456, 300, new_operation_key())


@pytest.mark.asyncio
async def test_delete_compatibility_result_preserves_original_error_and_observation():
    runtime = _runtime(DeviceCallResult((), None), _saved(AttemptStatus.FAILED))
    runtime.outputs = create_autospec(OutputsRepository, instance=True)
    runtime.outputs.finish_cleanup_attempt.return_value = _saved(AttemptStatus.FAILED)
    pending = _pending(runtime, AttemptTicket(1, "delete", "91", "delete/91", 7))
    assert (await _save_delete_return(runtime, pending)) is None
    outcome = runtime.outputs.finish_cleanup_attempt.call_args.args[0].outcome.outcome
    assert outcome.error.code == "local_io_failed"
    assert outcome.error.stage == "unlink"
    assert outcome.error.details == {"path": "file", "errno": 5}
    assert outcome.observations == pending.actual.observations


@pytest.mark.parametrize("ticket", [
    AttemptTicket(1, "delete", "92", "delete/92", 7),
    AttemptTicket(1, "query", "91", "exists/91", 7),
    AttemptTicket(1, "delete", "91", "delete/92", 7),
])
@pytest.mark.asyncio
async def test_delete_owner_refuses_foreign_ticket_before_saving(ticket):
    runtime = _runtime(DeviceCallResult((), None), _saved(AttemptStatus.FAILED))
    pending = _pending(runtime, ticket)
    with pytest.raises(ConsistencyError):
        (await _save_delete_return(runtime, pending))
    runtime.outputs.finish_cleanup_attempt.assert_not_called()


@pytest.mark.asyncio
async def test_malformed_member_receipt_keeps_original_save_responsibility():
    from camctl.outputs.cleanup_ownership import save_cleanup_request
    from camctl.outputs.cleanup_flow import FinishCleanupItem, CleanupOutcomeChoice
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    runtime = _runtime(DeviceCallResult((), None), _saved(AttemptStatus.FAILED))
    runtime.outputs.finish_cleanup_item.return_value = DbOutcome(DbOutcomeKind.COMPLETED, None)
    request = FinishCleanupItem(91, CleanupOutcomeChoice.DELETED, 1_750_000_000_123_456)
    with pytest.raises(ConsistencyError):
        (await save_cleanup_request(runtime, "finish_cleanup_item", request))
    original = runtime.outputs.finish_cleanup_item.call_args.args[:2]
    assert runtime.pending_saves[("finish_cleanup_item", 91)].request is request
    with pytest.raises(ConsistencyError):
        (await save_cleanup_request(runtime, "finish_cleanup_item", replace(request, occurred_at=request.occurred_at + 1)))
    assert runtime.outputs.finish_cleanup_item.call_args.args[:2] == original


def test_framework_dispatch_views_are_independent_and_leave_original_registry_unchanged():
    from camctl.outputs.cleanup_flow import _not_dispatched_evidence
    original = EvidenceRegistry((EvidenceContract("dispatch_prevented", 1, "delete", frozenset()),))
    delete = _not_dispatched_evidence(original, AttemptTicket(1, "delete", "91", "delete/91", 7))
    query = _not_dispatched_evidence(original, AttemptTicket(1, "query", "92", "exists/92", 8))
    assert original.contract("dispatch_prevented", 1).operation == "delete"
    assert delete.contract("dispatch_prevented", 1).operation == "delete"
    assert query.contract("dispatch_prevented", 1).operation == "query"
    assert original is not query and delete is not query


@pytest.mark.asyncio
async def test_driver_not_dispatched_return_cannot_replace_original_evidence_registration():
    from camctl.outputs.cleanup_flow import _not_dispatched, _run_query
    from camctl.operations.validation import OutcomeValidationError
    runtime = _runtime(_not_dispatched("query"), _saved(AttemptStatus.FAILED))
    original = EvidenceRegistry((EvidenceContract("dispatch_prevented", 1, "delete", frozenset()),))
    runtime.evidence = original
    ticket = AttemptTicket(1, "query", "91", "exists/91", 7)
    with pytest.raises(OutcomeValidationError):
        await _run_query(runtime, ticket, 91)
    assert runtime.pending_queries[(7, 1)].evidence is original
    runtime.operations.finish_attempt.assert_not_called()


@pytest.mark.asyncio
async def test_malformed_receipt_revokes_previous_reliable_disposition_basis():
    from camctl.outputs.cleanup_ownership import save_cleanup_request
    from camctl.outputs.cleanup_flow import FinishCleanupItem, CleanupOutcomeChoice
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    runtime = _runtime(DeviceCallResult((), None), _saved(AttemptStatus.FAILED))
    request = FinishCleanupItem(91, CleanupOutcomeChoice.DELETED, 1_750_000_000_123_456)
    runtime.outputs.finish_cleanup_item.return_value = DbOutcome(DbOutcomeKind.ROLLED_BACK)
    (await save_cleanup_request(runtime, "finish_cleanup_item", request))
    pending = runtime.pending_saves[("finish_cleanup_item", 91)]
    assert pending.last_kind is DbOutcomeKind.ROLLED_BACK
    runtime.outputs.finish_cleanup_item.return_value = DbOutcome(DbOutcomeKind.COMPLETED, None)
    with pytest.raises(ConsistencyError):
        (await save_cleanup_request(runtime, "finish_cleanup_item", request))
    assert pending.last_kind is None


@pytest.mark.asyncio
async def test_delete_owner_rejects_a_different_database_without_saving():
    runtime = _runtime(DeviceCallResult((), None), _saved(AttemptStatus.FAILED))
    pending = _pending(runtime, AttemptTicket(1, "delete", "91", "delete/91", 7))
    _set_database(runtime, replace(runtime._test_owned, metadata=replace(runtime._test_owned.metadata,
        instance_id="abcdef1234567890abcdef1234567890")))
    with pytest.raises(ConsistencyError):
        (await _save_delete_return(runtime, pending))
    runtime.outputs.finish_cleanup_attempt.assert_not_called()


@pytest.mark.asyncio
async def test_query_owner_rejects_changed_explicit_consumer_without_resaving():
    from camctl.outputs.cleanup_flow import _run_query, CleanupQueryConsumer
    from .test_cleanup_query_save_responsibility import _actual_result
    runtime = _runtime(_actual_result(True), _saved(AttemptStatus.SUCCEEDED))
    ticket = AttemptTicket(1, "query", "91", "exists/91", 7)
    await _run_query(runtime, ticket, 91, CleanupQueryConsumer.AFTER_DELETE)
    with pytest.raises(ConsistencyError):
        await _run_query(runtime, ticket, 91, CleanupQueryConsumer.CANCEL)
    assert runtime.operations.finish_attempt.call_count == 1
    runtime.driver.query_file.assert_awaited_once()


@pytest.mark.parametrize("broken", ["type", "version", "data", "status", "effect"])
def test_saved_not_dispatched_requires_its_fixed_original_contract(monkeypatch, broken):
    from camctl.outputs import cleanup_results
    from camctl.outputs.cleanup_flow import _not_dispatched
    from camctl.operations.models import EffectState
    ticket = AttemptTicket(1, "delete", "91", "delete/91", 7)
    actual = _not_dispatched("delete").outcome
    if broken in ("type", "version", "data"):
        changes = {broken: {"type": "other", "version": 2, "data": {"unexpected": True}}[broken]}
        actual = replace(actual, settlement=replace(actual.settlement,
            evidence=replace(actual.settlement.evidence, **changes)))
    elif broken == "status":
        actual = replace(actual, status=AttemptStatus.UNKNOWN)
    else:
        actual = replace(actual, effect=EffectState.UNKNOWN)
    saved = cleanup_results.SavedCleanupAttempt(ticket, DeviceCallResult.from_outcome(actual),
                                                new_operation_key(), 1_750_000_000_123_456)
    monkeypatch.setattr(cleanup_results, "_read_exact_cleanup_attempt", lambda *args: saved)
    with pytest.raises(ConsistencyError):
        cleanup_results.saved_cleanup_not_dispatched(None, ticket, 91)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["direct", "round"])
@pytest.mark.parametrize("member_status", [None, 1, 2, 3])
async def test_pending_companion_requires_terminal_member_before_any_progress(entry, member_status):
    from camctl.operations.attempts import StaleRunFinish, RunOutcome
    from camctl.outputs.cleanup_flow import (
        PendingCleanupCompanion, delete_source_file, advance_cleanup)
    runtime = _runtime(DeviceCallResult((), None), _saved(AttemptStatus.FAILED))
    pending = PendingCleanupCompanion(runtime._test_owned.metadata.instance_id,
        StaleRunFinish(("exists/91",), RunOutcome.CANCELED, 1_750_000_000_123_456),
        new_operation_key())
    runtime.pending_companions[91] = pending
    if entry == "round":
        from camctl.outputs.cleanup_ownership import PendingCleanupSave
        from camctl.outputs.cleanup_flow import FinishCleanupAction
        runtime.pending_saves[("finish_cleanup_action", 77)] = PendingCleanupSave(
            runtime._test_owned.metadata.instance_id, "finish_cleanup_action",
            FinishCleanupAction(77, 1_750_000_000_123_456), new_operation_key())
    original_read = runtime._test_owned.connection.execute.side_effect

    def member_read(statement, parameters=()):
        cursor = original_read(statement, parameters)
        if "SELECT status,error_code,error_details_json FROM cleanup_items" in statement:
            cursor.fetchone.return_value = None if member_status is None else (member_status, None, None)
        return cursor

    runtime._test_owned.connection.execute.side_effect = member_read
    with pytest.raises(ConsistencyError, match="待存伴随流程没有可靠成员终态"):
        if entry == "direct":
            await delete_source_file(runtime, 91)
        else:
            await advance_cleanup(runtime)
    assert runtime.pending_companions[91] is pending
    assert all(call.args[0].lstrip().upper().startswith("SELECT")
               for call in runtime._test_owned.connection.execute.call_args_list)
    assert runtime.outputs.mock_calls == []
    assert runtime.operations.mock_calls == []
    assert runtime.driver.mock_calls == []

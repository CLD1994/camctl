"""范围末页沿公共保存入口交给原仓储，可靠完成后释放内存责任。"""

import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.capture.handlers import (
    CaptureRuntime, ListingPhase, ListingRound, _finish_listing_result, _hold_recovered_result_page,
)
from camctl.capture.result_inputs import page_from_outcome
from camctl.capture.result_pages import ResultPageRef, SavedResultPage
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry, RESULT_PAGE_CONTRACT
from camctl.operations.attempts import FinishAttemptResult, FinishDisposition, RunOutcome
from camctl.operations.models import AttemptTicket, CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository, ResultCheckOutcome
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.scheduling import SchedulingRepository
from camctl.persistence.repositories.timelapse import TimelapseRepository
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from support.capture_database import capture_database


pytestmark = pytest.mark.asyncio
_KINDS = enum_for("operation_runs.kind")


def _world(mocker, kind, *, receipt_kind=DbOutcomeKind.COMPLETED, recovered=False):
    connection = create_autospec(sqlite3.Connection, instance=True)
    owned = OwnedConnection(connection, DatabaseMetadata(
        "camctl", "a" * 32, 6, "/staging", "/ready", "/processing"))
    capture = create_autospec(CaptureRepository, instance=True)
    capture.finish_result_check.return_value = DbOutcome(receipt_kind,
        ResultCheckOutcome(FinishAttemptResult(FinishDisposition.SAVED), None)
        if receipt_kind is DbOutcomeKind.COMPLETED else None)
    runtime = CaptureRuntime(capture_database(owned),
        create_autospec(SchedulingRepository, instance=True),
        create_autospec(OperationRepository, instance=True), capture,
        create_autospec(TimelapseRepository, instance=True), None, None,
        EvidenceRegistry((RESULT_PAGE_CONTRACT,
            EvidenceContract("returned", 1, "result", frozenset()))),
        lambda: 9000, lambda: 3000, None, None)
    responsibility = "results/20" if kind == _KINDS.CHECK_CAPTURE_RESULTS else "scope/20"
    ticket = AttemptTicket(1, "result", "20", responsibility, 89)
    actual = CallOutcome(effect=EffectState.CONFIRMED,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("returned", 1)),
        observations=(DeviceObservation("result_files_listed", 2,
            {"activity_id": "20", "entries": [], "cursor": None, "next_cursor": None,
             "set_finalized": True, "completion_evidence": None}),))
    ref = ResultPageRef(ticket, 1, 2780, None)
    saved = SavedResultPage(ref, page_from_outcome(ticket, actual), 2000)
    capture.read_last_result_page.return_value = saved
    mocker.patch("camctl.capture.handlers.row_facts", autospec=True,
        side_effect=lambda connection, table, identity: (
            {"kind": int(kind), "action_id": 43} if table == "operation_runs" else
            {"activity_state": 3, "output_set_finalized_event_id": None, "action_id": 43}))
    if recovered:
        _hold_recovered_result_page(runtime, saved, runtime.evidence)
    else:
        runtime.hold_call_result(ticket, actual, occurred_at=2000, returned_ns=3000,
            result_listing=())
    listing = ListingRound(ListingPhase.LISTED, (), ticket, actual, 2000, 3000,
        scan_complete=True, set_finalized=True)
    return runtime, listing, ref, owned


@pytest.mark.parametrize("kind", [_KINDS.CHECK_CAPTURE_RESULTS, _KINDS.RESOLVE_OUTPUT_SCOPE])
@pytest.mark.parametrize("recovered", [False, True], ids=["live_result", "saved_page"])
async def test_finalized_result_reaches_repository_with_original_page_and_attempt(mocker, kind, recovered):
    runtime, listing, ref, owned = _world(mocker, kind, recovered=recovered)

    await _finish_listing_result(runtime, listing, end_run=RunOutcome.SUCCEEDED,
        finalize_output_set=True)

    call = runtime.capture.finish_result_check.call_args
    finish, result_set, _, actual_owned = call.args
    assert finish.ticket == listing.ticket and finish.outcome.outcome is listing.outcome
    assert finish.occurred_at == 2000
    assert finish.run_finish.status is RunOutcome.SUCCEEDED
    assert result_set is None and actual_owned is owned
    assert call.kwargs == {"completion_page": None, "finalized_page": ref}
    assert not runtime.pending_start_results
    runtime.operations.finish_attempt.assert_not_called()


async def test_unrelated_operation_cannot_save_a_result_range(mocker):
    runtime, listing, _, _ = _world(mocker, _KINDS.START)

    with pytest.raises(ConsistencyError):
        await _finish_listing_result(runtime, listing, end_run=RunOutcome.SUCCEEDED,
            finalize_output_set=True)

    runtime.capture.finish_result_check.assert_not_called()
    assert runtime.pending_start_results


@pytest.mark.parametrize("receipt_kind", [DbOutcomeKind.UNKNOWN, DbOutcomeKind.ROLLED_BACK])
async def test_unreliable_scope_save_keeps_original_result_page_and_key(mocker, receipt_kind):
    runtime, listing, ref, _ = _world(mocker, _KINDS.RESOLVE_OUTPUT_SCOPE,
        receipt_kind=receipt_kind)

    with pytest.raises(ConsistencyError):
        await _finish_listing_result(runtime, listing, end_run=RunOutcome.SUCCEEDED,
            finalize_output_set=True)

    pending = runtime.pending_start_results[(listing.ticket.run_id, listing.ticket.attempt_id)]
    assert pending.finish.outcome.outcome is listing.outcome and pending.finalized_page == ref
    calls = runtime.capture.finish_result_check.call_args_list
    assert calls and all(call.args[2] == pending.key for call in calls)

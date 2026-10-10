"""轮次拥有者可靠保存原页后才沿同一票据读下一页。"""
import asyncio
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest

from camctl.capture import result_scans
from camctl.capture.result_inputs import page_from_outcome, RESULT_PAGE_CONTRACT
from camctl.capture.result_pages import ResultPageRef
from camctl.bootstrap.capture_assembly import DriverResultListing
from camctl.contracts.values import ConsistencyError
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.operations.models import AttemptStatus, EffectState, ErrorValue, EvidenceValue, Settlement, SettlementBasis
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from .test_result_pages import _TICKET, _CURSOR, _outcome

_REGISTRY = EvidenceRegistry((RESULT_PAGE_CONTRACT, EvidenceContract("results_returned", 1, "result", frozenset())))


def _page(*, cursor=None, next_cursor=None, finalized=False, error=None):
    actual = replace(_outcome(cursor=cursor, next_cursor=next_cursor, finalized=finalized),
        effect=EffectState.CONFIRMED, error=error,
        status=AttemptStatus.SUCCEEDED if error is None else AttemptStatus.FAILED,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("results_returned", 1, {})))
    return page_from_outcome(_TICKET, actual, cursor=cursor)


def _runtime():
    return SimpleNamespace(owned=object(), capture=create_autospec(CaptureRepository, instance=True),
        results=create_autospec(DriverResultListing, instance=True), evidence=_REGISTRY,
        wall_us=Mock(side_effect=[1_750_000_000_000_000, 1_750_000_000_000_100]),
        monotonic_ns=Mock(side_effect=[1_000_000, 2_000_000]))


@pytest.mark.asyncio
async def test_two_pages_use_original_attempt_and_reliable_save_before_next_call():
    runtime = _runtime()
    first, last = _page(next_cursor=_CURSOR), _page(cursor=_CURSOR, finalized=True)
    runtime.results.list_page.side_effect = [first, last]
    sequence = []
    def save(request, key, owned):
        sequence.append(f"save/{request.page_no}")
        return DbOutcome(DbOutcomeKind.COMPLETED, ResultPageRef(_TICKET, request.page_no, request.page_no + 9,
            _CURSOR if request.page_no == 1 else None))
    runtime.capture.save_result_page.side_effect = save
    async def read(ticket, **kwargs):
        sequence.append("read/1" if kwargs["cursor"] is None else "read/2")
        return first if kwargs["cursor"] is None else last
    runtime.results.list_page.side_effect = read
    pending = result_scans.PendingResultScan(_TICKET, {"directories": ["/DCIM"]}, Decimal("3"))
    assert await result_scans.advance_result_scan(pending, runtime=runtime)
    assert sequence == ["read/1", "save/1", "read/2", "save/2"]
    assert pending.last_ref == ResultPageRef(_TICKET, 2, 11, None)
    assert pending.last_outcome.outcome == last.outcome and pending.finished
    assert pending.occurred_at == 1_750_000_000_000_100 and pending.returned_ns == 2_000_000
    assert [call.args[0] for call in runtime.results.list_page.await_args_list] == [_TICKET, _TICKET]


@pytest.mark.asyncio
async def test_unknown_save_keeps_original_input_time_and_key_without_second_call():
    runtime = _runtime()
    first, last = _page(next_cursor=_CURSOR), _page(cursor=_CURSOR, finalized=True)
    runtime.results.list_page.side_effect = [first, last]
    runtime.capture.save_result_page.side_effect = [DbOutcome(DbOutcomeKind.UNKNOWN),
        DbOutcome(DbOutcomeKind.COMPLETED, ResultPageRef(_TICKET, 1, 10, _CURSOR)),
        DbOutcome(DbOutcomeKind.COMPLETED, ResultPageRef(_TICKET, 2, 11, None))]
    pending = result_scans.PendingResultScan(_TICKET, {"directories": ["/DCIM"]}, Decimal("3"))
    with pytest.raises(ConsistencyError):
        await result_scans.advance_result_scan(pending, runtime=runtime)
    runtime.results.list_page.assert_awaited_once()
    original = pending.saves.pending
    assert original.request.occurred_at == 1_750_000_000_000_000 and pending.last_ref is None
    assert await result_scans.advance_result_scan(pending, runtime=runtime)
    calls = runtime.capture.save_result_page.call_args_list
    assert calls[0].args[:2] == calls[1].args[:2] == (original.request, original.key)
    assert runtime.results.list_page.await_count == 2 and pending.saves.pending is None


@pytest.mark.asyncio
async def test_actual_error_ends_incomplete_scan_and_preserves_original_error():
    runtime = _runtime()
    error = ErrorValue("read_failed", "device", {"received_bytes": 17})
    actual = _page(next_cursor=_CURSOR, error=error)
    runtime.results.list_page.return_value = actual
    runtime.capture.save_result_page.return_value = DbOutcome(DbOutcomeKind.COMPLETED, ResultPageRef(_TICKET, 1, 10, _CURSOR))
    pending = result_scans.PendingResultScan(_TICKET, {"directories": ["/DCIM"]}, Decimal("3"))
    assert await result_scans.advance_result_scan(pending, runtime=runtime)
    assert pending.last_outcome.outcome.error == error
    assert pending.finished and not actual.scan_complete
    assert await result_scans.advance_result_scan(pending, runtime=runtime)
    runtime.results.list_page.assert_awaited_once()


@pytest.mark.asyncio
async def test_interrupted_scan_keeps_responsibility_and_cannot_silently_continue():
    runtime = _runtime()
    runtime.results.list_page.side_effect = asyncio.CancelledError
    pending = result_scans.PendingResultScan(_TICKET, {"directories": ["/DCIM"]}, Decimal("3"))
    with pytest.raises(asyncio.CancelledError):
        await result_scans.advance_result_scan(pending, runtime=runtime)
    assert pending.interrupted and not pending.finished and not pending.in_call
    with pytest.raises(ConsistencyError):
        await result_scans.advance_result_scan(pending, runtime=runtime)
    runtime.results.list_page.assert_awaited_once()
    runtime.capture.save_result_page.assert_not_called()

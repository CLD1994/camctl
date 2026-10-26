"""轮次拥有者可靠保存原页后才沿同一票据读下一页。"""
import asyncio
import json
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, create_autospec

import pytest
from support.capture_database import capture_database

from camctl.capture import result_scans
from camctl.capture.result_inputs import page_from_outcome, RESULT_PAGE_CONTRACT
from camctl.capture.result_pages import ResultPageRef
from camctl.bootstrap.capture_assembly import DriverResultListing
from camctl.contracts.values import ConsistencyError
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.devices.drivers.adb_cameras.directory_scan import AdbDirectoryScan
from camctl.operations.models import AttemptStatus, EffectState, ErrorValue, EvidenceValue, Settlement, SettlementBasis
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.result_sources import ResultSource, ResultSourceKind
from camctl.logging_runtime.events import event_logging
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.service import LogChannel
from .test_result_pages import _TICKET, _CURSOR, _outcome

_REGISTRY = EvidenceRegistry((RESULT_PAGE_CONTRACT,
    EvidenceContract("results_returned", 1, "result", frozenset({
        "file_completion", "file_completion_source_page_event_id"}))))


def _completion_context():
    return {"activity_id": _TICKET.target_id, "stop_result_event_id": 7,
        "stop_returned_at_us": 1_750_000_000_000_000,
        "stop_response": {"status": 2, "effect_state": 2, "error": None,
            "result": {"format_version": 1, "settlement": {"basis": 1,
                "evidence": {"type": "stop_returned", "version": 1, "data": {}}},
                "observations": [{"type": "stop_confirmed", "version": 1,
                    "data": {"activity_id": _TICKET.target_id}}]}},
        "file_completion_wait_ms": 5000, "prior_completion": None}


def _completion(**changes):
    return {"method": "stop_return_and_wait", "version": 1,
        "activity_id": _TICKET.target_id, "stop_result_event_id": 7,
        "required_wait_ms": 5000, "observed_wait_ns": 5_000_000_000,
        "completed": True, **changes}


def _page(*, cursor=None, next_cursor=None, finalized=False, error=None, completion=None,
          source_page_event_id=None):
    actual = replace(_outcome(cursor=cursor, next_cursor=next_cursor, finalized=finalized),
        effect=EffectState.CONFIRMED, error=error,
        status=AttemptStatus.SUCCEEDED if error is None else AttemptStatus.FAILED,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("results_returned", 1,
            {} if completion is None else {"file_completion": completion,
                "file_completion_source_page_event_id": source_page_event_id})))
    return page_from_outcome(_TICKET, actual, cursor=cursor)


def _runtime():
    capture = create_autospec(CaptureRepository, instance=True)
    capture.read_result_source.return_value = ResultSource(
        ResultSourceKind.STOP, 8, _completion_context(), requires_file_wait=True)
    return SimpleNamespace(database=capture_database(), capture=capture,
        results=create_autospec(DriverResultListing, instance=True), evidence=_REGISTRY,
        wall_us=Mock(side_effect=[1_750_000_000_000_000, 1_750_000_000_000_100]),
        monotonic_ns=Mock(side_effect=[1_000_000, 2_000_000]))


@pytest.mark.asyncio
async def test_scan_logs_one_round_across_pages_save_retry_and_terminal_revisit():
    runtime = _runtime()
    runtime.results.list_page.side_effect = [_page(next_cursor=_CURSOR),
        _page(cursor=_CURSOR, finalized=True)]
    runtime.capture.save_result_page.side_effect = [DbOutcome(DbOutcomeKind.UNKNOWN),
        DbOutcome(DbOutcomeKind.COMPLETED, ResultPageRef(_TICKET, 1, 10, _CURSOR)),
        DbOutcome(DbOutcomeKind.COMPLETED, ResultPageRef(_TICKET, 2, 11, None))]
    pending = result_scans.PendingResultScan(_TICKET, None, Decimal("3"),
        action_id=23, device_id="action6")
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    with event_logging(channel):
        with pytest.raises(ConsistencyError):
            await result_scans.advance_result_scan(pending, runtime=runtime)
        assert await result_scans.advance_result_scan(pending, runtime=runtime)
        assert await result_scans.advance_result_scan(pending, runtime=runtime)
    records = [call.args[0] for call in channel.slog.call_args_list]
    info = [json.loads(record.message) for record in records if record.level is LogLevel.INFO]
    assert [payload["event"] for payload in info] == ["started", "returned"]
    assert all(payload["stage"] == "capture.result_scan" for payload in info)
    assert all((payload["action_id"], payload["device_id"], payload["activity_id"],
        payload["run_id"], payload["attempt_id"]) == (23, "action6", _TICKET.target_id,
        _TICKET.run_id, _TICKET.attempt_id) for payload in info)
    assert info[-1]["page_count"] == 2 and info[-1]["scan_complete"] is True


@pytest.mark.asyncio
async def test_scan_wait_has_no_info_and_actual_cancellation_keeps_original_error():
    runtime = _runtime()
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    pending = result_scans.PendingResultScan(_TICKET, None, Decimal("3"))
    failure = asyncio.CancelledError()
    runtime.results.list_page.side_effect = failure
    with event_logging(channel):
        assert not await result_scans.advance_result_scan(pending, runtime=runtime,
            can_read=AsyncMock(return_value=False))
        channel.slog.assert_not_called()
        with pytest.raises(asyncio.CancelledError) as caught:
            await result_scans.advance_result_scan(pending, runtime=runtime)
        assert caught.value is failure
    payloads = [json.loads(call.args[0].message) for call in channel.slog.call_args_list]
    assert [payload["event"] for payload in payloads] == ["started", "cancelled"]


@pytest.mark.asyncio
async def test_scan_cancellation_between_pages_is_logged_without_changing_saved_page():
    runtime = _runtime()
    runtime.results.list_page.return_value = _page(next_cursor=_CURSOR)
    reliable = ResultPageRef(_TICKET, 1, 10, _CURSOR)
    runtime.capture.save_result_page.return_value = DbOutcome(DbOutcomeKind.COMPLETED, reliable)
    pending = result_scans.PendingResultScan(_TICKET, None, Decimal("3"))
    failure = asyncio.CancelledError("next page qualification cancelled")
    allowed = AsyncMock(side_effect=[True, failure])
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    with event_logging(channel):
        with pytest.raises(asyncio.CancelledError) as caught:
            await result_scans.advance_result_scan(pending, runtime=runtime, can_read=allowed)
    assert caught.value is failure
    assert pending.last_ref == reliable and not pending.interrupted and not pending.finished
    runtime.results.list_page.assert_awaited_once()
    payloads = [json.loads(call.args[0].message) for call in channel.slog.call_args_list]
    info = [payload for payload in payloads if payload["level"] == "INFO"]
    assert [payload["event"] for payload in info] == ["started", "cancelled"]


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
    assert all(call.kwargs["reading_context"] is pending.reading_context
        for call in runtime.results.list_page.await_args_list)
    assert pending.reading_context.closed


@pytest.mark.asyncio
async def test_unknown_save_keeps_original_input_time_and_key_without_second_call():
    runtime = _runtime()
    first, last = _page(next_cursor=_CURSOR), _page(cursor=_CURSOR, finalized=True)
    runtime.results.list_page.side_effect = [first, last]
    runtime.capture.save_result_page.side_effect = [DbOutcome(DbOutcomeKind.UNKNOWN),
        DbOutcome(DbOutcomeKind.COMPLETED, ResultPageRef(_TICKET, 1, 10, _CURSOR)),
        DbOutcome(DbOutcomeKind.COMPLETED, ResultPageRef(_TICKET, 2, 11, None))]
    pending = result_scans.PendingResultScan(_TICKET, {"directories": ["/DCIM"]}, Decimal("3"))
    scan = create_autospec(AdbDirectoryScan, instance=True, spec_set=True)
    pending.reading_context.directory_scan = scan
    with pytest.raises(ConsistencyError):
        await result_scans.advance_result_scan(pending, runtime=runtime)
    scan.close.assert_not_called()
    assert not pending.reading_context.closed
    runtime.results.list_page.assert_awaited_once()
    original = pending.saves.pending
    assert original.request.occurred_at == 1_750_000_000_000_000 and pending.last_ref is None
    assert await result_scans.advance_result_scan(pending, runtime=runtime)
    calls = runtime.capture.save_result_page.call_args_list
    assert calls[0].args[:2] == calls[1].args[:2] == (original.request, original.key)
    assert runtime.results.list_page.await_count == 2 and pending.saves.pending is None
    scan.close.assert_called_once()


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
    scan = create_autospec(AdbDirectoryScan, instance=True, spec_set=True)
    pending.reading_context.directory_scan = scan
    with pytest.raises(asyncio.CancelledError):
        await result_scans.advance_result_scan(pending, runtime=runtime)
    assert pending.interrupted and not pending.finished and not pending.in_call
    with pytest.raises(ConsistencyError):
        await result_scans.advance_result_scan(pending, runtime=runtime)
    runtime.results.list_page.assert_awaited_once()
    runtime.capture.save_result_page.assert_not_called()
    scan.close.assert_called_once()


@pytest.mark.asyncio
async def test_pause_preserves_directory_cache_until_original_scan_finishes():
    runtime = _runtime()
    runtime.results.list_page.return_value = _page(finalized=True)
    runtime.capture.save_result_page.return_value = DbOutcome(
        DbOutcomeKind.COMPLETED, ResultPageRef(_TICKET, 1, 10, None))
    pending = result_scans.PendingResultScan(_TICKET, {"directories": ["/DCIM"]}, Decimal("3"))
    scan = create_autospec(AdbDirectoryScan, instance=True, spec_set=True)
    pending.reading_context.directory_scan = scan

    assert not await result_scans.advance_result_scan(pending, runtime=runtime, can_read=AsyncMock(return_value=False))
    runtime.results.list_page.assert_not_awaited()
    scan.close.assert_not_called()
    assert not pending.reading_context.closed

    assert await result_scans.advance_result_scan(pending, runtime=runtime, can_read=AsyncMock(return_value=True))
    scan.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery_failure", [False, True])
async def test_cache_close_failure_keeps_saved_actual_page_and_retries_only_close(delivery_failure):
    runtime = _runtime()
    actual = _page(finalized=True)
    runtime.results.list_page.return_value = actual
    reliable = ResultPageRef(_TICKET, 1, 10, None)
    runtime.capture.save_result_page.return_value = DbOutcome(DbOutcomeKind.COMPLETED, reliable,
        control=RuntimeError("saved page notification failed") if delivery_failure else None)
    pending = result_scans.PendingResultScan(_TICKET, {"directories": ["/DCIM"]}, Decimal("3"))
    scan = create_autospec(AdbDirectoryScan, instance=True, spec_set=True)
    close_error = OSError("cache close failed")
    scan.close.side_effect = [close_error, None]
    pending.reading_context.directory_scan = scan
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    def info():
        payloads = [json.loads(call.args[0].message) for call in channel.slog.call_args_list]
        return [payload for payload in payloads if payload["level"] == "INFO"]
    with event_logging(channel):
        with pytest.raises(OSError) as caught:
            await result_scans.advance_result_scan(pending, runtime=runtime)
        assert caught.value is close_error
        assert pending.finished and pending.last_ref == reliable
        assert pending.last_outcome.outcome == actual.outcome and pending.saves.pending is None
        started, raised = info()
        assert (started["stage"], started["event"]) == ("capture.result_scan", "started")
        assert (raised["event"], raised["phase"], raised["exception_type"],
            raised["page_saved"], raised["cache_closed"]) == (
            "raised", "cache_close", "OSError", True, False)
        assert await result_scans.advance_result_scan(pending, runtime=runtime)
        retry_start, retry_end = info()[2:]
        assert (retry_start["stage"], retry_start["event"], retry_end["stage"], retry_end["event"]) == (
            "capture.result_scan_close", "started", "capture.result_scan_close", "returned")
        assert await result_scans.advance_result_scan(pending, runtime=runtime)
        assert len(info()) == 4
    runtime.results.list_page.assert_awaited_once()
    runtime.capture.save_result_page.assert_called_once()
    assert scan.close.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_saved_final_page_delivery_error_is_logged_after_cache_close_without_false_return(error_type):
    runtime = _runtime()
    actual = _page(finalized=True)
    runtime.results.list_page.return_value = actual
    reliable = ResultPageRef(_TICKET, 1, 10, None)
    failure = error_type("saved page notification failed")
    runtime.capture.save_result_page.return_value = DbOutcome(DbOutcomeKind.COMPLETED, reliable, control=failure)
    pending = result_scans.PendingResultScan(_TICKET, None, Decimal("3"))
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    with event_logging(channel):
        with pytest.raises(error_type) as caught:
            await result_scans.advance_result_scan(pending, runtime=runtime)
        assert caught.value is failure
        assert pending.finished and pending.last_ref == reliable and pending.reading_context.closed
        assert pending.last_outcome.outcome == actual.outcome and pending.saves.pending is None
        assert await result_scans.advance_result_scan(pending, runtime=runtime)
    runtime.results.list_page.assert_awaited_once()
    runtime.capture.save_result_page.assert_called_once()
    payloads = [json.loads(call.args[0].message) for call in channel.slog.call_args_list]
    info = [payload for payload in payloads if payload["level"] == "INFO"]
    assert [payload["event"] for payload in info] == ["started",
        "cancelled" if error_type is asyncio.CancelledError else "raised"]
    assert (info[-1]["phase"], info[-1]["exception_type"], info[-1]["page_saved"],
        info[-1]["cache_closed"]) == ("page_delivery", error_type.__name__, True, True)


@pytest.mark.asyncio
async def test_completion_context_is_frozen_and_next_page_reuses_reliable_wait_fact():
    runtime = _runtime()
    context = _completion_context()
    completion = _completion()
    runtime.results.list_page.side_effect = [
        _page(next_cursor=_CURSOR, completion=completion),
        _page(cursor=_CURSOR, finalized=True, completion=completion)]
    runtime.capture.save_result_page.side_effect = [
        DbOutcome(DbOutcomeKind.COMPLETED, ResultPageRef(_TICKET, 1, 10, _CURSOR)),
        DbOutcome(DbOutcomeKind.COMPLETED, ResultPageRef(_TICKET, 2, 11, None))]
    pending = result_scans.PendingResultScan(_TICKET, {"directories": ["/DCIM"]},
        Decimal("10"), completion_context=context)
    context["file_completion_wait_ms"] = 9000
    context["stop_response"]["result"]["observations"].clear()

    assert await result_scans.advance_result_scan(pending, runtime=runtime)

    first, second = runtime.results.list_page.await_args_list
    assert first.kwargs["timeout_s"] == Decimal("15")
    assert second.kwargs["timeout_s"] == Decimal("10")
    assert first.kwargs["completion_context"] == _completion_context()
    assert second.kwargs["completion_context"] == {
        **_completion_context(), "prior_completion": {
            "result_page_event_id": 10, "evidence": completion}}
    assert pending.completion_context["prior_completion"] == {
        "result_page_event_id": 11, "evidence": completion}


@pytest.mark.asyncio
async def test_copy_page_keeps_actual_wait_source_for_the_following_page():
    runtime = _runtime()
    runtime.wall_us.side_effect = [1_750_000_000_000_000, 1_750_000_000_000_100,
        1_750_000_000_000_200]
    runtime.monotonic_ns.side_effect = [1_000_000, 2_000_000, 3_000_000]
    completion = _completion()
    second_cursor = replace(_CURSOR, after_path="/DCIM/b.mp4")
    original = _page(next_cursor=_CURSOR, completion=completion)
    original_ref = ResultPageRef(_TICKET, 1, 10, _CURSOR)
    from camctl.capture.result_pages import SavedResultPage
    # 来源读取沿仓储公开责任边界。
    runtime.capture.read_result_page_at.return_value = SavedResultPage(
        original_ref, original, 1_750_000_000_000_000)
    runtime.results.list_page.side_effect = [original,
        _page(cursor=_CURSOR, next_cursor=second_cursor, completion=completion, source_page_event_id=10),
        _page(cursor=second_cursor, finalized=True, completion=completion, source_page_event_id=10)]
    runtime.capture.save_result_page.side_effect = [
        DbOutcome(DbOutcomeKind.COMPLETED, original_ref),
        DbOutcome(DbOutcomeKind.COMPLETED, ResultPageRef(_TICKET, 2, 11, second_cursor)),
        DbOutcome(DbOutcomeKind.COMPLETED, ResultPageRef(_TICKET, 3, 12, None))]
    pending = result_scans.PendingResultScan(_TICKET, None, Decimal("10"),
        completion_context=_completion_context())

    assert await result_scans.advance_result_scan(pending, runtime=runtime)

    assert runtime.results.list_page.await_args_list[2].kwargs["completion_context"]["prior_completion"] == {
        "result_page_event_id": 10, "evidence": completion}
    assert pending.completion_context["prior_completion"] == {
        "result_page_event_id": 10, "evidence": completion}


@pytest.mark.asyncio
async def test_unknown_page_save_never_reuses_wait_until_original_page_is_reliable():
    runtime = _runtime()
    completion = _completion()
    runtime.results.list_page.side_effect = [
        _page(next_cursor=_CURSOR, completion=completion),
        _page(cursor=_CURSOR, finalized=True, completion=completion)]
    runtime.capture.save_result_page.side_effect = [DbOutcome(DbOutcomeKind.UNKNOWN),
        DbOutcome(DbOutcomeKind.COMPLETED, ResultPageRef(_TICKET, 1, 10, _CURSOR)),
        DbOutcome(DbOutcomeKind.COMPLETED, ResultPageRef(_TICKET, 2, 11, None))]
    pending = result_scans.PendingResultScan(_TICKET, None, Decimal("10"),
        completion_context=_completion_context())

    with pytest.raises(ConsistencyError):
        await result_scans.advance_result_scan(pending, runtime=runtime)
    original = pending.saves.pending
    assert pending.completion_context["prior_completion"] is None
    runtime.results.list_page.assert_awaited_once()

    assert await result_scans.advance_result_scan(pending, runtime=runtime)
    assert runtime.capture.save_result_page.call_args_list[1].args[:2] == (
        original.request, original.key)
    assert runtime.results.list_page.await_args_list[1].kwargs["completion_context"]["prior_completion"] == {
        "result_page_event_id": 10, "evidence": completion}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"activity_id": "foreign"}, {"stop_result_event_id": 8},
    {"required_wait_ms": 4000}, {"completed": False},
])
async def test_next_page_does_not_reuse_completion_for_another_stop_or_unfinished_wait(change):
    runtime = _runtime()
    runtime.results.list_page.side_effect = [
        _page(next_cursor=_CURSOR, completion=_completion(**change)),
        _page(cursor=_CURSOR, finalized=True)]
    runtime.capture.save_result_page.side_effect = [
        DbOutcome(DbOutcomeKind.COMPLETED, ResultPageRef(_TICKET, 1, 10, _CURSOR)),
        DbOutcome(DbOutcomeKind.COMPLETED, ResultPageRef(_TICKET, 2, 11, None))]
    pending = result_scans.PendingResultScan(_TICKET, None, Decimal("10"),
        completion_context=_completion_context())

    assert await result_scans.advance_result_scan(pending, runtime=runtime)

    assert runtime.results.list_page.await_args_list[1].kwargs["completion_context"]["prior_completion"] is None


@pytest.mark.asyncio
async def test_natural_completion_context_does_not_expect_or_create_stop_wait_source():
    runtime = _runtime()
    runtime.results.list_page.return_value = _page(finalized=True)
    runtime.capture.save_result_page.return_value = DbOutcome(
        DbOutcomeKind.COMPLETED, ResultPageRef(_TICKET, 1, 10, None))
    context = {"kind": "timelapse_natural", "activity_id": _TICKET.target_id,
        "wait_completed_event_id": 7}
    pending = result_scans.PendingResultScan(_TICKET, None, Decimal("10"),
        completion_context=context)
    assert await result_scans.advance_result_scan(pending, runtime=runtime)
    assert runtime.results.list_page.await_args.kwargs["timeout_s"] == Decimal("10")
    assert pending.completion_context == context

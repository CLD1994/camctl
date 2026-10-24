"""可靠前页首次完整核验，连续读取只核验新保存的一页。"""

from dataclasses import replace
from decimal import Decimal
from unittest.mock import call, create_autospec

import pytest

from camctl.bootstrap.capture_assembly import DriverResultListing
from camctl.capture.result_inputs import RESULT_PAGE_CONTRACT, page_from_outcome
from camctl.capture.result_pages import ResultPageRef, SavedResultPage
from camctl.capture.result_reading import ResultReadingContext
from camctl.contracts.pages import Page
from camctl.contracts.values import ConsistencyError
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryCursor
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult, ResultDriver
from camctl.operations.models import (
    AttemptStatus,
    AttemptTicket,
    CallOutcome,
    EffectState,
    ErrorValue,
    EvidenceValue,
    Settlement,
    SettlementBasis,
)
from camctl.persistence.repositories.capture import CaptureRepository


_BINDING = DeviceBinding("cam-1", "dji-action6")
_TICKET = AttemptTicket(1, "result", "71", "results/71", 9)
_SCOPE = {"directories": ["/DCIM"]}
_EVIDENCE = EvidenceRegistry((
    RESULT_PAGE_CONTRACT,
    EvidenceContract("results_returned", 1, "result", frozenset()),
))


def _cursor(name):
    return DirectoryCursor(_BINDING, ("/DCIM",), 0, f"/DCIM/{name}.mp4")


def _entry(name, *, format_id="mp4"):
    identity = f"/DCIM/{name}"
    return {
        "identity": identity,
        "locator": {"path": identity},
        "complete": True,
        "size_bytes": 50,
        "kind": "video" if format_id == "mp4" else "photo",
        "format_id": format_id,
    }


def _outcome(*, cursor=None, next_cursor=None, entries=(), error=None, finalized=False):
    return CallOutcome(
        status=AttemptStatus.SUCCEEDED if error is None else AttemptStatus.FAILED,
        error=error,
        effect=EffectState.CONFIRMED,
        settlement=Settlement(
            SettlementBasis.OBSERVED, EvidenceValue("results_returned", 1, {})),
        observations=(DeviceObservation("result_files_listed", 2, {
            "activity_id": _TICKET.target_id,
            "entries": list(entries),
            "cursor": None if cursor is None else cursor.as_json(),
            "next_cursor": None if next_cursor is None else next_cursor.as_json(),
            "set_finalized": finalized,
            "completion_evidence": None,
        }),),
    )


def _saved(page_no, *, cursor=None, next_cursor=None, entries=(), file_ids=(),
           error=None, finalized=False):
    actual = _outcome(cursor=cursor, next_cursor=next_cursor, entries=entries,
                      error=error, finalized=finalized)
    page = page_from_outcome(_TICKET, actual, cursor=cursor, binding=_BINDING)
    return SavedResultPage(
        ResultPageRef(_TICKET, page_no, 100 + page_no, next_cursor),
        page,
        1_750_000_000_000_000 + page_no,
        file_ids,
    )


def _chain():
    return [
        _saved(1, next_cursor=_cursor("a")),
        _saved(2, cursor=_cursor("a"), next_cursor=_cursor("b")),
        _saved(3, cursor=_cursor("b"), next_cursor=_cursor("c")),
    ]


def _world(history):
    repository = create_autospec(CaptureRepository, instance=True, spec_set=True)
    driver = create_autospec(ResultDriver, instance=True, spec_set=True)
    owned = object()

    def read_pages(ticket, cursor, batch, connection):
        assert ticket == _TICKET and connection is owned
        if cursor is None:
            start = 0
        else:
            start = next(index for index, saved in enumerate(history)
                         if saved.ref.event_id == cursor) + 1
        items = tuple(history[start:start + batch])
        next_cursor = items[-1].ref.event_id if start + batch < len(history) else None
        return Page(items, next_cursor)

    repository.read_result_pages.side_effect = read_pages
    adapter = DriverResultListing(driver, _BINDING, _EVIDENCE,
                                  capture_repository=repository, owned=owned)
    return repository, driver, adapter, owned


def _reply(driver, *, cursor=None, next_cursor=None):
    actual = _outcome(cursor=cursor, next_cursor=next_cursor)
    driver.list_results.return_value = DeviceCallResult.from_outcome(actual)
    return actual


async def _list_page(adapter, context, cursor):
    return await adapter.list_page(
        _TICKET, cursor=cursor, timeout_s=Decimal("1.25"),
        output_scope=_SCOPE, reading_context=context)


@pytest.mark.asyncio
async def test_first_recovery_verifies_every_saved_page_before_dispatch():
    history = _chain()
    repository, driver, adapter, owned = _world(history)
    context = ResultReadingContext(_TICKET)
    actual = _reply(driver, cursor=_cursor("c"), next_cursor=_cursor("d"))

    page = await _list_page(adapter, context, _cursor("c"))

    assert page.outcome is actual
    assert context.verified_last_ref == history[-1].ref
    assert repository.read_result_pages.call_args_list == [
        call(_TICKET, None, 1, owned),
        call(_TICKET, 101, 1, owned),
        call(_TICKET, 102, 1, owned),
    ]
    driver.list_results.assert_awaited_once()


@pytest.mark.asyncio
async def test_same_owner_reads_only_each_of_three_new_reliable_pages():
    history = []
    repository, driver, adapter, owned = _world(history)
    context = ResultReadingContext(_TICKET)
    _reply(driver, next_cursor=_cursor("a"))
    await _list_page(adapter, context, None)

    for saved, following in zip(_chain(), ("b", "c", "d")):
        history.append(saved)
        _reply(driver, cursor=saved.ref.next_cursor, next_cursor=_cursor(following))
        await _list_page(adapter, context, saved.ref.next_cursor)

    assert context.verified_last_ref == history[-1].ref
    assert repository.read_result_pages.call_args_list == [
        call(_TICKET, None, 1, owned),
        call(_TICKET, None, 1, owned),
        call(_TICKET, 101, 1, owned),
        call(_TICKET, 102, 1, owned),
    ]
    assert driver.list_results.await_count == 4


@pytest.mark.asyncio
async def test_another_adapter_rebuilds_full_history_with_the_same_context():
    saved = _chain()
    history = saved[:2]
    repository, driver, adapter, owned = _world(history)
    context = ResultReadingContext(_TICKET)
    _reply(driver, cursor=_cursor("b"), next_cursor=_cursor("c"))
    await _list_page(adapter, context, _cursor("b"))
    original_owner = context.history_owner
    history.append(saved[2])
    repository.read_result_pages.reset_mock()
    replacement = DriverResultListing(
        driver, _BINDING, _EVIDENCE, capture_repository=repository, owned=owned)
    _reply(driver, cursor=_cursor("c"), next_cursor=_cursor("d"))

    await _list_page(replacement, context, _cursor("c"))

    assert context.history_owner is not original_owner
    assert context.verified_last_ref == saved[2].ref
    assert repository.read_result_pages.call_args_list == [
        call(_TICKET, None, 1, owned),
        call(_TICKET, 101, 1, owned),
        call(_TICKET, 102, 1, owned),
    ]
    assert driver.list_results.await_count == 2


@pytest.mark.asyncio
async def test_unsaved_first_page_cannot_dispatch_the_first_page_again():
    repository, driver, adapter, _ = _world([])
    context = ResultReadingContext(_TICKET)
    _reply(driver, next_cursor=_cursor("a"))
    await _list_page(adapter, context, None)

    with pytest.raises(ConsistencyError):
        await _list_page(adapter, context, None)

    assert context.verified_last_ref is None
    driver.list_results.assert_awaited_once()
    assert repository.read_result_pages.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_count", [0, 1])
async def test_unconfirmed_current_page_cannot_dispatch_a_continuation(saved_count):
    history = _chain()[:saved_count]
    _, driver, adapter, _ = _world(history)
    context = ResultReadingContext(_TICKET)
    current_cursor = None if not history else _cursor("a")
    following_cursor = _cursor("a" if not history else "b")
    _reply(driver, cursor=current_cursor, next_cursor=following_cursor)
    await _list_page(adapter, context, current_cursor)
    reliable_ref = context.verified_last_ref
    # 当前实际页的保存尚未确定，因此可靠历史没有增加。

    with pytest.raises(ConsistencyError):
        await _list_page(adapter, context, following_cursor)

    assert context.verified_last_ref == reliable_ref
    driver.list_results.assert_awaited_once()


@pytest.mark.asyncio
async def test_replacement_owner_cannot_reread_the_unconfirmed_current_page():
    history = _chain()[:1]
    repository, driver, adapter, owned = _world(history)
    context = ResultReadingContext(_TICKET)
    _reply(driver, cursor=_cursor("a"), next_cursor=_cursor("b"))
    await _list_page(adapter, context, _cursor("a"))
    replacement = DriverResultListing(driver, _BINDING, _EVIDENCE,
        capture_repository=repository, owned=owned)

    with pytest.raises(ConsistencyError):
        await _list_page(replacement, context, _cursor("a"))

    driver.list_results.assert_awaited_once()


def _invalid_tail(case, valid):
    if case == "ticket":
        return replace(valid, ref=replace(
            valid.ref, ticket=replace(_TICKET, attempt_id=2)))
    if case == "page_no":
        return replace(valid, ref=replace(valid.ref, page_no=3))
    if case == "event_id":
        return replace(valid, ref=replace(valid.ref, event_id=101))
    if case == "ref_next_cursor":
        return replace(valid, ref=replace(valid.ref, next_cursor=_cursor("c")))
    if case == "cursor":
        return _saved(2, cursor=_cursor("aa"), next_cursor=_cursor("b"))
    if case == "error":
        return _saved(2, cursor=_cursor("a"), next_cursor=_cursor("b"),
                      error=ErrorValue("read_failed", "device", {"received_bytes": 7}))
    assert case == "finalized"
    return _saved(2, cursor=_cursor("a"), finalized=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("incremental", [False, True], ids=["recovery", "same_owner"])
@pytest.mark.parametrize("case", [
    "ticket", "page_no", "event_id", "ref_next_cursor", "cursor", "error", "finalized",
])
async def test_invalid_reliable_tail_stops_dispatch_and_keeps_verified_prefix(case, incremental):
    first, second, _ = _chain()
    history = [first]
    _, driver, adapter, _ = _world(history)
    context = ResultReadingContext(_TICKET)
    if incremental:
        _reply(driver, cursor=_cursor("a"), next_cursor=_cursor("b"))
        await _list_page(adapter, context, _cursor("a"))
    reliable_ref = context.verified_last_ref
    history.append(_invalid_tail(case, second))

    with pytest.raises(ConsistencyError):
        await _list_page(adapter, context, _cursor("b"))

    assert context.verified_last_ref == reliable_ref
    assert driver.list_results.await_count == int(incremental)


@pytest.mark.asyncio
async def test_multiple_new_reliable_pages_stop_incremental_dispatch():
    saved = _chain()
    history = saved[:1]
    _, driver, adapter, _ = _world(history)
    context = ResultReadingContext(_TICKET)
    _reply(driver, cursor=_cursor("a"), next_cursor=_cursor("b"))
    await _list_page(adapter, context, _cursor("a"))
    history.extend(saved[1:])

    with pytest.raises(ConsistencyError):
        await _list_page(adapter, context, _cursor("c"))

    assert context.verified_last_ref == saved[0].ref
    driver.list_results.assert_awaited_once()


@pytest.mark.asyncio
async def test_prior_entries_repeat_only_the_reliable_file_id_members():
    history = [
        _saved(1, next_cursor=_cursor("b"), entries=(
            _entry("a.mp4"), _entry("b.mp4")),
            file_ids=(("/DCIM/b.mp4", 41),)),
        _saved(2, cursor=_cursor("b"), next_cursor=_cursor("d"), entries=(
            _entry("c.jpg", format_id="jpeg"), _entry("d.mp4")),
            file_ids=(("/DCIM/c.jpg", 42),)),
    ]
    _, driver, adapter, _ = _world(history)
    context = ResultReadingContext(_TICKET)
    observed = []
    actual = _outcome(cursor=_cursor("d"), next_cursor=_cursor("e"))

    async def read(request, batch):
        assert request.prior_result_entries is not None
        observed.append(tuple(request.prior_result_entries()))
        observed.append(tuple(request.prior_result_entries()))
        return DeviceCallResult.from_outcome(actual)

    driver.list_results.side_effect = read

    page = await _list_page(adapter, context, _cursor("d"))

    expected = (
        {"identity": "/DCIM/b.mp4", "format_id": "mp4"},
        {"identity": "/DCIM/c.jpg", "format_id": "jpeg"},
    )
    assert observed == [expected, expected]
    assert page.outcome is actual
    assert context.verified_last_ref == history[-1].ref

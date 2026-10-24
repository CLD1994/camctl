"""结果拥有者只复用同一尝试的目录清单，并在结束后关闭。"""

import asyncio
from decimal import Decimal
from unittest.mock import create_autospec

import pytest

from camctl.bootstrap.capture_assembly import DriverResultListing
from camctl.capture.result_reading import ResultReadingContext
from camctl.contracts.pages import Page
from camctl.contracts.values import ConsistencyError
from camctl.devices.directory import DirectoryRead
from camctl.devices.drivers.adb_cameras.directory_scan import AdbDirectoryScan
from camctl.devices.drivers.adb_cameras.filesystem import AdbFilesystem
from camctl.devices.ports import DeviceCallResult
from test_result_history_performance import (
    _BINDING, _TICKET, _SCOPE, _EVIDENCE, _cursor, _outcome, _saved, _world,
)


@pytest.mark.asyncio
async def test_same_scan_survives_pause_and_adapter_replacement_then_closes_once():
    history = []
    repository, driver, adapter, owned = _world(history)
    context = ResultReadingContext(_TICKET)
    filesystem = create_autospec(AdbFilesystem, instance=True, spec_set=True)
    scan = create_autospec(AdbDirectoryScan, instance=True, spec_set=True)
    filesystem.directory_scan.return_value = scan
    scan.read_directory.side_effect = [
        DirectoryRead(None, None, Page((), _cursor("a"))),
        DirectoryRead(None, None, Page((), None), call_performed=False)]

    async def read(request, batch):
        from camctl.devices.directory import DirectoryRequest
        cursor = None if request.params.get("cursor") is None else _cursor("a")
        listing = await request.result_directory_reader(filesystem,
            DirectoryRequest(_BINDING, ("/DCIM",), cursor, 32, request.timeout_s), stop=object())
        return DeviceCallResult.from_outcome(_outcome(cursor=cursor,
            next_cursor=listing.page.next_cursor))

    driver.list_results.side_effect = read
    await adapter.list_page(_TICKET, cursor=None, timeout_s=Decimal("2"),
        output_scope=_SCOPE, reading_context=context)
    scan.close.assert_not_called()
    assert context.directory_scan is scan
    history.append(_saved(1, next_cursor=_cursor("a")))
    replacement = DriverResultListing(driver, _BINDING, _EVIDENCE,
        capture_repository=repository, owned=owned)

    await replacement.list_page(_TICKET, cursor=_cursor("a"), timeout_s=Decimal("2"),
        output_scope=_SCOPE, reading_context=context)
    context.close()

    filesystem.directory_scan.assert_called_once()
    assert scan.read_directory.await_count == 2
    scan.close.assert_called_once()
    assert context.closed and context.directory_scan is None


@pytest.mark.asyncio
async def test_saved_pages_without_original_cache_cannot_reopen_directory():
    _, driver, adapter, _ = _world([_saved(1, next_cursor=_cursor("a"))])
    filesystem = create_autospec(AdbFilesystem, instance=True, spec_set=True)
    context = ResultReadingContext(_TICKET)

    async def read(request, batch):
        from camctl.devices.directory import DirectoryRequest
        await request.result_directory_reader(filesystem,
            DirectoryRequest(_BINDING, ("/DCIM",), _cursor("a"), 32, request.timeout_s), stop=object())

    driver.list_results.side_effect = read
    with pytest.raises(ConsistencyError):
        await adapter.list_page(_TICKET, cursor=_cursor("a"), timeout_s=Decimal("2"),
            output_scope=_SCOPE, reading_context=context)

    filesystem.directory_scan.assert_not_called()
    assert context.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ValueError("invalid response"), asyncio.CancelledError()])
async def test_actual_driver_error_closes_existing_directory_cache(error):
    _, driver, adapter, _ = _world([])
    context = ResultReadingContext(_TICKET)
    scan = create_autospec(AdbDirectoryScan, instance=True, spec_set=True)
    context.directory_scan = scan
    driver.list_results.side_effect = error

    with pytest.raises(type(error)):
        await adapter.list_page(_TICKET, cursor=None, timeout_s=Decimal("2"),
            output_scope=_SCOPE, reading_context=context)

    scan.close.assert_called_once()
    assert context.closed


@pytest.mark.asyncio
async def test_close_error_preserves_driver_failure_and_explicit_close_can_retry():
    _, driver, adapter, _ = _world([])
    context = ResultReadingContext(_TICKET)
    scan = create_autospec(AdbDirectoryScan, instance=True, spec_set=True)
    context.directory_scan = scan
    original = ValueError("invalid response")
    cleanup = OSError("cache close failed")
    scan.close.side_effect = [cleanup, None]
    driver.list_results.side_effect = original

    with pytest.raises(ValueError) as caught:
        await adapter.list_page(_TICKET, cursor=None, timeout_s=Decimal("2"),
            output_scope=_SCOPE, reading_context=context)

    assert caught.value is original and caught.value.__cause__ is cleanup
    assert context.directory_scan is scan and not context.closed
    adapter.close_scan(_TICKET)
    adapter.close_scan(_TICKET)
    assert context.closed and context.directory_scan is None
    assert scan.close.call_count == 2

"""默认共同 flow 核实原结果页，不重新查询设备。"""

from unittest.mock import create_autospec

import pytest

from camctl.bootstrap.capture_assembly import DriverResultListing
from camctl.capture.handlers import capture_handler
from camctl.contracts.values import ConsistencyError
from camctl.devices.ports import ResultDriver, DeviceCallResult
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository

from ..capture.test_result_page_history import _REGISTRY, _BINDING, _CURSOR, _actual, _entry
from ..support.bootstrap_capture_cases import bootstrap_case, SavedBoundary

pytestmark = pytest.mark.asyncio


async def test_common_resume_recovers_original_page_without_reading_next_page(tmp_path, bootstrap_case):
    case = bootstrap_case
    driver = create_autospec(ResultDriver, instance=True)

    async def read(request, batch):
        assert "cursor" not in request.params
        return DeviceCallResult.from_outcome(_actual(request.ticket,
            entries=[_entry("a")], cursor=None, next_cursor=_CURSOR, finalized=False))

    driver.list_results.side_effect = read
    runtime = await case.world(tmp_path, default=True,
        results=DriverResultListing(driver, _BINDING, _REGISTRY))
    await capture_handler(case.handler)(case.action_id, runtime)
    case.clock.monotonic += 60_000_000_000
    case.clock.wall += 60_000_000
    case.boundary = "conclude_activity"
    with pytest.raises(SavedBoundary):
        await capture_handler(case.handler)(case.action_id, await case.view())
    case.boundary = None

    def unknown(name, request, key, result):
        return (DbOutcome(DbOutcomeKind.UNKNOWN, error=ConsistencyError("原页回执未知"))
            if name == "save_result_page" else result)

    case.after_save = unknown
    with pytest.raises(ConsistencyError):
        await capture_handler(case.handler)(case.action_id, await case.view())
    original, key = [(request, key) for name, request, key in case.saves if name == "save_result_page"][0]
    assert driver.list_results.await_count == 1
    case.after_save = None
    await case.reconnect()
    await case.common_round()
    driver.list_results.assert_awaited_once()
    pages = CaptureRepository().read_result_pages(original.ticket, None, 1, case.owned).items
    assert len(pages) == 1 and pages[0].occurred_at == original.occurred_at
    assert pages[0].page.outcome == original.outcome.outcome and not pages[0].page.scan_complete
    assert case.owned.connection.execute("SELECT status FROM operation_attempts WHERE run_id=?",
        (original.ticket.run_id,)).fetchone() == (1,)

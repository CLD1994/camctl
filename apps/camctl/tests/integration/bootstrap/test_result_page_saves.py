"""默认共同恢复先核实原页，不取得当前设备或新的业务资格。"""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.bootstrap.lifecycle import _resume_capture_requests
from camctl.capture.handlers import _listing_round
from camctl.contracts.values import ConsistencyError
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from ..capture.result_consumer_fixtures import consumer_world
from ..capture.test_result_scan_runtime import _pages
from ..capture.test_result_file_recovery import FileWriteFault

pytestmark = pytest.mark.asyncio


async def test_common_resume_recovers_original_page_without_reading_next_page(tmp_path):
    owned, runtime, action_id, _ = await consumer_world(tmp_path, "record")
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    driver = _pages(runtime)
    runtime.owned = replace(owned, connection=FileWriteFault(owned.connection, "commit_after"))
    try:
        with pytest.raises(ConsistencyError):
            await _listing_round(runtime, action_id)
        pending, = runtime.pending_result_scans.values()
        original = pending.saves.pending
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        deps = SimpleNamespace(capture_baselines={}, capture_result_scans=runtime.pending_result_scans,
            capture_result_closes={}, capture_completions={}, capture_retry_gate=None)
        _resume_capture_requests(deps, owned)
        assert pending.saves.pending.response is not None
        driver.list_results.assert_awaited_once()
        saved = runtime.capture.read_result_pages(original.request.ticket, None, 1, owned).items[0]
        assert saved.occurred_at == original.request.occurred_at
        assert saved.page.outcome == original.request.outcome.outcome
        assert not pending.finished and pending.page_no == 1
    finally:
        owned.connection.close()

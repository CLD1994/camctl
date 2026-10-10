"""真实结果驱动、逐页仓储与运行时组合，原保存未知不读下一页。"""
from dataclasses import replace
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from camctl.bootstrap.capture_assembly import DriverResultListing
from camctl.capture.handlers import ListingPhase, ListingRound, _listing_round, _finish_listing_result, _saved_result_listing, _register_listing
from camctl.capture.result_scans import SavedResultEntries
from camctl.capture.handlers import capture_handler
from camctl.capture.handlers import _advance_recording_outcome
from camctl.capture.recording import RecordingPhase
from types import SimpleNamespace
import json
from camctl.operations.attempts import RunOutcome
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import CallOutcome, AttemptStatus, ErrorValue, EffectState, Settlement, SettlementBasis, EvidenceValue
from decimal import Decimal
from camctl.devices.ports import ResultDriver, DeviceCallResult
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.contracts.values import ConsistencyError
from .result_consumer_fixtures import consumer_world, ResultCatalog
from .test_capture_contract import _runtime
from .test_result_page_history import _REGISTRY, _BINDING, _CURSOR, _actual, _entry, _baseline_world, _request, _path_entry
from .test_baseline_start import owned
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.contracts.workflow_errors import registered_error
from .test_result_file_recovery import FileWriteFault

pytestmark = pytest.mark.asyncio


def _pages(runtime, *, first=None, last=None, finalized=True):
    driver = create_autospec(ResultDriver, instance=True)
    async def read(request, batch):
        cursor = None if "cursor" not in request.params else _CURSOR
        entries = (first if first is not None else [_entry("a")]) if cursor is None else (last if last is not None else [_entry("b")])
        actual = _actual(request.ticket, entries=entries,
            cursor=cursor, next_cursor=_CURSOR if cursor is None else None, finalized=finalized and cursor is not None)
        return DeviceCallResult.from_outcome(actual)
    driver.list_results.side_effect = read
    runtime.evidence = _REGISTRY
    runtime.results = DriverResultListing(driver, _BINDING, _REGISTRY)
    return driver


async def test_runtime_reads_and_saves_two_pages_in_one_results_attempt(tmp_path):
    owned, runtime, action_id, _ = await consumer_world(tmp_path, "record")
    driver = _pages(runtime)
    try:
        listing = await _listing_round(runtime, action_id)
        assert listing.phase is ListingPhase.LISTED
        assert [entry.identity for entry in listing.entries] == ["/DCIM/a.mp4", "/DCIM/b.mp4"]
        assert listing.scan_complete and listing.set_finalized
        assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE kind=7").fetchone() == (1,)
        assert owned.connection.execute("SELECT status,result_json FROM operation_attempts WHERE run_id=?", (listing.ticket.run_id,)).fetchone() == (1, None)
        assert owned.connection.execute("SELECT COUNT(*) FROM device_files").fetchone() == (2,)
        assert driver.list_results.await_count == 2
    finally:
        owned.connection.close()


async def test_runtime_reopens_unknown_first_page_before_reading_second_page(tmp_path):
    owned, runtime, action_id, _ = await consumer_world(tmp_path, "record")
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    driver = _pages(runtime)
    fault = FileWriteFault(owned.connection, "commit_after")
    runtime.owned = replace(owned, connection=fault)
    try:
        with pytest.raises(ConsistencyError):
            await _listing_round(runtime, action_id)
        driver.list_results.assert_awaited_once()
        pending, = runtime.pending_result_scans.values()
        original = pending.saves.pending
        assert original.request.page_no == 1 and not pending.finished
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        resumed = _runtime(owned)
        resumed.results, resumed.evidence = runtime.results, runtime.evidence
        resumed.pending_result_scans = runtime.pending_result_scans
        resumed.wall_us = lambda: original.request.occurred_at + 5_000_000
        listing = await _listing_round(resumed, action_id)
        assert listing.phase is ListingPhase.LISTED and listing.set_finalized
        assert driver.list_results.await_count == 2
        pages = resumed.capture.read_result_pages(listing.ticket, None, 128, owned).items
        assert len(pages) == 2 and pages[0].occurred_at == original.request.occurred_at
        assert pages[0].page.outcome == original.request.outcome.outcome
    finally:
        owned.connection.close()


async def test_closed_scan_recovers_original_pages_without_device_or_current_clock(tmp_path):
    owned, runtime, action_id, _ = await consumer_world(tmp_path, "record")
    driver = _pages(runtime)
    try:
        original = await _listing_round(runtime, action_id)
        _finish_listing_result(runtime, original, end_run=RunOutcome.SUCCEEDED)
        resumed = _runtime(owned)
        resumed.wall_us = lambda: pytest.fail("已保存的结果输入不能取得新的时钟")
        listing = _saved_result_listing(resumed, action_id)
        assert listing.phase is ListingPhase.CLOSED and listing.already_saved
        assert listing.set_finalized and listing.scan_complete
        assert [entry.identity for entry in listing.entries] == ["/DCIM/a.mp4", "/DCIM/b.mp4"]
        assert listing.outcome == original.outcome and listing.occurred_at == original.occurred_at
        assert driver.list_results.await_count == 2
    finally:
        owned.connection.close()


async def test_page_registration_resolves_preview_from_another_page(tmp_path):
    owned, runtime, action_id, _ = await consumer_world(tmp_path, "record")
    preview = {**_entry("a"), "paired_identity": "/DCIM/b.mp4", "kind": "other"}
    _pages(runtime, first=[preview])
    try:
        listing = await _listing_round(runtime, action_id)
        registered = _register_listing(runtime, action_id, listing)
        assert len(tuple(registered)) == 2
        rows = owned.connection.execute("SELECT role,original_device_file_id,completion_state,source_action_id FROM device_files ORDER BY id").fetchall()
        assert rows == [(3, 2, 3, action_id), (2, None, 3, action_id)]
    finally:
        owned.connection.close()


async def test_page_registration_uses_original_fixed_baseline_difference(owned, tmp_path):
    runtime, ticket, _ = await _baseline_world(owned, tmp_path, ["a.mp4"])
    root = "/mnt/media_rw/emulated/DCIM"
    actual = _actual(ticket, entries=[_path_entry(f"{root}/a.mp4"), _path_entry(f"{root}/b.mp4")], finalized=True)
    request = _request(runtime, ticket, actual)
    receipt = runtime.capture.save_result_page(request, new_operation_key(), owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    entries = SavedResultEntries(runtime.capture, owned, receipt.value)
    listing = ListingRound(ListingPhase.LISTED, entries, ticket, actual, request.occurred_at,
                           scan_complete=True, set_finalized=True)
    registered = tuple(_register_listing(runtime, 1, listing))
    assert len(registered) == 1 and registered[0][0].file_id == f"{root}/b.mp4"
    import json
    row = owned.connection.execute("SELECT source_action_id,ownership_evidence_json,completion_state FROM device_files").fetchone()
    assert row[0] == 1 and row[2] == 3
    assert json.loads(row[1])["method"] == 2 and json.loads(row[1])["activity_id"] == int(ticket.target_id)


@pytest.mark.parametrize("case", ["photos", "empty", "wrong_kind", "not_finalized", "incomplete"])
async def test_photo_consumes_original_pages_and_precise_output_failure(tmp_path, case):
    owned, runtime, action_id, handler = await consumer_world(tmp_path, "photo", independent_activity=True)
    first = [{**_entry("a"), "kind": "photo", "complete": case != "incomplete"}]
    last = [{**_entry("b"), "kind": "photo"}]
    if case == "empty":
        first = last = []
    if case == "wrong_kind":
        first, last = [_entry("a")], [_entry("b")]
    driver = _pages(runtime, first=first, last=last, finalized=case != "not_finalized")
    try:
        await capture_handler(handler)(action_id, runtime)
        status, code, details = owned.connection.execute("SELECT status,error_code,error_details_json FROM actions WHERE id=?", (action_id,)).fetchone()
        if case == "photos":
            assert status == 3 and code is None
            assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (2,)
        elif case in ("empty", "wrong_kind"):
            assert status == 4 and code == registered_error("capture_failed")["action_error_id"]
            assert json.loads(details) == {"activity_id": "1", "reason": "no_outputs" if case == "empty" else "invalid_outputs"}
        else:
            assert status == 2 and code is None
            assert owned.connection.execute("SELECT retry_wait_required FROM operation_runs WHERE kind=7").fetchone() == (1,)
        assert driver.list_results.await_count == 2
    finally:
        owned.connection.close()


@pytest.mark.parametrize("case", ["video", "empty", "wrong_kind"])
async def test_recording_consumes_finalized_pages_and_distinguishes_output_failures(tmp_path, case):
    owned, runtime, action_id, handler = await consumer_world(tmp_path, "record", independent_activity=True)
    if case == "empty":
        first = last = []
    elif case == "wrong_kind":
        first, last = [{**_entry("a"), "kind": "photo"}], []
    else:
        first, last = None, None
    driver = _pages(runtime, first=first, last=last)
    try:
        await capture_handler(handler)(action_id, runtime)
        status, error_code, raw_error = owned.connection.execute("SELECT status,error_code,error_details_json FROM actions WHERE id=?", (action_id,)).fetchone()
        if case == "video":
            assert status == 3 and error_code is None and raw_error is None
            assert owned.connection.execute("SELECT COUNT(*) FROM outputs WHERE source_action_id=?", (action_id,)).fetchone() == (2,)
        else:
            assert status == 4
            assert error_code == registered_error("capture_failed")["action_error_id"]
            assert json.loads(raw_error) == {"activity_id": "1", "reason": "no_outputs" if case == "empty" else "invalid_outputs"}
        assert driver.list_results.await_count == 2
    finally:
        owned.connection.close()


@pytest.mark.parametrize("case", ["matching", "missing_photo", "wrong_format", "wrong_count", "unknown_format"])
async def test_recording_uses_product_rules_fixed_at_acceptance(tmp_path, case):
    rules = [{"kind": "video", "format_id": "mp4", "min_count": 1,
              "exact_count": 3 if case == "wrong_count" else None, "require_pairing": False}]
    if case == "missing_photo":
        rules.append({"kind": "photo", "format_id": None, "min_count": 1,
                      "exact_count": None, "require_pairing": False})
    class Catalog(ResultCatalog):
        def parameter_definition(self, device_id, action_type, parameter_type):
            definition = super().parameter_definition(device_id, action_type, parameter_type)
            return replace(definition, task_factory=lambda params: replace(
                definition.task_factory(params), product_rules=tuple(rules)))
    owned, runtime, action_id, handler = await consumer_world(tmp_path, "record", catalog=Catalog())
    first, last = _entry("a"), _entry("b")
    if case in ("wrong_format", "unknown_format"):
        first["format_id"] = last["format_id"] = "mov" if case == "wrong_format" else None
    _pages(runtime, first=[first], last=[last])
    try:
        assert runtime.action(action_id)["execution_spec_json"]["product_rules"] == rules
        await capture_handler(handler)(action_id, runtime)
        status, error_code, details = owned.connection.execute("SELECT status,error_code,error_details_json FROM actions").fetchone()
        if case == "matching":
            assert (status, error_code, details) == (3, None, None)
        elif case == "unknown_format":
            assert (status, error_code, details) == (2, None, None)
            assert owned.connection.execute("SELECT retry_wait_required FROM operation_runs WHERE kind=7").fetchone() == (1,)
        else:
            assert status == 4 and error_code == registered_error("capture_failed")["action_error_id"]
            assert json.loads(details) == {"activity_id": "1", "reason": "invalid_outputs"}
    finally:
        owned.connection.close()


@pytest.mark.parametrize("case", ["video", "empty", "wrong_kind"])
async def test_timelapse_uses_wait_and_finalized_pages_without_fabricating_device_end(tmp_path, case):
    owned, runtime, action_id, handler = await consumer_world(tmp_path, "timelapse", independent_activity=True)
    first = last = [] if case == "empty" else None
    if case == "wrong_kind":
        first, last = [{**_entry("a"), "kind": "photo"}], []
    driver = _pages(runtime, first=first, last=last)
    try:
        await capture_handler(handler)(action_id, runtime)
        status, code, details = owned.connection.execute("SELECT status,error_code,error_details_json FROM actions WHERE id=?", (action_id,)).fetchone()
        activity = owned.connection.execute("SELECT activity_state,completion_basis,occupancy_state,capture_json,last_error_json FROM device_activities").fetchone()
        assert driver.list_results.await_count == 2
        if case == "video":
            assert (status, code, details) == (3, None, None)
            assert activity[:3] == (1, 3, 2)
            assert json.loads(activity[3]) == {"status": "completed"}
            assert [json.loads(row[0])["basis"] for row in owned.connection.execute("SELECT completion_evidence_json FROM device_files")] == [2, 2]
        else:
            assert status == 4 and code == registered_error("capture_failed")["action_error_id"]
            expected = {"activity_id": "1", "reason": "no_outputs" if case == "empty" else "invalid_outputs"}
            assert json.loads(details) == expected
            assert json.loads(activity[4]) == {"code": "capture_failed", "stage": "execution", "details": expected}
    finally:
        owned.connection.close()


async def test_later_page_read_error_keeps_previous_completed_files_at_exhaustion(tmp_path):
    owned, runtime, action_id, handler = await consumer_world(tmp_path, "record")
    driver = _pages(runtime, finalized=False)
    runtime.check_config = AttemptConfig(2, Decimal("1.25"), Decimal("2"))
    try:
        await capture_handler(handler)(action_id, runtime)
        assert runtime.action(action_id)["status"] == 2
        actual = CallOutcome(status=AttemptStatus.FAILED,
            error=ErrorValue("directory_read_failed", "device", {"received_bytes": 17}),
            effect=EffectState.UNKNOWN,
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("results_returned", 1, {})))
        driver.list_results.return_value = DeviceCallResult.from_outcome(actual)
        driver.list_results.side_effect = None
        runtime.monotonic_ns = lambda: 70_000_000_000
        runtime.wall_us = lambda: 1_750_000_070_000_000
        await capture_handler(handler)(action_id, runtime)
        runtime.monotonic_ns = lambda: 75_000_000_000
        runtime.wall_us = lambda: 1_750_000_075_000_000
        await capture_handler(handler)(action_id, runtime)
        assert runtime.action(action_id)["status"] == 4
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs WHERE source_action_id=?", (action_id,)).fetchone() == (2,)
        assert driver.list_results.await_count == 3
        error, = owned.connection.execute("SELECT error_json FROM operation_attempts WHERE run_id IN (SELECT id FROM operation_runs WHERE kind=7) ORDER BY attempt_no DESC LIMIT 1").fetchone()
        assert json.loads(error) == {"code": "directory_read_failed", "stage": "device", "details": {"received_bytes": 17}}
    finally:
        owned.connection.close()


async def test_source_file_consumer_continues_after_its_first_batch(tmp_path):
    owned, runtime, action_id, _ = await consumer_world(tmp_path, "record")
    _pages(runtime, first=[_entry(f"a{index:02d}") for index in range(33)], last=[])
    try:
        listing = await _listing_round(runtime, action_id)
        registered = _register_listing(runtime, action_id, listing)
        first = runtime.capture.read_result_sources(listing.ticket, None, 32, owned)
        assert len(first.items) == 32 and first.next_cursor == 32
        tail = runtime.capture.read_result_sources(listing.ticket, first.next_cursor, 32, owned)
        assert [entry.identity for entry, _, _ in tail.items] == ["/DCIM/a32.mp4"] and tail.next_cursor is None
        assert len(tuple(registered)) == 33
    finally:
        owned.connection.close()


@pytest.mark.parametrize("cached", [False, True])
async def test_completed_scan_pending_disposition_uses_reopened_database(tmp_path, cached):
    owned, runtime, action_id, _ = await consumer_world(tmp_path, "record")
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    driver = _pages(runtime)
    try:
        original = await _listing_round(runtime, action_id)
        if cached:
            registered = _register_listing(runtime, action_id, original)
            runtime.listing_cache = {action_id: (registered.entries, registered)}
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        resumed = _runtime(owned)
        resumed.pending_start_results = runtime.pending_start_results
        resumed.pending_result_scans = runtime.pending_result_scans
        resumed.listing_cache = runtime.listing_cache
        resumed.wall_us = lambda: pytest.fail("原轮次不能读取新的时钟")
        held = await _listing_round(resumed, action_id)
        assert held.set_finalized and held.scan_complete
        assert held.outcome is original.outcome and held.occurred_at == original.occurred_at
        assert [entry.identity for entry in held.entries] == ["/DCIM/a.mp4", "/DCIM/b.mp4"]
        assert driver.list_results.await_count == 2
        if cached:
            resumed.wall_us = runtime.wall_us
            await _advance_recording_outcome(resumed, resumed.action(action_id),
                SimpleNamespace(phase=RecordingPhase.CONTROL_COMPLETE))
            assert resumed.action(action_id)["status"] == 3
    finally:
        owned.connection.close()


async def test_timelapse_accepts_declared_photo_and_video_products(tmp_path):
    rules = tuple({"kind": kind, "format_id": format_id, "min_count": 1,
                   "exact_count": None, "require_pairing": False}
                  for kind, format_id in (("photo", "jpeg"), ("video", "mp4")))
    class Catalog(ResultCatalog):
        def parameter_definition(self, device_id, action_type, parameter_type):
            definition = super().parameter_definition(device_id, action_type, parameter_type)
            return replace(definition, task_factory=lambda params: replace(
                definition.task_factory(params), product_rules=rules))
    owned, runtime, action_id, handler = await consumer_world(tmp_path, "timelapse", catalog=Catalog())
    photo = {**_entry("a"), "identity": "/DCIM/a.jpg", "locator": {"path": "/DCIM/a.jpg"},
             "kind": "photo", "format_id": "jpeg"}
    _pages(runtime, first=[photo])
    try:
        await capture_handler(handler)(action_id, runtime)
        assert runtime.action(action_id)["status"] == 3
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs WHERE source_action_id=?", (action_id,)).fetchone() == (2,)
        assert owned.connection.execute("SELECT completion_basis FROM device_activities").fetchone() == (3,)
    finally:
        owned.connection.close()

"""真实结果驱动、逐页仓储与运行时组合，原保存未知不读下一页。"""
from dataclasses import replace
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from support.repository_ports import repository_port

from camctl.bootstrap.capture_assembly import DriverResultListing
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.models import DbOutcome
from ..support.result_cases import ResultPageFault, result_case, result_case_for, current_result_ticket, saved_result_pages
from camctl.capture.handlers import capture_handler
import json
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import CallOutcome, AttemptStatus, ErrorValue, EffectState, Settlement, SettlementBasis, EvidenceValue
from decimal import Decimal
from camctl.devices.ports import ResultDriver, DeviceCallResult
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.contracts.values import ConsistencyError
from .result_consumer_fixtures import ResultCatalog
from .test_result_page_history import _REGISTRY, _BINDING, _CURSOR, _actual, _entry, _baseline_world, _request, _path_entry
from .test_baseline_start import owned
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.contracts.workflow_errors import registered_error

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
    try:
        case = result_case_for()
    except RuntimeError:
        runtime.evidence = _REGISTRY
        runtime.results = DriverResultListing(driver, _BINDING, _REGISTRY)
    else:
        case.results.delegate = DriverResultListing(driver, _BINDING, _REGISTRY)
    return driver


async def test_runtime_reads_and_saves_two_pages_in_one_results_attempt(tmp_path):
    class ObserveConclusion(CaptureRepository):
        def __init__(self):
            self.before = []
        def finish_result_check(self, finish, confirm, key, owned, **options):
            self.before.append(owned.connection.execute(
                "SELECT status,result_json FROM operation_attempts WHERE run_id=?",
                (finish.ticket.run_id,)).fetchone())
            return super().finish_result_check(finish, confirm, key, owned, **options)
    owned, runtime, action_id, handler = await result_case_for().world(tmp_path, "record")
    driver = _pages(runtime)
    observer = ObserveConclusion()
    try:
        with repository_port(CaptureRepository, observer, ("finish_result_check",),
                matches=lambda _name, args, _kwargs: args[0].ticket.responsibility_key == f"results/{action_id}"):
            await capture_handler(handler)(action_id, runtime)
        pages = saved_result_pages(owned, action_id)
        assert [entry.identity for page in pages for entry in page.page.entries] == ["/DCIM/a.mp4", "/DCIM/b.mp4"]
        assert pages[-1].page.scan_complete and pages[-1].page.set_finalized
        assert observer.before == [(1, None)]
        assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE kind=7").fetchone() == (1,)
        assert owned.connection.execute("SELECT COUNT(*) FROM device_files").fetchone() == (2,)
        assert driver.list_results.await_count == 2
    finally:
        owned.connection.close()


@pytest.mark.parametrize("mode", ["unknown", "projection", "commit_before", "commit_after"])
async def test_runtime_reopens_first_page_before_reading_second_page(tmp_path, mode):
    class FirstPageFault(CaptureRepository):
        def __init__(self):
            self.requests = []
            self.fault = None
            self.unknown_enabled = True

        def save_result_page(self, request, key, owned):
            self.requests.append((request, key))
            if mode != "unknown" and self.fault is None:
                self.fault = ResultPageFault(owned.connection, mode)
                owned = replace(owned, connection=self.fault)
            if request.page_no == 1 and len(self.requests) > 1:
                assert driver.list_results.await_count == 1
            saved = super().save_result_page(request, key, owned)
            if mode == "unknown" and self.unknown_enabled:
                assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
                return DbOutcome(DbOutcomeKind.UNKNOWN, error=ConsistencyError("原页提交回执未知"))
            return saved

    case = result_case_for()
    owned, runtime, action_id, handler = await case.world(tmp_path, "record")
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    metadata = runtime.database.metadata
    driver = _pages(runtime)
    repository = FirstPageFault()
    try:
        with repository_port(CaptureRepository, repository, ("save_result_page",),
                matches=lambda _name, args, _kwargs: args[0].ticket.responsibility_key == f"results/{action_id}"):
            with pytest.raises(ConsistencyError):
                await capture_handler(handler)(action_id, runtime)
            driver.list_results.assert_awaited_once()
            original, key = repository.requests[0]
            assert all(request == original and saved_key == key for request, saved_key in repository.requests)
            assert original.page_no == 1
            if mode != "unknown":
                assert repository.fault is not None and not repository.fault.armed
            before = saved_result_pages(owned, action_id)
            assert len(before) == (0 if mode in ("projection", "commit_before") else 1)
            assert owned.connection.execute("SELECT COUNT(*) FROM device_files").fetchone() == (len(before),)
            repository.unknown_enabled = False
            owned.connection.close()
            owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
            resumed = await case.reopen(owned)
            assert resumed.database.metadata == metadata
            case.clock.wall = original.occurred_at + 5_000_000
            await capture_handler(handler)(action_id, resumed)
        first_requests = [(request, saved_key) for request, saved_key in repository.requests if request.page_no == 1]
        assert len(first_requests) >= 2 and all(item == (original, key) for item in first_requests)
        assert driver.list_results.await_count == 2
        pages = saved_result_pages(owned, action_id)
        assert len(pages) == 2 and pages[0].occurred_at == original.occurred_at
        assert pages[0].page.outcome == original.outcome.outcome
        assert pages[-1].page.scan_complete and pages[-1].page.set_finalized
        assert [entry.identity for page in pages for entry in page.page.entries] == ["/DCIM/a.mp4", "/DCIM/b.mp4"]
        assert [identity for page in pages for identity, _ in page.file_ids] == ["/DCIM/a.mp4", "/DCIM/b.mp4"]
        assert owned.connection.execute("SELECT COUNT(*) FROM device_files").fetchone() == (2,)
        assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE id=?", (original.ticket.run_id,)).fetchone() == (1,)
        assert owned.connection.execute(
            "SELECT t.operation_key,e.occurred_at FROM history_events e JOIN history_transactions t ON t.id=e.transaction_id WHERE e.id=?",
            (pages[0].ref.event_id,)).fetchone() == (str(key), original.occurred_at)
        assert owned.connection.execute("SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(key),)).fetchone() == (1,)
    finally:
        owned.connection.close()


async def test_closed_scan_pages_read_on_reopened_database_worker(tmp_path):
    owned, runtime, action_id, handler = await result_case_for().world(tmp_path, "record")
    driver = _pages(runtime)
    try:
        await capture_handler(handler)(action_id, runtime)
        original = saved_result_pages(owned, action_id)
        resumed = await result_case_for().reopen(owned, cold=True)
        pages = await resumed.database.read(lambda current: saved_result_pages(current, action_id))
        assert pages == original
        assert pages[-1].page.set_finalized and pages[-1].page.scan_complete
        assert [entry.identity for page in pages for entry in page.page.entries] == ["/DCIM/a.mp4", "/DCIM/b.mp4"]
        assert driver.list_results.await_count == 2
    finally:
        owned.connection.close()


async def test_page_registration_resolves_preview_from_another_page(tmp_path):
    owned, runtime, action_id, handler = await result_case_for().world(tmp_path, "record")
    preview = {**_entry("a"), "paired_identity": "/DCIM/b.mp4", "kind": "other"}
    _pages(runtime, first=[preview])
    try:
        await capture_handler(handler)(action_id, runtime)
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
    from camctl.capture.files import FileCompletionSave, OwnershipSave
    page = runtime.capture.read_result_page(receipt.value, owned)
    assert len(page.file_ids) == 1
    identity, file_id = page.file_ids[0]
    assert identity == f"{root}/b.mp4"
    for save, command in (
        (runtime.capture.save_file_ownership,
         OwnershipSave(file_id, 1, 2, 2, {"result_page_event_id": page.ref.event_id},
                       page.occurred_at, activity_id=int(ticket.target_id))),
        (runtime.capture.save_file_completion,
         FileCompletionSave(file_id, 3, page.occurred_at, basis=1,
                            observation={"size_verified": True}, size_bytes=50)),
    ):
        saved = save(command, new_operation_key(), owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    row = owned.connection.execute("SELECT source_action_id,ownership_evidence_json,completion_state FROM device_files").fetchone()
    assert row[0] == 1 and row[2] == 3
    assert json.loads(row[1])["method"] == 2 and json.loads(row[1])["activity_id"] == int(ticket.target_id)


@pytest.mark.parametrize("case", ["photos", "empty", "wrong_kind", "not_finalized", "incomplete"])
async def test_photo_consumes_original_pages_and_precise_output_failure(tmp_path, case):
    owned, runtime, action_id, handler = await result_case_for().world(tmp_path, "photo", independent_activity=True)
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
    owned, runtime, action_id, handler = await result_case_for().world(tmp_path, "record", independent_activity=True)
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
    owned, runtime, action_id, handler = await result_case_for().world(tmp_path, "record", catalog=Catalog())
    first, last = _entry("a"), _entry("b")
    if case in ("wrong_format", "unknown_format"):
        first["format_id"] = last["format_id"] = "mov" if case == "wrong_format" else None
    _pages(runtime, first=[first], last=[last])
    try:
        assert (await runtime.action(action_id))["execution_spec_json"]["product_rules"] == rules
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
    owned, runtime, action_id, handler = await result_case_for().world(tmp_path, "timelapse", independent_activity=True)
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
    owned, runtime, action_id, handler = await result_case_for().world(tmp_path, "record",
        assembly_options={"check_config": AttemptConfig(2, Decimal("1.25"), Decimal("2"))})
    driver = _pages(runtime, finalized=False)
    try:
        await capture_handler(handler)(action_id, runtime)
        assert (await runtime.action(action_id))["status"] == 2
        actual = CallOutcome(status=AttemptStatus.FAILED,
            error=ErrorValue("directory_read_failed", "device", {"received_bytes": 17}),
            effect=EffectState.UNKNOWN,
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("results_returned", 1, {})))
        driver.list_results.return_value = DeviceCallResult.from_outcome(actual)
        driver.list_results.side_effect = None
        result_case_for().clock.monotonic = 70_000_000_000
        result_case_for().clock.wall = 1_750_000_070_000_000
        await capture_handler(handler)(action_id, runtime)
        result_case_for().clock.monotonic = 75_000_000_000
        result_case_for().clock.wall = 1_750_000_075_000_000
        await capture_handler(handler)(action_id, runtime)
        assert (await runtime.action(action_id))["status"] == 4
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs WHERE source_action_id=?", (action_id,)).fetchone() == (2,)
        assert driver.list_results.await_count == 3
        error, = owned.connection.execute("SELECT error_json FROM operation_attempts WHERE run_id IN (SELECT id FROM operation_runs WHERE kind=7) ORDER BY attempt_no DESC LIMIT 1").fetchone()
        assert json.loads(error) == {"code": "directory_read_failed", "stage": "device", "details": {"received_bytes": 17}}
    finally:
        owned.connection.close()


async def test_source_file_consumer_continues_after_its_first_batch(tmp_path):
    owned, runtime, action_id, handler = await result_case_for().world(tmp_path, "record")
    _pages(runtime, first=[_entry(f"a{index:02d}") for index in range(33)], last=[])
    try:
        await capture_handler(handler)(action_id, runtime)
        ticket = current_result_ticket(owned, action_id)
        first = runtime.capture.read_result_sources(ticket, None, 32, owned)
        assert len(first.items) == 32 and first.next_cursor == 32
        tail = runtime.capture.read_result_sources(ticket, first.next_cursor, 32, owned)
        assert [entry.identity for entry, _, _ in tail.items] == ["/DCIM/a32.mp4"] and tail.next_cursor is None
        assert owned.connection.execute("SELECT COUNT(*) FROM device_files WHERE source_action_id=?", (action_id,)).fetchone() == (33,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("registered", [False, True])
async def test_completed_scan_pending_disposition_uses_reopened_database(tmp_path, registered):
    class UnknownDisposition(CaptureRepository):
        def __init__(self):
            self.faults = []
        def finish_result_check(self, finish, confirm, key, owned, **options):
            saved = super().finish_result_check(finish, confirm, key, owned, **options)
            if not registered:
                assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
                self.faults.append("finish_result_check")
                return DbOutcome(DbOutcomeKind.UNKNOWN, error=ConsistencyError("原结果交接回执未知"))
            return saved
        def finish_capture(self, request, key, owned):
            saved = super().finish_capture(request, key, owned)
            assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
            self.faults.append("finish_capture")
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=ConsistencyError("已登记文件的完成回执未知"))
    owned, runtime, action_id, handler = await result_case_for().world(tmp_path, "record")
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    driver = _pages(runtime)
    observer = UnknownDisposition()
    try:
        with repository_port(CaptureRepository, observer, ("finish_result_check", "finish_capture"),
                matches=lambda name, args, _kwargs: (
                    args[0].ticket.responsibility_key == f"results/{action_id}"
                    if name == "finish_result_check" else args[0].action_id == action_id)):
            with pytest.raises(ConsistencyError):
                await capture_handler(handler)(action_id, runtime)
        assert observer.faults and set(observer.faults) == {
            "finish_capture" if registered else "finish_result_check"}
        original = saved_result_pages(owned, action_id)
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        resumed = await result_case_for().reopen(owned)
        result_case_for().clock.wall_error = "原轮次读取不能取得新的时钟"
        held = saved_result_pages(owned, action_id)
        assert held == original and held[-1].page.set_finalized and held[-1].page.scan_complete
        assert [entry.identity for page in held for entry in page.page.entries] == ["/DCIM/a.mp4", "/DCIM/b.mp4"]
        assert driver.list_results.await_count == 2
        result_case_for().clock.wall_error = None
        await capture_handler(handler)(action_id, resumed)
        assert (await resumed.action(action_id))["status"] == 3
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
    owned, runtime, action_id, handler = await result_case_for().world(tmp_path, "timelapse", catalog=Catalog())
    photo = {**_entry("a"), "identity": "/DCIM/a.jpg", "locator": {"path": "/DCIM/a.jpg"},
             "kind": "photo", "format_id": "jpeg"}
    _pages(runtime, first=[photo])
    try:
        await capture_handler(handler)(action_id, runtime)
        assert (await runtime.action(action_id))["status"] == 3
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs WHERE source_action_id=?", (action_id,)).fetchone() == (2,)
        assert owned.connection.execute("SELECT completion_basis FROM device_activities").fetchone() == (3,)
    finally:
        owned.connection.close()

"""RESULTS 页事实沿一次原尝试逐页可靠保存；当前状态与历史一致。"""
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path

import pytest

from camctl.capture import result_pages
from camctl.capture.result_inputs import RESULT_PAGE_CONTRACT
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryCursor
from camctl.devices.evidence import DeviceObservation, EvidenceRegistry
from camctl.operations.models import CallOutcome, CallInfo, EffectState, EvidenceValue, Settlement, SettlementBasis, AttemptStatus, ErrorValue
from camctl.operations.validation import validate_outcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.capture.handlers import _begin_check_round
from camctl.contracts.values import ConsistencyError, new_operation_key
from .result_consumer_fixtures import consumer_world, RESULT_EVIDENCE
from .test_baseline_start import _prepared, _grant, owned
from .test_baseline_history import _append, _fix, _entries
from .test_capture_contract import _runtime
from camctl.operations.attempts import AttemptFinish, RunFinish, RunOutcome
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from .test_result_file_recovery import FileWriteFault
from camctl.persistence.repositories.history import HistoryRepository, SqliteSnapshotStore
from camctl.persistence.executor import DbExecutor
from camctl.history.snapshots import SnapshotRef, prepare_snapshot
from camctl.history.decoding import decode_event_row
from camctl.history.events import event_type_name, branch_of
from camctl.history.validators import ValidatedEvent
from camctl.history.replay import EntityImage, RestoreSeed, restore
from camctl.contracts.history_values import INITIAL_BOUNDARY

pytestmark = pytest.mark.asyncio
_BINDING = DeviceBinding("cam-1", "camctl-adb")
_CURSOR = DirectoryCursor(_BINDING, ("/DCIM",), 0, "/DCIM/a.mp4")
_REGISTRY = EvidenceRegistry((RESULT_PAGE_CONTRACT, RESULT_EVIDENCE.contract("results_returned", 1)))


def _actual(ticket, *, entries=(), cursor=None, next_cursor=None, finalized=False, error=None):
    return CallOutcome(status=AttemptStatus.SUCCEEDED if error is None else AttemptStatus.FAILED,
        error=error, effect=EffectState.CONFIRMED,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("results_returned", 1, {})),
        observations=(DeviceObservation("result_files_listed", 2, {
            "activity_id": ticket.target_id, "entries": list(entries),
            "cursor": None if cursor is None else cursor.as_json(),
            "next_cursor": None if next_cursor is None else next_cursor.as_json(),
            "set_finalized": finalized, "completion_evidence": None}),),
        call_info=CallInfo(local_exit_code=0 if error is None else 7))


def _entry(name):
    return {"identity": f"/DCIM/{name}.mp4", "locator": {"path": f"/DCIM/{name}.mp4"},
            "complete": True, "size_bytes": 50, "kind": "video", "format_id": "mp4"}


def _request(runtime, ticket, actual, *, page_no=1, cursor=None):
    return result_pages.ResultPageSave(ticket, page_no, cursor,
        validate_outcome(ticket, actual, _REGISTRY), runtime.wall_us())


async def test_two_pages_use_one_attempt_and_keep_both_actual_calls(tmp_path):
    owned, runtime, action_id, _ = await consumer_world(tmp_path, "record")
    repository = CaptureRepository()
    try:
        ticket = _begin_check_round(runtime, action_id).ticket
        first = _request(runtime, ticket, _actual(ticket, entries=[_entry("a")], next_cursor=_CURSOR))
        saved = repository.save_result_page(first, new_operation_key(), owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        second = _request(runtime, ticket, _actual(ticket, entries=[_entry("b")], cursor=_CURSOR, finalized=True), page_no=2, cursor=_CURSOR)
        last = repository.save_result_page(second, new_operation_key(), owned)
        assert last.kind is DbOutcomeKind.COMPLETED, last.error
        assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (1,)
        assert owned.connection.execute("SELECT status,result_json FROM operation_attempts WHERE run_id=?", (ticket.run_id,)).fetchone() == (1, None)
        pages = repository.read_result_pages(ticket, None, 1, owned)
        assert len(pages.items) == 1 and pages.next_cursor == saved.value.event_id
        tail = repository.read_result_pages(ticket, pages.next_cursor, 1, owned)
        assert len(tail.items) == 1 and tail.next_cursor is None
        assert pages.items[0].page.outcome == first.outcome.outcome
        assert tail.items[0].page.outcome == second.outcome.outcome
        assert [row[0] for row in owned.connection.execute("SELECT original_name FROM device_files ORDER BY id")] == [None, None]
    finally:
        owned.connection.close()


async def test_original_page_key_recovers_exact_input_and_rejects_changed_error(tmp_path):
    owned, runtime, action_id, _ = await consumer_world(tmp_path, "record")
    try:
        ticket = _begin_check_round(runtime, action_id).ticket
        actual = _actual(ticket, error=ErrorValue("directory_read_failed", "device", {"received_bytes": 9}))
        request = _request(runtime, ticket, actual)
        key = new_operation_key()
        first = runtime.capture.save_result_page(request, key, owned)
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        replay = runtime.capture.save_result_page(request, key, owned)
        assert replay.kind is DbOutcomeKind.COMPLETED and replay.value == first.value
        changed = _request(runtime, ticket, replace(actual, error=ErrorValue("directory_read_failed", "device", {"received_bytes": 10})))
        assert runtime.capture.save_result_page(changed, key, owned).kind is DbOutcomeKind.ROLLED_BACK
        page = runtime.capture.read_result_pages(ticket, None, 1, owned).items[0]
        assert page.page.outcome.error.details == {"received_bytes": 9}
        assert page.page.outcome.call_info.local_exit_code == 7
        assert not page.page.scan_complete
    finally:
        owned.connection.close()


@pytest.mark.parametrize("case", ["gap", "wrong_cursor", "after_end"])
async def test_page_sequence_rejects_gap_or_unrelated_cursor_or_after_end(tmp_path, case):
    owned, runtime, action_id, _ = await consumer_world(tmp_path, "record")
    try:
        ticket = _begin_check_round(runtime, action_id).ticket
        first = _request(runtime, ticket, _actual(ticket, next_cursor=None if case == "after_end" else _CURSOR))
        receipt = runtime.capture.save_result_page(first, new_operation_key(), owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        cursor = None if case == "wrong_cursor" else _CURSOR
        next_request = _request(runtime, ticket, _actual(ticket, cursor=cursor), page_no=3 if case == "gap" else 2, cursor=cursor)
        assert runtime.capture.save_result_page(next_request, new_operation_key(), owned).kind is DbOutcomeKind.ROLLED_BACK
        assert len(runtime.capture.read_result_pages(ticket, None, 128, owned).items) == 1
    finally:
        owned.connection.close()


async def _baseline_world(owned, tmp_path, names):
    activity_id = await _prepared(owned, tmp_path, "camera_record")
    for index in range(0, len(names), 128):
        saved = _append(owned, activity_id, _entries(*names[index:index + 128]), chunk_no=index // 128 + 1)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    fixed = _fix(owned, activity_id, chunks=(len(names) + 127) // 128, entries=len(names))
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    grant = _grant(owned)
    assert grant.kind is DbOutcomeKind.COMPLETED, grant.error
    ticket = grant.value.ticket
    actual = CallOutcome(effect=EffectState.CONFIRMED,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("operation_returned", 1, {})),
        observations=(DeviceObservation("start_confirmed", 1, {"activity_id": ticket.target_id}),))
    saved = OperationRepository().finish_attempt(AttemptFinish(ticket, validate_outcome(ticket, actual, RESULT_EVIDENCE),
        1_750_000_000_000_000, run_finish=RunFinish(RunOutcome.SUCCEEDED)), new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    runtime = _runtime(owned)
    return runtime, _begin_check_round(runtime, 1).ticket, fixed.value


def _path_entry(path):
    return {"identity": path, "locator": {"path": path}, "complete": True,
            "size_bytes": 50, "kind": "video", "format_id": "mp4"}


async def test_baseline_difference_excludes_old_files_and_resumes_bounded_position(owned, tmp_path):
    runtime, ticket, baseline = await _baseline_world(owned, tmp_path, ["a.mp4", "c.mp4", "e.mp4"])
    root = "/mnt/media_rw/emulated/DCIM"
    binding = DeviceBinding("cam-1", "dji-action6")
    cursor = DirectoryCursor(binding, (root, "/mnt/media_rw/sd/DCIM"), 0, f"{root}/b.mp4")
    first = _request(runtime, ticket, _actual(ticket, entries=[_path_entry(f"{root}/a.mp4"),
        _path_entry(f"{root}/b.mp4")], next_cursor=cursor))
    saved = runtime.capture.save_result_page(first, new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    page = runtime.capture.read_result_pages(ticket, None, 1, owned).items[0]
    assert [identity for identity, _ in page.file_ids] == [f"{root}/b.mp4"]
    evidence = json.loads(owned.connection.execute("SELECT body_json FROM history_events WHERE id=?", (saved.value.event_id,)).fetchone()[0])["evidence"]["result_page"]
    assert evidence["baseline_position"] == {"event_id": baseline.first_event_id, "entry_index": 1}
    second = _request(runtime, ticket, _actual(ticket, entries=[_path_entry(f"{root}/c.mp4"),
        _path_entry(f"{root}/d.mp4"), _path_entry(f"{root}/e.mp4")], cursor=cursor, finalized=True), page_no=2, cursor=cursor)
    saved = runtime.capture.save_result_page(second, new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    pages = runtime.capture.read_result_pages(ticket, None, 128, owned).items
    assert [identity for page in pages for identity, _ in page.file_ids] == [f"{root}/b.mp4", f"{root}/d.mp4"]
    assert owned.connection.execute("SELECT COUNT(*) FROM device_files").fetchone() == (2,)
    evidence = json.loads(owned.connection.execute("SELECT body_json FROM history_events WHERE id=?", (saved.value.event_id,)).fetchone()[0])["evidence"]["result_page"]
    assert evidence["baseline_position"] is None


@pytest.mark.parametrize("case", ["outside", "locator", "duplicate", "unsorted", "cursor_scope"])
async def test_baseline_result_page_rejects_wrong_scope_identity_or_order(owned, tmp_path, case):
    runtime, ticket, _ = await _baseline_world(owned, tmp_path, [])
    root = "/mnt/media_rw/emulated/DCIM"
    entries = [_path_entry(f"{root}/a.mp4")]
    next_cursor = None
    if case == "outside":
        entries = [_path_entry("/other/a.mp4")]
    elif case == "locator":
        entries[0]["locator"] = {"path": f"{root}/b.mp4"}
    elif case == "duplicate":
        entries *= 2
    elif case == "unsorted":
        entries = [_path_entry(f"{root}/b.mp4"), _path_entry(f"{root}/a.mp4")]
    else:
        next_cursor = DirectoryCursor(DeviceBinding("cam-1", "dji-action6"), ("/other",), 0, "/other/a.mp4")
    request = _request(runtime, ticket, _actual(ticket, entries=entries, next_cursor=next_cursor))
    receipt = runtime.capture.save_result_page(request, new_operation_key(), owned)
    assert receipt.kind is DbOutcomeKind.ROLLED_BACK
    assert owned.connection.execute("SELECT COUNT(*) FROM device_files").fetchone() == (0,)


async def test_empty_page_keeps_last_identity_and_rejects_cross_page_duplicate(owned, tmp_path):
    runtime, ticket, _ = await _baseline_world(owned, tmp_path, [])
    root = "/mnt/media_rw/emulated/DCIM"
    binding = DeviceBinding("cam-1", "dji-action6")
    cursor = DirectoryCursor(binding, (root, "/mnt/media_rw/sd/DCIM"), 0, f"{root}/a.mp4")
    next_cursor = DirectoryCursor(binding, (root, "/mnt/media_rw/sd/DCIM"), 0, f"{root}/b.mp4")
    first = _request(runtime, ticket, _actual(ticket, entries=[_path_entry(f"{root}/a.mp4")], next_cursor=cursor))
    assert runtime.capture.save_result_page(first, new_operation_key(), owned).kind is DbOutcomeKind.COMPLETED
    empty = _request(runtime, ticket, _actual(ticket, cursor=cursor, next_cursor=next_cursor), page_no=2, cursor=cursor)
    receipt = runtime.capture.save_result_page(empty, new_operation_key(), owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    evidence = json.loads(owned.connection.execute("SELECT body_json FROM history_events WHERE id=?", (receipt.value.event_id,)).fetchone()[0])["evidence"]["result_page"]
    assert evidence["last_identity"] == f"{root}/a.mp4"
    duplicate = _request(runtime, ticket, _actual(ticket, entries=[_path_entry(f"{root}/a.mp4")], cursor=next_cursor), page_no=3, cursor=next_cursor)
    assert runtime.capture.save_result_page(duplicate, new_operation_key(), owned).kind is DbOutcomeKind.ROLLED_BACK


async def test_baseline_comparison_crosses_chunk_boundary_and_persists_next_item(owned, tmp_path):
    names = [f"{number:03d}.mp4" for number in range(129)]
    runtime, ticket, baseline = await _baseline_world(owned, tmp_path, names)
    root = "/mnt/media_rw/emulated/DCIM"
    cursor = DirectoryCursor(DeviceBinding("cam-1", "dji-action6"), (root, "/mnt/media_rw/sd/DCIM"), 0, f"{root}/127.mp4")
    request = _request(runtime, ticket, _actual(ticket, entries=[_path_entry(f"{root}/127.mp4")], next_cursor=cursor))
    receipt = runtime.capture.save_result_page(request, new_operation_key(), owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    evidence = json.loads(owned.connection.execute("SELECT body_json FROM history_events WHERE id=?", (receipt.value.event_id,)).fetchone()[0])["evidence"]["result_page"]
    assert evidence["baseline_position"] == {"event_id": baseline.last_event_id, "entry_index": 0}
    request = _request(runtime, ticket, _actual(ticket, entries=[_path_entry(f"{root}/128.mp4"), _path_entry(f"{root}/129.mp4")], cursor=cursor), page_no=2, cursor=cursor)
    receipt = runtime.capture.save_result_page(request, new_operation_key(), owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    assert [identity for page in runtime.capture.read_result_pages(ticket, None, 128, owned).items
            for identity, _ in page.file_ids] == [f"{root}/129.mp4"]


async def test_new_file_in_previously_empty_declared_directory_is_in_difference(owned, tmp_path):
    runtime, ticket, _ = await _baseline_world(owned, tmp_path, ["a.mp4"])
    old = "/mnt/media_rw/emulated/DCIM/a.mp4"
    new = "/mnt/media_rw/sd/DCIM/a.mp4"
    request = _request(runtime, ticket, _actual(ticket, entries=[_path_entry(old), _path_entry(new)], finalized=True))
    receipt = runtime.capture.save_result_page(request, new_operation_key(), owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    page = runtime.capture.read_result_pages(ticket, None, 1, owned).items[0]
    assert [identity for identity, _ in page.file_ids] == [new]
    assert [entry.identity for entry in page.page.entries] == [old, new]


@pytest.mark.parametrize("mode", ["projection", "commit_before", "commit_after"])
async def test_page_and_first_discovery_fault_reuses_original_key_after_reopen(tmp_path, mode):
    owned, runtime, action_id, _ = await consumer_world(tmp_path, "record")
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    metadata = owned.metadata
    try:
        ticket = _begin_check_round(runtime, action_id).ticket
        request = _request(runtime, ticket, _actual(ticket, entries=[_entry("a")], next_cursor=_CURSOR))
        owner = result_pages.ResultPageSaveOwner()
        original = owner.begin(request)
        fault = FileWriteFault(owned.connection, mode)
        receipt = owner.save(runtime.capture, replace(owned, connection=fault))
        assert receipt.kind is not DbOutcomeKind.COMPLETED
        assert not fault.armed and owner.pending == original
        with pytest.raises(ConsistencyError):
            owner.take()
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        assert owned.metadata == metadata
        # 核实原页只访问状态库，拥有者仍保留实际调用与原时刻。
        receipt = owner.save(runtime.capture, owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        ref = owner.take()
        assert owned.connection.execute("SELECT COUNT(*) FROM device_files").fetchone() == (1,)
        pages = runtime.capture.read_result_pages(ticket, None, 1, owned)
        assert pages.items[0].ref == ref and pages.items[0].occurred_at == request.occurred_at
        assert pages.items[0].page.outcome == request.outcome.outcome
        assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (1,)
    finally:
        owned.connection.close()


async def test_original_first_page_key_remains_exact_after_later_pages(tmp_path):
    owned, runtime, action_id, _ = await consumer_world(tmp_path, "record")
    try:
        ticket = _begin_check_round(runtime, action_id).ticket
        first = _request(runtime, ticket, _actual(ticket, entries=[_entry("a")], next_cursor=_CURSOR))
        key = new_operation_key()
        receipt = runtime.capture.save_result_page(first, key, owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        second = _request(runtime, ticket, _actual(ticket, entries=[_entry("b")], cursor=_CURSOR), page_no=2, cursor=_CURSOR)
        assert runtime.capture.save_result_page(second, new_operation_key(), owned).kind is DbOutcomeKind.COMPLETED
        assert runtime.capture.save_result_page(first, key, owned).value == receipt.value
        assert runtime.capture.save_result_page(replace(first, occurred_at=first.occurred_at + 1), key, owned).kind is DbOutcomeKind.ROLLED_BACK
        altered = _request(runtime, ticket, _actual(ticket, entries=[_entry("b")], next_cursor=_CURSOR))
        assert runtime.capture.save_result_page(altered, key, owned).kind is DbOutcomeKind.ROLLED_BACK
    finally:
        owned.connection.close()


async def test_new_result_attempt_keeps_previous_interrupted_scan_separate(tmp_path):
    owned, runtime, action_id, _ = await consumer_world(tmp_path, "record")
    try:
        first_ticket = _begin_check_round(runtime, action_id).ticket
        first_actual = _actual(first_ticket, entries=[_entry("a")], next_cursor=_CURSOR)
        first = _request(runtime, first_ticket, first_actual)
        receipt = runtime.capture.save_result_page(first, new_operation_key(), owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        # 保留已知的本次实际返回，重新核实登记新轮次，不延长旧扫描。
        finish = OperationRepository().finish_attempt(AttemptFinish(first_ticket, first.outcome,
            first.occurred_at, retry_wait=True), new_operation_key(), owned)
        assert finish.kind is DbOutcomeKind.COMPLETED, finish.error
        second_ticket = _begin_check_round(runtime, action_id).ticket
        assert second_ticket.attempt_id == first_ticket.attempt_id + 1
        second = _request(runtime, second_ticket, _actual(second_ticket, entries=[_entry("b")], finalized=True))
        receipt = runtime.capture.save_result_page(second, new_operation_key(), owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        first_pages = runtime.capture.read_result_pages(first_ticket, None, 128, owned).items
        second_pages = runtime.capture.read_result_pages(second_ticket, None, 128, owned).items
        assert len(first_pages) == len(second_pages) == 1
        assert first_pages[0].page.entries[0].identity == "/DCIM/a.mp4" and not first_pages[0].page.scan_complete
        assert second_pages[0].page.entries[0].identity == "/DCIM/b.mp4" and second_pages[0].page.scan_complete
        assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE id=?", (first_ticket.run_id,)).fetchone() == (2,)
    finally:
        owned.connection.close()


async def test_page_range_forward_reverse_and_snapshot_restore_same_state(tmp_path):
    owned, runtime, action_id, _ = await consumer_world(tmp_path, "record")
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    history = HistoryRepository(path)
    try:
        ticket = _begin_check_round(runtime, action_id).ticket
        first = _request(runtime, ticket, _actual(ticket, entries=[_entry("a")], next_cursor=_CURSOR))
        receipt = runtime.capture.save_result_page(first, new_operation_key(), owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        middle = history.current_boundary()
        executor = DbExecutor(lambda: open_existing(path, DbOpenMode.EXISTING_RW, DbConfig()), capacity=4, enqueue_timeout_seconds=9)
        try:
            store = SqliteSnapshotStore(path, executor)
            image = (await store.load_entity_images((SnapshotRef(1, action_id),)))[0]
            assert image.boundary == middle
            await store.save_snapshots((prepare_snapshot(image),))
        finally:
            await executor.close()
        second = _request(runtime, ticket, _actual(ticket, entries=[_entry("b")], cursor=_CURSOR), page_no=2, cursor=_CURSOR)
        last = runtime.capture.save_result_page(second, new_operation_key(), owned)
        assert last.kind is DbOutcomeKind.COMPLETED, last.error
        end = history.current_boundary()
        reverse = history.restore_entity("action", action_id, middle)
        snapshot = history.restore_entity("action", action_id, end)
        events = []
        for raw in owned.connection.execute("SELECT h.id,h.transaction_id,h.event_type,h.event_version,h.occurred_at,h.clock_status,h.change_seq,h.body_json"
                " FROM history_events h JOIN entity_event_links l ON l.event_id=h.id"
                " WHERE l.entity_type=1 AND l.entity_id=? ORDER BY h.id", (action_id,)):
            event = decode_event_row(raw)
            events.append(ValidatedEvent(event, event_type_name(event.event_type), branch_of(event.event_type, event.reason)[0],
                ((1, action_id),), {(row.table, row.row_id): (1, action_id) for row in event.rows}))
        forward = restore(RestoreSeed(EntityImage(1, action_id, False, {}, 0, 0), INITIAL_BOUNDARY), events, end)
        attempt_id = owned.connection.execute("SELECT id FROM operation_attempts WHERE run_id=?", (ticket.run_id,)).fetchone()[0]
        row_key = ("operation_attempts", attempt_id)
        assert reverse[row_key]["result_last_page_event_id"] == receipt.value.event_id
        assert snapshot[row_key]["result_last_page_event_id"] == last.value.event_id
        assert {key: value for key, value in snapshot[row_key].items() if key != "id"} == forward.rows[row_key]
        pages = runtime.capture.read_result_pages(ticket, None, 128, owned)
        assert [page.ref.event_id for page in pages.items] == [receipt.value.event_id, last.value.event_id]
    finally:
        owned.connection.close()

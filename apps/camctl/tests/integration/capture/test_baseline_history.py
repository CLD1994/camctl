"""基准经公开受理、活动建立及历史事务保存，实际目录读取另测。"""
from dataclasses import replace
import pytest

from camctl.persistence.models import DbOutcomeKind
from camctl.capture import baseline_models as models
from camctl.capture import baseline_saves
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.bindings import DeviceBinding
from camctl.devices import file_identity
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.history import HistoryRepository
from camctl.persistence.models import DbOutcome
from camctl.history.snapshots import SnapshotRef, prepare_snapshot
from camctl.persistence.executor import DbExecutor
from camctl.persistence.repositories.history import SqliteSnapshotStore
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.contracts.history_values import INITIAL_BOUNDARY
from camctl.history.decoding import decode_event_row
from camctl.history.events import branch_of, event_type_name
from camctl.history.replay import EntityImage, RestoreSeed, restore
from camctl.history.validators import ValidatedEvent
from ..acceptance.test_acceptance import _plan_body
from ..acceptance.test_adb_camera_definitions import _catalog, _params
from ..scheduling.test_start_action import _SCHEDULED, _accept_plan, _start, owned

pytestmark = pytest.mark.asyncio


async def _activity(owned, tmp_path, *, request_id="42", device_id="cam-1"):
    body = _plan_body(request_id=request_id)
    body["actions"][0].update(type="camera_timelapse", device_id=device_id, params=_params("dji-action6", "camera_timelapse"))
    await _accept_plan(owned, tmp_path, body, _catalog("dji-action6", device_id=device_id))
    action_id = owned.connection.execute("SELECT MAX(id) FROM actions").fetchone()[0]
    result = _start(owned, action_id, now=_SCHEDULED)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    return result.value.activity_id


async def test_baseline_declaration_initializes_collecting(owned, tmp_path):
    activity_id = await _activity(owned, tmp_path)
    assert owned.connection.execute(
        "SELECT ownership_mode,baseline_state,dispatch_state,occupancy_state"
        " FROM device_activities WHERE id=?", (activity_id,)).fetchone() == (2, 2, 1, 1)
    assert owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (0,)


def _entries(*names, device="cam-1", driver="dji-action6", directory="/mnt/media_rw/emulated/DCIM"):
    return tuple(file_identity.FileIdentity(DeviceBinding(device, driver), f"{directory}/{name}") for name in names)


def _append(owned, activity_id, entries, *, chunk_no=1, key=None):
    request = models.BaselineChunkSave(activity_id, chunk_no, entries, _SCHEDULED)
    return CaptureRepository().append_baseline(request, key or new_operation_key(), owned)


def _fix(owned, activity_id, *, chunks=0, entries=0, key=None):
    request = models.BaselineFixSave(activity_id, chunks, entries, _SCHEDULED)
    return CaptureRepository().fix_baseline(request, key or new_operation_key(), owned)


def _ref(owned, activity_id):
    return CaptureRepository().baseline_ref(activity_id, owned)


async def test_fixed_empty_differs_from_collecting(owned, tmp_path):
    activity_id = await _activity(owned, tmp_path)
    collecting = _ref(owned, activity_id)
    assert collecting.state == enum_for("device_activities.baseline_state").COLLECTING
    with pytest.raises(ConsistencyError):
        CaptureRepository().read_baseline(collecting, None, 1, owned)
    fixed = _fix(owned, activity_id)
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    ref = fixed.value
    assert ref.state == enum_for("device_activities.baseline_state").FIXED
    assert (ref.first_event_id, ref.last_event_id, ref.chunk_count, ref.entry_count) == (None, None, 0, 0)
    assert CaptureRepository().read_baseline(ref, None, 1, owned).items == ()
    assert owned.connection.execute("SELECT COUNT(*) FROM device_files").fetchone() == (0,)


async def test_128_entries_form_one_complete_history_chunk(owned, tmp_path):
    activity_id = await _activity(owned, tmp_path)
    entries = _entries(*(f"{number:03d}.MP4" for number in range(128)))
    saved = _append(owned, activity_id, entries)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    fixed = _fix(owned, activity_id, chunks=1, entries=128)
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    page = CaptureRepository().read_baseline(fixed.value, None, 1, owned)
    assert len(page.items) == 1 and page.items[0].entries == entries
    assert page.items[0].event_id == saved.value.event_id and page.next_cursor is None


async def test_129_entries_are_not_accepted_as_one_chunk(owned, tmp_path):
    activity_id = await _activity(owned, tmp_path)
    with pytest.raises(ValueError):
        models.BaselineChunkSave(activity_id, 1, _entries(*(f"{n:03d}" for n in range(129))), _SCHEDULED)


@pytest.mark.parametrize("entries", [(), ("a", "a"), ("b", "a")])
async def test_empty_duplicate_or_unsorted_chunk_is_rejected(owned, tmp_path, entries):
    activity_id = await _activity(owned, tmp_path)
    with pytest.raises(ValueError):
        models.BaselineChunkSave(activity_id, 1, _entries(*entries), _SCHEDULED)


async def test_chunk_gap_and_cross_chunk_duplicate_are_rejected(owned, tmp_path):
    activity_id = await _activity(owned, tmp_path)
    first = _append(owned, activity_id, _entries("a"))
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    assert _append(owned, activity_id, _entries("b"), chunk_no=3).kind is DbOutcomeKind.ROLLED_BACK
    assert _append(owned, activity_id, _entries("a"), chunk_no=2).kind is DbOutcomeKind.ROLLED_BACK
    assert _append(owned, activity_id, _entries("0"), chunk_no=2).kind is DbOutcomeKind.ROLLED_BACK


@pytest.mark.parametrize("changes", [{"device": "other"}, {"driver": "other"}, {"directory": "/unrelated"},
                                     {"directory": "/mnt/media_rw/emulated/DCIM-other"}])
async def test_identity_from_wrong_binding_or_scope_is_rejected(owned, tmp_path, changes):
    activity_id = await _activity(owned, tmp_path)
    assert _append(owned, activity_id, _entries("a", **changes)).kind is DbOutcomeKind.ROLLED_BACK


async def test_wrong_fixed_counts_do_not_fix_partial_baseline(owned, tmp_path):
    activity_id = await _activity(owned, tmp_path)
    assert _append(owned, activity_id, _entries("a", "b")).kind is DbOutcomeKind.COMPLETED
    assert _fix(owned, activity_id, chunks=2, entries=2).kind is DbOutcomeKind.ROLLED_BACK
    assert _fix(owned, activity_id, chunks=1, entries=1).kind is DbOutcomeKind.ROLLED_BACK
    assert _ref(owned, activity_id).state == enum_for("device_activities.baseline_state").COLLECTING


async def test_recollection_excludes_old_range_and_interleaved_activity(owned, tmp_path):
    activity_id = await _activity(owned, tmp_path)
    old = _append(owned, activity_id, _entries("old"))
    other = await _activity(owned, tmp_path, request_id="43", device_id="cam-2")
    other_saved = _append(owned, other, _entries("other", device="cam-2"))
    first = _append(owned, activity_id, _entries("a"))
    assert all(result.kind is DbOutcomeKind.COMPLETED for result in (old, other_saved, first))
    assert _append(owned, other, _entries("other2", device="cam-2"), chunk_no=2).kind is DbOutcomeKind.COMPLETED
    last = _append(owned, activity_id, _entries("b"), chunk_no=2)
    fixed = _fix(owned, activity_id, chunks=2, entries=2)
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    assert (fixed.value.first_event_id, fixed.value.last_event_id) == (first.value.event_id, last.value.event_id)
    page = CaptureRepository().read_baseline(fixed.value, None, 1, owned)
    assert page.items[0].entries == _entries("a") and page.next_cursor == first.value.event_id
    final = CaptureRepository().read_baseline(fixed.value, page.next_cursor, 1, owned)
    assert final.items[0].entries == _entries("b") and final.next_cursor is None
    with pytest.raises(ConsistencyError):
        CaptureRepository().read_baseline(fixed.value, other_saved.value.event_id, 1, owned)
    with pytest.raises(ConsistencyError):
        CaptureRepository().read_baseline(replace(fixed.value, entry_count=3), None, 1, owned)


async def test_empty_recollection_clears_old_range(owned, tmp_path):
    activity_id = await _activity(owned, tmp_path)
    assert _append(owned, activity_id, _entries("old")).kind is DbOutcomeKind.COMPLETED
    fixed = _fix(owned, activity_id)
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    assert fixed.value.first_event_id is None and fixed.value.entry_count == 0


async def test_fixed_baseline_cannot_be_replaced(owned, tmp_path):
    activity_id = await _activity(owned, tmp_path)
    assert _fix(owned, activity_id).kind is DbOutcomeKind.COMPLETED
    assert _append(owned, activity_id, _entries("a")).kind is DbOutcomeKind.ROLLED_BACK
    assert _fix(owned, activity_id).kind is DbOutcomeKind.ROLLED_BACK


async def test_original_append_key_returns_original_event_and_rejects_changed_input(owned, tmp_path):
    activity_id = await _activity(owned, tmp_path)
    key = new_operation_key()
    first = _append(owned, activity_id, _entries("a"), key=key)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    assert _fix(owned, activity_id, chunks=1, entries=1).kind is DbOutcomeKind.COMPLETED
    replay = _append(owned, activity_id, _entries("a"), key=key)
    assert replay.kind is DbOutcomeKind.COMPLETED, replay.error
    assert replay.value.event_id == first.value.event_id
    assert _append(owned, activity_id, _entries("b"), key=key).kind is DbOutcomeKind.ROLLED_BACK
    request = models.BaselineChunkSave(activity_id, 1, _entries("a"), _SCHEDULED + 1)
    assert CaptureRepository().append_baseline(request, key, owned).kind is DbOutcomeKind.ROLLED_BACK


async def test_original_fix_key_preserves_original_counts_and_reference(owned, tmp_path):
    activity_id = await _activity(owned, tmp_path)
    assert _append(owned, activity_id, _entries("a")).kind is DbOutcomeKind.COMPLETED
    key = new_operation_key()
    first = _fix(owned, activity_id, chunks=1, entries=1, key=key)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    replay = _fix(owned, activity_id, chunks=1, entries=1, key=key)
    assert replay.kind is DbOutcomeKind.COMPLETED and replay.value == first.value
    assert _fix(owned, activity_id, chunks=1, entries=2, key=key).kind is DbOutcomeKind.ROLLED_BACK


async def test_history_forward_reverse_and_snapshot_preserve_range(owned, tmp_path):
    activity_id = await _activity(owned, tmp_path)
    history = HistoryRepository(tmp_path / "state.db")
    before = history.current_boundary()
    assert _append(owned, activity_id, _entries("a")).kind is DbOutcomeKind.COMPLETED
    snapshot_boundary = history.current_boundary()
    executor = DbExecutor(lambda: open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig()),
                          capacity=4, enqueue_timeout_seconds=9)
    try:
        store = SqliteSnapshotStore(tmp_path / "state.db", executor)
        image = (await store.load_entity_images((SnapshotRef(1, 1),)))[0]
        assert image.boundary == snapshot_boundary
        await store.save_snapshots((prepare_snapshot(image),))
    finally:
        await executor.close()
    assert _append(owned, activity_id, _entries("b"), chunk_no=2).kind is DbOutcomeKind.COMPLETED
    collecting = history.current_boundary()
    assert _append(owned, activity_id, _entries("c"), chunk_no=3).kind is DbOutcomeKind.COMPLETED
    assert _append(owned, activity_id, _entries("d"), chunk_no=4).kind is DbOutcomeKind.COMPLETED
    assert _fix(owned, activity_id, chunks=4, entries=4).kind is DbOutcomeKind.COMPLETED
    fixed = history.restore_entity("action", 1, history.current_boundary())[("device_activities", activity_id)]
    original = history.restore_entity("action", 1, before)[("device_activities", activity_id)]
    middle = history.restore_entity("action", 1, collecting)[("device_activities", activity_id)]
    assert original["baseline_state"] == middle["baseline_state"] == 2
    assert original["baseline_first_event_id"] is None
    assert middle["baseline_first_event_id"] == fixed["baseline_first_event_id"]
    assert middle["baseline_last_event_id"] < fixed["baseline_last_event_id"]
    events = []
    for raw in owned.connection.execute(
        "SELECT h.id,h.transaction_id,h.event_type,h.event_version,h.occurred_at,h.clock_status,h.change_seq,h.body_json"
        " FROM history_events h JOIN entity_event_links l ON l.event_id=h.id"
        " WHERE l.entity_type=1 AND l.entity_id=1 AND h.id<=? ORDER BY h.id", (collecting.last_event_id,)):
        event = decode_event_row(raw)
        assert all(row.table in {"actions", "device_activities"} for row in event.rows)
        events.append(ValidatedEvent(event, event_type_name(event.event_type),
            branch_of(event.event_type, event.reason)[0], ((1, 1),),
            {(row.table, row.row_id): (1, 1) for row in event.rows}))
    forward = restore(RestoreSeed(EntityImage(1, 1, False, {}, 0, 0), INITIAL_BOUNDARY), events, collecting)
    assert {key: value for key, value in middle.items() if key != "id"} == forward.rows[("device_activities", activity_id)]
    assert owned.connection.execute("SELECT COUNT(*) FROM entity_snapshots WHERE entity_type=1 AND entity_id=1").fetchone() == (1,)
    assert fixed["baseline_state"] == 3


@pytest.mark.parametrize("kind", ["append", "fix"])
async def test_committed_unknown_receipt_resumes_original_request(owned, tmp_path, kind):
    activity_id = await _activity(owned, tmp_path)
    request = (models.BaselineChunkSave(activity_id, 1, _entries("a"), _SCHEDULED) if kind == "append"
               else models.BaselineFixSave(activity_id, 0, 0, _SCHEDULED))
    calls = []

    class UnknownReceiptRepository(CaptureRepository):
        def _save(self, method, command, key, connection):
            calls.append((command, key))
            receipt = method(command, key, connection)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError("提交回执未取得")) if len(calls) == 1 else receipt

        def append_baseline(self, command, key, connection):
            return self._save(super().append_baseline, command, key, connection)

        def fix_baseline(self, command, key, connection):
            return self._save(super().fix_baseline, command, key, connection)

    owner = baseline_saves.BaselineSaveOwner()
    pending = owner.begin(request)
    repository = UnknownReceiptRepository()
    receipt = owner.save(repository, owned)
    assert receipt.kind is DbOutcomeKind.UNKNOWN and owner.pending == pending
    with pytest.raises(ConsistencyError):
        owner.take()
    with pytest.raises(ConsistencyError):
        owner.begin(replace(request, occurred_at=_SCHEDULED + 1))
    assert owner.save(repository, owned).kind is DbOutcomeKind.COMPLETED
    result = owner.take()
    assert owner.pending is None and result.activity_id == activity_id
    assert calls[0] == calls[1] and calls[0][0] is request
    assert owned.connection.execute("SELECT COUNT(*) FROM history_events WHERE event_type=14").fetchone() == (1,)

"""结果页与固定成员通过正式仓储保持真实事务和分页边界。"""
from dataclasses import replace
from pathlib import Path

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.persistence.repositories import result_pages
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import commit_operation

from .result_page_fixtures import begin_result_check
from ..capture.result_consumer_fixtures import consumer_world
from ..capture.test_result_page_history import _actual, _entry, _request


async def _page(tmp_path):
    owned, runtime, action_id, _ = await consumer_world(tmp_path, "record")
    ticket = begin_result_check(owned, runtime, action_id).ticket
    request = _request(runtime, ticket, _actual(ticket, entries=[_entry("a")], finalized=True))
    receipt = runtime.capture.save_result_page(request, new_operation_key(), owned)
    assert receipt.kind.value == "completed", receipt.error
    return owned, receipt.value


@pytest.mark.asyncio
async def test_later_read_transaction_rejects_changed_page_relations(tmp_path):
    owned, ref = await _page(tmp_path)
    db = owned.connection
    try:
        db.execute("BEGIN")
        try:
            first = result_pages.read_page(ref, owned)
            assert result_pages.read_page(ref, owned) == first
        finally:
            db.execute("ROLLBACK")
        db.execute("UPDATE result_page_files SET identity=? WHERE page_event_id=?", ("changed", ref.event_id))
        db.execute("BEGIN")
        try:
            with pytest.raises(ConsistencyError):
                result_pages.read_page(ref, owned)
        finally:
            db.execute("ROLLBACK")
        assert not db.in_transaction
    finally:
        db.close()


@pytest.mark.asyncio
async def test_another_connection_cannot_hide_changed_page_relations(tmp_path):
    owned, ref = await _page(tmp_path)
    db = owned.connection
    path = db.execute("PRAGMA database_list").fetchone()[2]
    other = open_existing(Path(path), DbOpenMode.EXISTING_RW, DbConfig())
    try:
        db.execute("BEGIN")
        try:
            first = result_pages.read_page(ref, owned)
            other.connection.execute("UPDATE result_page_files SET identity=? WHERE page_event_id=?", ("changed", ref.event_id))
            with pytest.raises(ConsistencyError):
                result_pages.read_page(ref, other)
            assert result_pages.read_page(ref, owned) == first
        finally:
            db.execute("ROLLBACK")
        with pytest.raises(ConsistencyError):
            result_pages.read_page(ref, owned)
    finally:
        other.connection.close()
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["reference", "attempt_range"])
async def test_page_reads_check_current_reference_and_attempt_range(tmp_path, damage):
    owned, ref = await _page(tmp_path)
    db = owned.connection
    try:
        db.execute("BEGIN IMMEDIATE")
        try:
            assert result_pages.read_page(ref, owned).ref == ref
            changed = ref
            if damage == "reference":
                changed = replace(ref, page_no=ref.page_no + 1)
            else:
                db.execute("UPDATE operation_attempts SET result_first_page_event_id=NULL,result_last_page_event_id=NULL WHERE run_id=?", (ref.ticket.run_id,))
            with pytest.raises(ConsistencyError):
                result_pages.read_page(changed, owned)
        finally:
            db.execute("ROLLBACK")
        assert result_pages.read_page(ref, owned).ref == ref
    finally:
        db.close()


@pytest.mark.asyncio
async def test_uncommitted_page_never_gains_committed_read_qualification(tmp_path):
    owned, runtime, action_id, _ = await consumer_world(tmp_path, "record")
    ticket = begin_result_check(owned, runtime, action_id).ticket
    request = _request(runtime, ticket, _actual(ticket, entries=[_entry("new")], finalized=True))
    key = new_operation_key()
    observed = []

    class DamagedPageCompletion:
        def plan(self, scope):
            plan = result_pages.SaveResultPageCommand(request, key).plan(scope)

            def complete(db, ref):
                with pytest.raises(ConsistencyError):
                    result_pages.read_page(ref, owned)
                observed.append(ref)
                db.execute("DELETE FROM result_page_files WHERE page_event_id=?", (ref.event_id,))
                return result_pages.read_page(ref, owned)

            return replace(plan, complete_result=complete)

    try:
        before = owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()
        receipt = commit_operation(DamagedPageCompletion(), key, owned)
        assert receipt.kind == "rolled_back"
        assert isinstance(receipt.error, ConsistencyError)
        assert len(observed) == 1
        assert observed[0].ticket == ticket
        assert owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone() == before
        assert owned.connection.execute("SELECT COUNT(*) FROM result_page_files").fetchone() == (0,)
        assert not owned.connection.in_transaction
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_three_saved_pages_keep_unknown_metrics_across_member_batches(tmp_path):
    from camctl.capture.files import OwnershipFromResultPageSave
    from camctl.capture.segment_models import (
        FileCompletionState, OwnershipState, PreviewPairingState, SegmentMemberRole,
        SegmentSetRef,
    )
    from camctl.capture.segment_storage import SegmentSetCreate
    from camctl.devices.bindings import DeviceBinding
    from camctl.devices.directory import DirectoryCursor
    from camctl.devices.segments import SegmentKey
    from camctl.persistence.repositories import segments
    from .test_segment_file_pairing import _entry as segment_entry, _ok
    from .test_segment_file_proofs import _boundary
    from .test_segment_storage import SegmentedCatalog

    owned, runtime, action_id, _ = await consumer_world(tmp_path, "record", catalog=SegmentedCatalog())
    db = owned.connection
    try:
        ticket = begin_result_check(owned, runtime, action_id).ticket
        action = await runtime.action(action_id)
        binding = DeviceBinding(action["device_id"], action["driver_id"])
        entries = [segment_entry(
            f"CAM_001/CAM_20261021090000_{number:04d}_D.OSV",
            complete=False, size=None) for number in range(1, 7)]
        pages = []
        cursor = None
        for offset in range(0, len(entries), 2):
            chunk = entries[offset:offset + 2]
            following = (DirectoryCursor(binding, ("/DCIM",), 0, chunk[-1]["identity"])
                         if offset + 2 < len(entries) else None)
            ref = _ok(runtime.capture.save_result_page(_request(
                runtime, ticket, _actual(ticket, entries=chunk, cursor=cursor,
                    next_cursor=following, finalized=following is None),
                page_no=offset // 2 + 1, cursor=cursor), new_operation_key(), owned))
            pages.append(runtime.capture.read_result_page(ref, owned))
            cursor = following
        assert [len(page.file_ids) for page in pages] == [2, 2, 2]
        assert len({page.ref.event_id for page in pages}) == 3
        for page in pages:
            for _, file_id in page.file_ids:
                _ok(runtime.capture.save_ownership_from_result_page(
                    OwnershipFromResultPageSave(page.ref, file_id, runtime.wall_us()),
                    new_operation_key(), owned))
        boundary = _boundary(owned)
        inputs = tuple(runtime.capture.read_saved_result_file_input(
            page.ref, file_id, boundary, owned)
            for page in pages for _, file_id in page.file_ids)
        assert [item.page_event_id for item in inputs] == [page.ref.event_id for page in pages for _ in page.file_ids]
        assert all(item.ownership is OwnershipState.OWNED
                   and item.completion is FileCompletionState.UNKNOWN for item in inputs)
        (attempt_id,) = db.execute(
            "SELECT id FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone()
        ref = SegmentSetRef(action_id, attempt_id, pages[-1].ref.event_id, 1)
        repo = segments.SegmentRepository()
        _ok(repo.create(SegmentSetCreate(ref, int(ticket.target_id), ticket.run_id,
            action["execution_spec_json"]["target_duration_ms"], runtime.wall_us()),
            new_operation_key(), owned))
        _ok(repo.append_members(ref, inputs, runtime.wall_us(), new_operation_key(), owned))
        _ok(repo.fix_members(ref, runtime.wall_us(), new_operation_key(), owned))
        db.execute("BEGIN")
        try:
            prefix = repo.read_members(ref, None, 4, owned)
            assert len(prefix.items) == 4 and prefix.next_cursor is not None
            tail = repo.read_members(ref, prefix.next_cursor, 2, owned)
            assert tail == repo.read_members(ref, prefix.next_cursor, 2, owned)
            assert len(tail.items) == 2 and tail.next_cursor is None
            stored_members = prefix.items + tail.items
            assert [item.file_id for item in stored_members] == [file_id for page in pages for _, file_id in page.file_ids]
            for number, stored in enumerate(stored_members, 1):
                member = stored.member
                assert stored.page_event_id == pages[(number - 1) // 2].ref.event_id
                assert member.identity == entries[number - 1]["identity"]
                assert member.key == SegmentKey(1, number)
                assert member.role is SegmentMemberRole.ORIGINAL
                assert member.ownership is OwnershipState.OWNED
                assert member.completion is FileCompletionState.UNKNOWN
                assert member.size_bytes is None
                assert member.pairing_state is PreviewPairingState.NOT_APPLICABLE
        finally:
            db.execute("ROLLBACK")
    finally:
        db.close()

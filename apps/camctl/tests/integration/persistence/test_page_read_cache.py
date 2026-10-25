"""页证明复用不越过真实事务、当前资格和候选写入边界。"""
from dataclasses import replace
from pathlib import Path

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.persistence.page_read_cache import opened_read_page_scope, page_read_scope
from camctl.persistence.read_reuse import read_reuse_scope
from camctl.persistence.repositories import result_pages
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from ..capture.result_consumer_fixtures import consumer_world
from ..capture.test_result_page_history import _actual, _entry, _request
from camctl.capture.handlers import _begin_check_round


async def _page(tmp_path):
    owned, runtime, action_id, _ = await consumer_world(tmp_path, 'record')
    ticket = _begin_check_round(runtime, action_id).ticket
    request = _request(runtime, ticket, _actual(ticket, entries=[_entry('a')], finalized=True))
    receipt = runtime.capture.save_result_page(request, new_operation_key(), owned)
    assert receipt.kind.value == 'completed', receipt.error
    return owned, receipt.value


@pytest.mark.asyncio
async def test_committed_page_is_reused_only_inside_one_read_transaction(tmp_path, monkeypatch):
    owned, ref = await _page(tmp_path)
    import weakref
    calls = []
    original = result_pages._verify_page_relations
    def verify(*args):
        calls.append(args[1].ref.event_id)
        return original(*args)
    monkeypatch.setattr(result_pages, '_verify_page_relations', verify)
    db = owned.connection
    try:
        db.execute('BEGIN')
        try:
            with read_reuse_scope(db), opened_read_page_scope(db, True):
                saved = result_pages.read_page(ref, owned)
                assert saved.ref == ref
                released = weakref.ref(saved)
                del saved
                assert released() is None
                assert result_pages.read_page(ref, owned).ref == ref
        finally:
            db.execute('ROLLBACK')
        assert calls == [ref.event_id]
        db.execute('UPDATE result_page_files SET identity=? WHERE page_event_id=?', ('changed', ref.event_id))
        db.execute('BEGIN')
        try:
            with opened_read_page_scope(db, True), pytest.raises(ConsistencyError):
                result_pages.read_page(ref, owned)
        finally:
            db.execute('ROLLBACK')
        assert calls == [ref.event_id, ref.event_id]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_page_record_from_another_connection_cannot_hide_changed_relations(tmp_path):
    owned, ref = await _page(tmp_path)
    db = owned.connection
    path = db.execute('PRAGMA database_list').fetchone()[2]
    other = open_existing(Path(path), DbOpenMode.EXISTING_RW, DbConfig())
    try:
        db.execute('BEGIN')
        try:
            with read_reuse_scope(db), opened_read_page_scope(db, True):
                result_pages.read_page(ref, owned)
                other.connection.execute('UPDATE result_page_files SET identity=? WHERE page_event_id=?', ('changed', ref.event_id))
                with pytest.raises(ConsistencyError):
                    result_pages.read_page(ref, other)
                # 原连接的同一快照仍只看到原关系。
                assert result_pages.read_page(ref, owned).ref == ref
        finally:
            db.execute('ROLLBACK')
    finally:
        other.connection.close()
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['reference', 'attempt_range'])
async def test_reused_page_keeps_current_reference_and_attempt_range_checks(tmp_path, damage):
    owned, ref = await _page(tmp_path)
    db = owned.connection
    try:
        db.execute('BEGIN IMMEDIATE')
        try:
            upper = db.execute('SELECT MAX(id) FROM history_events').fetchone()[0]
            with read_reuse_scope(db), page_read_scope(db, upper):
                result_pages.read_page(ref, owned)
                if damage == 'reference':
                    changed = replace(ref, page_no=ref.page_no + 1)
                else:
                    changed = ref
                    db.execute('UPDATE operation_attempts SET result_first_page_event_id=NULL,result_last_page_event_id=NULL WHERE run_id=?', (ref.ticket.run_id,))
                with pytest.raises(ConsistencyError):
                    result_pages.read_page(changed, owned)
        finally:
            db.execute('ROLLBACK')
    finally:
        db.close()


@pytest.mark.asyncio
async def test_new_page_is_rechecked_until_its_complete_transaction_commits(tmp_path, monkeypatch):
    owned, runtime, action_id, _ = await consumer_world(tmp_path, 'record')
    original = result_pages.SaveResultPageCommand.plan
    reads = []
    verify = result_pages._verify_page_relations
    def track(*args):
        reads.append(args[1].ref.event_id)
        return verify(*args)
    monkeypatch.setattr(result_pages, '_verify_page_relations', track)
    def plan(command, scope):
        value = original(command, scope)
        def complete(db, ref):
            result_pages.read_page(ref, owned)
            db.execute('DELETE FROM result_page_files WHERE page_event_id=?', (ref.event_id,))
            return result_pages.read_page(ref, owned)
        return replace(value, complete_result=complete)
    monkeypatch.setattr(result_pages.SaveResultPageCommand, 'plan', plan)
    try:
        ticket = _begin_check_round(runtime, action_id).ticket
        before = owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()
        request = _request(runtime, ticket, _actual(ticket, entries=[_entry('new')], finalized=True))
        receipt = runtime.capture.save_result_page(request, new_operation_key(), owned)
        assert receipt.kind.value == 'rolled_back'
        assert isinstance(receipt.error, ConsistencyError)
        assert len(reads) == 2 and reads[0] == reads[1]
        assert owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone() == before
        assert not owned.connection.in_transaction
    finally:
        owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('end', ['commit', 'rollback', 'unknown_commit', 'cancel'])
async def test_write_transaction_releases_page_proof_before_every_end(tmp_path, monkeypatch, end):
    import asyncio
    import sqlite3
    from camctl.capture.files import FilePresenceSave
    from camctl.persistence.page_read_cache import cached_page_record
    from camctl.persistence.repositories import capture

    owned, ref = await _page(tmp_path)
    original = capture._FilePresenceCommand.plan
    ended = []
    class Connection:
        def __getattr__(self, name):
            return getattr(owned.connection, name)
        def execute(self, sql, parameters=()):
            if sql in ('COMMIT', 'ROLLBACK'):
                ended.append((sql, cached_page_record(self, ref.event_id)))
            result = owned.connection.execute(sql, parameters)
            if sql == 'COMMIT' and end == 'unknown_commit':
                raise sqlite3.OperationalError('controlled lost commit receipt')
            return result
    connection = Connection()
    def plan(command, scope):
        result_pages._at_record(scope.connection, ref.event_id)
        assert cached_page_record(scope.connection, ref.event_id) is not None
        if end == 'cancel':
            raise asyncio.CancelledError()
        if end == 'rollback':
            raise ConsistencyError('controlled rejected operation')
        return original(command, scope)
    monkeypatch.setattr(capture._FilePresenceCommand, 'plan', plan)
    try:
        file_id = owned.connection.execute('SELECT id FROM device_files').fetchone()[0]
        request = FilePresenceSave(file_id, 2, 1_750_000_000_000_000)
        observed = replace(owned, connection=connection)
        if end == 'cancel':
            with pytest.raises(asyncio.CancelledError):
                capture.CaptureRepository().save_file_presence(request, new_operation_key(), observed)
        else:
            receipt = capture.CaptureRepository().save_file_presence(request, new_operation_key(), observed)
            assert receipt.kind.value == {'commit': 'completed', 'rollback': 'rolled_back', 'unknown_commit': 'unknown'}[end]
        assert ended == [('ROLLBACK' if end in ('rollback', 'cancel') else 'COMMIT', None)]
        assert cached_page_record(connection, ref.event_id) is None
        assert not owned.connection.in_transaction
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_three_page_strict_members_reuse_each_committed_page_within_scope(tmp_path, monkeypatch):
    from collections import Counter
    from camctl.capture.files import OwnershipFromResultPageSave
    from camctl.capture.segment_models import (
        FileCompletionState, OwnershipState, PreviewPairingState, SegmentMemberRole,
        SegmentSetRef,
    )
    from camctl.capture.segment_storage import SegmentSetCreate
    from camctl.contracts.enums import enum_for
    from camctl.devices.bindings import DeviceBinding
    from camctl.devices.directory import DirectoryCursor
    from camctl.devices.segments import SegmentKey
    from camctl.persistence.repositories import segments
    from camctl.persistence.transaction import row_facts
    from .test_segment_file_pairing import _entry as segment_entry, _ok
    from .test_segment_file_proofs import _boundary
    from .test_segment_storage import SegmentedCatalog

    owned, runtime, action_id, _ = await consumer_world(
        tmp_path, 'record', catalog=SegmentedCatalog())
    db = owned.connection
    try:
        ticket = _begin_check_round(runtime, action_id).ticket
        action = runtime.action(action_id)
        binding = DeviceBinding(action['device_id'], action['driver_id'])
        entries = [segment_entry(
            f'CAM_001/CAM_20261021090000_{number:04d}_D.OSV',
            complete=False, size=None) for number in range(1, 7)]
        pages = []
        cursor = None
        for offset in range(0, len(entries), 2):
            chunk = entries[offset:offset + 2]
            following = (DirectoryCursor(binding, ('/DCIM',), 0, chunk[-1]['identity'])
                         if offset + 2 < len(entries) else None)
            ref = _ok(runtime.capture.save_result_page(_request(
                runtime, ticket, _actual(ticket, entries=chunk, cursor=cursor,
                    next_cursor=following, finalized=following is None),
                page_no=offset // 2 + 1, cursor=cursor), new_operation_key(), owned))
            pages.append(runtime.capture.read_result_page(ref, owned))
            cursor = following
        assert [len(page.file_ids) for page in pages] == [2, 2, 2]
        method = int(enum_for(
            'device_files.ownership_evidence_json.method').RESULT_PAGE_DIFFERENCE)
        for page in pages:
            for _, file_id in page.file_ids:
                _ok(runtime.capture.save_ownership_from_result_page(
                    OwnershipFromResultPageSave(page.ref, file_id, runtime.wall_us()),
                    new_operation_key(), owned))
                facts = row_facts(db, 'device_files', file_id)
                assert facts['ownership_evidence_json'] == {
                    'method': method, 'result_page_event_id': page.ref.event_id}
        boundary = _boundary(owned)
        inputs = tuple(runtime.capture.read_saved_result_file_input(
            page.ref, file_id, boundary, owned)
            for page in pages for _, file_id in page.file_ids)
        assert all(segments._uses_strict_proof(
            item, row_facts(db, 'device_files', item.file_id)) for item in inputs)
        (attempt_id,) = db.execute(
            'SELECT id FROM operation_attempts WHERE run_id=? AND attempt_no=?',
            (ticket.run_id, ticket.attempt_id)).fetchone()
        ref = SegmentSetRef(action_id, attempt_id, pages[-1].ref.event_id, 1)
        repo = segments.SegmentRepository()
        _ok(repo.create(SegmentSetCreate(ref, int(ticket.target_id), ticket.run_id,
            action['execution_spec_json']['target_duration_ms'], runtime.wall_us()),
            new_operation_key(), owned))
        _ok(repo.append_members(ref, inputs, runtime.wall_us(), new_operation_key(), owned))
        _ok(repo.fix_members(ref, runtime.wall_us(), new_operation_key(), owned))
        prefix = repo.read_members(ref, None, 4, owned)
        assert len(prefix.items) == 4 and prefix.next_cursor is not None

        # 建档守卫已各自核验；这里只统计同一一致读范围内的严格成员消费。
        proved = Counter()
        original = result_pages._verify_page_relations
        def verify(*args):
            proved[args[1].ref.event_id] += 1
            return original(*args)
        monkeypatch.setattr(result_pages, '_verify_page_relations', verify)
        db.execute('BEGIN')
        try:
            with read_reuse_scope(db), opened_read_page_scope(db, True):
                first = repo.read_members(ref, prefix.next_cursor, 2, owned)
                repeated = repo.read_members(ref, prefix.next_cursor, 2, owned)
                assert first.items == repeated.items
                assert first.next_cursor is None and repeated.next_cursor is None
                assert [item.file_id for item in first.items] == [
                    file_id for _, file_id in pages[-1].file_ids]
                for number, stored in enumerate(first.items, 5):
                    member = stored.member
                    assert stored.page_event_id == pages[-1].ref.event_id
                    assert member.identity == entries[number - 1]['identity']
                    assert member.key == SegmentKey(1, number)
                    assert member.role is SegmentMemberRole.ORIGINAL
                    assert member.ownership is OwnershipState.OWNED
                    assert member.completion is FileCompletionState.UNKNOWN
                    assert member.size_bytes is None
                    assert member.pairing_state is PreviewPairingState.NOT_APPLICABLE
                assert proved == Counter({page.ref.event_id: 1 for page in pages})
        finally:
            db.execute('ROLLBACK')
    finally:
        db.close()

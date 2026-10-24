"""页证明复用不越过真实事务、当前资格和候选写入边界。"""
from dataclasses import replace
from pathlib import Path

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.persistence.page_read_cache import opened_read_page_scope, page_read_scope
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
            with opened_read_page_scope(db, True):
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
            with opened_read_page_scope(db, True):
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
            with page_read_scope(db, upper):
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

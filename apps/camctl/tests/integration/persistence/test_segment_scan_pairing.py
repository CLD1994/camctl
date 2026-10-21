"""完整原页扫描提供事后配对，原始观察与逐文件身份保持不变。"""
import pytest
from copy import deepcopy
from dataclasses import replace
import json
import sqlite3
from pathlib import Path

from camctl.capture import files
from camctl.capture.files import FileCompletionSave, OwnershipFromResultPageSave
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.transaction import commit_operation
from camctl.persistence.repositories.result_file_pairing import PairResultFilesCommand

from .test_segment_file_pairing import _bounded, _entry, _ok, _snapshot
from .test_segment_file_proofs import _boundary, _proof_world


async def _ready(tmp_path, entries, *, unfinished=(), next_cursor=None):
    owned, runtime, source, saved, preview, context = await _proof_world(
        tmp_path, entries=entries, next_cursor=next_cursor)
    ids = dict(saved.file_ids)
    try:
        for entry in saved.page.entries:
            fid = ids[entry.identity]
            _ok(runtime.capture.save_ownership_from_result_page(
                OwnershipFromResultPageSave(saved.ref, fid, runtime.wall_us()),
                new_operation_key(), owned))
            if entry.identity in unfinished or not entry.complete or entry.size_bytes is None:
                continue
            _ok(runtime.capture.save_file_completion(FileCompletionSave(
                fid, 3, runtime.wall_us(), basis=3, observation=entry.evidence,
                size_bytes=entry.size_bytes, activity_id=int(context['activity_id']),
                result_page_event_id=saved.ref.event_id,
                stop_result_event_id=context['stop_result_event_id']), new_operation_key(), owned))
        return owned, runtime, saved, ids, context
    except BaseException:
        owned.connection.close()
        raise


def _request(owned, runtime, saved, preview):
    return files.PairResultFilesSave(preview, saved.ref, _boundary(owned), runtime.wall_us())


@pytest.mark.asyncio
@pytest.mark.parametrize('names,reason,counts', [
    (['raw.LRF'], 'MISSING', (0, 1)),
    (['raw.LRF', 'raw.OSV', 'raw.osv'], 'AMBIGUOUS', (2, 1)),
    (['raw.LRF', 'raw.lrf', 'raw.OSV'], 'AMBIGUOUS', (1, 2)),
    (['raw.LRF', 'raw.OSV'], 'INVALID', (1, 1)),
    (['完整主干.lrf', '完整主干.oSv'], 'UNIQUE', (1, 1)),
])
async def test_complete_reliable_family_has_explicit_classification(tmp_path, names, reason, counts):
    entries = [_entry(name, size=0 if reason == 'INVALID' else 10) for name in names]
    owned, runtime, saved, ids, _ = await _ready(tmp_path, entries)
    try:
        preview = ids['/DCIM/' + names[0]]
        fact = _ok(runtime.capture.save_result_file_pairing(
            _request(owned, runtime, saved, preview), new_operation_key(), owned))
        assert owned.connection.execute(
            'SELECT original_count,preview_count,reason FROM device_file_pairing_facts WHERE id=?',
            (fact,)).fetchone() == (*counts, int(enum_for('device_file_pairing_facts.reason')[reason]))
        original = owned.connection.execute(
            'SELECT original_device_file_id FROM device_files WHERE id=?', (preview,)).fetchone()[0]
        assert (original is not None) == (reason == 'UNIQUE')
    finally:
        owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['metadata', 'writing', 'completion', 'ambiguous_unknown', 'partial'])
async def test_unknown_family_never_allocates_fact_event_or_key(tmp_path, case):
    from ..capture.test_result_page_history import _CURSOR
    entries = [_entry('raw.LRF'), _entry('raw.OSV',
        complete=case != 'writing', size=None if case == 'metadata' else 10)]
    if case == 'ambiguous_unknown':
        entries.append(_entry('raw.osv', complete=False, size=None))
    owned, runtime, saved, ids, _ = await _ready(tmp_path, entries,
        unfinished=('/DCIM/raw.OSV',) if case == 'completion' else (),
        next_cursor=_CURSOR if case == 'partial' else None)
    try:
        key = new_operation_key()
        request = _request(owned, runtime, saved, ids['/DCIM/raw.LRF'])
        before = _snapshot(owned)
        assert _ok(runtime.capture.save_result_file_pairing(request, key, owned)) is None
        assert _snapshot(owned) == before
        assert owned.connection.execute('SELECT COUNT(*) FROM device_file_pairing_facts').fetchone() == (0,)
        assert runtime.capture.verify_result_file_pairing(request, key, owned) is None
        assert _snapshot(owned) == before
    finally:
        owned.connection.close()


class _AlteredPair:
    def __init__(self, request, key, bounded, damage=None):
        self.request, self.key, self.bounded, self.damage = request, key, bounded, damage

    def plan(self, scope):
        plan = PairResultFilesCommand(self.request, self.key).plan(scope)
        if self.damage:
            event = plan.events[0]
            row = event.rows[0]
            after = dict(row.after.values)
            if self.damage == 'reason':
                after['reason'] = 3
                after['state'] = 2
            elif self.damage == 'count': after['original_count'] = 2
            elif self.damage == 'source': after['source_event_id'] -= 1
            elif self.damage == 'page': after['original_page_event_id'] = 1
            elif self.damage == 'file_event': after['preview_file_event_id'] = 1
            elif self.damage == 'endpoint': after['original_file_id'] = after['preview_file_id']
            if self.damage == 'owner':
                plan = replace(plan, owners={key: ('action', self.request.scan.ticket.run_id)
                                             for key in plan.owners})
            else:
                row = replace(row, after=replace(row.after, values=after))
                event = replace(event, rows=(row, *event.rows[1:]))
                plan = replace(plan, events=(event,))
        return _bounded(plan, scope.connection) if self.bounded else plan


@pytest.mark.asyncio
@pytest.mark.parametrize('bounded', [False, True])
@pytest.mark.parametrize('damage', [None, 'reason', 'count', 'source', 'page', 'file_event', 'endpoint', 'owner'])
async def test_real_transaction_guard_derives_all_pair_facts(tmp_path, bounded, damage):
    owned, runtime, saved, ids, _ = await _ready(tmp_path, [_entry('raw.LRF'), _entry('raw.OSV')])
    try:
        request = _request(owned, runtime, saved, ids['/DCIM/raw.LRF'])
        before = _snapshot(owned)
        key = new_operation_key()
        receipt = commit_operation(_AlteredPair(request, key, bounded, damage), key, owned)
        if damage:
            assert receipt.kind == 'rolled_back', receipt.error
            assert _snapshot(owned) == before
            assert owned.connection.execute('SELECT COUNT(*) FROM device_file_pairing_facts').fetchone() == (0,)
        else:
            assert receipt.kind == 'completed', receipt.error
            assert runtime.capture.verify_result_file_pairing(request, key, owned) == receipt.result
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_original_key_is_bound_to_request_time_boundary_and_scan(tmp_path):
    owned, runtime, saved, ids, _ = await _ready(tmp_path, [_entry('raw.LRF'), _entry('raw.OSV')])
    try:
        from camctl.contracts.history_values import HistoryBoundary
        request = _request(owned, runtime, saved, ids['/DCIM/raw.LRF'])
        key = new_operation_key()
        fact = _ok(runtime.capture.save_result_file_pairing(request, key, owned))
        assert runtime.capture.verify_result_file_pairing(request, key, owned) == fact
        before = _snapshot(owned)
        for changed in (replace(request, occurred_at=request.occurred_at + 1),
                replace(request, source_boundary=_boundary(owned)),
                replace(request, scan=replace(request.scan, page_no=2))):
            assert runtime.capture.save_result_file_pairing(changed, key, owned).kind is DbOutcomeKind.ROLLED_BACK
        assert _snapshot(owned) == before
        with pytest.raises(ConsistencyError):
            runtime.capture.verify_result_file_pairing(request, key, owned, execution_finished=False)
        class FailedRead:
            connection = sqlite3.connect(':memory:')
        try:
            with pytest.raises(sqlite3.OperationalError):
                runtime.capture.verify_result_file_pairing(request, key, FailedRead())
        finally:
            FailedRead.connection.close()
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_pair_cross_page_uses_original_preview_and_final_scan(tmp_path):
    from camctl.capture.result_pages import ResultPageSave
    from camctl.operations.models import EvidenceValue, Settlement, SettlementBasis
    from camctl.operations.validation import validate_outcome
    from ..capture.test_record_file_completion_wait import _REGISTRY, _completion
    from ..capture.test_result_page_history import _actual, _BINDING
    from camctl.devices.directory import DirectoryCursor
    cursor = DirectoryCursor(_BINDING, ('/DCIM',), 0, '/DCIM/raw.LRF')
    owned, runtime, first, ids, context = await _ready(
        tmp_path, [_entry('raw.LRF')], next_cursor=cursor)
    try:
        partial = _request(owned, runtime, first, ids['/DCIM/raw.LRF'])
        assert _ok(runtime.capture.save_result_file_pairing(partial, new_operation_key(), owned)) is None
        ticket = first.ref.ticket
        actual = replace(_actual(ticket, entries=[_entry('raw.OSV')], cursor=cursor, finalized=True),
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue('results_returned', 1,
                {'file_completion': _completion(context), 'file_completion_source_page_event_id': None})))
        final = _ok(runtime.capture.save_result_page(ResultPageSave(ticket, 2, cursor,
            validate_outcome(ticket, actual, _REGISTRY), runtime.wall_us()), new_operation_key(), owned))
        last = runtime.capture.read_result_page(final, owned)
        original = last.file_ids[0][1]
        _ok(runtime.capture.save_ownership_from_result_page(
            OwnershipFromResultPageSave(final, original, runtime.wall_us()), new_operation_key(), owned))
        _ok(runtime.capture.save_file_completion(FileCompletionSave(original, 3, runtime.wall_us(),
            basis=3, observation=last.page.entries[0].evidence, size_bytes=10,
            activity_id=int(context['activity_id']), result_page_event_id=final.event_id,
            stop_result_event_id=context['stop_result_event_id']), new_operation_key(), owned))
        request = _request(owned, runtime, last, ids['/DCIM/raw.LRF'])
        fact = _ok(runtime.capture.save_result_file_pairing(request, new_operation_key(), owned))
        assert owned.connection.execute('SELECT preview_page_event_id,original_page_event_id,'
            'final_page_event_id,original_count,preview_count FROM device_file_pairing_facts WHERE id=?',
            (fact,)).fetchone() == (first.ref.event_id, final.event_id, final.event_id, 1, 1)
    finally:
        owned.connection.close()


class _PairCommitFault:
    def __init__(self, real, after):
        self.real, self.after, self.armed, self.started = real, after, True, False

    def __getattr__(self, name):
        return getattr(self.real, name)

    def execute(self, sql, parameters=()):
        if sql.startswith('INSERT INTO device_file_pairing_facts'):
            self.started = True
        if sql == 'COMMIT' and self.started and self.armed:
            self.armed = False
            if self.after:
                self.real.execute(sql, parameters)
            raise sqlite3.OperationalError('配对提交回执丢失')
        return self.real.execute(sql, parameters)


@pytest.mark.asyncio
@pytest.mark.parametrize('after', [False, True])
async def test_unknown_commit_fresh_verifies_original_pair_responsibility(tmp_path, after):
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    owned, runtime, saved, ids, _ = await _ready(tmp_path, [_entry('raw.LRF'), _entry('raw.OSV')])
    try:
        request = _request(owned, runtime, saved, ids['/DCIM/raw.LRF'])
        key = new_operation_key()
        path = Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
        proxy = _PairCommitFault(owned.connection, after)
        receipt = runtime.capture.save_result_file_pairing(request, key, replace(owned, connection=proxy))
        assert receipt.kind is DbOutcomeKind.UNKNOWN, receipt.error
        assert not proxy.armed
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        value = runtime.capture.verify_result_file_pairing(request, key, owned)
        assert (value is not None) == after
        fact = _ok(runtime.capture.save_result_file_pairing(request, key, owned))
        assert runtime.capture.verify_result_file_pairing(request, key, owned) == fact
        assert owned.connection.execute('SELECT COUNT(*) FROM device_file_pairing_facts').fetchone() == (1,)
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_pair_fact_belongs_to_preview_history_and_snapshot(tmp_path):
    from camctl.persistence.repositories.history import HistoryRepository, SqliteSnapshotStore
    from camctl.persistence.executor import DbExecutor
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    from camctl.contracts.enums import load_registry
    from camctl.history.snapshots import SnapshotRef, prepare_snapshot
    owned, runtime, saved, ids, _ = await _ready(tmp_path, [_entry('raw.LRF'), _entry('raw.OSV')])
    executor = None
    try:
        path = Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
        history = HistoryRepository(path)
        preview = ids['/DCIM/raw.LRF']
        before = history.current_boundary()
        request = _request(owned, runtime, saved, preview)
        key = new_operation_key()
        fact = _ok(runtime.capture.save_result_file_pairing(request, key, owned))
        boundary = history.current_boundary()
        current = history.restore_entity('device_file', preview, boundary)
        assert ('device_file_pairing_facts', fact) in current
        assert ('device_file_pairing_facts', fact) not in history.restore_entity('device_file', preview, before)
        entity_type = load_registry()['history_objects']['device_file']['id']
        executor = DbExecutor(lambda: open_existing(path, DbOpenMode.EXISTING_RW, DbConfig()),
                              capacity=4, enqueue_timeout_seconds=9)
        store = SqliteSnapshotStore(path, executor)
        source = owned.connection.execute('SELECT source_action_id FROM device_files WHERE id=?',
                                         (preview,)).fetchone()[0]
        action_type = load_registry()['history_objects']['action']['id']
        images = await store.load_entity_images((SnapshotRef(entity_type, preview),
                                                SnapshotRef(action_type, source)))
        await store.save_snapshots(tuple(prepare_snapshot(image) for image in images))
        _ok(runtime.capture.save_file_presence(files.FilePresenceSave(preview, 2, runtime.wall_us()),
                                              new_operation_key(), owned))
        assert history.restore_entity('device_file', preview, boundary) == current
        assert runtime.capture.verify_result_file_pairing(request, key, owned) == fact
        source = owned.connection.execute('SELECT source_action_id FROM device_files WHERE id=?',
                                         (preview,)).fetchone()[0]
        for strategy in ('initial', 'current', 'snapshot'):
            with history.open_segment_history(source, boundary, strategy=strategy, batch_size=1) as handle:
                assert not handle.read_page('device_file_pairing_facts', limit=1).items
    finally:
        if executor is not None:
            await executor.close()
        owned.connection.close()


def test_family_sql_uses_prefix_index_and_deduplicates_without_catalog_rescan(tmp_path):
    """真实规格索引的读取量检查；关系样本不作为可回放业务历史。"""
    from .test_runtime import _create_valid_database
    from camctl.persistence.repositories.result_file_pairing import _family_counts
    path = tmp_path / 'queries.db'
    _create_valid_database(path)
    db = sqlite3.connect(path)
    try:
        # 此连接只测关系查询，不开启业务外键校验；所有写入留在私有事务后回滚。
        sql = ('INSERT INTO result_page_files(id,created_event_id,action_id,run_id,attempt_id,'
               'page_event_id,file_id,identity,family_directory,family_stem,family_role)'
               ' VALUES(?,?,?,?,?,?,?,?,?,?,?)')
        db.executemany(sql, [
            (1, 1, 1, 1, 1, 1, 1, '/DCIM/raw.OSV', '/DCIM', 'raw', 1),
            (2, 2, 1, 1, 1, 2, 1, '/DCIM/raw.OSV', '/DCIM', 'raw', 1),
            (3, 2, 1, 1, 1, 2, 2, '/DCIM/raw.LRF', '/DCIM', 'raw', 2)])
        previous = 4
        steps = []
        for total in (20, 200, 2000):
            db.executemany(sql, ((i, i, 1, 1, 1, i, i, f'/elsewhere/{i}.OSV',
                '/elsewhere', str(i), 1) for i in range(previous, total + 4)))
            previous = total + 4
            measured = [0]
            db.set_progress_handler(lambda: measured.__setitem__(0, measured[0] + 1) or 0, 1)
            statements = []
            db.set_trace_callback(statements.append)
            result = _family_counts(db, 1, '/DCIM', 'raw', 10000)
            db.set_progress_handler(None, 0)
            db.set_trace_callback(None)
            assert result == [(1, (1,)), (1, (2,))]
            steps.append(measured[0])
            for statement in statements:
                explanation = db.execute('EXPLAIN QUERY PLAN ' + statement).fetchall()
                assert any('result_page_files_family' in row[3] and 'SEARCH' in row[3]
                           for row in explanation)
        # 首次准备 SQL 可能多做一次编译；无关目录增长不得增加已测读取量。
        assert all(value <= steps[0] + 20 for value in steps[1:])
    finally:
        db.rollback()
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('conflict', [False, True])
async def test_later_scan_preserves_first_pair_and_original_key(tmp_path, conflict):
    from camctl.capture.handlers import _begin_check_round
    from camctl.capture.result_pages import ResultPageSave
    from camctl.operations.attempts import AttemptFinish
    from camctl.operations.models import EvidenceValue, Settlement, SettlementBasis
    from camctl.operations.validation import validate_outcome
    from camctl.persistence.repositories.operations import OperationRepository
    from ..capture.test_record_file_completion_wait import _REGISTRY, _completion
    from ..capture.test_result_page_history import _actual
    owned, runtime, first, ids, context = await _ready(tmp_path, [_entry('raw.LRF'), _entry('raw.OSV')])
    try:
        preview = ids['/DCIM/raw.LRF']
        original_request = _request(owned, runtime, first, preview)
        original_key = new_operation_key()
        first_fact = _ok(runtime.capture.save_result_file_pairing(original_request, original_key, owned))
        original_binding = owned.connection.execute('SELECT original_device_file_id,pairing_evidence_json'
            ' FROM device_files WHERE id=?', (preview,)).fetchone()
        _ok(OperationRepository().finish_attempt(AttemptFinish(first.ref.ticket,
            validate_outcome(first.ref.ticket, first.page.outcome, _REGISTRY), runtime.wall_us(),
            retry_wait=True), new_operation_key(), owned))
        source = owned.connection.execute('SELECT source_action_id FROM device_files WHERE id=?',
                                         (preview,)).fetchone()[0]
        ticket = _begin_check_round(runtime, source).ticket
        name = 'raw.osv' if conflict else 'raw.OSV'
        actual = replace(_actual(ticket, entries=[_entry('raw.LRF'), _entry(name)], finalized=True),
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue('results_returned', 1,
                {'file_completion': _completion(context), 'file_completion_source_page_event_id': None})))
        ref = _ok(runtime.capture.save_result_page(ResultPageSave(ticket, 1, None,
            validate_outcome(ticket, actual, _REGISTRY), runtime.wall_us()), new_operation_key(), owned))
        final = runtime.capture.read_result_page(ref, owned)
        if conflict:
            original = dict(final.file_ids)['/DCIM/' + name]
            _ok(runtime.capture.save_ownership_from_result_page(
                OwnershipFromResultPageSave(ref, original, runtime.wall_us()), new_operation_key(), owned))
            entry = final.page.entries[1]
            _ok(runtime.capture.save_file_completion(FileCompletionSave(original, 3, runtime.wall_us(),
                basis=3, observation=entry.evidence, size_bytes=10,
                activity_id=int(context['activity_id']), result_page_event_id=ref.event_id,
                stop_result_event_id=context['stop_result_event_id']), new_operation_key(), owned))
        new_fact = _ok(runtime.capture.save_result_file_pairing(
            _request(owned, runtime, final, preview), new_operation_key(), owned))
        assert new_fact != first_fact
        expected = enum_for('device_file_pairing_facts.reason').CONFLICT if conflict else enum_for('device_file_pairing_facts.reason').UNIQUE
        assert owned.connection.execute('SELECT reason FROM device_file_pairing_facts WHERE id=?',
                                        (new_fact,)).fetchone() == (int(expected),)
        assert owned.connection.execute('SELECT original_device_file_id,pairing_evidence_json'
            ' FROM device_files WHERE id=?', (preview,)).fetchone() == original_binding
        assert runtime.capture.verify_result_file_pairing(original_request, original_key, owned) == first_fact
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_complete_family_pairs_without_rewriting_raw_page(tmp_path):
    owned, runtime, _, saved, preview, context = await _proof_world(
        tmp_path, entries=[_entry("raw.LRF"), _entry("raw.OSV")])
    try:
        ids = dict(saved.file_ids)
        original = ids["/DCIM/raw.OSV"]
        _ok(runtime.capture.save_ownership_from_result_page(
            OwnershipFromResultPageSave(saved.ref, original, runtime.wall_us()),
            new_operation_key(), owned))
        for entry in saved.page.entries:
            _ok(runtime.capture.save_file_completion(FileCompletionSave(
                ids[entry.identity], 3, runtime.wall_us(), basis=3,
                observation=entry.evidence, size_bytes=10,
                activity_id=int(context["activity_id"]), result_page_event_id=saved.ref.event_id,
                stop_result_event_id=context["stop_result_event_id"]),
                new_operation_key(), owned))
        raw_before = owned.connection.execute(
            "SELECT body_json FROM history_events WHERE id=?", (saved.ref.event_id,)
        ).fetchone()
        request = files.PairResultFilesSave(preview, saved.ref, _boundary(owned), runtime.wall_us())
        key = new_operation_key()
        fact = _ok(runtime.capture.save_result_file_pairing(request, key, owned))
        assert type(fact) is int and fact > 0
        row = owned.connection.execute(
            "SELECT preview_file_id,original_file_id,original_count,preview_count,state,reason"
            " FROM device_file_pairing_facts WHERE id=?", (fact,)
        ).fetchone()
        assert row[:4] == (preview, original, 1, 1)
        assert owned.connection.execute(
            "SELECT original_device_file_id FROM device_files WHERE id=?", (preview,)
        ).fetchone() == (original,)
        assert owned.connection.execute(
            "SELECT body_json FROM history_events WHERE id=?", (saved.ref.event_id,)
        ).fetchone() == raw_before
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
        assert runtime.capture.verify_result_file_pairing(request, key, owned) == fact
        assert _ok(runtime.capture.save_result_file_pairing(request, key, owned)) == fact
    finally:
        owned.connection.close()

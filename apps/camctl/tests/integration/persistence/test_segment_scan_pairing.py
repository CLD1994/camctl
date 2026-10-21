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
@pytest.mark.parametrize('bounded', [False, True])
async def test_member_uses_frozen_pair_fact_and_candidates_share_strict_proof(tmp_path, bounded):
    from camctl.capture.segment_models import PreviewPairingState, SegmentSetRef
    from camctl.capture.segment_storage import SegmentSetCreate
    from camctl.persistence.repositories.segments import SegmentRepository
    owned, runtime, saved, ids, _ = await _ready(tmp_path, [_entry('raw.LRF'), _entry('raw.OSV')])
    try:
        preview, original = ids['/DCIM/raw.LRF'], ids['/DCIM/raw.OSV']
        fact = _ok(runtime.capture.save_result_file_pairing(
            _request(owned, runtime, saved, preview), new_operation_key(), owned))
        boundary = _boundary(owned)
        member = runtime.capture.read_strict_result_file_input(
            saved.ref, preview, boundary, owned, pairing_fact_id=fact)
        assert member.pairing_fact_id == fact
        assert member.pairing_state is PreviewPairingState.PAIRED
        assert member.pairing_identity == '/DCIM/raw.OSV'
        source, attempt = owned.connection.execute('SELECT r.action_id,a.id FROM operation_attempts a'
            ' JOIN operation_runs r ON r.id=a.run_id WHERE r.id=? AND a.attempt_no=?',
            (saved.ref.ticket.run_id, saved.ref.ticket.attempt_id)).fetchone()
        ref = SegmentSetRef(source, attempt, saved.ref.event_id, 1)
        repo = SegmentRepository()
        _ok(repo.create(SegmentSetCreate(ref, int(saved.ref.ticket.target_id), saved.ref.ticket.run_id,
            runtime.action(source)['execution_spec_json']['target_duration_ms'], runtime.wall_us()),
            new_operation_key(), owned))
        original_input = runtime.capture.read_strict_result_file_input(saved.ref, original, boundary, owned)
        key = new_operation_key()
        member_at = runtime.wall_us()
        if bounded:
            from camctl.history.validators import EventContext
            from camctl.persistence.event_reads import SqlEventReads
            from camctl.persistence.transaction import BoundedCommandPlan
            from camctl.persistence.repositories.segments import _Command
            class BoundedMembers:
                def plan(self, scope):
                    plan = _Command('MEMBERS', ref, (member, original_input), member_at, key).plan(scope)
                    return BoundedCommandPlan(iter(plan.events),
                        lambda event, transaction: EventContext(transaction, plan.owners,
                            deepcopy(plan.state_rows),
                            current_reads=SqlEventReads(scope.connection, plan.segment_read_scope)),
                        plan.result, lambda connection: None)
            receipt = commit_operation(BoundedMembers(), key, owned)
            assert receipt.kind == 'completed', receipt.error
        else:
            _ok(repo.append_members(ref, (member, original_input), member_at, key, owned))
        _ok(repo.fix_members(ref, runtime.wall_us(), new_operation_key(), owned))
        candidates = repo.read_output_candidates(ref, None, 128, owned).items
        assert {item.file_id for item in candidates} == {preview, original}
        paired = next(item for item in candidates if item.file_id == preview)
        assert paired.original_file_id == original
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs').fetchone() == (0,)
        # opaque 名称没有 CAM 顺序键，仍可使用实际完整主干的可靠配对。
        stored = repo.read_members(ref, None, 128, owned).items
        assert all(item.member.key is None for item in stored)
        assert next(item for item in stored if item.file_id == preview).pairing_fact_id == fact
        from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
        path = Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        _ok(repo.append_members(ref, (member, original_input), member_at, key, owned))
        member_id = next(item.member_id for item in stored if item.file_id == preview)
        owned.connection.execute('PRAGMA foreign_keys=OFF')
        owned.connection.execute('UPDATE segment_members SET pairing_fact_id=? WHERE id=?',
                                 (fact + 1, member_id))
        receipt = repo.append_members(ref, (member, original_input), member_at, key, owned)
        assert receipt.kind is DbOutcomeKind.ROLLED_BACK
    finally:
        owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('bounded', [False, True])
async def test_actual_pair_save_guard_and_verify_do_not_replay_unrelated_pages(tmp_path, bounded):
    from camctl.capture.result_pages import ResultPageSave
    from camctl.devices.directory import DirectoryCursor
    from camctl.operations.validation import validate_outcome
    from ..capture.test_result_page_history import _actual, _BINDING
    from ..capture.test_record_file_completion_wait import _REGISTRY
    measured = []
    for count in (20, 200, 2000):
        directory = tmp_path / str(count)
        directory.mkdir()
        cursor = DirectoryCursor(_BINDING, ('/DCIM',), 0, '/DCIM/raw.OSV')
        owned, runtime, first, ids, _ = await _ready(directory,
            [_entry('raw.LRF'), _entry('raw.OSV')], next_cursor=cursor)
        try:
            saved = first
            for number in range(count):
                name = f'z{number:05d}.OSV'
                following = (None if number == count - 1 else
                    DirectoryCursor(_BINDING, ('/DCIM',), 0, '/DCIM/' + name))
                actual = _actual(first.ref.ticket, entries=[_entry(name)], cursor=cursor,
                                 next_cursor=following, finalized=following is None)
                ref = _ok(runtime.capture.save_result_page(ResultPageSave(first.ref.ticket,
                    number + 2, cursor, validate_outcome(first.ref.ticket, actual, _REGISTRY),
                    runtime.wall_us()), new_operation_key(), owned))
                cursor = following
            saved = runtime.capture.read_result_page(ref, owned)
            request = _request(owned, runtime, saved, ids['/DCIM/raw.LRF'])
            key = new_operation_key()
            statements = []
            steps = [0]
            owned.connection.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 100) or 0, 100)
            owned.connection.set_trace_callback(statements.append)
            if bounded:
                receipt = commit_operation(_AlteredPair(request, key, True), key, owned)
                assert receipt.kind == 'completed', receipt.error
                fact = receipt.result
            else:
                fact = _ok(runtime.capture.save_result_file_pairing(request, key, owned))
            assert runtime.capture.verify_result_file_pairing(request, key, owned) == fact
            owned.connection.set_progress_handler(None, 0)
            owned.connection.set_trace_callback(None)
            reads = [sql for sql in statements if sql.startswith('SELECT')]
            assert not any('l.change_count, e.id' in sql and 'l.entity_type = 1 ' in sql
                           for sql in reads)
            measured.append((len(reads), steps[0]))
        finally:
            owned.connection.close()
    assert max(value[0] for value in measured) == min(value[0] for value in measured)
    assert measured[-1][1] < measured[0][1] * 2
    print('pair_save_guard_verify_growth', bounded, list(zip((20, 200, 2000), measured)))


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['future', 'missing', 'original', 'endpoint', 'source', 'first_binding'])
async def test_strict_pair_reference_rejects_future_foreign_and_damaged_fact(tmp_path, case):
    owned, runtime, saved, ids, _ = await _ready(tmp_path, [_entry('raw.LRF'), _entry('raw.OSV')])
    try:
        preview = ids['/DCIM/raw.LRF']
        before = _boundary(owned)
        fact = _ok(runtime.capture.save_result_file_pairing(
            _request(owned, runtime, saved, preview), new_operation_key(), owned))
        boundary = before if case == 'future' else _boundary(owned)
        file_id = ids['/DCIM/raw.OSV'] if case == 'original' else preview
        if case == 'endpoint':
            owned.connection.execute('UPDATE device_file_pairing_facts SET original_file_event_id=1 WHERE id=?', (fact,))
        elif case == 'source':
            owned.connection.execute('UPDATE device_file_pairing_facts SET source_event_id=1 WHERE id=?', (fact,))
        elif case == 'first_binding':
            owned.connection.execute('UPDATE device_files SET pairing_evidence_json=? WHERE id=?',
                (json.dumps({'method': 2, 'pairing_fact_id': fact + 1}), preview))
        with pytest.raises(ConsistencyError):
            runtime.capture.read_strict_result_file_input(saved.ref, file_id, boundary, owned,
                pairing_fact_id=fact + 1 if case == 'missing' else fact)
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_unknown_strict_completion_remains_unknown_in_member_and_candidate(tmp_path):
    from camctl.capture.segment_models import FileCompletionState, SegmentSetRef
    from camctl.capture.segment_storage import SegmentSetCreate
    from camctl.persistence.repositories.segments import SegmentRepository
    from .test_segment_file_pairing import _world
    owned, runtime, _, saved = await _world(tmp_path, [_entry('raw.OSV')])
    ids = dict(saved.file_ids)
    try:
        original = ids['/DCIM/raw.OSV']
        _ok(runtime.capture.save_ownership_from_result_page(
            OwnershipFromResultPageSave(saved.ref, original, runtime.wall_us()),
            new_operation_key(), owned))
        _ok(runtime.capture.save_file_completion(FileCompletionSave(original, 3, runtime.wall_us(),
            basis=1, observation={}, size_bytes=10), new_operation_key(), owned))
        member = runtime.capture.read_strict_result_file_input(saved.ref, original, _boundary(owned), owned)
        assert member.completion is FileCompletionState.UNKNOWN
        source, attempt = owned.connection.execute('SELECT r.action_id,a.id FROM operation_attempts a'
            ' JOIN operation_runs r ON r.id=a.run_id WHERE r.id=? AND a.attempt_no=?',
            (saved.ref.ticket.run_id, saved.ref.ticket.attempt_id)).fetchone()
        ref = SegmentSetRef(source, attempt, saved.ref.event_id, 1)
        repo = SegmentRepository()
        _ok(repo.create(SegmentSetCreate(ref, int(saved.ref.ticket.target_id), saved.ref.ticket.run_id,
            runtime.action(source)['execution_spec_json']['target_duration_ms'], runtime.wall_us()),
            new_operation_key(), owned))
        before = _snapshot(owned)
        receipt = repo.append_members(ref, (replace(member, completion=FileCompletionState.COMPLETE),),
            runtime.wall_us(), new_operation_key(), owned)
        assert receipt.kind is DbOutcomeKind.ROLLED_BACK
        assert _snapshot(owned) == before
        _ok(repo.append_members(ref, (member,), runtime.wall_us(), new_operation_key(), owned))
        _ok(repo.fix_members(ref, runtime.wall_us(), new_operation_key(), owned))
        assert not repo.read_output_candidates(ref, None, 128, owned).items
        assert repo.read_members(ref, None, 1, owned).items[0].member.completion is FileCompletionState.UNKNOWN
    finally:
        owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('bounded', [False, True])
@pytest.mark.parametrize('damage', ['fact', 'pair_identity', 'pair_state', 'file_event'])
async def test_member_guard_rederives_fixed_pair_input(tmp_path, bounded, damage):
    from camctl.capture.segment_models import SegmentSetRef
    from camctl.capture.segment_storage import SegmentSetCreate
    from camctl.history.validators import EventContext
    from camctl.persistence.event_reads import SqlEventReads
    from camctl.persistence.repositories.segments import SegmentRepository, _Command
    from camctl.persistence.transaction import BoundedCommandPlan
    owned, runtime, saved, ids, _ = await _ready(tmp_path, [_entry('raw.LRF'), _entry('raw.OSV')])
    try:
        preview = ids['/DCIM/raw.LRF']
        before_pair = _boundary(owned)
        fact = _ok(runtime.capture.save_result_file_pairing(
            _request(owned, runtime, saved, preview), new_operation_key(), owned))
        member = runtime.capture.read_strict_result_file_input(saved.ref, preview, _boundary(owned),
                                                              owned, pairing_fact_id=fact)
        source, attempt = owned.connection.execute('SELECT r.action_id,a.id FROM operation_attempts a'
            ' JOIN operation_runs r ON r.id=a.run_id WHERE r.id=? AND a.attempt_no=?',
            (saved.ref.ticket.run_id, saved.ref.ticket.attempt_id)).fetchone()
        ref = SegmentSetRef(source, attempt, saved.ref.event_id, 1)
        repo = SegmentRepository()
        _ok(repo.create(SegmentSetCreate(ref, int(saved.ref.ticket.target_id), saved.ref.ticket.run_id,
            runtime.action(source)['execution_spec_json']['target_duration_ms'], runtime.wall_us()),
            new_operation_key(), owned))
        key = new_operation_key()
        class ForgedMembers:
            def plan(self, scope):
                plan = _Command('MEMBERS', ref, (member,), runtime.wall_us(), key).plan(scope)
                event = plan.events[0]
                row = event.rows[0]
                after = dict(row.after.values)
                fields = {'fact': ('pairing_fact_id', fact + 1),
                    'pair_identity': ('pairing_identity', '/DCIM/wrong.OSV'),
                    'pair_state': ('pairing_state', 3),
                    'file_event': ('file_event_id', before_pair.last_event_id)}
                column, value = fields[damage]
                after[column] = value
                if damage == 'pair_state': after['pairing_identity'] = None
                evidence = deepcopy(event.evidence)
                evidence['segment_request']['input'][0][column] = value
                if damage == 'pair_state': evidence['segment_request']['input'][0]['pairing_identity'] = None
                event = replace(event, rows=(replace(row, after=replace(row.after, values=after)),),
                                evidence=evidence)
                plan = replace(plan, events=(event,))
                if not bounded: return plan
                return BoundedCommandPlan(iter(plan.events),
                    lambda event, transaction: EventContext(transaction, plan.owners,
                        deepcopy(plan.state_rows), current_reads=SqlEventReads(scope.connection,
                                                                              plan.segment_read_scope)),
                    plan.result, lambda connection: None)
        before = _snapshot(owned)
        receipt = commit_operation(ForgedMembers(), key, owned)
        assert receipt.kind == 'rolled_back', receipt.error
        assert _snapshot(owned) == before
        assert owned.connection.execute('SELECT COUNT(*) FROM segment_members').fetchone() == (0,)
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_committed_baseline_proof_uses_fixed_anchor_without_page_history_replay(tmp_path):
    from camctl.capture.baseline_models import BaselineFixSave
    from camctl.capture.handlers import _begin_check_round, _control_call
    from camctl.devices.directory import DirectoryCursor
    from camctl.persistence.repositories import result_pages
    from camctl.persistence.repositories.result_file_ownership import _proof
    from .test_segment_file_pairing import _BaselineCatalog
    from ..capture.result_consumer_fixtures import consumer_world
    from ..capture.test_result_page_history import _actual, _request as page_request, _BINDING
    owned, runtime, source, _ = await consumer_world(tmp_path, 'record', start=False,
                                                    catalog=_BaselineCatalog())
    try:
        activity = owned.connection.execute('SELECT id FROM device_activities WHERE action_id=?',
                                            (source,)).fetchone()[0]
        _ok(runtime.capture.fix_baseline(BaselineFixSave(activity, 0, 0, runtime.wall_us()),
                                         new_operation_key(), owned))
        await _control_call(runtime, runtime.action(source), 'start_recording', 'start_confirmed',
                            activity_facts={'started_at': runtime.wall_us(), 'activity_state': 2})
        ticket = _begin_check_round(runtime, source).ticket
        cursor = DirectoryCursor(_BINDING, ('/DCIM',), 0, '/DCIM/raw.OSV')
        first = _ok(runtime.capture.save_result_page(page_request(runtime, ticket,
            _actual(ticket, entries=[_entry('raw.LRF'), _entry('raw.OSV')], next_cursor=cursor)),
            new_operation_key(), owned))
        preview = dict(runtime.capture.read_result_page(first, owned).file_ids)['/DCIM/raw.LRF']
        measures = []
        for number in range(2000):
            name = f'z{number:05d}.OSV'
            following = None if number == 1999 else DirectoryCursor(_BINDING, ('/DCIM',), 0, '/DCIM/' + name)
            ref = _ok(runtime.capture.save_result_page(page_request(runtime, ticket,
                _actual(ticket, entries=[_entry(name)], cursor=cursor, next_cursor=following,
                    finalized=following is None), page_no=number + 2, cursor=cursor),
                new_operation_key(), owned))
            cursor = following
            if number + 1 not in (20, 200, 2000): continue
            steps = [0]
            statements = []
            owned.connection.execute('BEGIN')
            owned.connection.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 100) or 0, 100)
            owned.connection.set_trace_callback(statements.append)
            saved, raw, event, transaction = result_pages._at_record(owned.connection, ref.event_id)
            result_pages.committed_page_proof(owned.connection, saved, raw, event, transaction)
            _proof(owned.connection, OwnershipFromResultPageSave(first, preview, runtime.wall_us()))
            owned.connection.set_progress_handler(None, 0)
            owned.connection.set_trace_callback(None)
            owned.connection.execute('ROLLBACK')
            measures.append((len(statements), steps[0]))
            anchors = [sql for sql in statements if 'INDEXED BY history_events_type' in sql]
            assert len(anchors) == 2
            for sql in anchors:
                assert any('history_events_type' in row[3] and 'SEARCH' in row[3]
                           for row in owned.connection.execute('EXPLAIN QUERY PLAN ' + sql))
        assert len({value[0] for value in measures}) == 1
        assert measures[-1][1] < measures[0][1] * 2
        print('committed_baseline_proof_growth', list(zip((20, 200, 2000), measures)))
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

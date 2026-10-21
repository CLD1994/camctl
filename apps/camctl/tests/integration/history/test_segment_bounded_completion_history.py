"""真实有界完成组与同H历史、快照及后续独立产物事实共同消费。"""
from dataclasses import replace
from itertools import zip_longest
import sqlite3

import pytest

from camctl.capture.files import FileChecksumSave, FilePresenceSave
from camctl.capture.segment_models import DefaultScopeState, SegmentAssessmentReason
from camctl.contracts.enums import enum_for, load_registry
from camctl.contracts.values import new_operation_key
from camctl.contracts.json_values import json_equal, parse_exact_json
from camctl.history.snapshots import MaintenanceContext, MaintenanceState, maintain_snapshots
from camctl.persistence.executor import DbExecutor
from camctl.persistence.repositories import segment_transactions as transactions
from camctl.persistence.repositories.history import HistoryRepository, SqliteSnapshotStore
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import BoundedCommandPlan

from ..persistence.test_segment_storage import _ok, _preview_world, _world
from ..persistence.test_segment_transaction_batches import _ready_world, _request_for, _finish_attempt, ConnectionProbe
from ..outputs.test_cleanup_query_save_recovery import _start_cleanup


BATCH = 2


async def _unknown_world(tmp_path):
    # 顺序未知时首遍结果已是None，不向要求可靠顺序键的二遍生成器传入。
    from camctl.capture.segment_models import CompletionFacts, EvidenceState, SegmentAccumulator
    from camctl.capture.segments import advance_segment_assessment, finish_segment_assessment
    from camctl.devices.segments import SegmentKey
    from camctl.persistence.repositories.segments import iter_segment_members
    owned, runtime, repo, ref, members = await _world(tmp_path, indexes=(1, 2, 3), unknown_keys=True)
    try:
        for offset in range(0, len(members), BATCH):
            _ok(repo.append_members(ref, members[offset:offset+BATCH], runtime.wall_us(), new_operation_key(), owned))
        _ok(repo.fix_members(ref, runtime.wall_us(), new_operation_key(), owned))
        facts = CompletionFacts(EvidenceState.CONFIRMED, EvidenceState.CONFIRMED, EvidenceState.CONFIRMED,
            EvidenceState.CONFIRMED, SegmentKey(1, 1), EvidenceState.CONFIRMED, EvidenceState.UNKNOWN, True, False)
        state = SegmentAccumulator(ref, 60000, facts.start_key)
        for member in iter_segment_members(ref, owned):
            state = advance_segment_assessment(state, member)
        assessment = finish_segment_assessment(state, facts)
        assert assessment.gap_count is None
        _ok(repo.save_assessment(ref, facts, assessment, runtime.wall_us(), new_operation_key(), owned))
        _finish_attempt(owned, ref, runtime)
        request = _request_for(ref, repo.read_set(ref, owned), runtime.wall_us(), assessment, len(members))
        return owned, runtime, repo, ref, request
    except BaseException:
        owned.connection.close()
        raise


async def _snapshot_at_s(history, action_id, owned, *, threshold=None):
    """真实维护选择/恢复/准备/保存；不构造正文或替换候选端口。"""
    if threshold is None:
        threshold = owned.connection.execute(
            'SELECT change_count FROM actions WHERE id=?', (action_id,)).fetchone()[0]
    executor = DbExecutor(
        lambda: open_existing(history.path, DbOpenMode.EXISTING_RW, DbConfig()),
        capacity=4, enqueue_timeout_seconds=9)
    try:
        result = await maintain_snapshots(MaintenanceContext(
            store=SqliteSnapshotStore(history.path, executor), state=MaintenanceState(),
            threshold=threshold, batch_size=1))
        action_type = load_registry()['history_objects']['action']['id']
        assert any(item.ref.entity_type == action_type and item.ref.entity_id == action_id
                   for item in result.saved)
        row = owned.connection.execute(
            'SELECT boundary_event_id,change_count FROM entity_snapshots'
            ' WHERE entity_type=? AND entity_id=? ORDER BY boundary_event_id DESC LIMIT 1',
            (action_type, action_id)).fetchone()
        assert row is not None
        return row
    finally:
        await executor.close()


def _pages(handle, table, ref):
    cursor = None
    while True:
        page = handle.read_page(table, ref=ref, cursor=cursor, limit=BATCH)
        assert len(page.items) <= BATCH
        yield page
        if page.next_cursor is None:
            return
        cursor = page.next_cursor


def _rows(handle, table, ref):
    for page in _pages(handle, table, ref):
        yield from page.items


def _assert_self_rows(reference, actual):
    """逐行按精确JSON值比较全部字段，字典成员顺序不改变事实。"""
    count = 0
    for expected, row in zip_longest(reference.iter_rows(), actual.iter_rows()):
        assert expected is not None and row is not None
        assert row.table not in ('outputs', 'device_files')
        assert (row.table, row.row_id) == (expected.table, expected.row_id)
        assert json_equal(dict(row.values), dict(expected.values)), (row.table, row.row_id, expected.values, row.values)
        count += 1
    return count


def _identity_totals(connection):
    return tuple(connection.execute(f'SELECT COUNT(*),MAX(id) FROM {table}').fetchone()
                 for table in ('history_events', 'outputs', 'output_origins'))


def _assert_original_receipt(owned, request, key, result, monkeypatch):
    """原键核实及重送必须只读，不分配、不读完整组事件。"""
    import camctl.persistence.transaction as kernel
    before = _identity_totals(owned.connection)
    def forbidden(*args, **kwargs):
        raise AssertionError('原完成核实不得分配或整组读取')
    def read_only(action, *args):
        return sqlite3.SQLITE_DENY if action in (
            sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE) else sqlite3.SQLITE_OK
    with monkeypatch.context() as patch:
        patch.setattr(transactions, 'next_row_id', forbidden)
        patch.setattr(kernel, 'saved_transaction_events', forbidden)
        owned.connection.set_authorizer(read_only)
        try:
            assert transactions.verify_segment_finish(
                request, key, owned, execution_finished=True, batch_size=1) == result
            repeated = transactions.finish_segment_recording(request, key, owned, batch_size=1)
            assert repeated.kind == 'completed', repeated.error
            assert repeated.result == result
        finally:
            owned.connection.set_authorizer(None)
    assert _identity_totals(owned.connection) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['paired_scope', 'missing_start', 'unknown_order'])
async def test_bounded_completion_keeps_original_h_after_checksum_and_cleanup(tmp_path, monkeypatch, case):
    """遗漏ACTIVATE、配对或范围、读取当前文件事实、重分配原编号均须失败。"""
    if case == 'paired_scope':
        owned, runtime, repo, ref, _, _, _ = await _preview_world(
            tmp_path, original_sizes=(10,)*5, paired_index=1, page_size=1)
        row = repo.read_set(ref, owned)
        request = transactions.FinishSegmentRecording(
            ref, row['activity_id'], row['run_id'], runtime.wall_us())
        expected_originals, expected_previews, expected_scope = 5, 1, 5
        scope_state, gap_count = DefaultScopeState.NONEMPTY, 0
        expected_indexes = (1, 1, 2, 3, 4, 5)  # 同key LRF先于OSV，随后按设备顺序。
    else:
        if case == 'missing_start':
            owned, runtime, repo, ref, request = await _ready_world(tmp_path, count=3, indexes=(2, 3, 4))
        else:
            owned, runtime, repo, ref, request = await _unknown_world(tmp_path)
        expected_originals, expected_previews, expected_scope = 3, 0, 0
        scope_state = DefaultScopeState.EMPTY if case == 'missing_start' else DefaultScopeState.UNKNOWN
        gap_count = 1 if case == 'missing_start' else None
        expected_indexes = (2, 3, 4) if case == 'missing_start' else (None,)*3
    history = HistoryRepository(owned.connection.execute('PRAGMA database_list').fetchone()[2])
    try:
        start = history.current_boundary()
        snapshot_event, _ = await _snapshot_at_s(history, ref.action_id, owned)
        assert snapshot_event == start.last_event_id
        with history.open_segment_history(ref.action_id, start, strategy='snapshot', batch_size=BATCH) as seed:
            assert seed.active_ref() is None
            assert all(row.values['output_id'] is None
                       for row in _rows(seed, 'segment_scope_members', ref))
        first_output = owned.connection.execute('SELECT COALESCE(MAX(id),0)+1 FROM outputs').fetchone()[0]
        first_origin = owned.connection.execute('SELECT COALESCE(MAX(id),0)+1 FROM output_origins').fetchone()[0]
        key = new_operation_key()
        bounded = False
        original_plan = transactions._FinishCommand.plan
        def plan(command, scope):
            nonlocal bounded
            value = original_plan(command, scope)
            bounded = isinstance(value, BoundedCommandPlan)
            return value
        def lost_receipt(stage, statement, params):
            if stage == 'after' and statement == 'COMMIT':
                raise sqlite3.OperationalError('controlled receipt loss after actual commit')
        writer = (replace(owned, connection=ConnectionProbe(owned.connection, lost_receipt))
                  if case == 'unknown_order' else owned)
        with monkeypatch.context() as patch:
            patch.setattr(transactions._FinishCommand, 'plan', plan)
            receipt = transactions.finish_segment_recording(request, key, writer, batch_size=BATCH)
        assert bounded
        if case == 'unknown_order':
            assert receipt.kind == 'unknown'
            owned.connection.close()
            owned = open_existing(history.path, DbOpenMode.EXISTING_RW, DbConfig())
            result = transactions.verify_segment_finish(request, key, owned, execution_finished=True, batch_size=BATCH)
            assert result is not None
        else:
            assert receipt.kind == 'completed', receipt.error
            assert receipt.change_set is None
            result = receipt.result
            assert result.boundary == receipt.boundary
        boundary = result.boundary
        assert boundary == history.current_boundary() and boundary.last_event_id > snapshot_event
        assert result.ref == ref
        assert (result.original_count, result.preview_count, result.output_count, result.scope_count) == (
            expected_originals, expected_previews, expected_originals+expected_previews, expected_scope)
        assert result.action_status == int(enum_for('actions.status').SUCCEEDED if case == 'paired_scope'
                                           else enum_for('actions.status').FAILED)
        assert not hasattr(result, 'output_ids')
        transaction = transactions.read_transaction_range(owned.connection, boundary.txn_id)
        from camctl.persistence.transaction import read_saved_transaction_page
        first_page = read_saved_transaction_page(owned.connection, key, transaction, limit=BATCH)
        assert len(first_page.items) == BATCH and first_page.next_cursor is not None
        assert first_page.items[0].occurred_at == request.occurred_at
        _assert_original_receipt(owned, request, key, result, monkeypatch)

        file_id = owned.connection.execute('SELECT device_file_id FROM outputs WHERE id=?', (first_output,)).fetchone()[0]
        _ok(runtime.capture.save_file_checksum(FileChecksumSave(
            file_id, int(enum_for('device_files.checksum_support').SUPPORTED), runtime.wall_us(), 'a'*64),
            new_operation_key(), owned))
        cleanup_action, cleanup_item = _start_cleanup(owned, first_output, '902')
        assert owned.connection.execute('SELECT output_id FROM cleanup_items WHERE id=?', (cleanup_item,)).fetchone() == (first_output,)
        _ok(runtime.capture.save_file_presence(FilePresenceSave(
            file_id, int(enum_for('device_files.presence_state').ABSENT), runtime.wall_us()), new_operation_key(), owned))
        current = history.current_boundary()
        assert current.last_event_id > boundary.last_event_id
        changed_output = history.restore_primary_at_h('output', first_output, current, event_batch_size=BATCH)
        assert changed_output['availability'] == int(enum_for('outputs.availability').RESTRICTED)
        assert changed_output['cleanup_status'] == int(enum_for('outputs.cleanup_status').PENDING)
        changed_file = history.restore_primary_at_h('device_file', file_id, current, event_batch_size=BATCH)
        assert changed_file['sha256'] == 'a'*64
        assert changed_file['presence_state'] == int(enum_for('device_files.presence_state').ABSENT)
        _assert_original_receipt(owned, request, key, result, monkeypatch)

        # 真正逆向跨过A完整完成组，而不只在完成后的无来源变化区间查询。
        with history.open_segment_history(ref.action_id, start, strategy='current', batch_size=BATCH) as reversed_finish:
            assert reversed_finish.primary()['status'] == int(enum_for('actions.status').RUNNING)
            assert reversed_finish.active_ref() is None
            assert reversed_finish.read_set(ref)['published_event_id'] is None
            assert all(scope.values['output_id'] is None
                       for scope in _rows(reversed_finish, 'segment_scope_members', ref))
            with history.open_segment_history(ref.action_id, start, strategy='snapshot', batch_size=BATCH) as original_snapshot:
                _assert_self_rows(original_snapshot, reversed_finish)
        assert history.restore_primary_at_h('output', first_output, start, event_batch_size=BATCH) is None

        with history.open_segment_history(ref.action_id, boundary, strategy='initial', batch_size=BATCH) as reference:
            for strategy in ('initial', 'snapshot', 'current'):
                with history.open_segment_history(ref.action_id, boundary, strategy=strategy, batch_size=BATCH) as handle:
                    assert handle.primary() == history.restore_primary_at_h('action', ref.action_id, boundary, event_batch_size=BATCH)
                    assert handle.primary()['status'] == result.action_status
                    assert handle.active_ref() == ref
                    saved_set = handle.read_set(ref)
                    assert saved_set['published_event_id'] == boundary.last_event_id
                    assessment = handle.read_assessment(ref)
                    assert assessment.default_scope.state is scope_state and assessment.gap_count == gap_count
                    if case == 'missing_start':assert assessment.reason is SegmentAssessmentReason.GAP
                    positions = iter(expected_indexes)
                    member_count = 0
                    for page in _pages(handle, 'segment_members', ref):
                        for member in page.items:
                            assert member.values['segment_index'] == next(positions)
                            member_count += 1
                    assert member_count == len(expected_indexes)
                    assert next(positions, 'end') == 'end'
                    gaps = 0
                    for gap in _rows(handle, 'segment_gaps', ref):
                        gaps += 1
                        assert (gap.values['start_directory'], gap.values['start_index'],
                                gap.values['end_directory'], gap.values['end_index']) == (1, 1, 1, 1)
                    assert gaps == (1 if case == 'missing_start' else 0)
                    selected = 0
                    original_members = (member.row_id for member in _rows(handle, 'segment_members', ref)
                                        if member.values['role'] == int(enum_for('segment_members.role').ORIGINAL))
                    for scope in _rows(handle, 'segment_scope_members', ref):
                        assert scope.values['ordinal'] == selected+1
                        assert scope.values['member_id'] == next(original_members)
                        assert scope.values['output_id'] == first_output+selected
                        selected += 1
                    assert selected == expected_scope
                    if expected_scope:assert next(original_members, None) is None
                    own_rows = _assert_self_rows(reference, handle)
                for offset in range(result.output_count):
                    output_id = first_output+offset
                    output_rows = history.restore_entity('output', output_id, boundary, event_batch_size=BATCH)
                    output = output_rows[('outputs', output_id)]
                    assert output['source_action_id'] == ref.action_id
                    assert output['availability'] == int(enum_for('outputs.availability').AVAILABLE)
                    assert output['cleanup_status'] == int(enum_for('outputs.cleanup_status').NOT_REQUESTED)
                    source = history.restore_primary_at_h('device_file', output['device_file_id'], boundary, event_batch_size=BATCH)
                    assert source['sha256'] is None
                    assert source['size_bytes'] == 10
                    if case == 'unknown_order':
                        expected_identity = f'/DCIM/UNKNOWN_{offset:04d}.OSV'
                    else:
                        index = 1 if offset == expected_originals else offset+(2 if case == 'missing_start' else 1)
                        expected_identity = f'/DCIM/CAM001/CAM_20261021090000_{index:04d}_D.'+('LRF' if offset == expected_originals else 'OSV')
                    assert parse_exact_json(source['identity_key']) == ['cam-1', 'camctl-adb', expected_identity]
                    assert output['kind'] == int(enum_for('outputs.kind').PREVIEW if offset == expected_originals
                                                 else enum_for('outputs.kind').ORIGINAL)
                    assert source['presence_state'] == int(enum_for('device_files.presence_state').UNKNOWN)
                    if offset == expected_originals:
                        origin = output_rows[('output_origins', first_origin)]
                        assert origin['output_id'] == output_id and origin['original_output_id'] == first_output
                    else:
                        assert not any(table == 'output_origins' for table, _ in output_rows)
        # C快照实际编码已完成组的marker/范围赋值；旧H仍须选原S。
        snapshot_at_current, _ = await _snapshot_at_s(history, ref.action_id, owned, threshold=1)
        assert snapshot_at_current == current.last_event_id
        with history.open_segment_history(ref.action_id, current, strategy='snapshot', batch_size=BATCH) as complete_snapshot:
            assert complete_snapshot.active_ref() == ref
            assert complete_snapshot.read_set(ref)['published_event_id'] == boundary.last_event_id
            with history.open_segment_history(ref.action_id, boundary, strategy='snapshot', batch_size=BATCH) as old_snapshot:
                # 后续只有独立output/file/清理action变化，原来源自身事实完全相同。
                assert _assert_self_rows(old_snapshot, complete_snapshot) == own_rows
        old_file = history.restore_entity('device_file', file_id, boundary, event_batch_size=BATCH)[('device_files', file_id)]
        assert old_file['sha256'] is None and old_file['presence_state'] == int(enum_for('device_files.presence_state').UNKNOWN)
        old_output = history.restore_primary_at_h('output', first_output, boundary, event_batch_size=BATCH)
        assert old_output['availability'] == int(enum_for('outputs.availability').AVAILABLE)
        action_type = load_registry()['history_objects']['action']['id']
        assert owned.connection.execute('SELECT COUNT(*) FROM entity_snapshots WHERE entity_type=? AND entity_id=?',
            (action_type, ref.action_id)).fetchone()[0] == 2
        print('COMBINATION', case, 'ref', ref, 'S', start, 'H', boundary, 'C', current,
              'outputs', result.output_count, 'scope', result.scope_count, 'cleanup', cleanup_action,
              'receipt', receipt.kind, 'own_rows', own_rows)
    finally:
        owned.connection.close()

"""已固定成员消费保存事实，不再次执行来源事实的创建算法。"""

import sqlite3
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.capture.files import file_identity_key
from camctl.capture.segment_models import (
    FileCompletionState, OwnershipState, SegmentMemberRole,
)
from camctl.capture.segment_storage import SegmentResultRef
from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import HistoryBoundary, TransactionRange
from camctl.contracts.values import ConsistencyError
from camctl.persistence.repositories import result_file_pairing as pairing, result_file_proofs as files, segments
from camctl.persistence.fact_reads import EventRecord
from camctl.persistence.transaction import event_envelope, row_change


def _connection():
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    connection.in_transaction, connection.total_changes = True, 0
    return connection


def test_fixed_original_member_does_not_replay_source_page(monkeypatch):
    db = _connection()
    identity = "/DCIM/DJI_20261025_0077_D.OSV"
    row = dict(id=8, set_id=4, file_id=30, page_event_id=872,
               file_event_id=883, identity=identity, directory_number=1,
               segment_index=77, role=1, ownership=1, completion=1,
               pairing_identity=None, pairing_state=1, pairing_fact_id=None)
    row.update(role=int(enum_for("segment_members.role").ORIGINAL),
               ownership=int(enum_for("segment_members.ownership").OWNED),
               completion=int(enum_for("segment_members.completion").COMPLETE),
               pairing_state=int(enum_for("segment_members.pairing_state").NOT_APPLICABLE))
    file = dict(id=30, identity_key=file_identity_key("camera", "osmo", identity),
                size_bytes=4096, source_action_id=21,
                role=int(enum_for("device_files.role").ORIGINAL),
                completion_state=int(enum_for("device_files.completion_state").COMPLETE),
                last_event_id=883, ownership_evidence_json={"source": "saved"},
                completion_evidence_json={"source": "saved"},
                original_device_file_id=None, pairing_evidence_json=None)
    monkeypatch.setattr(segments, "read_fixed_file", create_autospec(
        segments.read_fixed_file, return_value=file))
    monkeypatch.setattr(segments, "row_facts", create_autospec(
        segments.row_facts, return_value=dict(id=4, action_id=21, run_id=28, source_event_id=885)))
    monkeypatch.setattr(segments, "_one", create_autospec(
        segments._one, return_value=(identity, 21, 28)))
    monkeypatch.setattr(segments, "_at", create_autospec(
        segments._at, side_effect=AssertionError("普通成员读取不应重放原页")))
    stored = segments._read_member(db, row)
    assert (stored.member_id, stored.file_id, stored.member.identity) == (8, 30, identity)
    assert stored.member.role is SegmentMemberRole.ORIGINAL
    assert stored.member.ownership is OwnershipState.OWNED
    assert stored.member.completion is FileCompletionState.COMPLETE
    assert stored.member.size_bytes == 4096


@pytest.fixture
def saved_pairing(monkeypatch):
    db = _connection()
    preview_identity, original_identity = '/DCIM/0077_D.LRF', '/DCIM/0077_D.OSV'
    fact = dict(id=15, preview_file_id=29, original_file_id=30, attempt_id=28,
                created_event_id=883, source_txn_id=598, source_event_id=882,
                final_page_event_id=876, preview_page_event_id=872, original_page_event_id=872,
                preview_file_event_id=881, original_file_event_id=880,
                original_count=1, preview_count=1, state=int(pairing._STATE.PAIRED),
                reason=int(pairing._WHY.UNIQUE))
    event = event_envelope(883, 599, pairing._EVENT['id'], pairing._REASON,
        (row_change('device_file_pairing_facts', 15, {name:value for name,value in fact.items() if name != 'id'}),),
        1000, {'result_scan_pairing_request': {}})
    record = EventRecord((), event, TransactionRange(599, 883, 883))
    def facts(connection, table, identity):
        return {'device_file_pairing_facts': fact,
                'operation_attempts': dict(id=28, run_id=28),
                'operation_runs': dict(id=28, action_id=21),
                'actions': dict(id=21, execution_spec_json={})}[table]
    monkeypatch.setattr(pairing, 'row_facts', create_autospec(pairing.row_facts, side_effect=facts))
    monkeypatch.setattr(pairing, 'read_transaction', create_autospec(pairing.read_transaction,
        side_effect=lambda connection, identity: TransactionRange(identity, 880, 882 if identity == 598 else 885)))
    monkeypatch.setattr(pairing, 'read_event', create_autospec(pairing.read_event, return_value=record))
    def fixed(connection, file_id, event_id):
        return dict(id=file_id, last_event_id=event_id, source_action_id=21,
            identity_key=file_identity_key('camera', 'osmo', preview_identity if file_id == 29 else original_identity),
            role=int(enum_for('device_files.role').PREVIEW if file_id == 29 else enum_for('device_files.role').ORIGINAL),
            size_bytes=4096, completion_state=int(enum_for('device_files.completion_state').COMPLETE))
    monkeypatch.setattr(pairing, 'read_fixed_file', create_autospec(pairing.read_fixed_file, side_effect=fixed))
    monkeypatch.setattr(pairing, '_one', create_autospec(pairing._one,
        side_effect=lambda connection, sql, parameters:
        (preview_identity if parameters[1] == 29 else original_identity, 28, 21, 28)))
    monkeypatch.setattr(pairing, '_verify', create_autospec(pairing._verify,
        side_effect=AssertionError('已有配对读取不应重做创建分类')))
    return db, fact


def test_existing_pairing_fact_does_not_reclassify_original_scan(saved_pairing):
    db, fact = saved_pairing
    assert pairing.read_pairing_fact(db, 15, 29, HistoryBoundary(600, 885)) == fact


@pytest.mark.parametrize('change', [dict(preview_file_id=30), dict(created_event_id=886),
                                    dict(source_event_id=883)])
def test_existing_pairing_fact_still_rejects_wrong_identity_or_boundary(saved_pairing, change):
    db, fact = saved_pairing
    fact.update(change)
    with pytest.raises(ConsistencyError):
        pairing.read_pairing_fact(db, 15, 29, HistoryBoundary(600, 885))


@pytest.mark.parametrize('first_page_empty', [False, True])
def test_finish_candidates_carry_the_file_already_read_for_each_member(monkeypatch, first_page_empty):
    """分页规则交付明确文件输入，调用方无需再按member_id重新取文件。"""
    db = _connection()
    ref = SegmentResultRef(21, 4, 1)
    setrow = dict(id=4, action_id=21, run_id=28, source_event_id=885, member_count=3)
    rows = []
    for identity in range(1, 4):
        rows.append(dict(id=identity, set_id=4, file_id=29+identity, page_event_id=872,
            file_event_id=880+identity, identity=f'/DCIM/{identity}.OSV', directory_number=1,
            segment_index=identity, role=int(enum_for('segment_members.role').ORIGINAL),
            ownership=int(enum_for('segment_members.ownership').OWNED),
            completion=int(enum_for('segment_members.completion').COMPLETE),
            pairing_identity=None, pairing_state=int(enum_for('segment_members.pairing_state').NOT_APPLICABLE),
            pairing_fact_id=None))
    monkeypatch.setattr(segments, '_set', create_autospec(segments._set, return_value=setrow))
    monkeypatch.setattr(segments, 'row_facts', create_autospec(segments.row_facts,
        side_effect=lambda connection, table, identity: rows[identity-1]))
    monkeypatch.setattr(segments, '_one', create_autospec(segments._one,
        side_effect=lambda connection, sql, parameters:
        (3,) if 'COUNT(*)' in sql else (rows[parameters[1]-30]['identity'], 21, 28)))
    def execute(sql, parameters=()):
        cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
        cursor.description = tuple((name,) for name in rows[0])
        after_id = parameters[4]
        cursor.fetchmany.return_value = [tuple(row.values()) for row in rows if row['id'] > after_id][:parameters[-1]]
        return cursor
    db.execute.side_effect = execute
    def fixed(connection, file_id, event_id):
        return dict(id=file_id, last_event_id=event_id, source_action_id=21,
            identity_key=file_identity_key('camera', 'osmo', rows[file_id-30]['identity']),
            role=int(enum_for('device_files.role').ORIGINAL),
            size_bytes=0 if first_page_empty and file_id < 32 else 4096,
            completion_state=int(enum_for('device_files.completion_state').COMPLETE),
            ownership_evidence_json={'saved': True})
    port = create_autospec(segments.read_fixed_file, side_effect=fixed)
    monkeypatch.setattr(segments, 'read_fixed_file', port)
    owned = type('Reader', (), {'connection': db})()
    first = segments.read_segment_finish_candidates(ref, None, 2, owned)
    last = segments.read_segment_finish_candidates(ref, first.next_cursor, 2, owned)
    expected = [32] if first_page_empty else [30, 31, 32]
    assert [item.candidate.file_id for item in first.items+last.items] == expected
    assert [item.file['size_bytes'] for item in first.items+last.items] == [4096]*len(expected)
    assert first.next_cursor is not None and last.next_cursor is None
    assert port.call_count == 3


@pytest.mark.parametrize('entry', ['fixed', 'boundary'])
@pytest.mark.parametrize('already_open', [False, True])
@pytest.mark.parametrize('failed', [False, True])
def test_public_fixed_file_read_owns_one_snapshot_before_first_query(monkeypatch, entry, already_open, failed):
    db = _connection()
    db.in_transaction = already_open
    statements = []
    def execute(sql, parameters=()):
        statements.append(sql)
        if sql == 'BEGIN': db.in_transaction = True
        elif sql == 'ROLLBACK': db.in_transaction = False
    db.execute.side_effect = execute
    error = ConsistencyError('固定事实读取失败')
    def event(connection, event_id):
        assert connection.in_transaction
        if failed: raise error
        return SimpleNamespace(transaction=TransactionRange(599, 883, 883))
    def transaction(connection, transaction_id):
        assert connection.in_transaction
        return TransactionRange(599, 883, 883)
    monkeypatch.setattr(files, 'read_event', create_autospec(files.read_event, side_effect=event))
    monkeypatch.setattr(files, 'read_transaction', create_autospec(files.read_transaction, side_effect=transaction))
    monkeypatch.setattr(files, '_one', create_autospec(files._one, return_value=(883,)))
    monkeypatch.setattr(files, '_reuse_fixed_file', create_autospec(files._reuse_fixed_file,
        return_value=dict(id=30, last_event_id=883)))
    call = (lambda: files.read_fixed_file(db, 30, 883)) if entry == 'fixed' else (
        lambda: files.read_file_at_boundary(db, 30, HistoryBoundary(599, 883)))
    if failed:
        with pytest.raises(ConsistencyError) as caught: call()
        assert caught.value is error
    else:
        assert call() == dict(id=30, last_event_id=883)
    assert db.in_transaction is already_open
    assert statements == ([] if already_open else ['BEGIN', 'ROLLBACK'])


@pytest.mark.parametrize('damage', [None, 'missing_member', 'foreign_member'])
def test_finish_scope_carries_same_set_file_identity(monkeypatch, damage):
    db = _connection()
    ref = SegmentResultRef(21, 4, 1)
    monkeypatch.setattr(segments, '_set', create_autospec(segments._set, return_value={'id': 4}))
    cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
    cursor.fetchmany.return_value = [(1, 4, 1, 8, None,
        None if damage == 'missing_member' else 30, 5 if damage == 'foreign_member' else 4)]
    db.execute.return_value = cursor
    owned = SimpleNamespace(connection=db)
    if damage is not None:
        with pytest.raises(ConsistencyError):
            segments.read_segment_finish_scope(ref, None, 2, owned)
    else:
        page = segments.read_segment_finish_scope(ref, None, 2, owned)
        assert page.items == ((dict(id=1, set_id=4, ordinal=1, member_id=8, output_id=None), 30),)
        assert page.next_cursor is None

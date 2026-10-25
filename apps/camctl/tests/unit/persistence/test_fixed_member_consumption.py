"""已固定成员消费保存事实，不再次执行来源事实的创建算法。"""

import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.capture.files import file_identity_key
from camctl.capture.segment_models import (
    FileCompletionState, OwnershipState, SegmentMemberRole,
)
from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import HistoryBoundary, TransactionRange
from camctl.contracts.values import ConsistencyError
from camctl.persistence.repositories import result_file_pairing as pairing, segments
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

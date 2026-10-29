"""公开选择固定沿真实 128 项分页保留 ordinal、不可用项及范围边界。"""

from dataclasses import replace
import json

import pytest

from camctl.capture.segment_models import SegmentCursor, SegmentKey
from camctl.capture.segment_storage import assessment_document, assessment_from_document
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.segment_selections import FixSegmentSelection, fix_segment_selection

from ..support.output_storage_cases import QueryProbe
from .test_qualification import _seed_device_file, _seed_output
from .test_segment_default_scope import _source, _obtain


async def _ordered_scope(tmp_path):
    """正式来源头与规定存储格式共同提供任意产物身份的有序输入。"""
    owned, capture, source = await _source(tmp_path, indexes=(7,))
    db = owned.connection
    set_id, raw = db.execute('SELECT id,assessment_json FROM segment_sets WHERE action_id=?', (source,)).fetchone()
    columns = [row[1] for row in db.execute('PRAGMA table_info(segment_members)')]
    template = dict(zip(columns, db.execute('SELECT * FROM segment_members WHERE set_id=? AND role=1 LIMIT 1', (set_id,)).fetchone()))
    # 选择存储专项通过正式表格式准备关联，身份故意不按设备 ordinal 排序。
    identities = (50, 2, 19, *range(1004, 1130))
    outside = 999001
    db.execute('DELETE FROM output_origins WHERE output_id IN (SELECT id FROM outputs WHERE source_action_id=?)', (source,))
    db.execute('DELETE FROM outputs WHERE source_action_id=?', (source,))
    db.execute('DELETE FROM segment_scope_members WHERE set_id=?', (set_id,))
    db.execute('DELETE FROM segment_members WHERE set_id=?', (set_id,))
    for ordinal, output_id in enumerate(identities, 1):
        file_id = 10000 + ordinal
        identity = f'/DCIM/CAM_001/CAM_20261022100000_{ordinal:04d}_D.OSV'
        _seed_device_file(db, file_id, source, size=10, presence_state=3 if ordinal == 2 else 2)
        db.execute('UPDATE device_files SET identity_key=?,locator_json=? WHERE id=?', (identity, json.dumps({'path': identity}), file_id))
        _seed_output(db, output_id, source, file_id, availability=3 if ordinal == 2 else 1, size=10)
        member = {**template, 'id': ordinal, 'file_id': file_id, 'identity': identity,
                  'directory_number': 1, 'segment_index': ordinal}
        db.execute('INSERT INTO segment_members (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')', tuple(member[column] for column in columns))
        db.execute('INSERT INTO segment_scope_members(id,created_event_id,set_id,ordinal,member_id,output_id) VALUES (?,?,?,?,?,?)',
                   (ordinal, template['created_event_id'], set_id, ordinal, ordinal, output_id))
    _seed_device_file(db, 20000, source, size=10)
    _seed_output(db, outside, source, 20000, size=10)
    assessment = assessment_from_document(json.loads(raw))
    first = SegmentCursor(SegmentKey(1, 1), '/DCIM/CAM_001/CAM_20261022100000_0001_D.OSV')
    last = SegmentCursor(SegmentKey(1, len(identities)), f'/DCIM/CAM_001/CAM_20261022100000_{len(identities):04d}_D.OSV')
    scope = replace(assessment.default_scope, first_cursor=first, last_cursor=last, member_count=len(identities))
    assessment = replace(assessment, default_scope=scope)
    db.execute('UPDATE segment_sets SET member_count=?,assessment_json=? WHERE id=?',
               (len(identities), json.dumps(assessment_document(assessment)), set_id))
    db.commit()
    return owned, capture, source, identities, outside


@pytest.mark.asyncio
async def test_scope_order_and_unavailable_member_do_not_select_outside_scope(tmp_path):
    owned, capture, source, identities, outside = await _ordered_scope(tmp_path)
    try:
        action, selection = _obtain(owned, capture, source, 'default', 2)
        def scope_query(columns, reads, parameters):
            return columns == {'ordinal', 'output_id'} and any(table == 'segment_scope_members' for table, column in reads)
        probe = QueryProbe(owned.connection, scope_query)
        receipt = fix_segment_selection(FixSegmentSelection(selection, capture.wall_us()),
            new_operation_key(), replace(owned, connection=probe))
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        actual = owned.connection.execute('SELECT output_id,status FROM obtain_items WHERE selection_id=? ORDER BY id', (selection,)).fetchall()
        assert len(identities) == len(actual) == receipt.value.count == 129
        assert [row[0] for row in actual] == list(identities)
        assert actual[:3] == [(50, 2), (2, 4), (19, 2)]
        assert all(row[1] == 2 for row in actual[2:])
        assert outside not in {row[0] for row in actual}
        assert owned.connection.execute('SELECT availability FROM outputs WHERE id=?', (outside,)).fetchone() == (1,)
        assert probe.sizes and set(probe.sizes) == {128}
        assert any(probe.returned[index:index + 3] == [128, 1, 0] for index in range(len(probe.returned) - 2))
        assert all(cursor.close.call_count == 1 for cursor in probe.cursors)
    finally:
        owned.connection.close()

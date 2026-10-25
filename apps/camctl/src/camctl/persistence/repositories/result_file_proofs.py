"""读取指定历史位置的已保存文件资格，不重新执行来源创建算法。"""
from contextlib import closing, contextmanager
from dataclasses import replace

from camctl.capture.result_pages import ResultPageRef
from camctl.capture.segment_models import (
    FileCompletionState, OwnershipState, PreviewPairingState, SegmentMemberRole,
)
from camctl.capture.segment_storage import SegmentMemberInput
from camctl.contracts.enums import enum_for, load_registry
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.json_values import json_equal, parse_exact_json
from camctl.contracts.values import ConsistencyError, ObjectId
from camctl.history.events import business_columns
from camctl.persistence.fact_reads import read_event, read_transaction, _reuse_fixed_file
from camctl.persistence.row_history import read_row_values_at_boundary
from camctl.persistence.transaction import row_facts
from . import result_pages


def _one(connection, sql, parameters=()):
    with closing(connection.execute(sql, parameters)) as cursor:
        return cursor.fetchone()


@contextmanager
def _read_transaction(connection):
    opened = not connection.in_transaction
    if opened: connection.execute('BEGIN')
    try:
        yield
    finally:
        if opened: connection.execute('ROLLBACK')


def read_fixed_file(connection, file_id, file_event_id):
    with _read_transaction(connection):
        return _read_fixed_file(connection, file_id, file_event_id)


def _read_fixed_file(connection, file_id, file_event_id):
    """读取文件自身完整事务中的最后事实；只保留这个固定版本。"""
    ObjectId(file_id); ObjectId(file_event_id)
    record = read_event(connection, file_event_id)
    boundary = HistoryBoundary(record.transaction.txn_id, record.transaction.last_event_id)
    return _reuse_fixed_file(connection, file_id, file_event_id, boundary,
                           lambda: _load_fixed_file(connection, file_id, file_event_id))


def read_file_at_boundary(connection, file_id, boundary):
    with _read_transaction(connection):
        return _read_file_at_boundary(connection, file_id, boundary)


def _read_file_at_boundary(connection, file_id, boundary):
    """在完整H定位文件自身最后事实，不从其他页补齐资格。"""
    ObjectId(file_id)
    if not isinstance(boundary, HistoryBoundary):
        raise TypeError("文件读取必须使用完整历史边界")
    transaction = read_transaction(connection, boundary.txn_id)
    if transaction.last_event_id != boundary.last_event_id:
        raise ConsistencyError("文件读取必须使用完整历史边界")
    owner = load_registry()["history_objects"]["device_file"]["id"]
    event = _one(connection, "SELECT MAX(event_id) FROM entity_event_links"
        " WHERE entity_type=? AND entity_id=? AND event_id<=?",
        (owner, file_id, boundary.last_event_id))
    if event is None or event[0] is None:
        raise ConsistencyError("固定边界没有该文件的实际自身事实")
    return read_fixed_file(connection, file_id, event[0])


def _load_fixed_file(db,file_id,file_event_id):
    """恢复独立文件到原固定事实事务；不读取整个动作。"""
    from camctl.contracts.enums import load_registry
    own_transaction=not db.in_transaction
    if own_transaction:db.execute('BEGIN')
    try:
        current=row_facts(db,'device_files',file_id)
        file_type=load_registry()['history_objects']['device_file']['id']
        anchor=_one(db,'SELECT e.transaction_id FROM history_events e JOIN entity_event_links l ON l.event_id=e.id WHERE e.id=? AND l.entity_type=? AND l.entity_id=?',
            (file_event_id,file_type,file_id))
        if current is None or anchor is None:
            raise ConsistencyError('固定文件位置没有同一真实文件的可靠自身历史')
        transaction=read_transaction(db,anchor[0])
        latest=_one(db,'SELECT MAX(event_id) FROM entity_event_links WHERE entity_type=? AND entity_id=? AND event_id<=?',(file_type,file_id,transaction.last_event_id))[0]
        if latest!=file_event_id:
            raise ConsistencyError('固定文件引用不是原完整事务内该文件的最后事实')
        # 独立文件的当前值由自身最后事件固定。使用该事件的完整
        # 事务作为C，避免为每份文件反复核实无关的增长完成事务。
        head=_one(db,'SELECT t.id,t.last_event_id FROM history_events e '
            'JOIN history_transactions t ON t.id=e.transaction_id WHERE e.id=?',
            (current['last_event_id'],))
        if head is None:raise ConsistencyError('文件当前状态缺少自己的可靠最后事件')
        frozen=read_row_values_at_boundary(db,owner=('device_file',file_id),table='device_files',row_id=file_id,
            columns=business_columns('device_files'),current_values=current,
            boundary=HistoryBoundary(transaction.txn_id,transaction.last_event_id),
            current_boundary=HistoryBoundary(head[0],head[1]))
        return {'id':file_id,**frozen,'last_event_id':file_event_id}
    finally:
        if own_transaction:db.execute('ROLLBACK')



def _page_file(connection, page_id, file, boundary, *, run_id, completed=False):
    if page_id > boundary.last_event_id or page_id > file['last_event_id']:
        raise ConsistencyError('文件引用页越过固定文件边界')
    saved, raw = result_pages.read_saved_page(connection, page_id)
    record = read_event(connection, page_id)
    identity = parse_exact_json(file['identity_key'])[2]
    if (record.transaction.last_event_id > boundary.last_event_id
            or raw['run_id'] != run_id or (identity, file['id']) not in saved.file_ids):
        raise ConsistencyError('文件引用页不是同一来源的真实文件')
    if completed:
        entries = [entry for entry in saved.page.entries if entry.identity == identity]
        if (len(entries) != 1 or not entries[0].complete or file['size_bytes'] is None
                or entries[0].size_bytes != file['size_bytes']
                or not json_equal(entries[0].locator, file['locator_json'])):
            raise ConsistencyError('固定完成文件与引用页的完整大小或定位不同')
    return saved


def _completion_page(connection, page, file, boundary):
    evidence = file['completion_evidence_json']
    if file['completion_state'] != int(enum_for('device_files.completion_state').COMPLETE):
        return None
    if not isinstance(evidence, dict) or 'result_page_event_id' not in evidence:
        return None
    page_id = ObjectId(evidence['result_page_event_id'])
    _page_file(connection, page_id, file, boundary, run_id=page.ticket.run_id, completed=True)
    return int(page_id)


def read_result_file_input(connection, page, file_id, boundary, *, pairing_fact_id=None):
    with _read_transaction(connection):
        return _read_result_file_input(connection, page, file_id, boundary, pairing_fact_id=pairing_fact_id)


def _read_result_file_input(connection, page, file_id, boundary, *, pairing_fact_id=None):
    """从已保存归属、完成和配对形成有限输入，未知资格不提升。"""
    if not isinstance(page, ResultPageRef) or not isinstance(boundary, HistoryBoundary):
        raise TypeError('文件读取要求原页引用和完整历史边界')
    ObjectId(file_id)
    if pairing_fact_id is not None: ObjectId(pairing_fact_id)
    saved, raw = result_pages.read_saved_page(connection, page.event_id)
    if saved.ref != page:
        raise ConsistencyError('文件引用改变原页票据、页号或游标')
    record = read_event(connection, page.event_id)
    if record.transaction.last_event_id > boundary.last_event_id:
        raise ConsistencyError('原页位于固定文件边界之后')
    run = row_facts(connection, 'operation_runs', raw['run_id'])
    action = None if run is None else row_facts(connection, 'actions', run['action_id'])
    if (action is None or run['id'] != page.ticket.run_id
            or str(run['activity_id']) != page.ticket.target_id):
        raise ConsistencyError('文件原页没有同一实际流程和活动')
    file = read_file_at_boundary(connection, file_id, boundary)
    identity = parse_exact_json(file['identity_key'])
    if (tuple(identity[:2]) != (action['device_id'], action['driver_id'])
            or (identity[2], file_id) not in saved.file_ids):
        raise ConsistencyError('固定文件与实际原页身份或绑定不同')
    from camctl.devices.segment_profiles import SegmentFileKind, profile_for_policy
    parsed = profile_for_policy(action['execution_spec_json']['result_policy']).classify_file(identity[2])
    role = (SegmentMemberRole.PREVIEW if parsed.kind is SegmentFileKind.PREVIEW else
            SegmentMemberRole.OTHER if parsed.kind is SegmentFileKind.PHOTO else SegmentMemberRole.ORIGINAL)
    actual_role = enum_for('device_files.role')
    wanted_role = int(actual_role.PREVIEW if role is SegmentMemberRole.PREVIEW else actual_role.ORIGINAL)
    owned = (file['source_action_id'] == action['id'] and file['role'] == wanted_role
             and file['ownership_evidence_json'] is not None)
    ownership = file['ownership_evidence_json']
    difference = int(enum_for('device_files.ownership_evidence_json.method').RESULT_PAGE_DIFFERENCE)
    if isinstance(ownership, dict) and ownership.get('method') == difference:
        _page_file(connection, ObjectId(ownership['result_page_event_id']), file, boundary,
                   run_id=run['id'])
    completed_page = _completion_page(connection, page, file, boundary)
    result = SegmentMemberInput(file_id, page.event_id if completed_page is None else completed_page,
        file['last_event_id'], role, OwnershipState.OWNED if owned else OwnershipState.UNKNOWN,
        FileCompletionState.UNKNOWN if completed_page is None else FileCompletionState.COMPLETE,
        pairing_state=(PreviewPairingState.UNKNOWN if role is SegmentMemberRole.PREVIEW
                       else PreviewPairingState.NOT_APPLICABLE))
    if role is not SegmentMemberRole.PREVIEW:
        if pairing_fact_id is not None:
            raise ConsistencyError('原片不能引用预览配对事实')
        return result
    evidence = file['pairing_evidence_json']
    method = int(enum_for('device_files.pairing_evidence_json.method').RESULT_SCAN_PAIRING)
    if pairing_fact_id is None and isinstance(evidence, dict) and evidence.get('method') == method:
        if set(evidence) != {'method', 'pairing_fact_id'}:
            raise ConsistencyError('固定文件的首次扫描配对证据不完整')
        pairing_fact_id = evidence['pairing_fact_id']
    if pairing_fact_id is None:
        return result
    from .result_file_pairing import read_pairing_fact
    fact = read_pairing_fact(connection, pairing_fact_id, file_id, boundary)
    paired = fact['state'] == int(enum_for('device_file_pairing_facts.state').PAIRED)
    paired_identity = None
    if paired:
        if (file['original_device_file_id'] != fact['original_file_id']
                or not isinstance(evidence, dict) or set(evidence) != {'method', 'pairing_fact_id'}
                or evidence['method'] != method):
            raise ConsistencyError('固定文件缺少相同原片的首次扫描配对关联')
        first = fact if evidence['pairing_fact_id'] == pairing_fact_id else read_pairing_fact(
            connection, evidence['pairing_fact_id'], file_id, boundary)
        if (first['state'] != int(enum_for('device_file_pairing_facts.state').PAIRED)
                or first['original_file_id'] != fact['original_file_id']
                or first['created_event_id'] > fact['created_event_id']):
            raise ConsistencyError('配对事实与文件首次正向关联不一致')
        original = read_file_at_boundary(connection, fact['original_file_id'], boundary)
        if original['source_action_id'] != file['source_action_id']:
            raise ConsistencyError('配对文件没有同一实际来源')
        paired_identity = parse_exact_json(original['identity_key'])[2]
    return replace(result, pairing_fact_id=pairing_fact_id, pairing_identity=paired_identity,
        pairing_state=PreviewPairingState.PAIRED if paired else PreviewPairingState.UNPAIRED)


def read_result_file_input_from_reader(page, file_id, boundary, owned, *, pairing_fact_id=None):
    connection = owned.connection
    opened = not connection.in_transaction
    if opened: connection.execute('BEGIN')
    try:
        return read_result_file_input(connection, page, file_id, boundary, pairing_fact_id=pairing_fact_id)
    finally:
        if opened: connection.execute('ROLLBACK')

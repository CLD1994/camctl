"""完整原扫描中的事后配对；原始页不变，事实归属于预览文件。"""
from contextlib import closing
from types import SimpleNamespace

from camctl.capture.files import OwnershipFromResultPageSave, PairResultFilesSave
from camctl.capture.segment_models import FileCompletionState, OwnershipState
from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError
from camctl.history.events import load_event_registry
from camctl.history.reads import CaptureResultReadScope
from camctl.history.validators import EventValidationError, register_guard, validate_event_structure
from camctl.persistence.event_reads import SqlEventReads
from camctl.persistence.read_reuse import reuse_read, typed_read_key
from camctl.persistence.transaction import (
    CommandPlan, TransactionError, event_envelope, next_row_id, read_transaction_range,
    row_change, row_facts, update_change,
)
from . import result_pages as pages
from .result_file_ownership import (
    _guard_boundary, _proof, _request_from_event as ownership_request,
    _saved_range, request_document as ownership_document,
)
from .result_file_proofs import _file_at, _one, load_completion_input

_EVENT = load_event_registry()['events']['DEVICE_FILE_OBSERVED']
_REASON = _EVENT['branches']['PAIR_FROM_RESULT_SCAN']['reason']
_STATE = enum_for('device_file_pairing_facts.state')
_WHY = enum_for('device_file_pairing_facts.reason')
_ROLE = enum_for('result_page_files.family_role')
_METHOD = enum_for('device_files.pairing_evidence_json.method').RESULT_SCAN_PAIRING
_PAIR_COLUMNS = frozenset({'original_device_file_id', 'pairing_evidence_json'})
_FAMILY_WHERE = ('attempt_id=? AND family_directory=? COLLATE BINARY'
    ' AND family_stem=? COLLATE BINARY AND family_role=? AND page_event_id<=?')


def request_document(request):
    page = ownership_document(OwnershipFromResultPageSave(
        request.scan, request.preview_file_id, request.occurred_at))['page']
    return {'preview_file_id': request.preview_file_id, 'scan': page,
            'source_boundary': {'txn_id': request.source_boundary.txn_id,
                                'last_event_id': request.source_boundary.last_event_id}}


def _request(event):
    if set(event.evidence) != {'result_scan_pairing_request'}:
        raise ConsistencyError('扫描配对必须保存唯一完整原申请')
    raw = event.evidence['result_scan_pairing_request']
    if not isinstance(raw, dict) or set(raw) != {'preview_file_id', 'scan', 'source_boundary'}:
        raise ConsistencyError('扫描配对申请成员不完整')
    boundary = raw['source_boundary']
    if not isinstance(boundary, dict) or set(boundary) != {'txn_id', 'last_event_id'}:
        raise ConsistencyError('配对来源必须是完整原边界')
    proxy = SimpleNamespace(occurred_at=event.occurred_at, evidence={
        'ownership_from_result_page_request': {'page': raw['scan'], 'file_id': raw['preview_file_id']}})
    original = ownership_request(proxy)
    result = PairResultFilesSave(original.file_id, original.page,
                                 HistoryBoundary(**boundary), event.occurred_at)
    if not json_equal(raw, request_document(result)):
        raise ConsistencyError('扫描配对申请不是规范完整编码')
    return result


def _validate_page(connection, saved, raw, event, transaction, current_boundary):
    action, _, attempt, _ = pages.committed_page_proof(connection, saved, raw, event, transaction)
    return action, attempt


def _scan(connection, request, current_boundary):
    boundary = request.source_boundary
    transaction = read_transaction_range(connection, boundary.txn_id)
    if transaction.last_event_id != boundary.last_event_id:
        raise ConsistencyError('配对文件来源不是完整边界')
    saved, raw, event, transaction = pages._at_record(connection, request.scan.event_id)
    if saved.ref != request.scan or transaction.last_event_id > boundary.last_event_id:
        raise ConsistencyError('扫描配对改变原末页引用或引用未来原页')
    action, attempt = _validate_page(connection, saved, raw, event, transaction, current_boundary)
    if (not saved.page.scan_complete or not saved.page.set_finalized
            or saved.page.outcome.error is not None or saved.ref.next_cursor is not None):
        return None
    # A1 两入口必经的逐页 guard/seal 已归纳保存连续前缀。本次独立重放
    # 末页 F 前资格及紧邻页，再核其原首点，避免每个 LRF 重扫全目录链。
    return action, attempt


def _family_counts(connection, attempt_id, directory, stem, last):
    result = []
    for role in (_ROLE.ORIGINAL, _ROLE.PREVIEW):
        parameters = (attempt_id, directory, stem, int(role), last)
        count = _one(connection, 'SELECT COUNT(*) FROM (SELECT file_id FROM result_page_files'
            f' WHERE {_FAMILY_WHERE} GROUP BY file_id)', parameters)[0]
        with closing(connection.execute('SELECT file_id FROM result_page_files'
                f' WHERE {_FAMILY_WHERE} GROUP BY file_id ORDER BY file_id LIMIT 2', parameters)) as cursor:
            samples = tuple(row[0] for row in cursor.fetchmany(2))
        result.append((count, samples))
    return result


def _classify(connection, request, *, current_boundary=None):
    """只保留计数、两个端点及一个原页；全部族元信息均须核实。"""
    scan = _scan(connection, request, current_boundary)
    if scan is None:
        return None
    action, attempt = scan
    preview = _file_at(connection, request.preview_file_id, request.source_boundary)
    relation = _one(connection,
        'SELECT family_directory,family_stem,family_role FROM result_page_files'
        ' WHERE run_id=? AND file_id=? AND attempt_id=? AND page_event_id<=?'
        ' ORDER BY page_event_id LIMIT 1',
        (request.scan.ticket.run_id, request.preview_file_id, attempt['id'], request.scan.event_id))
    if relation is None:
        raise ConsistencyError('申请预览不是给定扫描中的真实文件')
    directory, stem, role = relation
    if role is None:
        return None
    if role != int(_ROLE.PREVIEW):
        raise ConsistencyError('扫描配对申请必须指向真实 LRF 预览')
    (original_count, originals), (preview_count, _) = _family_counts(
        connection, attempt['id'], directory, stem, request.scan.event_id)
    if preview_count < 1:
        raise ConsistencyError('实际扫描缺少申请预览的族关系')
    original_id = originals[0] if original_count == 1 else None
    endpoints = {}
    reliable, valid = True, True
    # SQL 对增长族排序，Python 每次只持有一行及一个 <=128 文件的原页。
    with closing(connection.execute(
            'SELECT file_id,page_event_id,identity,family_role FROM result_page_files'
            ' WHERE attempt_id=? AND family_directory=? COLLATE BINARY'
            ' AND family_stem=? COLLATE BINARY AND page_event_id<=?'
            ' ORDER BY page_event_id,id',
            (attempt['id'], directory, stem, request.scan.event_id))) as cursor:
        current_page = None
        for file_id, page_id, identity, family_role in cursor:
            if current_page is None or current_page.ref.event_id != page_id:
                current_page, _ = pages._at(connection, page_id)
            entries = [entry for entry in current_page.page.entries if entry.identity == identity]
            if len(entries) != 1 or (identity, file_id) not in current_page.file_ids:
                raise ConsistencyError('族关系不属于实际原页差集')
            entry = entries[0]
            if pages._family_fields(
                entry.identity,
                result_policy=action["execution_spec_json"].get("result_policy"),
            ) != (directory, stem, family_role):
                raise ConsistencyError('族字段与原观察身份不符')
            member = load_completion_input(connection, current_page.ref, file_id,
                request.source_boundary, current_boundary=current_boundary)
            file = _file_at(connection, file_id, request.source_boundary)
            if (member.ownership is not OwnershipState.OWNED
                    or member.completion is not FileCompletionState.COMPLETE
                    or not entry.complete or entry.size_bytes is None):
                reliable = False
            elif entry.size_bytes != file['size_bytes'] or not json_equal(file['locator_json'], entry.locator):
                raise ConsistencyError('当前族原观察与固定文件大小或定位矛盾')
            elif entry.size_bytes == 0:
                valid = False
            if file_id in (request.preview_file_id, original_id):
                # 同身份重复页保留最早端点；不让后页覆盖未知来源。
                endpoints.setdefault(file_id, (page_id, member.file_event_id))
    if not reliable:
        return None
    why = (_WHY.MISSING if original_count == 0 else
           _WHY.AMBIGUOUS if original_count > 1 or preview_count > 1 else
           _WHY.INVALID if not valid else _WHY.UNIQUE)
    if why == _WHY.UNIQUE and preview['original_device_file_id'] not in (None, original_id):
        why = _WHY.CONFLICT
    original = endpoints.get(original_id)
    preview_endpoint = endpoints.get(request.preview_file_id)
    if preview_endpoint is None or original_id is not None and original is None:
        raise ConsistencyError('族实际端点缺失')
    return action['id'], preview, {
        'preview_file_id': request.preview_file_id, 'original_file_id': original_id,
        'attempt_id': attempt['id'], 'final_page_event_id': request.scan.event_id,
        'source_txn_id': request.source_boundary.txn_id,
        'source_event_id': request.source_boundary.last_event_id,
        'preview_page_event_id': preview_endpoint[0],
        'original_page_event_id': None if original is None else original[0],
        'preview_file_event_id': preview_endpoint[1],
        'original_file_event_id': None if original is None else original[1],
        'original_count': original_count, 'preview_count': preview_count,
        'state': int(_STATE.PAIRED if why == _WHY.UNIQUE else _STATE.UNPAIRED), 'reason': int(why)}


def _rows(fact_id, event_id, preview, values):
    rows = [row_change('device_file_pairing_facts', fact_id,
                       {'created_event_id': event_id, **values})]
    if values['state'] == int(_STATE.PAIRED) and preview['original_device_file_id'] is None:
        if preview['pairing_evidence_json'] is not None:
            raise ConsistencyError('预览关联和配对证据不一致')
        rows.append(update_change('device_files', preview['id'],
            {name: preview[name] for name in _PAIR_COLUMNS},
            {'original_device_file_id': values['original_file_id'], 'pairing_evidence_json': {
                'method': int(_METHOD), 'pairing_fact_id': fact_id}}))
    return tuple(rows)


def _check_event(connection, event, request, *, current_boundary=None):
    classified = _classify(connection, request, current_boundary=current_boundary)
    if classified is None:
        raise ConsistencyError('未知族不能保存配对结论')
    source, preview, values = classified
    if len(event.rows) not in (1, 2) or event.rows[0].table != 'device_file_pairing_facts':
        raise ConsistencyError('配对必须先创建一行事实，首次关联至多额外更新预览')
    expected = _rows(event.rows[0].row_id, event.event_id, preview, values)
    if event.rows != expected:
        raise ConsistencyError('配对事实或首次文件关联不是原扫描的独立分类结果')
    return source, preview, values


def _pairing_guard(event, context):
    reads = context.current_reads
    if type(reads) is not SqlEventReads or not isinstance(reads.scope, CaptureResultReadScope):
        raise EventValidationError('扫描配对缺少实际 SQL 读取端口')
    try:
        request = _request(event)
        boundary = _guard_boundary(reads.connection, event, context.transaction)
        before = pages._page_before_boundary(reads.connection, context.transaction)
        if request.source_boundary != before:
            raise ConsistencyError('配对申请的原文件边界不是本次保存前完整边界')
        source, preview, _ = _check_event(reads.connection, event, request, current_boundary=boundary)
        if reads.scope != pages.capture_result_scope(source, request.scan.ticket, request.scan.event_id):
            raise ConsistencyError('配对读取范围改变原动作、活动、尝试或末页')
        for row in event.rows:
            if context.owners.get((row.table, row.row_id)) != ('device_file', preview['id']):
                raise ConsistencyError('配对事实必须归属原预览文件自身')
        supplied = context.state_rows.get('device_files', {}).get(preview['id'])
        if supplied is None or any(not json_equal(supplied.get(name), preview[name])
                                   for name in _PAIR_COLUMNS | {'source_action_id', 'role'}):
            raise ConsistencyError('配对前态与实际固定文件事实不同')
    except (ConsistencyError, TypeError, ValueError, KeyError) as error:
        raise EventValidationError(str(error)) from error


def register_result_scan_pairing_guard():
    register_guard('result_scan_pairing', _pairing_guard)


class PairResultFilesCommand:
    def __init__(self, request, key):
        if not isinstance(request, PairResultFilesSave):
            raise TypeError('扫描配对要求 PairResultFilesSave')
        self.request, self.key = request, key

    def plan(self, scope):
        connection, request = scope.connection, self.request
        if _saved_range(connection, self.key) is not None:
            return CommandPlan((), {}, {}, result=_verify(request, self.key, connection), read_only=True)
        if pages._latest_history_boundary(connection) != request.source_boundary:
            raise ConsistencyError('新配对申请不能更换原保存前文件边界')
        classified = _classify(connection, request)
        if classified is None:
            return CommandPlan((), {}, {}, result=None, read_only=True)
        source, preview, values = classified
        existing = _one(connection, 'SELECT id FROM device_file_pairing_facts'
            ' WHERE preview_file_id=? AND attempt_id=? AND final_page_event_id=?',
            (request.preview_file_id, values['attempt_id'], request.scan.event_id))
        if existing is not None:
            raise ConsistencyError('同一扫描配对已有原保存责任，必须沿原操作键核实')
        fact_id = next_row_id(connection, 'device_file_pairing_facts')
        allocation = scope.allocate(1)
        rows = _rows(fact_id, allocation.first_event_id, preview, values)
        state = {'device_files': {preview['id']: preview}}
        from .capture import _load_related_outputs
        _load_related_outputs(connection, state, preview['id'])
        event = event_envelope(allocation.first_event_id, allocation.txn_id, _EVENT['id'],
            _REASON, rows, request.occurred_at, {'result_scan_pairing_request': request_document(request)})
        return CommandPlan((event,), {(row.table, row.row_id): ('device_file', preview['id'])
            for row in rows}, state, result=fact_id,
            capture_result_read_scope=pages.capture_result_scope(source, request.scan.ticket, request.scan.event_id))


def _verify(request, key, connection, *, current_boundary=None):
    transaction = _saved_range(connection, key)
    if transaction is None:
        return None
    if transaction.first_event_id != transaction.last_event_id:
        raise TransactionError('原键不属于独立配对事务')
    stored = _one(connection, f'SELECT {pages._COLUMNS} FROM history_events WHERE id=?',
                  (transaction.first_event_id,))
    event = pages.decode_event_row(stored)
    validate_event_structure(event)
    if (event.event_type, event.reason, event.transaction_id) != (_EVENT['id'], _REASON, transaction.txn_id):
        raise TransactionError('原键属于其他完整事务')
    original = _request(event)
    if original != request or not json_equal(request_document(original), request_document(request)):
        raise TransactionError('原键配对申请、边界或事实时刻不同')
    if request.source_boundary != pages._page_before_boundary(connection, transaction):
        raise ConsistencyError('原配对没有固定其实际保存前边界')
    _check_event(connection, event, request, current_boundary=current_boundary)
    fact_id = event.rows[0].row_id
    fact = row_facts(connection, 'device_file_pairing_facts', fact_id)
    if fact is None or not json_equal({name: fact[name] for name in event.rows[0].after.values},
                                      dict(event.rows[0].after.values)):
        raise ConsistencyError('原配对事实投影与完整历史不同')
    return fact_id


def verify_result_file_pairing(request, key, reader, *, execution_finished=True):
    if not isinstance(request, PairResultFilesSave):
        raise TypeError('配对核实必须保持完整原申请')
    if execution_finished is not True:
        raise ConsistencyError('原配对保存仍在途，不能核实为可靠缺键')
    connection = reader.connection
    opened = not connection.in_transaction
    if opened:
        connection.execute('BEGIN')
    try:
        return _verify(request, key, connection)
    finally:
        if opened:
            connection.execute('ROLLBACK')


def verify_pairing_fact(connection, fact_id, preview_file_id, boundary, *, current_boundary=None):
    """只核已有原事实及原操作键，绝不执行新的配对保存。"""
    key = (__name__, "pairing-fact", typed_read_key(fact_id), typed_read_key(preview_file_id),
           typed_read_key(boundary), typed_read_key(current_boundary))
    return reuse_read(connection, key, lambda: _load_verified_pairing_fact(
        connection, fact_id, preview_file_id, boundary, current_boundary=current_boundary))


def _load_verified_pairing_fact(connection, fact_id, preview_file_id, boundary, *, current_boundary=None):
    fact = row_facts(connection, 'device_file_pairing_facts', fact_id)
    if (fact is None or fact['preview_file_id'] != preview_file_id
            or fact['created_event_id'] > boundary.last_event_id
            or fact['source_event_id'] >= fact['created_event_id']):
        raise ConsistencyError('配对事实不存在、属于其他预览或越过固定边界')
    anchor = _one(connection, 'SELECT t.operation_key,t.last_event_id FROM history_events e'
        ' JOIN history_transactions t ON t.id=e.transaction_id WHERE e.id=?',
        (fact['created_event_id'],))
    if anchor is None or anchor[1] > boundary.last_event_id:
        raise ConsistencyError('配对事实没有边界内完整创建事务')
    stored = _one(connection, f'SELECT {pages._COLUMNS} FROM history_events WHERE id=?',
                  (fact['created_event_id'],))
    request = _request(pages.decode_event_row(stored))
    if _verify(request, anchor[0], connection, current_boundary=current_boundary) != fact_id:
        raise ConsistencyError('实际配对事实不是原完整事务的唯一事实')
    return fact

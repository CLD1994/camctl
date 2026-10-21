"""原结果页差集的独立文件归属；不保存配对、完成或产物。"""

from contextlib import closing
from types import SimpleNamespace

from camctl.capture.files import (
    ObservationDisposition, ObservationOutcome, OwnershipFromResultPageSave,
    file_identity_key,
)
from camctl.capture.result_pages import ResultPageRef
from camctl.devices.tasks import CaptureResultPolicy
from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError
from camctl.devices.directory import DirectoryCursor
from camctl.history.events import load_event_registry
from camctl.history.validators import EventValidationError, register_guard, validate_event_structure
from camctl.operations.models import AttemptTicket
from camctl.persistence.event_reads import SqlEventReads
from camctl.history.reads import CaptureResultReadScope
from camctl.persistence.row_history import read_row_values_at_boundary
from camctl.persistence.transaction import (
    CommandPlan, TransactionError, event_envelope, read_transaction_range,
    row_facts, update_change,
)
from . import result_pages as pages

_EVENT = load_event_registry()['events']['DEVICE_FILE_OBSERVED']
_REASON = _EVENT['branches']['OWNERSHIP_FROM_RESULT_PAGE']['reason']
_ROLE = enum_for('device_files.role')
_METHOD = enum_for('device_files.ownership_evidence_json.method').RESULT_PAGE_DIFFERENCE
_COLUMNS = frozenset({'source_action_id', 'ownership_evidence_json', 'role'})
_PAIRS = frozenset({'original_device_file_id', 'pairing_evidence_json'})


def request_document(request):
    """完整原申请只有一个编码来源，事实时刻由事件信封保存。"""
    ref, ticket = request.page, request.page.ticket
    return {'page': {'ticket': {'run_id': ticket.run_id,
        'attempt_id': ticket.attempt_id, 'operation': ticket.operation,
        'target_id': ticket.target_id, 'responsibility_key': ticket.responsibility_key},
        'page_no': ref.page_no, 'event_id': ref.event_id,
        'next_cursor': None if ref.next_cursor is None else ref.next_cursor.as_json()},
        'file_id': request.file_id}


def _request_from_event(event):
    if set(event.evidence) != {'ownership_from_result_page_request'}:
        raise ConsistencyError('原页归属事件缺少唯一完整申请')
    raw = event.evidence['ownership_from_result_page_request']
    if not isinstance(raw, dict) or set(raw) != {'page', 'file_id'}:
        raise ConsistencyError('原页归属申请成员不完整')
    ref = raw['page']
    if not isinstance(ref, dict) or set(ref) != {'ticket', 'page_no', 'event_id', 'next_cursor'}:
        raise ConsistencyError('原页归属必须保存完整原引用')
    ticket = ref['ticket']
    if not isinstance(ticket, dict) or set(ticket) != {
            'run_id', 'attempt_id', 'operation', 'target_id', 'responsibility_key'}:
        raise ConsistencyError('原页归属必须保存完整原票据')
    cursor = None if ref['next_cursor'] is None else DirectoryCursor.from_json(ref['next_cursor'])
    request = OwnershipFromResultPageSave(ResultPageRef(AttemptTicket(**ticket),
        ref['page_no'], ref['event_id'], cursor), raw['file_id'], event.occurred_at)
    if not json_equal(raw, request_document(request)):
        raise ConsistencyError('原页归属申请不是规范完整编码')
    return request


def _proof(connection, request, *, current_boundary=None):
    """独立核原页、完整 INDEX 及原 F 前差集，不采用调用方行切片。"""
    ref = request.page
    saved, raw, event, transaction = pages._at_record(connection, ref.event_id)
    if saved.ref != ref:
        raise ConsistencyError('归属引用改变原页票据、页号或游标')
    run, actual_attempt, action, activity = pages._load(connection, ref.ticket)
    first, last = (actual_attempt['result_first_page_event_id'],
                   actual_attempt['result_last_page_event_id'])
    if first is None or last is None or not first <= ref.event_id <= last:
        raise ConsistencyError('归属原页不在真实尝试可靠页范围')
    if action['execution_spec_json'].get('result_policy') not in {
            item.value for item in CaptureResultPolicy}:
        raise ConsistencyError('原动作没有声明 OSMO 结果策略')
    old_action, old_activity, attempt, old_run = pages.committed_page_proof(
        connection, saved, raw, event, transaction)
    if (old_action['id'] != action['id'] or old_activity['id'] != activity['id']
            or old_run['id'] != run['id'] or attempt['id'] != actual_attempt['id']
            or (old_action['device_id'], old_action['driver_id'])
                != (action['device_id'], action['driver_id'])):
        raise ConsistencyError('原页实际尝试、活动或固定绑定发生矛盾')
    identities = [identity for identity, file_id in saved.file_ids if file_id == request.file_id]
    if len(identities) != 1:
        raise ConsistencyError('文件不是原结果页真实差集成员')
    entries = [entry for entry in saved.page.entries if entry.identity == identities[0]]
    if len(entries) != 1:
        raise ConsistencyError('原差集文件没有唯一实际观察')
    entry = entries[0]
    suffix = entry.identity.rpartition('.')[2].lower()
    if (suffix not in {'osv', 'lrf'} or not isinstance(entry.format_id, str)
            or entry.format_id.lower() != suffix):
        raise ConsistencyError('原观察格式与 OSV/LRF 文件后缀不一致')
    role = int(_ROLE.ORIGINAL if suffix == 'osv' else _ROLE.PREVIEW)
    file = row_facts(connection, 'device_files', request.file_id)
    if (file is None or file['identity_key'] != file_identity_key(
            action['device_id'], action['driver_id'], entry.identity)):
        raise ConsistencyError('差集文件与原设备绑定的实际稳定身份不符')
    return action['id'], role, file


def _after(request, source, role):
    return {'source_action_id': source, 'ownership_evidence_json': {
        'method': int(_METHOD), 'result_page_event_id': request.page.event_id}, 'role': role}


def _unknown(file):
    return (file['source_action_id'] is None and file['ownership_evidence_json'] is None
        and file['role'] == int(_ROLE.UNDETERMINED)
        and all(file[name] is None for name in _PAIRS))


def _guard_boundary(connection, event, transaction):
    """区分有限入口完整 C 与 Bounded 尚未写入的单事件 header。"""
    if (event.transaction_id != transaction.txn_id
            or event.event_id != transaction.first_event_id
            or event.event_id != transaction.last_event_id):
        raise ConsistencyError('原页归属只允许独立单事件完整组')
    with closing(connection.execute('SELECT id,first_event_id,last_event_id'
            ' FROM history_transactions ORDER BY id DESC LIMIT 1')) as cursor:
        latest = cursor.fetchone()
    with closing(connection.execute('SELECT MAX(id) FROM history_events')) as cursor:
        last = cursor.fetchone()[0]
    if latest is None or last != event.event_id - 1:
        raise ConsistencyError('归属前态不是实际末事件的完整投影')
    pending = latest[0] == transaction.txn_id
    if pending and latest != (transaction.txn_id, event.event_id, event.event_id):
        raise ConsistencyError('实际分配 header 不是独立单事件归属')
    prior_id = transaction.txn_id - 1
    if not pending and latest[0] != prior_id:
        raise ConsistencyError('归属范围没有紧邻真实完整历史')
    prior = read_transaction_range(connection, prior_id)
    if prior.last_event_id != last:
        raise ConsistencyError('归属前一事务没有完整覆盖真实末事件')
    return HistoryBoundary(prior.txn_id, prior.last_event_id) if pending else None


def _check_row(event, request, before, source, role):
    if (len(event.rows) != 1 or event.rows[0].table != 'device_files'
            or event.rows[0].row_id != request.file_id):
        raise ConsistencyError('原页归属事件只能更新申请中的一个设备文件')
    row = event.rows[0]
    if (not row.before.exists or not row.after.exists or not _unknown(before)
            or dict(row.before.values) != {name: before[name] for name in _COLUMNS}
            or not json_equal(dict(row.after.values), _after(request, source, role))):
        raise ConsistencyError('原页归属前后值必须精确固定来源、证据及用途，配对保持空')


def _ownership_from_result_page_guard(event, context):
    reads = context.current_reads
    if type(reads) is not SqlEventReads or not isinstance(reads.scope, CaptureResultReadScope):
        raise EventValidationError('原页归属缺少原事务的实际 SQL 读取端口')
    try:
        request = _request_from_event(event)
        boundary = _guard_boundary(reads.connection, event, context.transaction)
        if request.page.event_id >= event.event_id:
            raise ConsistencyError('归属必须引用更早的可靠原页')
        source, role, file = _proof(reads.connection, request, current_boundary=boundary)
        if reads.scope != pages.capture_result_scope(source, request.page.ticket, request.page.event_id):
            raise ConsistencyError('归属读取范围改变原动作、活动、尝试或页上界')
        supplied = context.state_rows.get('device_files', {}).get(request.file_id)
        if (supplied is None or any(name not in supplied or not json_equal(supplied[name], file[name])
                for name in _COLUMNS | _PAIRS | {'identity_key', 'observer_action_id'})
                or context.owners.get(('device_files', request.file_id)) != ('device_file', request.file_id)):
            raise ConsistencyError('原页归属的调用方文件前态或独立owner与实际SQL不同')
        _check_row(event, request, file, source, role)
    except (ConsistencyError, TypeError, ValueError) as error:
        raise EventValidationError(str(error)) from error


def register_ownership_from_result_page_guard():
    register_guard('ownership_from_result_page', _ownership_from_result_page_guard)


class _FileOwnershipFromResultPageCommand:
    def __init__(self, request, key):
        if not isinstance(request, OwnershipFromResultPageSave):
            raise TypeError('原页归属必须使用 OwnershipFromResultPageSave')
        self.request, self.key = request, key

    def plan(self, scope):
        connection, request = scope.connection, self.request
        if _saved_range(connection, self.key) is not None:
            result = _verify(request, self.key, connection)
            return CommandPlan((), {}, {}, result=result, read_only=True)
        source, role, file = _proof(connection, request)
        if not _unknown(file):
            if (file['source_action_id'] == source and file['role'] == role
                    and file['ownership_evidence_json'] is not None):
                return CommandPlan((), {}, {}, read_only=True,
                    result=ObservationOutcome(ObservationDisposition.ALREADY, request.file_id))
            raise ConsistencyError('文件来源或用途已经固定，不能改指')
        state = {'device_files': {request.file_id: file}}
        from .capture import _load_related_outputs
        _load_related_outputs(connection, state, request.file_id)
        allocation = scope.allocate(1)
        row = update_change('device_files', request.file_id,
            {name: file[name] for name in _COLUMNS}, _after(request, source, role))
        event = event_envelope(allocation.first_event_id, allocation.txn_id,
            _EVENT['id'], _REASON, (row,), request.occurred_at,
            {'ownership_from_result_page_request': request_document(request)})
        return CommandPlan((event,), {('device_files', request.file_id): ('device_file', request.file_id)},
            state, result=ObservationOutcome(ObservationDisposition.SAVED, request.file_id),
            capture_result_read_scope=pages.capture_result_scope(source, request.page.ticket, request.page.event_id))


def _saved_range(connection, key):
    with closing(connection.execute('SELECT id FROM history_transactions WHERE operation_key=?',
                                   (str(key),))) as cursor:
        found = cursor.fetchone()
    return None if found is None else read_transaction_range(connection, found[0])


def _verify(request, key, connection):
    transaction = _saved_range(connection, key)
    if transaction is None:
        return None
    if transaction.first_event_id != transaction.last_event_id:
        raise TransactionError('原操作键不属于独立单事件归属事务')
    event_id = transaction.first_event_id
    with closing(connection.execute(f'SELECT {pages._COLUMNS} FROM history_events WHERE id=?',
                                   (event_id,))) as cursor:
        stored = cursor.fetchone()
    event = pages.decode_event_row(stored)
    validate_event_structure(event)
    if (event.event_type, event.reason, event.transaction_id) != (_EVENT['id'], _REASON, transaction.txn_id):
        raise TransactionError('原操作键属于其他完整事务')
    original = _request_from_event(event)
    if (request.occurred_at != original.occurred_at
            or not json_equal(request_document(request), request_document(original))):
        raise TransactionError('原键归属的完整申请或事实时刻不同')
    source, role, file = _proof(connection, request)
    restored = read_row_values_at_boundary(connection, owner=('device_file', request.file_id),
        table='device_files', row_id=request.file_id, columns=_COLUMNS | _PAIRS,
        current_values=file, boundary=pages._page_before_boundary(connection, transaction),
        current_boundary=pages._latest_history_boundary(connection), validated_transaction=transaction)
    _check_row(event, request, restored, source, role)
    return ObservationOutcome(ObservationDisposition.ALREADY, request.file_id)


def verify_ownership_from_result_page(request, key, reader, *, execution_finished=True):
    """原工作收场后只读核原完整组；缺键与读取失败分别处理。"""
    if not isinstance(request, OwnershipFromResultPageSave):
        raise TypeError('原页归属核实要求完整原申请')
    if execution_finished is not True:
        raise ConsistencyError('原保存仍可能在途，不能核实为可靠缺键')
    connection = reader.connection
    opened = not connection.in_transaction
    if opened:
        connection.execute('BEGIN')
    try:
        return _verify(request, key, connection)
    finally:
        if opened:
            connection.execute('ROLLBACK')

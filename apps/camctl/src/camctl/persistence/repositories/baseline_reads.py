"""基准目录调用原始输入的保存、独立核对及同边界读取。"""
from contextlib import closing
from dataclasses import replace
from decimal import Decimal

from camctl.capture.baseline_models import (
    BaselineReadCallView, BaselineReadIntent, BaselineRef, BaselineState)
from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import HistoryBoundary, INITIAL_BOUNDARY
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError, ObjectId, OperationKey
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryCursor, DirectoryRequest
from camctl.history.events import load_event_registry
from camctl.history.validators import EventValidationError, register_guard, validate_event_structure
from camctl.persistence.row_history import read_row_values_at_boundary
from camctl.persistence.transaction import (
    CommandPlan, row_change, event_envelope, read_transaction_range,
    row_facts, saved_transaction_events)

from .baseline import _activity, _directories, _event_at

_EVENT = load_event_registry()['events']['BASELINE_READ']
_STATE = enum_for('baseline_read_calls.state')


def _one(connection, sql, parameters=()):
    with closing(connection.execute(sql, parameters)) as cursor:
        return cursor.fetchone()


def _request_json(request):
    ref = request.baseline_ref
    return {'action_id': request.action_id, 'activity_id': request.activity_id,
        'device_id': request.binding.device_id, 'driver_id': request.binding.driver_id,
        'directories': list(request.directories),
        'cursor': None if request.cursor is None else request.cursor.as_json(),
        'timeout_s': request.timeout_s,
        'baseline_first_event_id': ref.first_event_id, 'baseline_last_event_id': ref.last_event_id,
        'chunk_count': ref.chunk_count, 'entry_count': ref.entry_count, 'read_no': request.read_no,
        'call_key': str(request.call_key), 'result_save_key': str(request.result_save_key),
        'occurred_at': request.occurred_at}


def _decode_request(raw):
    binding = DeviceBinding(raw['device_id'], raw['driver_id'])
    ref = BaselineRef(raw['activity_id'], BaselineState.COLLECTING,
        raw['baseline_first_event_id'], raw['baseline_last_event_id'],
        raw['chunk_count'], raw['entry_count'])
    request = BaselineReadIntent(raw['action_id'], raw['activity_id'], binding,
        tuple(raw['directories']), None if raw['cursor'] is None else DirectoryCursor.from_json(raw['cursor']),
        Decimal(raw['timeout_s']), ref, raw['read_no'], OperationKey(raw['call_key']),
        OperationKey(raw['result_save_key']), raw['occurred_at'])
    if not json_equal(raw, _request_json(request)):
        raise ConsistencyError('基准调用证据不是完整的原输入')
    return request


def _intent_values(request, event_id):
    raw = _request_json(request)
    return {'created_event_id': event_id, 'action_id': request.action_id,
        'activity_id': request.activity_id, 'call_key': str(request.call_key),
        'result_save_key': str(request.result_save_key), 'device_id': request.binding.device_id,
        'driver_id': request.binding.driver_id, 'directories_json': raw['directories'],
        'cursor_json': raw['cursor'], 'timeout_s_json': request.timeout_s,
        **{name: raw[name] for name in ('baseline_first_event_id', 'baseline_last_event_id',
                                      'chunk_count', 'entry_count', 'read_no')},
        'state': int(_STATE.INTENDED), 'returned_at': None, 'result_event_id': None,
        'error_json': None, 'next_cursor_json': None, 'chunk_event_id': None}


def _check_intent(connection, request, transaction_id=None):
    activity, action = _activity(connection, request.activity_id)
    expected_directories = DirectoryRequest(DeviceBinding(action['device_id'], action['driver_id']),
        tuple(_directories(activity)), None, 128, request.timeout_s).directories
    if (request.action_id != action['id'] or request.binding != DeviceBinding(action['device_id'], action['driver_id'])
            or request.directories != expected_directories
            or action['status'] != enum_for('actions.status').RUNNING or action['execution_started'] != 1
            or activity['baseline_state'] != BaselineState.COLLECTING
            or activity['dispatch_state'] != enum_for('device_activities.dispatch_state').NOT_DISPATCHED
            or activity['occupancy_state'] != enum_for('device_activities.occupancy_state').HELD):
        raise ConsistencyError('基准调用不属于原动作正在占用的未派发收集活动')
    previous = _one(connection, 'SELECT id FROM baseline_read_calls WHERE activity_id=?'
        ' ORDER BY read_no DESC LIMIT 1', (request.activity_id,))
    if previous is not None or request.read_no != 1:
        raise ConsistencyError('首次基准调用必须建立新链，后续调用须沿实际返回继续')
    if (activity['baseline_first_event_id'] is not None or activity['baseline_last_event_id'] is not None
            or request.baseline_ref != BaselineRef(request.activity_id, BaselineState.COLLECTING,
                                                 None, None, 0, 0)):
        raise ConsistencyError('首次基准调用必须引用原活动的空基准前缀')
    for key in (request.call_key, request.result_save_key):
        if (_one(connection, 'SELECT id FROM baseline_read_calls WHERE call_key=? OR result_save_key=?',
                 (str(key), str(key))) is not None
                or _one(connection, 'SELECT id FROM history_transactions WHERE operation_key=? AND id<>?',
                        (str(key), transaction_id or 0)) is not None):
            raise ConsistencyError('基准调用的原始双键已经被其他责任采用')
    return activity, action


def _current_boundary(connection):
    row = _one(connection, 'SELECT id,last_event_id FROM history_transactions ORDER BY id DESC LIMIT 1')
    return INITIAL_BOUNDARY if row is None else HistoryBoundary(*row)


def _read_call(connection, call_id, boundary):
    ObjectId(call_id)
    if not isinstance(boundary, HistoryBoundary):
        raise ValueError('基准调用读取需要完整历史边界')
    current = _current_boundary(connection)
    if boundary.last_event_id > current.last_event_id:
        raise ConsistencyError('基准调用读取边界晚于实际状态')
    if boundary != INITIAL_BOUNDARY:
        transaction = read_transaction_range(connection, boundary.txn_id)
        if transaction.last_event_id != boundary.last_event_id:
            raise ConsistencyError('基准调用读取边界不是完整事务')
    row = row_facts(connection, 'baseline_read_calls', call_id)
    if row is None or row['created_event_id'] > boundary.last_event_id:
        return None
    columns = frozenset(load_event_registry()['tables']['baseline_read_calls']['immutable']
                        + load_event_registry()['tables']['baseline_read_calls']['mutable'])
    values = read_row_values_at_boundary(connection, owner=('action', row['action_id']),
        table='baseline_read_calls', row_id=call_id, columns=columns, current_values=row,
        boundary=boundary, current_boundary=current)
    event = _event_at(connection, values['created_event_id'])
    transaction = read_transaction_range(connection, event.transaction_id)
    if (validate_event_structure(event)[:2] != ('BASELINE_READ', 'INTEND')
            or transaction.first_event_id != event.event_id or transaction.last_event_id != event.event_id
            or len(event.rows) != 1 or event.rows[0].row_id != call_id):
        raise ConsistencyError('基准调用没有原独立意图事务')
    request = _decode_request(event.evidence['baseline_read_request'])
    original = _intent_values(request, event.event_id)
    if any(not json_equal(values[name], original[name])
           for name in load_event_registry()['tables']['baseline_read_calls']['immutable']):
        raise ConsistencyError('基准调用当前固定输入与原意图不一致')
    if _STATE(values['state']) is not _STATE.INTENDED:
        raise ConsistencyError('基准调用实际结果须由返回读取入口解释')
    return BaselineReadCallView(call_id, event.event_id, _STATE.INTENDED, request,
        None, None, None, None, None, HistoryBoundary(transaction.txn_id, transaction.last_event_id))


def read_call(call_id, boundary, owned):
    connection = owned.connection
    begun = not connection.in_transaction
    if begun:
        connection.execute('BEGIN')
    try:
        return _read_call(connection, call_id, boundary)
    finally:
        if begun:
            connection.execute('ROLLBACK')


class SaveBaselineReadIntent:
    def __init__(self, request, key):
        if not isinstance(request, BaselineReadIntent):
            raise TypeError('基准调用意图需要完整原始输入')
        self.request, self.key = request, OperationKey(key)

    def plan(self, scope):
        request = self.request
        if self.key != request.call_key:
            raise ConsistencyError('基准调用意图必须使用原call_key保存')
        saved = saved_transaction_events(scope.connection, self.key)
        if saved is not None:
            if (len(saved) != 1 or saved[0]['type'] != _EVENT['id'] or saved[0]['reason'] != 1
                    or saved[0]['occurred_at'] != request.occurred_at
                    or not json_equal(saved[0]['body']['evidence'], {'baseline_read_request': _request_json(request)})):
                raise ConsistencyError('基准调用原键的完整输入或时刻不同')
            event = saved[0]
            call_id = event['body']['rows'][0]['id']
            transaction = event['transaction']
            view = _read_call(scope.connection, call_id,
                HistoryBoundary(transaction.txn_id, transaction.last_event_id))
            if view is None or view.request != request:
                raise ConsistencyError('基准调用原键不属于原始申请')
            return CommandPlan((), {}, {}, result=view, read_only=True)
        activity, action = _check_intent(scope.connection, request)
        allocation = scope.allocate(1)
        call_id = _one(scope.connection, 'SELECT COALESCE(MAX(id),0)+1 FROM baseline_read_calls')[0]
        ObjectId(call_id)
        values = _intent_values(request, allocation.first_event_id)
        event = event_envelope(allocation.first_event_id, allocation.txn_id, _EVENT['id'], 1,
            (row_change('baseline_read_calls', call_id, values),), request.occurred_at,
            {'baseline_read_request': _request_json(request)})
        owner = ('action', request.action_id)
        state = {'device_activities': {activity['id']: activity}, 'actions': {action['id']: action}}
        view = BaselineReadCallView(call_id, event.event_id, _STATE.INTENDED, request,
            None, None, None, None, None, HistoryBoundary(event.transaction_id, event.event_id))
        return CommandPlan((event,), {('baseline_read_calls', call_id): owner}, state, result=view)


class _IntentReads:
    def __init__(self, connection, event, transaction, operation_key):
        self.connection, self.event = connection, event
        self.transaction, self.operation_key = transaction, operation_key

    def validate(self, event, context):
        if (replace(event, change_seq=self.event.change_seq) != self.event
                or context.transaction != self.transaction or not self.connection.in_transaction
                or self.transaction.first_event_id != event.event_id
                or self.transaction.last_event_id != event.event_id):
            raise ConsistencyError('基准调用意图必须由原内核在独立事务内核对')
        request = _decode_request(event.evidence['baseline_read_request'])
        if self.operation_key != request.call_key or event.occurred_at != request.occurred_at:
            raise ConsistencyError('基准调用意图使用了其他原键或时刻')
        _check_intent(self.connection, request, self.transaction.txn_id)
        if (len(event.rows) != 1 or event.rows[0].table != 'baseline_read_calls'
                or event.rows[0].before.exists or not event.rows[0].after.exists
                or not json_equal(event.rows[0].after.values, _intent_values(request, event.event_id))):
            raise ConsistencyError('基准调用意图没有精确保存完整原输入')


def bind_intent(context, event, transaction, connection, operation_key):
    if context.baseline_read_reads is not None:
        raise ConsistencyError('基准调用独立读取端口由原事务内核提供')
    if event.event_type == _EVENT['id'] and event.reason == 1:
        return replace(context, baseline_read_reads=_IntentReads(connection, event, transaction, operation_key))
    return context


def baseline_read_guard(event, context):
    try:
        if event.reason != 1 or type(context.baseline_read_reads) is not _IntentReads:
            raise ConsistencyError('基准调用意图缺少同连接独立读取')
        context.baseline_read_reads.validate(event, context)
    except (KeyError, TypeError, ValueError) as error:
        raise EventValidationError(f'基准调用事实不符合原始输入: {error}') from error


def register_baseline_read_guard():
    register_guard('baseline_read', baseline_read_guard)

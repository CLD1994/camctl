"""基准目录调用原始输入的保存、独立核对及同边界读取。"""
from contextlib import closing, contextmanager
from dataclasses import replace
from decimal import Decimal

from camctl.capture.baseline_models import (
    BaselineCallsCursor, BaselineChunkSave, BaselineReadCallView, BaselineReadIntent, BaselineReadReturn,
    BaselineReadUnconfirmedSave, BaselineRef, BaselineState)
from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import HistoryBoundary, INITIAL_BOUNDARY
from camctl.contracts.json_values import json_equal
from camctl.contracts.pages import Page
from camctl.contracts.values import ConsistencyError, ObjectId, OperationKey
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryCursor, DirectoryRequest
from camctl.history.events import load_event_registry
from camctl.history.reads import BaselineReadWriteScope
from camctl.history.validators import EventValidationError, register_guard, validate_event_structure
from camctl.persistence.row_history import read_row_values_at_boundary
from camctl.persistence.transaction import (
    CommandPlan, TransactionScope, row_change, event_envelope, read_transaction_range,
    row_facts, saved_transaction_events, update_change)
from camctl.operations.models import ErrorValue
from camctl.operations.result_format import error_document

from .baseline import (_activity, _check_entries, _chunk_at, _decode_chunk,
    _directories, _event_at, _tail_read, append_event)

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
            or (action['status'], action['execution_started']) not in ((1, 0), (2, 1))
            or action['cancel_requested'] != 0
            or activity['baseline_state'] != BaselineState.COLLECTING
            or activity['dispatch_state'] != enum_for('device_activities.dispatch_state').NOT_DISPATCHED
            or activity['occupancy_state'] != enum_for('device_activities.occupancy_state').HELD):
        raise ConsistencyError('基准调用不属于原动作正在占用的未派发收集活动')
    previous = _one(connection, 'SELECT id FROM baseline_read_calls WHERE activity_id=?'
        ' ORDER BY read_no DESC LIMIT 1', (request.activity_id,))
    if previous is None:
        if (request.read_no != 1 or activity['baseline_first_event_id'] is not None
                or activity['baseline_last_event_id'] is not None
                or request.baseline_ref != BaselineRef(request.activity_id,
                    BaselineState.COLLECTING, None, None, 0, 0)):
            raise ConsistencyError('首次基准调用必须引用原活动的空基准前缀')
    else:
        view = _read_call(connection, previous[0], _current_boundary(connection))
        if (view.state is not _STATE.RETURNED or view.error is not None or view.next_cursor is None
                or request.read_no != view.request.read_no + 1 or request.cursor != view.next_cursor):
            raise ConsistencyError('后续基准调用必须沿原成功返回的继续游标及连续序号')
        ref = view.request.baseline_ref
        if view.chunk_event_id is not None:
            chunk = _chunk_at(connection, view.chunk_event_id, request.activity_id)
            ref = BaselineRef(request.activity_id, BaselineState.COLLECTING,
                ref.first_event_id or chunk.event_id, chunk.event_id,
                ref.chunk_count + 1, ref.entry_count + len(chunk.entries))
        if (request.baseline_ref != ref or (activity['baseline_first_event_id'],
                activity['baseline_last_event_id']) != (ref.first_event_id, ref.last_event_id)):
            raise ConsistencyError('后续基准调用没有保留上一实际返回形成的原前缀')
    for key in (request.call_key, request.result_save_key):
        if (_one(connection, 'SELECT id FROM baseline_read_calls WHERE call_key=? OR result_save_key=?',
                 (str(key), str(key))) is not None
                or _one(connection, 'SELECT id FROM history_transactions WHERE operation_key=? AND id<>?',
                        (str(key), transaction_id or 0)) is not None):
            raise ConsistencyError('基准调用的原始双键已经被其他责任采用')
    return activity, action


def _current_boundary(connection):
    row = _one(connection, 'SELECT t.id,t.last_event_id FROM history_transactions t'
        ' JOIN history_events h ON h.id=t.last_event_id ORDER BY t.id DESC LIMIT 1')
    return INITIAL_BOUNDARY if row is None else HistoryBoundary(*row)


def _read_boundary(connection,boundary):
    if not isinstance(boundary, HistoryBoundary):
        raise ValueError('基准调用读取需要完整历史边界')
    current = _current_boundary(connection)
    if boundary.last_event_id > current.last_event_id:
        raise ConsistencyError('基准调用读取边界晚于实际状态')
    if boundary != INITIAL_BOUNDARY:
        transaction = read_transaction_range(connection, boundary.txn_id)
        if transaction.last_event_id != boundary.last_event_id:
            raise ConsistencyError('基准调用读取边界不是完整事务')
    return current


def _read_call(connection, call_id, boundary):
    ObjectId(call_id)
    current=_read_boundary(connection,boundary)
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
    state = _STATE(values['state'])
    if state is _STATE.INTENDED:
        if any(values[name] is not None for name in ('result_event_id','returned_at',
                                                   'error_json','next_cursor_json','chunk_event_id')):
            raise ConsistencyError('基准意图包含尚未返回的结果')
        return BaselineReadCallView(call_id, event.event_id, state, request,
            None, None, None, None, None, HistoryBoundary(transaction.txn_id, transaction.last_event_id))
    if state is _STATE.UNCONFIRMED:
        result=_event_at(connection,values['result_event_id'])
        result_tx=read_transaction_range(connection,result.transaction_id)
        expected=_unconfirmed_event(call_id,result.occurred_at,result.event_id,result.transaction_id)
        if (replace(result,change_seq=None) != expected or result_tx.first_event_id != result.event_id
                or result_tx.last_event_id != result.event_id
                or any(values[name] is not None for name in
                    ('returned_at','error_json','next_cursor_json','chunk_event_id'))):
            raise ConsistencyError('基准返回未知没有原独立结果事务')
        return BaselineReadCallView(call_id,event.event_id,state,request,result.event_id,
            None,None,None,None,HistoryBoundary(result_tx.txn_id,result.event_id))
    result = _event_at(connection, values['result_event_id'])
    result_tx = read_transaction_range(connection, result.transaction_id)
    if (validate_event_structure(result)[:2] != ('BASELINE_READ','RETURN')
            or result_tx.last_event_id != result.event_id or result.occurred_at != values['returned_at']
            or len(result.rows) != 1 or result.rows[0].row_id != call_id):
        raise ConsistencyError('基准实际返回没有完整原结果组')
    raw = {'call_id':call_id,'returned_at':values['returned_at'],'error':values['error_json'],
           'next_cursor':values['next_cursor_json'],'chunk_event_id':values['chunk_event_id']}
    if not json_equal(result.evidence, {'baseline_read_request':raw}):
        raise ConsistencyError('基准实际返回与原结果证据不同')
    expected = {'state':int(_STATE.RETURNED),'result_event_id':result.event_id,
        **{name:values[name] for name in ('returned_at','error_json','next_cursor_json','chunk_event_id')}}
    before = {name:original[name] for name in expected if not json_equal(original[name],expected[name])}
    after = {name:expected[name] for name in before}
    if (not json_equal(result.rows[0].before.values,before)
            or not json_equal(result.rows[0].after.values,after)):
        raise ConsistencyError('基准实际返回没有从原意图一次固定结果')
    error = None if values['error_json'] is None else ErrorValue(**values['error_json'])
    cursor = None if values['next_cursor_json'] is None else DirectoryCursor.from_json(values['next_cursor_json'])
    if values['chunk_event_id'] is None:
        if result_tx.first_event_id != result.event_id:
            raise ConsistencyError('空页或错误返回不能包含额外基准批次')
    else:
        chunk = _chunk_at(connection,values['chunk_event_id'],request.activity_id)
        if (error is not None or chunk.event_id != result_tx.first_event_id
                or result.event_id != chunk.event_id + 1 or chunk.chunk_no != request.baseline_ref.chunk_count + 1):
            raise ConsistencyError('基准非空返回没有同组原批次')
    return BaselineReadCallView(call_id,event.event_id,state,request,result.event_id,
        values['returned_at'],error,cursor,values['chunk_event_id'],
        HistoryBoundary(result_tx.txn_id,result_tx.last_event_id))


@contextmanager
def _read_transaction(owned):
    connection = owned.connection
    begun = not connection.in_transaction
    if begun:
        connection.execute('BEGIN')
    try:
        yield connection
    finally:
        if begun:
            connection.execute('ROLLBACK')


def read_call(call_id,boundary,owned):
    with _read_transaction(owned) as connection:
        return _read_call(connection,call_id,boundary)


def read_calls(action_id,boundary,cursor,limit,owned):
    ObjectId(action_id)
    if type(limit) is not int or not 1 <= limit <=128:
        raise ValueError('基准调用分页上限必须为1—128')
    previous=0
    if cursor is not None:
        if (not isinstance(cursor,BaselineCallsCursor) or cursor.action_id != action_id
                or cursor.boundary != boundary):
            raise ConsistencyError('基准调用游标不能改变原动作及完整边界')
        previous=ObjectId(cursor.after_call_id)
    with _read_transaction(owned) as connection:
        _read_boundary(connection,boundary)
        with closing(connection.execute('SELECT id FROM baseline_read_calls INDEXED BY baseline_calls_action'
                ' WHERE action_id=? AND id>? AND created_event_id<=? ORDER BY id LIMIT ?',
                (action_id,previous,boundary.last_event_id,limit+1))) as rows:
            identities=tuple(row[0] for row in rows)
        items=tuple(_read_call(connection,identity,boundary) for identity in identities[:limit])
        next_cursor=(None if len(identities)<=limit else
                     BaselineCallsCursor(action_id,boundary,identities[limit-1]))
        return Page(items,next_cursor)


def read_call_result(call_id,owned,*,execution_finished=True):
    if not execution_finished:
        return None
    with _read_transaction(owned) as connection:
        view=_read_call(connection,call_id,_current_boundary(connection))
        if view is None:
            raise ConsistencyError('基准调用结果读取缺少原意图')
        saved=saved_transaction_events(connection,view.request.result_save_key)
        if saved is None:
            if view.state is not _STATE.INTENDED:
                raise ConsistencyError('基准已知结果缺少原结果事务')
            return None
        if view.result_event_id != saved[-1]['event_id']:
            raise ConsistencyError('基准调用结果键属于其他完整事务')
        return view


def verify_write(request,key,owned,*,execution_finished=True):
    if not execution_finished:
        return None
    with _read_transaction(owned) as connection:
        saved=saved_transaction_events(connection,key)
        if saved is None:
            return None
        boundary=_current_boundary(connection)
        scope=TransactionScope(connection,boundary.txn_id,boundary.last_event_id)
        command=(SaveBaselineReadIntent(request,key) if isinstance(request,BaselineReadIntent)
                 else SaveBaselineReadUnconfirmed(request,key) if isinstance(request,BaselineReadUnconfirmedSave)
                 else SaveBaselineReadReturn(request,key))
        plan=command.plan(scope)
        if not plan.read_only or scope.allocation is not None:
            raise ConsistencyError('原基准调用核实不能创建新写入')
        return plan.result


def read_failure_anchor(action_id,call_id,boundary,owned):
    ObjectId(action_id)
    with _read_transaction(owned) as connection:
        view=_read_call(connection,call_id,boundary)
        if view is None or view.request.action_id != action_id:
            raise ConsistencyError('基准失败依据不属于原动作和调用')
        return view if view.error is not None or view.state is _STATE.UNCONFIRMED else None


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
        reads = context.baseline_read_reads
        if event.reason == 1 and type(reads) is _IntentReads:
            reads.validate(event, context)
        elif event.reason == 2 and type(reads) is BaselineReturnGroup:
            reads.validate_return(event)
        elif event.reason == 3 and type(reads) is BaselineReturnGroup:
            reads.validate_unconfirmed(event)
        else:
            raise ConsistencyError('基准调用事实缺少同连接独立读取')
    except (KeyError, TypeError, ValueError) as error:
        raise EventValidationError(f'基准调用事实不符合原始输入: {error}') from error


def register_baseline_read_guard():
    register_guard('baseline_read', baseline_read_guard)


def _return_raw(call_id, returned_at, error, cursor, chunk_event_id):
    return {'call_id':call_id,'returned_at':returned_at,'error':error_document(error),
        'next_cursor':None if cursor is None else cursor.as_json(),'chunk_event_id':chunk_event_id}


def _return_event(view, raw, event_id, transaction_id):
    before = {'state':int(_STATE.INTENDED),'returned_at':None,'result_event_id':None,
              'error_json':None,'next_cursor_json':None,'chunk_event_id':None}
    after = {'state':int(_STATE.RETURNED),'returned_at':raw['returned_at'],'result_event_id':event_id,
             'error_json':raw['error'],'next_cursor_json':raw['next_cursor'],'chunk_event_id':raw['chunk_event_id']}
    return event_envelope(event_id,transaction_id,_EVENT['id'],2,
        (update_change('baseline_read_calls',view.call_id,before,after),),raw['returned_at'],
        {'baseline_read_request':raw})


def _check_return_page(view, request, connection):
    page = request.returned.page
    if page is None:
        return
    intent = view.request
    activity, action = _activity(connection,intent.activity_id)
    _check_entries(page.items,activity,action)
    index = 0 if intent.cursor is None else intent.cursor.directory_index
    previous = None if intent.cursor is None else intent.cursor.after_path
    if (any(not item.path.startswith(intent.directories[index]+'/') for item in page.items)
            or any(a.sort_key >= b.sort_key for a,b in zip(page.items,page.items[1:]))
            or page.items and previous is not None and page.items[0].path <= previous):
        raise ConsistencyError('基准返回没有沿原输入目录及游标有序推进')
    cursor = page.next_cursor
    if cursor is not None:
        if cursor.binding != intent.binding or cursor.directories != intent.directories:
            raise ConsistencyError('基准返回游标改变原绑定或完整目录')
        same = cursor.directory_index == index and page.items and cursor.after_path == page.items[-1].path
        following = cursor.directory_index == index+1 and cursor.after_path is None
        if not (same or following):
            raise ConsistencyError('基准返回游标不是本目录进度或下一目录')
    elif index+1 != len(intent.directories):
        raise ConsistencyError('基准返回尚有目录但提前结束')


def _pending_prefix(view,connection):
    activity,action=_activity(connection,view.request.activity_id)
    stats=_tail_read(connection,activity)
    ref=view.request.baseline_ref
    if (activity['baseline_state'] != BaselineState.COLLECTING
            or activity['dispatch_state'] != enum_for('device_activities.dispatch_state').NOT_DISPATCHED
            or activity['occupancy_state'] != enum_for('device_activities.occupancy_state').HELD
            or (stats.first_event_id,stats.last_event_id,stats.chunk_count) !=
               (ref.first_event_id,ref.last_event_id,ref.chunk_count)):
        raise ConsistencyError('基准实际返回没有保留原调用的准备活动和前缀')
    return activity,action,stats


class SaveBaselineReadReturn:
    def __init__(self, request, key):
        if not isinstance(request,BaselineReadReturn):
            raise TypeError('基准实际返回需要冻结的完整申请')
        self.request,self.key=request,OperationKey(key)

    def plan(self, scope):
        request=self.request
        view=_read_call(scope.connection,request.call_id,_current_boundary(scope.connection))
        if view is None or self.key != view.request.result_save_key:
            raise ConsistencyError('基准返回不属于原调用和冻结结果键')
        _check_return_page(view,request,scope.connection)
        page=request.returned.page
        saved=saved_transaction_events(scope.connection,self.key)
        if saved is not None:
            raw=_return_raw(view.call_id,request.returned_at,request.error,
                None if page is None else page.next_cursor,view.chunk_event_id)
            if (view.state is not _STATE.RETURNED or saved[-1]['event_id'] != view.result_event_id
                    or not json_equal(saved[-1]['body']['evidence'],{'baseline_read_request':raw})):
                raise ConsistencyError('基准返回原键与完整实际申请不同')
            entries=() if page is None else page.items
            old=() if view.chunk_event_id is None else _chunk_at(
                scope.connection,view.chunk_event_id,view.request.activity_id).entries
            if entries != old:
                raise ConsistencyError('基准返回原键的原批次内容不同')
            return CommandPlan((),{}, {},result=view,read_only=True)
        if view.state is not _STATE.INTENDED:
            raise ConsistencyError('基准调用已经保存其他结果')
        activity,action,stats=_pending_prefix(view,scope.connection)
        ref=view.request.baseline_ref
        nonempty=page is not None and bool(page.items)
        allocation=scope.allocate(2 if nonempty else 1)
        chunk_id=allocation.first_event_id if nonempty else None
        events=[]
        if nonempty:
            chunk=BaselineChunkSave(activity['id'],ref.chunk_count+1,page.items,request.returned_at)
            events.append(append_event(chunk,activity,chunk_id,allocation.txn_id))
        raw=_return_raw(view.call_id,request.returned_at,request.error,
            None if page is None else page.next_cursor,chunk_id)
        event=_return_event(view,raw,allocation.last_event_id,allocation.txn_id)
        events.append(event)
        result=BaselineReadCallView(view.call_id,view.intent_event_id,_STATE.RETURNED,view.request,
            event.event_id,request.returned_at,request.error,None if page is None else page.next_cursor,
            chunk_id,HistoryBoundary(event.transaction_id,event.event_id))
        owners={(table,identity):('action',action['id']) for table,identity in
                [('baseline_read_calls',view.call_id),('device_activities',activity['id'])]}
        state={'actions':{action['id']:action},'device_activities':{activity['id']:activity},
               'baseline_read_calls':{view.call_id:row_facts(scope.connection,'baseline_read_calls',view.call_id)}}
        return CommandPlan(tuple(events),owners,state,result=result,
            baseline_reads={activity['id']:stats},baseline_read_write_scope=BaselineReadWriteScope(
                action['id'],activity['id'],view.call_id))


class BaselineReturnGroup:
    """原内核持有的至多两事件组：实际APPEND后唯一RETURN。"""
    def __init__(self,scope,transaction,connection,operation_key):
        if not isinstance(scope,BaselineReadWriteScope):
            raise ConsistencyError('基准返回必须声明原调用写入范围')
        self.scope,self.transaction,self.connection=scope,transaction,connection
        self.view=_read_call(connection,scope.call_id,_current_boundary(connection))
        if (self.view is None or self.view.state is not _STATE.INTENDED
                or self.view.request.action_id != scope.action_id
                or self.view.request.activity_id != scope.activity_id
                or self.view.request.result_save_key != operation_key):
            raise ConsistencyError('基准返回组不属于原调用和结果键')
        self.activity,_,self.stats=_pending_prefix(self.view,connection)
        self.chunk=None
        self.chunk_returned_at=None
        self.result=None
        self.pending=None

    def bind(self,context,event):
        if context.baseline_read_reads is not None or self.result is not None:
            raise ConsistencyError('基准返回组不能替换读取端口或追加结果后事件')
        if event.event_type == load_event_registry()['events']['BASELINE_CHUNK']['id']:
            _,chunk=_decode_chunk(event)
            if (self.chunk is not None or chunk.chunk_no != self.view.request.baseline_ref.chunk_count+1
                    or chunk.event_id != self.transaction.first_event_id
                    or event.event_id+1 != self.transaction.last_event_id):
                raise ConsistencyError('基准返回只接受一个前序实际批次')
            self.pending=event
        elif event.event_type != _EVENT['id'] or event.reason not in (2,3):
            raise ConsistencyError('基准返回组包含其他业务事件')
        return replace(context,baseline_read_reads=self,
            baseline_reads={self.scope.activity_id:self.stats})

    def validate_return(self,event):
        from camctl.devices.directory import DirectoryRead
        from camctl.contracts.pages import Page
        raw=event.evidence['baseline_read_request']
        expected_chunk=None if self.chunk is None else self.chunk.event_id
        if (event.event_id != self.transaction.last_event_id
                or raw['call_id'] != self.scope.call_id or raw['returned_at'] != event.occurred_at
                or raw['chunk_event_id'] != expected_chunk
                or self.chunk is not None and self.chunk_returned_at != event.occurred_at
                or self.transaction.first_event_id != event.event_id-int(self.chunk is not None)):
            raise ConsistencyError('基准返回不是原调用完整组末事件')
        error=None if raw['error'] is None else ErrorValue(**raw['error'])
        cursor=None if raw['next_cursor'] is None else DirectoryCursor.from_json(raw['next_cursor'])
        entries=() if self.chunk is None else self.chunk.entries
        returned=DirectoryRead(None,error,None if error is not None else Page(entries,cursor))
        request=BaselineReadReturn(self.scope.call_id,event.occurred_at,error,returned)
        _check_return_page(self.view,request,self.connection)
        expected=_return_event(self.view,_return_raw(request.call_id,request.returned_at,error,cursor,expected_chunk),
            event.event_id,event.transaction_id)
        if replace(event,change_seq=None) != expected:
            raise ConsistencyError('基准返回没有精确固定原意图及实际批次')
        if self.chunk is not None:
            from camctl.history.decoding import decode_event_row
            row=_one(self.connection,'SELECT id,transaction_id,event_type,event_version,'
                'occurred_at,clock_status,change_seq,body_json FROM history_events WHERE id=?',
                (self.chunk.event_id,))
            actual=None if row is None else decode_event_row(row)
            if actual is not None and (actual.transaction_id != self.transaction.txn_id
                    or _decode_chunk(actual)[1] != self.chunk):
                raise ConsistencyError('基准返回与同连接已写前序批次不一致')
        self.pending=event

    def validate_unconfirmed(self,event):
        _unconfirmed_qualification(self.connection,self.view)
        if (self.chunk is not None or self.transaction.first_event_id != event.event_id
                or self.transaction.last_event_id != event.event_id
                or replace(event,change_seq=None) != _unconfirmed_event(
                    self.scope.call_id,event.occurred_at,event.event_id,event.transaction_id)):
            raise ConsistencyError('基准返回未知必须独立保存原调用的未知事实')
        self.pending=event

    def seal(self,validated):
        event=validated.envelope
        if self.pending is None or replace(event,change_seq=None) != replace(self.pending,change_seq=None):
            raise ConsistencyError('基准返回组没有经独立守卫核同的实际事件')
        if event.event_type == _EVENT['id']:
            self.result=event
        else:
            self.chunk=_decode_chunk(event)[1]
            self.chunk_returned_at=event.occurred_at
        self.pending=None

    def finish(self):
        if self.result is None or self.pending is not None:
            raise ConsistencyError('基准实际返回组缺少唯一结果，不能提交部分批次')


def _unconfirmed_event(call_id,occurred_at,event_id,transaction_id):
    return event_envelope(event_id,transaction_id,_EVENT['id'],3,
        (update_change('baseline_read_calls',call_id,{'state':int(_STATE.INTENDED),'result_event_id':None},
            {'state':int(_STATE.UNCONFIRMED),'result_event_id':event_id}),),occurred_at,
        {'baseline_read_request':{'call_id':call_id,'occurred_at':occurred_at}})


def _unconfirmed_qualification(connection,view):
    from .capture_facts import load_start_facts
    activity,action,stats=_pending_prefix(view,connection)
    if not ((action['status'], action['execution_started']) in ((1, 0), (2, 1))
            or action['status'] in (3, 4, 5, 6)):
        raise ConsistencyError('基准返回未知只在原执行动作的准备阶段保存')
    if not load_start_facts(connection,action).not_started:
        raise ConsistencyError('基准返回未知不能替代可能派发的启动责任')
    kind=enum_for('operation_runs.kind').CHECK_CAPTURE_RESULTS
    if _one(connection,'SELECT id FROM operation_runs WHERE activity_id=? AND kind=? LIMIT 1',
            (activity['id'],int(kind))) is not None:
        raise ConsistencyError('基准返回未知不能覆盖已建立的结果核实责任')
    return activity,action,stats


class SaveBaselineReadUnconfirmed:
    def __init__(self,request,key):
        if not isinstance(request,BaselineReadUnconfirmedSave):
            raise TypeError('基准返回未知需要原调用及本次收场时刻')
        self.request,self.key=request,OperationKey(key)

    def plan(self,scope):
        view=_read_call(scope.connection,self.request.call_id,_current_boundary(scope.connection))
        if view is None or self.key != view.request.result_save_key:
            raise ConsistencyError('基准返回未知必须使用原结果保存键')
        saved=saved_transaction_events(scope.connection,self.key)
        if saved is not None:
            event=_event_at(scope.connection,saved[-1]['event_id'])
            if (view.state is not _STATE.UNCONFIRMED or len(saved) != 1
                    or replace(event,change_seq=None) != _unconfirmed_event(
                        view.call_id,self.request.occurred_at,event.event_id,event.transaction_id)):
                raise ConsistencyError('基准返回未知原键与原完整申请不同')
            return CommandPlan((),{},{},result=view,read_only=True)
        if view.state is not _STATE.INTENDED:
            raise ConsistencyError('已有实际基准结果不能替换为未知')
        activity,action,_=_unconfirmed_qualification(scope.connection,view)
        allocation=scope.allocate(1)
        event=_unconfirmed_event(view.call_id,self.request.occurred_at,
            allocation.first_event_id,allocation.txn_id)
        result=replace(view,state=_STATE.UNCONFIRMED,result_event_id=event.event_id,
            boundary=HistoryBoundary(event.transaction_id,event.event_id))
        state={'actions':{action['id']:action},'device_activities':{activity['id']:activity},
            'baseline_read_calls':{view.call_id:row_facts(scope.connection,'baseline_read_calls',view.call_id)}}
        return CommandPlan((event,),{('baseline_read_calls',view.call_id):('action',action['id'])},
            state,result=result,baseline_read_write_scope=BaselineReadWriteScope(
                action['id'],activity['id'],view.call_id))

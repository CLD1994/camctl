"""取回 CHECK 的原事务读取：固定归属和已提交的紧邻结果。"""
from contextlib import closing
from dataclasses import replace

from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError
from camctl.history.decoding import decode_event_row
from camctl.history.events import load_event_registry
from camctl.history.validators import validate_event_structure
from camctl.persistence.transaction import read_transaction_range, row_facts


def previous_result(connection, run, before_event_id):
    """定点核上一票及其完整原结果，不消费提案提供的结果副本。"""
    from .operations import _verify_complete_obtain_result
    with closing(connection.execute('SELECT id FROM operation_attempts WHERE run_id=?'
            ' ORDER BY attempt_no DESC LIMIT 1', (run['id'],))) as cursor:
        identity = cursor.fetchone()
    attempt = None if identity is None else row_facts(connection, 'operation_attempts', identity[0])
    if attempt is None or attempt['attempt_no'] != run['attempts_used']:
        raise ConsistencyError('取回 CHECK 没有与累计次数一致的真实前票')
    _verify_complete_obtain_result(attempt)
    with closing(connection.execute('SELECT id,transaction_id,event_type,event_version,'
            'occurred_at,clock_status,change_seq,body_json FROM history_events WHERE id=?',
            (attempt['result_event_id'],))) as cursor:
        stored = cursor.fetchone()
    if stored is None:
        raise ConsistencyError('取回 CHECK 前票的实际结果事件缺失')
    event = decode_event_row(stored)
    name, _, _ = validate_event_structure(event)
    transaction = read_transaction_range(connection, event.transaction_id)
    changes = [row for row in event.rows if row.table == 'operation_attempts'
               and row.row_id == attempt['id']]
    if (name != 'ATTEMPT_RESULT' or transaction.last_event_id >= before_event_id
            or event.event_id != attempt['result_event_id'] or len(changes) != 1
            or not changes[0].before.exists or not changes[0].after.exists):
        raise ConsistencyError('取回 CHECK 前票与实际已提交的完整结果事件不同')
    # 结果事件只写实际改变的列；未改变的效果等值来自原意图创建行。
    with closing(connection.execute('SELECT id,transaction_id,event_type,event_version,'
            'occurred_at,clock_status,change_seq,body_json FROM history_events WHERE id=?',
            (attempt['intent_event_id'],))) as cursor:
        stored_intent = cursor.fetchone()
    if stored_intent is None:
        raise ConsistencyError('取回 CHECK 前票没有实际原意图')
    intent = decode_event_row(stored_intent)
    intent_name, _, _ = validate_event_structure(intent)
    intent_transaction = read_transaction_range(connection, intent.transaction_id)
    created = [row for row in intent.rows if row.table == 'operation_attempts'
               and row.row_id == attempt['id'] and not row.before.exists and row.after.exists]
    if (intent_name != 'ATTEMPT_STARTED' or len(created) != 1
            or intent_transaction.last_event_id >= transaction.first_event_id
            or any(not json_equal(created[0].after.values.get(column), attempt[column])
                   for column in ('run_id', 'attempt_no', 'intent_event_id'))
            or any(not json_equal(created[0].after.values.get(column), value)
                   for column, value in changes[0].before.values.items())):
        raise ConsistencyError('取回 CHECK 前票结果没有同一原意图的实际前态')
    resulting = {**created[0].after.values, **changes[0].after.values}
    if any(not json_equal(resulting.get(column), attempt[column])
           for column in ('status', 'effect_state', 'result_json', 'error_json', 'result_event_id')):
        raise ConsistencyError('取回 CHECK 前票的实际结果正文与固定行不同')
    return attempt


def _chain(connection, run):
    from camctl.operations.attempts import AttemptTarget, OperationKind
    from .operations import (_load_flow_context, _obtain_check_is_host,
        _verify_obtain_config, _verify_run_identity)
    state = {}
    _load_flow_context(connection, OperationKind.CHECK_FILE_EXISTS, run['action_id'],
        AttemptTarget(obtain_item_id=run['obtain_item_id']), None, state)
    _verify_run_identity(run, state)
    _verify_obtain_config(run, state)
    return _obtain_check_is_host(state, run['obtain_item_id'], run['action_id'])


class _ObtainCheckReads:
    """仅由原内核为当前事件建立；没有调用方可省略的授权标记。"""
    def __init__(self, connection, transaction, event, run_ids):
        self.connection, self.transaction, self.event = connection, transaction, event
        self.run_ids = tuple(run_ids)

    def verify(self, event, transaction):
        from .operations import _RUN_STATUS
        if (replace(event, change_seq=self.event.change_seq) != self.event or transaction != self.transaction
                or not self.connection.in_transaction):
            raise ConsistencyError('取回 CHECK 读取端口不属于当前实际事务和事件')
        immutable = load_event_registry()['tables']['operation_runs']['immutable']
        for run_id in self.run_ids:
            actual = row_facts(self.connection, 'operation_runs', run_id)
            changes = [row for row in event.rows if row.table == 'operation_runs' and row.row_id == run_id]
            if len(changes) > 1:
                raise ConsistencyError('取回 CHECK 事件重复改变同一流程')
            if actual is None:
                if not changes or changes[0].before.exists or not changes[0].after.exists:
                    raise ConsistencyError('取回 CHECK 缺少实际新建流程')
                after = {'id': run_id, **changes[0].after.values}
            else:
                after = dict(actual)
                if changes:
                    change = changes[0]
                    if not change.before.exists or not change.after.exists:
                        raise ConsistencyError('取回 CHECK 不能替换已有流程')
                    if any(column in change.after.values
                           and not json_equal(change.after.values[column], actual[column])
                           for column in immutable):
                        raise ConsistencyError('取回 CHECK 的固定身份与实际原流程不同')
                    after.update(change.after.values)
            host = _chain(self.connection, after)
            attempts = [row for row in event.rows if row.table == 'operation_attempts'
                        and not row.before.exists and row.after.values.get('run_id') == run_id]
            if not attempts:
                continue
            if len(attempts) != 1:
                raise ConsistencyError('一次取回 CHECK 意图只能增加紧邻的一票')
            incoming = attempts[0].after.values
            if any(not json_equal(incoming.get(column), after.get(column))
                   for column in ('max_attempts_used', 'timeout_s_json', 'retry_interval_s_json')):
                raise ConsistencyError('取回 CHECK 意图与实际流程采用配置不同')
            used = 0 if actual is None else actual['attempts_used']
            if (incoming['attempt_no'] != used + 1 or after['attempts_used'] != used + 1
                    or used >= after['max_attempts_used']
                    or actual is not None and actual['status'] not in (_RUN_STATUS.PENDING, _RUN_STATUS.ACTIVE)):
                raise ConsistencyError('取回 CHECK 的实际累计次数、预算或开放状态不允许新票')
            if used:
                _chain(self.connection, actual)
                previous_result(self.connection, actual, transaction.first_event_id)
                if not host and actual['retry_wait_required'] != 1:
                    raise ConsistencyError('设备取回 CHECK 缺少真实重试等待责任')


def bind(context, event, transaction, connection):
    """按实际 SQL 及创建行识别 obtain；调用方不能关闭或替换此检查。"""
    if context.obtain_check_reads is not None:
        raise ConsistencyError('取回 CHECK 读取端口必须由原写事务内核创建')
    run_ids = set()
    for change in event.rows:
        if change.table == 'operation_runs':
            actual = row_facts(connection, 'operation_runs', change.row_id)
            if ((actual is not None and actual['obtain_item_id'] is not None)
                    or change.after.exists and change.after.values.get('obtain_item_id') is not None):
                run_ids.add(change.row_id)
        elif change.table == 'operation_attempts':
            actual = row_facts(connection, 'operation_attempts', change.row_id)
            run_id = actual['run_id'] if actual is not None else change.after.values.get('run_id')
            run = None if run_id is None else row_facts(connection, 'operation_runs', run_id)
            if run is not None and run['obtain_item_id'] is not None:
                run_ids.add(run_id)
    if not run_ids:
        return context
    return replace(context, obtain_check_reads=_ObtainCheckReads(connection, transaction, event, run_ids))

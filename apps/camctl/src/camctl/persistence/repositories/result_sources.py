"""从原结果或范围意图之前的可靠事实重建文件来源，不改变原历史。"""
from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError, ObjectId
from camctl.devices.capture_completion import validate_completion_context, validate_control_confirmation
from camctl.history.events import load_event_registry
from camctl.persistence.transaction import row_facts


class ResultSourceKind(StrEnum):
    NATURAL = 'natural'
    STOP = 'stop'
    DIRECT = 'direct'
    UNCONFIRMED = 'unconfirmed'
    UNANCHORED = 'unanchored'


@dataclass(frozen=True)
class ResultSource:
    kind: ResultSourceKind
    intent_event_id: int
    context: dict | None = None
    natural_lower_bound: int | None = None
    requires_file_wait: bool = False


def result_source_from_state(action, activity, attempt, state):
    """持久读取与独立历史守卫共同核对原意图及其可采用的来源。"""
    from .capture import _CaptureStopCommand, _history_source_event, _source_fact
    from .operations import _ATTEMPT_STARTED_EVENT
    from .result_pages import result_responsibility
    from .timelapse import _verify_wait_basis

    intent_id = ObjectId(attempt['intent_event_id'])
    intent = _history_source_event(state, intent_id)
    rows = [row for row in intent.rows if row.table == 'operation_attempts' and row.row_id == attempt['id']]
    if (intent.event_type != _ATTEMPT_STARTED_EVENT or len(rows) != 1
            or rows[0].after.values.get('intent_event_id') != intent_id
            or rows[0].after.values.get('run_id') != attempt['run_id']
            or rows[0].after.values.get('attempt_no') != attempt['attempt_no']):
        raise ConsistencyError('结果来源缺少原可靠意图及尝试身份')
    run = _source_fact(state, 'operation_runs', attempt['run_id'])
    result_responsibility(run)
    if (run['action_id'] != action['id'] or run['activity_id'] != activity['id']
            or activity['action_id'] != action['id']):
        raise ConsistencyError('结果来源的原责任、动作与活动不符')
    spec = action['execution_spec_json']
    requires_wait = 'file_completion_wait_ms' in spec
    natural = (action['type'] == 3 and spec.get('wait_after_send')
        and spec.get('end_control') == 1)
    if action['type'] not in (2, 3) or not (requires_wait or natural):
        return ResultSource(ResultSourceKind.DIRECT, int(intent_id))
    stops = []
    for candidate in state.get('operation_attempts', {}).values():
        owner = state.get('operation_runs', {}).get(candidate['run_id'])
        if (owner is not None and owner['kind'] == int(enum_for('operation_runs.kind').STOP)
                and owner['action_id'] == action['id'] and owner['activity_id'] == activity['id']
                and owner['responsibility_key'] == f"stop/{action['id']}"
                and candidate['effect_state'] == int(enum_for('operation_attempts.effect_state').CONFIRMED)
                and candidate['result_event_id'] is not None and candidate['result_event_id'] < intent_id):
            stops.append(candidate)
    if stops:
        if not requires_wait:
            # 旧直接文件保证不消费停止等待；独立停止事实由控制消费者核对。
            return ResultSource(ResultSourceKind.DIRECT, int(intent_id))
        stopped = max(stops, key=lambda value: value['result_event_id'])
        event = _history_source_event(state, stopped['result_event_id'])
        actual_activity, returned_at = _CaptureStopCommand.source_from_rows(event, state)
        if actual_activity['id'] != activity['id']:
            raise ConsistencyError('原结果意图之前的停止属于其他活动')
        response = {'status': stopped['status'], 'effect_state': stopped['effect_state'],
            'result': stopped['result_json'], 'error': stopped['error_json']}
        validate_control_confirmation(response, 'stop_confirmed', str(activity['id']))
        context = None
        if requires_wait:
            context = validate_completion_context({**({'kind': 'timelapse_stop'} if action['type'] == 3 else {}),
                'activity_id': str(activity['id']), 'stop_result_event_id': int(event.event_id),
                'stop_returned_at_us': int(returned_at), 'stop_response': response,
                'file_completion_wait_ms': spec['file_completion_wait_ms'], 'prior_completion': None},
                activity_id=str(activity['id']))
        return ResultSource(ResultSourceKind.STOP, int(intent_id), context, requires_file_wait=requires_wait)
    wait_id = activity['wait_completed_event_id']
    if not requires_wait and (wait_id is None or wait_id >= intent_id):
        # 能消费直接文件，不等于原请求已经取得自然完成资格。
        return ResultSource(ResultSourceKind.DIRECT, int(intent_id))
    if natural and activity['sent_at'] is None:
        from camctl.operations.models import EffectState
        from camctl.operations.result_format import read_result_document
        from .capture import _ATTEMPT_RESULT_EVENT
        starts = [candidate for candidate in state.get('operation_attempts', {}).values()
            if state['operation_runs'][candidate['run_id']]['responsibility_key'] == f"start/{action['id']}"
            and state['operation_runs'][candidate['run_id']]['activity_id'] == activity['id']]
        if not starts:
            raise ConsistencyError('无发送锚点的结果来源缺少原 START 实际结果')
        started = max(starts, key=lambda value: value['attempt_no'])
        event = _history_source_event(state, started['result_event_id'])
        rows = [row for row in event.rows if row.table == 'operation_attempts' and row.row_id == started['id']]
        original_intent = _history_source_event(state, started['intent_event_id'])
        initial = [row for row in original_intent.rows if row.table == 'operation_attempts' and row.row_id == started['id']]
        owner = _source_fact(state, 'operation_runs', started['run_id'])
        if (original_intent.event_type != _ATTEMPT_STARTED_EVENT or len(initial) != 1
                or initial[0].before.exists or not original_intent.event_id < event.event_id
                or initial[0].after.values.get('run_id') != started['run_id']
                or initial[0].after.values.get('attempt_no') != started['attempt_no']
                or initial[0].after.values.get('intent_event_id') != original_intent.event_id
                or owner['kind'] != int(enum_for('operation_runs.kind').START)
                or owner['action_id'] != action['id']):
            raise ConsistencyError('无发送锚点的结果缺少同活动的原 START 意图')
        saved = {**initial[0].after.values, **(rows[0].after.values if len(rows) == 1 else {})}
        actual = read_result_document(started['status'], started['effect_state'], started['result_json'], started['error_json'])
        if (wait_id is not None or event.event_type != _ATTEMPT_RESULT_EVENT
                or event.event_id >= intent_id or len(rows) != 1
                or any(not json_equal(saved.get(field), started[field])
                    for field in ('status', 'effect_state', 'result_json', 'error_json', 'result_event_id'))
                or actual.effect is not EffectState.UNKNOWN):
            raise ConsistencyError('无发送锚点不匹配原合法未知 START，不能补造自然来源')
        return ResultSource(ResultSourceKind.UNANCHORED, int(intent_id), requires_file_wait=requires_wait)
    if natural and wait_id is not None and wait_id < intent_id:
        wait = _history_source_event(state, wait_id)
        contract = load_event_registry()['events']['CAPTURE_WAIT_CHANGED']
        wait_rows = [row for row in wait.rows if row.table == 'device_activities' and row.row_id == activity['id']]
        if (wait.event_type != contract['id'] or wait.reason != contract['branches']['COMPLETE']['reason']
                or len(wait_rows) != 1 or wait_rows[0].after.values.get('wait_completed_event_id') != wait_id):
            raise ConsistencyError('原结果意图之前缺少同活动的自然等待完成事实')
        _verify_wait_basis(activity, action, state, occurred_at=wait.occurred_at, complete=True, event_id=wait_id)
        starts = [candidate for candidate in state.get('operation_attempts', {}).values()
            if state['operation_runs'][candidate['run_id']]['responsibility_key'] == f"start/{action['id']}"
            and state['operation_runs'][candidate['run_id']]['activity_id'] == activity['id']]
        start = max(starts, key=lambda value: value['attempt_no'])
        context = validate_completion_context({'kind': 'timelapse_natural', 'activity_id': str(activity['id']),
            'start_result_event_id': start['result_event_id'],
            'start_response': {'status': start['status'], 'effect_state': start['effect_state'],
                'result': start['result_json'], 'error': start['error_json']},
            'sent_at_us': activity['sent_at'], 'wait_completed_event_id': wait_id,
            'wait_completed_at_us': int(wait.occurred_at), 'target_duration_ms': spec['target_duration_ms'],
            'result_wait_margin_ms': activity['result_wait_margin_ms'],
            'extra_wait_ms': activity['extra_wait_ms_used'], 'expected_check_at_us': activity['expected_check_at'],
            **({'startup_margin_ms': spec['startup_margin_ms']} if 'startup_margin_ms' in spec else {})},
            activity_id=str(activity['id']))
        return ResultSource(ResultSourceKind.NATURAL, int(intent_id), context if requires_wait else None,
            max(context['expected_check_at_us'], context['wait_completed_at_us']), requires_wait)
    return ResultSource(ResultSourceKind.UNCONFIRMED if natural or requires_wait else ResultSourceKind.DIRECT,
        int(intent_id), requires_file_wait=requires_wait)


def load_result_source(connection, ticket, state=None):
    """只读取原尝试、一个原控制来源及固定等待，不缓存目录或整段历史。"""
    from .capture import _history_source_row
    from .result_pages import _load
    from .timelapse import _load_wait_start

    run, attempt, action, activity = _load(connection, ticket)
    state = {} if state is None else state
    for table, row in (('operation_runs', run), ('operation_attempts', attempt),
            ('actions', action), ('device_activities', activity)):
        state.setdefault(table, {})[row['id']] = row
    events = state.setdefault('history_events', {})
    events[attempt['intent_event_id']] = _history_source_row(connection, attempt['intent_event_id'])
    spec = action['execution_spec_json']
    requires_wait = 'file_completion_wait_ms' in spec
    natural = action['type'] == 3 and spec.get('wait_after_send') and spec.get('end_control') == 1
    if not (requires_wait or natural):
        return result_source_from_state(action, activity, attempt, state)
    with closing(connection.execute(
        'SELECT r.id,t.id,t.result_event_id FROM operation_runs r JOIN operation_attempts t ON t.run_id=r.id'
        ' WHERE r.responsibility_key=? AND r.action_id=? AND r.activity_id=? AND t.effect_state=?'
        ' AND t.result_event_id<? ORDER BY t.result_event_id DESC LIMIT 1',
        (f"stop/{action['id']}", action['id'], activity['id'],
            int(enum_for('operation_attempts.effect_state').CONFIRMED), attempt['intent_event_id']))) as cursor:
        stop = cursor.fetchone()
    if stop is not None:
        owner, actual = row_facts(connection, 'operation_runs', stop[0]), row_facts(connection, 'operation_attempts', stop[1])
        state['operation_runs'][owner['id']] = owner
        state['operation_attempts'][actual['id']] = actual
        if requires_wait:
            events[stop[2]] = _history_source_row(connection, stop[2])
    elif action['type'] == 3:
        if activity['wait_completed_event_id'] is not None and activity['wait_completed_event_id'] < attempt['intent_event_id']:
            events[activity['wait_completed_event_id']] = _history_source_row(connection, activity['wait_completed_event_id'])
            _load_wait_start(connection, activity, state)
        elif requires_wait and activity['sent_at'] is None:
            _load_wait_start(connection, activity, state)
            started = max((value for value in state['operation_attempts'].values()
                if state['operation_runs'][value['run_id']]['responsibility_key'] == f"start/{action['id']}"),
                key=lambda value: value['attempt_no'])
            events[started['intent_event_id']] = _history_source_row(connection, started['intent_event_id'])
    return result_source_from_state(action, activity, attempt, state)


def natural_result_source(action, activity, state):
    """自然集合消费者核对原请求；独立旧结论没有请求时不补造 I。"""
    attempts = [attempt for attempt in state.get('operation_attempts', {}).values()
        if (owner := state.get('operation_runs', {}).get(attempt['run_id'])) is not None
        and owner['kind'] == int(enum_for('operation_runs.kind').CHECK_CAPTURE_RESULTS)
        and owner['action_id'] == action['id'] and owner['activity_id'] == activity['id']]
    if not attempts:
        return None
    attempt = max(attempts, key=lambda value: value['intent_event_id'])
    source = result_source_from_state(action, activity, attempt, state)
    if source.kind is not ResultSourceKind.NATURAL:
        raise ConsistencyError('原结果请求没有自然完成资格，不能采用后来的等待')
    return source

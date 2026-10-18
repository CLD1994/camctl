"""有限设备任务取消后的独立结束事实；不保存目标完成或成功停止。"""
from dataclasses import dataclass
from collections.abc import Mapping

from camctl.capture.finite_cancel import finite_cancel_policy
from camctl.capture.result_pages import ResultPageRef
from camctl.capture.results import CaptureFile, CaptureFileSet, FileKind, ProductRequirements, ProductRule, assess_capture_files, is_optional_preview
from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError, ObjectId, UtcMicros, new_operation_key
from camctl.devices.tasks import EndControl, StartReturn, CompletionMode
from camctl.devices.capture_timing import CaptureTiming
from camctl.history.events import load_event_registry
from camctl.history.validators import EventValidationError, register_guard
from camctl.operations.models import AttemptStatus, EffectState
from camctl.operations.result_format import read_result_document
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.transaction import CommandPlan, TransactionError, commit_operation, event_envelope, row_facts, saved_transaction_events, update_change

_BRANCH = load_event_registry()['events']['DEVICE_OBSERVED']
_TYPE = _BRANCH['id']
_ATTEMPT_RESULT_TYPE = load_event_registry()['events']['ATTEMPT_RESULT']['id']
_WAIT_EVENT = load_event_registry()['events']['CAPTURE_WAIT_CHANGED']
_REASON = _BRANCH['branches']['FINITE_CANCEL_END']['reason']
_RUN = enum_for('operation_runs.status')
_KIND = enum_for('operation_runs.kind')
_ATTEMPT = enum_for('operation_attempts.status')


@dataclass(frozen=True)
class FiniteCancelEndSave:
    """原可靠结果末页及原事实时刻；UNKNOWN 重送不更换输入。"""
    action_id: int
    page: ResultPageRef
    occurred_at: int

    def __post_init__(self):
        ObjectId(self.action_id)
        UtcMicros(self.occurred_at)
        if not isinstance(self.page, ResultPageRef):
            raise TypeError('有限取消收场必须引用原完整结果页')


def _load(scope, request):
    from .capture import load_activity_of_action, _history_source_row
    state = {'actions': {}, 'device_activities': {}, 'operation_runs': {},
             'operation_attempts': {}, 'history_events': {}}
    action = row_facts(scope.connection, 'actions', request.action_id)
    if action is None:
        raise ConsistencyError('有限取消收场缺少原动作')
    activity = load_activity_of_action(scope.connection, request.action_id)
    state['actions'][action['id']] = action
    state['device_activities'][activity['id']] = activity
    for key in (f"start/{action['id']}", f"stop/{action['id']}", f"results/{activity['id']}"):
        found = scope.connection.execute('SELECT id FROM operation_runs WHERE responsibility_key=?', (key,)).fetchone()
        if found is None:
            raise ConsistencyError('有限取消收场缺少原控制或结果责任')
        run = row_facts(scope.connection, 'operation_runs', found[0])
        state['operation_runs'][run['id']] = run
        found = scope.connection.execute('SELECT id FROM operation_attempts WHERE run_id=? ORDER BY attempt_no DESC LIMIT 1', (run['id'],)).fetchone()
        if found is None:
            raise ConsistencyError('有限取消收场缺少原实际尝试')
        attempt = row_facts(scope.connection, 'operation_attempts', found[0])
        state['operation_attempts'][attempt['id']] = attempt
        if attempt['result_event_id'] is not None:
            state['history_events'][attempt['result_event_id']] = _history_source_row(scope.connection, attempt['result_event_id'])
    for event_id in (activity['wait_completed_event_id'], request.page.event_id):
        ObjectId(event_id)
        state['history_events'][event_id] = _history_source_row(scope.connection, event_id)
    from .result_sources import load_result_source
    load_result_source(scope.connection, request.page.ticket, state)
    return state, action, activity


def _facts(state, action_id):
    action = state['actions'][action_id]
    activity, = (a for a in state['device_activities'].values() if a['action_id'] == action_id)
    runs = state['operation_runs'].values()
    start, = (r for r in runs if r['responsibility_key'] == f'start/{action_id}')
    stop, = (r for r in runs if r['responsibility_key'] == f'stop/{action_id}')
    results, = (r for r in runs if r['responsibility_key'] == f"results/{activity['id']}")
    attempts = state['operation_attempts'].values()
    start_attempt, = (a for a in attempts if a['run_id'] == start['id'])
    stop_attempt, = (a for a in attempts if a['run_id'] == stop['id'])
    result_attempt, = (a for a in attempts if a['run_id'] == results['id'])
    return action, activity, start, stop, results, start_attempt, stop_attempt, result_attempt


def _evidence(state, request):
    action, activity, start, stop, results, start_attempt, stop_attempt, result_attempt = _facts(state, request.action_id)
    spec = action['execution_spec_json']
    return {
        'policy': spec.get('cancel_completion_policy'), 'activity_id': activity['id'],
        **({'startup_margin_ms': spec['startup_margin_ms']} if 'startup_margin_ms' in spec else {}),
        'start_result_event_id': start_attempt['result_event_id'],
        'stop_run_id': stop['id'], 'stop_result_event_id': stop_attempt['result_event_id'],
        'sent_at_us': activity['sent_at'], 'target_duration_ms': spec.get('target_duration_ms'),
        'result_wait_margin_ms': activity['result_wait_margin_ms'],
        'extra_wait_ms': activity['extra_wait_ms_used'],
        'expected_check_at_us': activity['expected_check_at'],
        'wait_completed_event_id': activity['wait_completed_event_id'],
        'result_attempt_id': result_attempt['id'], 'result_page_event_id': request.page.event_id,
    }


def _verify_sources(state, request, evidence, *, event_id):
    from .capture import _history_source_event
    from .result_pages import _decode
    from .result_sources import ResultSourceKind, result_source_from_state
    action, activity, start, stop, results, sent, stopped, result = _facts(state, request.action_id)
    spec = action['execution_spec_json']
    if (not finite_cancel_policy(action) or not action['cancel_requested']
            or spec.get('end_control') != int(EndControl.DEVICE)
            or spec.get('start_return_meaning') != int(StartReturn.SENT)
            or spec.get('completion_mode') != int(CompletionMode.TIME_AND_OUTPUTS)
            or spec.get('wait_after_send') is not True or spec.get('duration_based') is not True):
        raise ConsistencyError('有限取消结束要求原冻结策略、有限设备任务和已生效取消')
    if not json_equal(evidence, _evidence(state, request)):
        raise ConsistencyError('有限取消结束改变原调用、等待或结果页身份')
    for run, kind in ((start, _KIND.START), (stop, _KIND.STOP), (results, _KIND.CHECK_CAPTURE_RESULTS)):
        if (run['kind'] != int(kind) or run['action_id'] != action['id']
                or run['activity_id'] != activity['id']):
            raise ConsistencyError('有限取消结束的原责任不属于同一活动')
    if (stop['status'] != int(_RUN.FAILED) or stop['attempts_used'] != 1
            or stopped['status'] == int(_ATTEMPT.RUNNING)
            or stopped['effect_state'] == int(enum_for('operation_attempts.effect_state').CONFIRMED)):
        raise ConsistencyError('有限取消结束缺少第一次停止失败的终局责任')
    original = read_result_document(sent['status'], sent['effect_state'], sent['result_json'], sent['error_json'])
    if (original.status is not AttemptStatus.SUCCEEDED or original.effect is not EffectState.CONFIRMED
            or not any(o.type == 'timelapse_sent' and o.data.get('activity_id') == str(activity['id']) for o in original.observations)):
        raise ConsistencyError('有限取消结束缺少原活动的可靠 SENT')
    for attempt in (sent, stopped, result):
        source = _history_source_event(state, attempt['result_event_id'])
        original_rows = [row for row in source.rows if row.table == 'operation_attempts']
        if (source.event_type != _ATTEMPT_RESULT_TYPE or len(original_rows) != 1
                or original_rows[0].row_id != attempt['id']
                or any(not json_equal(original_rows[0].after.values.get(name), attempt[name])
                    for name in ('status', 'result_event_id', 'effect_state', 'result_json', 'error_json')
                    if name in original_rows[0].after.values)):
            raise ConsistencyError('有限取消结束引用的实际结果与原尝试完整输入不同')
    sent_event = _history_source_event(state, sent['result_event_id'])
    wait_event = _history_source_event(state, activity['wait_completed_event_id'])
    page_event = _history_source_event(state, request.page.event_id)
    page, _ = _decode(page_event)
    original_source = result_source_from_state(action, activity, result, state)
    if original_source.kind is not ResultSourceKind.NATURAL:
        raise ConsistencyError('有限取消结束缺少原结果意图之前的自然来源')
    if max(sent['result_event_id'], stopped['result_event_id'], result['result_event_id'],
            wait_event.event_id, page_event.event_id) >= event_id:
        raise ConsistencyError('有限取消结束不能早于任一原控制、等待或完整结果来源')
    expected = CaptureTiming(spec['target_duration_ms'], spec.get('startup_margin_ms', 0),
        spec['result_wait_margin_ms'], activity['extra_wait_ms_used']).deadline_us(activity['sent_at'])
    if (sent_event.occurred_at != activity['sent_at'] or activity['expected_check_at'] != expected
            or activity['result_wait_margin_ms'] != spec['result_wait_margin_ms']
            or wait_event.event_type != _WAIT_EVENT['id']
            or wait_event.reason != _WAIT_EVENT['branches']['COMPLETE']['reason']
            or not any(r.table == 'device_activities' and r.row_id == activity['id']
                and r.after.values.get('wait_completed_event_id') == wait_event.event_id for r in wait_event.rows)
            or wait_event.occurred_at < expected or page_event.occurred_at < wait_event.occurred_at
            or request.occurred_at < page_event.occurred_at
            or not sent_event.event_id < wait_event.event_id < page_event.event_id < event_id):
        raise ConsistencyError('有限取消结束未满足原发送锚点、原规则及可靠等待页顺序')
    if (results['status'] != int(_RUN.SUCCEEDED) or result['status'] != int(_ATTEMPT.SUCCEEDED)
            or result['error_json'] is not None or result['result_last_page_event_id'] != page.ref.event_id
            or page.ref != request.page or page.ref.ticket.run_id != results['id']
            or page.ref.ticket.target_id != str(activity['id']) or not page.page.scan_complete
            or not page.page.set_finalized or page.page.outcome.error is not None
            or not sent_event.event_id < stopped['result_event_id'] < event_id
            or page.page.outcome != read_result_document(result['status'], result['effect_state'],
                result['result_json'], result['error_json'])
            or activity['output_set_finalized_event_id'] is None):
        raise ConsistencyError('有限取消结束要求可靠完整结果集合及原末页')


def _read_scope(request):
    from .result_pages import capture_result_scope
    return capture_result_scope(request.action_id, request.page.ticket, request.page.event_id)


def _assess_files(reader, action, request):
    """按页复核全部候选，拒绝空集合、缺件、零长及尚未完成文件。"""
    if reader is None or reader.scope != _read_scope(request):
        raise ConsistencyError('有限取消结束缺少原有界文件事实读取范围')
    rules = tuple(ProductRule.from_json(value) for value in action['execution_spec_json'].get('product_rules', ()))
    from .result_pages import iter_result_file_facts
    def files():
        for entry, facts in iter_result_file_facts(reader, action, request.page.ticket, request.page.event_id):
            # 当前页与可靠固定长度必须同时成立，旧完整事实不填补当前缺口。
            complete = (entry.complete and facts['completion_state'] == 3
                and type(entry.size_bytes) is int and entry.size_bytes == facts['size_bytes'])
            owned = (facts['source_action_id'] == request.action_id
                     and facts['ownership_evidence_json'] is not None)
            if (is_optional_preview(entry.kind, entry.format_id, entry.paired_identity, rules)
                    and facts['source_action_id'] in (None, request.action_id)
                    and (not complete or not owned or entry.size_bytes <= 0)):
                continue
            yield CaptureFile(entry.identity, entry.kind, complete, owned,
                format_id=entry.format_id, pairing_confirmed=entry.paired_identity is not None,
                size_bytes=entry.size_bytes)
    return assess_capture_files(CaptureFileSet(files(), True),
        ProductRequirements(frozenset({FileKind.VIDEO}), rules))


def _verify_files(reader, action, request, state):
    if not _assess_files(reader, action, request).is_complete:
        raise ConsistencyError('有限取消结束尚无满足原必要规则的可靠文件集合')
    from .capture import _history_source_event
    activity = state['device_activities'][int(request.page.ticket.target_id)]
    wait = _history_source_event(state, activity['wait_completed_event_id'])
    lower_bound = max(activity['expected_check_at'], wait.occurred_at)
    cursor = None
    while True:
        pages = reader.result_pages(request.page.ticket, cursor, 1)
        if any(not lower_bound <= saved.occurred_at <= request.occurred_at for saved in pages.items):
            raise ConsistencyError('有限取消结束的原扫描不满足原等待及真实收场时刻')
        if pages.next_cursor is None:
            return
        cursor = pages.next_cursor


class _FiniteCancelQualificationCommand:
    """只读核对当前扫描，避免跨轮保留文件制造必然被拒绝的结束申请。"""

    def __init__(self, request):
        self.request = request

    def plan(self, scope):
        from camctl.persistence.event_reads import SqlEventReads
        action = row_facts(scope.connection, 'actions', self.request.action_id)
        if action is None:
            raise ConsistencyError('有限取消资格缺少原动作')
        assessment = _assess_files(SqlEventReads(scope.connection, _read_scope(self.request)),
            action, self.request)
        return CommandPlan((), {}, {'actions': {action['id']: action}},
            read_only=True, result=assessment)


def read_finite_cancel_assessment(request, owned):
    """在原只读事务内核实当前扫描，区分充分、明确不符和仍未知。"""
    receipt = commit_operation(_FiniteCancelQualificationCommand(request), new_operation_key(), owned)
    if receipt.kind != 'completed':
        raise ConsistencyError(f'有限取消的当前扫描资格无法可靠读取: {receipt.error}')
    return receipt.result


class _FiniteCancelEndCommand:
    def __init__(self, request, key):
        self.request, self.key = request, key

    def plan(self, scope):
        request = self.request
        state, action, activity = _load(scope, request)
        evidence = _evidence(state, request)
        saved = saved_transaction_events(scope.connection, self.key)
        if saved is not None:
            if (len(saved) != 1 or saved[0]['type'] != _TYPE or saved[0]['reason'] != _REASON
                    or saved[0]['occurred_at'] != request.occurred_at
                    or not json_equal(saved[0]['body']['evidence'], {'cancel_completion': evidence})
                    or saved[0]['body']['rows'][0]['id'] != activity['id']):
                raise TransactionError('有限取消结束的重送必须保留原完整申请')
            return CommandPlan((), {}, state, read_only=True)
        if activity['activity_state'] == 3:
            raise ConsistencyError('已结束活动不能补写新的有限取消结束依据')
        from camctl.persistence.event_reads import SqlEventReads
        _verify_files(SqlEventReads(scope.connection, _read_scope(request)), action, request, state)
        allocation = scope.allocate(1)
        _verify_sources(state, request, evidence, event_id=allocation.first_event_id)
        event = event_envelope(allocation.first_event_id, allocation.txn_id, _TYPE, _REASON,
            (update_change('device_activities', activity['id'], {'activity_state': activity['activity_state']}, {'activity_state': 3}),),
            request.occurred_at, evidence={'cancel_completion': evidence})
        return CommandPlan((event,), {('device_activities', activity['id']): ('action', action['id'])},
            state, capture_result_read_scope=_read_scope(request))


def save_finite_cancel_end(request, key, owned):
    receipt = commit_operation(_FiniteCancelEndCommand(request, key), key, owned)
    return DbOutcome(kind=DbOutcomeKind(receipt.kind), error=receipt.error)


def _guard(event, context):
    if (len(event.rows) != 1 or set(event.evidence) != {'cancel_completion'}
            or not isinstance(event.evidence['cancel_completion'], Mapping)):
        raise EventValidationError('有限取消结束只保存原活动与完整收场依据')
    row = event.rows[0]
    try:
        from .capture import _history_source_event
        from .result_pages import _decode
        evidence = event.evidence['cancel_completion']
        saved, _ = _decode(_history_source_event(context.state_rows, evidence['result_page_event_id']))
        activity = context.state_rows['device_activities'][row.row_id]
        request = FiniteCancelEndSave(activity['action_id'], saved.ref, event.occurred_at)
        _verify_sources(context.state_rows, request, evidence, event_id=event.event_id)
        action = context.state_rows['actions'][activity['action_id']]
        _verify_files(context.current_reads, action, request, context.state_rows)
    except (KeyError, ValueError, TypeError, ConsistencyError) as error:
        raise EventValidationError(f'有限取消结束依据无效: {error}') from error


def register_finite_cancel_guard():
    register_guard('finite_cancel_end', _guard)

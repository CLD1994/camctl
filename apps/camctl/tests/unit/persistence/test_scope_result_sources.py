"""范围收尾保留原停止来源及冻结等待，不改变动作终态。"""
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.devices.evidence import DeviceObservation
from camctl.history.events import load_event_registry
from camctl.operations.models import CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis
from camctl.operations.result_format import result_document
from camctl.persistence.repositories import capture, result_sources
from camctl.persistence.transaction import event_envelope, row_change, update_change


@pytest.fixture
def source_case(monkeypatch):
    events = {}
    monkeypatch.setattr(capture, '_history_source_event', create_autospec(
        capture._history_source_event, side_effect=lambda state, identity: events[identity]))

    def event(name, branch, identity, occurred_at, rows):
        definition = load_event_registry()['events'][name]
        return event_envelope(identity, identity, definition['id'],
            definition['branches'][branch]['reason'], rows, occurred_at)

    def build(*, kind='RESOLVE_OUTPUT_SCOPE', action_type=2, wait_ms=None, stop=False):
        action = {'id': 3, 'type': action_type, 'status': 4,
            'execution_spec_json': {} if wait_ms is None else {'file_completion_wait_ms': wait_ms}}
        activity = {'id': 8, 'action_id': 3, 'state': 3, 'occupancy_state': 1,
            'baseline_state': 3, 'sent_at': None, 'wait_completed_event_id': None}
        key = 'scope/8' if kind == 'RESOLVE_OUTPUT_SCOPE' else 'results/8'
        run = {'id': 5, 'kind': int(enum_for('operation_runs.kind')[kind]),
            'action_id': 3, 'activity_id': 8, 'responsibility_key': key}
        attempt = {'id': 9, 'run_id': 5, 'attempt_no': 1, 'intent_event_id': 30,
            'status': 1, 'effect_state': 2, 'result_event_id': None,
            'result_json': None, 'error_json': None}
        events[30] = event('ATTEMPT_STARTED', 'NEW', 30, 20_000_000,
            (row_change('operation_attempts', 9, {k: v for k, v in attempt.items() if k != 'id'}),))
        state = {'actions': {3: action}, 'device_activities': {8: activity},
            'operation_runs': {5: run}, 'operation_attempts': {9: attempt}}
        response = None
        if stop:
            outcome = CallOutcome(effect=EffectState.CONFIRMED,
                settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue('stop_returned', 1, {})),
                observations=(DeviceObservation('stop_confirmed', 1, {'activity_id': '8'}),))
            response = {'status': 2, 'effect_state': 3, 'result': result_document(outcome), 'error': None}
            stopped = {'id': 7, 'run_id': 6, 'attempt_no': 1, 'intent_event_id': 10,
                'status': response['status'], 'effect_state': response['effect_state'],
                'result_event_id': 20, 'result_json': response['result'], 'error_json': None}
            state['operation_runs'][6] = {'id': 6, 'kind': 2, 'action_id': 3,
                'activity_id': 8, 'responsibility_key': 'stop/3'}
            state['operation_attempts'][7] = stopped
            fields = ('status', 'effect_state', 'result_event_id', 'result_json', 'error_json')
            events[20] = event('ATTEMPT_RESULT', 'SUCCEED', 20, 10_000_000,
                (update_change('operation_attempts', 7,
                    dict(status=1, effect_state=2, result_event_id=None, result_json=None, error_json=None),
                    {field: stopped[field] for field in fields}),))
        return SimpleNamespace(action=action, activity=activity, run=run, attempt=attempt,
            state=state, response=response)

    return build


@pytest.mark.parametrize('status', [3, 4, 5, 6])
def test_scope_source_accepts_terminal_action(source_case, status):
    case = source_case(action_type=1)
    case.action['status'] = status
    assert result_sources.result_source_from_state(case.action, case.activity, case.attempt, case.state) == (
        result_sources.ResultSource(result_sources.ResultSourceKind.DIRECT, 30))


@pytest.mark.parametrize('kind', ['CHECK_CAPTURE_RESULTS', 'RESOLVE_OUTPUT_SCOPE'])
@pytest.mark.parametrize('action_type', [2, 3])
def test_scope_keeps_original_stop_and_wait(source_case, kind, action_type):
    case = source_case(kind=kind, action_type=action_type, wait_ms=3000, stop=True)
    actual = result_sources.result_source_from_state(case.action, case.activity, case.attempt, case.state)
    expected = {'activity_id': '8', 'stop_result_event_id': 20,
        'stop_returned_at_us': 10_000_000, 'stop_response': case.response,
        'file_completion_wait_ms': 3000, 'prior_completion': None}
    if action_type == 3:
        expected['kind'] = 'timelapse_stop'
    assert actual == result_sources.ResultSource(result_sources.ResultSourceKind.STOP,
        30, expected, requires_file_wait=True)


@pytest.mark.parametrize('kind,key', [
    ('RESOLVE_OUTPUT_SCOPE', 'results/8'), ('CHECK_CAPTURE_RESULTS', 'scope/8'),
])
def test_source_rejects_crossed_kind_and_key(source_case, kind, key):
    case = source_case(kind=kind)
    case.run['responsibility_key'] = key
    with pytest.raises(ConsistencyError):
        result_sources.result_source_from_state(case.action, case.activity, case.attempt, case.state)

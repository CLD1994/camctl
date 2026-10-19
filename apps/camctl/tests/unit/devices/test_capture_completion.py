"""完成上下文只接受原活动的完整控制依据和实际等待规则。"""
from copy import deepcopy
from decimal import Decimal

import pytest


def _response(observation):
    return {'status': 2, 'effect_state': 3, 'error': None,
        'result': {'format_version': 1, 'settlement': {'basis': 'observed',
            'evidence': {'type': 'control_returned', 'version': 1, 'data': {}}},
            'observations': [{'type': observation, 'version': 1,
                'data': {'activity_id': '7'}}]}}


def _natural():
    return {'kind': 'timelapse_natural', 'activity_id': '7',
        'start_result_event_id': 10, 'start_response': _response('timelapse_sent'),
        'sent_at_us': 1_000_000, 'wait_completed_event_id': 20,
        'wait_completed_at_us': 331_000_000, 'target_duration_ms': 300000,
        'result_wait_margin_ms': 30000, 'extra_wait_ms': 0,
        'expected_check_at_us': 331_000_000}


def _stop(kind='timelapse_stop'):
    return {'kind': kind, 'activity_id': '7', 'stop_result_event_id': 10,
        'stop_returned_at_us': 1000000, 'stop_response': _response('stop_confirmed'),
        'file_completion_wait_ms': 30000, 'prior_completion': None}


def test_native_natural_context_validates_original_sent_and_due_wait():
    from camctl.devices.capture_completion import validate_completion_context
    context = _natural()
    assert validate_completion_context(context, activity_id='7') == context


@pytest.mark.parametrize('changes', [
    {'activity_id': '8'}, {'wait_completed_event_id': 10},
    {'expected_check_at_us': 330_000_000}, {'wait_completed_at_us': 330_999_999},
    {'start_result_event_id': True}, {'target_duration_ms': 0},
    {'result_wait_margin_ms': -1}, {'extra_wait_ms': True},
    {'stop_result_event_id': 9}, {'start_response': _response('start_confirmed')},
])
def test_native_natural_context_rejects_wrong_identity_rules_or_stop_mixture(changes):
    from camctl.devices.capture_completion import validate_completion_context
    with pytest.raises(ValueError):
        validate_completion_context({**_natural(), **changes}, activity_id='7')


@pytest.mark.parametrize('kind', ['record_stop', 'timelapse_stop'])
def test_stop_context_keeps_original_stop_identity_and_wait_budget(kind):
    from camctl.devices.capture_completion import validate_completion_context, result_page_timeout_s
    context = _stop(kind)
    assert validate_completion_context(context, activity_id='7') == context
    assert result_page_timeout_s(Decimal('10'), context) == Decimal('40')


def test_legacy_record_stop_context_remains_readable():
    from camctl.devices.capture_completion import validate_completion_context
    context = _stop(); del context['kind']
    assert validate_completion_context(context, activity_id='7') == context


def test_actual_prior_wait_reuses_original_source_and_original_device_budget():
    from camctl.devices.capture_completion import result_page_timeout_s, validate_completion_context
    context = _stop()
    context['prior_completion'] = {'result_page_event_id': 15, 'evidence': {
        'method': 'stop_return_and_wait', 'version': 1, 'activity_id': '7',
        'stop_result_event_id': 10, 'required_wait_ms': 30000,
        'observed_wait_ns': 30_000_000_000, 'completed': True}}
    assert validate_completion_context(context, activity_id='7') == context
    assert result_page_timeout_s(Decimal('10'), context) == Decimal('10')
    context['prior_completion']['result_page_event_id'] = 9
    with pytest.raises(ValueError):
        validate_completion_context(context, activity_id='7')


@pytest.mark.parametrize('mutate', ['activity', 'stop', 'wait', 'incomplete', 'mixed'])
def test_stop_context_rejects_mismatched_control_and_natural_fields(mutate):
    from camctl.devices.capture_completion import validate_completion_context
    context = deepcopy(_stop())
    if mutate == 'activity': context['stop_response']['result']['observations'][0]['data']['activity_id'] = '8'
    elif mutate == 'stop': context['stop_response'] = _response('timelapse_sent')
    elif mutate == 'wait': context['file_completion_wait_ms'] = 0
    elif mutate == 'incomplete': context['stop_response']['effect_state'] = 2
    else: context['wait_completed_event_id'] = 19
    with pytest.raises(ValueError):
        validate_completion_context(context, activity_id='7')


@pytest.mark.parametrize('margin_ms,due_us', [(0, 331000000), (1000, 332000000)])
def test_new_natural_context_includes_frozen_startup_margin_once(margin_ms, due_us):
    from camctl.devices.capture_completion import validate_completion_context
    context = {**_natural(), 'startup_margin_ms': margin_ms,
        'expected_check_at_us': due_us, 'wait_completed_at_us': due_us}
    assert validate_completion_context(context, activity_id='7') == context
    for changes in ({'expected_check_at_us': due_us-1000000}, {'expected_check_at_us': due_us+1000000},
                    {'wait_completed_at_us': due_us-1}, {'startup_margin_ms': -1},
                    {'startup_margin_ms': True}):
        with pytest.raises(ValueError):
            validate_completion_context({**context, **changes}, activity_id='7')

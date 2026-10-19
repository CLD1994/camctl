"""结果读取使用的原控制与等待上下文；不从文件反推采集结束。"""
from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal
from enum import StrEnum

from camctl.contracts.values import MAX_OBJECT_ID, parse_object_id
from camctl.devices.recording_completion import is_completed_file_wait
from camctl.devices.capture_timing import CaptureTiming
from camctl.operations.models import AttemptStatus, EffectState
from camctl.operations.result_format import read_result_document


class CompletionContextKind(StrEnum):
    RECORD_STOP = 'record_stop'
    TIMELAPSE_STOP = 'timelapse_stop'
    TIMELAPSE_NATURAL = 'timelapse_natural'


_STOP_FIELDS = {'activity_id', 'stop_result_event_id', 'stop_returned_at_us',
    'stop_response', 'file_completion_wait_ms', 'prior_completion'}
_NATURAL_FIELDS = {'kind', 'activity_id', 'start_result_event_id', 'start_response',
    'sent_at_us', 'wait_completed_event_id', 'wait_completed_at_us',
    'target_duration_ms', 'result_wait_margin_ms', 'extra_wait_ms', 'expected_check_at_us'}


def _integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= MAX_OBJECT_ID:
        raise ValueError('完成上下文要求安全整数时间、规则与事件引用')
    return value


def validate_control_confirmation(response, observation_type, activity_id):
    """等待与完成上下文共同核对完整原结果的同活动可靠确认。"""
    if not isinstance(response, Mapping) or set(response) != {'status', 'effect_state', 'error', 'result'}:
        raise ValueError('完成上下文缺少完整原控制结果')
    actual = read_result_document(response['status'], response['effect_state'],
        response['result'], response['error'])
    if (actual.status is not AttemptStatus.SUCCEEDED or actual.effect is not EffectState.CONFIRMED
            or not any(observation.type == observation_type
                and observation.data.get('activity_id') == activity_id
                for observation in actual.observations)):
        raise ValueError('完成上下文没有原活动所需的可靠控制确认')


def is_stop_context(context) -> bool:
    return context is not None and context.get('kind') != CompletionContextKind.TIMELAPSE_NATURAL


def validate_completion_context(context, *, activity_id: str) -> dict | None:
    """检查完整原事实结构；持久化边界另核对这些引用实际属于原任务。"""
    if context is None:
        return None
    parse_object_id(activity_id)
    if not isinstance(context, Mapping) or context.get('activity_id') != activity_id:
        raise ValueError('完成上下文改变原活动')
    kind = (CompletionContextKind.RECORD_STOP if 'kind' not in context
            else CompletionContextKind(context['kind']))
    if kind is CompletionContextKind.TIMELAPSE_NATURAL:
        if set(context) != _NATURAL_FIELDS | ({"startup_margin_ms"} if "startup_margin_ms" in context else set()):
            raise ValueError('自然完成上下文缺少固定规则或混入停止字段')
        start_event = _integer(context['start_result_event_id'], 1)
        if _integer(context['wait_completed_event_id'], 1) <= start_event:
            raise ValueError('自然等待必须晚于原发送结果')
        sent = _integer(context['sent_at_us'])
        duration = _integer(context['target_duration_ms'], 1)
        margin = _integer(context['result_wait_margin_ms'])
        extra = _integer(context['extra_wait_ms'])
        due = _integer(context['expected_check_at_us'])
        startup = _integer(context['startup_margin_ms']) if 'startup_margin_ms' in context else 0
        if due != CaptureTiming(duration, startup, margin, extra).deadline_us(sent):
            raise ValueError('自然等待改变原发送时间或采用的等待规则')
        if _integer(context['wait_completed_at_us']) < due:
            raise ValueError('自然等待尚未满足原目标和余量')
        validate_control_confirmation(context['start_response'], 'timelapse_sent', activity_id)
    else:
        if set(context) != _STOP_FIELDS | ({'kind'} if 'kind' in context else set()):
            raise ValueError('停止完成上下文缺少原停止或混入自然等待字段')
        stop_event = _integer(context['stop_result_event_id'], 1)
        _integer(context['stop_returned_at_us'])
        wait = _integer(context['file_completion_wait_ms'], 1)
        validate_control_confirmation(context['stop_response'], 'stop_confirmed', activity_id)
        prior = context['prior_completion']
        if prior is not None:
            if (not isinstance(prior, Mapping) or set(prior) != {'result_page_event_id', 'evidence'}
                    or _integer(prior['result_page_event_id'], 1) <= stop_event
                    or not is_completed_file_wait(prior['evidence'], activity_id=activity_id,
                        stop_result_event_id=stop_event, required_wait_ms=wait)):
                raise ValueError('原实际等待与活动、停止、来源页或固定规则不符')
    return deepcopy(dict(context))


def result_page_timeout_s(device_budget_s: Decimal, context) -> Decimal:
    """实际停止后等待不消耗配置的设备结果读取预算。"""
    if (not isinstance(device_budget_s, Decimal) or not device_budget_s.is_finite()
            or device_budget_s <= 0):
        raise ValueError('结果读取要求有限正数设备预算')
    if is_stop_context(context) and context.get('prior_completion') is None:
        return device_budget_s + Decimal(context['file_completion_wait_ms']) / 1000
    return device_budget_s

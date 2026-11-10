"""原有限设备任务的取消收场资格。"""
from camctl.devices.tasks import CancelCompletionPolicy


def finite_cancel_policy(action) -> bool:
    """只消费首次固定定义；旧任务保持原取消规则。"""
    return (action['type'] == 3 and action['execution_spec_json'].get(
        'cancel_completion_policy') == CancelCompletionPolicy.FINITE_DEVICE_END.value)


def require_finite_cancel_end(action):
    """取消已生效且原冻结策略允许以有限任务自然结束收场。"""
    from camctl.contracts.values import ConsistencyError
    from camctl.devices.tasks import EndControl, StartReturn, CompletionMode
    spec = action['execution_spec_json']
    if (not finite_cancel_policy(action) or not action['cancel_requested']
            or spec.get('end_control') != int(EndControl.DEVICE)
            or spec.get('start_return_meaning') != int(StartReturn.SENT)
            or spec.get('completion_mode') != int(CompletionMode.TIME_AND_OUTPUTS)
            or spec.get('wait_after_send') is not True or spec.get('duration_based') is not True):
        raise ConsistencyError('有限取消结束要求原冻结策略、有限设备任务和已生效取消')


def require_failed_stop(stop, attempt):
    """有限取消仅在首个停止尝试失败且没有确认效果后继续。"""
    from camctl.contracts.enums import enum_for
    from camctl.contracts.values import ConsistencyError
    if (stop['status'] != int(enum_for('operation_runs.status').FAILED) or stop['attempts_used'] != 1
            or attempt['status'] == int(enum_for('operation_attempts.status').RUNNING)
            or attempt['effect_state'] == int(enum_for('operation_attempts.effect_state').CONFIRMED)):
        raise ConsistencyError('有限取消结束缺少第一次停止失败的终局责任')


def require_capture_sent(outcome, activity_id):
    """原启动结果必须可靠确认同一活动的发送。"""
    from camctl.contracts.values import ConsistencyError
    from camctl.operations.models import AttemptStatus, EffectState
    if (outcome.status is not AttemptStatus.SUCCEEDED or outcome.effect is not EffectState.CONFIRMED
            or not any(o.type == 'timelapse_sent' and o.data.get('activity_id') == str(activity_id) for o in outcome.observations)):
        raise ConsistencyError('有限取消结束缺少原活动的可靠 SENT')


def finite_cancel_file(entry, facts, action_id, rules):
    """逐条核对当前结果与固定文件事实，按原规则跳过非必要预览。"""
    from camctl.capture.results import CaptureFile, is_optional_preview
    complete = (entry.complete and facts['completion_state'] == 3
                and type(entry.size_bytes) is int and entry.size_bytes == facts['size_bytes'])
    owned = (facts['source_action_id'] == action_id and facts['ownership_evidence_json'] is not None)
    if (is_optional_preview(entry.kind, entry.format_id, entry.paired_identity, rules)
            and facts['source_action_id'] in (None, action_id)
            and (not complete or not owned or entry.size_bytes <= 0)):
        return None
    return CaptureFile(entry.identity, entry.kind, complete, owned,
        format_id=entry.format_id, pairing_confirmed=entry.paired_identity is not None,
        size_bytes=entry.size_bytes)


def finite_cancel_results_succeeded(run, attempt):
    """自然结果流程与实际尝试必须成功且没有调用错误。"""
    from camctl.contracts.enums import enum_for
    return (run['status'] == int(enum_for('operation_runs.status').SUCCEEDED)
            and attempt['status'] == int(enum_for('operation_attempts.status').SUCCEEDED)
            and attempt['error_json'] is None)


def finite_cancel_page_complete(page):
    """结束只能使用完整扫描且集合固定的无错误结果页。"""
    return page.scan_complete and page.set_finalized and page.outcome.error is None


def finite_cancel_timing_matches(activity, spec, sent_at, expected):
    """原发送锚点和冻结等待规则必须与当前活动一致。"""
    return (sent_at == activity['sent_at'] and activity['expected_check_at'] == expected
            and activity['result_wait_margin_ms'] == spec['result_wait_margin_ms'])

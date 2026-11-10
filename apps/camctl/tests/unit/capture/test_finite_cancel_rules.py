"""有限设备任务取消与停止失败的资格分区。"""

import pytest

from camctl.capture.finite_cancel import require_failed_stop, require_finite_cancel_end
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.devices.tasks import CancelCompletionPolicy, CompletionMode, EndControl, StartReturn


def test_finite_end_requires_original_frozen_policy_and_applied_cancel():
    action = {'type': 3, 'cancel_requested': 1, 'execution_spec_json': {
        'cancel_completion_policy': CancelCompletionPolicy.FINITE_DEVICE_END.value,
        'end_control': int(EndControl.DEVICE), 'start_return_meaning': int(StartReturn.SENT),
        'completion_mode': int(CompletionMode.TIME_AND_OUTPUTS), 'wait_after_send': True, 'duration_based': True}}
    require_finite_cancel_end(action)
    for invalid in ({**action, 'cancel_requested': 0},
                    {**action, 'execution_spec_json': {**action['execution_spec_json'], 'duration_based': False}}):
        with pytest.raises(ConsistencyError):
            require_finite_cancel_end(invalid)


def test_stop_must_be_failed_once_and_without_confirmed_effect():
    run = {'status': int(enum_for('operation_runs.status').FAILED), 'attempts_used': 1}
    attempt = {'status': int(enum_for('operation_attempts.status').FAILED),
               'effect_state': int(enum_for('operation_attempts.effect_state').NO_EFFECT)}
    require_failed_stop(run, attempt)
    for bad_run, bad_attempt in (({**run, 'attempts_used': 2}, attempt),
                                 (run, {**attempt, 'status': int(enum_for('operation_attempts.status').RUNNING)}),
                                 (run, {**attempt, 'effect_state': int(enum_for('operation_attempts.effect_state').CONFIRMED)})):
        with pytest.raises(ConsistencyError):
            require_failed_stop(bad_run, bad_attempt)


def test_file_uses_current_page_and_fixed_size_and_ownership():
    from types import SimpleNamespace
    from camctl.capture.finite_cancel import finite_cancel_file
    from camctl.capture.results import FileKind
    entry = SimpleNamespace(identity='video', kind=FileKind.VIDEO, complete=True,
        size_bytes=100, format_id='mp4', paired_identity=None)
    facts = {'completion_state': 3, 'size_bytes': 100, 'source_action_id': 7,
             'ownership_evidence_json': {'basis': 'fixed'}}
    file = finite_cancel_file(entry, facts, 7, ())
    assert file.complete and file.ownership_confirmed
    assert not finite_cancel_file(entry, {**facts, 'size_bytes': 101}, 7, ()).complete
    assert not finite_cancel_file(entry, {**facts, 'ownership_evidence_json': None}, 7, ()).ownership_confirmed

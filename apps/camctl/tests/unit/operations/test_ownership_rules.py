"""检查目标必须使用真实所属动作、载体与读取机会。"""

import pytest

from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.operations.attempts import OperationKind, QueryPurpose
from camctl.operations.ownership_rules import (require_obtain_check_action, require_obtain_check_output,
    require_obtain_check_carrier, require_obtain_check_config, require_host_read_slot,
    require_device_read_slot, read_round_matches, require_run_activity_owner)


def test_obtain_check_uses_requested_source_and_unique_carrier():
    dependency = {'action_id': 1, 'depends_on_action_id': 2}
    action = {'type': int(enum_for('actions.type').OBTAIN_ACTION_OUTPUTS)}
    item = {'output_id': 3, 'requested_output_id': 3}
    output = {'source_action_id': 2, 'device_file_id': 4, 'intermediate_file_id': None}
    require_obtain_check_action(dependency, action, 1)
    assert require_obtain_check_output(item, dependency, output) == (4, None)
    require_obtain_check_carrier(output, {'source_action_id': 2}, device=True)
    with pytest.raises(ConsistencyError):
        require_obtain_check_output(item, dependency, {**output, 'intermediate_file_id': 5})
    with pytest.raises(ConsistencyError):
        require_obtain_check_carrier(output, {'source_action_id': 8}, device=True)


def test_read_slot_preserves_binding_and_round():
    observer = {'device_id': 'camera', 'driver_id': 'driver'}
    require_device_read_slot('camera', observer, observer)
    require_host_read_slot(None)
    with pytest.raises(ConsistencyError):
        require_device_read_slot('other', observer, observer)
    with pytest.raises(ConsistencyError):
        require_host_read_slot('camera')
    assert read_round_matches(1, 1)
    assert not read_round_matches(1, 2)
    with pytest.raises(ConsistencyError):
        read_round_matches(True, 1)


def test_residual_activity_can_belong_to_original_action():
    require_run_activity_owner(OperationKind.STOP_RESIDUAL, None, 2, 1)
    require_run_activity_owner(OperationKind.QUERY_ACTIVITY, QueryPurpose.RESIDUAL_STOP_CONFIRMATION, 2, 1)
    with pytest.raises(ConsistencyError):
        require_run_activity_owner(OperationKind.START, None, 2, 1)


def test_host_and_device_obtain_checks_keep_different_timeout_contracts():
    require_obtain_check_config(host_source=True, timeout=None, interval=None)
    require_obtain_check_config(host_source=False, timeout=1, interval=0)
    with pytest.raises(ConsistencyError):
        require_obtain_check_config(host_source=True, timeout=1, interval=None)
    with pytest.raises(ConsistencyError):
        require_obtain_check_config(host_source=False, timeout=None, interval=0)


@pytest.mark.parametrize('cancelled,status,error,binding_error,expected', [
    (True, 'CANCELED', None, {'code': 'device_binding_unavailable'}, True),
    (True, 'FAILED', {'code': 'device_binding_unavailable'}, {'code': 'device_binding_unavailable'}, False),
    (True, 'CANCELED', {'code': 'x'}, {'code': 'device_binding_unavailable'}, False),
    (False, 'FAILED', {'code': 'device_binding_unavailable'}, {'code': 'device_binding_unavailable'}, True),
    (False, 'FAILED', {'code': 'other'}, {'code': 'device_binding_unavailable'}, False),
    (False, 'FAILED', None, {'code': 'device_binding_unavailable'}, False),
    (False, 'CANCELED', None, {'code': 'device_binding_unavailable'}, False),
])
def test_binding_read_finish_keeps_cancel_and_original_error_partition(cancelled, status, error, binding_error, expected):
    from camctl.operations.ownership_rules import binding_read_finish_matches
    status_code = int(enum_for('operation_runs.status')[status])
    assert binding_read_finish_matches(cancelled, status_code, error, binding_error) is expected

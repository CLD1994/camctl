"""软件恢复阶段依据业务事实决定，数据库事务负责提供原事实。"""

import pytest

from camctl.capture.recovery_rules import validate_recovery_transition
from camctl.capture.recovery_types import RecoveryPhase


def first_state(**changes):
    return {"reboots": 0, "checks": 0, "pages": 0,
            "prepared": False, "scope_finalized": False, **changes}


def test_initial_recovery_requires_empty_checking_and_cannot_end():
    state = first_state()
    validate_recovery_transition(None, state, phase=RecoveryPhase.CHECKING, ended=False, evidence={})
    with pytest.raises(ValueError, match="首次责任"):
        validate_recovery_transition(None, first_state(reboots=1), phase=RecoveryPhase.CHECKING, ended=False, evidence={})
    with pytest.raises(ValueError, match="首次责任"):
        validate_recovery_transition(None, state, phase=RecoveryPhase.SCANNING, ended=False, evidence={})
    with pytest.raises(ValueError, match="设备结束"):
        validate_recovery_transition(None, state, phase=RecoveryPhase.CHECKING, ended=True, evidence={})


def test_reliable_new_boot_is_required_to_end_activity():
    previous = {
        'phase': 'confirming', 'version': 1, 'origin_trigger_id': 1, 'trigger_id': 1,
        'device_id': 'camera', 'driver_id': 'driver', 'serial': 'serial', 'fingerprint': 'firmware',
        'created_at': 1, 'deadline_at': 100, 'timeout_s': '1', 'retry_interval_s': '1',
        'max_reboots': 1, 'max_checks': 4, 'max_pages': 2, 'baseline_first': None,
        'baseline_last': None, 'reboots': 1, 'checks': 1, 'pages': 0,
        'boot_before': 'old', 'boot_after': None, 'reported_serial': 'serial',
        'cursor': None, 'scope_pages': 0, 'scope_members': 0,
        'scope_first_event_id': None, 'scope_last_event_id': None,
        'prepared': False, 'scope_finalized': False,
    }
    state = {**previous, 'phase': 'preparing', 'boot_after': 'new'}
    evidence = {'identity': ['new', 'firmware', 'serial']}
    validate_recovery_transition(previous, state, phase=RecoveryPhase.PREPARING, ended=True, evidence=evidence)
    with pytest.raises(ValueError, match='设备结束'):
        validate_recovery_transition(previous, {**state, 'boot_after': 'old'},
                                     phase=RecoveryPhase.PREPARING, ended=True, evidence=evidence)
    with pytest.raises(ValueError, match='必须伴随设备结束'):
        validate_recovery_transition(previous, state, phase=RecoveryPhase.PREPARING, ended=False, evidence=evidence)


def test_recovery_owner_requires_retained_occupancy_and_matching_device():
    from camctl.capture.recovery_rules import validate_recovery_owner
    state = {'device_id': 'camera', 'driver_id': 'driver'}
    action = {'status': 4, 'device_id': 'camera', 'driver_id': 'driver'}
    activity = {'occupancy_state': 1, 'dispatch_state': 2}
    validate_recovery_owner(state, activity, action, ended=True)
    for invalid_activity, invalid_action in (({**activity, 'occupancy_state': 2}, action),
                                            (activity, {**action, 'device_id': 'other'}),
                                            ({**activity, 'dispatch_state': 1}, action)):
        with pytest.raises(ValueError):
            validate_recovery_owner(state, invalid_activity, invalid_action, ended=True)


def test_final_directory_page_completes_prepared_scanning():
    previous = {
        'phase': 'scanning', 'version': 1, 'origin_trigger_id': 1, 'trigger_id': 1,
        'device_id': 'camera', 'driver_id': 'driver', 'serial': 'serial', 'fingerprint': 'firmware',
        'created_at': 1, 'deadline_at': 100, 'timeout_s': '1', 'retry_interval_s': '1',
        'max_reboots': 1, 'max_checks': 4, 'max_pages': 2, 'baseline_first': None,
        'baseline_last': None, 'reboots': 1, 'checks': 1, 'pages': 1,
        'boot_before': 'old', 'boot_after': 'new', 'reported_serial': 'serial',
        'cursor': {'page': 1}, 'scope_pages': 1, 'scope_members': 0,
        'scope_first_event_id': 11, 'scope_last_event_id': 11,
        'prepared': True, 'scope_finalized': False,
    }
    state = {**previous, 'phase': 'ready', 'scope_finalized': True, 'cursor': None,
             'pages': 2, 'scope_pages': 2, 'scope_last_event_id': 12}
    validate_recovery_transition(previous, state, phase=RecoveryPhase.READY, ended=False,
                                 evidence={'directory': {'next_cursor': None}})
    with pytest.raises(ValueError, match='完整分页末页'):
        validate_recovery_transition(previous, state, phase=RecoveryPhase.READY, ended=False,
                                     evidence={'directory': {'next_cursor': {'page': 2}}})
    with pytest.raises(ValueError, match='范围游标和累计事实'):
        validate_recovery_transition(previous, state, phase=RecoveryPhase.READY, ended=False, evidence={})
    with pytest.raises(ValueError, match='就绪阶段必须具有完整范围事实'):
        validate_recovery_transition(previous, {**state, 'scope_finalized': False},
                                     phase=RecoveryPhase.READY, ended=False,
                                     evidence={'directory': {'next_cursor': None}})


def test_runtime_and_rules_share_the_same_phase_type():
    from camctl.capture import recovery_rules, software_recovery
    assert recovery_rules.RecoveryPhase is RecoveryPhase
    assert software_recovery.RecoveryPhase is RecoveryPhase
    assert RecoveryPhase('ready') is RecoveryPhase.READY
    assert RecoveryPhase.READY.value == 'ready'
    import pickle
    encoded = pickle.dumps(RecoveryPhase.READY)
    assert b'camctl.capture.software_recovery' in encoded
    assert pickle.loads(encoded) is RecoveryPhase.READY

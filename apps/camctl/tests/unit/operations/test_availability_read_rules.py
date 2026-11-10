"""设备检查必须沿用取回拷贝与来源的原身份和机会。"""
from types import SimpleNamespace

from camctl.contracts.enums import enum_for
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
from camctl.operations.availability_rules import (read_check_source_matches, read_check_action_active,
    read_check_binding_matches, read_holder_blocks_check)


def test_read_check_requires_original_output_and_live_obtain():
    action = {'id': 2, 'type': int(enum_for('actions.type').OBTAIN_ACTION_OUTPUTS),
              'status': int(enum_for('actions.status').RUNNING), 'cancel_requested': 0}
    copy, delivery, source, output = {}, {'action_id': 2}, {'id': 4}, {'device_file_id': 4}
    assert read_check_source_matches(action, copy, delivery, source, output)
    assert not read_check_source_matches(action, copy, delivery, source, {**output, 'device_file_id': 5})
    assert read_check_action_active(action)
    assert not read_check_action_active({**action, 'cancel_requested': 1})


def test_read_check_uses_original_binding_and_holder():
    fixed = SimpleNamespace(binding=SimpleNamespace(device_id='camera', driver_id=DRIVER_ID))
    action = {'id': 1, 'device_id': 'camera', 'driver_id': DRIVER_ID, 'scheduled_at': 100}
    assert read_check_binding_matches({'source_action_id': 1}, action, action, fixed)
    assert not read_check_binding_matches({'source_action_id': 1}, action, {**action, 'device_id': 'other'}, fixed)
    assert read_holder_blocks_check(3, 2, action, 100)
    assert not read_holder_blocks_check(2, 2, action, 100)
    assert read_holder_blocks_check(3, None, action, 99)
    assert not read_holder_blocks_check(3, None, action, 100)

"""设备检查所使用的原取回和读取资格。"""

from camctl.contracts.enums import enum_for


def read_check_source_matches(action, copy, delivery, source, output):
    """检查触发拷贝须属于原取回动作及同一设备产物。"""
    return (copy is not None and delivery is not None and source is not None and output is not None
            and output['device_file_id'] == source['id'] and delivery['action_id'] == action['id']
            and action['type'] == int(enum_for('actions.type').OBTAIN_ACTION_OUTPUTS))


def read_check_action_active(action):
    """只有运行中且未取消的取回动作可触发读取检查。"""
    return action['status'] == int(enum_for('actions.status').RUNNING) and not action['cancel_requested']


def read_check_binding_matches(output, origin, observer, fixed):
    """来源动作、观察者和文件固定机身必须属于同一设备。"""
    from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
    binding = (fixed.binding.device_id, fixed.binding.driver_id)
    return (origin is not None and observer is not None and output['source_action_id'] == origin['id']
            and binding[1] == DRIVER_ID and (origin['device_id'], origin['driver_id']) == binding
            and (observer['device_id'], observer['driver_id']) == binding)


def read_holder_blocks_check(holder, trigger_copy_id, action, now):
    """读取机会由其他拷贝持有时不越过其原责任。"""
    return (holder is not None and ((trigger_copy_id is not None and holder != trigger_copy_id)
            or (trigger_copy_id is None and now < action["scheduled_at"])))

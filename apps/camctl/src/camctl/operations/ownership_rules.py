"""读取及取回检查的来源归属、机会与配置规则。"""

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import is_json_integer, json_equal
from camctl.contracts.values import ConsistencyError


def require_obtain_check_action(dependency, action, action_id):
    if (not json_equal(dependency.get('action_id'), action_id)
            or action.get('type') != int(enum_for('actions.type').OBTAIN_ACTION_OUTPUTS)):
        raise ConsistencyError('obtain CHECK 不属于原获取动作')


def require_obtain_check_output(item, dependency, output):
    if not json_equal(output.get('source_action_id'), dependency.get('depends_on_action_id')):
        raise ConsistencyError('obtain CHECK 的请求产物不属于原来源依赖')
    if item.get('output_id') is not None and not json_equal(item['output_id'], item['requested_output_id']):
        raise ConsistencyError('obtain CHECK 的已选产物与原请求身份不符')
    device, local = output.get('device_file_id'), output.get('intermediate_file_id')
    if (device is None) == (local is None):
        raise ConsistencyError('obtain CHECK 产物必须恰有一种实际文件载体')
    return device, local


def require_obtain_check_carrier(output, carrier, *, device):
    source_column = 'source_action_id' if device else 'owner_action_id'
    if not json_equal(carrier.get(source_column), output.get('source_action_id')):
        raise ConsistencyError('obtain CHECK 的实际载体与原产物来源不符')


def require_obtain_check_config(*, host_source, timeout, interval):
    if (host_source and (timeout is not None or interval is not None)
            or not host_source and (timeout is None or interval is None)):
        raise ConsistencyError('obtain CHECK 的配置必须符合真实主机或设备载体分区')


def require_host_read_slot(slot):
    if slot is not None:
        raise ConsistencyError('主机源拷贝不能持有相机读取机会')


def require_device_read_slot(slot, observer, origin):
    binding = (observer['device_id'], observer['driver_id'])
    if (not all(isinstance(value, str) and value for value in binding)
            or binding != (origin['device_id'], origin['driver_id'])):
        raise ConsistencyError('读取源观察者与可靠来源的原设备绑定不一致')
    if slot is not None and slot != binding[0]:
        raise ConsistencyError('拷贝的读取机会不属于原来源设备')


def read_round_matches(current_round, requested_round):
    if not is_json_integer(current_round) or current_round < 1:
        raise ConsistencyError('当前拷贝轮次无效')
    return json_equal(requested_round, current_round)


def require_run_activity_owner(kind, purpose, action_id, activity_action_id):
    from camctl.operations.attempts import OperationKind, QueryPurpose
    residual = kind is OperationKind.STOP_RESIDUAL or purpose is QueryPurpose.RESIDUAL_STOP_CONFIRMATION
    if not residual and not json_equal(activity_action_id, action_id):
        raise ConsistencyError('流程与目标活动所属动作不符')


def require_copy_read_owner(action_id, parent):
    if not json_equal(parent.get('action_id'), action_id):
        raise ConsistencyError('读取流程与拷贝所属动作不符')


def start_camera_matches(original, current, resource_key):
    """新启动只能使用活动首次固定的同一机身和资源键。"""
    return current == original and resource_key == original.resource_key


def binding_read_finish_matches(cancel_requested, status, error, binding_error):
    """已取消的读取按取消结束，否则保留原绑定失败。"""
    from collections.abc import Mapping
    statuses = enum_for('operation_runs.status')
    if cancel_requested:
        return status == int(statuses.CANCELED) and error is None
    return (status == int(statuses.FAILED) and isinstance(error, Mapping)
            and json_equal(error, binding_error))

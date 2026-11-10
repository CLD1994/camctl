"""分段取回查询的可靠在场结果及逐项结算。"""

from camctl.contracts.json_values import json_equal
from camctl.contracts.values import ConsistencyError
from camctl.contracts.workflow_errors import item_error_id, validate_error_details


_UNAVAILABLE = item_error_id('obtain_items', 'output_unavailable')
_UNCONFIRMED = item_error_id('obtain_items', 'source_file_unconfirmed')
_BINDING = item_error_id('obtain_items', 'device_binding_unavailable')


def query_presence_from_outcome(outcome, item_id):
    """从一次实际调用结果读取与原取回项绑定的唯一 v2 观察。"""
    observations = [value for value in outcome.observations
                    if value.type == 'file_presence' and value.version == 2]
    if len(observations) > 1:
        raise ConsistencyError('QUERY v2 结果重复报告在场事实')
    if observations:
        observation = observations[0]
        if observation.data.get('obtain_item_id') != str(item_id):
            raise ConsistencyError('QUERY v2 观察改变原取回项身份')
        return observation.data['present']
    return None


def require_query_operation_source(*, kind, target, item_id, responsibility,
                                   action_type, dependency, owner_id, source):
    """原调用必须属于显式取回项及其关联的真实来源动作。"""
    if (kind != 5 or target != item_id or responsibility != f'obtain-exists/{item_id}'
            or action_type != 4 or dependency is None or dependency[0] != owner_id
            or source is None):
        raise ConsistencyError('QUERY v2 责任不属于原分段来源取回')


def require_query_target(*, item, selection, owner, output, source_id):
    """只有执行中的显式未解析项及其原产物可以进行查询结算。"""
    if (item['status'] != 1 or item['basis'] != 5 or item['output_id'] is not None
            or selection['status'] != 2 or selection['error_code'] is not None
            or owner['status'] != 2 or owner['cancel_requested']
            or output is None or output['source_action_id'] != source_id):
        raise ConsistencyError('QUERY 结算要求执行中显式未解析项和原分段产物')


def require_query_observable_output(output):
    """实际查询只应用于未知且没有清理限制的设备产物。"""
    if (output['device_file_id'] is None or output['availability'] != 5
            or output['cleanup_status'] not in (1, 6)):
        raise ConsistencyError('只有未知且无清理限制的显式设备产物可应用 QUERY v2')


def require_query_observed_file(file, present):
    """产物变化必须跟随同一在场结果的实际文件事实。"""
    if file is None or file['presence_state'] != (2 if present else 3):
        raise ConsistencyError('文件存在性事实尚未先行保存')


def queried_output_values(present, output_id):
    """根据真实 QUERY 在场结果生成产物可用状态及错误事实。"""
    return {
        'availability': 1 if present else 4,
        'error_json': None if present else {
            'code': 'output_unavailable', 'stage': 'output_selection',
            'details': {'output_id': str(output_id), 'availability': 'missing'},
        },
    }


def require_query_output_projection(*, present, output, file, before, after):
    """原文件已保存同一在场事实，产物才能从未知推进到对应状态。"""
    expected = queried_output_values(present, output['id'])
    if (output['device_file_id'] is None or file is None
            or file['presence_state'] != (2 if present else 3)
            or set(after) != {'availability', 'error_json'}
            or after.get('availability') != expected['availability']
            or not json_equal(after.get('error_json'), expected['error_json'])
            or before.get('availability') != 5):
        raise ConsistencyError('QUERY v2 产物投影与原文件结果不符')


def require_query_resolution(*, present, resolve):
    """可靠在场用于解析，可靠缺席用于拒绝。"""
    if resolve and present is not True:
        raise ConsistencyError('显式取回解析必须由可靠在场结果证明')
    if not resolve and present is not False:
        raise ConsistencyError('缺席拒绝必须由可靠缺席结果证明')


def queried_item_values(*, output, present, error_code, error_details, result_event_id):
    """形成原显式取回项的可靠结算；显式错误保留已登记详情。"""
    if error_code is None:
        if present is None:
            raise ConsistencyError('有实际 QUERY 的结算必须引用可靠观察')
        if present and output['availability'] != 1:
            raise ConsistencyError('在场解析要求产物已更新为可用')
        if not present and output['availability'] != 4:
            raise ConsistencyError('缺席拒绝要求产物已更新为缺失')
        error_code = None if present else _UNAVAILABLE
        details = None if present else {'output_id': str(output['id']), 'availability': 'missing'}
    else:
        details = dict(error_details)
    name = {_UNAVAILABLE: 'output_unavailable', _UNCONFIRMED: 'source_file_unconfirmed',
            _BINDING: 'device_binding_unavailable'}.get(error_code)
    if error_code is not None and name is None:
        raise ConsistencyError('QUERY 结算错误未登记')
    if name is not None:
        validate_error_details(name, details)
    if error_code == _UNAVAILABLE and result_event_id is None:
        availability = {2: 'restricted', 3: 'cleaned', 4: 'missing'}.get(output['availability'])
        if availability is None or not json_equal(
                details, {'output_id': str(output['id']), 'availability': availability}):
            raise ConsistencyError('产物不可用必须保留当前可靠状态')
    return ({'output_id': output['id'], 'status': 2} if error_code is None else
            {'output_id': output['id'], 'status': 4, 'error_code': error_code,
             'error_details_json': details})

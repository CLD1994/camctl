"""权威报告 Schema 与分段错误登记的完整对象接受域对照。"""
from copy import deepcopy
from dataclasses import replace

import pytest

from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.schemas import create_validator, validation_errors
from camctl.contracts.workflow_errors import _public_json_registry
from camctl.reporting.stream_validation import (
    CompletionRequirement, DeferredField, FieldKind, NodeBinding,
    ProjectionDescriptor, ReadEnd, StreamValidationError, derive_stream_plan,
)

DRAFT = 'https://json-schema.org/draft/2020-12/schema'
OUTPUTS = ('outputs',)
DELIVERIES = ('deliveries',)
SEGMENT_PATHS = (
    ('error', 'details', 'gaps'),
    ('error', 'details', 'retained_output_ids'),
    ('error', 'details', 'default_scope', 'output_ids'),
)


def _plan(name, fields, completions=()):
    return derive_stream_plan(
        'status-report.schema.json#/$defs/' + name,
        ProjectionDescriptor('/integration/' + name, fields, completions),
        registry=_public_json_registry(),
    )


def _binding(plan):
    return NodeBinding('db-1', 'action', 10, HistoryBoundary(2, 40),
                       ('plans', 1, 'actions', 10), plan.schema_uri, plan.projection.pointer)


def _original_accepts(name, document):
    validator = create_validator(
        {'$schema': DRAFT, '$ref': 'status-report.schema.json#/$defs/' + name},
        registry=_public_json_registry(),
    )
    return not validation_errors(validator, document)


def _finish(gate, path):
    gate.exhaust_array(path, ReadEnd(gate.binding, path, gate.count(path), gate.last_position(path)))


def _action(kind='camera_record', status='succeeded'):
    action = {'action_instance_id': '10', 'name': '拍摄', 'type': kind, 'status': status}
    if kind.startswith('camera_'):
        action.update(device_id='cam-1', scheduled_at='2025-01-15 09:00:00',
                      input_params={'type': 'record'}, effective_params={'type': 'record'}, policy={})
    if status == 'expired':
        action['expiration_reason'] = 'window_missed'
    return action


def _output():
    return {
        'output_id': '20', 'source_action_instance_id': '10', 'kind': 'original',
        'availability': 'available', 'cleanup': {'status': 'not_requested'},
        'checksum': {'status': 'not_obtained'},
        'media': {'check_status': 'not_performed', 'duration': {'status': 'unknown'}},
    }


def _delivery():
    return {'delivery_id': '30', 'output_id': '20', 'source_action_instance_id': '10',
            'file_name': '30.mp4', 'display_name': '录像.mp4', 'status': 'pending'}


def _stream_action(document):
    plan = _plan('action', (DeferredField(OUTPUTS), DeferredField(DELIVERIES)))
    fields = deepcopy(document)
    children = {path: fields.pop(path[0]) for path in (OUTPUTS, DELIVERIES) if path[0] in fields}
    gate = plan.begin(_binding(plan), fields)
    for path in (OUTPUTS, DELIVERIES):
        if path not in children:
            gate.omit(path)
            continue
        gate.open_array(path)
        for index, item in enumerate(children[path]):
            gate.accept_item(path, item, position=index)
        _finish(gate, path)
    return gate


@pytest.mark.parametrize('case', ['pending', 'noncamera', 'nonobtain', 'admission_failure'])
@pytest.mark.parametrize('count', [0, 1, 2])
def test_legal_child_cannot_bypass_action_parent_conditions(case, count):
    document = _action()
    if case == 'pending':
        document['status'] = 'pending'
    elif case == 'noncamera':
        document = _action('report_status', 'running')
    elif case == 'admission_failure':
        document = {'action_instance_id': '10', 'name': '拒绝受理', 'type': 'camera_record', 'status': 'failed'}
        document['error'] = {'code': 'future_error', 'stage': 'admission', 'details': {}}
    field, item_name, item = (DELIVERIES, 'delivery', _delivery()) if case == 'nonobtain' else (OUTPUTS, 'output', _output())
    assert _original_accepts('action', document)
    assert _original_accepts(item_name, item)
    document[field[0]] = [item] * count
    assert not _original_accepts('action', document)
    with pytest.raises(StreamValidationError):
        _stream_action(document).close_node()


@pytest.mark.parametrize('kind', ['camera_record', 'camera_take_photo', 'camera_timelapse'])
@pytest.mark.parametrize('status', ['running', 'expired', 'canceled'])
@pytest.mark.parametrize('count', [0, 1, 2])
def test_current_action_count_constraints_match_original_validator(kind, status, count):
    document = _action(kind, status)
    document['outputs'] = [_output()] * count
    expected = count == 0 or (kind != 'camera_record' and status == 'canceled')
    assert _original_accepts('action', document) is expected
    gate = _stream_action(document)
    if expected:
        gate.close_node()
    else:
        with pytest.raises(StreamValidationError):
            gate.close_node()


@pytest.mark.parametrize('name,field', [('obtain_result', 'failures'), ('delete_result', 'items'), ('cancel_result', 'items')])
def test_required_ordinary_arrays_are_not_optional_empty_entities(name, field):
    plan = _plan(name, (DeferredField((field,)),))
    assert not _original_accepts(name, {})
    assert _original_accepts(name, {field: []})
    omitted = plan.begin(_binding(plan), {})
    omitted.omit((field,))
    with pytest.raises(StreamValidationError):
        omitted.close_node()
    empty = plan.begin(_binding(plan), {})
    empty.open_array((field,))
    _finish(empty, (field,))
    empty.close_node()


@pytest.mark.parametrize('delivery,output,expected', [(False, False, True), (False, True, True), (True, True, True), (True, False, False)])
def test_shared_obtain_failure_dependent_required_acceptance_matrix(delivery, output, expected):
    fields = tuple(DeferredField((name,), kind=FieldKind.VALUE) for name in ('delivery_id', 'output_id'))
    plan = _plan('obtain_failure', fields)
    document = {'source_action_instance_id': '10', 'error': {'code': 'future_error', 'stage': 'obtain', 'details': {}}}
    gate = plan.begin(_binding(plan), document)
    for name, present, value in [('delivery_id', delivery, '30'), ('output_id', output, '20')]:
        if present:
            document[name] = value
            gate.set_value((name,), value)
        else:
            gate.omit((name,))
    assert _original_accepts('obtain_failure', document) is expected
    if expected:
        gate.close_node()
    else:
        with pytest.raises(StreamValidationError):
            gate.close_node()


def test_shared_obtain_failure_unknown_presence_blocks_vacuous_dependency():
    fields = tuple(DeferredField((name,), kind=FieldKind.VALUE) for name in ('delivery_id', 'output_id'))
    plan = _plan('obtain_failure', fields)
    gate = plan.begin(_binding(plan), {'source_action_instance_id': '10', 'error': {'code': 'future_error', 'stage': 'obtain', 'details': {}}})
    gate.omit(('delivery_id',))
    with pytest.raises(StreamValidationError):
        gate.close_node()


def _source_failure():
    return {'source_action_instance_id': '10', 'error': {
        'code': 'default_segment_scope_unconfirmed', 'stage': 'output_selection',
        'details': {'source_action_instance_id': '10', 'description': '范围未确认', 'gap_count': None,
                    'default_scope': {'state': 'unknown', 'member_count': None, 'output_ids': []},
                    'retained_original_count': 0, 'gaps': [], 'retained_output_ids': []},
    }}


def _source_plan():
    return _plan('obtain_failure', tuple(
        DeferredField(path, unique_by='segment_expansion' if path != SEGMENT_PATHS[0] else None)
        for path in SEGMENT_PATHS
    ), (CompletionRequirement('segment_expansion', ('error', 'details'), SEGMENT_PATHS[1:]),))


class _ContractProof:
    """仅证明 A1 端口的绑定消费；不充当真实 I2 历史生产者。"""
    def __init__(self, expected):
        self.expected = expected
        self.used = False

    def consume(self, request):
        assert not self.used
        assert request.plan is self.expected.plan
        assert request == self.expected
        self.used = True


def test_real_source_scope_error_requires_three_exhaustions_and_business_proof():
    from camctl.reporting.stream_validation import CompletionRequest
    document = _source_failure()
    assert _original_accepts('obtain_failure', document)
    plan = _source_plan()
    header = deepcopy(document)
    del header['error']['details']['gaps']
    del header['error']['details']['retained_output_ids']
    del header['error']['details']['default_scope']['output_ids']
    gate = plan.begin(_binding(plan), header)
    for path in SEGMENT_PATHS:
        gate.open_array(path)
    gate.supply_completion('segment_expansion', _ContractProof(CompletionRequest(
        gate.binding, plan.projection.completions[0], tuple((path, 0) for path in SEGMENT_PATHS), plan)))
    _finish(gate, SEGMENT_PATHS[0])
    _finish(gate, SEGMENT_PATHS[1])
    with pytest.raises(StreamValidationError):
        gate.close_node()
    _finish(gate, SEGMENT_PATHS[2])
    gate.close_node()


@pytest.mark.parametrize('failure', ['not_exhausted', 'last_page_error', 'wrong_h'])
def test_source_scope_completion_does_not_replace_array_exhaustion(failure):
    document = _source_failure()
    plan = _source_plan()
    for path in SEGMENT_PATHS:
        parent = document
        for name in path[:-1]:
            parent = parent[name]
        del parent[path[-1]]
    gate = plan.begin(_binding(plan), document)
    for path in SEGMENT_PATHS:
        gate.open_array(path)
    if failure == 'last_page_error':
        gate.fail(RuntimeError('末页失败'))
    elif failure == 'wrong_h':
        with pytest.raises(StreamValidationError):
            gate.exhaust_array(SEGMENT_PATHS[0], ReadEnd(
                replace(gate.binding, boundary=HistoryBoundary(3, 41)), SEGMENT_PATHS[0], 0, None))
    with pytest.raises(StreamValidationError):
        gate.close_node()


@pytest.mark.parametrize('delivery', [False, True])
def test_shared_schema_ordinary_item_and_delivery_failures_close(delivery):
    fields = tuple(DeferredField(path, identity=int if path != SEGMENT_PATHS[0] else None) for path in SEGMENT_PATHS)
    plan = _plan('obtain_failure', fields)
    document = {'source_action_instance_id': '10', 'output_id': '20',
                'error': {'code': 'future_error', 'stage': 'obtain', 'details': {}}}
    if delivery:
        document['delivery_id'] = '30'
    assert _original_accepts('obtain_failure', document)
    gate = plan.begin(_binding(plan), document)
    # 无分段详情的合法省略由投影适用性给出；不要求 I2 分段业务凭据。
    gate.omit(SEGMENT_PATHS[0])
    gate.omit(SEGMENT_PATHS[1])
    gate.close_node()


def test_real_error_details_failure_result_and_action_require_nested_receipts():
    from camctl.reporting.stream_validation import CompletionRequest
    shared = _public_json_registry()
    details_fields = tuple(DeferredField(path[2:], unique_by='segment_expansion' if path != SEGMENT_PATHS[0] else None) for path in SEGMENT_PATHS)
    details_plan = derive_stream_plan(
        'workflow-codes.json#/codes/default_segment_scope_unconfirmed/details_schema',
        ProjectionDescriptor('/source_failure/error/details', details_fields,
                             (CompletionRequirement('segment_expansion', (), tuple(path[2:] for path in SEGMENT_PATHS[1:])),)), registry=shared)
    error_plan = _plan('error', (DeferredField(('details',), kind=FieldKind.OBJECT, node=details_plan),))
    failure_projection = ProjectionDescriptor('/source_failure', (
        DeferredField(('error',), kind=FieldKind.OBJECT, node=error_plan),))
    result_plan = _plan('obtain_result', (DeferredField(('failures',), item=failure_projection),))
    action_plan = _plan('action', (DeferredField(('result',), kind=FieldKind.OBJECT, node=result_plan),))
    document = {'action_instance_id': '11', 'name': '取回', 'type': 'obtain_action_outputs',
                'status': 'running', 'scheduled_at': '2025-01-15 09:00:00', 'input_params': {},
                'result': {'failures': [_source_failure()]}}
    assert _original_accepts('action', document)
    action_binding = replace(_binding(action_plan), owner_id=11, occurrence=('plans', 1, 'actions', 11))
    action = action_plan.begin(action_binding, {k: v for k, v in document.items() if k != 'result'})
    result = result_plan.begin(action.object_binding(('result',)), {})
    result.open_array(('failures',))
    failure_plan = result.item_plan(('failures',))
    failure = failure_plan.begin(result.child_binding(('failures',), 1), {'source_action_instance_id': '10'})
    error = error_plan.begin(failure.object_binding(('error',)), {
        'code': 'default_segment_scope_unconfirmed', 'stage': 'output_selection'})
    header = deepcopy(_source_failure()['error']['details'])
    del header['gaps']
    del header['retained_output_ids']
    del header['default_scope']['output_ids']
    details = details_plan.begin(error.object_binding(('details',)), header)
    for spec in details_fields:
        details.open_array(spec.path)
        _finish(details, spec.path)
    # Schema 本身接受有限头；本阶段仍明确要求未来 I2 的真实完整凭据。
    with pytest.raises(StreamValidationError):
        details.close_node()
    details.supply_completion('segment_expansion', _ContractProof(CompletionRequest(
        details.binding, details_plan.projection.completions[0], tuple((spec.path, 0) for spec in details_fields), details_plan)))
    error.accept_object(('details',), details.close_node())
    failure.accept_object(('error',), error.close_node())
    result.accept_item(('failures',), failure.close_node(), position=1)
    with pytest.raises(StreamValidationError):
        result.close_node()
    _finish(result, ('failures',))
    action.accept_object(('result',), result.close_node())
    action.close_node()


@pytest.mark.parametrize('tail', ['bad_item', 'page_error'])
def test_cross_page_tail_failure_never_yields_parent_receipt(tail):
    plan = _plan('delete_result', (DeferredField(('items',)),))
    gate = plan.begin(_binding(plan), {})
    gate.open_array(('items',))
    # 两页前缀合法；末页才出现损坏或读取错误。
    for position in (1, 2):
        gate.accept_item(('items',), {'output_id': str(position), 'status': 'pending'}, position=position)
    if tail == 'bad_item':
        gate.accept_item(('items',), {'output_id': '3', 'status': 'succeeded'}, position=3)
        _finish(gate, ('items',))
    else:
        gate.fail(RuntimeError('末页读取失败'))
    with pytest.raises(StreamValidationError):
        gate.close_node()

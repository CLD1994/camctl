"""逐层报告编码：只隔离资源/投影边界，测试真实编码状态与字节。"""
import gc
import weakref
from decimal import Decimal, localcontext

import pytest

from camctl.reporting import encoding
from camctl.reporting.encoding import ReportDocument, ReportStream, chunk_bytes, encode_report

from .test_encoding import fragments


def test_nested_entity_stream_matches_existing_small_encoder(fragments):
    fragments[('plan', 1)] = {'plan_instance_id': '1', 'actions': []}
    expected = encode_report(ReportDocument('7', 0, 5, (('plan', 1, {}),)), {})
    stream = ReportStream(ReportDocument('7', 0, 5))
    tokens = list(stream.open())
    tokens.extend(stream.open_array('plans'))
    tokens.extend(stream.open_object(identity=1))
    tokens.extend(stream.field('plan_instance_id', '1'))
    tokens.extend(stream.open_array('actions'))
    tokens.extend(stream.close_array())
    tokens.extend(stream.close_object())
    tokens.extend(stream.close_array())
    tokens.extend(stream.finish())
    assert b''.join(tokens) == expected


def test_interleaved_emission_failure_cannot_resume_original_document():
    stream = ReportStream(ReportDocument('7', 0, 5))
    opening = stream.open()
    assert next(opening) == b'{'
    with pytest.raises(ValueError):
        list(stream.finish())
    with pytest.raises(ValueError):
        next(opening)


_EMISSIONS = [
    ('open', (), {}), ('section', ('plans', None), {}), ('field', ('name', 'finite'), {}),
    ('item', (1,), {}), ('open_object', (), {'identity': 1}),
    ('open_array', ('plans',), {}), ('close_object', (), {}),
    ('close_array', (), {}), ('finish', (), {}),
]


def _call_emission(stream, emission, fragments):
    name, args, keywords = emission
    if name == 'section':
        args = ('plans', fragments[('plan', 1)])
    return getattr(stream, name)(*args, **keywords)


@pytest.mark.parametrize('emission', _EMISSIONS, ids=lambda item: item[0])
@pytest.mark.parametrize('position', ['section-array-end', 'section-document-end', 'nodes-document-end'])
def test_active_finish_interleaving_rejects_and_aborts_remaining_tokens(position, emission, fragments):
    stream = ReportStream(ReportDocument('7', 0, 5))
    list(stream.open())
    if position.startswith('section'):
        list(stream.section('plans', fragments[('plan', 1)]))
    else:
        list(stream.open_array('plans'))
        list(stream.close_array())
    ending = stream.finish()
    first = next(ending)
    assert first == (b']' if position.startswith('section') else b'}\n')
    if position == 'section-document-end':
        assert next(ending) == b'}\n'
    attempted = []
    with pytest.raises(ValueError):
        attempted.extend(_call_emission(stream, emission, fragments))
    assert attempted == []
    resumed = []
    with pytest.raises(ValueError):
        resumed.extend(ending)
    assert resumed == []  # 已经交出的 token 无法撤销；不再追加剩余 token。
    assert stream._failed


def _active_emission(name, fragments):
    stream = ReportStream(ReportDocument('7', 0, 5))
    if name == 'open':
        return stream, stream.open()
    list(stream.open())
    if name == 'section':
        return stream, stream.section('plans', fragments[('plan', 1)])
    if name == 'finish':
        return stream, stream.finish()
    list(stream.open_array('plans'))
    list(stream.open_object(identity=1))
    list(stream.field('plan_instance_id', '1'))
    if name == 'field':
        return stream, stream.field('z', 'finite')
    if name == 'open_object':
        return stream, stream.open_object('z')
    if name == 'open_array':
        return stream, stream.open_array('z')
    if name == 'close_object':
        return stream, stream.close_object()
    list(stream.open_array('z'))
    return stream, stream.item(1) if name == 'item' else stream.close_array()


@pytest.mark.parametrize('emission', _EMISSIONS, ids=lambda item: item[0])
@pytest.mark.parametrize('active', [item[0] for item in _EMISSIONS])
def test_each_active_emission_aborts_after_any_interleaved_api(active, emission, fragments):
    stream, pending = _active_emission(active, fragments)
    assert isinstance(next(pending), bytes)
    attempted = []
    with pytest.raises(ValueError):
        attempted.extend(_call_emission(stream, emission, fragments))
    assert attempted == []
    resumed = []
    with pytest.raises(ValueError):
        resumed.extend(pending)
    assert resumed == []
    assert stream._failed


@pytest.mark.parametrize('active', [item[0] for item in _EMISSIONS])
def test_explicit_close_of_each_active_emission_leaves_terminal_failure(active, fragments):
    stream, pending = _active_emission(active, fragments)
    next(pending)
    pending.close()
    assert stream._failed
    for emission in _EMISSIONS:
        attempted = []
        with pytest.raises(ValueError):
            attempted.extend(_call_emission(stream, emission, fragments))
        assert attempted == []


@pytest.mark.parametrize('emission', _EMISSIONS, ids=lambda item: item[0])
def test_reliably_exhausted_finish_clears_frames_and_rejects_without_bytes(emission, fragments):
    stream = _plan_stream()
    list(stream.field('plan_instance_id', '2'))
    list(stream.close_object())
    list(stream.close_array())
    assert list(stream.finish()) == [b'}\n']
    assert stream._frames == []
    attempted = []
    with pytest.raises(ValueError):
        attempted.extend(_call_emission(stream, emission, fragments))
    assert attempted == []


@pytest.mark.parametrize('emission', _EMISSIONS, ids=lambda item: item[0])
def test_structure_failure_rejects_all_apis_without_new_bytes(emission, fragments):
    stream = ReportStream(ReportDocument('7', 0, 5))
    list(stream.open())
    with pytest.raises(ValueError):
        list(stream.close_array())
    attempted = []
    with pytest.raises(ValueError):
        attempted.extend(_call_emission(stream, emission, fragments))
    assert attempted == []


def _value(stream, value, field=None):
    if isinstance(value, dict):
        yield from stream.open_object(field)
        for name in sorted(value):
            yield from _value(stream, value[name], name)
        yield from stream.close_object()
    elif isinstance(value, list):
        yield from stream.open_array(field)
        for member in value:
            yield from _value(stream, member)
        yield from stream.close_array()
    elif field is None:
        yield from stream.item(value)
    else:
        yield from stream.field(field, value)


def entity_tokens(stream, entity, value):
    """小型测试输入适配；不作为生产投影解释器。"""
    structure = encoding.projection_structure(entity)
    yield from stream.open_object(identity=int(value[structure.identity_field]))
    children = dict(structure.entity_fields)
    for name in sorted(value.keys() - children.keys()):
        yield from _value(stream, value[name], name)
    for name, child in structure.entity_fields:
        if name in value:
            yield from stream.open_array(name)
            for member in sorted(value[name], key=lambda item: int(item[encoding.projection_structure(child).identity_field])):
                yield from entity_tokens(stream, child, member)
            yield from stream.close_array()
    yield from stream.close_object()


def test_finite_values_nested_rows_and_entity_layout_match_old_encoder(fragments):
    ordinary = {'outputs': [{'output_id': '10'}, {'output_id': '2'}],
                'empty': [], 'null': None, 'object': {}, 'values': [
                    9007199254740993, Decimal('1.0000000000000001'), Decimal('0.00000012'),
                    Decimal('0.008'), Decimal('-0'), '中文😀/"\\\b\f\n\r\t\x00\x1f'],
                'nested': [[], [1, {'z': [], 'a': {}}]],
                'items': [{'withdrawals': [{'z': 2, 'a': 1}], 'target': {'type': 'obtain'}},
                          {'withdrawals': [], 'target': {'type': 'record'}}]}
    camera = {'action_instance_id': '2', 'result': ordinary, 'outputs': [
        {'output_id': '10', 'checksum': {'status': 'unknown'}}, {'output_id': '2'}],
              'deliveries': [{'delivery_id': '1'}]}
    fragments[('plan', 1)] = {'plan_instance_id': '1', 'actions': [camera]}
    fragments[('diagnostic', 2)] = {'diagnostic_id': '2', 'errors': []}
    document = ReportDocument('7', 0, 5, (('plan', 1, {}),), (('diagnostic', 2, {}),))
    with localcontext() as context:
        context.prec = 1
        expected = encode_report(document, {})
        stream = ReportStream(ReportDocument('7', 0, 5))
        tokens = list(stream.open())
        tokens.extend(stream.open_array('plans'))
        tokens.extend(entity_tokens(stream, 'plan', fragments[('plan', 1)]))
        tokens.extend(stream.close_array())
        tokens.extend(stream.open_array('plan_file_diagnostics'))
        tokens.extend(entity_tokens(stream, 'diagnostic', fragments[('diagnostic', 2)]))
        tokens.extend(stream.close_array())
        tokens.extend(stream.finish())
    assert b''.join(tokens) == expected
    assert b'"outputs":[{"output_id":"2"},{"checksum"' in expected
    assert b'"output_id":"10"}],"deliveries"' in expected
    assert b'9007199254740993' in expected and b'1.2e-7' in expected


def _script(stream, operations):
    for name, args in operations:
        list(getattr(stream, name)(*args))


@pytest.mark.parametrize('operations', [
    [('field', ('x', 1))], [('finish', ())], [('section', ('plans', {}))],
    [('open', ()), ('open', ())], [('open', ()), ('close_object', ())],
    [('open', ()), ('close_array', ())], [('open', ()), ('item', (1,))],
    [('open', ()), ('open_array', ('unknown',))],
    [('open', ()), ('open_array', ('plans',)), ('finish', ())],
    [('open', ()), ('open_array', ('plans',)), ('close_object', ())],
    [('open', ()), ('open_array', ('plans',)), ('field', ('x', 1))],
    [('open', ()), ('open_array', ('plans',)), ('item', ({'plan_instance_id': '1'},))],
    [('open', ()), ('open_array', ('plans',)), ('open_array', ())],
    [('open', ()), ('open_array', ('plans',)), ('close_array', ()), ('open_array', ('plans',))],
    [('open', ()), ('open_array', ('plan_file_diagnostics',)), ('close_array', ()), ('open_array', ('plans',))],
])
def test_document_structure_errors_are_terminal(operations):
    stream = ReportStream(ReportDocument('7', 0, 5))
    with pytest.raises(ValueError):
        _script(stream, operations)
    with pytest.raises(ValueError):
        list(stream.finish())


def _plan_stream():
    stream = ReportStream(ReportDocument('7', 0, 5))
    list(stream.open())
    list(stream.open_array('plans'))
    list(stream.open_object(identity=2))
    return stream


@pytest.mark.parametrize('operations', [
    [('finish', ())], [('close_array', ())], [('close_object', ())],
    [('field', ('plan_instance_id', '1'))], [('field', ('plan_instance_id', 2))],
    [('field', ('plan_instance_id', '02'))],
    [('field', ('b', 1)), ('field', ('a', 2))],
    [('field', ('b', 1)), ('field', ('b', 2))],
    [('field', ('actions', []))], [('open_object', ('actions',))],
    [('open_array', ('actions',)), ('close_array', ()), ('field', ('z', 1))],
    [('open_array', ('actions',)), ('close_array', ()), ('open_array', ('actions',))],
])
def test_entity_fields_require_identity_and_strict_layout(operations):
    stream = _plan_stream()
    with pytest.raises(ValueError):
        _script(stream, operations)
    with pytest.raises(ValueError):
        list(stream.finish())


@pytest.mark.parametrize('identity', [2, 1, True, 0, -1, 1.0, '3', 2**63])
def test_entity_identity_must_advance_as_exact_integer(identity):
    stream = _plan_stream()
    list(stream.field('plan_instance_id', '2'))
    list(stream.close_object())
    with pytest.raises(ValueError):
        list(stream.open_object(identity=identity))


@pytest.mark.parametrize('name,args', [('open', ()), ('finish', ()), ('field', ('x', 1)),
    ('item', (1,)), ('open_object', ()), ('open_array', ('plans',)),
    ('close_object', ()), ('close_array', ()), ('section', ('plans', {}))])
def test_all_emissions_reject_calls_after_finish(name, args):
    stream = ReportStream(ReportDocument('7', 0, 5))
    list(stream.open())
    list(stream.finish())
    with pytest.raises(ValueError):
        list(getattr(stream, name)(*args))


@pytest.mark.parametrize('first', ['section', 'nodes'])
def test_legacy_section_and_layered_supply_cannot_mix(first, fragments):
    stream = ReportStream(ReportDocument('7', 0, 5))
    list(stream.open())
    if first == 'section':
        list(stream.section('plans', fragments[('plan', 1)]))
        operation = stream.open_array('plan_file_diagnostics')
    else:
        list(stream.open_array('plans'))
        list(stream.close_array())
        operation = stream.section('plans', fragments[('plan', 1)])
    with pytest.raises(ValueError):
        list(operation)


@pytest.mark.parametrize('following', ['2', '1'])
def test_legacy_sections_reject_repeated_or_regressing_root_id(following):
    stream = ReportStream(ReportDocument('7', 0, 5))
    list(stream.open())
    list(stream.section('plans', {'plan_instance_id': '2'}))
    with pytest.raises(ValueError):
        list(stream.section('plans', {'plan_instance_id': following}))


def test_abandoned_field_emission_does_not_allow_successful_finish():
    stream = _plan_stream()
    pending = stream.field('plan_instance_id', '2')
    next(pending)
    pending.close()
    with pytest.raises(ValueError):
        list(stream.finish())


@pytest.mark.parametrize('value', [1.5, Decimal('NaN'), chr(0xD800), (1, 2)])
@pytest.mark.parametrize('slot', ['field', 'item'])
def test_invalid_finite_value_terminates_layered_document(value, slot):
    stream = _plan_stream()
    if slot == 'item':
        list(stream.open_array('ordinary'))
        operation = stream.item(value)
    else:
        operation = stream.field('ordinary', value)
    with pytest.raises(ValueError):
        list(operation)
    with pytest.raises(ValueError):
        list(stream.finish())


def test_layered_entity_ids_above_safe_integer_keep_exact_order():
    stream = ReportStream(ReportDocument('7', 0, 5))
    tokens = list(stream.open()) + list(stream.open_array('plans'))
    for identity in (9007199254740992, 9007199254740993):
        tokens.extend(stream.open_object(identity=identity))
        tokens.extend(stream.field('plan_instance_id', str(identity)))
        tokens.extend(stream.close_object())
    tokens.extend(stream.close_array())
    tokens.extend(stream.finish())
    assert b''.join(tokens) == b'{"from_wm":0,"report_id":"7","to_wm":5,"plans":[{"plan_instance_id":"9007199254740992"},{"plan_instance_id":"9007199254740993"}]}\n'


@pytest.mark.parametrize('batch', [1, 2, 128])
@pytest.mark.parametrize('capacity', [1, 7, 64])
def test_paginated_members_are_released_and_byte_buffer_is_bounded(batch, capacity, fragments):
    class Member(dict):
        pass
    seen = []
    stream = ReportStream(ReportDocument('7', 0, 5))
    counts = []
    def tokens():
        yield from stream.open()
        yield from stream.open_array('plans')
        yield from stream.open_object(identity=1)
        yield from stream.field('plan_instance_id', '1')
        yield from stream.open_array('actions')
        for start in range(1, 258, batch):
            for identity in range(start, min(start + batch, 258)):
                yield from stream.open_object(identity=identity)
                yield from stream.field('action_instance_id', str(identity))
                member = Member({'text': '中文' * 30, 'values': [identity, Decimal('0.008')]})
                seen.append(weakref.ref(member))
                yield from stream.field('params', member)
                del member
                assert seen[-1]() is None
                counts.append(len(stream._frames))
                yield from stream.close_object()
        yield from stream.close_array()
        yield from stream.close_object()
        yield from stream.close_array()
        yield from stream.finish()
    chunks = []
    buffered = chunk_bytes(tokens(), buffer_size=capacity)
    maximum = 0
    for chunk in buffered:
        maximum = max(maximum, len(buffered.gi_frame.f_locals['pending']))
        chunks.append(chunk)
    assert all(0 < len(chunk) <= capacity for chunk in chunks)
    assert maximum <= capacity
    assert max(counts) == 5
    gc.collect()
    assert all(reference() is None for reference in seen)
    combined = b''.join(chunks)
    assert combined.count(b'"action_instance_id"') == 257
    assert combined.endswith(b'}\n')
    fragments[('plan', 1)] = {'plan_instance_id': '1', 'actions': [
        {'action_instance_id': str(identity), 'params': {
            'text': '中文' * 30, 'values': [identity, Decimal('0.008')]}}
        for identity in range(1, 258)]}
    assert combined == encode_report(ReportDocument('7', 0, 5, (('plan', 1, {}),)), {})
    print({'batch': batch, 'capacity': capacity, 'maximum_pending': maximum,
           'maximum_frames': max(counts), 'members': len(seen),
           'retained_members': sum(reference() is not None for reference in seen)})

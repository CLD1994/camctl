"""字段完成门禁的纯内存契约；Schema 和引用不触及真实资源。"""
from dataclasses import replace
from decimal import Decimal
import pytest
from referencing import Registry
from referencing.jsonschema import DRAFT202012
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.schemas import create_validator, validation_errors
from camctl.reporting.stream_validation import (
    CompletionRequirement, DeferredField, FieldKind, FieldState, NodeBinding,
    ProjectionDescriptor, ReadEnd, StreamValidationError, derive_stream_plan,
)
URI = 'https://camctl.invalid/test.schema.json'


def registry(schema):
    return Registry().with_resource(URI, DRAFT202012.create_resource(schema))


def binding(plan, **changes):
    value = NodeBinding('db-1', 'action', 10, HistoryBoundary(2, 4), ('plans', 1, 'actions', 10), plan.schema_uri, plan.projection.pointer)
    return replace(value, **changes)


def plan_for(schema, fields=(DeferredField(('items',)),)):
    return derive_stream_plan(URI, ProjectionDescriptor('/test', fields), registry=registry(schema))


def end(gate, path=('items',)):
    gate.exhaust_array(path, ReadEnd(gate.binding, path, gate.count(path), gate.last_position(path)))


def test_required_empty_array_is_real_and_exhausted():
    plan = plan_for({'type': 'object', 'properties': {'items': {'type': 'array', 'items': {'type': 'integer'}}}, 'required': ['items']})
    gate = plan.begin(binding(plan), {})
    with pytest.raises(StreamValidationError):
        gate.close_node()
    assert gate.state(('items',)) is FieldState.UNDECIDED
    gate.open_array(('items',))
    with pytest.raises(StreamValidationError):
        gate.close_node()
    end(gate)
    receipt = gate.close_node()
    assert receipt.binding == gate.binding
    assert gate.state(('items',)) is FieldState.CLOSED


@pytest.mark.parametrize('count,expected', [(0, True), (1, True), (2, False)])
def test_actual_count_and_items_are_checked(count, expected):
    plan = plan_for({'type': 'object', 'properties': {'items': {'type': 'array', 'maxItems': 1, 'items': {'type': 'integer'}}}})
    gate = plan.begin(binding(plan), {})
    gate.open_array(('items',))
    for i in range(count):
        gate.accept_item(('items',), i, position=i)
    end(gate)
    if expected:
        gate.close_node()
    else:
        with pytest.raises(StreamValidationError):
            gate.close_node()


def test_invalid_item_cannot_close():
    plan = plan_for({'properties': {'items': {'type': 'array', 'items': {'type': 'integer'}}}})
    gate = plan.begin(binding(plan), {})
    gate.open_array(('items',))
    with pytest.raises(StreamValidationError):
        gate.accept_item(('items',), True, position=1)
        end(gate)
        gate.close_node()


@pytest.mark.parametrize('mutation', ['owner_id', 'boundary', 'occurrence', 'schema_uri', 'projection_pointer'])
def test_exhaustion_binding_and_actual_position_are_not_substitutable(mutation):
    plan = plan_for({'properties': {'items': {'type': 'array'}}})
    gate = plan.begin(binding(plan), {})
    gate.open_array(('items',))
    changes = dict(owner_id=99, boundary=HistoryBoundary(3, 9), occurrence=('other',), schema_uri=URI + '#/other', projection_pointer='/other')
    wrong = replace(gate.binding, **{mutation: changes[mutation]})
    with pytest.raises(StreamValidationError):
        gate.exhaust_array(('items',), ReadEnd(wrong, ('items',), 0, None))
    with pytest.raises(StreamValidationError):
        gate.close_node()


@pytest.mark.parametrize('count,position', [(1, None), (0, 4)])
def test_exhaustion_checks_count_and_position(count, position):
    plan = plan_for({'properties': {'items': {'type': 'array'}}})
    gate = plan.begin(binding(plan), {})
    gate.open_array(('items',))
    with pytest.raises(StreamValidationError):
        gate.exhaust_array(('items',), ReadEnd(gate.binding, ('items',), count, position))


def test_failed_gate_cannot_reopen_or_close():
    plan = plan_for({'properties': {'items': {'type': 'array'}}})
    gate = plan.begin(binding(plan), {})
    gate.open_array(('items',))
    gate.fail(RuntimeError('last page failed'))
    for operation in [lambda: gate.open_array(('items',)), lambda: gate.close_node(), lambda: end(gate)]:
        with pytest.raises(StreamValidationError):
            operation()


def test_required_omission_is_not_empty_array():
    plan = plan_for({'properties': {'items': {'type': 'array'}}, 'required': ['items']})
    gate = plan.begin(binding(plan), {})
    gate.omit(('items',))
    with pytest.raises(StreamValidationError):
        gate.close_node()


def test_empty_entity_omission_needs_authority_end():
    plan = plan_for({'properties': {'items': {'type': 'array'}}}, (DeferredField(('items',), omit_empty=True),))
    gate = plan.begin(binding(plan), {})
    gate.omit(('items',), authority_end=ReadEnd(gate.binding, ('items',), 0, None))
    assert gate.state(('items',)) is FieldState.OMITTED
    gate.close_node()


@pytest.mark.parametrize('a,b,ok', [(False, False, True), (False, True, True), (True, True, True), (True, False, False)])
def test_dependent_required_uses_real_presence(a, b, ok):
    schema = {'type': 'object', 'properties': {'a': {'type': 'string'}, 'b': {'type': 'string'}, 'items': {'type': 'array'}}, 'dependentRequired': {'a': ['b']}}
    fields = (DeferredField(('items',)), DeferredField(('a',), kind=FieldKind.VALUE), DeferredField(('b',), kind=FieldKind.VALUE))
    plan = plan_for(schema, fields)
    gate = plan.begin(binding(plan), {})
    for name, present in [('a', a), ('b', b)]:
        if present:
            gate.set_value((name,), 'x')
        else:
            gate.omit((name,))
    gate.open_array(('items',))
    end(gate)
    if ok:
        gate.close_node()
    else:
        with pytest.raises(StreamValidationError):
            gate.close_node()


def test_unknown_presence_blocks_even_vacuous_implication():
    plan = plan_for({'properties': {'a': {}, 'b': {}}, 'dependentRequired': {'a': ['b']}}, (DeferredField(('a',), kind=FieldKind.VALUE), DeferredField(('b',), kind=FieldKind.VALUE)))
    gate = plan.begin(binding(plan), {})
    gate.omit(('a',))
    with pytest.raises(StreamValidationError):
        gate.close_node()


@pytest.mark.parametrize('flag,count,ok', [('hide', 0, False), ('zero', 0, True), ('zero', 1, False), ('other', 1, True)])
def test_refs_siblings_boolean_conditions_and_counts(flag, count, ok):
    schema = {'$defs': {'base': {'type': 'object', 'properties': {'mode': {'type': 'string'}, 'items': {'type': 'array', 'items': {'type': 'integer'}}}}}, '$ref': '#/$defs/base', 'required': ['mode'], 'allOf': [{'if': {'properties': {'mode': {'const': 'hide'}}}, 'then': {'not': {'anyOf': [{'required': ['items']}]}}}, {'if': {'properties': {'mode': {'const': 'zero'}}}, 'then': {'properties': {'items': {'maxItems': 0}}}, 'else': {'properties': {'items': {'maxItems': 2}}}}]}
    plan = plan_for(schema)
    gate = plan.begin(binding(plan), {'mode': flag})
    gate.open_array(('items',))
    for i in range(count):
        gate.accept_item(('items',), i, position=i)
    end(gate)
    if ok:
        gate.close_node()
    else:
        with pytest.raises(StreamValidationError):
            gate.close_node()


@pytest.mark.parametrize('keyword,value', [('contains', {'type': 'integer'}), ('oneOf', [{}, {}]), ('dependentSchemas', {'mode': {}}), ('unevaluatedProperties', False)])
def test_unsupported_cross_stream_keyword_rejected_during_derivation(keyword, value):
    with pytest.raises(StreamValidationError, match=keyword):
        plan_for({'properties': {'items': {'type': 'array'}}, keyword: value})


def test_finite_contains_oneof_and_precision_delegate_original_validator():
    schema = {'properties': {'items': {'type': 'array'}, 'finite': {'type': 'array', 'contains': {'oneOf': [{'const': Decimal('1.0000000000000001')}, {'const': 'exact'}]}}}, 'required': ['finite']}
    plan = plan_for(schema)
    gate = plan.begin(binding(plan), {'finite': [Decimal('1.0000000000000001')]})
    gate.omit(('items',))
    gate.close_node()
    bad = plan.begin(binding(plan), {'finite': [Decimal('1.0')]})
    bad.omit(('items',))
    with pytest.raises(StreamValidationError):
        bad.close_node()


def test_missing_ref_and_cross_stream_cycles_reject_at_initialization():
    for schema in ({'$ref': 'missing.json'}, {'$ref': '#', 'properties': {'items': {'type': 'array'}}}):
        with pytest.raises(StreamValidationError):
            plan_for(schema)


def test_additional_properties_and_object_property_counts_are_preserved():
    schema = {'type': 'object', 'properties': {'items': {'type': 'array'}}, 'additionalProperties': False, 'maxProperties': 1}
    plan = plan_for(schema)
    gate = plan.begin(binding(plan), {'extra': 1})
    gate.open_array(('items',))
    end(gate)
    with pytest.raises(StreamValidationError):
        gate.close_node()


def test_deferred_values_cannot_be_smuggled_as_finite_arrays():
    plan = plan_for({'properties': {'items': {'type': 'array'}}})
    with pytest.raises(StreamValidationError):
        plan.begin(binding(plan), {'items': []})


def test_item_receipt_binds_parent_and_does_not_retain_items():
    child = ProjectionDescriptor('/item', (DeferredField(('inner',)),))
    plan = plan_for({'properties': {'items': {'type': 'array', 'items': {'type': 'object', 'properties': {'inner': {'type': 'array', 'items': {'type': 'integer'}}}, 'required': ['inner']}}}}, (DeferredField(('items',), item=child),))
    gate = plan.begin(binding(plan), {})
    gate.open_array(('items',))
    child_plan = gate.item_plan(('items',))
    child_gate = child_plan.begin(gate.child_binding(('items',), 1), {})
    child_gate.open_array(('inner',))
    end(child_gate, ('inner',))
    receipt = child_gate.close_node()
    gate.accept_item(('items',), receipt, position=1)
    with pytest.raises(StreamValidationError):
        gate.accept_item(('items',), receipt, position=2)


def test_completion_contract_requires_typed_bound_one_shot_proof():
    spec = CompletionRequirement('segment', ())
    projection = ProjectionDescriptor('/test', (DeferredField(('items',)),), (spec,))
    plan = derive_stream_plan(URI, projection, registry=registry({'properties': {'items': {'type': 'array'}}}))
    gate = plan.begin(binding(plan), {})
    gate.open_array(('items',))
    end(gate)
    with pytest.raises(StreamValidationError):
        gate.close_node()
    with pytest.raises(StreamValidationError):
        gate.supply_completion('segment', True)


def test_completion_port_consumes_exact_request_only_after_schema_success():
    calls = []

    class Proof:

        def consume(self, request):
            assert request.plan is plan
            assert request.binding == gate.binding
            assert request.requirement.name == 'segment'
            assert request.counts == ((('items',), 0),)
            calls.append(request)
    projection = ProjectionDescriptor('/test', (DeferredField(('items',)),), (CompletionRequirement('segment', ()),))
    plan = derive_stream_plan(URI, projection, registry=registry({'properties': {'items': {'type': 'array'}}}))
    gate = plan.begin(binding(plan), {})
    gate.open_array(('items',))
    end(gate)
    gate.supply_completion('segment', Proof())
    gate.close_node()
    assert len(calls) == 1
    with pytest.raises(StreamValidationError):
        gate.close_node()


def _object_world():
    shared = registry({'$defs': {'details': {'type': 'object', 'properties': {'items': {'type': 'array', 'items': {'type': 'integer'}}}, 'required': ['items']}, 'error': {'type': 'object', 'properties': {'details': {'$ref': '#/$defs/details'}}, 'required': ['details']}, 'parent': {'type': 'object', 'properties': {'error': {'$ref': '#/$defs/error'}}, 'required': ['error']}}})
    details = derive_stream_plan(URI + '#/$defs/details', ProjectionDescriptor('/details', (DeferredField(('items',)),)), registry=shared)
    error = derive_stream_plan(URI + '#/$defs/error', ProjectionDescriptor('/error', (DeferredField(('details',), kind=FieldKind.OBJECT, node=details),)), registry=shared)
    parent = derive_stream_plan(URI + '#/$defs/parent', ProjectionDescriptor('/parent', (DeferredField(('error',), kind=FieldKind.OBJECT, node=error),)), registry=shared)
    parent_gate = parent.begin(binding(parent), {})
    error_gate = error.begin(parent_gate.object_binding(('error',)), {})
    details_gate = details.begin(error_gate.object_binding(('details',)), {})
    return (parent_gate, error_gate, details_gate)


def test_real_nested_objects_require_closed_child_receipts():
    parent, error, details = _object_world()
    with pytest.raises(StreamValidationError):
        error.accept_object(('details',), details)
    with pytest.raises(StreamValidationError):
        parent.close_node()
    details.open_array(('items',))
    with pytest.raises(StreamValidationError):
        details.close_node()
    details.accept_item(('items',), 7, position=1)
    end(details)
    error.accept_object(('details',), details.close_node())
    parent.accept_object(('error',), error.close_node())
    parent.close_node()


@pytest.mark.parametrize('changed', ['boundary', 'occurrence', 'schema_uri', 'projection_pointer', 'owner_id'])
def test_object_receipt_checks_parent_field_and_complete_binding(changed):
    parent, error, details = _object_world()
    alternatives = dict(boundary=HistoryBoundary(3, 5), occurrence=('other_field',), schema_uri=URI, projection_pointer='/other', owner_id=99)
    wrong = replace(details.binding, **{changed: alternatives[changed]})
    if changed in ('schema_uri', 'projection_pointer'):
        with pytest.raises(StreamValidationError):
            details.plan.begin(wrong, {})
        with pytest.raises(StreamValidationError):
            error.close_node()
        return
    details = details.plan.begin(wrong, {})
    details.open_array(('items',))
    end(details)
    receipt = details.close_node()
    with pytest.raises(StreamValidationError):
        error.accept_object(('details',), receipt)
    with pytest.raises(StreamValidationError):
        error.close_node()
    with pytest.raises(StreamValidationError):
        parent.close_node()


def test_failed_object_child_or_reused_receipt_cannot_close_parent():
    parent, error, details = _object_world()
    details.open_array(('items',))
    details.fail(RuntimeError('last page'))
    with pytest.raises(StreamValidationError):
        details.close_node()
    with pytest.raises(StreamValidationError):
        error.close_node()
    parent, error, details = _object_world()
    details.open_array(('items',))
    end(details)
    receipt = details.close_node()
    error.accept_object(('details',), receipt)
    other = error.plan.begin(error.binding, {})
    with pytest.raises(StreamValidationError):
        other.accept_object(('details',), receipt)
    with pytest.raises(StreamValidationError):
        other.close_node()


def test_missing_reference_in_finite_leaf_is_rejected_at_initialization():
    with pytest.raises(StreamValidationError):
        plan_for({'properties': {'items': {'type': 'array'}, 'finite': {'$ref': 'missing.json'}}})


def test_completion_contract_cannot_return_success_boolean():

    class BooleanProof:

        def consume(self, request):
            return True
    projection = ProjectionDescriptor('/test', (DeferredField(('items',)),), (CompletionRequirement('segment', ()),))
    plan = derive_stream_plan(URI, projection, registry=registry({'properties': {'items': {'type': 'array'}}}))
    gate = plan.begin(binding(plan), {})
    gate.open_array(('items',))
    end(gate)
    gate.supply_completion('segment', BooleanProof())
    with pytest.raises(StreamValidationError):
        gate.close_node()


def test_local_reference_respects_nested_resource_id_and_sibling_count():
    schema = {'properties': {'group': {'$id': 'nested/group.json', 'type': 'object', 'properties': {'items': {'$ref': 'array.json', 'maxItems': 1}}}}}
    shared = registry(schema).with_resource('https://camctl.invalid/nested/array.json', DRAFT202012.create_resource({'type': 'array', 'items': {'type': 'integer'}}))
    plan = derive_stream_plan(URI, ProjectionDescriptor('/test', (DeferredField(('group', 'items')),)), registry=shared)
    good = plan.begin(binding(plan), {'group': {}})
    good.open_array(('group', 'items'))
    good.accept_item(('group', 'items'), 3, position=1)
    end(good, ('group', 'items'))
    good.close_node()
    bad = plan.begin(binding(plan), {'group': {}})
    bad.open_array(('group', 'items'))
    bad.accept_item(('group', 'items'), '3', position=1)
    end(bad, ('group', 'items'))
    with pytest.raises(StreamValidationError):
        bad.close_node()


def test_child_receipt_cannot_bypass_expected_projection_completion():
    child = ProjectionDescriptor('/item', (), (CompletionRequirement('segment', ()),))
    plan = plan_for({'properties': {'items': {'type': 'array', 'items': {'type': 'object'}}}}, (DeferredField(('items',), item=child),))
    gate = plan.begin(binding(plan), {})
    gate.open_array(('items',))
    item_plan = gate.item_plan(('items',))
    incomplete_plan = derive_stream_plan(item_plan.schema_uri, ProjectionDescriptor('/item'), registry=plan._compiler.registry)
    receipt = incomplete_plan.begin(gate.child_binding(('items',), 1), {}).close_node()
    with pytest.raises(StreamValidationError):
        gate.accept_item(('items',), receipt, position=1)


def test_consumed_members_do_not_stay_in_gate():
    import gc
    import weakref

    class Member(dict):
        pass
    plan = plan_for({'properties': {'items': {'type': 'array', 'items': {'type': 'object', 'properties': {'id': {'type': 'integer'}}, 'required': ['id']}}}})
    gate = plan.begin(binding(plan), {})
    gate.open_array(('items',))
    references = []
    for position in range(512):
        item = Member(id=position)
        references.append(weakref.ref(item))
        gate.accept_item(('items',), item, position=position)
    del item
    gc.collect()
    assert all((reference() is None for reference in references))
    assert gate.count(('items',)) == 512
    end(gate)
    gate.close_node()


@pytest.mark.parametrize('items', [[], [1], [1, 2]])
def test_min_and_max_count_use_complete_consumption(items):
    schema = {'properties': {'items': {'type': 'array', 'items': {'type': 'integer'}, 'minItems': 1, 'maxItems': 1}}}
    original = create_validator({'$schema': 'https://json-schema.org/draft/2020-12/schema', **schema}, registry=registry(schema))
    expected = len(items) == 1
    assert (not validation_errors(original, {'items': items})) is expected
    plan = plan_for(schema)
    gate = plan.begin(binding(plan), {})
    gate.open_array(('items',))
    for position, item in enumerate(items):
        gate.accept_item(('items',), item, position=position)
    end(gate)
    if expected:
        gate.close_node()
    else:
        with pytest.raises(StreamValidationError):
            gate.close_node()


def test_multiple_dependent_triggers_are_conjoined():
    schema = {'properties': {'items': {'type': 'array'}, 'a': {}, 'b': {}, 'c': {}}, 'dependentRequired': {'a': ['b'], 'b': ['c']}}
    plan = plan_for(schema)
    gate = plan.begin(binding(plan), {'a': 1, 'b': 2})
    gate.omit(('items',))
    with pytest.raises(StreamValidationError):
        gate.close_node()


def test_unique_items_requires_registered_order_and_checks_every_item():
    schema = {'properties': {'items': {'type': 'array', 'items': {'type': 'integer'}, 'uniqueItems': True}}}
    with pytest.raises(StreamValidationError):
        plan_for(schema)
    plan = plan_for(schema, (DeferredField(('items',), identity=lambda value: value),))
    gate = plan.begin(binding(plan), {})
    gate.open_array(('items',))
    gate.accept_item(('items',), 5, position=1)
    with pytest.raises(StreamValidationError):
        gate.accept_item(('items',), 5, position=2)
    with pytest.raises(StreamValidationError):
        gate.close_node()


@pytest.mark.parametrize('keyword,body', [('anyOf', [{'items': {'type': 'integer'}}, {'items': {'type': 'string'}}]), ('not', {'items': {'type': 'integer'}})])
@pytest.mark.parametrize('members', [[], [1], ['x'], [1, 'x']])
def test_item_branch_verdicts_preserve_original_boolean_acceptance(keyword, body, members):
    schema = {'properties': {'items': {'type': 'array', keyword: body}}}
    original = create_validator({'$schema': 'https://json-schema.org/draft/2020-12/schema', **schema}, registry=registry(schema))
    expected = not validation_errors(original, {'items': members})
    plan = plan_for(schema)
    gate = plan.begin(binding(plan), {})
    gate.open_array(('items',))
    for position, item in enumerate(members):
        gate.accept_item(('items',), item, position=position)
    end(gate)
    if expected:
        gate.close_node()
    else:
        with pytest.raises(StreamValidationError):
            gate.close_node()


def test_derived_evidence_keeps_authoritative_uri_and_value_path():
    plan = plan_for({'properties': {'label': {'type': 'string'}, 'items': {'type': 'array', 'maxItems': 0}}, 'required': ['items']})
    assert any((check.schema_uri == URI + '#/properties/label' and check.value_path == ('label',) for check in plan.finite_checks))
    assert any((rule.schema_uri == URI + '#/required' and rule.value_path == () and (rule.keyword == 'required') for rule in plan.parent_predicates))
    field = plan.field_obligations[0]
    assert field.path == ('items',)
    assert URI + '#/properties/items' in field.schema_uris
    assert (URI + '#/properties/items/maxItems', 'maxItems') in field.array_rules



@pytest.mark.parametrize('position', [True, [], {'id': 1}, None])
def test_item_position_must_be_a_stable_finite_identity(position):
    plan = plan_for({'properties': {'items': {'type': 'array', 'items': {'type': 'integer'}}}})
    gate = plan.begin(binding(plan), {})
    gate.open_array(('items',))
    with pytest.raises(StreamValidationError):
        gate.accept_item(('items',), 1, position=position)
    with pytest.raises(StreamValidationError):
        gate.close_node()



@pytest.mark.parametrize('finite', [False, True])
def test_other_schema_dialect_is_rejected_before_consumption(finite):
    incompatible = {'$schema': 'http://json-schema.org/draft-07/schema#', 'type': 'object'}
    schema = {'properties': {'items': {'type': 'array'}, 'finite': incompatible}} if finite else dict(incompatible, properties={'items': {'type': 'array'}})
    with pytest.raises(StreamValidationError):
        plan_for(schema)



def test_authority_unique_proof_keeps_saved_array_order_and_is_mandatory():
    requirement = CompletionRequirement('same_ref_members', (), (('items',),))
    schema = {'properties': {'items': {'type': 'array', 'items': {'type': 'integer'}, 'uniqueItems': True}}}
    projection = ProjectionDescriptor('/test', (DeferredField(('items',), unique_by=requirement.name),), (requirement,))
    plan = derive_stream_plan(URI, projection, registry=registry(schema))
    gate = plan.begin(binding(plan), {})
    gate.open_array(('items',))
    for position, item in enumerate([50, 2, 30]):
        gate.accept_item(('items',), item, position=position)
    end(gate)
    with pytest.raises(StreamValidationError):
        gate.close_node()
    class AuthorityProof:
        def consume(self, request):
            assert request.plan is plan
            assert request.binding == gate.binding
            assert request.requirement == requirement
            assert request.counts == ((('items',), 3),)
    gate.supply_completion(requirement.name, AuthorityProof())
    gate.close_node()



def test_unique_proof_requirement_must_cover_actual_field():
    requirement = CompletionRequirement('other', ('elsewhere',))
    schema = {'properties': {'items': {'type': 'array', 'uniqueItems': True}}}
    projection = ProjectionDescriptor('/test', (DeferredField(('items',), unique_by=requirement.name),), (requirement,))
    with pytest.raises(StreamValidationError):
        derive_stream_plan(URI, projection, registry=registry(schema))



def test_unrelated_completion_cannot_prove_unique_items():
    requirement = CompletionRequirement('unrelated_business_check', ())
    projection = ProjectionDescriptor('/test', (DeferredField(('items',), unique_by=requirement.name),), (requirement,))
    with pytest.raises(StreamValidationError):
        derive_stream_plan(URI, projection, registry=registry({'properties': {'items': {'type': 'array', 'uniqueItems': True}}}))



def test_gate_and_receipt_bindings_cannot_be_reassigned():
    plan = plan_for({'properties': {'items': {'type': 'array'}}})
    gate = plan.begin(binding(plan), {})
    gate.omit(('items',))
    receipt = gate.close_node()
    for token in (gate, receipt):
        for name in ('binding', 'plan'):
            with pytest.raises(AttributeError):
                setattr(token, name, None)


@pytest.mark.parametrize('wrong', ['boundary', 'field', 'schema', 'plan'])
def test_completion_consumer_rejects_wrong_bound_request(wrong):
    from camctl.reporting.stream_validation import CompletionRequest
    requirement = CompletionRequirement('same_ref_members', (), (('items',),))
    schema = {'properties': {'items': {'type': 'array', 'uniqueItems': True}}}
    projection = ProjectionDescriptor('/test', (DeferredField(('items',), unique_by=requirement.name),), (requirement,))
    shared = registry(schema)
    plan = derive_stream_plan(URI, projection, registry=shared)
    gate = plan.begin(binding(plan), {})
    gate.open_array(('items',))
    end(gate)
    expected = CompletionRequest(gate.binding, requirement, ((('items',), 0),), plan)
    if wrong == 'boundary':
        expected = replace(expected, binding=replace(expected.binding, boundary=HistoryBoundary(3, 5)))
    elif wrong == 'field':
        expected = replace(expected, requirement=replace(requirement, unique_fields=(('other',),)))
    elif wrong == 'schema':
        expected = replace(expected, binding=replace(expected.binding, schema_uri=URI + '#/other'))
    else:
        expected = replace(expected, plan=derive_stream_plan(URI, projection, registry=shared))
    class BoundProof:
        def consume(self, request):
            if request.plan is not expected.plan or request != expected:
                raise StreamValidationError('业务端口的原计划/字段/历史绑定不符')
    gate.supply_completion(requirement.name, BoundProof())
    with pytest.raises(StreamValidationError):
        gate.close_node()
    with pytest.raises(StreamValidationError):
        gate.close_node()

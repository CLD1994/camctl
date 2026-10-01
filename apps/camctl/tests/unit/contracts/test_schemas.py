"""精确 Schema 校验的单元测试，包资源通过字节读取边界隔离。"""

from __future__ import annotations

import json
from decimal import Decimal, localcontext

import pytest

from camctl.acceptance.schema import BodySchemaError, RuleError, validate_precise
from camctl.contracts import schemas
from camctl.contracts.json_values import parse_exact_json

REPORT_SCHEMA = "protocol/status-report.schema.json"
_SCHEMA_HEADER = '"$schema":"https://json-schema.org/draft/2020-12/schema",'


@pytest.fixture()
def resources(monkeypatch):
    documents = {
        name: b'{"$schema":"https://json-schema.org/draft/2020-12/schema"}'
        for name in schemas._SCHEMA_RESOURCES
    }

    def read(name: str) -> bytes:
        return documents[name]

    schemas._registry.cache_clear()
    schemas._validator.cache_clear()
    monkeypatch.setattr(schemas, "resource_bytes", read)
    yield documents
    schemas._registry.cache_clear()
    schemas._validator.cache_clear()


def _validate(resources, schema_text: str, document, entry: str) -> None:
    schema_text = '{' + _SCHEMA_HEADER + schema_text[1:]
    resources[REPORT_SCHEMA] = schema_text.encode()
    if entry == "public":
        schemas.validate_document(REPORT_SCHEMA, document)
    else:
        validate_precise(parse_exact_json(schema_text), document)


@pytest.mark.parametrize("entry", ["public", "acceptance"])
@pytest.mark.parametrize("number", [Decimal("1.0"), Decimal("1e0")])
def test_integral_decimal_satisfies_integer(resources, entry, number) -> None:
    _validate(resources, '{"type":"integer"}', number, entry)


@pytest.mark.parametrize("entry", ["public", "acceptance"])
@pytest.mark.parametrize("number", [True, "1", 1.0, Decimal("1.0000000000000001")])
def test_noninteger_inputs_are_rejected(resources, entry, number) -> None:
    expected = schemas.SchemaValidationError if entry == "public" else BodySchemaError
    with pytest.raises(expected):
        _validate(resources, '{"type":"integer"}', number, entry)


@pytest.mark.parametrize("entry", ["public", "acceptance"])
@pytest.mark.parametrize(
    "schema_text, number",
    [
        ('{"type":"number","minimum":0.10000000000000001}', Decimal("0.100000000000000007")),
        ('{"type":"number","maximum":0.1}', Decimal("0.100000000000000001")),
    ],
)
def test_decimal_schema_boundaries_remain_exact(resources, entry, schema_text, number) -> None:
    expected = schemas.SchemaValidationError if entry == "public" else BodySchemaError
    with pytest.raises(expected):
        _validate(resources, schema_text, number, entry)


@pytest.mark.parametrize("entry", ["public", "acceptance"])
def test_multiple_of_is_independent_of_context(resources, entry) -> None:
    with localcontext() as context:
        context.prec = 3
        _validate(
            resources, '{"type":"number","multipleOf":0.1}',
            Decimal("123456789012345678901234567890.3"), entry,
        )


@pytest.mark.parametrize("entry", ["public", "acceptance"])
def test_nonmultiple_is_rejected(resources, entry) -> None:
    expected = schemas.SchemaValidationError if entry == "public" else BodySchemaError
    with pytest.raises(expected):
        _validate(resources, '{"type":"number","multipleOf":0.1}', Decimal("0.35"), entry)


@pytest.mark.parametrize("entry", ["public", "acceptance"])
@pytest.mark.parametrize("keyword", ["const", "enum"])
def test_numeric_equality_excludes_boolean(resources, entry, keyword) -> None:
    schema_text = '{"const":1.0}' if keyword == "const" else '{"enum":[1.0]}'
    _validate(resources, schema_text, 1, entry)
    expected = schemas.SchemaValidationError if entry == "public" else BodySchemaError
    with pytest.raises(expected):
        _validate(resources, schema_text, True, entry)


@pytest.mark.parametrize("entry", ["public", "acceptance"])
def test_equal_numeric_items_are_duplicates(resources, entry) -> None:
    expected = schemas.SchemaValidationError if entry == "public" else BodySchemaError
    with pytest.raises(expected):
        _validate(resources, '{"type":"array","uniqueItems":true}', [1, Decimal("1.0")], entry)


@pytest.mark.parametrize("entry", ["public", "acceptance"])
def test_boolean_and_number_are_distinct_items(resources, entry) -> None:
    _validate(resources, '{"type":"array","uniqueItems":true}', [True, Decimal("1.0")], entry)


@pytest.mark.parametrize("entry", ["public", "acceptance"])
def test_combined_numeric_rules_use_exact_integer(resources, entry) -> None:
    _validate(resources, '{"allOf":[{"type":"integer"},{"minimum":1},{"maximum":10}]}',
              Decimal("1e0"), entry)


@pytest.mark.parametrize("entry", ["public", "acceptance"])
def test_invalid_schema_is_a_rule_error(resources, entry) -> None:
    expected = schemas.SchemaRuleError if entry == "public" else RuleError
    with pytest.raises(expected) as caught:
        _validate(resources, '{"type":"not-a-json-type"}', 1, entry)
    assert not isinstance(caught.value, (schemas.SchemaValidationError, BodySchemaError))


@pytest.mark.parametrize("entry", ["public", "acceptance"])
def test_missing_local_reference_is_a_rule_error(resources, entry) -> None:
    expected = schemas.SchemaRuleError if entry == "public" else RuleError
    with pytest.raises(expected) as caught:
        _validate(resources, '{"$ref":"missing.schema.json"}', 1, entry)
    assert not isinstance(caught.value, (schemas.SchemaValidationError, BodySchemaError))


def test_report_watermarks_accept_equivalent_decimal_notation(resources) -> None:
    resources[REPORT_SCHEMA] = (
        '{' + _SCHEMA_HEADER + '"type":"object","properties":{'
        '"from_wm":{"type":"integer","minimum":0},'
        '"to_wm":{"type":"integer","minimum":0}}}'
    ).encode()
    schemas.validate_document(REPORT_SCHEMA, parse_exact_json('{"from_wm":1.0,"to_wm":1e0}'))


@pytest.mark.parametrize("entry", ["public", "acceptance"])
def test_whole_local_schema_reference_keeps_exact_integer(resources, entry) -> None:
    resources["protocol/plan.schema.json"] = (
        '{' + _SCHEMA_HEADER + '"type":"integer"}'
    ).encode()
    _validate(resources, '{"$ref":"plan.schema.json"}', Decimal("1.0"), entry)


@pytest.mark.parametrize("entry", ["public", "acceptance"])
def test_integral_decimal_schema_constraint_is_valid(resources, entry) -> None:
    _validate(resources, '{"type":"string","minLength":1.0}', "a", entry)


def _validate_rule(resources, schema, document, entry: str) -> None:
    resources[REPORT_SCHEMA] = json.dumps(schema).encode()
    if entry == "public":
        schemas.validate_document(REPORT_SCHEMA, document)
    else:
        validate_precise(schema, document)


@pytest.mark.parametrize("entry", ["public", "acceptance"])
@pytest.mark.parametrize("version", [None, "http://json-schema.org/draft-07/schema#", "urn:unknown:draft"])
def test_root_requires_supported_schema_version(resources, entry, version) -> None:
    schema = {"type": "integer"}
    if version is not None:
        schema["$schema"] = version
    expected = schemas.SchemaRuleError if entry == "public" else RuleError
    with pytest.raises(expected):
        _validate_rule(resources, schema, 1, entry)


@pytest.mark.parametrize("entry", ["public", "acceptance"])
@pytest.mark.parametrize("version", [None, "http://json-schema.org/draft-07/schema#", "urn:unknown:draft"])
def test_independent_resource_requires_supported_version(resources, entry, version) -> None:
    child = {"$id": "urn:camctl:integer", "type": "integer"}
    if version is not None:
        child["$schema"] = version
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": {"integer": child}, "$ref": "urn:camctl:integer",
    }
    expected = schemas.SchemaRuleError if entry == "public" else RuleError
    with pytest.raises(expected):
        _validate_rule(resources, schema, Decimal("1.0"), entry)


@pytest.mark.parametrize("entry", ["public", "acceptance"])
@pytest.mark.parametrize("reference", [True, False])
def test_internal_rule_inherits_version(resources, entry, reference) -> None:
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema"}
    if reference:
        schema.update({"$defs": {"integer": {"type": "integer"}}, "$ref": "#/$defs/integer"})
    else:
        schema["allOf"] = [{"type": "integer"}]
    _validate_rule(resources, schema, Decimal("1.0"), entry)


@pytest.mark.parametrize("entry", ["public", "acceptance"])
def test_internal_rule_cannot_switch_version(resources, entry) -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "allOf": [{"$schema": "http://json-schema.org/draft-07/schema#", "type": "integer"}],
    }
    expected = schemas.SchemaRuleError if entry == "public" else RuleError
    with pytest.raises(expected):
        _validate_rule(resources, schema, Decimal("1.0"), entry)


@pytest.mark.parametrize("entry", ["public", "acceptance"])
def test_supported_independent_resource_keeps_precision(resources, entry) -> None:
    version = "https://json-schema.org/draft/2020-12/schema"
    schema = {
        "$schema": version,
        "$defs": {"integer": {"$id": "urn:camctl:integer", "$schema": version, "type": "integer"}},
        "$ref": "urn:camctl:integer",
    }
    _validate_rule(resources, schema, Decimal("1.0"), entry)


@pytest.mark.parametrize("entry", ["public", "acceptance"])
@pytest.mark.parametrize("keyword", ["const", "enum", "default"])
def test_instance_data_does_not_declare_resource_version(resources, entry, keyword) -> None:
    value = {"$id": "urn:instance", "$schema": "urn:instance-version"}
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema"}
    schema[keyword] = [value] if keyword == "enum" else value
    _validate_rule(resources, schema, value, entry)

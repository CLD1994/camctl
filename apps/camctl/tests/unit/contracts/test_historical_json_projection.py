"""固定 H 的 JSON 实例校验：内存资源与真实精确 Schema 校验器。"""

from copy import deepcopy
import json

import pytest

from camctl.contracts import public_projection as projection, schemas, workflow_errors
from camctl.contracts.json_values import JsonParseError
from camctl.resources import ResourceError


_DRAFT = "https://json-schema.org/draft/2020-12/schema"
_VALID_ERROR = {"code": "capture_result_unconfirmed", "stage": "execution",
                "details": {"activity_id": "7", "reason": "outputs_unknown"}}


@pytest.fixture
def resources(monkeypatch):
    """只声明本单元需要的一个登记和两个 Schema，不访问包资源。"""
    report = {"$schema": _DRAFT, "$defs": {
        "entity_id": {"type": "string", "pattern": "^[1-9][0-9]*$"},
        "error": {"type": "object", "required": ["code", "stage", "details"],
            "properties": {"code": {"type": "string", "minLength": 1},
                "stage": {"type": "string", "minLength": 1}, "details": {"type": "object"}},
            "additionalProperties": False},
        "measurement": {"type": "object", "required": ["count"],
            "properties": {"count": {"type": "integer", "minimum": 1}},
            "additionalProperties": False},
        "nested_object": {"type": "object", "required": ["error"],
            "properties": {"error": {"$ref": "#/$defs/error"}}, "additionalProperties": False},
        "nested_array": {"type": "array", "items": {
            "allOf": [{"$ref": "#/$defs/error"}]}},
    }}
    registry = {"codes": {"capture_result_unconfirmed": {
        "action_error_id": 12, "stage": "execution",
        "details_schema": {"type": "object", "required": ["activity_id", "reason"],
            "properties": {
                "activity_id": {"$ref": "status-report.schema.json#/$defs/entity_id"},
                "reason": {"enum": ["start_unknown", "completion_unknown", "outputs_unknown"]}},
            "additionalProperties": False},
    }}}
    dependencies = {"format_version": 1, "relations": {}, "projections": {
        "sample": {"root_table": "samples", "fields": {"error": {"value": {
            "op": "read", "column": "samples.error_json", "encoding": "json",
            "schema": "#/$defs/error"}}}}}}
    documents = {name: {"$schema": _DRAFT} for name in schemas._SCHEMA_RESOURCES}
    documents.update({"protocol/status-report.schema.json": report,
        "protocol/workflow-codes.json": registry, "registry/report-dependencies.json": dependencies})

    def read(name):
        return json.dumps(documents[name], ensure_ascii=False).encode("utf-8")

    def clear():
        projection._dependencies.cache_clear()
        schemas._registry.cache_clear()
        schemas._validator.cache_clear()
        workflow_errors._registry.cache_clear()
        workflow_errors._details_validator.cache_clear()
        workflow_errors._public_json_registry.cache_clear()
        workflow_errors._public_json_validator.cache_clear()

    clear()
    monkeypatch.setattr(projection, "resource_bytes", read)
    monkeypatch.setattr(schemas, "resource_bytes", read)
    monkeypatch.setattr(workflow_errors, "resource_bytes", read)
    yield documents
    clear()


def _facts(value, *, encoded=False):
    return projection.ProjectionInput("sample", 3, {"samples": {
        3: {"error_json": json.dumps(value, ensure_ascii=False) if encoded else value}}})


def _assert_historical_failure(facts, source="samples.error_json"):
    before = deepcopy(facts.tables)
    with pytest.raises(projection.PublicProjectionError) as caught:
        projection.project_public(facts)
    # 仅断言稳定字段身份，不绑定可翻译的诊断句子。
    assert "sample.error" in str(caught.value) and source in str(caught.value)
    assert facts.tables == before


@pytest.mark.parametrize("value", [
    {"stage": "vendor", "details": {}},
    {"code": "vendor_error", "details": {}},
    {"code": "vendor_error", "stage": "vendor"},
    {"code": "", "stage": "vendor", "details": {}},
    {"code": "vendor_error", "stage": "", "details": {}},
    {"code": 1, "stage": "vendor", "details": {}},
    {"code": "vendor_error", "stage": True, "details": {}},
    {"code": "vendor_error", "stage": "vendor", "details": []},
    {"code": "vendor_error", "stage": "vendor", "details": None},
    {"code": "vendor_error", "stage": "vendor", "details": {}, "internal": 1},
    [],
], ids=["missing-code", "missing-stage", "missing-details", "empty-code", "empty-stage", "numeric-code",
        "boolean-stage", "list-details", "null-details", "extra-member", "nonobject"])
def test_declared_error_schema_rejects_invalid_history_with_field_path(resources, value):
    _assert_historical_failure(_facts(value))


@pytest.mark.parametrize("replacement", [
    {"stage": "device"},
    {"details": {"activity_id": "7", "reason": "no_outputs"}},
    {"details": {"reason": "outputs_unknown"}},
], ids=["wrong-stage", "wrong-reason", "missing-activity"])
def test_registered_error_rejects_invalid_saved_meaning(resources, replacement):
    _assert_historical_failure(_facts({**deepcopy(_VALID_ERROR), **replacement}))


@pytest.mark.parametrize("encoded", [False, True], ids=["structured", "json-text"])
@pytest.mark.parametrize("value", [
    _VALID_ERROR,
    {"code": "future_vendor_error", "stage": "vendor_transfer", "details": {
        "message": "相机\n\"原响应\"", "received": 0, "retry": False,
        "nested": {"value": [None, True, 1, "raw"]}}},
], ids=["registered", "unknown-driver"])
def test_complete_historical_error_preserves_value_and_input(resources, value, encoded):
    facts = _facts(deepcopy(value), encoded=encoded)
    before = deepcopy(facts.tables)
    assert projection.project_public(facts) == {"error": value}
    assert facts.tables == before


@pytest.mark.parametrize("entry", ["read", "registered_error"])
@pytest.mark.parametrize("fault", ["invalid-schema", "missing-reference"])
def test_schema_rule_failure_is_not_a_historical_instance_failure(resources, entry, fault):
    report = resources["protocol/status-report.schema.json"]
    if fault == "invalid-schema":
        report["$defs"]["error"]["type"] = "invalid-json-type"
    else:
        report["$defs"]["error"]["$ref"] = "missing-local.schema.json"
        report["$defs"]["entity_id"]["$ref"] = "missing-local.schema.json"
    if entry == "registered_error":
        resources["registry/report-dependencies.json"]["projections"]["sample"]["fields"]["error"]["value"] = {
            "op": "registered_error", "registry_key": "action_error_id",
            "code_column": "samples.error_code", "details_column": "samples.details_json",
            "schema": "#/$defs/error"}
        facts = projection.ProjectionInput("sample", 3, {"samples": {
            3: {"error_code": 12, "details_json": deepcopy(_VALID_ERROR["details"])}}})
    else:
        facts = _facts(deepcopy(_VALID_ERROR))
    with pytest.raises(schemas.SchemaRuleError) as caught:
        projection.project_public(facts)
    assert not isinstance(caught.value, projection.PublicProjectionError)


@pytest.mark.parametrize("encoded", [False, True], ids=["structured", "json-text"])
@pytest.mark.parametrize("value,valid", [({"count": 2}, True), ({"count": 0}, False),
                                        ({"count": True}, False)])
def test_other_declared_json_schema_is_honored(resources, encoded, value, valid):
    resources["registry/report-dependencies.json"]["projections"]["sample"]["fields"]["error"]["value"]["schema"] = (
        "#/$defs/measurement")
    facts = _facts(deepcopy(value), encoded=encoded)
    if valid:
        before = deepcopy(facts.tables)
        assert projection.project_public(facts) == {"error": value}
        assert facts.tables == before
    else:
        _assert_historical_failure(facts)


@pytest.mark.parametrize("encoded", [False, True], ids=["structured", "json-text"])
def test_json_without_declared_schema_preserves_unconstrained_value(resources, encoded):
    del resources["registry/report-dependencies.json"]["projections"]["sample"]["fields"]["error"]["value"]["schema"]
    value = {"raw": [None, True, 1, "相机原值"], "nested": {"no_public_error_contract": True}}
    facts = _facts(value, encoded=encoded)
    before = deepcopy(facts.tables)
    assert projection.project_public(facts) == {"error": value}
    assert facts.tables == before


def test_invalid_json_text_identifies_public_field_and_source(resources):
    facts = projection.ProjectionInput("sample", 3, {"samples": {3: {"error_json": "{"}}})
    _assert_historical_failure(facts)


def _nested_error_facts(resources, structure, error):
    resources["registry/report-dependencies.json"]["projections"]["sample"]["fields"]["error"]["value"]["schema"] = (
        f"#/$defs/{structure}")
    value = {"error": error} if structure == "nested_object" else [error]
    return _facts(value), value


@pytest.mark.parametrize("structure", ["nested_object", "nested_array"])
@pytest.mark.parametrize("replacement", [
    {"stage": "device"},
    {"details": {"activity_id": "7", "reason": "no_outputs"}},
], ids=["wrong-stage", "wrong-reason"])
def test_declared_nested_error_rejects_invalid_registered_meaning(resources, structure, replacement):
    error = {**deepcopy(_VALID_ERROR), **replacement}
    facts, _ = _nested_error_facts(resources, structure, error)
    _assert_historical_failure(facts)


@pytest.mark.parametrize("structure", ["nested_object", "nested_array"])
@pytest.mark.parametrize("ordinary_error_member", [False, True], ids=["unknown-driver", "ordinary-details-error"])
def test_declared_nested_error_preserves_unknown_business_details(
        resources, structure, ordinary_error_member):
    error = {"code": "future_vendor_error", "stage": "vendor_capture", "details": {
        "vendor_message": "原始\n响应", "received": 0, "retry": False}}
    if ordinary_error_member:
        # details 是开放业务数据；此同名成员没有被 Schema 声明为公共错误。
        error["details"]["error"] = {"code": "capture_result_unconfirmed",
            "stage": "vendor_unrelated_stage", "details": {"reason": "ordinary_business_value"}}
    facts, value = _nested_error_facts(resources, structure, error)
    before = deepcopy(facts.tables)
    assert projection.project_public(facts) == {"error": value}
    assert facts.tables == before


@pytest.mark.parametrize("entry", ["read", "registered_error"])
@pytest.mark.parametrize("fault", ["invalid-utf8", "invalid-json", "resource-error", "os-error"])
def test_workflow_registry_resource_failure_preserves_rule_error_and_cause(
        resources, monkeypatch, entry, fault):
    resource_name = "protocol/workflow-codes.json"
    original_read = workflow_errors.resource_bytes
    injected = {"resource-error": ResourceError("登记资源不可读取"),
                "os-error": OSError("登记资源读取失败")}.get(fault)

    def read(name):
        if name != resource_name:
            return original_read(name)
        if injected is not None:
            raise injected
        return b"\xff" if fault == "invalid-utf8" else b"{"

    monkeypatch.setattr(workflow_errors, "resource_bytes", read)
    if entry == "registered_error":
        resources["registry/report-dependencies.json"]["projections"]["sample"]["fields"]["error"]["value"] = {
            "op": "registered_error", "registry_key": "action_error_id",
            "code_column": "samples.error_code", "details_column": "samples.details_json",
            "schema": "#/$defs/error"}
        facts = projection.ProjectionInput("sample", 3, {"samples": {
            3: {"error_code": 12, "details_json": deepcopy(_VALID_ERROR["details"])}}})
    else:
        facts = _facts(deepcopy(_VALID_ERROR))
    before = deepcopy(facts.tables)
    with pytest.raises(schemas.SchemaRuleError) as caught:
        projection.project_public(facts)
    assert not isinstance(caught.value, projection.PublicProjectionError)
    assert resource_name in str(caught.value)
    if injected is not None:
        assert caught.value.__cause__ is injected
    else:
        expected = UnicodeDecodeError if fault == "invalid-utf8" else JsonParseError
        assert isinstance(caught.value.__cause__, expected)
    assert facts.tables == before


@pytest.mark.parametrize("entry", ["read", "registered_error"])
@pytest.mark.parametrize("fault", ["invalid-type", "invalid-min-length", "missing-reference"])
def test_registered_details_schema_definition_is_checked_at_projection_boundary(resources, entry, fault):
    details_schema = resources["protocol/workflow-codes.json"]["codes"]["capture_result_unconfirmed"]["details_schema"]
    if fault == "invalid-type":
        details_schema["type"] = "invalid-json-type"
    elif fault == "invalid-min-length":
        details_schema["properties"]["activity_id"]["minLength"] = -1
    else:
        details_schema["properties"]["activity_id"]["$ref"] = "missing-local.schema.json"
    if entry == "registered_error":
        resources["registry/report-dependencies.json"]["projections"]["sample"]["fields"]["error"]["value"] = {
            "op": "registered_error", "registry_key": "action_error_id",
            "code_column": "samples.error_code", "details_column": "samples.details_json",
            "schema": "#/$defs/error"}
        facts = projection.ProjectionInput("sample", 3, {"samples": {
            3: {"error_code": 12, "details_json": deepcopy(_VALID_ERROR["details"])}}})
    else:
        facts = _facts(deepcopy(_VALID_ERROR))
    before = deepcopy(facts.tables)
    with pytest.raises(schemas.SchemaRuleError) as caught:
        projection.project_public(facts)
    assert not isinstance(caught.value, projection.PublicProjectionError)
    assert caught.value.__cause__ is not None
    assert facts.tables == before

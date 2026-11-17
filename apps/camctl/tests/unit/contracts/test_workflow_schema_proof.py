"""固定错误规则的构建检查与公共 JSON 校验。"""

import json

import pytest

from camctl.contracts import schemas, workflow_errors

VERSION = "https://json-schema.org/draft/2020-12/schema"
REPORT = "protocol/status-report.schema.json"
CODES = "protocol/workflow-codes.json"


@pytest.fixture
def package_resources(monkeypatch):
    documents = {name: json.dumps({"$schema": VERSION}).encode() for name in schemas._SCHEMA_RESOURCES}
    documents[REPORT] = json.dumps({
        "$schema": VERSION,
        "$defs": {"error": {"type": "object", "required": ["code", "stage", "details"],
                            "properties": {"code": {"type": "string"}}}},
    }).encode()
    documents[CODES] = json.dumps({"codes": {
        "BAD": {"stage": "RUN", "details_schema": {"type": "object", "properties": {
            "number": {"type": "integer"}}, "required": ["number"]}},
    }}).encode()

    def read(name):
        try:
            return documents[name]
        except KeyError:
            raise schemas.ResourceError(name)

    for module in (schemas, workflow_errors):
        monkeypatch.setattr(module, "resource_bytes", read)
    for cached in (schemas._checked_schema, schemas._registry, schemas._validator,
                   workflow_errors._registry, workflow_errors._public_json_registry,
                   workflow_errors._public_json_validator):
        cached.cache_clear()
    yield documents
    for cached in (schemas._checked_schema, schemas._registry, schemas._validator,
                   workflow_errors._registry, workflow_errors._public_json_registry,
                   workflow_errors._public_json_validator):
        cached.cache_clear()


def test_matching_build_check_reuses_fixed_rules_and_still_checks_document(package_resources, monkeypatch):
    package_resources[schemas._VALIDATION_RESOURCE] = schemas.build_validation_resource(package_resources.copy())
    draft = schemas._precise_draft()
    original = draft.check_schema
    checked = []

    def check(schema, *args, **kwargs):
        checked.append(schema)
        return original(schema, *args, **kwargs)

    monkeypatch.setattr(draft, "check_schema", check)
    workflow_errors.validate_public_json("#/$defs/error", {
        "code": "BAD", "stage": "RUN", "details": {"number": 1},
    })
    assert not any("$defs" in rule or rule.get("type") == "object" for rule in checked)
    with pytest.raises(ValueError):
        workflow_errors.validate_public_json("#/$defs/error", {
            "code": "BAD", "stage": "RUN", "details": {"number": "wrong"},
        })


@pytest.mark.parametrize("change", ["missing", "malformed", "incomplete", "duplicate", "environment", "reference", "registry"])
def test_unmatched_check_rechecks_all_fixed_rules(package_resources, monkeypatch, change):
    monkeypatch.setattr(schemas, "_validation_context", lambda: {"version": 1})
    proof = schemas.build_validation_resource(package_resources.copy())
    package_resources[schemas._VALIDATION_RESOURCE] = proof
    if change == "missing":
        del package_resources[schemas._VALIDATION_RESOURCE]
    elif change == "malformed":
        package_resources[schemas._VALIDATION_RESOURCE] = b"invalid"
    elif change == "incomplete":
        package_resources[schemas._VALIDATION_RESOURCE] = b'{}'
    elif change == "duplicate":
        package_resources[schemas._VALIDATION_RESOURCE] = proof.rstrip()[:-1] + b',"workflow":"duplicate"}'
    elif change == "environment":
        monkeypatch.setattr(schemas, "_validation_context", lambda: {"version": 2})
    elif change == "reference":
        package_resources["protocol/plan.schema.json"] += b" "
    else:
        package_resources[CODES] += b" "

    draft = schemas._precise_draft()
    original = draft.check_schema
    checked = []

    def check(schema, *args, **kwargs):
        checked.append(schema)
        return original(schema, *args, **kwargs)

    monkeypatch.setattr(draft, "check_schema", check)
    workflow_errors.validate_public_json("#/$defs/error", {
        "code": "BAD", "stage": "RUN", "details": {"number": 1},
    })
    assert any("$defs" in rule for rule in checked)
    assert any(rule.get("required") == ["number"] for rule in checked)


def test_changed_actual_registry_cannot_borrow_resource_check(package_resources):
    package_resources[schemas._VALIDATION_RESOURCE] = schemas.build_validation_resource(package_resources.copy())
    workflow_errors.registered_error("BAD")["details_schema"]["type"] = "invalid"
    with pytest.raises(schemas.SchemaRuleError):
        workflow_errors.validate_public_json("#/$defs/error", {"code": "BAD", "stage": "RUN", "details": {}})


def test_build_rejects_invalid_detail_and_arbitrary_rules_remain_checked(package_resources):
    registry = json.loads(package_resources[CODES])
    registry["codes"]["BAD"]["details_schema"]["type"] = "invalid"
    package_resources[CODES] = json.dumps(registry).encode()
    with pytest.raises(schemas.SchemaRuleError):
        schemas.build_validation_resource(package_resources.copy())
    with pytest.raises(schemas.SchemaRuleError):
        schemas.create_validator({"$schema": VERSION, "type": "invalid"})


def test_document_still_enforces_stage_and_exact_integer(package_resources):
    from decimal import Decimal

    package_resources[schemas._VALIDATION_RESOURCE] = schemas.build_validation_resource(package_resources.copy())
    workflow_errors.validate_public_json("#/$defs/error", {
        "code": "BAD", "stage": "RUN", "details": {"number": Decimal("1.0")},
    })
    for stage, number in (("OTHER", 1), ("RUN", Decimal("1.0000000000000001"))):
        with pytest.raises(ValueError):
            workflow_errors.validate_public_json("#/$defs/error", {
                "code": "BAD", "stage": stage, "details": {"number": number},
            })


def test_changed_actual_reference_cannot_borrow_resource_check(package_resources, monkeypatch):
    package_resources[schemas._VALIDATION_RESOURCE] = schemas.build_validation_resource(package_resources.copy())
    schemas.schema_registry().contents("plan.schema.json")["type"] = "invalid"
    draft = schemas._precise_draft()
    original = draft.check_schema
    checked = []

    def check(schema, *args, **kwargs):
        checked.append(schema)
        return original(schema, *args, **kwargs)

    monkeypatch.setattr(draft, "check_schema", check)
    with pytest.raises(schemas.SchemaRuleError):
        workflow_errors.validate_public_json("#/$defs/error", {
            "code": "BAD", "stage": "RUN", "details": {"number": 1},
        })
    assert any("$defs" in rule for rule in checked)
    assert any(rule.get("required") == ["number"] for rule in checked)

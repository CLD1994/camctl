"""固定错误规则的构建检查与公共 JSON 校验。"""

import importlib
from importlib import metadata, resources
import json
import sys
from types import SimpleNamespace

import pytest
from referencing import Registry

import camctl.contracts
import camctl.resources

VERSION = "https://json-schema.org/draft/2020-12/schema"
REPORT = "protocol/status-report.schema.json"
CODES = "protocol/workflow-codes.json"


@pytest.fixture
def isolated(monkeypatch):
    names = (
        "protocol/plan.schema.json", "protocol/status-report.schema.json",
        "protocol/capabilities.schema.json", "protocol/host-notification.schema.json",
    )
    documents = {name: json.dumps({"$schema": VERSION}).encode() for name in names}
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
            raise camctl.resources.ResourceError(name) from None

    class CodeFiles:
        def joinpath(self, name):
            return SimpleNamespace(read_bytes=lambda: ("code:" + name).encode())

    originals = {name: importlib.import_module(f"camctl.contracts.{name}")
                 for name in ("schemas", "workflow_errors")}
    actual_files = resources.files
    actual_version = metadata.version
    monkeypatch.setattr(camctl.resources, "resource_bytes", read)
    monkeypatch.setattr(resources, "files", lambda package: (
        CodeFiles() if package == "camctl.contracts" else actual_files(package)))
    monkeypatch.setattr(metadata, "version", lambda name: (
        "1.0" if name in ("jsonschema", "referencing", "jsonschema-specifications")
        else actual_version(name)))

    # 重新导入两个模块，使每例使用独立进程内状态；结束时恢复原模块及包绑定。
    for name in ("schemas", "workflow_errors"):
        qualified = f"camctl.contracts.{name}"
        monkeypatch.setattr(camctl.contracts, name, originals[name])
        monkeypatch.delitem(sys.modules, qualified)
    schemas = importlib.import_module("camctl.contracts.schemas")
    workflow_errors = importlib.import_module("camctl.contracts.workflow_errors")
    return SimpleNamespace(documents=documents, schemas=schemas, workflow_errors=workflow_errors)


@pytest.fixture
def package_resources(isolated):
    return isolated.documents


@pytest.fixture
def schemas(isolated):
    return isolated.schemas


@pytest.fixture
def workflow_errors(isolated):
    return isolated.workflow_errors


def spy_rule_checks(schemas, monkeypatch):
    validator_type = type(schemas.create_validator({"$schema": VERSION}, registry=Registry()))
    original = validator_type.check_schema
    checked = []

    def check(schema, *args, **kwargs):
        checked.append(schema)
        return original(schema, *args, **kwargs)

    monkeypatch.setattr(validator_type, "check_schema", check)
    return checked


def test_matching_build_check_reuses_fixed_rules_and_still_checks_document(package_resources, schemas, workflow_errors, monkeypatch):
    package_resources["protocol/schema-validation.json"] = schemas.build_validation_resource(package_resources.copy())
    checked = spy_rule_checks(schemas, monkeypatch)
    workflow_errors.validate_public_json("#/$defs/error", {
        "code": "BAD", "stage": "RUN", "details": {"number": 1},
    })
    assert not any("$defs" in rule or rule.get("type") == "object" for rule in checked)
    with pytest.raises(ValueError):
        workflow_errors.validate_public_json("#/$defs/error", {
            "code": "BAD", "stage": "RUN", "details": {"number": "wrong"},
        })


@pytest.mark.parametrize("change", ["missing", "malformed", "incomplete", "duplicate", "environment", "reference", "registry"])
def test_unmatched_check_rechecks_all_fixed_rules(package_resources, schemas, workflow_errors, monkeypatch, change):
    proof = schemas.build_validation_resource(package_resources.copy())
    package_resources["protocol/schema-validation.json"] = proof
    if change == "missing":
        del package_resources["protocol/schema-validation.json"]
    elif change == "malformed":
        package_resources["protocol/schema-validation.json"] = b"invalid"
    elif change == "incomplete":
        package_resources["protocol/schema-validation.json"] = b'{}'
    elif change == "duplicate":
        package_resources["protocol/schema-validation.json"] = proof.rstrip()[:-1] + b',"workflow":"duplicate"}'
    elif change == "environment":
        old_proof = json.loads(proof)
        old_proof["context"]["python"] = [0, 0]
        package_resources["protocol/schema-validation.json"] = json.dumps(old_proof).encode()
    elif change == "reference":
        package_resources["protocol/plan.schema.json"] += b" "
    else:
        package_resources[CODES] += b" "

    checked = spy_rule_checks(schemas, monkeypatch)
    workflow_errors.validate_public_json("#/$defs/error", {
        "code": "BAD", "stage": "RUN", "details": {"number": 1},
    })
    assert any("$defs" in rule for rule in checked)
    assert any(rule.get("required") == ["number"] for rule in checked)


def test_changed_actual_registry_cannot_borrow_resource_check(package_resources, schemas, workflow_errors):
    package_resources["protocol/schema-validation.json"] = schemas.build_validation_resource(package_resources.copy())
    workflow_errors.registered_error("BAD")["details_schema"]["type"] = "invalid"
    with pytest.raises(schemas.SchemaRuleError):
        workflow_errors.validate_public_json("#/$defs/error", {"code": "BAD", "stage": "RUN", "details": {}})


def test_build_rejects_invalid_detail_and_arbitrary_rules_remain_checked(package_resources, schemas):
    registry = json.loads(package_resources[CODES])
    registry["codes"]["BAD"]["details_schema"]["type"] = "invalid"
    package_resources[CODES] = json.dumps(registry).encode()
    with pytest.raises(schemas.SchemaRuleError):
        schemas.build_validation_resource(package_resources.copy())
    with pytest.raises(schemas.SchemaRuleError):
        schemas.create_validator({"$schema": VERSION, "type": "invalid"})


def test_document_still_enforces_stage_and_exact_integer(package_resources, schemas, workflow_errors):
    from decimal import Decimal

    package_resources["protocol/schema-validation.json"] = schemas.build_validation_resource(package_resources.copy())
    workflow_errors.validate_public_json("#/$defs/error", {
        "code": "BAD", "stage": "RUN", "details": {"number": Decimal("1.0")},
    })
    for stage, number in (("OTHER", 1), ("RUN", Decimal("1.0000000000000001"))):
        with pytest.raises(ValueError):
            workflow_errors.validate_public_json("#/$defs/error", {
                "code": "BAD", "stage": stage, "details": {"number": number},
            })


def test_changed_actual_reference_cannot_borrow_resource_check(package_resources, schemas, workflow_errors, monkeypatch):
    package_resources["protocol/schema-validation.json"] = schemas.build_validation_resource(package_resources.copy())
    schemas.schema_registry().contents("plan.schema.json")["type"] = "invalid"
    checked = spy_rule_checks(schemas, monkeypatch)
    with pytest.raises(schemas.SchemaRuleError):
        workflow_errors.validate_public_json("#/$defs/error", {
            "code": "BAD", "stage": "RUN", "details": {"number": 1},
        })
    assert any("$defs" in rule for rule in checked)
    assert any(rule.get("required") == ["number"] for rule in checked)

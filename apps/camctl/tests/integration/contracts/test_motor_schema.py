"""共同电机夹具与真实包 Schema、本地引用及精确受理的组合验证。"""

from __future__ import annotations

import json
from decimal import localcontext
from pathlib import Path

import pytest

from camctl.acceptance.schema import BodySchemaError, plan_fragment, validate_precise
from camctl.contracts.json_values import JsonParseError, parse_exact_json
from camctl.contracts.schemas import SchemaValidationError, validate_document

FIXTURES = Path(__file__).resolve().parents[5] / "protocol/examples/host-notifications/cases.json"
CASES = json.loads(FIXTURES.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_shared_motor_case_through_packaged_schema(case) -> None:
    try:
        document = parse_exact_json(case["json"])
    except JsonParseError:
        assert case["valid"] is False
        return
    schema_name = {
        "notification": "protocol/host-notification.schema.json",
        "plan": "protocol/plan.schema.json",
        "report": "protocol/status-report.schema.json",
    }[case["kind"]]
    if case["kind"] == "report":
        # 动作片段同样通过生产本地注册表解析资源，不假造根报告。
        fragment = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$ref": "status-report.schema.json#/$defs/action",
        }
        if case["valid"]:
            validate_precise(fragment, document)
        else:
            with pytest.raises(BodySchemaError):
                validate_precise(fragment, document)
    elif case["valid"]:
        validate_document(schema_name, document)
    else:
        with pytest.raises(SchemaValidationError):
            validate_document(schema_name, document)


@pytest.mark.parametrize(
    "case", [case for case in CASES if case["kind"] == "plan"],
    ids=lambda case: case["name"],
)
def test_shared_motor_plan_keeps_admission_boundary(case) -> None:
    document = parse_exact_json(case["json"])
    if case["admission"] == "plan_rejected":
        with pytest.raises(BodySchemaError):
            validate_precise(plan_fragment("plan_structure"), document)
        return
    validate_precise(plan_fragment("plan_structure"), document)
    if case["admission"] == "action_failed":
        with pytest.raises(BodySchemaError):
            validate_precise(plan_fragment("action"), document["actions"][0])
    else:
        validate_precise(plan_fragment("action"), document["actions"][0])


@pytest.mark.parametrize(
    "case", [case for case in CASES if case["kind"] == "notification" and case["valid"]],
    ids=lambda case: case["name"],
)
def test_shared_wire_is_exact_canonical_integer(case) -> None:
    document = parse_exact_json(case["json"])
    wire = parse_exact_json(case["wire"])
    assert wire == document
    assert type(wire["params"]["position"]) is int
    assert str(wire["params"]["position"]) == case["position"]
    assert case["wire"].endswith("\n")
    validate_document("protocol/host-notification.schema.json", wire)


@pytest.mark.parametrize("text,valid", [
    ('{"type":"motor_control","action_instance_id":"12","params":{"position":1.0}}', True),
    ('{"type":"motor_control","action_instance_id":"12","params":{"position":1.00000000000000000001}}', False),
    ('{"type":"motor_control","action_instance_id":"12","params":{"position":2147483647.00000000000001}}', False),
])
def test_motor_number_validation_ignores_decimal_context(text, valid) -> None:
    with localcontext() as context:
        context.prec = 2
        value = parse_exact_json(text)
        if valid:
            validate_document("protocol/host-notification.schema.json", value)
        else:
            with pytest.raises(SchemaValidationError):
                validate_document("protocol/host-notification.schema.json", value)


@pytest.mark.parametrize("code,details,valid", [
    ("motor_channel_unavailable", {"reason": "not_connected"}, True),
    ("motor_channel_unavailable", {"reason": "invalid_descriptor", "errno": 9}, True),
    ("motor_channel_unavailable", {"reason": "disabled_after_partial_write"}, True),
    ("motor_channel_unavailable", {}, False),
    ("motor_channel_unavailable", {"reason": "unknown"}, False),
    ("motor_channel_unavailable", {"reason": "not_connected", "errno": 9}, False),
    ("motor_channel_unavailable", {"reason": "invalid_descriptor", "errno": True}, False),
    ("motor_notification_failed", {"reason": "would_block", "written_bytes": 0, "errno": 11}, True),
    ("motor_notification_failed", {"reason": "broken_pipe", "written_bytes": 0, "errno": 32}, True),
    ("motor_notification_failed", {"reason": "os_error", "written_bytes": 0, "errno": 4}, True),
    ("motor_notification_failed", {"reason": "short_write", "written_bytes": 2}, True),
    ("motor_notification_failed", {"reason": "short_write", "written_bytes": 0}, True),
    ("motor_notification_failed", {"reason": "message_too_large", "written_bytes": 0}, True),
    ("motor_notification_failed", {"reason": "os_error", "written_bytes": 0}, False),
    ("motor_notification_failed", {"reason": "os_error", "written_bytes": 1, "errno": 4}, False),
    ("motor_notification_failed", {"reason": "short_write", "written_bytes": 1, "errno": 4}, False),
    ("motor_notification_failed", {"reason": "message_too_large", "written_bytes": 1}, False),
    ("motor_notification_failed", {"reason": "short_write", "written_bytes": -1}, False),
    ("motor_notification_failed", {"reason": "short_write", "written_bytes": True}, False),
    ("motor_notification_unconfirmed", {}, True),
    ("motor_notification_unconfirmed", {"written_bytes": 0}, False),
])
def test_motor_error_details_preserve_known_facts(code, details, valid) -> None:
    from camctl.contracts.workflow_errors import validate_error_details

    if valid:
        validate_error_details(code, details)
    else:
        with pytest.raises(ValueError):
            validate_error_details(code, details)

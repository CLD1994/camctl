"""真实包资源与精确公共及受理校验器的组合验证。"""

from __future__ import annotations

from decimal import Decimal
import subprocess
import sys

import pytest

from camctl.acceptance.schema import validate_plan_structure
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.schemas import SchemaValidationError, validate_document


def test_packaged_report_accepts_equivalent_integer_numbers() -> None:
    report = parse_exact_json('{"report_id":"1","from_wm":1.0,"to_wm":1e0}')
    validate_document("protocol/status-report.schema.json", report)


@pytest.mark.parametrize("watermark", [True, Decimal("1.0000000000000001")])
def test_packaged_report_rejects_noninteger_watermark(watermark) -> None:
    with pytest.raises(SchemaValidationError):
        validate_document(
            "protocol/status-report.schema.json",
            {"report_id": "1", "from_wm": 0, "to_wm": watermark},
        )


def test_packaged_plan_local_reference_accepts_decimal_integer() -> None:
    plan = parse_exact_json('''{
        "request_id":"1","created_at":"2026-01-15 08:00:00","name":"拍摄",
        "actions":[{"name":"拍照","type":"camera_take_photo","device_id":"cam-1",
        "scheduled_at":"2026-01-15 09:00:00","params":{"type":"single_shot"},
        "policy":{"max_delay_ms":1.0}}]
    }''')
    validate_document("protocol/plan.schema.json", plan)
    validate_plan_structure(plan)


def test_packaged_capabilities_schema_is_available() -> None:
    validate_document("protocol/capabilities.schema.json", {"devices": []})


def test_workflow_error_registry_first_load_preserves_resource_failure() -> None:
    script = r'''
import camctl.resources as resources

original = resources.resource_bytes
hits = []
failure = None

def read(name):
    if name == "protocol/workflow-codes.json":
        hits.append(name)
        if failure is not None:
            raise failure
    return original(name)

resources.resource_bytes = read
from camctl.contracts.workflow_errors import registered_error
from camctl.contracts.schemas import SchemaRuleError
assert not hits
for failure in (resources.ResourceError("资源不可读取"), OSError("读取出错")):
    try:
        registered_error("capture_result_unconfirmed")
    except SchemaRuleError as error:
        assert error.__cause__ is failure
    else:
        raise AssertionError("登记资源错误未传播")
failure = None
assert registered_error("capture_result_unconfirmed")["stage"] == "execution"
assert hits
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr

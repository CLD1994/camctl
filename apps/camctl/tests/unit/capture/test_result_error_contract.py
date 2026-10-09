"""ResultSetSave 的公共错误契约；资源读取由内存替身隔离。"""

from copy import deepcopy
import json

import pytest

from camctl.capture.models import ResultSetPhase, ResultSetSave
from camctl.contracts import schemas, workflow_errors

_NOW = 1_750_000_000_000_000
_VALID = {
    "code": "capture_result_unconfirmed", "stage": "execution",
    "details": {"activity_id": "7", "reason": "outputs_unknown"},
}


@pytest.fixture(autouse=True)
def error_resources(monkeypatch):
    """只提供本单元需要的公共结构和单个登记，真实资源由组件测试核验。"""
    header = {"$schema": "https://json-schema.org/draft/2020-12/schema"}
    report = {**header, "$defs": {
        "entity_id": {"type": "string", "pattern": "^[1-9][0-9]*$"},
        "error": {
            "type": "object", "required": ["code", "stage", "details"],
            "properties": {
                "code": {"type": "string", "minLength": 1},
                "stage": {"type": "string", "minLength": 1},
                "details": {"type": "object"},
            }, "additionalProperties": False,
        },
    }}
    registry = {"codes": {"capture_result_unconfirmed": {
        "stage": "execution", "action_error_id": 12,
        "details_schema": {
            "type": "object", "required": ["activity_id", "reason"],
            "properties": {
                "activity_id": {"$ref": "status-report.schema.json#/$defs/entity_id"},
                "reason": {"enum": ["start_unknown", "completion_unknown", "outputs_unknown"]},
            }, "additionalProperties": False,
        },
    }}}
    documents = {name: json.dumps(header).encode() for name in schemas._SCHEMA_RESOURCES}
    documents["protocol/status-report.schema.json"] = json.dumps(report).encode()
    documents["protocol/workflow-codes.json"] = json.dumps(registry).encode()

    def clear():
        schemas._registry.cache_clear()
        schemas._validator.cache_clear()
        workflow_errors._registry.cache_clear()
        workflow_errors._details_validator.cache_clear()

    clear()
    monkeypatch.setattr(schemas, "resource_bytes", documents.__getitem__)
    monkeypatch.setattr(workflow_errors, "resource_bytes", documents.__getitem__)
    yield
    clear()


def _command(location, error):
    return ResultSetSave(
        action_id=3, occurred_at=_NOW, phase=ResultSetPhase.UNCONFIRMED,
        contract="task_scope_files", observation={"reason": "attempts_exhausted"},
        capture={"status": "unconfirmed", "error": error if location == "capture" else deepcopy(_VALID)},
        error=error if location == "last_error" else deepcopy(_VALID),
    )


_BAD_ERRORS = [
    {"stage": "execution", "details": {}},
    {"code": "vendor_failure", "details": {}},
    {"code": "vendor_failure", "stage": "read"},
    {"code": "", "stage": "read", "details": {}},
    {"code": "vendor_failure", "stage": "", "details": {}},
    {"code": 1, "stage": "read", "details": {}},
    {"code": "vendor_failure", "stage": True, "details": {}},
    {"code": "vendor_failure", "stage": "read", "details": []},
    {"code": "vendor_failure", "stage": "read", "details": None},
    {"code": "vendor_failure", "stage": "read", "details": {}, "internal": 1},
]


@pytest.mark.parametrize("location", ["capture", "last_error"])
@pytest.mark.parametrize("error", _BAD_ERRORS, ids=[
    "missing-code", "missing-stage", "missing-details", "empty-code", "empty-stage",
    "numeric-code", "boolean-stage", "list-details", "null-details", "extra-member",
])
def test_result_error_shape_rejects_incomplete_input(location, error):
    with pytest.raises(ValueError):
        _command(location, error)


@pytest.mark.parametrize("location", ["capture", "last_error"])
@pytest.mark.parametrize("replacement", [
    {"stage": "device"},
    {"details": {"activity_id": "7", "reason": "no_outputs"}},
    {"details": {"reason": "outputs_unknown"}},
], ids=["wrong-stage", "wrong-reason", "missing-activity"])
def test_registered_error_rejects_wrong_stage_or_details(location, replacement):
    error = {**deepcopy(_VALID), **replacement}
    with pytest.raises(ValueError):
        _command(location, error)


@pytest.mark.parametrize("location", ["capture", "last_error"])
def test_unknown_driver_error_preserves_full_value(location):
    error = {"code": "future_vendor_failure", "stage": "vendor_transfer",
             "details": {"message": "相机\n\"响应\"", "retry": False, "received": 0,
                         "nested": {"vendor": [None, "raw", 2]}}}
    original = deepcopy(error)
    command = _command(location, error)
    saved = command.capture["error"] if location == "capture" else command.error
    assert saved == original and error == original


@pytest.mark.parametrize("location", ["capture", "last_error"])
def test_registered_complete_error_is_preserved(location):
    error = deepcopy(_VALID)
    command = _command(location, error)
    saved = command.capture["error"] if location == "capture" else command.error
    assert saved == _VALID and error == _VALID

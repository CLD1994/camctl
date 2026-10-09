"""活动与结果守卫核实际保存值；公共错误资源由内存替身提供。"""

from copy import deepcopy
from decimal import Decimal

import pytest

from camctl.contracts import schemas, workflow_errors
from camctl.contracts.history_values import TransactionRange
from camctl.contracts.schemas import SchemaRuleError
from camctl.history import validators
from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.history.validators import EventContext, EventValidationError
from camctl.persistence.repositories.capture import register_capture_guards

from .test_result_error_contract import error_resources

_ACTIVITY_ID = 7
_ACTION_ID = 3
_KNOWN = {
    "code": "capture_result_unconfirmed", "stage": "execution",
    "details": {"activity_id": "7", "reason": "outputs_unknown"},
}
_UNKNOWN = {
    "code": "vendor_future", "stage": "vendor_transfer",
    "details": {"message": "响应\n\"原值\"", "number": Decimal("0.125"),
                "flag": False, "nested": [None, {"error": "普通业务内容"}]},
}
_BRANCHES = ["create", "observe", "release", "complete", "unsatisfied",
             "unconfirmed", "emergency"]


@pytest.fixture
def guards(error_resources, monkeypatch):
    """隔离守卫登记和派生缓存，不隔离被测校验器。"""
    monkeypatch.setattr(validators, "NAMED_GUARDS", dict(validators.NAMED_GUARDS))
    register_capture_guards()
    workflow_errors._public_json_registry.cache_clear()
    workflow_errors._public_json_validator.cache_clear()
    yield validators.NAMED_GUARDS
    workflow_errors._public_json_registry.cache_clear()
    workflow_errors._public_json_validator.cache_clear()


def _activity():
    return {
        "id": _ACTIVITY_ID, "action_id": _ACTION_ID,
        "activity_state": 1, "dispatch_state": 3, "occupancy_state": 1,
        "ownership_mode": 1, "baseline_state": 1,
        "capture_json": None, "last_error_json": None,
        "completion_basis": 1, "completion_evidence_json": None,
        "result_set_state": 1, "result_check_json": None,
    }


def _world(branch="observe", *, current=None, changes=None):
    """构造各守卫分支的事实；结束有停止依据，结论有原核实状态。"""
    facts = _activity() if current is None else deepcopy(current)
    event_type, reason = 13, 2
    if branch == "create":
        values = {key: value for key, value in facts.items() if key != "id"}
        values.update(dispatch_state=1)
        values.update(deepcopy(changes or {}))
        row = RowChange("device_activities", _ACTIVITY_ID,
                        RowImage(False, {}), RowImage(True, values))
        reason = 1
    else:
        if branch == "release":
            facts["activity_state"] = 3
            after = {"occupancy_state": 2}
            reason = 3
        elif branch in ("complete", "unsatisfied", "unconfirmed"):
            event_type = 16
            reason, state, outcome = {
                "complete": (1, 3, 1), "unsatisfied": (2, 3, 2),
                "unconfirmed": (3, 4, 3),
            }[branch]
            after = {"result_set_state": state, "result_check_json": {
                "contract": "task_scope_files", "outcome": outcome,
                "observation": {"reason": "independent_result_evidence"},
            }}
            if branch in ("complete", "unsatisfied"):
                if branch == "complete":
                    facts["activity_state"] = 3
                after.update(
                    completion_basis=2 if branch == "complete" else 4,
                    completion_evidence_json={
                        "method": "device_evidence" if branch == "complete" else "known_failure",
                        "observation": {"source": "independent_evidence"},
                    })
        elif branch == "emergency":
            event_type, reason = 33, 1
            facts["activity_state"] = 2
            after = {"activity_state": 3}
        else:
            after = {"activity_state": 2}
        after.update(deepcopy(changes or {}))
        before = {key: facts[key] for key in after if key in facts}
        row = RowChange("device_activities", _ACTIVITY_ID,
                        RowImage(True, before), RowImage(True, after))
    event = EventEnvelope(1, 1, event_type, 1, 0, 1, None, reason, {}, (row,))
    context = EventContext(
        TransactionRange(1, 1, 1),
        {("device_activities", _ACTIVITY_ID): ("action", _ACTION_ID)},
        {"device_activities": {_ACTIVITY_ID: facts},
         "actions": {_ACTION_ID: {"id": _ACTION_ID, "type": 1}},
         "operation_runs": {9: {"id": 9, "status": 3, "activity_id": _ACTIVITY_ID}}},
    )
    return event, context


def _error_changes(location, error):
    if location == "capture":
        return {"capture_json": {"status": "unconfirmed", "error": deepcopy(error)}}
    return {"last_error_json": deepcopy(error)}


def _reject(guard, event, context):
    with pytest.raises(EventValidationError) as raised:
        guard(event, context)
    assert raised.value.__cause__ is not None
    assert not isinstance(raised.value, SchemaRuleError)


def _snapshot(event, context):
    """比较守卫收到的事实，保持只读覆盖声明的原对象。"""
    return deepcopy((event, context.owners, context.state_rows))


@pytest.mark.parametrize("branch", _BRANCHES)
def test_activity_accepts_legal_empty_fields_in_all_registered_branches(guards, branch):
    event, context = _world(branch)
    original = _snapshot(event, context)
    guards["activity"](event, context)
    assert _snapshot(event, context) == original


@pytest.mark.parametrize("branch", _BRANCHES)
def test_activity_rejects_incomplete_error_in_every_registered_branch(guards, branch):
    event, context = _world(branch, changes={"last_error_json": {"code": "vendor"}})
    _reject(guards["activity"], event, context)


_BAD_ERRORS = [
    pytest.param({"stage": "execution", "details": {}}, id="missing-code"),
    pytest.param({"code": "vendor", "details": {}}, id="missing-stage"),
    pytest.param({"code": "vendor", "stage": "device"}, id="missing-details"),
    pytest.param({"code": "", "stage": "device", "details": {}}, id="empty-code"),
    pytest.param({"code": "vendor", "stage": "", "details": {}}, id="empty-stage"),
    pytest.param({"code": True, "stage": "device", "details": {}}, id="code-type"),
    pytest.param({"code": "vendor", "stage": 1, "details": {}}, id="stage-type"),
    pytest.param({"code": "vendor", "stage": "device", "details": []}, id="details-type"),
    pytest.param({"code": "vendor", "stage": "device", "details": None}, id="null-details"),
    pytest.param({"code": "vendor", "stage": "device", "details": {}, "extra": 1}, id="extra"),
    pytest.param({**_KNOWN, "stage": "device"}, id="registered-stage"),
    pytest.param({**_KNOWN, "details": {"activity_id": "7", "reason": "no_outputs"}}, id="registered-reason"),
    pytest.param({**_KNOWN, "details": {"reason": "outputs_unknown"}}, id="registered-detail"),
    pytest.param([], id="non-object"),
]


@pytest.mark.parametrize("location", ["capture", "last_error"])
@pytest.mark.parametrize("error", _BAD_ERRORS)
def test_activity_rejects_actual_error_shape_or_registered_meaning(guards, location, error):
    event, context = _world(changes=_error_changes(location, error))
    _reject(guards["activity"], event, context)


@pytest.mark.parametrize("location", ["capture", "last_error"])
@pytest.mark.parametrize("error", [_KNOWN, _UNKNOWN], ids=["known", "unknown"])
def test_activity_preserves_complete_actual_errors(guards, location, error):
    event, context = _world(changes=_error_changes(location, error))
    original = _snapshot(event, context)
    guards["activity"](event, context)
    assert _snapshot(event, context) == original


@pytest.mark.parametrize("location", ["capture", "last_error"])
def test_activity_rejects_unchanged_bad_error_from_current_facts(guards, location):
    current = _activity()
    current.update(_error_changes(location, {"code": "vendor"}))
    event, context = _world(current=current)
    _reject(guards["activity"], event, context)


@pytest.mark.parametrize("location", ["capture", "last_error"])
def test_activity_uses_replacement_after_instead_of_old_invalid_error(guards, location):
    current = _activity()
    current.update(_error_changes(location, {"code": "vendor"}))
    event, context = _world(current=current, changes=_error_changes(location, _KNOWN))
    original = _snapshot(event, context)
    guards["activity"](event, context)
    assert _snapshot(event, context) == original


@pytest.mark.parametrize("branch", ["create", "observe"])
@pytest.mark.parametrize("column", ["capture_json", "last_error_json"])
def test_activity_rejects_missing_reliable_fields_instead_of_defaulting_empty(guards, branch, column):
    current = _activity()
    del current[column]
    event, context = _world(branch, current=current)
    with pytest.raises(EventValidationError):
        guards["activity"](event, context)


def test_activity_rejects_missing_reliable_action_type(guards):
    event, context = _world(changes={"capture_json": {"status": "running"}})
    context.state_rows["actions"][_ACTION_ID].pop("type")
    with pytest.raises(EventValidationError):
        guards["activity"](event, context)


@pytest.mark.parametrize("status", ["running", "completed", "canceled", "failed", "unconfirmed"])
def test_activity_accepts_capture_status_with_required_error_presence(guards, status):
    capture = {"status": status, "captured_count": 0, "elapsed_s": Decimal("0.125")}
    if status in ("failed", "unconfirmed"):
        capture["error"] = deepcopy(_UNKNOWN)
    event, context = _world(changes={"capture_json": capture})
    original = _snapshot(event, context)
    guards["activity"](event, context)
    assert _snapshot(event, context) == original


@pytest.mark.parametrize("status", ["running", "completed", "canceled", "failed", "unconfirmed"])
def test_activity_rejects_wrong_error_presence_for_capture_status(guards, status):
    capture = {"status": status}
    if status in ("running", "completed", "canceled"):
        capture["error"] = deepcopy(_KNOWN)
    event, context = _world(changes={"capture_json": capture})
    _reject(guards["activity"], event, context)


@pytest.mark.parametrize("capture", [
    pytest.param({}, id="missing-status"),
    pytest.param({"status": "future"}, id="unknown-status"),
    pytest.param({"status": 1}, id="status-type"),
    pytest.param({"status": "running", "extra": 1}, id="unknown-member"),
    pytest.param({"status": "running", "captured_count": None}, id="null-count"),
    pytest.param({"status": "running", "captured_count": True}, id="count-bool"),
    pytest.param({"status": "running", "captured_count": -1}, id="count-negative"),
    pytest.param({"status": "running", "captured_count": Decimal("1.5")}, id="count-fraction"),
    pytest.param({"status": "running", "captured_count": 9007199254740992}, id="count-unsafe"),
    pytest.param({"status": "running", "elapsed_s": None}, id="null-seconds"),
    pytest.param({"status": "running", "elapsed_s": True}, id="seconds-bool"),
    pytest.param({"status": "running", "elapsed_s": "1"}, id="seconds-string"),
    pytest.param({"status": "running", "elapsed_s": -1}, id="seconds-negative"),
    pytest.param({"status": "running", "elapsed_s": 0.125}, id="seconds-float"),
    pytest.param({"status": "running", "elapsed_s": float("inf")}, id="seconds-infinite"),
    pytest.param({"status": "running", "elapsed_s": Decimal("Infinity")}, id="decimal-seconds-infinite"),
    pytest.param({"status": "running", "elapsed_s": Decimal("NaN")}, id="seconds-nan"),
    pytest.param([], id="capture-type"),
])
def test_activity_rejects_invalid_capture_fields(guards, capture):
    event, context = _world(changes={"capture_json": capture})
    _reject(guards["activity"], event, context)


@pytest.mark.parametrize("capture", [
    {"status": "running"},
    {"status": "completed", "captured_count": 9007199254740991, "elapsed_s": 0},
])
def test_activity_accepts_omitted_unknown_measurements_or_safe_boundaries(guards, capture):
    event, context = _world(changes={"capture_json": capture})
    guards["activity"](event, context)


def test_activity_preserves_unchanged_decimal_seconds_from_current_facts(guards):
    current = _activity()
    current["capture_json"] = {"status": "running", "elapsed_s": Decimal("0.5")}
    event, context = _world(current=current)
    original = _snapshot(event, context)
    guards["activity"](event, context)
    assert _snapshot(event, context) == original
    assert type(context.state_rows["device_activities"][_ACTIVITY_ID]["capture_json"]["elapsed_s"]) is Decimal


def test_activity_accepts_large_finite_integer_seconds_without_float_conversion(guards):
    event, context = _world(changes={"capture_json": {
        "status": "running", "elapsed_s": 10 ** 1000}})
    original = _snapshot(event, context)
    guards["activity"](event, context)
    assert _snapshot(event, context) == original


def test_activity_rejects_explicit_null_capture_error(guards):
    event, context = _world(changes={"capture_json": {"status": "unconfirmed", "error": None}})
    _reject(guards["activity"], event, context)


@pytest.mark.parametrize("branch,status", [
    ("complete", "completed"), ("unsatisfied", "failed"), ("unconfirmed", "unconfirmed"),
])
def test_result_check_accepts_capture_matching_conclusion(guards, branch, status):
    capture = {"status": status}
    if status != "completed":
        capture["error"] = deepcopy(_UNKNOWN)
    event, context = _world(branch, changes={"capture_json": capture})
    original = _snapshot(event, context)
    guards["result_check"](event, context)
    assert _snapshot(event, context) == original


@pytest.mark.parametrize("branch,status", [
    ("complete", "unconfirmed"), ("unsatisfied", "completed"), ("unconfirmed", "failed"),
])
def test_result_check_rejects_capture_mismatching_conclusion(guards, branch, status):
    capture = {"status": status}
    if status != "completed":
        capture["error"] = deepcopy(_UNKNOWN)
    event, context = _world(branch, changes={"capture_json": capture})
    with pytest.raises(EventValidationError):
        guards["result_check"](event, context)


@pytest.mark.parametrize("branch", ["complete", "unsatisfied", "unconfirmed"])
def test_result_check_allows_conclusion_without_new_capture(guards, branch):
    event, context = _world(branch)
    guards["result_check"](event, context)


@pytest.mark.parametrize("location", ["capture", "last_error"])
def test_activity_propagates_schema_resource_failure_without_instance_conversion(guards, monkeypatch, location):
    event, context = _world(changes=_error_changes(location, _KNOWN))
    schemas._registry.cache_clear()
    schemas._validator.cache_clear()
    monkeypatch.setattr(schemas, "resource_bytes", lambda name: b"not-json")
    with pytest.raises(SchemaRuleError) as raised:
        guards["activity"](event, context)
    assert not isinstance(raised.value, EventValidationError)
    assert raised.value.__cause__ is not None

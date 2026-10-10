"""应急补记具名守卫的组合契约单元测试。

直接以事件信封核对守卫自己的语义契约：停止依据与结果分类的组合、
尝试结果中的停止证据，以及一条最终流程的行形状。经公共保存入口的
组合由集成测试验证。
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, Inexact, Rounded, localcontext
import json

import pytest

from camctl.contracts import schemas, workflow_errors
from camctl.contracts.history_values import TransactionRange
from camctl.contracts.schemas import SchemaRuleError
from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.history.validators import EventContext, EventValidationError
from camctl.persistence.repositories import capture as capture_repository
from camctl.persistence.repositories.capture import _emergency_guard

_SESSION = "a" * 32
_EVENT_ID = 33

_RESULT = {
    "format_version": 1,
    "settlement": {
        "basis": "observed",
        "evidence": {"type": "stop_confirmed", "version": 1, "data": {}},
    },
    "observations": [],
}


def _stopped_result(activity_id: int = 1) -> dict:
    """携带指向目标活动的停止观察的尝试结果。"""
    return {
        "format_version": 1,
        "settlement": _RESULT["settlement"],
        "observations": [{
            "type": "stop_confirmed",
            "version": 1,
            "data": {"activity_id": str(activity_id)},
        }],
    }


def _stop_observation(activity_id: int = 1) -> dict:
    return {"type": "stop_confirmed", "version": 1,
            "data": {"activity_id": str(activity_id)}}


def _final_run(status: int, *, attempts_used: int, error=None,
               activity_id: int = 1) -> RowChange:
    return RowChange(
        table="operation_runs",
        row_id=11,
        before=RowImage(exists=False, values={}),
        after=RowImage(exists=True, values={
            "kind": 9,
            "status": status,
            "attempts_used": attempts_used,
            "max_attempts_used": 3,
            "timeout_s_json": Decimal("10"),
            "retry_interval_s_json": Decimal("1"),
            "error_json": error,
            "retry_wait_required": 0,
            "session_key": _SESSION,
            "activity_id": activity_id,
            "responsibility_key": f"emergency/{_SESSION}/{activity_id}",
        }),
    )


def _attempt_row(attempt_no: int, *, status: int = 3, result=None) -> RowChange:
    return RowChange(
        table="operation_attempts",
        row_id=100 + attempt_no,
        before=RowImage(exists=False, values={}),
        after=RowImage(exists=True, values={
            "run_id": 11,
            "attempt_no": attempt_no,
            "status": status,
            "copy_round": None,
            "intent_event_id": None,
            "result_event_id": _EVENT_ID,
            "result_json": _RESULT if result is None else result,
            "max_attempts_used": 3,
            "timeout_s_json": Decimal("10"),
            "retry_interval_s_json": Decimal("1"),
        }),
    )


def _envelope(rows, evidence=None) -> EventEnvelope:
    return EventEnvelope(
        event_id=_EVENT_ID,
        transaction_id=7,
        event_type=33,
        event_version=1,
        occurred_at=1,
        clock_status=1,
        change_seq=None,
        reason=1,
        evidence={} if evidence is None else evidence,
        rows=tuple(rows),
    )


def _context(activity_id=1, *, error=None) -> EventContext:
    """提供可靠的目标原事实，不计算事件应用后的预期。"""
    return EventContext(
        TransactionRange(7, _EVENT_ID, _EVENT_ID),
        {("operation_runs", 11): ("action", 3),
         ("device_activities", activity_id): ("action", 3)},
        {"device_activities": {activity_id: {
            "id": activity_id, "action_id": 3,
            "activity_state": 2, "occupancy_state": 1,
            "capture_json": None, "last_error_json": deepcopy(error),
        }}, "actions": {3: {"id": 3, "type": 2}}},
    )


def _snapshot(event, context):
    return deepcopy(event), deepcopy(context.state_rows), dict(context.owners)


class TestAttemptedStopEvidence:
    """停止成功的补记必须有可靠停止证据：尝试结果或事件依据。"""

    def test_stopped_with_attempts_requires_result_stop_evidence(self) -> None:
        event = _envelope((_final_run(3, attempts_used=1), _attempt_row(1)))
        with pytest.raises(EventValidationError, match="停止观察"):
            _emergency_guard(event, _context())

    @pytest.mark.parametrize("result", [
        None,
        {"format_version": 1, "observations": []},
        {"format_version": 1, "observations": "not-a-list"},
        {"format_version": 1, "observations": [{
            "type": "", "version": 1, "data": {"activity_id": "1"},
        }]},
    ])
    def test_result_without_reliable_observation_is_not_evidence(self, result) -> None:
        event = _envelope((
            _final_run(3, attempts_used=1), _attempt_row(1, result=result),
        ))
        with pytest.raises(EventValidationError, match="停止观察"):
            _emergency_guard(event, _context())

    def test_result_observation_pointing_elsewhere_is_not_evidence(self) -> None:
        event = _envelope((
            _final_run(3, attempts_used=1),
            _attempt_row(1, result=_stopped_result(activity_id=9)),
        ))
        with pytest.raises(EventValidationError, match="停止观察"):
            _emergency_guard(event, _context())

    def test_pointing_result_observation_is_accepted(self) -> None:
        event = _envelope((
            _final_run(3, attempts_used=2),
            _attempt_row(1),
            _attempt_row(2, result=_stopped_result()),
        ))
        _emergency_guard(event, _context())

    def test_attempt_external_evidence_covers_attempted_stop(self) -> None:
        """依据来自尝试结果之外时经事件依据成员保存，同样成立。"""
        event = _envelope(
            (_final_run(3, attempts_used=1), _attempt_row(1)),
            evidence={"observation": _stop_observation()},
        )
        _emergency_guard(event, _context())


@pytest.mark.usefixtures("emergency_error_resources")
class TestStopObservationBoundaries:
    """停止依据只伴随停止成功；最终流程行恰好一条。"""

    @pytest.mark.parametrize("rows", [
        (_final_run(4, attempts_used=0,
                    error={"code": "emergency_not_attempted", "stage": "emergency",
                           "details": {"activity_id": "1", "reason": "配置未知"}}),),
        (_final_run(6, attempts_used=1,
                    error={"code": "emergency_stop_unconfirmed", "stage": "emergency",
                           "details": {"activity_id": "1", "reason": "停止仍未确认"}}),
         _attempt_row(1, status=4)),
    ])
    def test_stop_observation_only_with_stopped_outcome(self, rows) -> None:
        event = _envelope(rows, evidence={"observation": _stop_observation()})
        with pytest.raises(EventValidationError, match="停止成功的补记"):
            _emergency_guard(event, _context(error=rows[0].after.values["error_json"]))

    def test_second_final_flow_row_is_rejected(self) -> None:
        event = _envelope(
            (_final_run(3, attempts_used=0), _final_run(3, attempts_used=0)),
            evidence={"observation": _stop_observation()},
        )
        with pytest.raises(EventValidationError, match="一个最终流程"):
            _emergency_guard(event, _context())


@pytest.fixture
def emergency_error_resources(monkeypatch):
    """只隔离公共资源 IO，守卫仍执行真实错误结构和登记校验。"""
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
    details = {
        "type": "object", "required": ["activity_id", "reason"],
        "properties": {
            "activity_id": {"$ref": "status-report.schema.json#/$defs/entity_id"},
            "reason": {"type": "string", "minLength": 1},
        }, "additionalProperties": False,
    }
    registry = {"codes": {
        "emergency_not_attempted": {"stage": "emergency", "details_schema": details},
        "emergency_stop_unconfirmed": {"stage": "emergency", "details_schema": details},
    }}
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


def _failed_event(status, error):
    attempts = () if status == 4 else (_attempt_row(1, status=4),)
    return _envelope((
        _final_run(status, attempts_used=len(attempts), error=error, activity_id=7),
        *attempts,
    ))


_BAD_AGGREGATE_ERRORS = [
    pytest.param({"stage": "emergency", "details": {}}, id="missing-code"),
    pytest.param({"code": "emergency_not_attempted", "details": {}}, id="missing-stage"),
    pytest.param({"code": "emergency_not_attempted", "stage": "emergency"}, id="missing-details"),
    pytest.param({"code": "vendor_failure", "stage": "emergency", "details": {}}, id="wrong-code"),
    pytest.param({"code": "emergency_not_attempted", "stage": "execution", "details": {
        "activity_id": "7", "reason": "配置未知"}}, id="wrong-stage"),
    pytest.param({"code": "emergency_not_attempted", "stage": "emergency", "details": []}, id="details-type"),
    pytest.param({"code": "emergency_not_attempted", "stage": "emergency", "details": {
        "reason": "配置未知"}}, id="missing-activity"),
    pytest.param({"code": "emergency_not_attempted", "stage": "emergency", "details": {
        "activity_id": 7, "reason": "配置未知"}}, id="activity-type"),
    pytest.param({"code": "emergency_not_attempted", "stage": "emergency", "details": {
        "activity_id": "07", "reason": "配置未知"}}, id="activity-leading-zero"),
    pytest.param({"code": "emergency_not_attempted", "stage": "emergency", "details": {
        "activity_id": "9", "reason": "配置未知"}}, id="different-activity"),
    pytest.param({"code": "emergency_not_attempted", "stage": "emergency", "details": {
        "activity_id": "7"}}, id="missing-reason"),
    pytest.param({"code": "emergency_not_attempted", "stage": "emergency", "details": {
        "activity_id": "7", "reason": ""}}, id="empty-reason"),
    pytest.param({"code": "emergency_not_attempted", "stage": "emergency", "details": {
        "activity_id": "7", "reason": None}}, id="null-reason"),
    pytest.param({"code": "emergency_not_attempted", "stage": "emergency", "details": {
        "activity_id": "7", "reason": False}}, id="reason-type"),
]


@pytest.mark.usefixtures("emergency_error_resources")
class TestAggregateErrorWithoutActivityChange:
    """活动原值相同时没有活动变化行，流程错误仍必须完整校验。"""

    @pytest.mark.parametrize("error", _BAD_AGGREGATE_ERRORS)
    def test_rejects_actual_invalid_error_without_activity_row(self, error) -> None:
        event = _failed_event(4, error)
        with pytest.raises(EventValidationError) as raised:
            _emergency_guard(event, _context(7, error={
                "code": "emergency_not_attempted", "stage": "emergency",
                "details": {"activity_id": "7", "reason": "配置未知"},
            }))
        assert not isinstance(raised.value, SchemaRuleError)

    @pytest.mark.parametrize("status, code", [
        (4, "emergency_stop_unconfirmed"),
        (6, "emergency_not_attempted"),
    ])
    def test_rejects_other_partition_code(self, status, code) -> None:
        error = {
            "code": code, "stage": "emergency",
            "details": {"activity_id": "7", "reason": "本次实际原因"},
        }
        event = _failed_event(status, error)
        with pytest.raises(EventValidationError):
            _emergency_guard(event, _context(7, error=error))

    @pytest.mark.parametrize("status, code", [
        (4, "emergency_not_attempted"),
        (6, "emergency_stop_unconfirmed"),
    ])
    def test_accepts_and_preserves_full_actual_error(self, status, code) -> None:
        error = {
            "code": code, "stage": "emergency", "details": {
                "activity_id": "7", "reason": '相机“原值”\n含引号"与反斜线\\',
            },
        }
        event, context = _failed_event(status, error), _context(7, error=error)
        original = _snapshot(event, context)
        _emergency_guard(event, context)
        assert _snapshot(event, context) == original


def _aggregate_error(code, *, activity_id="7", reason="本次实际原因"):
    return {"code": code, "stage": "emergency", "details": {
        "activity_id": activity_id, "reason": reason,
    }}


def _activity_error_row(before_error, after_error, *, activity_id=7):
    return RowChange(
        "device_activities", activity_id,
        RowImage(True, {"last_error_json": deepcopy(before_error)}),
        RowImage(True, {"last_error_json": deepcopy(after_error)}),
    )


@pytest.mark.usefixtures("emergency_error_resources")
class TestRunAndTargetActivityErrorAgreement:
    """流程与本目标活动的完整错误相同，活动省略字段沿用原事实。"""

    @pytest.mark.parametrize("status, code", [
        (4, "emergency_not_attempted"),
        (6, "emergency_stop_unconfirmed"),
    ])
    @pytest.mark.parametrize("different", ["reason", "activity_id"])
    def test_rejects_two_legal_but_different_actual_errors(self, status, code, different):
        run_error = _aggregate_error(code)
        activity_error = _aggregate_error(
            code, reason="另一个实际原因" if different == "reason" else "本次实际原因",
            activity_id="9" if different == "activity_id" else "7")
        event = _failed_event(status, run_error)
        event = _envelope((*event.rows, _activity_error_row(None, activity_error)))
        with pytest.raises(EventValidationError):
            _emergency_guard(event, _context(7))

    @pytest.mark.parametrize("status, code", [
        (4, "emergency_not_attempted"),
        (6, "emergency_stop_unconfirmed"),
    ])
    def test_omitted_activity_row_cannot_keep_other_original_error(self, status, code):
        event = _failed_event(status, _aggregate_error(code))
        context = _context(7, error=_aggregate_error(code, reason="原有另一个原因"))
        with pytest.raises(EventValidationError):
            _emergency_guard(event, context)

    def test_other_activity_change_does_not_replace_target_original_error(self):
        error = _aggregate_error("emergency_not_attempted")
        event = _failed_event(4, error)
        event = _envelope((*event.rows, _activity_error_row(None, error, activity_id=9)))
        context = _context(7, error=_aggregate_error(
            "emergency_not_attempted", reason="目标保留的原有原因"))
        with pytest.raises(EventValidationError):
            _emergency_guard(event, context)

    @pytest.mark.parametrize("missing", ["context", "target", "error_field"])
    def test_missing_reliable_target_error_is_rejected(self, missing):
        event = _failed_event(4, _aggregate_error("emergency_not_attempted"))
        context = _context(7)
        if missing == "context":
            context = None
        elif missing == "target":
            context.state_rows["device_activities"].clear()
        else:
            context.state_rows["device_activities"][7].pop("last_error_json")
        with pytest.raises(EventValidationError):
            _emergency_guard(event, context)

    @pytest.mark.parametrize("status, code", [
        (4, "emergency_not_attempted"),
        (6, "emergency_stop_unconfirmed"),
    ])
    def test_matching_after_error_replaces_different_original_error(self, status, code):
        actual = _aggregate_error(code, reason='本次"实际原因"\n完整保留')
        previous = _aggregate_error(code, reason="原有原因")
        event = _failed_event(status, actual)
        event = _envelope((*event.rows, _activity_error_row(previous, actual)))
        context = _context(7, error=previous)
        original = _snapshot(event, context)
        _emergency_guard(event, context)
        assert _snapshot(event, context) == original

    def test_stopped_without_activity_change_cannot_keep_failure_error(self):
        event = _envelope(
            (_final_run(3, attempts_used=0, activity_id=7),),
            evidence={"observation": _stop_observation(7)})
        context = _context(7, error=_aggregate_error("emergency_not_attempted"))
        with pytest.raises(EventValidationError):
            _emergency_guard(event, context)

    def test_stopped_after_error_cleared_matches_successful_run(self):
        previous = _aggregate_error("emergency_not_attempted")
        event = _envelope(
            (_final_run(3, attempts_used=0, activity_id=7),
             _activity_error_row(previous, None)),
            evidence={"observation": _stop_observation(7)})
        context = _context(7, error=previous)
        original = _snapshot(event, context)
        _emergency_guard(event, context)
        assert _snapshot(event, context) == original

    def test_public_error_resource_failure_preserves_original_exception(self, monkeypatch):
        error = _aggregate_error("emergency_not_attempted")
        event, context = _failed_event(4, error), _context(7, error=error)
        failure = SchemaRuleError("公共资源不能读取")

        def unavailable(value):
            raise failure

        monkeypatch.setattr(capture_repository, "validate_public_error", unavailable)
        with pytest.raises(SchemaRuleError) as raised:
            _emergency_guard(event, context)
        assert raised.value is failure


_CONFIGURATION_FIELDS = [
    "max_attempts_used", "timeout_s_json", "retry_interval_s_json",
]
_INVALID_CONFIGURATION_VALUES = [
    pytest.param("max_attempts_used", 0, id="maximum-zero"),
    pytest.param("max_attempts_used", -1, id="maximum-negative"),
    pytest.param("max_attempts_used", True, id="maximum-boolean"),
    pytest.param("max_attempts_used", Decimal("1.5"), id="maximum-fraction"),
    pytest.param("max_attempts_used", "3", id="maximum-string"),
    pytest.param("timeout_s_json", Decimal("0"), id="timeout-zero"),
    pytest.param("timeout_s_json", Decimal("-0.25"), id="timeout-negative"),
    pytest.param("timeout_s_json", True, id="timeout-boolean"),
    pytest.param("timeout_s_json", 1.25, id="timeout-float"),
    pytest.param("timeout_s_json", "1", id="timeout-string"),
    pytest.param("timeout_s_json", Decimal("Infinity"), id="timeout-infinite"),
    pytest.param("timeout_s_json", Decimal("NaN"), id="timeout-nan"),
    pytest.param("retry_interval_s_json", Decimal("-0.25"), id="interval-negative"),
    pytest.param("retry_interval_s_json", False, id="interval-boolean"),
    pytest.param("retry_interval_s_json", 0.25, id="interval-float"),
    pytest.param("retry_interval_s_json", "1", id="interval-string"),
    pytest.param("retry_interval_s_json", Decimal("Infinity"), id="interval-infinite"),
    pytest.param("retry_interval_s_json", Decimal("NaN"), id="interval-nan"),
]


def _configuration_case(*, attempts_used=0):
    """其他前提完全合法的未尝试或停止未确认事件。"""
    status, code = ((4, "emergency_not_attempted") if not attempts_used
                    else (6, "emergency_stop_unconfirmed"))
    error = _aggregate_error(code)
    run = _final_run(status, attempts_used=attempts_used, error=error, activity_id=7)
    attempts = tuple(_attempt_row(number, status=4)
                     for number in range(1, attempts_used + 1))
    return _envelope((run, *attempts)), _context(7, error=error)


@pytest.mark.usefixtures("emergency_error_resources")
class TestEmergencyConfiguration:
    """最终流程与每次实际尝试共同保存合法且相同的固定配置。"""

    @pytest.mark.parametrize("field, value", _INVALID_CONFIGURATION_VALUES)
    def test_rejects_invalid_known_run_configuration(self, field, value):
        event, context = _configuration_case()
        event.rows[0].after.values[field] = value
        with pytest.raises(EventValidationError):
            _emergency_guard(event, context)

    @pytest.mark.parametrize("location", ["run", "attempt"])
    @pytest.mark.parametrize("field", _CONFIGURATION_FIELDS)
    @pytest.mark.parametrize("missing", [False, True], ids=["unknown", "missing"])
    def test_attempted_configuration_must_be_complete(self, location, field, missing):
        event, context = _configuration_case(attempts_used=1)
        values = event.rows[0 if location == "run" else 1].after.values
        if missing:
            values.pop(field)
        else:
            values[field] = None
        with pytest.raises(EventValidationError):
            _emergency_guard(event, context)

    @pytest.mark.parametrize("attempt_no", [1, 2])
    @pytest.mark.parametrize("field, other", [
        ("max_attempts_used", 2),
        ("timeout_s_json", Decimal("9.5")),
        ("retry_interval_s_json", Decimal("0")),
    ])
    def test_each_attempt_configuration_must_match_fixed_run(self, attempt_no, field, other):
        event, context = _configuration_case(attempts_used=2)
        event.rows[attempt_no].after.values[field] = other
        with pytest.raises(EventValidationError):
            _emergency_guard(event, context)

    @pytest.mark.parametrize("fields", [
        ("max_attempts_used",), ("timeout_s_json",), ("retry_interval_s_json",),
        ("max_attempts_used", "timeout_s_json", "retry_interval_s_json"),
    ], ids=["unknown-limit", "unknown-timeout", "unknown-interval", "all-unknown"])
    @pytest.mark.parametrize("stopped", [False, True])
    def test_zero_attempt_unknown_configuration_is_explicit_and_legal(self, fields, stopped):
        if stopped:
            event = _envelope(
                (_final_run(3, attempts_used=0, activity_id=7),),
                evidence={"observation": _stop_observation(7)})
            context = _context(7)
        else:
            event, context = _configuration_case()
        for field in fields:
            event.rows[0].after.values[field] = None
        original = _snapshot(event, context)
        _emergency_guard(event, context)
        assert _snapshot(event, context) == original

    def test_exact_seconds_are_preserved_under_low_decimal_precision(self):
        event, context = _configuration_case(attempts_used=1)
        timeout = Decimal("0.00000000000000000000000000001")
        interval = Decimal("0.1000000000000000000000000001")
        for row in event.rows:
            row.after.values["timeout_s_json"] = timeout
            row.after.values["retry_interval_s_json"] = interval
        original = _snapshot(event, context)
        with localcontext() as arithmetic:
            arithmetic.prec = 2
            arithmetic.traps[Inexact] = True
            arithmetic.traps[Rounded] = True
            _emergency_guard(event, context)
        assert _snapshot(event, context) == original
        assert event.rows[0].after.values["timeout_s_json"] == Decimal(
            "0.00000000000000000000000000001")
        assert event.rows[1].after.values["retry_interval_s_json"] == Decimal(
            "0.1000000000000000000000000001")

    def test_integer_seconds_and_zero_interval_are_legal(self):
        event, context = _configuration_case(attempts_used=1)
        for row in event.rows:
            row.after.values["timeout_s_json"] = 10
            row.after.values["retry_interval_s_json"] = 0
        original = _snapshot(event, context)
        _emergency_guard(event, context)
        assert _snapshot(event, context) == original

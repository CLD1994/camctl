"""等待事件的时间契约通过真实事件校验和具名守卫执行。"""

import pytest

from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import TransactionRange
from camctl.devices.tasks import CompletionMode, EndControl, StartReturn
from camctl.history import validators
from camctl.history.events import load_event_registry
from camctl.history.validators import EventContext, EventValidationError, validate_event
from camctl.persistence.repositories.timelapse import register_timelapse_guards
from camctl.persistence.transaction import _encode_body, event_envelope, update_change


_SENT_AT = 1_700_000_000_000_000
_EXPECTED_CHECK_AT = {
    0: 1_700_000_001_200_000,
    1000: 1_700_000_002_200_000,
}
_WAIT_EVENT_ID = 2
_ATTEMPT_EVENT_ID = 1
_ACTION_ID = 1
_ACTIVITY_ID = 20
_RUN_ID = 10
_ATTEMPT_ID = 30


@pytest.fixture
def wait_guard(monkeypatch):
    """在本用例私有注册表中装入真实等待守卫，结束后恢复原注册表。"""
    monkeypatch.setattr(validators, "NAMED_GUARDS", dict(validators.NAMED_GUARDS))
    register_timelapse_guards()
    return validators.NAMED_GUARDS["capture_wait"]


def _sent_result(activity_id: int):
    return {
        "format_version": 1,
        "settlement": {
            "basis": "observed",
            "evidence": {"type": "operation_returned", "version": 1, "data": {}},
        },
        "observations": [
            {"type": "timelapse_sent", "version": 1,
             "data": {"activity_id": str(activity_id)}}
        ],
    }


def _source_attempt_event(attempt):
    """从内存尝试事实构造原 ATTEMPT_RESULT 历史输入。"""
    registry = load_event_registry()
    status = enum_for("operation_attempts.status")
    effect = enum_for("operation_attempts.effect_state")
    branch_name = "FAIL" if attempt["status"] == int(status.FAILED) else "SUCCEED"
    event_spec = registry["events"]["ATTEMPT_RESULT"]
    event = event_envelope(
        _ATTEMPT_EVENT_ID,
        _ATTEMPT_EVENT_ID,
        event_spec["id"],
        event_spec["branches"][branch_name]["reason"],
        (update_change(
            "operation_attempts",
            _ATTEMPT_ID,
            {
                "status": int(status.RUNNING),
                "result_event_id": None,
                "effect_state": int(effect.UNKNOWN),
                "result_json": None,
                "error_json": None,
            },
            {
                "status": attempt["status"],
                "result_event_id": _ATTEMPT_EVENT_ID,
                "effect_state": attempt["effect_state"],
                "result_json": attempt["result_json"],
                "error_json": attempt["error_json"],
            },
        ),),
        _SENT_AT,
        evidence={"attempt_id": _ATTEMPT_ID},
    )
    return {
        "id": _ATTEMPT_EVENT_ID,
        "transaction_id": _ATTEMPT_EVENT_ID,
        "event_type": event.event_type,
        "event_version": event.event_version,
        "occurred_at": event.occurred_at,
        "clock_status": event.clock_status,
        "change_seq": event.change_seq,
        "body_json": _encode_body(event),
    }


def _memory_wait_case(
    startup: int,
    branch: str,
    *,
    change: str = "none",
    extra_wait_ms: int = 1000,
):
    """用无副作用的行事实和原 START 事件构造一次等待变更。"""
    registry = load_event_registry()
    status = enum_for("operation_attempts.status")
    effect = enum_for("operation_attempts.effect_state")
    run_kind = enum_for("operation_runs.kind")
    action_type = enum_for("actions.type")
    due = _EXPECTED_CHECK_AT[startup]
    result = _sent_result(_ACTIVITY_ID)
    attempt = {
        "id": _ATTEMPT_ID,
        "run_id": _RUN_ID,
        "attempt_no": 1,
        "status": int(status.SUCCEEDED),
        "effect_state": int(effect.CONFIRMED),
        "result_event_id": _ATTEMPT_EVENT_ID,
        "result_json": result,
        "error_json": None,
    }
    action = {
        "id": _ACTION_ID,
        "type": int(action_type.CAMERA_TIMELAPSE),
        "execution_spec_json": {
            "video_result_mode": "single_file",
            "duration_based": True,
            "wait_after_send": True,
            "end_control": int(EndControl.DEVICE),
            "stop_supported": True,
            "start_return_meaning": int(StartReturn.SENT),
            "completion_mode": int(CompletionMode.TIME_AND_OUTPUTS),
            "startup_margin_ms": startup,
            "target_duration_ms": 1000,
            "result_wait_margin_ms": 200,
        },
    }
    scheduled = branch != "schedule"
    activity = {
        "id": _ACTIVITY_ID,
        "action_id": _ACTION_ID,
        "sent_at": _SENT_AT,
        "result_wait_margin_ms": 200 if scheduled else None,
        "extra_wait_ms_used": 0 if scheduled else None,
        "expected_check_at": due if scheduled else None,
        "wait_completed_event_id": None,
    }
    run = {
        "id": _RUN_ID,
        "responsibility_key": f"start/{_ACTION_ID}",
        "action_id": _ACTION_ID,
        "activity_id": _ACTIVITY_ID,
        "kind": int(run_kind.START),
    }

    if change in ("photo_taken", "start_confirmed"):
        result["observations"][0]["type"] = change
    elif change == "wrong_activity":
        result["observations"][0]["data"]["activity_id"] = "999"
    elif change == "no_observation":
        result["observations"] = []
    elif change == "failed":
        attempt["status"] = int(status.FAILED)
        attempt["error_json"] = {
            "code": "device_error", "stage": "device", "details": {},
        }
    elif change == "unknown_effect":
        attempt["effect_state"] = int(effect.UNKNOWN)

    if change == "a":
        activity["sent_at"] += 1
    elif change == "r" and branch != "schedule":
        activity["result_wait_margin_ms"] += 1
    elif change == "d":
        action["execution_spec_json"]["target_duration_ms"] += 1
    elif change == "source":
        run = None

    if branch == "schedule":
        before = {
            "result_wait_margin_ms": None,
            "extra_wait_ms_used": None,
            "expected_check_at": None,
        }
        after = {
            "result_wait_margin_ms": 201 if change == "r" else 200,
            "extra_wait_ms_used": 0,
            "expected_check_at": due + (1 if change == "q" else 0),
        }
        reason = registry["events"]["CAPTURE_WAIT_CHANGED"]["branches"]["SCHEDULE"]["reason"]
        occurred_at = _SENT_AT
    elif branch == "reconfigure":
        before = {"extra_wait_ms_used": 0, "expected_check_at": due}
        delta_us = {1: 1000, 1000: 1_000_000}[extra_wait_ms]
        after = {
            "extra_wait_ms_used": extra_wait_ms,
            "expected_check_at": due + delta_us + (1 if change == "q" else 0),
        }
        reason = registry["events"]["CAPTURE_WAIT_CHANGED"]["branches"]["RECONFIGURE"]["reason"]
        occurred_at = _SENT_AT
    else:
        if change == "q":
            activity["expected_check_at"] += 1
        before = {"wait_completed_event_id": None}
        after = {"wait_completed_event_id": _WAIT_EVENT_ID}
        reason = registry["events"]["CAPTURE_WAIT_CHANGED"]["branches"]["COMPLETE"]["reason"]
        occurred_at = due + 100

    event_spec = registry["events"]["CAPTURE_WAIT_CHANGED"]
    event = event_envelope(
        _WAIT_EVENT_ID,
        _WAIT_EVENT_ID,
        event_spec["id"],
        reason,
        (update_change("device_activities", _ACTIVITY_ID, before, after),),
        occurred_at,
    )
    state = {
        "actions": {_ACTION_ID: action},
        "device_activities": {_ACTIVITY_ID: activity},
        "operation_runs": {} if run is None else {_RUN_ID: run},
        "operation_attempts": {_ATTEMPT_ID: attempt},
        "history_events": {_ATTEMPT_EVENT_ID: _source_attempt_event(attempt)},
    }
    context = EventContext(
        TransactionRange(_WAIT_EVENT_ID, _WAIT_EVENT_ID, _WAIT_EVENT_ID),
        {("device_activities", _ACTIVITY_ID): ("action", _ACTION_ID)},
        state,
    )
    return event, context


@pytest.mark.parametrize("startup", [0, 1000])
@pytest.mark.parametrize("branch", ["schedule", "complete"])
def test_wait_schedule_and_completion_accept_original_finite_sent(wait_guard, startup, branch):
    event, context = _memory_wait_case(startup, branch)

    assert validate_event(event, context).envelope == event


@pytest.mark.parametrize("startup", [0, 1000])
@pytest.mark.parametrize("extra_wait_ms", [1, 1000])
def test_wait_reconfigure_accepts_both_explicit_millisecond_values(
    wait_guard, startup, extra_wait_ms,
):
    event, context = _memory_wait_case(
        startup, "reconfigure", extra_wait_ms=extra_wait_ms)

    assert validate_event(event, context).envelope == event


@pytest.mark.parametrize("startup", [0, 1000])
@pytest.mark.parametrize("branch", ["schedule", "reconfigure", "complete"])
@pytest.mark.parametrize(
    "change",
    ["photo_taken", "start_confirmed", "wrong_activity", "no_observation", "failed", "unknown_effect"],
)
def test_wait_event_rejects_unreliable_start_result(wait_guard, startup, branch, change):
    event, context = _memory_wait_case(startup, branch, change=change)

    with pytest.raises(EventValidationError):
        validate_event(event, context)


@pytest.mark.parametrize("startup", [0, 1000])
@pytest.mark.parametrize("branch", ["schedule", "reconfigure", "complete"])
@pytest.mark.parametrize("change", ["q", "a", "r", "d", "source"])
def test_wait_event_rejects_changed_original_timing_or_source(wait_guard, startup, branch, change):
    event, context = _memory_wait_case(startup, branch, change=change)

    with pytest.raises(EventValidationError):
        validate_event(event, context)

"""应急补记具名守卫的组合契约单元测试。

直接以事件信封核对守卫自己的语义契约：停止依据与结果分类的组合、
尝试结果中的停止证据，以及一条最终流程的行形状。经公共保存入口的
组合由集成测试验证。
"""

from __future__ import annotations

import pytest

from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.history.validators import EventValidationError
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


class TestAttemptedStopEvidence:
    """停止成功的补记必须有可靠停止证据：尝试结果或事件依据。"""

    def test_stopped_with_attempts_requires_result_stop_evidence(self) -> None:
        event = _envelope((_final_run(3, attempts_used=1), _attempt_row(1)))
        with pytest.raises(EventValidationError, match="停止观察"):
            _emergency_guard(event, None)

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
            _emergency_guard(event, None)

    def test_result_observation_pointing_elsewhere_is_not_evidence(self) -> None:
        event = _envelope((
            _final_run(3, attempts_used=1),
            _attempt_row(1, result=_stopped_result(activity_id=9)),
        ))
        with pytest.raises(EventValidationError, match="停止观察"):
            _emergency_guard(event, None)

    def test_pointing_result_observation_is_accepted(self) -> None:
        event = _envelope((
            _final_run(3, attempts_used=2),
            _attempt_row(1),
            _attempt_row(2, result=_stopped_result()),
        ))
        _emergency_guard(event, None)

    def test_attempt_external_evidence_covers_attempted_stop(self) -> None:
        """依据来自尝试结果之外时经事件依据成员保存，同样成立。"""
        event = _envelope(
            (_final_run(3, attempts_used=1), _attempt_row(1)),
            evidence={"observation": _stop_observation()},
        )
        _emergency_guard(event, None)


class TestStopObservationBoundaries:
    """停止依据只伴随停止成功；最终流程行恰好一条。"""

    @pytest.mark.parametrize("rows", [
        (_final_run(4, attempts_used=0,
                    error={"code": "emergency_not_attempted", "stage": "emergency"}),),
        (_final_run(6, attempts_used=1,
                    error={"code": "emergency_stop_unconfirmed", "stage": "emergency"}),
         _attempt_row(1, status=4)),
    ])
    def test_stop_observation_only_with_stopped_outcome(self, rows) -> None:
        event = _envelope(rows, evidence={"observation": _stop_observation()})
        with pytest.raises(EventValidationError, match="停止成功的补记"):
            _emergency_guard(event, None)

    def test_second_final_flow_row_is_rejected(self) -> None:
        event = _envelope(
            (_final_run(3, attempts_used=0), _final_run(3, attempts_used=0)),
            evidence={"observation": _stop_observation()},
        )
        with pytest.raises(EventValidationError, match="一个最终流程"):
            _emergency_guard(event, None)

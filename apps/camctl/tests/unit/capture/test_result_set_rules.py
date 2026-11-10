"""结果集合结论只使用已保存的活动事实与原核实结果。"""

import pytest

from camctl.capture.models import ResultSetPhase
from camctl.capture.result_set_rules import decide_result_basis, result_conclusion_error
from camctl.contracts.values import ConsistencyError


def _activity():
    return {"completion_basis": None, "completion_mode": 2, "dispatch_state": 3,
            "sent_at": 10, "expected_check_at": 30, "wait_completed_event_id": 12,
            "activity_state": 3}


def test_time_and_outputs_requires_original_wait_and_preserves_evidence():
    facts = _activity()
    evidence = {"method": "time_and_outputs", "wait_completed_event_id": 12, "other": "original"}
    basis, saved = decide_result_basis(facts, ResultSetPhase.COMPLETE, evidence, None)
    assert basis == 3 and saved == evidence and saved is not evidence
    with pytest.raises(ConsistencyError):
        decide_result_basis(facts, ResultSetPhase.COMPLETE,
                            evidence | {"wait_completed_event_id": 13}, None)
    with pytest.raises(ConsistencyError):
        decide_result_basis(facts | {"activity_state": 2}, ResultSetPhase.COMPLETE, evidence, None)


def test_existing_decision_cannot_be_changed_to_unsatisfied_or_unconfirmed():
    facts = _activity() | {"completion_basis": 2}
    for phase in (ResultSetPhase.UNSATISFIED, ResultSetPhase.UNCONFIRMED):
        with pytest.raises(ConsistencyError):
            decide_result_basis(facts, phase, {"method": "failure"}, {})
    assert decide_result_basis(facts, ResultSetPhase.BEGIN, None, None) == (2, None)


def test_unconfirmed_capture_initializes_only_pending_basis():
    facts = _activity()
    assert decide_result_basis(facts, ResultSetPhase.UNCONFIRMED, None, {}) == (1, None)
    assert decide_result_basis(facts, ResultSetPhase.UNCONFIRMED, None, None) == (None, None)
    assert decide_result_basis(facts | {"completion_basis": 1}, ResultSetPhase.UNCONFIRMED, None, {}) == (1, None)


def test_conclusion_requires_matching_state_and_result_code():
    values = {"result_set_state": 3, "result_check_json": {
        "contract": "result-v1", "outcome": 1, "observation": {}}}
    assert result_conclusion_error(1, 2, values) is None
    assert result_conclusion_error(1, 2, values | {"result_set_state": 4})
    assert result_conclusion_error(2, 2, values)
    assert result_conclusion_error(1, 3, values)
    assert result_conclusion_error(1, 2, values | {"result_check_json": None})

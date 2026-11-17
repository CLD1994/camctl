"""普通意图的流程关联在资源隔离环境中验证。"""

from types import SimpleNamespace

import pytest


@pytest.fixture
def guard():
    from camctl.persistence.repositories.operations import _attempt_intent_guard
    return _attempt_intent_guard


def intent(run_id):
    attempt = SimpleNamespace(table="operation_attempts", row_id=5,
        before=SimpleNamespace(exists=False), after=SimpleNamespace(values={
            "run_id": run_id, "attempt_no": 1, "intent_event_id": 21,
            "status": 1, "effect_state": 1, "result_event_id": None,
            "result_json": None, "error_json": None, "copy_round": None}))
    from camctl.contracts.enums import enum_for
    from camctl.persistence.repositories.file_batch_reads import FileBatchReads
    run = SimpleNamespace(table="operation_runs", row_id=3,
        before=SimpleNamespace(exists=False, values={}),
        after=SimpleNamespace(values={"kind": int(enum_for("operation_runs.kind").STOP),
                                      "action_id": 7, "attempts_used": 1}))
    context = SimpleNamespace(state_rows={"actions": {7: {"id": 7}}},
                              file_batch_reads=FileBatchReads(None))
    return SimpleNamespace(event_id=21, rows=(attempt, run)), context


def test_attempt_belongs_to_same_event_run(guard):
    event, context = intent(3)
    guard(event, context)


def test_attempt_cannot_reference_other_or_future_run(guard):
    event, context = intent(4)
    from camctl.history.validators import EventValidationError
    with pytest.raises(EventValidationError):
        guard(event, context)


@pytest.mark.parametrize("shape", ["no_attempt", "two_attempts", "two_runs"])
def test_intent_requires_one_attempt_and_one_run(guard, shape):
    event, context = intent(3)
    attempt, run = event.rows
    if shape == "no_attempt":
        event.rows = (run,)
    elif shape == "two_attempts":
        second = SimpleNamespace(table="operation_attempts", row_id=6,
            before=attempt.before, after=SimpleNamespace(values={**attempt.after.values, "attempt_no": 2}))
        event.rows = (attempt, second, run)
    else:
        second = SimpleNamespace(table="operation_runs", row_id=4,
                                 before=run.before, after=run.after)
        event.rows = (attempt, run, second)
    from camctl.history.validators import EventValidationError
    with pytest.raises(EventValidationError):
        guard(event, context)

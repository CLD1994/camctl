"""普通意图的流程关联在资源隔离环境中验证。"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def guard():
    names = (
        "camctl.capture.software_recovery", "camctl.contracts.enums",
        "camctl.contracts.history_values", "camctl.contracts.json_values",
        "camctl.contracts.values", "camctl.history.events", "camctl.history.validators",
        "camctl.history.reads", "camctl.operations.attempts", "camctl.operations.models",
        "camctl.operations.result_format", "camctl.persistence.models", "camctl.persistence.runtime",
        "camctl.persistence.row_history", "camctl.scheduling.resources", "camctl.persistence.transaction",
    )
    modules = {}
    for name in names:
        module = ModuleType(name)
        def get_attribute(attribute, module=module):
            if attribute.startswith("__"):
                raise AttributeError(attribute)
            value = MagicMock(name=attribute)
            setattr(module, attribute, value)
            return value
        module.__getattr__ = get_attribute
        modules[name] = module
    values = SimpleNamespace(START=1, READ_FILE=3, RESOLVE_OUTPUT_SCOPE=11,
                             CHECK_CAPTURE_RESULTS=7, PREPARE_CAPTURE=10, RUNNING=1,
                             UNKNOWN=1, STOP=2, STOP_RESIDUAL=8, CHECK_DEVICE_AVAILABILITY=12)
    modules["camctl.contracts.enums"].enum_for = lambda _: values
    modules["camctl.history.validators"].EventValidationError = ValueError
    path = Path(__file__).parents[3] / "src/camctl/persistence/repositories/operations.py"
    spec = importlib.util.spec_from_file_location("camctl.persistence.repositories._intent_test", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
        yield module._attempt_intent_guard


def intent(run_id):
    attempt = SimpleNamespace(table="operation_attempts", row_id=5,
        before=SimpleNamespace(exists=False), after=SimpleNamespace(values={
            "run_id": run_id, "attempt_no": 1, "intent_event_id": 21,
            "status": 1, "effect_state": 1, "result_event_id": None,
            "result_json": None, "error_json": None, "copy_round": None}))
    run = SimpleNamespace(table="operation_runs", row_id=3,
        before=SimpleNamespace(exists=False, values={}),
        after=SimpleNamespace(values={"kind": 2, "attempts_used": 1}))
    return SimpleNamespace(event_id=21, rows=(attempt, run)), SimpleNamespace(state_rows={})


def test_attempt_belongs_to_same_event_run(guard):
    event, context = intent(3)
    guard(event, context)


def test_attempt_cannot_reference_other_or_future_run(guard):
    event, context = intent(4)
    with pytest.raises(ValueError):
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
    with pytest.raises(ValueError):
        guard(event, context)

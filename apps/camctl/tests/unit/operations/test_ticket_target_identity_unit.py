"""票据目标规则不依赖持久化或包资源。"""

from importlib.util import find_spec, module_from_spec, spec_from_file_location
import sys
from types import ModuleType, SimpleNamespace

import pytest

from camctl import resources


@pytest.fixture
def attempts(monkeypatch):
    monkeypatch.setattr(resources, "resource_bytes", lambda name: pytest.fail("单元测试不能读取包资源"))
    persistence = ModuleType("camctl.persistence.models")
    persistence.DbOutcome = type("DbOutcome", (), {})
    persistence.DbOutcomeKind = SimpleNamespace(COMPLETED="completed", UNKNOWN="unknown", ROLLED_BACK="rolled_back")
    monkeypatch.setitem(sys.modules, "camctl.persistence.models", persistence)
    source = find_spec("camctl.operations.attempts")
    spec = spec_from_file_location("camctl.operations._isolated_ticket_rules", source.origin)
    module = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_check_ticket_uses_check_identity_for_intent_and_saved_result(attempts):
    target = attempts.AttemptTarget(availability_check_id=71)
    intent = attempts.AttemptIntent("availability", 1, attempts.OperationKind.CHECK_DEVICE_AVAILABILITY,
        target, None, attempts.AttemptConfig(2, "5", "1"), 10)
    assert attempts.ticket_target_id(intent) == "71"
    assert attempts.ticket_target_for_identity(intent.kind, target, None) == "71"
    assert attempts.responsibility_key(intent) == "availability/71"


@pytest.mark.parametrize("operation,timeout,interval,target", [
    ("query", "5", "1", {"availability_check_id": 71}),
    ("availability", None, "1", {"availability_check_id": 71}),
    ("availability", "5", None, {"availability_check_id": 71}),
    ("availability", "5", "1", {"activity_id": 71}),
    ("availability", "5", "1", {"availability_check_id": 71, "copy_id": 2}),
])
def test_check_intent_rejects_wrong_operation_budget_or_target(attempts, operation, timeout, interval, target):
    with pytest.raises(attempts.AttemptConfigError):
        attempts.AttemptIntent(operation, 1, attempts.OperationKind.CHECK_DEVICE_AVAILABILITY,
            attempts.AttemptTarget(**target), None, attempts.AttemptConfig(2, timeout, interval), 10)


@pytest.mark.parametrize("identity", [False, True, 1.0, "1", 0, -1, 9223372036854775808])
def test_check_target_rejects_invalid_identity(attempts, identity):
    with pytest.raises(attempts.AttemptConfigError):
        attempts.AttemptTarget(availability_check_id=identity)


@pytest.mark.parametrize("kind,target,purpose,expected", [
    ("START", {"activity_id": 4}, None, "4"),
    ("READ_FILE", {"copy_id": 5}, None, "5"),
    ("DELETE_FILE", {"cleanup_item_id": 6}, None, "6"),
    ("QUERY_ACTIVITY", {}, "BEFORE_EXECUTION", None),
    ("QUERY_ACTIVITY", {"activity_id": 7}, "START_CONFIRMATION", "7"),
])
def test_other_ticket_targets_keep_their_identity(attempts, kind, target, purpose, expected):
    purpose = None if purpose is None else attempts.QueryPurpose[purpose]
    assert attempts.ticket_target_for_identity(
        attempts.OperationKind[kind], attempts.AttemptTarget(**target), purpose) == expected

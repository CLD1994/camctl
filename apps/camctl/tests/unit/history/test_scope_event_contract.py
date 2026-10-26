"""内存登记下，范围责任仍须通过创建规格及完整前态的种类校验。

真实登记文件的兼容性由 integration/history/test_scope_event_registration.py
验证；本单元只验证正常校验入口，不读取包资源。
"""

from unittest.mock import create_autospec

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.history import events, validators
from camctl.history.events import EventEnvelope, RowChange, RowImage


@pytest.fixture
def registration(monkeypatch):
    registry = {"state_models": {}, "tables": {"operation_runs": {
        "immutable": ["kind"], "mutable": ["status"], "write_once": []}},
        "events": {"ATTEMPT_STARTED": {"id": 11, "version": 1, "branches": {
            "NEW": {"reason": 1, "evidence": [], "guards": [], "rows": [{
                "table": "operation_runs", "op": "create", "after": {"kind": [11]}}]}}},
            "OPERATION_CONFIGURED": {"id": 10, "version": 1, "branches": {
                "FINISH": {"reason": 3, "evidence": [], "guards": [], "rows": [{
                    "table": "operation_runs", "op": "update", "columns": ["status"],
                    "before": {"kind": [11], "status": [2]}, "after": {"status": [3]}}]}}}}}
    read = create_autospec(events.load_event_registry, return_value=registry)
    monkeypatch.setattr(events, "load_event_registry", read)
    monkeypatch.setattr(validators, "load_event_registry", read)
    monkeypatch.setattr(validators, "load_enum_registry", create_autospec(
        validators.load_enum_registry, return_value={"enums": {
            "operation_runs.kind": {"members": {"RESOLVE_OUTPUT_SCOPE": 11, "EMERGENCY_STOP": 9}},
            "operation_runs.status": {"members": {"ACTIVE": 2, "SUCCEEDED": 3}},
            "history_events.clock_status": {"members": {"UNCHECKED": 1}},
        }, "json_enums": {}}))
    monkeypatch.setattr(validators, "_history_objects", create_autospec(
        validators._history_objects, return_value={"action": {"id": 1}}))
    resource = create_autospec(events.resource_bytes,
        side_effect=AssertionError("内存登记单元不能读取真实包资源"))
    monkeypatch.setattr(events, "resource_bytes", resource)
    yield
    resource.assert_not_called()


@pytest.mark.parametrize("kind,accepted", [(11, True), (9, False)])
def test_scope_create_uses_the_declared_kind_filter(registration, kind, accepted):
    row = RowChange("operation_runs", 88, RowImage(False, {}),
        RowImage(True, {"kind": kind, "status": 2}))
    event = EventEnvelope(101, 7, 11, 1, 1, 1, None, 1, {}, (row,))
    if accepted:
        assert validators.validate_event_structure(event)[:2] == ("ATTEMPT_STARTED", "NEW")
    else:
        with pytest.raises(validators.EventValidationError):
            validators.validate_event_structure(event)


@pytest.mark.parametrize("kind,accepted", [(11, True), (9, False)])
def test_scope_finish_checks_kind_from_the_unchanged_context(registration, kind, accepted):
    row = RowChange("operation_runs", 88, RowImage(True, {"status": 2}),
        RowImage(True, {"status": 3}))
    event = EventEnvelope(101, 7, 10, 1, 1, 1, None, 3, {}, (row,))
    context = validators.EventContext(TransactionRange(7, 101, 101),
        {("operation_runs", 88): ("action", 43)},
        {"operation_runs": {88: {"kind": kind, "status": 2}}})
    if accepted:
        assert validators.validate_event(event, context).branch_name == "FINISH"
    else:
        with pytest.raises(validators.EventValidationError):
            validators.validate_event(event, context)

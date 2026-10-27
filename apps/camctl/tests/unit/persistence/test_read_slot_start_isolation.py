"""读取机会与同设备 START 互斥的隔离测试。"""

from enum import IntEnum
from importlib.util import find_spec, module_from_spec, spec_from_file_location
import sqlite3
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl import resources
from camctl.contracts import enums


class _ActionType(IntEnum):
    OBTAIN_ACTION_OUTPUTS = 1
    CAMERA_TAKE_PHOTO = 2
    CAMERA_RECORD = 3
    CAMERA_TIMELAPSE = 4


class _ActionStatus(IntEnum):
    RUNNING = 1
    SUCCEEDED = 2
    FAILED = 3
    EXPIRED = 4
    CANCELED = 5


class _RunKind(IntEnum):
    START = 1
    READ_FILE = 2
    PREPARE_CAPTURE = 3


class _RunStatus(IntEnum):
    ACTIVE = 1
    SUCCEEDED = 2
    FAILED = 3
    CANCELED = 4
    UNCONFIRMED = 5
    EXPIRED = 6


class _AttemptStatus(IntEnum):
    RUNNING = 1


class _ProcessingState(IntEnum):
    PENDING = 1
    RUNNING = 2


class _VerificationState(IntEnum):
    NOT_PERFORMED = 0
    MATCHED = 1
    MISMATCHED = 2
    SOURCE_CHECKSUM_UNAVAILABLE = 3
    FAILED = 4


class _InspectionState(IntEnum):
    PENDING = 1


class _RestrictionState(IntEnum):
    ACTIVE = 1
    IRREVERSIBLE = 2


class _FormatVersion(IntEnum):
    V2 = 2
    V4 = 4
    V5 = 5
    V6 = 6


class _FilePurpose(IntEnum):
    DELIVERY_COPY = 1
    RECORDING_INPUT = 2


class _DeliveryStatus(IntEnum):
    PUBLISHED = 1
    FAILED = 2
    CANCELED = 3
    WITHDRAWN = 4


_ENUMS = {
    "actions.type": _ActionType,
    "actions.status": _ActionStatus,
    "operation_runs.kind": _RunKind,
    "operation_runs.status": _RunStatus,
    "operation_attempts.status": _AttemptStatus,
    "recording_processing.check_state": _ProcessingState,
    "recording_processing.repair_state": _ProcessingState,
    "recording_processing.discard_state": _ProcessingState,
    "file_copies.verification_state": _VerificationState,
    "segment_inspections.state": _InspectionState,
    "cleanup_items.restriction_state": _RestrictionState,
    "database_metadata.format_version": _FormatVersion,
    "deliveries.status": _DeliveryStatus,
    "intermediate_files.purpose": _FilePurpose,
}


class _GuardContext:
    def __init__(self, state_rows, preparation_reads=None):
        self.state_rows = state_rows
        self.preparation_reads = preparation_reads

    def complete_rows(self, table, column, identity):
        assert (table, column, identity) == ("operation_attempts", "run_id", 31)
        return {}


def _cursor(row=None, rows=()):
    cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
    cursor.fetchone.return_value = row
    cursor.fetchall.return_value = list(rows)
    return cursor


@pytest.fixture
def production(monkeypatch):
    def forbidden_resource(name):
        raise AssertionError(f"单元测试不得读取包资源: {name}")

    monkeypatch.setattr(resources, "resource_bytes", forbidden_resource)
    monkeypatch.setattr(enums, "resource_bytes", forbidden_resource)
    monkeypatch.setattr(enums, "enum_for", lambda column: _ENUMS.get(column, SimpleNamespace()))
    monkeypatch.setattr(enums, "load_registry", lambda: {
        "enums": {}, "json_enums": {},
        "history_objects": {"runtime_state": {"id": 1}},
    })

    from camctl.contracts import workflow_errors
    from camctl.history import events
    import camctl.history as history_package
    import camctl.host_files as host_files_package
    import camctl.outputs as outputs_package
    import camctl.persistence.repositories as repositories_package

    class EventValidationError(ValueError):
        pass

    validators = ModuleType("camctl.history.validators")
    validators.EventContext = type("EventContext", (), {})
    validators.EventValidationError = EventValidationError
    validators.ValidatedEvent = type("ValidatedEvent", (), {})
    validators.register_guard = lambda *_: None
    validators.validate_event = lambda *_: None
    validators.validate_event_structure = lambda *_: None

    class UnusedPaths(ModuleType):
        PathRuleError = type("PathRuleError", (ValueError,), {})

        def __getattr__(self, name):
            def fail(*_args, **_kwargs):
                pytest.fail(f"不应调用路径协作者 {name}")
            return fail

    paths = UnusedPaths("camctl.host_files.paths")

    models = ModuleType("camctl.host_files.models")
    models.FilePurpose = _FilePurpose
    for name in ("BoundDirectories", "FileObservation", "FileObservationKind", "FileRef", "HostPath"):
        setattr(models, name, type(name, (), {}))

    operations = ModuleType("camctl.persistence.repositories.operations")
    for name in ("BeginAttemptCommand", "FinishAttemptCommand", "_FinishStaleRunsCommand"):
        setattr(operations, name, type(name, (), {}))

    obtain_summary = ModuleType("camctl.outputs.obtain_summary")
    for name in (
        "ObtainFacts", "ObtainAggregate", "ObtainItemStage", "ObtainPhase",
        "ObtainSourceFacts",
    ):
        setattr(obtain_summary, name, type(name, (), {}))

    def unused_summary(*_args, **_kwargs):
        raise AssertionError("slot 测试不应调用取回汇总")

    obtain_summary.decide_obtain_finish = unused_summary
    obtain_summary.obtain_item_stage = unused_summary

    existing_modules = set(sys.modules)
    replaced = {
        (history_package, "validators"): getattr(history_package, "validators", None),
        (host_files_package, "models"): getattr(host_files_package, "models", None),
        (host_files_package, "paths"): getattr(host_files_package, "paths", None),
        (outputs_package, "obtain_summary"): getattr(outputs_package, "obtain_summary", None),
        (repositories_package, "operations"): getattr(repositories_package, "operations", None),
    }
    monkeypatch.setitem(sys.modules, "camctl.history.validators", validators)
    monkeypatch.setitem(sys.modules, "camctl.host_files.models", models)
    monkeypatch.setitem(sys.modules, "camctl.host_files.paths", paths)
    monkeypatch.setitem(sys.modules, "camctl.outputs.obtain_summary", obtain_summary)
    monkeypatch.setitem(sys.modules, "camctl.persistence.repositories.operations", operations)

    monkeypatch.setattr(events, "resource_bytes", forbidden_resource)
    monkeypatch.setattr(events, "load_event_registry", lambda: {
        "format_version": 1, "events": {},
        "tables": {"file_copies": {
            "immutable": [], "mutable": ["slot_device_id"], "write_once": [],
        }},
    })
    monkeypatch.setattr(events, "reason_for_branch", lambda *_: 1)
    monkeypatch.setattr(workflow_errors, "resource_bytes", forbidden_resource)
    monkeypatch.setattr(workflow_errors, "action_error_id", lambda *_: 1)
    monkeypatch.setattr(workflow_errors, "item_error_id", lambda *_: 1)
    monkeypatch.setattr(workflow_errors, "registered_error", lambda *_: {})
    monkeypatch.setattr(workflow_errors, "registered_error_spec", lambda *_: ("test", {}))
    monkeypatch.setattr(workflow_errors, "validate_error_details", lambda *_: None)

    from camctl.scheduling import resources as scheduling_resources
    monkeypatch.setattr(
        scheduling_resources,
        "enum_for",
        lambda column: _ENUMS.get(column, SimpleNamespace()),
    )
    source = find_spec("camctl")
    assert source is not None and source.origin is not None
    module_name = "camctl.persistence.repositories._isolated_read_slot_tests"
    spec = spec_from_file_location(
        module_name,
        source.origin.replace("__init__.py", "persistence/repositories/outputs.py"),
    )
    assert spec is not None and spec.loader is not None
    outputs = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, outputs)
    try:
        spec.loader.exec_module(outputs)
        preparation = __import__(
            "camctl.persistence.repositories.preparation", fromlist=["PreparationReads"]
        )
        yield SimpleNamespace(
            outputs=outputs,
            scheduling=scheduling_resources,
            preparation=preparation,
            validation_error=EventValidationError,
        )
    finally:
        for (package, name), previous in replaced.items():
            if previous is None:
                vars(package).pop(name, None)
            else:
                setattr(package, name, previous)
        for name in tuple(sys.modules):
            if name.startswith("camctl.") and name not in existing_modules:
                module = sys.modules.pop(name)
                parent_name, _, child_name = name.rpartition(".")
                parent = sys.modules.get(parent_name)
                if parent is not None and vars(parent).get(child_name) is module:
                    vars(parent).pop(child_name)


def _read_command(outputs, *, slot=None):
    request = outputs.SlotRequest(copy_id=11, occurred_at=1)
    return outputs._SlotChangeCommand(request, "grant", "a" * 32)


def _grant_inputs(outputs, connection, *, slot=None):
    scope = SimpleNamespace(connection=connection)
    copy = {"id": 11, "slot_device_id": slot}
    run = {
        "id": 31, "status": int(outputs._RUN_STATUS.ACTIVE),
        "retry_wait_required": 0,
    }
    action = {
        "status": int(outputs._ACTION_STATUS.RUNNING), "cancel_requested": 0,
        "scheduled_at": 0,
    }
    return scope, copy, run, action


def test_new_camera_slot_waits_for_in_flight_start_without_event(monkeypatch, production):
    outputs = production.outputs
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    connection.execute.return_value = _cursor((1,))
    monkeypatch.setattr(outputs._SlotChangeCommand, "_device_holder", lambda *_: None)
    monkeypatch.setattr(production.scheduling, "current_preparation_holder", lambda *_: None)
    monkeypatch.setattr(
        outputs._SlotChangeCommand, "_first_candidate",
        lambda *_args, **_kwargs: pytest.fail("在途 START 必须先阻止授予"),
    )
    scope, copy, run, action = _grant_inputs(outputs, connection)

    plan = _read_command(outputs)._grant(scope, copy, "camera-a", run, action)

    assert plan.result == outputs.SlotDecision(outputs.SlotOutcome.WAIT, "device_busy")
    assert plan.read_only and plan.events == ()
    sql, parameters = connection.execute.call_args.args
    assert "a.device_id=?" in sql and parameters == (int(outputs._RUN_KIND.START), "camera-a")


def test_existing_slot_stays_held_while_start_is_in_flight(monkeypatch, production):
    outputs = production.outputs
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    monkeypatch.setattr(outputs._SlotChangeCommand, "_device_holder", lambda *_: 11)
    scope, copy, run, action = _grant_inputs(outputs, connection, slot="camera-a")

    plan = _read_command(outputs)._grant(scope, copy, "camera-a", run, action)

    assert plan.result.outcome is outputs.SlotOutcome.HELD
    assert plan.events == ()
    connection.execute.assert_not_called()


def _slot_event(outputs, before, after):
    row = SimpleNamespace(
        table="file_copies", row_id=11,
        before=SimpleNamespace(exists=True, values={"slot_device_id": before}),
        after=SimpleNamespace(exists=True, values={"slot_device_id": after}),
    )
    return SimpleNamespace(
        event_type=outputs._COPY_CHANGED_EVENT, reason=outputs._SLOT_REASON,
        occurred_at=1, rows=(row,),
    )


def _guard_context(outputs, *, slot=None, source_device_file_id=41, reads=None):
    return _GuardContext({
        "file_copies": {11: {
            "id": 11, "slot_device_id": slot,
            "source_device_file_id": source_device_file_id,
            "source_intermediate_file_id": None, "delivery_id": 51,
            "processing_id": None, "inspection_id": None,
            "committed_bytes": 8, "source_size": 8,
        }},
        "device_files": {41: {"observer_action_id": 22, "source_action_id": 23}},
        "actions": {
            21: {"id": 21, "status": int(outputs._ACTION_STATUS.RUNNING),
                "cancel_requested": 0, "scheduled_at": 0},
            22: {"device_id": "camera-a", "driver_id": "driver-a"},
            23: {"device_id": "camera-a", "driver_id": "driver-a"},
        },
        "deliveries": {51: {"action_id": 21}},
        "operation_runs": {31: {
            "id": 31, "copy_id": 11, "kind": int(outputs._RUN_KIND.READ_FILE),
            "status": int(outputs._RUN_STATUS.ACTIVE), "retry_wait_required": 0,
        }},
    }, reads)


def test_slot_guard_reads_start_state_and_rejects_only_in_flight_start(production):
    outputs = production.outputs
    for start_result, rejected in ((None, False), ((1,), True)):
        connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
        connection.execute.return_value = _cursor(start_result)
        reads = production.preparation.PreparationReads(connection)
        context = _guard_context(outputs, reads=reads)
        event = _slot_event(outputs, None, "camera-a")

        if rejected:
            with pytest.raises(production.validation_error):
                outputs._read_slot_guard(event, context)
        else:
            outputs._read_slot_guard(event, context)

        sql, parameters = connection.execute.call_args.args
        assert "a.device_id=?" in sql
        assert parameters == (int(outputs._RUN_KIND.START), "camera-a")


def test_host_source_never_gets_a_camera_slot(production):
    outputs = production.outputs
    context = _guard_context(outputs, source_device_file_id=None)

    with pytest.raises(production.validation_error, match="主机源拷贝"):
        outputs._read_slot_guard(_slot_event(outputs, None, "camera-a"), context)


def test_slot_guard_requires_kernel_reader_for_a_new_grant(production):
    outputs = production.outputs

    with pytest.raises(production.validation_error):
        outputs._read_slot_guard(
            _slot_event(outputs, None, "camera-a"), _guard_context(outputs)
        )


def test_releasing_existing_slot_is_not_blocked_by_start(production):
    outputs = production.outputs
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    reads = production.preparation.PreparationReads(connection)
    context = _guard_context(outputs, slot="camera-a", reads=reads)

    outputs._read_slot_guard(_slot_event(outputs, "camera-a", None), context)

    connection.execute.assert_not_called()

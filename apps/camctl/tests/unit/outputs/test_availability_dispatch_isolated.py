"""实际读取派发只使用内存替身核对尚未收场的设备检查。"""

from importlib.util import find_spec, module_from_spec, spec_from_file_location
import sys
from types import ModuleType, SimpleNamespace

import pytest

from camctl import resources
from camctl.contracts import enums


@pytest.fixture
def read_module(monkeypatch):
    monkeypatch.setattr(resources, "resource_bytes", lambda *_: pytest.fail("不能读取包资源"))
    monkeypatch.setattr(enums, "resource_bytes", lambda *_: pytest.fail("不能读取包资源"))
    enums_by_column = {"operation_runs.status": SimpleNamespace(PENDING=1, ACTIVE=2),
                       "operation_runs.kind": SimpleNamespace(READ_FILE=3),
                       "operation_attempts.status": SimpleNamespace(RUNNING=1)}
    monkeypatch.setattr(enums, "enum_for", lambda name: enums_by_column[name])

    def stub(name, names):
        module = ModuleType(name)
        for member in names:
            setattr(module, member, type(member, (), {}))
        monkeypatch.setitem(sys.modules, name, module)
        return module

    stub("camctl.capture.recovery", ("RecoveryBoundary", "RecoveryBlockedReason", "RecoveryDiagnostic"))
    stub("camctl.operations.attempts", ("AttemptFinish", "BeginDisposition", "FinishDisposition",
         "ReadResumeDisposition", "ReadResumeRequest", "RunFinish", "RunOutcome"))
    stub("camctl.operations.models", ("AttemptStatus", "AttemptTicket", "CallOutcome", "EffectState", "ErrorValue",
         "EvidenceValue", "Settlement", "SettlementBasis"))
    validation = stub("camctl.operations.validation", ("validate_outcome",))
    validation.validate_outcome = lambda *args: None
    diagnostics = stub("camctl.operations.error_diagnostics", ("exception_diagnostic",))
    diagnostics.exception_diagnostic = lambda *args: None
    stub("camctl.persistence.models", ("DbOutcomeKind", "db_failure", "db_outcome_for"))
    stub("camctl.persistence.business", ("SharedSave", "settle_required"))
    stub("camctl.outputs.database_reads", ("read_one",))
    stub("camctl.logging_runtime.diagnostics", ("DiagnosticState", "diagnostic_state_for", "record_call_outcome", "record_error"))
    outputs = stub("camctl.persistence.repositories.outputs", ("_read_owner_eligible",))
    outputs._read_owner_eligible = lambda action: True
    discovery = stub("camctl.outputs.read_discovery", ("read_copy_dispatch_eligible",))
    discovery.read_copy_dispatch_eligible = lambda *args: True
    definitions = stub("camctl.devices.drivers.hasselblad.definitions", ("DRIVER_ID",))
    definitions.DRIVER_ID = "hasselblad_cfv100c"
    facts = stub("camctl.persistence.transaction", ("row_facts",))
    facts.row_facts = lambda connection, table, identity: connection.rows[(table, identity)]
    preparation = stub("camctl.persistence.repositories.preparation", ("PreparationReads",))
    preparation.PreparationReads = lambda connection: SimpleNamespace(
        availability_in_flight=lambda device, identity: connection.check_running,
        capture_holds_device=lambda device: getattr(connection, "capture_held", False))
    scheduling = stub("camctl.scheduling.resources", ("current_preparation_holder",))
    scheduling.current_preparation_holder = lambda connection, device: None
    source = find_spec("camctl.outputs.read_attempts")
    spec = spec_from_file_location("camctl.outputs._isolated_availability_dispatch", source.origin)
    module = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


class _Cursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row

    def close(self):
        pass


class _Connection:
    def __init__(self, check_running):
        self.check_running = check_running
        self.rows = {("device_files", 9): {"observer_action_id": 10, "source_action_id": 11},
                     ("actions", 10): {"device_id": "camera", "driver_id": "other"},
                     ("actions", 11): {"device_id": "camera", "driver_id": "other"}}

    def execute(self, sql, args=()):
        if "history_events" in sql:
            return _Cursor((99,))
        return _Cursor((2, 0, 2, 1, 1, None, 9, None, "camera", 17, 1, 1, 3, 50, None))


@pytest.mark.parametrize("check_running", [False, True])
def test_actual_device_read_is_blocked_only_by_running_check(read_module, check_running):
    connection = _Connection(check_running)
    ticket = SimpleNamespace(operation="read", attempt_id=1, run_id=3, target_id="2", responsibility_key="read/2")
    result = read_module.read_dispatch_qualification(SimpleNamespace(connection=connection), ticket, 9, device_source=True)
    assert (None if result is None else result.reason) == ("device_busy" if check_running else None)


@pytest.mark.parametrize("never_dispatched", [False, True])
def test_only_original_unentered_read_is_excluded_for_capture(read_module, monkeypatch, never_dispatched):
    connection = _Connection(False)
    for action in (10, 11):
        connection.rows[("actions", action)]["driver_id"] = "hasselblad_cfv100c"
    alias = ModuleType("camctl.persistence.repositories.physical_alias")
    alias.source_physical_alias_conflict = lambda *args: False
    alias.source_physical_identity = lambda *args: "identity"
    selection = ModuleType("camctl.persistence.repositories.capture_selection")
    checked = []
    def priority(*args, **kwargs):
        checked.append(kwargs["exclude_read_attempt_id"])
        return SimpleNamespace(action_id=42)
    selection.read_capture_priority = priority
    monkeypatch.setitem(sys.modules, alias.__name__, alias)
    monkeypatch.setitem(sys.modules, selection.__name__, selection)
    ticket = SimpleNamespace(operation="read", attempt_id=1, run_id=3,
                             target_id="2", responsibility_key="read/2")
    result = read_module.read_dispatch_qualification(SimpleNamespace(connection=connection), ticket, 9,
        device_source=True, capture_selection=SimpleNamespace(trusted_wall_now=10),
        never_dispatched=never_dispatched)
    assert (None if result is None else result.reason) == ("capture_priority" if never_dispatched else None)
    assert checked == ([17] if never_dispatched else [])


def test_dispatch_rejects_slot_from_a_different_source_binding(read_module):
    connection = _Connection(False)
    connection.rows[("actions", 10)]["device_id"] = "other"
    connection.rows[("actions", 11)]["device_id"] = "other"
    ticket = SimpleNamespace(operation="read", attempt_id=1, run_id=3,
                             target_id="2", responsibility_key="read/2")
    with pytest.raises(read_module.ConsistencyError, match="原来源绑定"):
        read_module.read_dispatch_qualification(SimpleNamespace(connection=connection), ticket, 9,
                                                device_source=True, never_dispatched=True)


def test_dispatch_rejects_wrong_read_responsibility(read_module):
    ticket = SimpleNamespace(operation="read", attempt_id=1, run_id=3,
                             target_id="2", responsibility_key="read/other")
    with pytest.raises(read_module.ConsistencyError):
        read_module.read_dispatch_qualification(SimpleNamespace(connection=_Connection(False)), ticket, 9,
                                                device_source=True)


def test_prepared_capture_blocks_only_an_unentered_read(read_module, monkeypatch):
    connection = _Connection(False)
    connection.capture_held = True
    for action in (10, 11):
        connection.rows[("actions", action)]["driver_id"] = "hasselblad_cfv100c"
    alias = ModuleType("camctl.persistence.repositories.physical_alias")
    alias.source_physical_alias_conflict = lambda *args: False
    alias.source_physical_identity = lambda *args: "identity"
    selection = ModuleType("camctl.persistence.repositories.capture_selection")
    selection.read_capture_priority = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, alias.__name__, alias)
    monkeypatch.setitem(sys.modules, selection.__name__, selection)
    ticket = SimpleNamespace(operation="read", attempt_id=1, run_id=3,
                             target_id="2", responsibility_key="read/2")
    current = SimpleNamespace(trusted_wall_now=10)
    unentered = read_module.read_dispatch_qualification(SimpleNamespace(connection=connection), ticket, 9,
        device_source=True, capture_selection=current, never_dispatched=True)
    assert unentered.reason == "capture_priority"
    assert read_module.read_dispatch_qualification(SimpleNamespace(connection=connection), ticket, 9,
        device_source=True, capture_selection=current, never_dispatched=False) is None

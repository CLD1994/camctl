"""START 授予和取消共用原入口许可与原授予键。"""

import ast
import asyncio
from importlib.util import find_spec, module_from_spec, spec_from_file_location
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

from camctl import resources


@pytest.fixture
def entries(monkeypatch):
    monkeypatch.setattr(resources, "resource_bytes", lambda name: pytest.fail("不能读取包资源"))
    values = ModuleType("camctl.contracts.values")
    values.ConsistencyError, values.ObjectId = ValueError, int
    evidence = ModuleType("camctl.devices.evidence")
    evidence.DeviceObservation = lambda *args, **kwargs: None
    evidence.EvidenceContract = lambda *args, **kwargs: SimpleNamespace(type=args[0], version=args[1])
    models = ModuleType("camctl.operations.models")
    models.AttemptTicket = type("AttemptTicket", (), {})
    for module in (values, evidence, models):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    source = find_spec("camctl.devices.start_entry")
    spec = spec_from_file_location("camctl.devices._isolated_start_entry", source.origin)
    module = module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module, models.AttemptTicket


@pytest.mark.parametrize("phase", ["GRANTING", "UNKNOWN"])
def test_cancel_during_grant_waits_for_original_ticket(entries, phase):
    module, ticket_type = entries
    coordinator = module.StartEntryCoordinator()
    permit = coordinator.prepare("db", 1)
    permit.phase = module.EntryPhase[phase]
    ticket = ticket_type()
    ticket.operation, ticket.responsibility_key = "control", "start/1"

    assert coordinator.reserve("db", 1, no_intent=True) is None
    assert permit.cancel_pending
    permit.bind("db", ticket)
    assert permit.phase is module.EntryPhase.READY
    assert coordinator.reserve("db", 1, no_intent=False) is permit
    assert permit.ticket is ticket and permit.phase is module.EntryPhase.RESERVED


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
async def test_cancel_waits_for_grant_and_unknown_receipt_reuses_key(entries, unknown):
    module, ticket_type = entries
    path = Path(__file__).parents[3] / "src/camctl/capture/handlers.py"
    tree = ast.parse(path.read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "CaptureRuntime")
    method = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "grant")
    program = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                               method], type_ignores=[])

    class SharedSave:
        def __init__(self): self.task = None
        async def run(self, callback):
            if self.task is None:
                self.task = asyncio.create_task(callback())
            return await asyncio.shield(self.task)
        def retry_failed(self):
            if self.task is not None and self.task.done() and self.task.exception():
                self.task = None

    ticket = ticket_type()
    ticket.operation, ticket.responsibility_key = "control", "start/1"
    success = SimpleNamespace(kind="completed", value=SimpleNamespace(
        outcome=SimpleNamespace(value="granted"), ticket=ticket))
    receipts = ([SimpleNamespace(kind="unknown", error=RuntimeError()), success] if unknown else [success])
    started, release = asyncio.Event(), asyncio.Event()
    seen = []
    async def write(callback, *, key, description):
        seen.append((key, callback("owned")))
        started.set()
        if not unknown:
            await release.wait()
        return receipts.pop(0)
    namespace = {"GrantRequest": lambda **kwargs: kwargs, "AttemptConfig": lambda **kwargs: kwargs,
        "_ACTION_TYPE": SimpleNamespace(CAMERA_RECORD=2), "_start_call_timeout": lambda action: 3,
        "is_hasselblad_photo": lambda action: True, "EntryPhase": module.EntryPhase,
        "new_operation_key": lambda: "original-key", "SharedSave": SharedSave,
        "DbOutcomeKind": SimpleNamespace(COMPLETED="completed"), "db_outcome_for": lambda error: None,
        "db_failure": lambda kind, message, outcome: RuntimeError(message), "ConsistencyError": ValueError}
    exec(compile(ast.fix_missing_locations(program), str(path), "exec"), namespace)
    coordinator = module.StartEntryCoordinator()
    runtime = SimpleNamespace(database=SimpleNamespace(metadata=SimpleNamespace(instance_id="db"), write=write),
        start_entries=coordinator, window_of=lambda action: "window", wall_us=lambda: 100,
        fixed_camera_identity="camera", scheduling=SimpleNamespace(grant_start=lambda request, key, owned: request))
    action = {"id": 1, "device_id": "device", "type": 1}

    task = asyncio.create_task(namespace["grant"](runtime, action))
    await started.wait()
    permit = coordinator.get("db", 1)
    if unknown:
        with pytest.raises(RuntimeError):
            await task
        assert permit.phase is module.EntryPhase.UNKNOWN
    else:
        assert permit.phase is module.EntryPhase.GRANTING
    assert coordinator.reserve("db", 1, no_intent=True) is None
    if unknown:
        assert await coordinator.pending_grants[("db", 1)]() == (ticket, None)
        assert seen[0] == seen[1]
    else:
        release.set()
        assert await task == (ticket, None)
    assert permit.ticket is ticket and permit.cancel_pending


@pytest.mark.asyncio
async def test_cancel_selection_uses_unentered_fact_while_grant_is_pending(entries, monkeypatch):
    module, _ = entries
    monkeypatch.setitem(sys.modules, "camctl.devices.start_entry", module)
    path = Path(__file__).parents[3] / "src/camctl/bootstrap/flows.py"
    tree = ast.parse(path.read_text())
    method = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_resolve_and_fix")
    program = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                               method], type_ignores=[])
    loaded = SimpleNamespace(action={"id": 1, "status": 2, "cancel_requested": 0}, facts="snapshot")
    phases = []
    rules = ModuleType("camctl.cancellation.rules")
    def current_facts(value, phase):
        phases.append(phase)
        return SimpleNamespace(terminal=False)
    rules.current_eligibility_facts = current_facts
    rules.decide_cancel_eligibility = lambda facts: True
    rules.load_eligibility_inputs = lambda connection, identity, motor_permit=None: loaded
    rules.may_link_auto_preview = lambda action, result: result
    models = ModuleType("camctl.cancellation.models")
    models.CancelTargetsDisposition = SimpleNamespace(SAVED="saved")
    models.FixCancelTargets = object
    models.ResolvedTargets = lambda **kwargs: kwargs
    models.TargetFacts = lambda **kwargs: SimpleNamespace(**kwargs)
    models.CancelTargetError = type("CancelTargetError", (), {})
    targets = ModuleType("camctl.cancellation.targets")
    targets.missing_target_error = lambda target: None
    targets.prepare_cancel_set = lambda *args: SimpleNamespace(targets=(SimpleNamespace(action_id=1),))
    targets.resolve_cancel_target = lambda *args: SimpleNamespace(lookup_failed=None, missing=False, action_ids=(1,))
    repositories = ModuleType("camctl.persistence.repositories.cancellation")
    repositories.SqliteCancelLookup = lambda connection: object()
    repositories.sqlite_auto_candidates = lambda *args: ()
    facts_module = ModuleType("camctl.persistence.repositories.capture_facts")
    facts_module.load_start_facts = lambda *args: SimpleNamespace(attempts_used=1)
    for item in (rules, models, targets, repositories, facts_module):
        monkeypatch.setitem(sys.modules, item.__name__, item)
    monkeypatch.setattr(module, "is_hasselblad_photo", lambda action: True)
    namespace = {"_cancel_target_from_input": lambda spec: spec}
    exec(compile(ast.fix_missing_locations(program), str(path), "exec"), namespace)
    coordinator = module.StartEntryCoordinator()
    permit = coordinator.prepare("db", 1)
    permit.phase = module.EntryPhase.GRANTING
    async def read(callback):
        return callback(SimpleNamespace(connection=object()))
    database = SimpleNamespace(read=read, metadata=SimpleNamespace(instance_id="db"))

    result = await namespace["_resolve_and_fix"](database, object(), 8, "target", lambda: 100, {}, coordinator)

    assert result is None
    assert phases == [module.EntryPhase.READY]
    assert permit.cancel_pending and permit.ticket is None

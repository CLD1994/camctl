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

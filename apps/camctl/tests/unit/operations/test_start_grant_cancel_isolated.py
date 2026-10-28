"""START 授予和取消共用原入口许可与原授予键。"""

import ast
import asyncio
from enum import Enum
from importlib.util import find_spec, module_from_spec, spec_from_file_location
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from camctl import resources


def test_abandoned_disposition_belongs_to_target_fixing():
    path = Path(__file__).parents[3] / "src/camctl/cancellation/models.py"
    tree = ast.parse(path.read_text())
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name in ("CancelStartDisposition", "CancelTargetsDisposition")]
    namespace = {"Enum": Enum}
    exec(compile(ast.fix_missing_locations(ast.Module(body=classes, type_ignores=[])), str(path), "exec"), namespace)
    assert namespace["CancelTargetsDisposition"].ABANDONED.value == "abandoned"
    assert not hasattr(namespace["CancelStartDisposition"], "ABANDONED")


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

    assert coordinator.reserve("db", 1, no_intent=True, origin_action_id=8) is None
    assert permit.cancel_origins == {8}
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
    assert coordinator.reserve("db", 1, no_intent=True, origin_action_id=8) is None
    if unknown:
        assert await coordinator.pending_grants[("db", 1)]() == (ticket, None)
        assert seen[0] == seen[1]
    else:
        release.set()
        assert await task == (ticket, None)
    assert permit.ticket is ticket and permit.cancel_origins == {8}
    count = len(seen)
    assert await namespace["grant"](runtime, action) == (ticket, None)
    assert len(seen) == count


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["GRANTING", "RESERVED"])
async def test_cancel_selection_uses_unentered_fact_while_grant_is_pending(entries, monkeypatch, phase):
    module, ticket_type = entries
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
    if phase == "RESERVED":
        ticket = ticket_type()
        ticket.operation, ticket.responsibility_key = "control", "start/1"
        permit.bind("db", ticket)
        permit.cancel_origins.add(9)
    permit.phase = module.EntryPhase[phase]
    async def read(callback):
        return callback(SimpleNamespace(connection=object()))
    database = SimpleNamespace(read=read, metadata=SimpleNamespace(instance_id="db"))

    result = await namespace["_resolve_and_fix"](database, object(), 8, "target", lambda: 100, {}, coordinator)

    assert result is None
    assert phases == [module.EntryPhase.READY]
    assert permit.cancel_origins == ({8} if phase == "GRANTING" else {8, 9})
    assert (permit.ticket is None) == (phase == "GRANTING")


@pytest.mark.asyncio
async def test_cancel_origin_refresh_clears_only_finished_origins(entries):
    module, _ = entries
    path = Path(__file__).parents[3] / "src/camctl/capture/handlers.py"
    tree = ast.parse(path.read_text())
    method = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                  and node.name == "_refresh_photo_cancel_origins")
    program = ast.Module(body=[method], type_ignores=[])
    namespace = {"ConsistencyError": ValueError}
    exec(compile(ast.fix_missing_locations(program), str(path), "exec"), namespace)
    permit = module.StartEntryCoordinator().prepare("db", 1)
    permit.cancel_origins.update((8, 9))
    facts = {8: (6, 6, 1, 1), 9: (6, 2, 0, 1)}
    class Connection:
        def execute(self, sql, args):
            return SimpleNamespace(fetchone=lambda: facts[args[0]], close=lambda: None)
    async def read(callback, **kwargs):
        value = callback(SimpleNamespace(connection=Connection()))
        permit.cancel_origins.add(10)
        return value
    from contextlib import closing
    namespace["closing"] = closing
    runtime = SimpleNamespace(database=SimpleNamespace(read=read))

    await namespace["_refresh_photo_cancel_origins"](runtime, permit)

    assert permit.cancel_origins == {9, 10}


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_photo_revisits_original_unentered_ticket(entries, monkeypatch, expired):
    module, ticket_type = entries
    monkeypatch.setitem(sys.modules, "camctl.devices.start_entry", module)
    path = Path(__file__).parents[3] / "src/camctl/capture/host_photo.py"
    tree = ast.parse(path.read_text())
    method = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "advance_host_photo")
    method.decorator_list = []
    program = ast.Module(body=[method], type_ignores=[])
    ticket = ticket_type()
    ticket.operation, ticket.responsibility_key = "control", "start/1"
    saved = SimpleNamespace(result_json=None, status=1, ticket=ticket)
    permit = module.StartEntryCoordinator().prepare("db", 1)
    permit.bind("db", ticket)
    control = AsyncMock(return_value=SimpleNamespace(phase="not_granted"))
    handlers = ModuleType("camctl.capture.handlers")
    for name in ("_baseline_before_control", "_conclude_activity", "_finish_canceled_capture",
                 "_handle_binding_failure", "_expire_unentered_photo"):
        setattr(handlers, name, AsyncMock())
    handlers._control_call = control
    windows = ModuleType("camctl.scheduling.rules")
    windows.WindowPhase = SimpleNamespace(BEFORE_START="before", AFTER_WINDOW="after", IN_WINDOW="inside")
    windows.window_phase = lambda *args: "after" if expired else "inside"
    monkeypatch.setitem(sys.modules, handlers.__name__, handlers)
    monkeypatch.setitem(sys.modules, windows.__name__, windows)
    saved_start = AsyncMock(return_value=saved)
    namespace = {"_ACTION_STATUS": SimpleNamespace(SUCCEEDED=3, FAILED=4, CANCELED=6, EXPIRED=5),
        "_ATTEMPT": SimpleNamespace(RUNNING=1), "_owned_repository": lambda *args: (None, None),
        "_saved_start": saved_start, "_prepare_files": AsyncMock(return_value=object())}
    exec(compile(ast.fix_missing_locations(program), str(path), "exec"), namespace)
    runtime = SimpleNamespace(action=AsyncMock(return_value={"status": 2, "cancel_requested": 0}),
        start_entries=SimpleNamespace(get=lambda *args: permit),
        database=SimpleNamespace(metadata=SimpleNamespace(instance_id="db")),
        window_of=lambda action: object(), wall_us=lambda: 100, recover_attempt=AsyncMock())

    await namespace["advance_host_photo"](runtime, 1)

    if expired:
        control.assert_not_awaited()
        handlers._expire_unentered_photo.assert_awaited_once_with(runtime, {"status": 2, "cancel_requested": 0}, ticket, permit)
    else:
        control.assert_awaited_once()
    runtime.recover_attempt.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("abandoned", [False, True])
async def test_fixed_request_preserves_or_releases_reserved_permit(entries, monkeypatch, abandoned):
    module, ticket_type = entries
    monkeypatch.setitem(sys.modules, "camctl.devices.start_entry", module)
    path = Path(__file__).parents[3] / "src/camctl/bootstrap/flows.py"
    tree = ast.parse(path.read_text())
    method = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_fix_cancel_request")
    program = ast.Module(body=[method], type_ignores=[])
    dispositions = SimpleNamespace(ABANDONED="abandoned", SAVED="saved")
    models = ModuleType("camctl.cancellation.models")
    models.CancelTargetsDisposition = dispositions
    monkeypatch.setitem(sys.modules, models.__name__, models)
    async def settle(awaitable): return await awaitable
    namespace = {"DbOutcomeKind": SimpleNamespace(COMPLETED="completed"), "settle_required": settle}
    exec(compile(ast.fix_missing_locations(program), str(path), "exec"), namespace)
    ticket = ticket_type()
    ticket.operation, ticket.responsibility_key = "control", "start/1"
    coordinator = module.StartEntryCoordinator()
    permit = coordinator.prepare("db", 1)
    permit.bind("db", ticket)
    permit.cancel_origins.update((8, 9))
    permit.phase = module.EntryPhase.RESERVED
    block = SimpleNamespace(database_id="db", action_id=1, ticket=ticket)
    request = SimpleNamespace(action_id=8, entry_blocks=(block,))
    coordinator.pending_cancels[8] = (request, "key")
    outcome = SimpleNamespace(kind="completed", value=SimpleNamespace(
        disposition=dispositions.ABANDONED if abandoned else dispositions.SAVED))
    repository = SimpleNamespace(fix_cancel_targets=lambda *args, **kwargs: outcome)
    async def write(callback, **kwargs): return callback("owned")

    assert await namespace["_fix_cancel_request"](SimpleNamespace(write=write), repository, request, "key", coordinator) is outcome
    assert permit.phase is (module.EntryPhase.READY if abandoned else module.EntryPhase.BLOCKED)
    assert permit.cancel_origins == ({9} if abandoned else {8, 9})
    assert 8 not in coordinator.pending_cancels


@pytest.mark.parametrize("status,canceled", [(2, 1), (6, 1)])
def test_canceled_origin_abandons_uncommitted_entry_transaction(entries, monkeypatch, status, canceled):
    module, _ = entries
    module.PREVENTED = object()
    monkeypatch.setitem(sys.modules, "camctl.devices.start_entry", module)
    path = Path(__file__).parents[3] / "src/camctl/persistence/repositories/cancellation.py"
    tree = ast.parse(path.read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "_FixPhotoEntriesCommand")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "plan")
    helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_cancel_origin_abandoned")
    program = ast.Module(body=[helper, method], type_ignores=[])
    module_map = {
        "camctl.devices.evidence": ("EvidenceRegistry",),
        "camctl.operations.models": ("CallOutcome", "AttemptStatus", "EffectState", "ErrorValue", "Settlement", "SettlementBasis", "EvidenceValue"),
        "camctl.operations.attempts": ("AttemptFinish",),
        "camctl.operations.validation": ("validate_outcome",),
        "camctl.persistence.repositories.capture": ("_CompositeScope", "_FinishStartResultCommand", "ActivityObservationSave", "_merged_state_rows"),
    }
    for name, names in module_map.items():
        fake = ModuleType(name)
        for symbol in names:
            setattr(fake, symbol, object())
        monkeypatch.setitem(sys.modules, name, fake)
    class Result:
        def __init__(self, disposition): self.disposition = disposition
    dispositions = SimpleNamespace(ABANDONED="abandoned")
    namespace = {"saved_transaction_events": lambda *args: None,
        "row_facts": lambda *args: {"type": 6, "status": status, "cancel_requested": canceled,
                                     "target_selection_state": 1},
        "_CANCEL_ACTION_TYPE": 6, "_TARGET_PENDING": 1,
        "CancelTargetsSaved": Result, "CancelTargetsDisposition": dispositions,
        "CommandPlan": lambda *args, **kwargs: SimpleNamespace(result=args[3], read_only=kwargs["read_only"])}
    exec(compile(ast.fix_missing_locations(program), str(path), "exec"), namespace)
    command = SimpleNamespace(command=SimpleNamespace(action_id=8), key="key")

    plan = namespace["plan"](command, SimpleNamespace(connection=object()))

    assert plan.read_only and plan.result.disposition == dispositions.ABANDONED


@pytest.mark.parametrize("saved", [False, True])
def test_plain_cancel_fix_checks_original_key_before_abandoning(saved):
    path = Path(__file__).parents[3] / "src/camctl/persistence/repositories/cancellation.py"
    tree = ast.parse(path.read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "_FixCancelTargetsCommand")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "plan")
    helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_cancel_origin_abandoned")
    program = ast.Module(body=[helper, method], type_ignores=[])
    class Result:
        def __init__(self, disposition): self.disposition = disposition
    dispositions = SimpleNamespace(ABANDONED="abandoned")
    namespace = {"saved_transaction_events": lambda *args: ["event"] if saved else None,
        "row_facts": lambda *args: {"type": 6, "status": 6, "cancel_requested": 1,
                                     "target_selection_state": 1},
        "_CANCEL_ACTION_TYPE": 6, "_TARGET_PENDING": 1, "ConsistencyError": ValueError,
        "CancelTargetsSaved": Result, "CancelTargetsDisposition": dispositions,
        "CommandPlan": lambda *args, **kwargs: SimpleNamespace(result=args[3], read_only=kwargs["read_only"])}
    exec(compile(ast.fix_missing_locations(program), str(path), "exec"), namespace)
    command = SimpleNamespace(_command=SimpleNamespace(action_id=8), _key="key", _reuse=lambda *args: "original")

    plan = namespace["plan"](command, SimpleNamespace(connection=object()))

    if saved:
        assert plan == "original"
    else:
        assert plan.read_only and plan.result.disposition == dispositions.ABANDONED


@pytest.mark.asyncio
async def test_expired_unentered_ticket_saves_prevented_result(entries, monkeypatch):
    module, ticket_type = entries
    monkeypatch.setitem(sys.modules, "camctl.devices.start_entry", module)
    path = Path(__file__).parents[3] / "src/camctl/capture/handlers.py"
    tree = ast.parse(path.read_text())
    method = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_expire_unentered_photo")
    program = ast.Module(body=[method], type_ignores=[])
    namespace = {"EntryPhase": module.EntryPhase, "ConsistencyError": ValueError,
        "AttemptStatus": SimpleNamespace(FAILED="failed"), "EffectState": SimpleNamespace(NO_EFFECT="none"),
        "ErrorValue": lambda *args: args, "SettlementBasis": SimpleNamespace(NOT_DISPATCHED="not-dispatched"),
        "EvidenceValue": SimpleNamespace(of=lambda value: value), "Settlement": lambda *args: args,
        "CallOutcome": lambda **kwargs: SimpleNamespace(**kwargs), "RunOutcome": SimpleNamespace(EXPIRED="expired"),
        "ActivityObservationSave": lambda *args, **kwargs: (args, kwargs),
        "_DISPATCH_STATE": SimpleNamespace(NOT_DISPATCHED=1), "ExpireActionRequest": lambda *args: args}
    exec(compile(ast.fix_missing_locations(program), str(path), "exec"), namespace)
    ticket = ticket_type()
    ticket.operation, ticket.responsibility_key = "control", "start/1"
    permit = module.StartEntryCoordinator().prepare("db", 1)
    permit.bind("db", ticket)
    runtime = SimpleNamespace(wall_us=lambda: 100, monotonic_ns=lambda: 10, finish=AsyncMock())

    await namespace["_expire_unentered_photo"](runtime, {"id": 1}, ticket, permit)

    assert permit.phase is module.EntryPhase.BLOCKED
    args, kwargs = runtime.finish.await_args
    assert args[0] is ticket and args[1].settlement[0] == "not-dispatched"
    assert kwargs["end_run"] == "expired" and kwargs["expiration"] == (1, 100, 100)

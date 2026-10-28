"""H1 公开分类和投影边界；不加载真实包资源或业务协作者。"""

import ast
import asyncio
import importlib.abc
import importlib.util
import json
import sys
import unittest
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


SOURCE = Path(__file__).parents[3] / "src" / "camctl"


class BlockCamctl(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "camctl" or fullname.startswith("camctl."):
            raise AssertionError(f"禁止加载真实包: {fullname}")
        return None


def load_source(name, path, dependencies):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {name: module, **dependencies}), patch.object(
            sys, "meta_path", [BlockCamctl(), *sys.meta_path]):
        spec.loader.exec_module(module)
    return module


class EvidenceError(ValueError):
    pass


@dataclass(frozen=True)
class Contract:
    type: str
    version: int
    operation: str
    fields: frozenset
    identity_field: str | None = None


class Registry:
    def __init__(self, contracts):
        self.contracts = tuple(contracts)

    def contract(self, kind, version):
        for contract in self.contracts:
            if (contract.type, contract.version) == (kind, version):
                return contract
        raise EvidenceError(kind)

    def with_contract(self, contract):
        return Registry((*self.contracts, contract))


def recovery_module():
    evidence = ModuleType("camctl.devices.evidence")
    evidence.EvidenceError = EvidenceError
    evidence.EvidenceRegistry = Registry
    return load_source("isolated_h1_recovery", SOURCE / "operations" / "recovery.py",
                       {evidence.__name__: evidence})


class RecoveryBoundaryTests(unittest.TestCase):
    def test_production_recovery_prerequisites_keep_original_boundary(self):
        original = recovery_module()
        capture = load_source("isolated_h1_capture_recovery", SOURCE / "capture" / "recovery.py",
                              {"camctl.operations.recovery": original})
        boundary, reason = original.RecoveryBoundary, original.RecoveryBlockedReason
        check = capture.recovery_block_reason
        self.assertEqual(check(boundary.UNCONFIRMED, 10, 9, evidence_lookup_available=True),
                         reason.UNCONFIRMED_BOUNDARY)
        self.assertEqual(check(boundary.HOST_LOCAL_SETTLED, None, 9, evidence_lookup_available=True),
                         reason.MISSING_HORIZON)
        self.assertEqual(check(boundary.HOST_LOCAL_SETTLED, 10, 9, evidence_lookup_available=False),
                         reason.MISSING_EVIDENCE_LOOKUP)
        self.assertEqual(check(boundary.HOST_LOCAL_SETTLED, 10, 11, evidence_lookup_available=True),
                         reason.INTENT_OUTSIDE_HORIZON)
        self.assertEqual(check(boundary.HOST_LOCAL_SETTLED, 10, None, evidence_lookup_available=True),
                         reason.INTENT_OUTSIDE_HORIZON)
        self.assertEqual(check(boundary.HOST_LOCAL_SETTLED, 10, 10, evidence_lookup_available=True,
                               active_local_control=True), reason.ACTIVE_LOCAL_CONTROL)
        self.assertEqual(check(boundary.HOST_LOCAL_SETTLED, 10, 10, evidence_lookup_available=True,
                               evidence_available=False), reason.EVIDENCE_UNAVAILABLE)
        self.assertIsNone(check(boundary.HOST_LOCAL_SETTLED, 10, 10, evidence_lookup_available=True))

    def test_original_result_and_host_boundary_are_distinct(self):
        recovery = recovery_module()
        facts = recovery.RecoveryFacts
        cases = (
            (facts(True, False, True), recovery.RecoveryClass.UNKNOWN_NEEDS_VERIFICATION, True),
            (facts(True, False, False), recovery.RecoveryClass.WAIT_HOST_SETTLEMENT, False),
            (facts(True, True, False), recovery.RecoveryClass.ALREADY_SETTLED, False),
            (facts(False, False, True), recovery.RecoveryClass.NO_INTENT_RECORDED, False),
        )
        for value, expected, save in cases:
            with self.subTest(value=value):
                decision = recovery.recover_attempt(value)
                self.assertEqual(decision.recovery, expected)
                self.assertFalse(decision.redispatch)
                self.assertEqual(decision.save_basis, save)
        self.assertNotEqual(recovery.RecoveryBoundary.UNCONFIRMED,
                            recovery.RecoveryBoundary.HOST_LOCAL_SETTLED)

    def test_control_recovery_requires_original_driver_declaration_and_contract(self):
        recovery = recovery_module()
        contract = Contract("local_control_recovery", 1, "control", frozenset())
        entry = SimpleNamespace(declaration=SimpleNamespace(local_control_recovery_supported=True),
                                evidence=Registry((contract,)))
        saved = recovery.recovery_registry(entry, "control")
        self.assertEqual(saved.contract("local_control_recovery", 1), contract)
        entry.declaration.local_control_recovery_supported = False
        self.assertIsNone(recovery.recovery_registry(entry, "control"))
        entry.declaration.local_control_recovery_supported = True
        entry.evidence = Registry(())
        self.assertIsNone(recovery.recovery_registry(entry, "control"))
        entry.evidence = Registry((Contract("local_control_recovery", 1, "control", frozenset({"x"})),))
        with self.assertRaises(ValueError):
            recovery.recovery_registry(entry, "control")


def release_functions():
    path = SOURCE / "persistence" / "repositories" / "capture_facts.py"
    tree = ast.parse(path.read_text())
    wanted = {"release_basis_holds", "output_scope_resolved", "occupancy_release_allowed"}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    code = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                            *nodes], type_ignores=[])
    namespace = {"Mapping": dict}
    exec(compile(ast.fix_missing_locations(code), str(path), "exec"), namespace)
    return namespace["occupancy_release_allowed"]


class ReleaseBoundaryTests(unittest.TestCase):
    def test_no_effect_requires_preparation_and_scope_resolution(self):
        allowed = release_functions()
        facts = {"activity_state": 1, "dispatch_state": 4, "completion_basis": None,
                 "occupancy_state": 1, "ownership_mode": 2, "baseline_state": 1}
        self.assertFalse(allowed(facts, unstarted=True, preparation_resolved=False))
        self.assertTrue(allowed(facts, unstarted=True, preparation_resolved=True))
        self.assertFalse(allowed(facts, unstarted=True, preparation_resolved=True,
                                 preparation_calls_resolved=False))

    def test_unknown_or_incomplete_output_scope_keeps_occupancy(self):
        allowed = release_functions()
        facts = {"activity_state": 1, "dispatch_state": 2, "completion_basis": None,
                 "occupancy_state": 1, "ownership_mode": 2, "baseline_state": 3,
                 "result_set_state": 1, "output_set_finalized_event_id": None}
        self.assertFalse(allowed(facts))
        facts["activity_state"] = 3
        self.assertFalse(allowed(facts))
        facts["output_set_finalized_event_id"] = 100
        self.assertTrue(allowed(facts))


def projection_module():
    modules = {}
    def dependency(name, **values):
        module = ModuleType(name)
        module.__dict__.update(values)
        modules[name] = module

    activity_unknown = {"op": "enum_is", "column": "device_activities.activity_state", "members": ["UNKNOWN"]}
    applicable = {"op": "all", "args": [
        {"op": "enum_is", "column": "actions.status", "members": ["FAILED"]},
        {"op": "exists", "table": "device_activities"},
        {"op": "enum_is", "column": "device_activities.dispatch_state", "members": ["MAY_HAVE_DISPATCHED"]},
        activity_unknown,
        {"op": "not", "arg": {"op": "enum_is", "column": "device_activities.completion_basis",
                                 "members": ["DEVICE_EVIDENCE", "TIME_AND_OUTPUTS"]}},
    ]}
    registry = {"format_version": 1,
        "relations": {"action_activity": {"from": "actions.id", "to": "device_activities.action_id",
                                           "cardinality": "optional_one"}},
        "projections": {
            "action_example": {"root_table": "actions", "relations": ["action_activity"], "fields": {
                "error": {"value": {"op": "read", "column": "actions.error_json", "encoding": "json"}},
                "device_execution": {"when": applicable, "value": {"op": "project", "projection": "execution_example"}},
            }},
            "execution_example": {"root_table": "actions", "relations": ["action_activity"], "fields": {
                "status": {"value": {"op": "cases", "branches": [
                    {"when": activity_unknown, "value": {"op": "literal", "value": "end_unconfirmed"}}],
                    "otherwise": {"op": "error", "code": "state_database_error"}}},
            }},
        }}
    enums = {"actions.status": {"FAILED": 4}, "device_activities.dispatch_state": {"MAY_HAVE_DISPATCHED": 2},
             "device_activities.activity_state": {"UNKNOWN": 1, "ENDED": 3},
             "device_activities.completion_basis": {"DEVICE_EVIDENCE": 2, "TIME_AND_OUTPUTS": 3}}
    dependency("camctl.resources", resource_bytes=lambda name: json.dumps(registry).encode())
    dependency("camctl.contracts.enums", load_registry=lambda: {
        "enums": {name: {"members": members} for name, members in enums.items()}, "json_enums": {}})
    dependency("camctl.contracts.json_values", MISSING=object(), JsonParseError=ValueError,
               is_json_integer=lambda value: type(value) is int, parse_exact_json=json.loads)
    dependency("camctl.contracts.input_fields", reconstruct_action_input=lambda value: value)
    dependency("camctl.contracts.values", ConsistencyError=ValueError, format_utc_micros=lambda value, **kwargs: value)
    dependency("camctl.contracts.schemas", SchemaRuleError=type("SchemaRuleError", (ValueError,), {}))
    dependency("camctl.contracts.workflow_errors", registered_error_spec=lambda *args: None,
               validate_public_error=lambda *args: None, validate_public_json=lambda *args: None)
    return load_source("isolated_h1_projection", SOURCE / "contracts" / "public_projection.py", modules)


class ReportProjectionTests(unittest.TestCase):
    def test_unknown_original_activity_keeps_original_error_when_occupancy_changes(self):
        projection = projection_module()
        error = {"code": "capture_result_unconfirmed", "stage": "capture", "details": {}}
        action = {"status": 4, "error_json": error}
        activity = {"action_id": 1, "dispatch_state": 2, "activity_state": 1,
                    "completion_basis": None, "occupancy_state": 1}
        facts = projection.ProjectionInput("action_example", 1,
            {"actions": {1: action}, "device_activities": {7: activity}})
        expected = {"error": error, "device_execution": {"status": "end_unconfirmed"}}
        self.assertEqual(projection.project_public(facts), expected)
        activity["occupancy_state"] = 2
        self.assertEqual(projection.project_public(facts), expected)
        activity["activity_state"] = 3
        self.assertEqual(projection.project_public(facts), {"error": error})


def shared_save_type():
    path = SOURCE / "persistence" / "business.py"
    tree = ast.parse(path.read_text())
    wanted = {"_settle", "_RequiredResult", "_complete_required", "_deliver_required", "SharedSave"}
    nodes = [node for node in tree.body
             if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted]
    code = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                            *nodes], type_ignores=[])
    namespace = {"asyncio": asyncio, "dataclass": dataclass, "DbExecutorError": RuntimeError,
                 "DbOutcome": type("DbOutcome", (), {})}
    exec(compile(ast.fix_missing_locations(code), str(path), "exec"), namespace)
    return namespace["SharedSave"]


def pending_result_type():
    path = SOURCE / "capture" / "handlers.py"
    tree = ast.parse(path.read_text())
    result = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PendingCallResult")
    code = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                            result], type_ignores=[])
    namespace = {"dataclass": dataclass, "field": field, "SharedSave": shared_save_type(),
                 "DiagnosticState": lambda: object(), "AttemptFinish": type("AttemptFinish", (), {})}
    exec(compile(ast.fix_missing_locations(code), str(path), "exec"), namespace)
    return namespace["PendingCallResult"]


class OriginalSaveTests(unittest.IsolatedAsyncioTestCase):
    async def test_derived_original_result_keeps_owner_ticket_and_key(self):
        pending_type = pending_result_type()
        ticket = SimpleNamespace(run_id=8, attempt_id=1, responsibility_key="start/21")
        finish = SimpleNamespace(ticket=ticket)
        original = pending_type("original-key", finish, None)
        prepared = replace(original, observation={"actual": "observed"})
        self.assertIs(prepared.shared, original.shared)
        self.assertIs(prepared.finish.ticket, ticket)
        self.assertEqual(prepared.key, "original-key")

    async def test_active_save_has_one_owner_and_original_key(self):
        shared = shared_save_type()()
        entered, release = asyncio.Event(), asyncio.Event()
        writes = []
        original = ("start/21", 8, 1, "original-key")
        async def save():
            writes.append(original)
            entered.set()
            await release.wait()
            return "saved"
        first = asyncio.create_task(shared.run(save))
        await entered.wait()
        second = asyncio.create_task(shared.run(save))
        await asyncio.sleep(0)
        self.assertEqual(writes, [original])
        release.set()
        self.assertEqual(await asyncio.gather(first, second), ["saved", "saved"])
        self.assertEqual(writes, [original])

    async def test_unknown_receipt_is_rechecked_with_original_key(self):
        shared = shared_save_type()()
        writes = []
        original = ("start/21", 8, 1, "original-key")
        async def save():
            writes.append(original)
            if len(writes) == 1:
                raise RuntimeError("receipt unknown")
            return "saved"
        with self.assertRaises(RuntimeError):
            await shared.run(save)
        with self.assertRaises(RuntimeError):
            await shared.run(save)
        self.assertEqual(writes, [original])
        shared.retry_failed()
        self.assertEqual(await shared.run(save), "saved")
        self.assertEqual(writes, [original, original])


if __name__ == "__main__":
    unittest.main()

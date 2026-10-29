"""可用性公开驱动入口，不加载包资源或真实工具协作者。"""

import ast
import asyncio
import importlib.abc
import sys
from contextlib import nullcontext
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import unittest


SOURCE = Path(__file__).parents[3] / "src" / "camctl" / "devices" / "drivers" / "hasselblad" / "driver.py"


@dataclass(frozen=True)
class RawToolOutcome:
    exit: object
    output: bytes | None
    stderr: bytes | None = b""
    error: str | None = None
    output_failure: object = None
    used_grace_s: Decimal | None = None
    signal_failures: tuple = ()


@dataclass(frozen=True)
class ToolSpec:
    argv: tuple
    timeout_s: Decimal
    terminate_grace_s: Decimal


class ToolCallCancelled(Exception):
    def __init__(self, outcome):
        self.outcome = outcome


class ToolStartError(Exception):
    pass


class DeviceCallResult:
    def __init__(self, observations, error, outcome, *, diagnostic_error=None):
        self.observations, self.error, self.outcome = observations, error, outcome
        self.diagnostic_error = diagnostic_error

    @staticmethod
    def _error_view(outcome):
        return outcome.error


def driver_type():
    tree = ast.parse(SOURCE.read_text())
    wanted = {"AvailabilityCall", "AvailabilityResult", "AvailabilityCancelled", "_NeverStop"}
    nodes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name in wanted]
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "HasselbladDriver")
    methods = {"__init__", "_binding_config", "check_availability", "_local_return_failure"}
    driver = ast.ClassDef(name=original.name, bases=[], keywords=[], decorator_list=[],
        body=[node for node in original.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
              and node.name in methods], type_params=[])
    driver.body.insert(0, ast.parse('driver_id = "hasselblad_cfv100c"').body[0])
    program = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                               *nodes, driver], type_ignores=[])
    matched = object()
    namespace = {"__name__": __name__, "dataclass": dataclass, "replace": replace, "Decimal": Decimal, "asyncio": asyncio,
        "Mapping": dict, "DRIVER_DECLARATION": object(), "PreparedCaptureCache": lambda: object(),
        "DeviceConfigurationError": ValueError, "BindingStatus": SimpleNamespace(MATCHED=matched),
        "check_binding": lambda binding, owner: SimpleNamespace(status=matched),
        "read_camera_config": lambda device: device, "ToolSpec": ToolSpec,
        "_NANOSECONDS_PER_SECOND": Decimal(1_000_000_000), "RawToolOutcome": RawToolOutcome,
        "diagnostic_monotonic_ns": lambda: 0,
        "ToolCallCancelled": ToolCallCancelled, "ToolStartError": ToolStartError,
        "current_call_scope": lambda: None, "log_context": lambda **kwargs: nullcontext(),
        "parse_devices": lambda output: [SimpleNamespace(vendor_id=0x2756, product_id=10)],
        "parse_serial": lambda output: output.decode(), "doctor_passed": lambda stdout, stderr: stdout == b"PASS" and stderr == b"",
        "CliParseError": type("CliParseError", (ValueError,), {}), "_VENDOR_ID": 0x2756,
        "parse_object_id": lambda value: int(value),
        "DeviceObservation": lambda kind, version, data: SimpleNamespace(type=kind, version=version, data=data),
        "_AVAILABILITY": SimpleNamespace(type="device_availability", version=1),
        "_AVAILABILITY_RETURNED": "returned", "_AVAILABILITY_NOT_DISPATCHED": "not_dispatched",
        "ToolStreamState": SimpleNamespace(UNAVAILABLE="unavailable", COMPLETE="complete", UNCONFIRMED="unconfirmed"),
        "ToolStream": lambda state, data: SimpleNamespace(state=state, data=data),
        "ToolCallOutput": lambda index, name, stdout, stderr: SimpleNamespace(
            index=index, name=name, stdout=stdout, stderr=stderr),
        "ToolDiagnostics": lambda limit, calls: SimpleNamespace(limit=limit, calls=calls),
        "ErrorValue": lambda code, stage, details: SimpleNamespace(code=code, stage=stage, details=details),
        "AttemptStatus": SimpleNamespace(SUCCEEDED="succeeded", FAILED="failed"),
        "EffectState": SimpleNamespace(NO_EFFECT="no_effect"),
        "SettlementBasis": SimpleNamespace(OBSERVED="observed", NOT_DISPATCHED="not_dispatched"),
        "EvidenceValue": SimpleNamespace(of=lambda contract: contract),
        "Settlement": lambda basis, evidence: SimpleNamespace(basis=basis, evidence=evidence),
        "CallOutcome": lambda **values: SimpleNamespace(**values), "DeviceCallResult": DeviceCallResult,
        "exception_diagnostic": lambda error: None}
    exec(compile(ast.fix_missing_locations(program), str(SOURCE), "exec"), namespace)
    return namespace["HasselbladDriver"], namespace["AvailabilityCancelled"]


class Transport:
    def __init__(self, clock, outcomes):
        self.clock, self.outcomes, self.calls = clock, list(outcomes), []

    async def run(self, spec, stop):
        self.calls.append(spec)
        self.clock[0] += 1_000_000_000
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def raw(output):
    return RawToolOutcome(SimpleNamespace(exit_code=0, signal=None), output)


class AvailabilityPortTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        class BlockCamctl(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "camctl" or fullname.startswith("camctl."):
                    raise AssertionError(f"禁止导入真实包: {fullname}")
                return None
        cls.blocker = BlockCamctl()
        sys.meta_path.insert(0, cls.blocker)

    @classmethod
    def tearDownClass(cls):
        sys.meta_path.remove(cls.blocker)

    def setup_driver(self, outcomes):
        driver, cancelled = driver_type()
        clock = [10_000_000_000]
        transport = Transport(clock, outcomes)
        config = SimpleNamespace(serial="SERIAL", product_id=10, executable="hb")
        instance = driver({"camera": config}, None, monotonic_ns=lambda: clock[0], transport=transport)
        binding = SimpleNamespace(device_id="camera", driver_id="hasselblad_cfv100c")
        return instance, binding, transport, cancelled

    async def test_success_uses_one_deadline_and_retains_each_actual_result(self):
        driver, binding, transport, _ = self.setup_driver([raw(b"list"), raw(b"SERIAL"), raw(b"PASS")])
        result = await driver.check_availability(binding, expected_serial="SERIAL", expected_product_id=10,
                                                 timeout_s=Decimal(5))
        self.assertTrue(result.available)
        self.assertEqual([item.argv[-1] for item in transport.calls], ["list", "info", "doctor"])
        self.assertEqual([item.timeout_s for item in transport.calls], [Decimal(5), Decimal(4), Decimal(3)])
        self.assertEqual([call.outcome.output for call in result.calls], [b"list", b"SERIAL", b"PASS"])
        self.assertEqual((result.started_monotonic_ns, result.finished_monotonic_ns),
                         (10_000_000_000, 13_000_000_000))
        actual = result.to_device_result("31").outcome
        self.assertEqual(actual.status, "succeeded")
        self.assertEqual(actual.observations[0].data["check_id"], "31")
        self.assertEqual([call.stdout.data for call in actual.diagnostics.calls], [b"list", b"SERIAL", b"PASS"])
        self.assertEqual([call.stderr.data for call in actual.diagnostics.calls], [b"", b"", b""])

    async def test_failed_doctor_preserves_original_results(self):
        driver, binding, transport, _ = self.setup_driver([raw(b"list"), raw(b"SERIAL"), raw(b"FAIL")])
        result = await driver.check_availability(binding, expected_serial="SERIAL", expected_product_id=10,
                                                 timeout_s=Decimal(5))
        self.assertFalse(result.available)
        self.assertEqual(result.reason, "doctor_not_passed")
        self.assertEqual([call.outcome.output for call in result.calls], [b"list", b"SERIAL", b"FAIL"])
        self.assertEqual(len(transport.calls), 3)
        actual = result.to_device_result("31").outcome
        self.assertEqual(actual.status, "failed")
        self.assertEqual(actual.error.details, {"reason": "doctor_not_passed", "step": "doctor", "steps": [
            {"name": "list", "elapsed_ms": 0}, {"name": "info", "elapsed_ms": 0},
            {"name": "doctor", "elapsed_ms": 0}]})
        self.assertEqual(actual.diagnostics.calls[-1].stdout.data, b"FAIL")

    async def test_binding_mismatch_prevents_every_tool_call(self):
        driver, binding, transport, _ = self.setup_driver([])
        with self.assertRaises(ValueError):
            await driver.check_availability(binding, expected_serial="OTHER", expected_product_id=10,
                                            timeout_s=Decimal(5))
        self.assertEqual(transport.calls, [])

    async def test_deadline_stops_before_doctor(self):
        driver, binding, transport, _ = self.setup_driver([raw(b"list"), raw(b"SERIAL")])
        result = await driver.check_availability(binding, expected_serial="SERIAL", expected_product_id=10,
                                                 timeout_s=Decimal(2))
        self.assertFalse(result.available)
        self.assertEqual(result.reason, "deadline_elapsed")
        self.assertEqual([item.argv[-1] for item in transport.calls], ["list", "info"])

    async def test_cancelled_call_preserves_settled_step_and_propagates(self):
        driver, binding, transport, cancelled = self.setup_driver([ToolCallCancelled(raw(b"list"))])
        with self.assertRaises(cancelled) as caught:
            await driver.check_availability(binding, expected_serial="SERIAL", expected_product_id=10,
                                            timeout_s=Decimal(5))
        self.assertEqual(caught.exception.result.reason, "cancelled")
        self.assertEqual(caught.exception.result.calls[0].outcome.output, b"list")
        self.assertEqual(len(transport.calls), 1)

    async def test_unsettled_transport_keeps_original_diagnostics_without_result(self):
        error = RuntimeError("transport unsettled")
        driver, binding, transport, _ = self.setup_driver([raw(b"list"), error])
        result = await driver.check_availability(binding, expected_serial="SERIAL", expected_product_id=10,
                                                 timeout_s=Decimal(5))
        self.assertFalse(result.settled)
        self.assertIs(result.calls[-1].error, error)
        diagnostics = result.tool_diagnostics()
        self.assertEqual([call.name for call in diagnostics.calls], ["list", "info"])
        self.assertEqual(diagnostics.calls[0].stdout.data, b"list")
        self.assertEqual(diagnostics.calls[1].stdout.state, "unavailable")
        with self.assertRaises(ValueError):
            result.to_device_result("31")


if __name__ == "__main__":
    unittest.main()

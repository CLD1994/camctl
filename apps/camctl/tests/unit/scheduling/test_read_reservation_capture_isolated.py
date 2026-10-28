"""拍摄准备独立守卫使用隔离事实判断读取机会。"""

import ast
from contextlib import closing
from decimal import Decimal
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).parents[3] / "src" / "camctl" / "persistence" / "repositories" / "preparation.py"


class CaptureReservationGuardTests(unittest.TestCase):
    def verify(self, *, now=10, blocked=False, held=True):
        tree = ast.parse(SOURCE.read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PreparationReads")
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "verify_create")
        isolated = ast.Module(body=[method], type_ignores=[])
        namespace = {"__name__": "isolated.preparation", "__package__": "camctl.persistence.repositories",
                     "CAPTURE_ACTION_TYPES": (1, 2, 3), "photo_start_recorded": lambda *args: False,
                     "decode_member": lambda *args: SimpleNamespace(name="CAMERA_TAKE_PHOTO")}
        exec(compile(ast.fix_missing_locations(isolated), str(SOURCE), "exec"), namespace)
        values = {name: None for name in ("state_query_supported", "stop_supported", "safe_repeat_stop",
                  "start_return_meaning", "completion_mode", "ownership_mode", "output_scope_json")}
        class Cursor:
            def fetchone(self): return None
            def __iter__(self): return iter(())
            def close(self): pass
        class Connection:
            def execute(self, *args): return Cursor()
        action = {"id": 1, "status": 1, "execution_started": 0, "cancel_requested": 0,
                  "type": 1, "scheduled_at": 10, "max_delay_ms": 10, "driver_id": "driver",
                  "device_id": "camera", "plan_id": 2, "input_index": 0, "execution_spec_json": {}}
        request = {"action_id": 1, "occurred_at": 10, "trusted_wall_now": now,
                   "prepare_lead_s": Decimal(1), "expected_identity": None}
        event = SimpleNamespace(evidence={"preparation_request": request}, occurred_at=10)
        row = SimpleNamespace(after=SimpleNamespace(values={"action_id": 1, "expected_identity_json": None,
                                                            "resource_key": None, **values}))
        context = SimpleNamespace(state_rows={"actions": {1: action}})
        reader = SimpleNamespace(connection=Connection(), availability_in_flight=lambda *args: False)
        definitions = ModuleType("camctl.contracts.values")
        definitions.ConsistencyError = ValueError
        json_values = ModuleType("camctl.contracts.json_values")
        json_values.json_equal = lambda first, second: first == second
        capture_models = ModuleType("camctl.capture.models")
        capture_models.activity_capabilities = lambda *args: SimpleNamespace(**values)
        scheduling = ModuleType("camctl.persistence.repositories.scheduling")
        scheduling.ReservePreparationRequest = lambda **args: SimpleNamespace(**args)
        scheduling._fixed_identity = lambda *args: (None, None)
        history = ModuleType("camctl.history.validators")
        history.parse_preparation_request = lambda raw: raw
        retries = ModuleType("camctl.persistence.repositories.retry_wait_reads")
        retries.read_reservation_blocks_control = lambda *args: blocked
        discovery = ModuleType("camctl.outputs.read_discovery")
        discovery.device_holder = lambda *args: 7 if held else None
        driver = ModuleType("camctl.devices.drivers.hasselblad.definitions")
        driver.DRIVER_ID = "hasselblad_cfv100c"
        modules = (definitions, json_values, capture_models, scheduling, history, retries, discovery, driver)
        with patch.dict(sys.modules, {module.__name__: module for module in modules}):
            return namespace["verify_create"](reader, event, context, row)

    def test_due_capture_can_use_settled_reserved_device(self):
        self.assertIsNone(self.verify())

    def test_early_capture_cannot_preempt_reservation(self):
        with self.assertRaises(ValueError): self.verify(now=9)

    def test_unsettled_read_blocks_due_capture(self):
        with self.assertRaises(ValueError): self.verify(blocked=True)

    def test_early_capture_without_reservation_still_qualifies(self):
        self.assertIsNone(self.verify(now=9, held=False))


if __name__ == "__main__":
    unittest.main()

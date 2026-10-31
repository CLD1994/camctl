"""检查目标及完整目标页的隔离契约。"""

import importlib.abc
import importlib.util
import sys
import unittest
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


SOURCE = Path(__file__).parents[3] / "src" / "camctl" / "persistence" / "repositories" / "availability_target.py"


@dataclass(frozen=True)
class CameraIdentity:
    serial: str
    product_id: int

    def __post_init__(self):
        if not isinstance(self.serial, str) or not self.serial or type(self.product_id) is not int:
            raise ValueError("invalid camera identity")

    @property
    def resource_key(self):
        return f"camera/{self.serial}"


class BlockCamctl(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "camctl" or fullname.startswith("camctl."):
            raise AssertionError(f"禁止导入真实包: {fullname}")
        return None


def target_module():
    modules = {}
    def stub(name, **values):
        module = ModuleType(name)
        module.__dict__.update(values)
        modules[name] = module
    enums = {"actions.status": SimpleNamespace(FAILED=4),
        "device_activities.activity_state": SimpleNamespace(UNKNOWN=1),
        "device_activities.occupancy_state": SimpleNamespace(HELD=1),
        "device_activities.ownership_mode": SimpleNamespace(TASK_SCOPE=1),
        "device_activities.dispatch_state": SimpleNamespace(MAY_HAVE_DISPATCHED=2, SUCCESS_RETURNED=3)}
    def object_id(value):
        if type(value) is not int or value < 1:
            raise ValueError("invalid id")
        return value
    stub("camctl.contracts.enums", enum_for=lambda name: enums[name])
    stub("camctl.contracts.json_values", json_equal=lambda a, b: a == b)
    stub("camctl.contracts.values", ObjectId=object_id)
    stub("camctl.devices.drivers.hasselblad.identity", CameraIdentity=CameraIdentity)
    stub("camctl.devices.start_entry", is_hasselblad_photo=lambda action: action["type"] == 1)
    stub("camctl.history.events", load_event_registry=lambda: {
        "events": {"AVAILABILITY_CHECK": {"id": 20, "branches": {"CREATE": {"reason": 1}}}}})
    spec = importlib.util.spec_from_file_location("isolated_h2_target", SOURCE)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {spec.name: module, **modules}), patch.object(sys, "meta_path", [BlockCamctl(), *sys.meta_path]):
        spec.loader.exec_module(module)
    return module


def target_facts():
    identity = {"serial": "SERIAL", "product_id": 10}
    check = {"id": 31, "action_id": 12, "device_id": "camera", "driver_id": "hb",
             "expected_identity_json": identity, "resource_key": "camera/SERIAL"}
    activity = {"id": 25, "action_id": 7, "activity_state": 1, "occupancy_state": 1,
                "ownership_mode": 1, "dispatch_state": 2, "availability_release_attempt_id": None,
                "expected_identity_json": dict(identity), "resource_key": "camera/SERIAL"}
    action = {"id": 7, "type": 1, "status": 4, "execution_started": 1, "device_id": "camera",
              "driver_id": "hb", "execution_spec_json": {}}
    return check, activity, action


class TargetGuardTests(unittest.TestCase):
    def test_original_target_rejects_active_public_scope_and_wrong_identity(self):
        module = target_module()
        module.require_target_facts(*target_facts())
        for part, key, value in ((1, "activity_state", 2), (1, "ownership_mode", 2),
                                 (1, "availability_release_attempt_id", 42),
                                 (1, "expected_identity_json", None), (2, "device_id", "other"),
                                 (0, "resource_key", "camera/other")):
            facts = list(deepcopy(target_facts()))
            facts[part][key] = value
            with self.subTest(part=part, key=key), self.assertRaises(ValueError):
                module.require_target_facts(*facts)

    def test_fixed_target_group_rejects_incomplete_or_forged_pages(self):
        module = target_module()
        module.iter_creation_targets = lambda connection, check, batch_size: iter([(25, 26)])
        transaction = SimpleNamespace(txn_id=1, first_event_id=100, last_event_id=101)
        def row(identity):
            return SimpleNamespace(table="device_availability_targets", before=SimpleNamespace(exists=False),
                after=SimpleNamespace(exists=True, values={"check_id": 31, "action_id": 12, "activity_id": identity}))
        def event(rows):
            return SimpleNamespace(transaction_id=1, event_type=20, reason=1, event_id=101, rows=tuple(rows))
        check = target_facts()[0]
        for rows in ((row(25),), (row(25), row(27))):
            group = module.AvailabilityCreationTargets(None, check, transaction)
            with self.assertRaises(module.AvailabilityTargetStateError):
                group.prepare(event(rows))
        group = module.AvailabilityCreationTargets(None, check, transaction)
        page = event((row(25), row(26)))
        group.prepare(page)
        group.seal(SimpleNamespace(envelope=page))
        group.finish()


if __name__ == "__main__":
    unittest.main()

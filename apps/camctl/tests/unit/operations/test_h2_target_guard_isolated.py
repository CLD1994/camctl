"""检查目标及完整目标页的隔离契约。"""

import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
from camctl.devices.drivers.hasselblad.identity import CameraIdentity
from camctl.history.events import load_event_registry
from camctl.persistence.repositories import availability_target as module


def target_facts():
    identity = {"serial": "SERIAL", "product_id": 10}
    resource_key = CameraIdentity(**identity).resource_key
    check = {"id": 31, "action_id": 12, "device_id": "camera", "driver_id": DRIVER_ID,
             "expected_identity_json": identity, "resource_key": resource_key}
    activity = {"id": 25, "action_id": 7, "activity_state": 1, "occupancy_state": 1,
                "ownership_mode": 1, "dispatch_state": 2, "availability_release_attempt_id": None,
                "expected_identity_json": dict(identity), "resource_key": resource_key}
    action = {"id": 7, "type": 1, "status": 4, "execution_started": 1, "device_id": "camera",
              "driver_id": DRIVER_ID, "execution_spec_json": {"output_storage": "host"}}
    return check, activity, action


class TargetGuardTests(unittest.TestCase):
    def test_original_target_rejects_active_public_scope_and_wrong_identity(self):
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
        self.enterContext(patch.object(module, "iter_creation_targets",
                                      lambda connection, check, batch_size: iter([(25, 26)])))
        definition = load_event_registry()["events"]["AVAILABILITY_CHECK"]
        transaction = SimpleNamespace(txn_id=1, first_event_id=100, last_event_id=101)
        def row(identity):
            return SimpleNamespace(table="device_availability_targets", before=SimpleNamespace(exists=False),
                after=SimpleNamespace(exists=True, values={"check_id": 31, "action_id": 12, "activity_id": identity}))
        def event(rows):
            return SimpleNamespace(transaction_id=1, event_type=definition["id"],
                reason=definition["branches"]["CREATE"]["reason"], event_id=101, rows=tuple(rows))
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

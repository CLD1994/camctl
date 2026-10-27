"""可用性目标的固定事实分类不读取包资源或数据库。"""

from copy import deepcopy
from importlib.util import find_spec, module_from_spec, spec_from_file_location
import sys
from types import ModuleType, SimpleNamespace

import pytest

from camctl import resources
from camctl.contracts import enums


@pytest.fixture
def target_rules(monkeypatch):
    monkeypatch.setattr(resources, "resource_bytes", lambda name: pytest.fail("单元测试不能读取包资源"))
    definitions = ModuleType("camctl.devices.drivers.hasselblad.definitions")
    definitions.DRIVER_ID = "hasselblad_cfv100c"
    monkeypatch.setitem(sys.modules, definitions.__name__, definitions)
    members = {
        "actions.status": SimpleNamespace(FAILED=4),
        "device_activities.activity_state": SimpleNamespace(UNKNOWN=1),
        "device_activities.occupancy_state": SimpleNamespace(HELD=1),
        "device_activities.ownership_mode": SimpleNamespace(TASK_SCOPE=1),
        "device_activities.dispatch_state": SimpleNamespace(MAY_HAVE_DISPATCHED=2, SUCCESS_RETURNED=3),
    }
    monkeypatch.setattr(enums, "enum_for", lambda column: members[column])
    source = find_spec("camctl.persistence.repositories.availability_target")
    spec = spec_from_file_location("camctl.persistence.repositories._isolated_target", source.origin)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def facts(target_rules):
    identity = target_rules.CameraIdentity("CAMERA-1", 10)
    check = {"action_id": 12, "device_id": "hb0", "driver_id": "hasselblad_cfv100c",
             "expected_identity_json": {"serial": "CAMERA-1", "product_id": 10},
             "resource_key": identity.resource_key}
    activity = {"action_id": 7, "activity_state": 1, "occupancy_state": 1,
                "ownership_mode": 1, "dispatch_state": 2, "availability_release_attempt_id": None,
                "expected_identity_json": {"serial": "CAMERA-1", "product_id": 10},
                "resource_key": identity.resource_key}
    action = {"id": 7, "type": 1, "driver_id": "hasselblad_cfv100c", "device_id": "hb0",
              "status": 4, "execution_started": 1, "execution_spec_json": {"output_storage": "host"}}
    return check, activity, action


@pytest.mark.parametrize("dispatch", [2, 3])
def test_old_unknown_photo_can_be_a_target(target_rules, facts, dispatch):
    check, activity, action = facts
    activity["dispatch_state"] = dispatch
    target_rules.require_target_facts(check, activity, action)


@pytest.mark.parametrize("part,field,value", [
    ("action", "status", 2), ("action", "execution_started", 0),
    ("action", "type", 2), ("action", "driver_id", "other"),
    ("action", "device_id", "other"), ("action", "id", 12),
    ("activity", "action_id", 8), ("activity", "activity_state", 2),
    ("activity", "activity_state", 3), ("activity", "occupancy_state", 2),
    ("activity", "ownership_mode", 2), ("activity", "dispatch_state", 1),
    ("activity", "dispatch_state", 4), ("activity", "availability_release_attempt_id", 99),
    ("activity", "expected_identity_json", None),
    ("activity", "expected_identity_json", {"serial": "CAMERA-2", "product_id": 10}),
    ("activity", "expected_identity_json", {"serial": "CAMERA-1", "product_id": 11}),
    ("activity", "resource_key", "other"), ("check", "resource_key", "other"),
    ("action", "id", None), ("check", "action_id", True),
    ("action", "execution_started", True), ("activity", "activity_state", True),
])
def test_inapplicable_target_is_rejected(target_rules, facts, part, field, value):
    check, activity, action = deepcopy(facts)
    {"check": check, "activity": activity, "action": action}[part][field] = value
    with pytest.raises(ValueError):
        target_rules.require_target_facts(check, activity, action)

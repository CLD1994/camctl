"""拍摄候选读取仅使用隔离的连接替身。"""

from decimal import Decimal
from types import ModuleType, SimpleNamespace
import importlib.util
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

from camctl.contracts import enums
DRIVER_ID = "hasselblad_cfv100c"


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchmany(self, size):
        return self.rows[:size]

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def close(self):
        pass


class Connection:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def execute(self, sql, parameters):
        self.calls += 1
        assert parameters[:3] == ("camera", DRIVER_ID, 1)
        assert "NOT EXISTS" in sql
        after = parameters[3:7] if self.calls > 1 else None
        rows = [row for row in self.rows
                if after is None or (row[1], row[2], row[3], row[0]) > after]
        return Cursor(rows[:parameters[-1]])


@pytest.fixture
def selection(monkeypatch):
    from camctl import resources
    monkeypatch.setattr(resources, "resource_bytes", lambda *_: pytest.fail("不能读取真实包资源"))
    monkeypatch.setattr(enums, "resource_bytes", lambda *_: pytest.fail("不能读取真实包资源"))
    definitions = {"actions.type": SimpleNamespace(CAMERA_TAKE_PHOTO=1),
                   "actions.status": SimpleNamespace(PENDING=1),
                   "operation_runs.kind": SimpleNamespace(START=1, CHECK_DEVICE_AVAILABILITY=12),
                   "operation_runs.status": SimpleNamespace(PENDING=1, ACTIVE=2)}
    monkeypatch.setattr(enums, "enum_for", lambda name: definitions[name])
    driver = ModuleType("camctl.devices.drivers.hasselblad.definitions")
    driver.DRIVER_ID = DRIVER_ID
    with patch.dict(sys.modules, {driver.__name__: driver}):
        from camctl.devices.drivers.hasselblad.identity import CameraIdentity
        path = Path(__file__).parents[3] / "src/camctl/persistence/repositories/capture_selection.py"
        spec = importlib.util.spec_from_file_location("camctl.persistence.repositories._capture_selection_test", path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {spec.name: module}):
            spec.loader.exec_module(module)
            yield module, CameraIdentity


def request(selection, now, lead="0"):
    module, identity = selection
    return module.CaptureSelectionInput("camera", DRIVER_ID, identity("serial", 10), Decimal(lead), now)


def test_pages_preserve_original_order_and_exclude_expired_and_future(selection):
    db = Connection([(4, 0, 1, 0, 0, None),
                     (5, 1_000_000, 1, 0, 1000, None),
                     (6, 1_000_000, 2, 0, 1000, 12),
                     (7, 4_000_000, 1, 0, 1000, None)])
    result = list(selection[0].pending_capture_candidates(db, request(selection, 2_000_000, "1"), batch_size=2))
    assert [(item.action_id, item.activity_id) for item in result] == [(5, None), (6, 12)]
    assert db.calls == 2


def test_negative_timestamp_is_not_lost_at_first_page(selection):
    db = Connection([(2, -1_000_000, 1, 0, 1000, None)])
    assert [item.action_id for item in selection[0].pending_capture_candidates(db, request(selection, 0))] == [2]


def test_invalid_current_inputs_do_not_produce_candidates(selection):
    module, CameraIdentity = selection
    with pytest.raises(ValueError):
        module.CaptureSelectionInput("", DRIVER_ID, CameraIdentity("serial", 10), Decimal(0), 0)
    with pytest.raises(ValueError):
        module.CaptureSelectionInput("camera", DRIVER_ID, CameraIdentity("serial", 10), Decimal("NaN"), 0)
    with pytest.raises(ValueError):
        list(module.pending_capture_candidates(Connection([]), request(selection, 0), batch_size=129))


def test_selection_skips_candidates_without_resource_qualification(selection, monkeypatch):
    module, _ = selection
    candidates = [module.CaptureCandidate(2, 0, 1, 0, 10, None),
                  module.CaptureCandidate(3, 1, 1, 0, 10, None),
                  module.CaptureCandidate(4, 2, 1, 0, 10, None)]
    monkeypatch.setattr(module, "pending_capture_candidates", lambda *_: iter(candidates))
    target = ModuleType("camctl.persistence.repositories.availability_target")
    target.AvailabilityTargetBlocked = type("AvailabilityTargetBlocked", (ValueError,), {})
    target.AvailabilityTargetIdentityMismatch = type("AvailabilityTargetIdentityMismatch", (ValueError,), {})
    target.AvailabilityTargetNotRequired = type("AvailabilityTargetNotRequired", (ValueError,), {})
    target.AvailabilityTargetReuse = type("AvailabilityTargetReuse", (ValueError,), {})
    create = ModuleType("camctl.persistence.repositories.availability_create")
    create.CreateAvailabilityCheck = lambda action_id, *args: SimpleNamespace(action_id=action_id)
    checked = []
    def qualify(_connection, check, _boundary, **kwargs):
        checked.append(check.action_id)
        assert kwargs["check_candidate_order"] is False
        if check.action_id == 2:
            raise target.AvailabilityTargetBlocked("读取仍占用设备")
        raise target.AvailabilityTargetNotRequired()
    create._qualify = qualify
    transaction = ModuleType("camctl.persistence.transaction")
    transaction.row_facts = lambda *_: pytest.fail("无需读取已存在的检查")
    with patch.dict(sys.modules, {target.__name__: target, create.__name__: create,
                                 transaction.__name__: transaction}):
        selected = module.selected_pending_capture(Connection([]), request(selection, 2), 9)
    assert selected.action_id == 3
    assert checked == [2, 3]


def test_selection_does_not_hide_database_failure(selection, monkeypatch):
    module, _ = selection
    candidate = module.CaptureCandidate(2, 0, 1, 0, 10, None)
    monkeypatch.setattr(module, "pending_capture_candidates", lambda *_: iter((candidate,)))
    target = ModuleType("camctl.persistence.repositories.availability_target")
    for name in ("AvailabilityTargetBlocked", "AvailabilityTargetIdentityMismatch",
                 "AvailabilityTargetNotRequired", "AvailabilityTargetReuse"):
        setattr(target, name, type(name, (ValueError,), {}))
    create = ModuleType("camctl.persistence.repositories.availability_create")
    create.CreateAvailabilityCheck = lambda *args: object()
    def fail(*args, **kwargs):
        raise OSError("database unavailable")
    create._qualify = fail
    transaction = ModuleType("camctl.persistence.transaction")
    transaction.row_facts = lambda *_: None
    with patch.dict(sys.modules, {target.__name__: target, create.__name__: create,
                                 transaction.__name__: transaction}):
        with pytest.raises(OSError, match="database unavailable"):
            module.selected_pending_capture(Connection([]), request(selection, 0), 9)

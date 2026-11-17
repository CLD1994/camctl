"""实际读取派发只使用内存替身核对尚未收场的设备检查。"""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from camctl.capture.selection import CaptureSelectionInput
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
from camctl.devices.drivers.hasselblad.identity import CameraIdentity
from camctl.operations.models import AttemptTicket
from camctl.outputs import read_attempts, read_discovery
from camctl.persistence.repositories import capture_selection, physical_alias, transactions
from camctl.persistence.repositories.file_batch_reads import FileBatchReads
from camctl.persistence.repositories.preparation import PreparationReads
from camctl.scheduling import resources as scheduling_resources


IDENTITY = CameraIdentity("serial-1", 1)
SELECTION = CaptureSelectionInput("camera", DRIVER_ID, IDENTITY, Decimal("0"), 10)


@pytest.fixture
def read_module(monkeypatch):
    monkeypatch.setattr(transactions, "row_facts",
                        lambda connection, table, identity: connection.rows[(table, identity)])
    def permit_batch(self, action, run):
        assert (action["id"], run["id"], run["action_id"], run["copy_id"]) == (12, 3, 12, 2)
        return True
    monkeypatch.setattr(FileBatchReads, "operation_permitted", permit_batch)
    monkeypatch.setattr(PreparationReads, "availability_in_flight",
                        lambda self, device, identity: self.connection.check_running)
    monkeypatch.setattr(PreparationReads, "capture_holds_device",
                        lambda self, device: self.connection.capture_held)
    monkeypatch.setattr(scheduling_resources, "current_preparation_holder", lambda connection, device: None)
    monkeypatch.setattr(read_discovery, "read_copy_dispatch_eligible", lambda connection, copy_id: True)
    return read_attempts


class _Cursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row

    def close(self):
        pass


class _Connection:
    def __init__(self, check_running):
        self.check_running = check_running
        self.capture_held = False
        self.rows = {("device_files", 9): {"observer_action_id": 10, "source_action_id": 11},
                     ("actions", 10): {"device_id": "camera", "driver_id": "other"},
                     ("actions", 11): {"device_id": "camera", "driver_id": "other"},
                     ("actions", 12): {"id": 12, "device_id": "camera"},
                     ("operation_runs", 3): {"id": 3, "action_id": 12, "copy_id": 2}}

    def execute(self, sql, args=()):
        if "history_events" in sql:
            return _Cursor((99,))
        return _Cursor((2, 0, 2, 1, 1, None, 9, None, "camera", 17, 1, 1, 3, 50, None))


@pytest.mark.parametrize("check_running", [False, True])
def test_actual_device_read_is_blocked_only_by_running_check(read_module, check_running):
    connection = _Connection(check_running)
    ticket = AttemptTicket(1, "read", "2", "read/2", 3)
    result = read_module.read_dispatch_qualification(SimpleNamespace(connection=connection), ticket, 9, device_source=True)
    assert (None if result is None else result.reason) == ("device_busy" if check_running else None)


@pytest.mark.parametrize("never_dispatched", [False, True])
def test_only_original_unentered_read_is_excluded_for_capture(read_module, monkeypatch, never_dispatched):
    connection = _Connection(False)
    for action in (10, 11):
        connection.rows[("actions", action)]["driver_id"] = DRIVER_ID
    monkeypatch.setattr(physical_alias, "source_physical_alias_conflict", lambda *args: False)
    monkeypatch.setattr(physical_alias, "source_physical_identity", lambda *args: IDENTITY)
    checked = []
    def priority(*args, **kwargs):
        assert args == (connection, SELECTION, 99)
        assert (kwargs["device_id"], kwargs["identity"], kwargs["occurred_at"]) == ("camera", IDENTITY, 10)
        checked.append(kwargs["exclude_read_attempt_id"])
        return capture_selection.CaptureCandidate(42, 10, 4, 0, 1000, None)
    monkeypatch.setattr(capture_selection, "read_capture_priority", priority)
    ticket = AttemptTicket(1, "read", "2", "read/2", 3)
    result = read_module.read_dispatch_qualification(SimpleNamespace(connection=connection), ticket, 9,
        device_source=True, capture_selection=SELECTION,
        never_dispatched=never_dispatched)
    assert (None if result is None else result.reason) == ("capture_priority" if never_dispatched else None)
    assert checked == ([17] if never_dispatched else [])


def test_dispatch_rejects_slot_from_a_different_source_binding(read_module):
    connection = _Connection(False)
    connection.rows[("actions", 10)]["device_id"] = "other"
    connection.rows[("actions", 11)]["device_id"] = "other"
    ticket = AttemptTicket(1, "read", "2", "read/2", 3)
    with pytest.raises(read_module.ConsistencyError, match="原来源绑定"):
        read_module.read_dispatch_qualification(SimpleNamespace(connection=connection), ticket, 9,
                                                device_source=True, never_dispatched=True)


def test_dispatch_rejects_wrong_read_responsibility(read_module):
    ticket = AttemptTicket(1, "read", "2", "read/other", 3)
    with pytest.raises(read_module.ConsistencyError):
        read_module.read_dispatch_qualification(SimpleNamespace(connection=_Connection(False)), ticket, 9,
                                                device_source=True)


def test_prepared_capture_blocks_unentered_and_recovered_reads(read_module, monkeypatch):
    connection = _Connection(False)
    connection.capture_held = True
    for action in (10, 11):
        connection.rows[("actions", action)]["driver_id"] = DRIVER_ID
    monkeypatch.setattr(physical_alias, "source_physical_alias_conflict", lambda *args: False)
    monkeypatch.setattr(physical_alias, "source_physical_identity", lambda *args: IDENTITY)
    monkeypatch.setattr(capture_selection, "read_capture_priority", lambda *args, **kwargs: None)
    ticket = AttemptTicket(1, "read", "2", "read/2", 3)
    current = SELECTION
    unentered = read_module.read_dispatch_qualification(SimpleNamespace(connection=connection), ticket, 9,
        device_source=True, capture_selection=current, never_dispatched=True)
    assert unentered.reason == "capture_priority"
    recovered = read_module.read_dispatch_qualification(SimpleNamespace(connection=connection), ticket, 9,
        device_source=True, capture_selection=current, never_dispatched=False)
    assert recovered.reason == "capture_priority"

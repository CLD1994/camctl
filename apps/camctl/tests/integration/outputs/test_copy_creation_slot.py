"""拷贝创建尚未持有相机机会，原建档响应不随当前归属变化。"""

from dataclasses import replace

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.history.validators import EventValidationError
from camctl.operations.attempts import BeginDisposition
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories import outputs
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.transaction import commit_operation, saved_transaction_events

from .test_grant_reuse import granted, read_request, read_targets, local_read, _change_event, _reuse
from ..operations.test_read_attempt_slot import _intent


def test_copy_creation_keeps_camera_slot_empty(granted):
    owned, _, key, prepared = granted
    assert owned.connection.execute("SELECT slot_device_id FROM file_copies WHERE id=?",
                                    (prepared.copy_id,)).fetchone() == (None,)
    events = saved_transaction_events(owned.connection, key)
    copy, = [row for event in events for row in event["body"]["rows"] if row["table"] == "file_copies"]
    assert copy["after"]["values"]["slot_device_id"] is None
    assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE id=?",
                                    (prepared.run_id,)).fetchone() == (0,)


@pytest.mark.parametrize("read_request", ["device", "internal"], indirect=True)
def test_device_copy_cannot_start_reading_immediately_after_creation(granted):
    owned, command, _, prepared = granted
    before = tuple(owned.connection.iterdump())
    result = OperationRepository().begin_attempt(_intent(command, prepared), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.disposition is BeginDisposition.REJECTED
    assert result.value.reason == "read_slot_not_held"
    assert tuple(owned.connection.iterdump()) == before


class _CopyCreation:
    def __init__(self, command, key, slot):
        self.command, self.key, self.slot = command, key, slot

    def plan(self, scope):
        plan = outputs._GrantFileCommand(self.command, self.key).plan(scope)
        events = []
        for event in plan.events:
            row, = event.rows
            if row.table == "file_copies":
                row = replace(row, after=replace(row.after, values={**row.after.values, "slot_device_id": self.slot}))
                event = replace(event, rows=(row,))
            events.append(event)
        return replace(plan, events=tuple(events))


@pytest.mark.parametrize("slot", [None, "cam-1"])
def test_formal_copy_creation_requires_empty_initial_slot(read_request, slot):
    owned, command = read_request
    before = tuple(owned.connection.iterdump())
    key = new_operation_key()
    receipt = commit_operation(_CopyCreation(command, key, slot), key, owned)
    if slot is None:
        assert receipt.kind == "completed", receipt.error
    else:
        assert receipt.kind == "rolled_back", receipt
        assert isinstance(receipt.error, EventValidationError), receipt.error
        assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("read_request", ["device", "internal"], indirect=True)
@pytest.mark.parametrize("current", ["waiting", "acquired", "released", "reacquired", "foreign"])
def test_original_empty_slot_is_independent_of_current_device_ownership(granted, current):
    owned, command, key, first = granted
    # 构造格式 1 的空初始机会；当前归属是后续投影夹具，不代替机会事务验收。
    _change_event(owned, key, "file_copies",
                  lambda body: body["rows"][0]["after"]["values"].update(slot_device_id=None))
    stages = {"waiting": (None,), "acquired": ("cam-1",), "released": ("cam-1", None),
              "reacquired": ("cam-1", None, "cam-1"), "foreign": ("another-camera",)}[current]
    for slot in stages:
        owned.connection.execute("UPDATE file_copies SET slot_device_id=? WHERE id=?", (slot, first.copy_id))
        owned.connection.commit()
    result = _reuse(owned, command, key)
    if current == "foreign":
        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert isinstance(result.error, ConsistencyError), result.error
    else:
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value == first


def test_original_creation_cannot_claim_a_nonempty_slot(granted):
    owned, command, key, _ = granted
    _change_event(owned, key, "file_copies",
                  lambda body: body["rows"][0]["after"]["values"].update(slot_device_id="cam-1"))
    result = _reuse(owned, command, key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result
    assert isinstance(result.error, ConsistencyError), result.error


@pytest.mark.parametrize("read_request", ["host"], indirect=True)
def test_host_copy_cannot_reuse_a_current_device_slot(granted):
    owned, command, key, first = granted
    owned.connection.execute("UPDATE file_copies SET slot_device_id='cam-1' WHERE id=?", (first.copy_id,))
    owned.connection.commit()
    result = _reuse(owned, command, key)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, ConsistencyError), result.error

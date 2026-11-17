"""读取机会与同设备 START 互斥的隔离测试。"""

from decimal import Decimal
import sqlite3
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.capture.selection import CaptureSelectionInput
from camctl.devices.drivers.hasselblad.identity import CameraIdentity
from camctl.history.validators import EventValidationError
from camctl.persistence.repositories import capture_selection, outputs, physical_alias, preparation
from camctl.persistence.repositories.file_batch_reads import FileBatchReads
from camctl.persistence.repositories.retry_wait_reads import RetryWaitReads
from camctl.scheduling import resources as scheduling_resources


class _GuardContext:
    def __init__(self, state_rows, preparation_reads=None):
        self.state_rows = state_rows
        self.preparation_reads = preparation_reads
        self.retry_wait_reads = RetryWaitReads(None)
        self.file_batch_reads = FileBatchReads(None)
        self.validated_events = ()

    def complete_rows(self, table, column, identity):
        assert (table, column, identity) == ("operation_attempts", "run_id", 31)
        return {}


def _cursor(row=None, rows=()):
    cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
    cursor.fetchone.return_value = row
    cursor.fetchall.return_value = list(rows)
    cursor.fetchmany.return_value = list(rows)
    cursor.__iter__.return_value = iter(rows)
    return cursor


@pytest.fixture
def production(monkeypatch):
    # 测试已有对应动作持有的同机批次；机会与启动资格仍由真实守卫判断。
    monkeypatch.setattr(FileBatchReads, "permits", create_autospec(FileBatchReads.permits,
        side_effect=lambda self, action_id, device_id, require_owned=False:
            action_id == 21 and device_id == "camera-a" and require_owned))
    monkeypatch.setattr(RetryWaitReads, "no_unsettled_attempts",
        create_autospec(RetryWaitReads.no_unsettled_attempts, return_value=True))
    return SimpleNamespace(outputs=outputs, scheduling=scheduling_resources,
                           preparation=preparation, validation_error=EventValidationError)


def _read_command(outputs, *, slot=None):
    request = outputs.SlotRequest(copy_id=11, occurred_at=1)
    return outputs._SlotChangeCommand(request, "grant", "a" * 32)


def _grant_inputs(outputs, connection, *, slot=None):
    scope = SimpleNamespace(connection=connection)
    copy = {"id": 11, "slot_device_id": slot, "source_device_file_id": 41, "read_end_event_id": None}
    run = {
        "id": 31, "status": int(outputs._RUN_STATUS.ACTIVE),
        "retry_wait_required": 0,
    }
    action = {
        "id": 21, "type": int(outputs._ACTION_TYPE.OBTAIN_ACTION_OUTPUTS),
        "status": int(outputs._ACTION_STATUS.RUNNING), "cancel_requested": 0,
        "scheduled_at": 0,
    }
    return scope, copy, run, action


def test_new_camera_slot_waits_for_in_flight_start_without_event(monkeypatch, production):
    outputs = production.outputs
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    connection.execute.return_value = _cursor((1,))
    monkeypatch.setattr(outputs._SlotChangeCommand, "_device_holder", lambda *_: None)
    monkeypatch.setattr(production.scheduling, "current_preparation_holder", lambda *_: None)
    monkeypatch.setattr(
        outputs._SlotChangeCommand, "_first_candidate",
        lambda *_args, **_kwargs: pytest.fail("在途 START 必须先阻止授予"),
    )
    scope, copy, run, action = _grant_inputs(outputs, connection)

    copy["source_device_file_id"] = 41
    command = _read_command(outputs)
    command._state.update({"device_files": {41: {"id": 41, "observer_action_id": 22, "source_action_id": 23}},
                           "actions": {22: {"id": 22, "device_id": "camera-a", "driver_id": "driver-a"},
                                       23: {"id": 23, "device_id": "camera-a", "driver_id": "driver-a"}}})
    plan = command._grant(scope, copy, "camera-a", run, action)

    assert plan.result == outputs.SlotDecision(outputs.SlotOutcome.WAIT, "device_busy")
    assert plan.read_only and plan.events == ()
    sql, parameters = connection.execute.call_args.args
    assert "a.device_id=?" in sql and parameters == (int(outputs._RUN_KIND.START), "camera-a")


def test_existing_slot_stays_held_while_start_is_in_flight(monkeypatch, production):
    outputs = production.outputs
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    monkeypatch.setattr(outputs._SlotChangeCommand, "_device_holder", lambda *_: 11)
    scope, copy, run, action = _grant_inputs(outputs, connection, slot="camera-a")

    plan = _read_command(outputs)._grant(scope, copy, "camera-a", run, action)

    assert plan.result.outcome is outputs.SlotOutcome.HELD
    assert plan.events == ()
    connection.execute.assert_not_called()


def test_canceled_owner_cannot_receive_a_new_slot(monkeypatch, production):
    outputs = production.outputs
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    connection.execute.return_value = _cursor(None)
    monkeypatch.setattr(outputs._SlotChangeCommand, "_device_holder", lambda *_: None)
    scope, copy, run, action = _grant_inputs(outputs, connection)
    action["cancel_requested"] = 1

    result = _read_command(outputs)._grant(scope, copy, "camera-a", run, action)

    assert result.result.outcome is outputs.SlotOutcome.FINISHED
    assert result.events == ()
    connection.execute.assert_not_called()
    reads = production.preparation.PreparationReads(connection)
    context = _guard_context(outputs, reads=reads)
    context.state_rows["actions"][21]["cancel_requested"] = 1
    with pytest.raises(production.validation_error, match="未取消"):
        outputs._read_slot_guard(_slot_event(outputs, None, "camera-a"), context)


def test_new_camera_slot_waits_for_in_flight_check(monkeypatch, production):
    outputs = production.outputs
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    monkeypatch.setattr(outputs._SlotChangeCommand, "_device_holder", lambda *_: None)
    monkeypatch.setattr(production.preparation.PreparationReads, "availability_in_flight", lambda *args: True)
    monkeypatch.setattr(outputs._SlotChangeCommand, "_first_candidate",
                        lambda *_args, **_kwargs: pytest.fail("在途检查必须先阻止授予"))
    scope, copy, run, action = _grant_inputs(outputs, connection)
    copy["source_device_file_id"] = 41
    command = _read_command(outputs)
    command._state.update({"device_files": {41: {"id": 41, "observer_action_id": 22, "source_action_id": 23}},
                           "actions": {22: {"id": 22, "device_id": "camera-a", "driver_id": "driver-a"},
                                       23: {"id": 23, "device_id": "camera-a", "driver_id": "driver-a"}}})
    plan = command._grant(scope, copy, "camera-a", run, action)
    assert plan.result == outputs.SlotDecision(outputs.SlotOutcome.WAIT, "device_busy")
    assert plan.read_only and plan.events == ()


def test_new_hasselblad_slot_waits_for_selected_capture(monkeypatch, production):
    outputs = production.outputs
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    monkeypatch.setattr(outputs._SlotChangeCommand, "_device_holder", lambda *_: None)
    scope, copy, run, action = _grant_inputs(outputs, connection)
    scope.max_event_id = 99
    command = _read_command(outputs)
    identity = CameraIdentity("CAMERA-1", 10)
    selection = CaptureSelectionInput("camera-a", "hasselblad_cfv100c", identity, Decimal("1"), 1)
    command._request = outputs.SlotRequest(copy_id=11, occurred_at=1, capture_selection=selection)
    command._state.update({"device_files": {41: {"id": 41, "observer_action_id": 22, "source_action_id": 22}},
                           "actions": {22: {"id": 22, "device_id": "camera-a", "driver_id": "hasselblad_cfv100c"}}})
    monkeypatch.setattr(physical_alias, "source_physical_alias_conflict",
                        create_autospec(physical_alias.source_physical_alias_conflict, return_value=False))
    monkeypatch.setattr(physical_alias, "source_physical_identity",
                        create_autospec(physical_alias.source_physical_identity, return_value=identity))
    monkeypatch.setattr(capture_selection, "read_capture_priority",
                        create_autospec(capture_selection.read_capture_priority,
                            return_value=capture_selection.CaptureCandidate(7, 0, 2, 0, 3000, None)))
    result = command._grant(scope, copy, "camera-a", run, action)
    assert result.result == outputs.SlotDecision(outputs.SlotOutcome.WAIT, "capture_priority")
    assert result.events == ()
    capture_selection.read_capture_priority.assert_called_once_with(
        connection, selection, 99, device_id="camera-a", identity=identity, occurred_at=1)


def test_slot_guard_rejects_selected_capture(monkeypatch, production):
    outputs = production.outputs
    identity = CameraIdentity("CAMERA-1", 10)
    selection = CaptureSelectionInput("camera-a", "hasselblad_cfv100c", identity, Decimal("1"), 1)
    monkeypatch.setattr(physical_alias, "source_physical_alias_conflict",
                        create_autospec(physical_alias.source_physical_alias_conflict, return_value=False))
    monkeypatch.setattr(physical_alias, "source_physical_identity",
                        create_autospec(physical_alias.source_physical_identity, return_value=identity))
    monkeypatch.setattr(capture_selection, "read_capture_priority",
                        create_autospec(capture_selection.read_capture_priority,
                            return_value=capture_selection.CaptureCandidate(7, 0, 2, 0, 3000, None)))
    reads = production.preparation.PreparationReads(object())
    monkeypatch.setattr(reads, "device_start_in_flight", lambda *_: False)
    context = _guard_context(outputs, reads=reads)
    context.state_rows["actions"][22]["driver_id"] = "hasselblad_cfv100c"
    context.state_rows["actions"][23]["driver_id"] = "hasselblad_cfv100c"
    context.transaction = SimpleNamespace(first_event_id=100)
    event = _slot_event(outputs, None, "camera-a")
    event.evidence = {"capture_selection": selection.document()}
    with pytest.raises(production.validation_error, match="已选中的拍摄"):
        outputs._read_slot_guard(event, context)
    capture_selection.read_capture_priority.assert_called_once_with(
        reads.connection, selection, 99, device_id="camera-a", identity=identity, occurred_at=1)


def _slot_event(outputs, before, after):
    row = SimpleNamespace(
        table="file_copies", row_id=11,
        before=SimpleNamespace(exists=True, values={"slot_device_id": before}),
        after=SimpleNamespace(exists=True, values={"slot_device_id": after}),
    )
    return SimpleNamespace(
        event_type=outputs._COPY_CHANGED_EVENT, reason=outputs._SLOT_REASON,
        occurred_at=1, rows=(row,),
    )


def _guard_context(outputs, *, slot=None, source_device_file_id=41, reads=None):
    return _GuardContext({
        "file_copies": {11: {
            "id": 11, "slot_device_id": slot,
            "source_device_file_id": source_device_file_id,
            "source_intermediate_file_id": None, "delivery_id": 51,
            "processing_id": None, "inspection_id": None,
            "committed_bytes": 8, "source_size": 8,
        }},
        "device_files": {41: {"observer_action_id": 22, "source_action_id": 23}},
        "actions": {
            21: {"id": 21, "status": int(outputs._ACTION_STATUS.RUNNING),
                "type": int(outputs._ACTION_TYPE.OBTAIN_ACTION_OUTPUTS),
                "cancel_requested": 0, "scheduled_at": 0},
            22: {"device_id": "camera-a", "driver_id": "driver-a"},
            23: {"device_id": "camera-a", "driver_id": "driver-a"},
        },
        "deliveries": {51: {"action_id": 21}},
        "operation_runs": {31: {
            "id": 31, "copy_id": 11, "kind": int(outputs._RUN_KIND.READ_FILE),
            "status": int(outputs._RUN_STATUS.ACTIVE), "retry_wait_required": 0,
        }},
    }, reads)


def test_slot_guard_reads_start_state_and_rejects_only_in_flight_start(production):
    outputs = production.outputs
    for start_result, rejected in ((None, False), ((1,), True)):
        connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
        connection.execute.return_value = _cursor(start_result)
        reads = production.preparation.PreparationReads(connection)
        context = _guard_context(outputs, reads=reads)
        event = _slot_event(outputs, None, "camera-a")

        if rejected:
            with pytest.raises(production.validation_error, match="启动收场"):
                outputs._read_slot_guard(event, context)
        else:
            outputs._read_slot_guard(event, context)

        sql, parameters = connection.execute.call_args_list[0].args
        assert "a.device_id=?" in sql
        assert parameters == (int(outputs._RUN_KIND.START), "camera-a")


def test_slot_guard_independently_rejects_in_flight_check(production, monkeypatch):
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    connection.execute.return_value = _cursor(None)
    monkeypatch.setattr(production.preparation.PreparationReads, "availability_in_flight", lambda *args: True)
    reads = production.preparation.PreparationReads(connection)
    with pytest.raises(production.validation_error, match="设备检查"):
        production.outputs._read_slot_guard(_slot_event(production.outputs, None, "camera-a"),
                                            _guard_context(production.outputs, reads=reads))


def test_host_source_never_gets_a_camera_slot(production):
    outputs = production.outputs
    context = _guard_context(outputs, source_device_file_id=None)

    with pytest.raises(production.validation_error, match="主机源拷贝"):
        outputs._read_slot_guard(_slot_event(outputs, None, "camera-a"), context)


def test_slot_guard_requires_kernel_reader_for_a_new_grant(production):
    outputs = production.outputs

    with pytest.raises(production.validation_error):
        outputs._read_slot_guard(
            _slot_event(outputs, None, "camera-a"), _guard_context(outputs)
        )


def test_releasing_existing_slot_is_not_blocked_by_start(production):
    outputs = production.outputs
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    reads = production.preparation.PreparationReads(connection)
    context = _guard_context(outputs, slot="camera-a", reads=reads)

    outputs._read_slot_guard(_slot_event(outputs, "camera-a", None), context)

    connection.execute.assert_not_called()

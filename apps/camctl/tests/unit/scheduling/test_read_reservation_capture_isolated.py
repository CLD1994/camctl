"""拍摄准备守卫按原读取机会与已收场事实判断资格。"""

from decimal import Decimal
import sqlite3
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.devices.tasks import CompletionMode, StartReturn
from camctl.outputs import read_discovery
from camctl.persistence.repositories import retry_wait_reads
from camctl.persistence.repositories.preparation import PreparationReads
from camctl.persistence.runtime import CURRENT_FORMAT_VERSION


@pytest.mark.parametrize("now,blocked,held,permitted", [
    (10, False, True, True),
    (9, False, True, False),
    (10, True, True, False),
    (9, False, False, True),
], ids=["到时且原读取已收场", "提前准备不能越过原机会", "原读取未收场", "无原机会时允许提前准备"])
def test_capture_preparation_respects_original_read_reservation(monkeypatch, now, blocked, held, permitted):
    connection = create_autospec(sqlite3.Connection, instance=True)

    def execute(sql, parameters=()):
        cursor = create_autospec(sqlite3.Cursor, instance=True)
        cursor.fetchone.return_value = ((CURRENT_FORMAT_VERSION,)
            if sql == "SELECT format_version FROM database_metadata WHERE id=1" else None)
        cursor.fetchall.return_value = []
        cursor.fetchmany.return_value = []
        cursor.__iter__.return_value = iter(())
        return cursor

    connection.execute.side_effect = execute
    reservation = create_autospec(retry_wait_reads.read_reservation_blocks_control, return_value=blocked)
    holder = create_autospec(read_discovery.device_holder, return_value=7 if held else None)
    monkeypatch.setattr(retry_wait_reads, "read_reservation_blocks_control", reservation)
    monkeypatch.setattr(read_discovery, "device_holder", holder)
    action = {"id": 1, "status": 1, "execution_started": 0, "cancel_requested": 0,
              "type": 1, "scheduled_at": 10, "max_delay_ms": 10, "driver_id": "driver",
              "device_id": "camera", "plan_id": 2, "input_index": 0,
              "execution_spec_json": {"output_storage": "host", "start_call_timeout_s": Decimal("60")}}
    request = {"action_id": 1, "occurred_at": 10, "trusted_wall_now": now,
               "prepare_lead_s": Decimal(1), "expected_identity": None}
    event = SimpleNamespace(evidence={"preparation_request": request}, occurred_at=10)
    row = SimpleNamespace(after=SimpleNamespace(values={
        "action_id": 1, "expected_identity_json": None, "resource_key": None,
        "state_query_supported": 0, "stop_supported": 0, "safe_repeat_stop": 0,
        "start_return_meaning": int(StartReturn.COMPLETED),
        "completion_mode": int(CompletionMode.DEVICE_EVIDENCE),
        "ownership_mode": 1, "output_scope_json": {},
    }))
    context = SimpleNamespace(state_rows={"actions": {1: action}})
    reader = PreparationReads(connection)
    if permitted:
        reader.verify_create(event, context, row)
    else:
        reason = "原读取调用" if blocked else "原读取机会"
        with pytest.raises(ConsistencyError, match=reason):
            reader.verify_create(event, context, row)
    reservation.assert_called_once_with(connection, "camera")
    if now < action["scheduled_at"] and not blocked:
        holder.assert_called_once_with(connection, "camera")
    else:
        holder.assert_not_called()

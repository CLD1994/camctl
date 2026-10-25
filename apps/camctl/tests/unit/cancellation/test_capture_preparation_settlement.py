"""取消成员等待准备的实际调用、原保存和占用释放。"""

import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest

from camctl.cancellation.settlement import TargetSettlement
from camctl.persistence.repositories.capture_facts import StartFacts
from camctl.persistence.repositories.cancellation import CancellationRepository
from camctl.persistence.repositories.outputs import OutputsRepository


@pytest.mark.asyncio
@pytest.mark.parametrize("pending,held,canceled", [(True, False, 1), (False, True, 1), (False, True, 0)])
async def test_terminal_capture_waits_for_original_preparation(mocker, pending, held, canceled):
    connection = create_autospec(sqlite3.Connection, instance=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    cursor.fetchone.return_value = (2, 6)
    connection.execute.return_value = cursor
    action = {"id": 1, "type": 2, "status": 6, "cancel_requested": canceled}
    mocker.patch("camctl.cancellation.settlement.row_facts", autospec=True, return_value=action)
    mocker.patch("camctl.cancellation.settlement.load_start_facts", autospec=True,
        return_value=StartFacts({"id": 7, "occupancy_state": 1 if held else 2}, None, 0, True, 1))
    settlement = TargetSettlement(
        owned=SimpleNamespace(connection=connection),
        outputs=create_autospec(OutputsRepository, instance=True),
        cancellations=create_autospec(CancellationRepository, instance=True),
        withdrawal_positions=Mock(spec=lambda identity: "ready"), occurred_at=lambda: 10,
        preparation_pending=Mock(spec=lambda identity: False, return_value=pending))
    assert not (await settlement.settle(1)).complete

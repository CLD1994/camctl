"""状态库不可读时保留应急收场诊断。"""

from unittest.mock import create_autospec

import pytest

from camctl.capture.emergency_flow import EmergencySettlement
from camctl.devices.drivers.registry import DriverRegistry

pytestmark = pytest.mark.asyncio
_NOW = 1_750_000_000_000_000
_SESSION = "b" * 32


async def test_unreadable_state_keeps_diagnosis_without_device_calls():
    registry = create_autospec(DriverRegistry, instance=True)

    class _UnreadableDatabase:
        async def read(self, operation, *, description):
            raise RuntimeError("database disk image is malformed")

    settlement = EmergencySettlement(
        session_key=_SESSION, anchors={1: (_NOW, _NOW + 1)},
        stop_config={"cam-1": {"max_attempts": 2}},
        registry=registry, database=_UnreadableDatabase(),
        wall_us=lambda: _NOW)
    entries = await settlement.settle()

    registry.entry.assert_not_called()
    assert len(entries) == 1
    assert entries[0]["outcome"] == "not_attempted"
    assert entries[0]["record_status"] == "not_recorded"
    assert "目标身份读取失败" in entries[0]["error"]
    assert "database disk image is malformed" in entries[0]["error"]

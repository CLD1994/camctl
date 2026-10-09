"""两个读取装配入口交付本次设备配置，不访问设备或持久化。"""

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.bootstrap.capture_assembly import _media_flow_with
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.obtain_assembly import session_obtain_assembly
from camctl.capture.media import MediaTools
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.evidence import EvidenceRegistry
from camctl.devices.ports import DriverDeclaration, ReadDriver
from camctl.operations.attempts import RetryWaitGate


def _entry():
    return DriverEntry("camctl-adb", create_autospec(ReadDriver, instance=True),
        DriverDeclaration(False, False, False, False, True, False, False),
        EvidenceRegistry(()), DriverStatus.SOFTWARE_CONTRACT_VERIFIED)


@pytest.mark.parametrize("consumer", ["obtain", "media"])
@pytest.mark.parametrize("settings,expected", [
    ({}, (3, Decimal("10"), 1, Decimal("3"))),
    ({"max_read_attempts": 7, "read_idle_timeout_s": "1.25", "max_recopies": 2,
      "retry_interval_s": "0.5"}, (7, Decimal("1.25"), 2, Decimal("0.5"))),
])
def test_current_device_copy_configuration_reaches_both_consumers(consumer, settings, expected):
    config = load_config({"devices": {"cam-1": {
        "kind": "camera", "driver": "camctl-adb", "copy": settings}}}, ConfigDefaults())
    entry = _entry()
    owned = SimpleNamespace(connection=None)
    if consumer == "obtain":
        runtime = session_obtain_assembly(
            devices=config.devices, drivers=DriverRegistry((entry,)), staging=Path("/staging"),
            ready=Path("/ready"), processing=Path("/processing"))(owned)
        consumer_port = runtime.devices["cam-1"]
    else:
        consumer_port = _media_flow_with(
            owned, entry, "cam-1", "camctl-adb", create_autospec(MediaTools, instance=True),
            Path("/staging"), lambda: 1000, config.devices["cam-1"], RetryWaitGate(), lambda: 0)

    assert consumer_port is not None
    assert (consumer_port.max_read_attempts, consumer_port.read_idle_timeout_s,
            consumer_port.max_recopies, consumer_port.retry_interval_s) == expected

"""设备声明无法可靠使用时保留配置前提错误。"""

from types import SimpleNamespace

import pytest

from camctl.devices.bindings import BindingResult, BindingStatus, DeviceBinding, binding_failure_details
from camctl.session.service import _drive_flows, _restricted_session


async def _unavailable(context):
    binding_failure_details(BindingResult(BindingStatus.UNAVAILABLE,
        DeviceBinding("cam-1", "original"), None))


@pytest.mark.asyncio
async def test_normal_flow_does_not_turn_unreliable_device_configuration_into_database_error():
    context = SimpleNamespace(flows={"capture": _unavailable}, failure_log=None)

    with pytest.raises(ValueError):
        await _drive_flows(context)


@pytest.mark.asyncio
async def test_restricted_flow_reports_device_configuration_error_before_report_work():
    context = SimpleNamespace(restricted_flows={"capture": _unavailable}, once_report=None)

    outcome = await _restricted_session(context)

    assert outcome.reason == "configuration_error"

"""设备声明无法可靠使用时保留配置前提错误。"""

import asyncio
import json
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


@pytest.mark.asyncio
async def test_slow_flow_reports_its_name_without_logging_fast_flows(monkeypatch):
    from camctl.logging_runtime.events import event_logging, monitor_slow_stage
    from camctl.session import service

    records = []
    channel = SimpleNamespace(slog=lambda record: records.append(json.loads(record.rendered_message())))
    monkeypatch.setattr(service, "monitor_slow_stage",
                        lambda stage, **fields: monitor_slow_stage(stage, threshold_s=0.01, **fields))

    async def fast(context):
        return None

    async def slow(context):
        await asyncio.sleep(0.03)

    context = SimpleNamespace(flows={"fast": fast, "scheduling": slow}, failure_log=None)
    with event_logging(channel):
        assert await _drive_flows(context) is None
    assert [(r["stage"], r["event"], r["flow"]) for r in records] == [
        ("session.flow", "started", "scheduling"),
        ("session.flow", "returned", "scheduling")]

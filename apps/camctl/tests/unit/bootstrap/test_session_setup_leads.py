"""候选发现沿会话缓存动态切换首次及后续准备时段。"""

from decimal import Decimal
from pathlib import Path
import shlex
import sqlite3
from unittest.mock import AsyncMock, create_autospec

import pytest

from camctl.bootstrap.capture_assembly import _PreparationLeads
from camctl.bootstrap.capture_assembly import execution_wait_config, session_capture_assembly
from camctl.bootstrap.flows import CaptureFlow
from camctl.contracts.pages import Page
from camctl.devices.adb_transport import ManagedTransport
from camctl.devices.bindings import DeviceBinding
from camctl.devices.drivers.adb_cameras.driver import AdbCameraDriver
from camctl.devices.drivers.adb_cameras.osmo360ii_capture import osmo360ii_contract
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.ports import ControlRequest
from camctl.operations.models import AttemptTicket
from camctl.operations.process import LocalExit, RawToolOutcome
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from support.business_database import inline_business_database
from test_capture_window_progress import _CaptureState


def test_preparation_leads_follow_setup_completion_and_keep_disabled_advance():
    pending = [Decimal("30")]
    leads = _PreparationLeads(
        {"camera": Decimal("10"), "disabled": Decimal("0"), "photo": Decimal("10")},
        {"camera": lambda: pending[0], "disabled": lambda: pending[0]})

    assert dict(leads) == {"camera": Decimal("40"), "disabled": Decimal("0"), "photo": Decimal("10")}
    pending[0] = Decimal("0")
    assert dict(leads) == {"camera": Decimal("10"), "disabled": Decimal("0"), "photo": Decimal("10")}


@pytest.mark.asyncio
async def test_capture_discovery_uses_updated_setup_lead_without_rebuilding_flow(mocker):
    state = _CaptureState(mocker, ())
    pending = [Decimal("30")]
    state.factory.preparation_leads = _PreparationLeads(
        {"cam-1": Decimal("10")}, {"cam-1": lambda: pending[0]})
    flow = CaptureFlow(state.factory)
    flow._discovery = state.discovery
    seen = []

    async def discover(database, now, leads):
        seen.append(dict(leads))
        return Page((), None)

    state.discovery.captures.side_effect = discover
    mocker.patch("camctl.outputs.read_discovery.next_preparation_at", autospec=True, return_value=None)

    await flow(state.context)
    pending[0] = Decimal("0")
    await flow(state.context)

    assert seen == [{"cam-1": Decimal("40")}, {"cam-1": Decimal("10")}]


@pytest.mark.asyncio
async def test_production_assembly_uses_actual_driver_cache_for_lead_and_stage_budget():
    contract = osmo360ii_contract()
    devices = {"camera": {"driver": contract.driver_id, "adb": {"serial": "serial-1"}}}
    transport = create_autospec(ManagedTransport, instance=True)

    async def run(spec, stop):
        command = shlex.split(shlex.split(spec.argv[5])[2])
        output = (b"service connected\n" if command[0] == "simulate_device" else
            b"Resp message, len = 1, data:\n 00\n")
        return RawToolOutcome(LocalExit(exit_code=0), output, None, None, stderr=b"")

    transport.run.side_effect = run
    driver = AdbCameraDriver(contract, devices, transport,
        terminate_grace_s=Decimal("1"), monotonic_ns=lambda: 0)
    entry = DriverEntry(contract.driver_id, driver, driver.declaration,
        contract.evidence, DriverStatus.SOFTWARE_CONTRACT_VERIFIED)
    factory = session_capture_assembly(devices=devices, drivers=DriverRegistry((entry,)),
        staging=Path("/staging"), wait_config=execution_wait_config, media_enabled=False)
    connection = create_autospec(sqlite3.Connection, instance=True)
    connection.execute.return_value.fetchone.return_value = None
    database = inline_business_database(OwnedConnection(connection, DatabaseMetadata(
        "camctl", "a" * 32, 6, "/staging", "/ready", "/processing")))
    runtime = await factory(database, "camera")
    binding = DeviceBinding("camera", contract.driver_id)

    assert runtime.prepare_lead_s == Decimal("10")
    assert runtime.session_setup_budget(binding) == Decimal("30")
    assert factory.preparation_leads["camera"] == Decimal("40")

    request = ControlRequest("start_recording", binding,
        {"type": "osmo360ii_record", "duration_s": 10, "bitrate": "standard", "exposure": {"mode": "auto"}},
        ticket=AttemptTicket(1, "control", "7", "prepare/7/session", 10),
        timeout_s=Decimal("40"), dispatch_check=AsyncMock(return_value=None),
        session_setup_timeout_s=Decimal("30"))
    assert (await driver.prepare_capture(request)).outcome.error is None
    assert runtime.session_setup_budget(binding) == 0
    assert factory.preparation_leads["camera"] == Decimal("10")

    driver.begin_session()
    assert runtime.session_setup_budget(binding) == Decimal("30")
    assert factory.preparation_leads["camera"] == Decimal("40")

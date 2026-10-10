"""双相机候选隔离及正常装配使用同一份完整契约。"""

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from camctl.acceptance.service import PlanDisposition
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.contracts.enums import enum_for
from camctl.devices.catalog import DriverDefinition, DriverDefinitions, build_catalog
from camctl.devices.drivers.adb_cameras.commands import CameraModel
from camctl.devices.drivers.adb_cameras.definitions import candidate_capabilities

from ..acceptance.test_acceptance import _accept, _plan_body, environment
from ..acceptance.test_adb_camera_definitions import _params
from .adb_camera_fixtures import software_contract
from camctl.devices.drivers.adb_cameras import registration
from camctl.devices.drivers.adb_cameras.contracts import CameraCall
from camctl.devices.drivers import runtime as drivers_runtime
from camctl.devices import definitions_runtime
from camctl.acceptance.schema import RuleError
from camctl.devices.drivers.adb_cameras.driver import AdbCameraDriver
from camctl.devices.bindings import DeviceBinding
from camctl.devices.ports import ControlRequest
from camctl.devices.drivers.registry import CapabilityNotDeclaredError
from camctl.operations.models import AttemptTicket, EffectState, SettlementBasis
from camctl.operations.process import LocalExit, RawToolOutcome
from decimal import Decimal


@pytest.fixture
def isolated_registration(monkeypatch):
    monkeypatch.setattr(definitions_runtime, "_definitions", {})
    monkeypatch.setattr(drivers_runtime, "_entries", {})


@pytest.mark.parametrize("model", list(CameraModel))
def test_static_and_runtime_registration_share_complete_contract_and_refresh_binding(isolated_registration, monkeypatch, model):
    contract = software_contract(model)
    monkeypatch.setattr(registration, "builtin_camera_contracts", lambda: (contract,))
    def config(serial):
        return load_config({"devices": {"cam-1": {"kind": "camera", "driver": model,
                           "adb": {"serial": serial}}}}, ConfigDefaults())
    original, current = config("serial-1"), config("serial-2")
    registration.register_builtin_camera_drivers(original)
    entry = drivers_runtime.current_registry().entry(model)
    described = build_catalog(original, definitions_runtime.current_driver_definitions()).describe_document()
    assert {action["type"] for action in described["devices"][0]["actions"]} == {"camera_record", "camera_timelapse"}
    assert entry.evidence is contract.evidence
    assert entry.declaration == contract.declaration
    assert entry.driver.contract is contract
    assert not entry.declaration.query_supported
    registration.register_builtin_camera_drivers(current)
    assert drivers_runtime.current_registry().entry(model) is entry
    assert entry.driver.devices is current.devices


@pytest.mark.parametrize("missing", [CameraCall.SETTING, CameraCall.START, CameraCall.RESULT, "task", "files", "evidence"])
def test_incomplete_contract_contents_are_filtered_even_with_task_factory(isolated_registration, monkeypatch, missing):
    from camctl.devices.evidence import EvidenceRegistry
    contract = software_contract(CameraModel.ACTION6)
    if isinstance(missing, CameraCall):
        contract = replace(contract, commands={kind: factory for kind, factory in contract.commands.items() if kind is not missing})
    else:
        contract = replace(contract, **{"task_factories": {} if missing == "task" else contract.task_factories,
            "file_tools": None if missing == "files" else contract.file_tools,
            "evidence": EvidenceRegistry(()) if missing == "evidence" else contract.evidence})
    monkeypatch.setattr(registration, "builtin_camera_contracts", lambda: (contract,))
    config = load_config({"devices": {"cam-1": {"kind": "camera", "driver": CameraModel.ACTION6}}}, ConfigDefaults())
    registration.register_builtin_camera_drivers(config)
    assert build_catalog(config, definitions_runtime.current_driver_definitions()).describe_document()["devices"][0]["actions"] == []


def test_collision_is_rejected_before_either_registry_changes(isolated_registration, monkeypatch):
    contracts = tuple(software_contract(model) for model in CameraModel)
    monkeypatch.setattr(registration, "builtin_camera_contracts", lambda: contracts)
    foreign = DriverDefinition(CameraModel.OSMO360II, {"camera_record": (candidate_capabilities(CameraModel.OSMO360II)[0],)})
    definitions_runtime.register_driver_definitions(foreign)
    with pytest.raises(RuleError):
        registration.register_builtin_camera_drivers(load_config(None, ConfigDefaults()))
    assert definitions_runtime.current_driver_definitions().drivers == {CameraModel.OSMO360II: foreign}
    assert drivers_runtime.current_registry().entry(CameraModel.ACTION6) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("model", list(CameraModel))
@pytest.mark.parametrize("operation", ["stop", "result", "query"])
async def test_driver_ports_use_original_ticket_and_explicit_capabilities(model, operation):
    calls = []
    class Transport:
        async def run(self, spec, stop):
            calls.append(spec)
            return RawToolOutcome(LocalExit(exit_code=0), b"OK", None, None, stderr=b"")
    contract = software_contract(model)
    driver = AdbCameraDriver(contract, {"cam-1": {"driver": model, "adb": {"serial": "serial-1"}}},
                            Transport(), terminate_grace_s=Decimal("1"), monotonic_ns=lambda: 0)
    request = ControlRequest("stop_recording" if operation == "stop" else operation,
        DeviceBinding("cam-1", model), {"activity_id": "7"},
        AttemptTicket(1, operation, "7", f"{operation}/3", 9), Decimal("5"))
    if operation == "query":
        with pytest.raises(CapabilityNotDeclaredError):
            await driver.query_state(request)
        assert not calls
        return
    result = await (driver.stop(request) if operation == "stop" else driver.list_results(request, 128))
    assert len(calls) == 1 and calls[0].argv[:5] == ("adb", "-s", "serial-1", "shell", "-T")
    assert calls[0].timeout_s == Decimal("5")
    assert result.outcome.effect is EffectState.CONFIRMED
    assert result.outcome.settlement.basis is SettlementBasis.OBSERVED
    assert result.observations[0].data["activity_id"] == "7"
    if operation == "result":
        assert result.observations[0].version == 2
        assert result.observations[0].data["set_finalized"] is True


@pytest.mark.parametrize("model", list(CameraModel))
def test_fresh_cli_registers_camera_without_device_io(tmp_path, model):
    config = tmp_path / "config.toml"
    config.write_text(f'[devices.cam-1]\nkind = "camera"\ndriver = "{model}"\n'
                      '[devices.cam-1.adb]\nserial = "explicit-serial"\n', encoding="utf-8")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[3] / "src")
    result = subprocess.run([sys.executable, "-m", "camctl", "describe", "--config", str(config)],
                            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    device = json.loads(result.stdout)["devices"][0]
    assert device["device_id"] == "cam-1" and device["driver_id"] == model
    assert [action["type"] for action in device["actions"]] == (
        ["camera_record"] if model == CameraModel.ACTION6 else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("model", list(CameraModel))
@pytest.mark.parametrize("action_type", ["camera_record", "camera_timelapse"])
async def test_pending_contract_is_not_described_or_accepted(environment, tmp_path, model, action_type):
    candidates = candidate_capabilities(model)
    config = load_config({"devices": {"cam-1": {"kind": "camera", "driver": model}}}, ConfigDefaults())
    catalog = build_catalog(config, DriverDefinitions({model: DriverDefinition(model, {
        capability.action_type: (capability,) for capability in candidates})}))
    described = catalog.describe_document()["devices"][0]["actions"]
    assert described == []
    assert not catalog.device_supports("cam-1", action_type)
    assert catalog.parameter_definition("cam-1", action_type, _params(model, action_type)["type"]) is None
    body = _plan_body()
    body["actions"][0].update(type=action_type, params=_params(model, action_type))
    connection, context = environment
    outcome = await _accept((connection, replace(context, catalog=catalog)), tmp_path, body)
    assert outcome.plan_disposition is PlanDisposition.REGISTERED
    assert connection.execute("SELECT status FROM actions").fetchall() == [(
        int(enum_for("actions.status").FAILED),)]
    assert connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (0,)

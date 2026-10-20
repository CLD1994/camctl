"""双相机候选隔离及正常装配使用同一份完整契约。"""

from dataclasses import replace
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import shlex

import pytest

from device_driver_fixtures import (
    synthetic_driver_declaration,
    synthetic_source_storage_resolver,
)

from camctl.acceptance.schema import RuleError
from camctl.acceptance.service import PlanDisposition
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.contracts.enums import enum_for
from camctl.devices import definitions_runtime
from camctl.devices.bindings import DeviceBinding
from camctl.devices.catalog import DriverDefinition, DriverDefinitions, build_catalog
from camctl.devices.adb_transport import DeviceCommand, InterpretedFacts
from camctl.devices.drivers import runtime as drivers_runtime
from camctl.devices.drivers.adb_cameras import registration
from camctl.devices.drivers.adb_cameras.commands import CameraModel
from camctl.devices.drivers.adb_cameras.contracts import CameraCall, CameraContract
from camctl.devices.drivers.adb_cameras.definitions import candidate_capabilities
from camctl.devices.drivers.adb_cameras.driver import AdbCameraDriver
from camctl.devices.drivers.adb_cameras.filesystem import ShellFileTools
from camctl.devices.drivers.adb_cameras.transport import shell_argv
from camctl.devices.drivers.registry import (
    CapabilityNotDeclaredError, DriverEntry, DriverStatus, port_for)
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import ControlRequest
from camctl.devices.tasks import CaptureStorage
from camctl.operations.models import AttemptTicket, EffectState, SettlementBasis
from camctl.operations.process import LocalExit, RawToolOutcome
from ..acceptance.test_acceptance import _accept, _plan_body, _owned, environment
from ..acceptance.test_adb_camera_definitions import _params
from .adb_camera_fixtures import software_contract


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
    catalog = build_catalog(original, definitions_runtime.current_driver_definitions())
    definition = definitions_runtime.current_driver_definitions().drivers[model]
    assert definition.source_storage_resolver(original.devices["cam-1"]) is CaptureStorage.DEVICE
    assert catalog.source_delete_supported("cam-1") is contract.declaration.delete_supported
    assert entry.evidence is contract.evidence
    assert entry.declaration == contract.declaration
    assert definitions_runtime.current_driver_definitions().drivers[model].declaration == entry.declaration
    assert entry.driver.contract is contract
    assert not entry.declaration.query_supported
    assert entry.declaration.file_query_supported
    assert entry.declaration.delete_supported
    registration.register_builtin_camera_drivers(current)
    assert drivers_runtime.current_registry().entry(model) is entry
    assert entry.driver.devices is current.devices


def test_builtin_adb_contracts_keep_device_source_and_declared_delete_value(isolated_registration):
    config = load_config({"devices": {
        "action6": {"kind": "camera", "driver": CameraModel.ACTION6,
                    "adb": {"serial": "serial-action6"}},
        "osmo": {"kind": "camera", "driver": CameraModel.OSMO360II,
                 "adb": {"serial": "serial-osmo"}},
    }}, ConfigDefaults())

    registration.register_builtin_camera_drivers(config)

    definitions = definitions_runtime.current_driver_definitions()
    catalog = build_catalog(config, definitions)
    for device_id, driver_id in (
        ("action6", CameraModel.ACTION6),
        ("osmo", CameraModel.OSMO360II),
    ):
        declaration = definitions.drivers[driver_id].declaration
        assert catalog.source_delete_supported(device_id) is declaration.delete_supported
    assert definitions.drivers[CameraModel.OSMO360II].declaration.delete_supported
    assert definitions.drivers[CameraModel.ACTION6].declaration.delete_supported


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
    foreign = DriverDefinition(CameraModel.OSMO360II,
        {"camera_record": (candidate_capabilities(CameraModel.OSMO360II)[0],)},
        synthetic_driver_declaration(), source_storage_resolver=synthetic_source_storage_resolver)
    definitions_runtime.register_driver_definitions(foreign)
    with pytest.raises(RuleError):
        registration.register_builtin_camera_drivers(load_config(None, ConfigDefaults()))
    assert definitions_runtime.current_driver_definitions().drivers == {CameraModel.OSMO360II: foreign}
    assert drivers_runtime.current_registry().entry(CameraModel.ACTION6) is None


def test_builtin_camera_contracts_declare_file_cleanup_independently_of_capture_query(isolated_registration):
    config = load_config(None, ConfigDefaults())

    registration.register_builtin_camera_drivers(config)

    for model in CameraModel:
        declaration = drivers_runtime.current_registry().entry(model).declaration
        assert not declaration.query_supported
    osmo = drivers_runtime.current_registry().entry(CameraModel.OSMO360II).declaration
    assert osmo.file_query_supported and osmo.delete_supported


@pytest.mark.asyncio
async def test_real_session_open_resets_settings_but_catalog_reads_do_not(isolated_registration, environment):
    from camctl.bootstrap.lifecycle import _initialize_recovery

    config = load_config({"devices": {"cam-1": {"kind": "camera", "driver": CameraModel.OSMO360II,
                           "adb": {"serial": "serial-1"}}}}, ConfigDefaults())
    registration.register_builtin_camera_drivers(config)
    driver = drivers_runtime.current_registry().entry(CameraModel.OSMO360II).driver
    commands = []

    class Transport:
        async def run(self, spec, stop):
            command = shlex.split(spec.argv[5])[2]
            commands.append(command)
            output = b"service connected\n" if command.startswith("simulate_device ") else b"Resp message, len = 1, data:\n 00\n"
            return RawToolOutcome(LocalExit(exit_code=0), output, None, None, stderr=b"")

    driver.transport = Transport()
    request = ControlRequest("start_recording", DeviceBinding("cam-1", CameraModel.OSMO360II),
        _params(CameraModel.OSMO360II, "camera_record"),
        AttemptTicket(1, "control", "7", "start/7", 9), Decimal("10"), lambda: None)
    assert (await driver.control(request)).error is None
    before = len(commands)
    registration.register_builtin_camera_drivers(config)
    build_catalog(config, definitions_runtime.current_driver_definitions()).describe_document()
    assert len(commands) == before
    assert (await driver.control(request)).error is None
    fixed = ("simulate_device -n DualGuiCmd 12", "simulate_device -s 0xa13cdabe 3",
             "simulate_device -s BaseFormat 6 69 0")
    assert all(commands.count(command) == 1 for command in fixed)
    _initialize_recovery(SimpleNamespace(work_files=None), _owned(environment))
    assert (await driver.control(request)).error is None
    assert all(commands.count(command) == 2 for command in fixed)
    action6 = drivers_runtime.current_registry().entry(CameraModel.ACTION6).declaration
    assert action6.file_query_supported and action6.delete_supported


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


@pytest.mark.asyncio
async def test_file_query_uses_query_ticket_without_enabling_capture_state_query():
    binding = DeviceBinding("cam-1", CameraModel.ACTION6)
    presence = EvidenceContract(
        "file_presence", 1, "query", frozenset({"cleanup_item_id", "present"}),
        identity_field="cleanup_item_id")
    returned = EvidenceContract("query_returned", 1, "query", frozenset())
    assumption = EvidenceContract(
        "query_foreground_recovery", 1, "query", frozenset({"terminate_grace_s"}))
    evidence = EvidenceRegistry((presence, returned, assumption))
    seen = []

    class Interpreter:
        def interpret(self, raw):
            return InterpretedFacts((DeviceObservation(
                "file_presence", 1, {"cleanup_item_id": "3", "present": False}),),
                None, EffectState.CONFIRMED)

    def file_query_factory(request, serial, argv, batch):
        seen.append((request, serial))
        return DeviceCommand(
            operation="query", argv=shell_argv(serial, "true"), binding=request.binding,
            timeout_s=request.timeout_s, terminate_grace_s=Decimal("1"), evidence=evidence,
            returned_contract=returned, assumption_contract=assumption,
            interpreter=Interpreter(), capability=CameraCall.FILE_QUERY.value)

    calls = []

    class Transport:
        async def run(self, spec, stop):
            calls.append(spec)
            return RawToolOutcome(LocalExit(exit_code=0), b"", None, None, stderr=b"")

    contract = CameraContract(
        CameraModel.ACTION6, commands={CameraCall.FILE_QUERY: file_query_factory},
        file_tools=ShellFileTools(), evidence=evidence,
        recovery_operations=frozenset({"query"}))
    driver = AdbCameraDriver(contract,
        {"cam-1": {"driver": CameraModel.ACTION6, "adb": {"serial": "serial-1"}}},
        Transport(), terminate_grace_s=Decimal("1"), monotonic_ns=lambda: 0)
    entry = DriverEntry(CameraModel.ACTION6, driver, contract.declaration, evidence,
        DriverStatus.SOFTWARE_CONTRACT_VERIFIED)
    ticket = AttemptTicket(1, "query", "3", "exists/3", 9)
    request = ControlRequest("query", binding, {
        "cleanup_item_id": "3", "file_id": "8",
        "identity_key": json.dumps(["cam-1", CameraModel.ACTION6, "/DCIM/3.OSV"]),
        "locator": {"path": "/DCIM/3.OSV"},
    }, ticket=ticket, timeout_s=Decimal("5"))

    assert entry.declaration.file_query_supported
    assert not entry.declaration.query_supported
    assert port_for(entry, "file_query") is driver
    with pytest.raises(CapabilityNotDeclaredError):
        port_for(entry, "query")

    result = await port_for(entry, "file_query").query_file(request)

    assert seen == [(request, "serial-1")]
    assert len(calls) == 1
    assert calls[0].argv[:5] == ("adb", "-s", "serial-1", "shell", "-T")
    assert result.observations == (DeviceObservation(
        "file_presence", 1, {"cleanup_item_id": "3", "present": False}),)


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
    expected_actions = {
        CameraModel.ACTION6: ["camera_record", "camera_timelapse"],
        CameraModel.OSMO360II: ["camera_record", "camera_timelapse"],
    }
    assert [action["type"] for action in device["actions"]] == expected_actions[model]


@pytest.mark.asyncio
@pytest.mark.parametrize("model", list(CameraModel))
@pytest.mark.parametrize("action_type", ["camera_record", "camera_timelapse"])
@pytest.mark.parametrize("estimate_present", [False, True])
async def test_pending_contract_is_not_described_or_accepted(environment, tmp_path, model, action_type, estimate_present):
    candidates = tuple(replace(cap, video_size_estimate=cap.video_size_estimate if estimate_present else None)
                       for cap in candidate_capabilities(model))
    assert all(cap.task_factory is None for cap in candidates)
    config = load_config({"devices": {"cam-1": {"kind": "camera", "driver": model}}}, ConfigDefaults())
    contract = software_contract(model)
    catalog = build_catalog(config, DriverDefinitions({model: DriverDefinition(model, {
        capability.action_type: (capability,) for capability in candidates},
        contract.declaration, source_storage_resolver=contract.source_storage_resolver)}))
    described = catalog.describe_document()["devices"][0]["actions"]
    assert described == []
    assert not catalog.device_supports("cam-1", action_type)
    assert catalog.parameter_definition("cam-1", action_type, _params(model, action_type)["type"]) is None
    body = _plan_body()
    body["actions"][0].update(type=action_type, params=_params(model, action_type))
    if action_type == "camera_timelapse":
        body["actions"][0]["params"].update(interval_s=1, duration_s=300)
    connection, context = environment
    outcome = await _accept((connection, replace(context, catalog=catalog)), tmp_path, body)
    assert outcome.plan_disposition is PlanDisposition.REGISTERED
    assert connection.execute("SELECT status FROM actions").fetchall() == [(
        int(enum_for("actions.status").FAILED),)]
    assert connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (0,)

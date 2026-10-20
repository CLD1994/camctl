from pathlib import Path
from decimal import Decimal

import pytest

from camctl.bootstrap.capture_assembly import session_capture_assembly
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
from camctl.devices.drivers.hasselblad.driver import EVIDENCE, HasselbladDriver
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.host_files.models import BoundDirectories
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.repositories.host_capture import HostCaptureRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing


@pytest.mark.parametrize("first,current", [("host", "device"), ("device", "host")])
def test_hasselblad_assembled_start_grant_keeps_original_storage_and_deadline(tmp_path, first, current):
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import CommandMode
    from camctl.contracts.values import new_operation_key
    from camctl.devices.catalog import DriverDefinitions, build_catalog
    from camctl.devices.drivers.hasselblad.definitions import driver_definition
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput, register_acceptance_guards
    from camctl.persistence.repositories.capture import register_capture_guards
    from camctl.persistence.repositories.operations import register_operation_guards
    from camctl.persistence.repositories.outputs import register_outputs_guards
    from camctl.persistence.repositories.scheduling import (
        ObserveWindowRequest, SchedulingRepository, StartActionRequest, register_window_guard,
    )

    register_acceptance_guards()
    register_capture_guards()
    register_operation_guards()
    register_outputs_guards()
    register_window_guard()
    raw = {
        "paths": {name: str(tmp_path / filename) for name, filename in (
            ("state_db", "state.db"), ("log_file", "camctl.log"), ("staging", "staging"),
            ("ready", "ready"), ("processing", "processing"),
        )},
        "devices": {"cam-1": {"kind": "camera", "driver": DRIVER_ID, "hasselblad": {
            "executable": "/usr/bin/hb_cli", "serial": "SERIAL-1", "output_storage": first,
        }}},
    }
    config = load_config(raw, ConfigDefaults())
    initialized = initialize_state(config, Path(config.paths.state_db))
    assert initialized.outcome is InitOutcome.CREATED, initialized.detail
    owned = open_existing(Path(config.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
    now = 1_750_000_000_000_000
    try:
        accepted = AcceptanceRepository().process_input(ProcessInput(
            ParsedInput("photo.json", {"request_id": "1", "created_at": "2025-06-15 15:06:40",
                "name": "单张", "actions": [{"name": "拍摄", "type": "camera_take_photo",
                    "device_id": "cam-1", "scheduled_at": "2025-06-15 15:06:40",
                    "params": {"type": "hasselblad_photo", "capture_format": "raw",
                        "exposure": {"mode": "P", "iso": 100},
                        "white_balance": {"mode": "auto"}, "focus": {"mode": "MF"}}, "policy": {"max_delay_ms": 5000}}]}),
            build_catalog(config, DriverDefinitions({DRIVER_ID: driver_definition()})), CommandMode.RUN, now,
        ), new_operation_key(), owned)
        assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
        scheduling = SchedulingRepository()
        for save, request in (
            (scheduling.observe_window, ObserveWindowRequest(1, now, now)),
            (scheduling.start_action, StartActionRequest(1, now, now)),
        ):
            saved = save(request, new_operation_key(), owned)
            assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        raw["devices"]["cam-1"]["hasselblad"]["output_storage"] = current
        changed = load_config(raw, ConfigDefaults())
        driver = HasselbladDriver(changed.devices, BoundDirectories(Path(changed.paths.staging)),
            monotonic_ns=lambda: 1)
        entry = DriverEntry(driver_id=DRIVER_ID, driver=driver, declaration=driver.declaration,
            evidence=EVIDENCE, status=DriverStatus.SOFTWARE_CONTRACT_VERIFIED)
        runtime = session_capture_assembly(devices=changed.devices, drivers=DriverRegistry((entry,)),
            staging=Path(changed.paths.staging), wait_config=lambda _: None)(owned, "cam-1")
        runtime.wall_us = lambda: now
        action = runtime.action(1)
        assert action["execution_spec_json"] == {"output_storage": first, "start_call_timeout_s": 180}
        ticket, reason = runtime.grant(action)
        assert ticket is not None, reason
        timeout, = owned.connection.execute(
            "SELECT timeout_s_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id),
        ).fetchone()
        assert Decimal(str(timeout)) == Decimal("180")
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_hasselblad_assembly_keeps_local_host_recovery_without_result_port(tmp_path):
    config = load_config({
        "paths": {
            "state_db": str(tmp_path / "state.db"),
            "log_file": str(tmp_path / "camctl.log"),
            "staging": str(tmp_path / "staging"),
            "ready": str(tmp_path / "ready"),
            "processing": str(tmp_path / "processing"),
        },
        "devices": {"cam-1": {
            "kind": "camera",
            "driver": DRIVER_ID,
            "hasselblad": {"executable": "/usr/bin/hb_cli", "serial": "SERIAL-1"},
        }},
    }, ConfigDefaults())
    initialized = initialize_state(config, Path(config.paths.state_db))
    assert initialized.outcome is InitOutcome.CREATED, initialized.detail
    owned = open_existing(Path(config.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())

    driver = HasselbladDriver(
        config.devices, BoundDirectories(Path(config.paths.staging)),
        monotonic_ns=lambda: 1,
    )
    # Assembly only resolves the declared control port. The default transport
    # is not called, and the omitted result capability remains an ordinary None.
    entry = DriverEntry(
        driver_id=DRIVER_ID,
        driver=driver,
        declaration=driver.declaration,
        evidence=EVIDENCE,
        status=DriverStatus.SOFTWARE_CONTRACT_VERIFIED,
    )
    wait_config = lambda _action: None
    try:
        runtime = session_capture_assembly(
            devices=config.devices,
            drivers=DriverRegistry((entry,)),
            staging=Path(config.paths.staging),
            wait_config=wait_config,
        )(owned, "cam-1")
        assert runtime is not None
        assert runtime.results is None
        assert runtime.roots == BoundDirectories(Path(config.paths.staging))
        assert isinstance(runtime.host_capture, HostCaptureRepository)
        assert runtime.host_photo_operation_verifier is not None

        # A reconnect session can lose the current device declaration after
        # the saved START. The binding-only runtime still carries local file
        # roots and repository to resume the original archive transaction.
        binding_only = session_capture_assembly(
            devices={},
            drivers=DriverRegistry((entry,)),
            staging=Path(config.paths.staging),
            wait_config=wait_config,
        )(owned, "cam-1")
        assert binding_only.driver is None
        assert binding_only.results is None
        assert binding_only.roots == BoundDirectories(Path(config.paths.staging))
        assert isinstance(binding_only.host_capture, HostCaptureRepository)
        assert binding_only.host_photo_operation_verifier is not None
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_runtime_deps_shares_one_start_entry_coordinator_with_every_capture_and_cancel_factory(tmp_path,monkeypatch):
    from camctl.bootstrap import flows
    from .test_capture_call_session_state import _session_factories
    from ..capture.test_recording_start_runtime import _result
    actual=flows.cancel_flow
    entries=[]
    def observed(**kwargs):
        entries.append(kwargs['start_entries'])
        return actual(**kwargs)
    monkeypatch.setattr(flows,'cancel_flow',observed)
    owned,driver,wall,mono,ordinary,residual,restricted=await _session_factories(tmp_path,monkeypatch,_result(confirmed=True))
    try:
        shared=ordinary(owned,'cam-1').start_entries
        assert residual(owned,'cam-1').start_entries is shared
        assert restricted(owned,'cam-1').start_entries is shared
        assert ordinary(owned,'missing').start_entries is shared
        assert len(entries)==2 and all(value is shared for value in entries)
    finally:
        owned.connection.close()

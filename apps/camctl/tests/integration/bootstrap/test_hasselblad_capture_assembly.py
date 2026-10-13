from pathlib import Path

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

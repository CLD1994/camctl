"""拍摄装配共享会话恢复边界，不从当前设备绑定推测原调用依据。"""

from pathlib import Path
from unittest.mock import create_autospec

import pytest

from camctl.bootstrap.capture_assembly import execution_wait_config, session_capture_assembly
from camctl.capture.handlers import PendingStartResult, ResultFilesPort
from camctl.capture.recovery import RecoveryBoundary
from camctl.devices.bindings import DeviceBinding, DeviceConfigurationError
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.devices.ports import ControlDriver, DriverDeclaration


def _entry(identity, *, recovery_enabled=False):
    return DriverEntry(
        driver_id=identity, driver=create_autospec(ControlDriver, instance=True, spec_set=True),
        declaration=DriverDeclaration(
            control_supported=True, stop_supported=False, query_supported=False,
            result_supported=False, read_supported=False, digest_supported=False,
            delete_supported=False,
            adb_foreground_recovery_operations=(frozenset({"control"}) if recovery_enabled else frozenset())),
        evidence=EvidenceRegistry((EvidenceContract(
            "adb_foreground_recovery", 1, "control", frozenset()),) if recovery_enabled else ()),
        status=DriverStatus.SOFTWARE_CONTRACT_VERIFIED)


@pytest.mark.parametrize("current", ["missing", "mismatch"])
def test_capture_factory_passes_fixed_session_recovery_boundary(current):
    original = _entry("original")
    alternate = _entry("alternate")
    fixed_boundary = 69
    factory = session_capture_assembly(
        devices={} if current == "missing" else {"cam-1": {"driver": "alternate"}},
        drivers=DriverRegistry((original, alternate)),
        results=create_autospec(ResultFilesPort, instance=True, spec_set=True),
        staging=Path("/staging"), wait_config=execution_wait_config, media_enabled=False,
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
        recovery_max_event_id=lambda: fixed_boundary,
    )

    runtime = factory(object(), "cam-1")

    assert runtime.recovery_boundary is RecoveryBoundary.HOST_LOCAL_SETTLED
    assert runtime.recovery_max_event_id == fixed_boundary
    assert runtime.recovery_evidence_for(DeviceBinding("cam-1", "original"), "control") is None


def test_capture_factory_unknown_driver_is_configuration_error():
    factory = session_capture_assembly(
        devices={"cam-1": {"driver": "unregistered-driver"}},
        drivers=DriverRegistry(()),
        results=create_autospec(ResultFilesPort, instance=True, spec_set=True),
        staging=Path("/staging"), wait_config=execution_wait_config, media_enabled=False)

    with pytest.raises(DeviceConfigurationError) as caught:
        factory(object(), "cam-1")

    assert "cam-1" in str(caught.value)
    assert "unregistered-driver" in str(caught.value)


@pytest.mark.parametrize("current", ["normal", "missing"])
def test_capture_factory_preserves_local_recovery_diagnostic_consumer(current):
    records = []
    emit = records.append
    factory = session_capture_assembly(
        devices={"cam-1": {"driver": "original"}} if current == "normal" else {},
        drivers=DriverRegistry((_entry("original"),)),
        results=create_autospec(ResultFilesPort, instance=True, spec_set=True),
        staging=Path("/staging"), wait_config=execution_wait_config, media_enabled=False,
        on_recovery_diagnostic=emit)

    runtime = factory(object(), "cam-1")

    assert runtime.on_recovery_diagnostic is emit


@pytest.mark.parametrize("current", ["missing", "mismatch"])
def test_capture_factory_selects_recovery_evidence_from_original_driver(current):
    factory = session_capture_assembly(
        devices={} if current == "missing" else {"cam-1": {"driver": "alternate"}},
        drivers=DriverRegistry((_entry("original", recovery_enabled=True), _entry("alternate"))),
        results=create_autospec(ResultFilesPort, instance=True, spec_set=True),
        staging=Path("/staging"), wait_config=execution_wait_config, media_enabled=False)
    runtime = factory(object(), "cam-1")

    evidence = runtime.recovery_evidence_for(DeviceBinding("cam-1", "original"), "control")

    assert evidence.contract("adb_foreground_recovery", 1) == EvidenceContract(
        "adb_foreground_recovery", 1, "control", frozenset())
    assert runtime.recovery_evidence_for(DeviceBinding("cam-1", "alternate"), "control") is None


@pytest.mark.parametrize("path", ["normal", "missing", "normal_to_missing"])
def test_capture_factory_preserves_actual_pending_start_result_between_rounds(path):
    devices = {} if path == "missing" else {"cam-1": {"driver": "original"}}
    factory = session_capture_assembly(
        devices=devices, drivers=DriverRegistry((_entry("original"),)),
        results=create_autospec(ResultFilesPort, instance=True, spec_set=True),
        staging=Path("/staging"), wait_config=execution_wait_config, media_enabled=False)
    first = factory(object(), "cam-1")
    pending = create_autospec(PendingStartResult, instance=True, spec_set=True)
    first.pending_start_results[(1, 1)] = pending

    next_round = factory(object(), "cam-2" if path == "normal_to_missing" else "cam-1")

    assert next_round.pending_start_results[(1, 1)] is pending

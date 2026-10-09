"""会话装配消费本次等待配置并共享未完成等待。"""

from pathlib import Path
from unittest.mock import create_autospec

from camctl.bootstrap.capture_assembly import execution_wait_config, session_capture_assembly
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.capture.handlers import ResultFilesPort
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.evidence import EvidenceRegistry
from camctl.devices.ports import ControlDriver, DriverDeclaration


def _factory():
    config = load_config({"devices": {"cam-1": {
        "kind": "camera", "driver": "driver-1", "capture": {"extra_wait_ms": 3000},
    }}}, ConfigDefaults())
    driver = create_autospec(ControlDriver, instance=True)
    entry = DriverEntry(
        driver_id="driver-1", driver=driver,
        declaration=DriverDeclaration(control_supported=True, stop_supported=False,
            query_supported=False, result_supported=False, read_supported=False,
            digest_supported=False, delete_supported=False),
        evidence=EvidenceRegistry(()), status=DriverStatus.SOFTWARE_CONTRACT_VERIFIED)
    return session_capture_assembly(
        devices=config.devices, drivers=DriverRegistry((entry,)),
        results=create_autospec(ResultFilesPort, instance=True), staging=Path("/staging"),
        wait_config=execution_wait_config)


def test_current_device_extra_wait_is_added_to_fixed_execution_definition():
    runtime = _factory()(object(), "cam-1")
    config = runtime.wait_config({"execution_spec_json": {
        "target_duration_ms": 600000, "result_wait_margin_ms": 2000,
    }})
    assert (config.target_duration_ms, config.driver_margin_ms, config.extra_wait_ms) == (
        600000, 2000, 3000)


def test_same_session_reuses_unfinished_wait_deadline_between_advances():
    factory = _factory()
    first = factory(object(), "cam-1")
    first.timelapse_deadlines[7] = 123456789
    second = factory(object(), "cam-1")
    assert second.timelapse_deadlines == {7: 123456789}

"""会话装配把本次录像和结果核实预算交给真实执行运行时。"""

from decimal import Decimal
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from camctl.bootstrap.capture_assembly import execution_wait_config, session_capture_assembly
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.capture.handlers import ResultFilesPort
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.evidence import EvidenceRegistry
from camctl.devices.ports import ControlDriver, DriverDeclaration
from camctl.operations.attempts import AttemptConfig
from camctl.persistence.repositories.scheduling import SchedulingRepository
from camctl.scheduling.rules import LaunchWindow


def _runtime(device_settings):
    config = load_config({"devices": {"cam-1": {
        "kind": "camera", "driver": "driver-1", **device_settings,
    }}}, ConfigDefaults())
    driver = create_autospec(ControlDriver, instance=True)
    entry = DriverEntry(
        driver_id="driver-1", driver=driver,
        declaration=DriverDeclaration(
            control_supported=True, stop_supported=False, query_supported=False,
            result_supported=False, read_supported=False, digest_supported=False,
            delete_supported=False),
        evidence=EvidenceRegistry(()), status=DriverStatus.SOFTWARE_CONTRACT_VERIFIED)
    factory = session_capture_assembly(
        devices=config.devices, drivers=DriverRegistry((entry,)),
        results=create_autospec(ResultFilesPort, instance=True), staging=Path("/staging"),
        wait_config=execution_wait_config, wall_us=lambda: 1000, media_enabled=False)
    return factory(object(), "cam-1")


@pytest.mark.parametrize("field", ["start_config", "stop_config", "check_config"])
def test_omitted_capture_execution_config_uses_defined_budget(field):
    actual = getattr(_runtime({}), field)
    assert actual == AttemptConfig(
        max_attempts=3, timeout_s=Decimal("10"), retry_interval_s=Decimal("3"))


@pytest.mark.parametrize("field,settings,expected", [
    ("start_config", {"recording": {
        "max_start_attempts": 5, "start_timeout_s": "1.25", "start_retry_interval_s": "0.5",
    }}, AttemptConfig(5, Decimal("1.25"), Decimal("0.5"))),
    ("stop_config", {"recording": {
        "max_stop_attempts": 7, "stop_timeout_s": "2.75", "stop_retry_interval_s": "0",
    }}, AttemptConfig(7, Decimal("2.75"), Decimal("0"))),
    ("check_config", {"result_check": {
        "max_attempts": 9, "call_timeout_s": "0.125", "retry_interval_s": "4.5",
    }}, AttemptConfig(9, Decimal("0.125"), Decimal("4.5"))),
])
def test_current_capture_execution_config_reaches_runtime(field, settings, expected):
    assert getattr(_runtime(settings), field) == expected


def test_recording_grant_saves_current_start_budget():
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.persistence.repositories.scheduling import GrantOutcome, GrantResult

    runtime = _runtime({"recording": {
        "max_start_attempts": 6, "start_timeout_s": "0.375", "start_retry_interval_s": "1.5",
    }})
    runtime.scheduling = create_autospec(SchedulingRepository, instance=True)
    ticket = object()
    runtime.scheduling.grant_start.return_value = DbOutcome(
        kind=DbOutcomeKind.COMPLETED, value=GrantResult(outcome=GrantOutcome.GRANTED, ticket=ticket))
    runtime.window_of = lambda action: LaunchWindow(1000, 2000)

    actual_ticket, reason = runtime.grant({"id": 12, "device_id": "cam-1", "type": 2})

    assert actual_ticket is ticket
    assert reason is None
    request = runtime.scheduling.grant_start.call_args.args[0]
    assert request.config == AttemptConfig(6, Decimal("0.375"), Decimal("1.5"))

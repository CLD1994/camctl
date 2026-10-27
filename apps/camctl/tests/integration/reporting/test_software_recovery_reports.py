"""旧格式完整报告链及软件恢复设备结束后的冻结报告重建。"""
from dataclasses import replace
from decimal import Decimal
import json
from unittest.mock import create_autospec

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode
from camctl.bootstrap.capture_assembly import execution_wait_config, session_capture_assembly
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.capture.handlers import capture_handler
from camctl.capture.software_recovery import advance_recovery
from camctl.contracts.schemas import validate_document
from camctl.contracts.values import new_operation_key
from camctl.devices.drivers.adb_cameras.action6_record import action6_record_contract
from camctl.devices.drivers.adb_cameras.commands import CameraModel
from camctl.devices.drivers.adb_cameras.driver import AdbCameraDriver
from camctl.devices.drivers.adb_cameras.firmware_identity import RecoveryQualification
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry
from camctl.devices.tasks import VideoResultMode
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.directory_switch import canonical_to_path
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput
from camctl.persistence.runtime import open_existing, DbOpenMode, DbConfig
from camctl.reporting.generation import generate_report_file
from camctl.reporting.policy import record_report_failure
from ..acceptance.test_acceptance import _plan_body
from ..acceptance.test_adb_camera_definitions import _catalog, _params
from ..capture.test_baseline_history import _fix
from ..capture.test_capture_contract import ResultsDouble
from ..capture.test_software_recovery import Transport, _FINGERPRINT
from ..scheduling.test_start_action import owned, _SCHEDULED, _start
from .test_generation import _freeze, _spec

pytestmark = pytest.mark.asyncio


def action(document):
    return next(item for plan in document["plans"] for item in plan["actions"] if item["action_instance_id"] == "1")


async def failed_world(owned, tmp_path):
    catalog = _catalog("dji-action6")
    for request_id in ("42", "43"):
        body = _plan_body(request_id=request_id)
        body["actions"][0].update(type="camera_record", params=_params("dji-action6", "camera_record"))
        if request_id == "43":
            body["actions"][0]["scheduled_at"] = "2026-01-15 09:00:10"
        accepted = AcceptanceRepository().process_input(
            ProcessInput(ParsedInput("record.json", body), catalog, CommandMode.RUN, _SCHEDULED),
            new_operation_key(), owned)
        assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
    started = _start(owned, 1, now=_SCHEDULED)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    activity = started.value.activity_id
    fixed = _fix(owned, activity)
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    transport = Transport(owned.connection)
    devices = {"cam-1": {"kind": "camera", "driver": "dji-action6",
        "adb": {"serial": "camera-serial"},
        "recording": {"stop_timeout_s": Decimal("0.001"), "max_stop_attempts": 1},
        "software_recovery": {"enabled": True, "retry_interval_s": 0}}}
    config = load_config({"devices": devices}, ConfigDefaults())
    clock = {"wall": _SCHEDULED, "monotonic": 0}
    driver = AdbCameraDriver(action6_record_contract(), config.devices, transport,
        terminate_grace_s=Decimal("1"), monotonic_ns=lambda: clock["monotonic"])
    driver.recovery_qualification = create_autospec(driver.recovery_qualification,
        return_value=RecoveryQualification(CameraModel.ACTION6, _FINGERPRINT, 2,
            VideoResultMode.SINGLE_FILE, "software-test-fixture"))
    entry = DriverEntry("dji-action6", driver, driver.declaration, driver.contract.evidence, driver.contract.status)
    factory = session_capture_assembly(
        devices=config.devices, drivers=DriverRegistry((entry,)), results=ResultsDouble({}),
        staging=canonical_to_path(owned.metadata.staging_path), wait_config=execution_wait_config,
        wall_us=lambda: clock["wall"], monotonic_ns=lambda: clock["monotonic"], media_enabled=False)
    database = await BusinessDatabase.open(lambda: open_existing(
        tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig()))
    try:
        runtime = await factory(database, "cam-1")
        await capture_handler("camera_record")(1, runtime)
        clock.update(wall=_SCHEDULED + 10_000_000, monotonic=10_000_000_000)
        await capture_handler("camera_record")(1, runtime)
        await capture_handler("camera_record")(1, runtime)
        assert (await runtime.action(1))["status"] == 4
        return runtime, driver, transport, activity, factory
    except BaseException:
        await database.close()
        raise


@pytest.mark.parametrize("database_format", [2, 3])
async def test_legacy_reports_and_recovery_preserve_original_failure_and_frozen_bytes(owned, tmp_path, database_format):
    runtime, driver, transport, activity, factory = await failed_world(owned, tmp_path)
    active = owned
    try:
        if database_format == 2:
            await runtime.database.close()
            owned.connection.execute("UPDATE database_metadata SET format_version=2")
            owned.connection.execute("DROP TABLE software_recoveries")
            active = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
            database = await BusinessDatabase.open(lambda: open_existing(
                tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig()))
            try:
                runtime = await factory(database, "cam-1")
            except BaseException:
                await database.close()
                raise
            transport.connection = active.connection
        before = _freeze(active)
        before_spec = _spec(tmp_path, before, name="staging/before-recovery.json")
        before_file = generate_report_file(before_spec)
        before_bytes = before_file.path.read_bytes()
        document = json.loads(before_bytes)
        validate_document("protocol/status-report.schema.json", document)
        original = action(document)
        assert original["status"] == "failed"
        assert original["error"]["code"] == "recording_stop_failed"
        assert original["device_execution"]["status"] == "still_running"
        if database_format == 2:
            # 旧库完整报告和重建无需新表；新恢复功能不隐式升级。
            await advance_recovery(runtime, activity, await runtime.action(2))
            assert transport.reboots == 0
            assert active.connection.execute("SELECT format_version FROM database_metadata").fetchone() == (2,)
            assert active.connection.execute("SELECT 1 FROM sqlite_master WHERE name='software_recoveries'").fetchone() is None
        else:
            failed = record_report_failure(new_operation_key(), active, before.report_id,
                {"code": "controlled_report_failure", "stage": "report", "details": {}}, occurred_at=runtime.wall_us())
            assert failed.kind is DbOutcomeKind.COMPLETED, failed.error
            for _ in range(8): await advance_recovery(runtime, activity, await runtime.action(2))
            assert transport.reboots == 1
            after = _freeze(active)
            after_file = generate_report_file(_spec(tmp_path, after, name="staging/after-recovery.json"))
            after_document = json.loads(after_file.path.read_bytes())
            validate_document("protocol/status-report.schema.json", after_document)
            actual = action(after_document)
            assert actual["status"] == original["status"]
            assert actual["error"] == original["error"]
            assert "device_execution" not in actual
        rebuilt = generate_report_file(replace(before_spec, staging_path=tmp_path / "staging/rebuilt-before.json"))
        assert rebuilt.path.read_bytes() == before_bytes
    finally:
        await runtime.database.close()
        if active is not owned:
            active.connection.close()

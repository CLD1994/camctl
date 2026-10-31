"""旧格式完整报告链及软件恢复设备结束后的冻结报告重建。"""
from dataclasses import replace
import json

import pytest

from camctl.contracts.schemas import validate_document
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.runtime import open_existing, DbOpenMode, DbConfig
from camctl.reporting.generation import generate_report_file
from camctl.reporting.policy import record_report_failure
from camctl.capture.residual import pass_residual_gate
from ..support.capture import capture_runtime
from ..support.baseline_cases import baseline_case
from camctl.persistence.transaction import row_facts
from ..capture.test_software_recovery import failed_world
from ..scheduling.test_start_action import owned
from .test_generation import _freeze, _spec

pytestmark = pytest.mark.asyncio


def action(document):
    return next(item for plan in document["plans"] for item in plan["actions"] if item["action_instance_id"] == "1")


@pytest.mark.parametrize("database_format", [2, 3])
async def test_legacy_reports_and_recovery_preserve_original_failure_and_frozen_bytes(owned, tmp_path, database_format, capture_resources, baseline_case):
    runtime, driver, transport, activity = await failed_world(owned, tmp_path)
    active = owned
    if database_format == 2:
        # 结束旧会话拥有的文件责任和数据库线程后，再改变格式并重开。
        await baseline_case.drain()
        await capture_resources.close()
        owned.connection.execute("UPDATE database_metadata SET format_version=2")
        owned.connection.execute("DROP TABLE software_recoveries")
        active = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
        database = await capture_resources.database(tmp_path / "state.db")
        from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
        registry = DriverRegistry((DriverEntry("dji-action6", driver,
            replace(driver.declaration, query_supported=False), driver.contract.evidence,
            DriverStatus.SOFTWARE_CONTRACT_VERIFIED),))
        runtime = await capture_runtime(database, driver=driver,
            evidence=driver.contract.evidence, drivers=registry,
            devices={"cam-1": {"kind": "camera", "driver": "dji-action6",
                "adb": {"serial": "camera-serial"},
                "software_recovery": {"enabled": True, "retry_interval_s": "0"}}},
            wall_us=runtime.wall_us, monotonic_ns=runtime.monotonic_ns)
        transport.connection = active.connection
    try:
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
            await pass_residual_gate(runtime, row_facts(active.connection, "actions", 2))
            assert transport.reboots == 0
            assert active.connection.execute("SELECT format_version FROM database_metadata").fetchone() == (2,)
            assert active.connection.execute("SELECT 1 FROM sqlite_master WHERE name='software_recoveries'").fetchone() is None
        else:
            failed = record_report_failure(new_operation_key(), active, before.report_id,
                {"code": "controlled_report_failure", "stage": "report", "details": {}}, occurred_at=runtime.wall_us())
            assert failed.kind is DbOutcomeKind.COMPLETED, failed.error
            for _ in range(8):
                await pass_residual_gate(runtime, row_facts(active.connection, "actions", 2))
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
        if active is not owned:
            active.connection.close()

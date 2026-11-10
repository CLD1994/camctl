"""正式拍摄处理器、SQLite 历史及真实 ADB 驱动的重启恢复组合。"""
from dataclasses import replace
from decimal import Decimal
from datetime import datetime, timezone
import json
from unittest.mock import create_autospec

import pytest

from camctl.capture.handlers import capture_handler
from camctl.capture.residual import pass_residual_gate
from camctl.devices.drivers.adb_cameras.action6_record import action6_record_contract
from camctl.devices.drivers.adb_cameras.driver import AdbCameraDriver
from camctl.operations.attempts import AttemptConfig
from camctl.operations.process import LocalExit, RawToolOutcome
from camctl.persistence.repositories.transactions import row_facts
from camctl.persistence.models import DbOutcomeKind
from .test_baseline_start import _prepared, _accept_plan
from ..support.baseline_cases import (baseline_case, baseline_case_for,
    advance_existing_recovery, saved_recovery, request_baseline_cancel, RecoveryCommitFault)
from camctl.bootstrap.capture_assembly import execution_wait_config
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from .test_baseline_history import _fix
from ..scheduling.test_start_action import owned, _SCHEDULED, _start
from ..acceptance.test_acceptance import _plan_body
from ..acceptance.test_adb_camera_definitions import _catalog, _params

pytestmark = pytest.mark.asyncio
_BEFORE = "00000000-0000-4000-8000-000000000001"
_AFTER = "00000000-0000-4000-8000-000000000002"
from camctl.devices.drivers.adb_cameras.firmware_identity import firmware_fingerprint, RecoveryQualification
from camctl.devices.drivers.adb_cameras.commands import CameraModel
from camctl.devices.tasks import VideoResultMode
_FIRMWARE_SAMPLE = {"Version": "v1.2.3.4", "Metabuild_Info": {
    "Meta_Build_ID": "software-test-build", "Product_Flavor": "asic", "Time_Stamp": "2026-10-20 19:32:30"}}
_FINGERPRINT = firmware_fingerprint("dji-action6", json.dumps(_FIRMWARE_SAMPLE).encode())


class Transport:
    """受 ManagedTransport 约束；仅替换实际 ADB 工具执行。"""
    def __init__(self, connection):
        self.connection = connection
        self.boot = _BEFORE
        self.reboots = 0
        self.commands = []
        self.prepare_ready = True
        self.directory_ready = True
        self.new_boot = True
        self.identity_serial = "camera-serial"
        self.firmware_version = "v1.2.3.4"
        self.interrupt_reboot = False
        self.directory_frames = []

    async def run(self, spec, stop):
        self.commands.append(spec.argv)
        assert spec.timeout_s is not None and spec.timeout_s > 0
        script = spec.argv[-1]
        if spec.timeout_s == Decimal("0.001"):
            return RawToolOutcome(LocalExit(signal=9), b"", "timeout", Decimal("1"), stderr=b"")
        if spec.argv[-1] == "reboot":
            saved = self.connection.execute("SELECT phase,state_json FROM software_recoveries").fetchone()
            assert saved is not None
            from camctl.contracts.enums import enum_for
            assert saved[0] == int(enum_for("software_recoveries.phase").INTENT)
            assert json.loads(saved[1])["reboots"] == 1
            self.reboots += 1
            if self.new_boot:
                self.boot = _AFTER
            if self.interrupt_reboot:
                raise RuntimeError("已可能派发，主机在保存返回前中断")
            output = b""
        elif "/proc/sys/kernel/random/boot_id" in script:
            document = {**_FIRMWARE_SAMPLE, "Version": self.firmware_version}
            output = (b"CAMCTL-RECOVERY-IDENTITY/1\n" + self.boot.encode()
                + b"\nCAMCTL-FIRMWARE\n" + json.dumps(document).encode()
                + b"\nCAMCTL-SERIAL\n" + self.identity_serial.encode() + b"\nCAMCTL-END\n")
        elif "CAMCTL-DIRECTORY/1" in script:
            if not self.directory_ready:
                return RawToolOutcome(LocalExit(exit_code=1), b"", None, None, stderr=b"directory unavailable")
            output = self.directory_frames.pop(0) if self.directory_frames else b"CAMCTL-DIRECTORY/1\0END\0"
        elif "df -k" in script:
            if not self.prepare_ready:
                return RawToolOutcome(LocalExit(exit_code=1), b"", None, None, stderr=b"storage unavailable")
            output = b"Filesystem 1K-blocks Used Available Use% Mounted on\n/dev/mmc 1000 100 900 10% /mnt/media_rw\n" * 2
        elif "simulate_device" in script:
            output = b""
        else:
            output = b"Resp message, len = 1, data: 00"
        return RawToolOutcome(LocalExit(exit_code=0), output, None, None, stderr=b"")


async def failed_world(owned, tmp_path, *, baseline=(), software_recovery=None, sent_start=False):
    catalog = _catalog("dji-action6")
    if sent_start:
        from camctl.devices.tasks import StartReturn
        class SentCatalog:
            def __getattr__(self, name):
                return getattr(catalog, name)
            def parameter_definition(self, *args):
                definition = catalog.parameter_definition(*args)
                return replace(definition, task_factory=lambda params: replace(
                    definition.task_factory(params), start_return_meaning=StartReturn.SENT, startup_margin_ms=0))
        preparation_catalog = SentCatalog()
    else:
        preparation_catalog = catalog
    identity = await _prepared(owned, tmp_path, "camera_record", catalog=preparation_catalog)
    if baseline:
        from .test_baseline_history import _append, _entries
        assert _append(owned, identity, _entries(*baseline)).kind is DbOutcomeKind.COMPLETED
    assert _fix(owned, identity, chunks=int(bool(baseline)), entries=len(baseline)).kind is DbOutcomeKind.COMPLETED
    transport = Transport(owned.connection)
    driver = AdbCameraDriver(action6_record_contract(),
        {"cam-1": {"kind": "camera", "driver": "dji-action6", "adb": {"serial": "camera-serial"}}},
        transport, terminate_grace_s=Decimal("1"), monotonic_ns=lambda: 0)
    # 合成固件资格只用于本软件组合，不声明生产 Action6 已完成硬件验收。
    driver.recovery_qualification = create_autospec(driver.recovery_qualification,
        return_value=RecoveryQualification(CameraModel.ACTION6, _FINGERPRINT, 2,
            VideoResultMode.SINGLE_FILE, "software-test-fixture"))
    clock = baseline_case_for().clock
    devices = {"cam-1": {"kind": "camera", "driver": "dji-action6",
        "adb": {"serial": "camera-serial"},
        "recording": {"max_stop_attempts": 1, "stop_timeout_s": Decimal("0.001"),
                      "stop_retry_interval_s": Decimal(0)},
        "software_recovery": {"enabled": True, "retry_interval_s": Decimal(0),
                              **({} if software_recovery is None else software_recovery)}}}
    registry = DriverRegistry((DriverEntry("dji-action6", driver,
        replace(driver.declaration, query_supported=False), driver.contract.evidence,
        DriverStatus.SOFTWARE_CONTRACT_VERIFIED),))
    runtime = await baseline_case_for().runtime(owned, directory=None,
        driver=driver, evidence=driver.contract.evidence, drivers=registry,
        devices=devices, results=None, wait_config=execution_wait_config)
    await capture_handler("camera_record")(1, runtime)
    spec, = owned.connection.execute("SELECT execution_spec_json FROM actions WHERE id=1").fetchone()
    duration_ms = json.loads(spec)["target_duration_ms"]
    clock.wall += duration_ms * 1000
    clock.monotonic += duration_ms * 1_000_000
    # 正式驱动执行实际 STOP 超时，下一处理器轮次保存预算耗尽终态。
    for _ in range(3):
        await capture_handler("camera_record")(1, runtime)
        if owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (4,):
            break
    assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (4,)
    body = _plan_body(request_id="43")
    body["actions"][0].update(type="camera_record", params=_params("dji-action6", "camera_record"),
        scheduled_at=datetime.fromtimestamp(baseline_case_for().clock.wall // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    await _accept_plan(owned, tmp_path, body, _catalog("dji-action6"))
    return runtime, driver, transport, identity


async def test_failed_unknown_activity_reboots_once_then_releases_through_formal_gate(owned, tmp_path):
    runtime, driver, transport, activity = await failed_world(owned, tmp_path, sent_start=True)
    original = tuple(owned.connection.execute("SELECT id,status,error_code,error_details_json FROM actions WHERE id=1").fetchone())
    original_calls = tuple(owned.connection.execute("SELECT id,status,result_json,error_json FROM operation_attempts").fetchall())
    # 已受理的 SENT 合同没有确认实际开始；真实 START/STOP 返回留下 UNKNOWN。
    assert owned.connection.execute("SELECT activity_state FROM device_activities WHERE id=?", (activity,)).fetchone() == (1,)
    for _ in range(8):
        if await pass_residual_gate(runtime, (await runtime.action(2))):
            break
    assert transport.reboots == 1
    assert owned.connection.execute("SELECT activity_state,occupancy_state FROM device_activities WHERE id=?", (activity,)).fetchone() == (3, 2)
    assert tuple(owned.connection.execute("SELECT id,status,error_code,error_details_json FROM actions WHERE id=1").fetchone()) == original
    assert tuple(owned.connection.execute("SELECT id,status,result_json,error_json FROM operation_attempts").fetchall()) == original_calls
    assert owned.connection.execute("SELECT count(*) FROM operation_runs WHERE kind=8").fetchone() == (0,)
    state = saved_recovery(owned, activity)
    assert state["phase"] == "ready" and state["prepared"] and state["scope_finalized"]
    started = _start(owned, 2, now=runtime.wall_us())
    assert started.kind is DbOutcomeKind.COMPLETED and started.value.activity_id != activity
    # 新活动有自己的基准和 START；旧 START 从未重新派发。
    assert _fix(owned, started.value.activity_id).kind is DbOutcomeKind.COMPLETED
    await capture_handler("camera_record")(2, runtime)
    assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE responsibility_key='start/2'").fetchone() == (1,)
    assert transport.reboots == 1


async def test_dispatched_intent_survives_interruption_without_second_reboot(owned, tmp_path):
    runtime, driver, transport, activity = await failed_world(owned, tmp_path)
    transport.interrupt_reboot = True
    with pytest.raises(RuntimeError, match="主机"):
        await pass_residual_gate(runtime, (await runtime.action(2)))
    assert transport.reboots == 1
    # 模拟下一 run：清空驱动缓存，恢复原持久责任，触发动作结束仍继续确认。
    driver.begin_session()
    await request_baseline_cancel(owned, tmp_path, 2, baseline_case_for().clock.wall)
    for _ in range(8):
        await advance_existing_recovery()
    assert transport.reboots == 1
    assert owned.connection.execute("SELECT activity_state,occupancy_state FROM device_activities WHERE id=?", (activity,)).fetchone() == (3, 2)
    assert (await runtime.action(1))["status"] == 4 and (await runtime.action(2))["cancel_requested"] == 1


@pytest.mark.parametrize("fault", ["unchanged_boot", "not_prepared", "unknown_directory", "wrong_identity"])
async def test_unverified_recovery_keeps_device_held_with_bounded_budget(owned, tmp_path, fault):
    runtime, driver, transport, activity = await failed_world(owned, tmp_path,
        software_recovery={"max_checks": 3, "max_pages": 2})
    if fault == "unchanged_boot": transport.new_boot = False
    if fault == "not_prepared": transport.prepare_ready = False
    if fault == "unknown_directory": transport.directory_ready = False
    if fault == "wrong_identity": transport.firmware_version = "v2.2.3.4"
    for _ in range(12):
        await pass_residual_gate(runtime, (await runtime.action(2)))
    state = saved_recovery(owned, activity)
    assert state["phase"] == "isolated"
    assert owned.connection.execute("SELECT occupancy_state FROM device_activities WHERE id=?", (activity,)).fetchone() == (1,)
    assert transport.reboots == (0 if fault == "wrong_identity" else 1)
    original = state.copy()
    for _ in range(3): await advance_existing_recovery()
    assert saved_recovery(owned, activity) == original


async def test_nonempty_baseline_and_paged_difference_survive_connection_restart(owned, tmp_path):
    runtime, driver, transport, activity = await failed_world(owned, tmp_path, baseline=("old.MP4",))
    root = b"/mnt/media_rw/emulated/DCIM/"
    transport.directory_frames = [b"CAMCTL-DIRECTORY/1\0" + root + b"old.MP4\0MORE\0",
        b"CAMCTL-DIRECTORY/1\0" + root + b"z-new.MP4\0END\0"]
    for _ in range(4): await pass_residual_gate(runtime, (await runtime.action(2)))
    first = saved_recovery(owned, activity)
    assert first["scope_pages"] == 1 and first["scope_members"] == 0
    assert first["cursor"]["after_path"].endswith("old.MP4")
    path = owned.connection.execute("PRAGMA database_list").fetchone()[2]
    from pathlib import Path
    from camctl.persistence.runtime import open_existing, DbOpenMode, DbConfig
    reopened = open_existing(Path(path), DbOpenMode.EXISTING_RW, DbConfig())
    try:
        owned.connection.close()
        runtime = await baseline_case_for().reopen(reopened, cold=True)
        transport.connection = reopened.connection
        driver.begin_session()
        for _ in range(4): await advance_existing_recovery()
        state = saved_recovery(reopened, activity)
        assert state["scope_pages"] == 3 and state["scope_members"] == 1
        pages = [json.loads(row[0])["evidence"]["recovery"]["directory"] for row in reopened.connection.execute(
            "SELECT body_json FROM history_events WHERE json_extract(body_json,'$.evidence.recovery.directory.activity_id')=? ORDER BY id", (activity,))]
        assert pages[0]["new_entries"] == []
        assert [item["path"] for item in pages[1]["new_entries"]] == [root.decode() + "z-new.MP4"]
        assert pages[1]["previous_event_id"] == state["scope_first_event_id"]
        assert reopened.connection.execute("SELECT occupancy_state FROM device_activities WHERE id=?", (activity,)).fetchone() == (2,)
        assert transport.reboots == 1
    finally:
        reopened.connection.close()


async def test_boot_baseline_and_skipped_scope_page_are_rejected_by_real_guard(owned, tmp_path):
    runtime, driver, transport, activity = await failed_world(owned, tmp_path)
    from camctl.persistence.repositories.software_recovery import RecoverySave, save_recovery
    from camctl.contracts.values import new_operation_key
    await pass_residual_gate(runtime, (await runtime.action(2)))
    state = saved_recovery(owned, activity)
    changed = {**state, "boot_before": _AFTER}
    result = save_recovery(RecoverySave(activity, state, changed, _SCHEDULED, {}), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    for _ in range(2): await advance_existing_recovery()
    state = saved_recovery(owned, activity)
    assert state["phase"] == "scanning"
    from camctl.devices.directory import DirectoryCursor
    from camctl.devices.bindings import DeviceBinding
    directories = tuple(row_facts(owned.connection, "device_activities", activity)["output_scope_json"]["directories"])
    skipped = DirectoryCursor(DeviceBinding("cam-1", "dji-action6"), directories, 0, directories[0] + "/skipped.MP4").as_json()
    result = save_recovery(RecoverySave(activity, state,
        {**state, "phase": "ready", "scope_finalized": True, "cursor": None}, _SCHEDULED,
        {"directory": {"cursor": skipped, "next_cursor": None, "entries": []}}), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert owned.connection.execute("SELECT occupancy_state FROM device_activities WHERE id=?", (activity,)).fetchone() == (1,)


async def test_empty_reported_serial_is_optional_and_original_deadline_is_not_reset(owned, tmp_path):
    runtime, driver, transport, activity = await failed_world(owned, tmp_path)
    transport.identity_serial = ""
    await pass_residual_gate(runtime, (await runtime.action(2)))
    state = saved_recovery(owned, activity)
    assert transport.reboots == 1 and state["reported_serial"] is None
    baseline_case_for().clock.wall = state["deadline_at"] + 1
    await advance_existing_recovery()
    state_after = saved_recovery(owned, activity)
    assert state_after["phase"] == "isolated" and state_after["deadline_at"] == state["deadline_at"]
    assert state_after["reboots"] == 1
    await advance_existing_recovery()
    assert transport.reboots == 1


async def test_new_trigger_reuses_undispatched_responsibility_budget(owned, tmp_path):
    runtime, driver, transport, activity = await failed_world(owned, tmp_path)
    # ADB 尚不可达，只使用一次身份核实，不派发重启。
    original_identity = driver.recovery_identity
    async def unreachable(binding, *, timeout_s):
        return {"identity": None, "call": {"not_started": True}}
    driver.recovery_identity = unreachable
    await pass_residual_gate(runtime, (await runtime.action(2)))
    first = saved_recovery(owned, activity)
    assert first["checks"] == 1 and first["reboots"] == 0
    await request_baseline_cancel(owned, tmp_path, 2, baseline_case_for().clock.wall)
    await advance_existing_recovery()
    body = _plan_body(request_id="44")
    body["actions"][0].update(type="camera_record", params=_params("dji-action6", "camera_record"),
        scheduled_at=datetime.fromtimestamp(baseline_case_for().clock.wall // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    await _accept_plan(owned, tmp_path, body, _catalog("dji-action6"))
    driver.recovery_identity = original_identity
    await pass_residual_gate(runtime, (await runtime.action(4)))
    second = saved_recovery(owned, activity)
    assert second["trigger_id"] == 4 and second["origin_trigger_id"] == 2
    assert second["checks"] == 2 and second["reboots"] == 1
    assert second["deadline_at"] == first["deadline_at"]
    assert owned.connection.execute("SELECT COUNT(*) FROM software_recoveries").fetchone() == (1,)


async def test_isolated_camera_does_not_block_other_camera_or_create_periodic_reboot(owned, tmp_path):
    runtime, driver, transport, activity = await failed_world(owned, tmp_path, software_recovery={"max_checks": 2})
    before = len(transport.commands)
    await advance_existing_recovery()
    assert transport.reboots == 0 and len(transport.commands) == before
    transport.new_boot = False
    for _ in range(5): await pass_residual_gate(runtime, (await runtime.action(2)))
    assert saved_recovery(owned, activity)["phase"] == "isolated"
    other_device = {"kind": "camera", "driver": "dji-action6", "adb": {"serial": "camera-2"}}
    driver.devices["cam-2"] = other_device
    baseline_case_for().devices["cam-2"] = other_device
    body = _plan_body(request_id="45")
    body["actions"][0].update(type="camera_record", device_id="cam-2", params=_params("dji-action6", "camera_record"))
    await _accept_plan(owned, tmp_path, body, _catalog("dji-action6", device_id="cam-2"))
    result = _start(owned, 3, now=_SCHEDULED)
    assert result.kind is DbOutcomeKind.COMPLETED
    assert _fix(owned, result.value.activity_id).kind is DbOutcomeKind.COMPLETED
    baseline_case_for().clock.wall = _SCHEDULED
    baseline_case_for().clock.monotonic = 0
    other = await baseline_case_for().view(device_id="cam-2")
    await capture_handler("camera_record")(3, other)
    assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE responsibility_key='start/3'").fetchone() == (1,)
    assert owned.connection.execute("SELECT occupancy_state FROM device_activities WHERE id=?", (activity,)).fetchone() == (1,)
    assert transport.reboots == 1


async def test_recovery_blocks_new_stop_query_and_read_dispatch_even_after_ended(owned, tmp_path):
    runtime, driver, transport, activity = await failed_world(owned, tmp_path)
    await pass_residual_gate(runtime, (await runtime.action(2)))
    await advance_existing_recovery()
    assert row_facts(owned.connection, "device_activities", activity)["activity_state"] == 3
    from camctl.operations.attempts import AttemptIntent, AttemptTarget, OperationKind, QueryPurpose
    from camctl.contracts.values import new_operation_key
    request = AttemptIntent("query", 2, OperationKind.QUERY_ACTIVITY, AttemptTarget(),
        QueryPurpose.BEFORE_EXECUTION, AttemptConfig(1, Decimal("10"), Decimal("0")), _SCHEDULED)
    result = runtime.operations.begin_attempt(request, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED and result.value.reason == "device_recovery"
    from camctl.outputs.dispatch import DeviceFacts, decide_device_work
    work = decide_device_work(DeviceFacts("cam-1", (), (), (10,), (), True, True))
    assert work.grant_reads == () and work.resume_reads == ()
    from camctl.devices.ports import ControlRequest
    from camctl.devices.bindings import DeviceBinding
    from camctl.operations.models import AttemptTicket
    run_id = owned.connection.execute("SELECT id FROM operation_runs WHERE responsibility_key='stop/1'").fetchone()[0]
    with pytest.raises(RuntimeError, match="独占"):
        await driver.stop(ControlRequest("stop_recording", DeviceBinding("cam-1", "dji-action6"), {},
            ticket=AttemptTicket(1, "stop", str(activity), "stop/1", run_id), timeout_s=Decimal("10")))


@pytest.mark.parametrize("unknown_phase", ["intent", "confirming"])
async def test_actual_sqlite_commit_unknown_never_reissues_reboot(owned, tmp_path, unknown_phase):
    runtime, driver, transport, activity = await failed_world(owned, tmp_path)
    from camctl.contracts.values import ConsistencyError
    from camctl.contracts.enums import enum_for
    from camctl.persistence.business import BusinessDatabase
    from camctl.persistence.runtime import open_existing, DbOpenMode, DbConfig
    from ..support.database import database_path
    path = database_path(owned)
    faults = []
    expected = int(enum_for("software_recoveries.phase")[unknown_phase.upper()])
    def open_fault_connection():
        real = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        proxy = RecoveryCommitFault(real.connection, expected)
        faults.append(proxy)
        return replace(real, connection=proxy)
    database = await BusinessDatabase.open(open_fault_connection)
    try:
        actual = await baseline_case_for().factory(database, "cam-1")
        with pytest.raises(ConsistencyError):
            await pass_residual_gate(actual, (await actual.action(2)))
        assert len(faults) == 1 and faults[0].injected
    finally:
        await baseline_case_for().drain()
        await database.close()
    assert transport.reboots == (0 if unknown_phase == "intent" else 1)
    for _ in range(8):
        await advance_existing_recovery()
    assert transport.reboots == (0 if unknown_phase == "intent" else 1)
    state = saved_recovery(owned, activity)
    if unknown_phase == "confirming":
        assert state["phase"] == "ready"
    else:
        assert state["phase"] == "intent" and state["reboots"] == 1


async def test_recovery_history_restores_responsibility_and_legacy_v2_without_table(owned, tmp_path):
    runtime, driver, transport, activity = await failed_world(owned, tmp_path)
    from camctl.persistence.repositories.history import HistoryRepository
    history = HistoryRepository(tmp_path / "state.db")
    before = history.current_boundary()
    await pass_residual_gate(runtime, (await runtime.action(2)))
    current = history.current_boundary()
    restored = history.restore_entity("action", 1, current)
    recovery_key = next(key for key in restored if key[0] == "software_recoveries")
    from camctl.contracts.enums import enum_for
    assert enum_for("software_recoveries.phase")(restored[recovery_key]["phase"]).name == "CONFIRMING"
    assert recovery_key not in history.restore_entity("action", 1, before)
    with history.open_segment_history(1, current, strategy="current") as handle:
        assert handle.row(*recovery_key)["state_json"]["reboots"] == 1
    # 真正旧格式不包含新表，也没有新分支事件；沿原完整历史继续读取。
    # 使用独立旧库，避免修改已经包含 V3 事实的状态库版本。
    from ..persistence.test_runtime import _create_valid_database
    from pathlib import Path
    import sqlite3
    old_path = tmp_path / "legacy.db"
    _create_valid_database(old_path, format_version=2)
    with sqlite3.connect(old_path) as connection:
        connection.execute("DROP TABLE software_recoveries")
    from camctl.persistence.runtime import open_existing, DbOpenMode, DbConfig
    legacy = open_existing(old_path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        await _prepared(legacy, tmp_path, "camera_record")
        old_history = HistoryRepository(old_path)
        boundary = old_history.current_boundary()
        assert old_history.restore_entity("action", 1, boundary)[("actions", 1)]["status"] == 2
        with old_history.open_segment_history(1, boundary, strategy="current") as handle:
            assert handle.primary()["status"] == 2
            assert handle.row("software_recoveries", 1) is None
    finally:
        legacy.connection.close()


async def test_session_assembly_consumes_enabled_deployment_config_in_formal_gate(owned, tmp_path):
    runtime, driver, transport, activity = await failed_world(owned, tmp_path)
    from camctl.bootstrap.config import load_config, ConfigDefaults
    from camctl.bootstrap.capture_assembly import session_capture_assembly, execution_wait_config
    from camctl.devices.drivers.registry import DriverEntry, DriverRegistry
    config = load_config({"devices": {"cam-1": {"kind": "camera", "driver": "dji-action6",
        "adb": {"serial": "camera-serial"}, "recording": {"stop_timeout_s": Decimal("0.001"), "max_stop_attempts": 1},
        "software_recovery": {"enabled": True, "retry_interval_s": 0}}}}, ConfigDefaults())
    entry = DriverEntry("dji-action6", driver, driver.declaration, driver.contract.evidence, driver.contract.status)
    factory = session_capture_assembly(devices=config.devices, drivers=DriverRegistry((entry,)),
        staging=tmp_path, wait_config=execution_wait_config,
        wall_us=baseline_case_for().clock.wall_us,
        monotonic_ns=baseline_case_for().clock.monotonic_ns, media_enabled=False)
    for _ in range(8):
        assembled = await factory(await baseline_case_for().session.database(owned), "cam-1")
        assert assembled.state_query is None
        assert assembled.stop_config.timeout_s == Decimal("0.001")
        if await pass_residual_gate(assembled, (await assembled.action(2))):
            break
    assert transport.reboots == 1
    assert owned.connection.execute("SELECT occupancy_state FROM device_activities WHERE id=?", (activity,)).fetchone() == (2,)

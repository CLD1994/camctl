"""用户启用选择与驱动资格分离，原冻结恢复继续保留身份和预算。"""

from copy import deepcopy
from decimal import Decimal
from unittest.mock import create_autospec
import sqlite3

import pytest
from support.capture_database import capture_database

from camctl.capture import software_recovery as recovery
from camctl.capture.handlers import CaptureRuntime
from camctl.contracts.values import ConsistencyError
from camctl.devices.drivers.adb_cameras.driver import AdbCameraDriver
from camctl.devices.drivers.adb_cameras.firmware_identity import recovery_qualification
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from camctl.scheduling.rules import LaunchWindow

_SAVE_PORT = recovery._save


@pytest.fixture
def session(monkeypatch):
    runtime = create_autospec(CaptureRuntime, instance=True)
    runtime.owned = OwnedConnection(create_autospec(sqlite3.Connection, instance=True),
        DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing"))
    runtime.database = capture_database(runtime.owned)
    runtime.software_recovery_config = recovery.SoftwareRecoveryConfig(enabled=True)
    runtime.binding_check = runtime.state_query = None
    runtime.wall_us = lambda: 1_000_000
    runtime.window_of = lambda action: LaunchWindow(0, 10_000_000)
    owner = dict(id=8, device_id="camera", driver_id="dji-osmo360-ii", type=2,
                 execution_spec_json={"video_result_mode": "segmented"})
    trigger = dict(id=9, device_id="camera", type=3, status=1, cancel_requested=0)
    runtime.action.side_effect = lambda action_id: owner if action_id == 8 else trigger
    activity = dict(id=11, action_id=8, activity_state=2,
                    baseline_first_event_id=4, baseline_last_event_id=7)
    current, saved = [None], []
    monkeypatch.setattr(recovery, "row_facts", create_autospec(recovery.row_facts, return_value=activity))
    monkeypatch.setattr(recovery, "recovery_of", create_autospec(recovery.recovery_of,
        side_effect=lambda *args: deepcopy(current[0])))
    monkeypatch.setattr(recovery, "recovery_blocker", create_autospec(recovery.recovery_blocker, return_value=None))

    def save(runtime, activity, state, **kwargs):
        current[0] = deepcopy(state)
        saved.append((deepcopy(state), deepcopy(kwargs)))
        return activity

    monkeypatch.setattr(recovery, "_save", create_autospec(_SAVE_PORT, side_effect=save))
    driver = create_autospec(AdbCameraDriver, instance=True)
    runtime.software_recovery_driver = driver
    driver.recovery_serial.return_value = "camera-serial"
    driver.recovery_qualification.side_effect = lambda binding, **kwargs: recovery_qualification(binding.driver_id, **kwargs)
    qualified = recovery_qualification("dji-osmo360-ii", action_type=2, video_result_mode="segmented")
    driver.recovery_identity.return_value = {"identity": ["boot-before", qualified.fingerprint, None], "call": {"exit_code": 0}}

    async def reboot(*args, **kwargs):
        assert saved[-1][0]["phase"] == "intent"
        assert saved[-1][0]["reboots"] == 1
        assert saved[-1][0]["boot_before"] == "boot-before"
        return {"exit_code": 0}

    driver.recovery_reboot.side_effect = reboot
    return runtime, owner, trigger, current, saved, driver, save


@pytest.mark.asyncio
async def test_disabled_new_recovery_does_not_read_identity_or_create_intent(session):
    runtime, _, trigger, _, saved, driver, _ = session
    runtime.software_recovery_config = recovery.SoftwareRecoveryConfig()
    assert await recovery.advance_recovery(runtime, 11, trigger) is False
    assert saved == []
    driver.recovery_identity.assert_not_awaited()
    driver.recovery_reboot.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("driver_id,action_type,mode", [
    ("dji-action6", 2, "segmented"), ("dji-osmo360-ii", 1, None),
    ("dji-osmo360-ii", 3, "segmented"), ("dji-osmo360-ii", 2, "single_file"),
])
async def test_qualification_uses_the_original_activity_capture_scope(session, driver_id, action_type, mode):
    runtime, owner, trigger, _, saved, driver, _ = session
    owner.update(driver_id=driver_id, type=action_type, execution_spec_json={"video_result_mode": mode})
    assert await recovery.advance_recovery(runtime, 11, trigger) is False
    assert saved == []
    diagnostic = runtime.record_recovery_diagnostic.call_args.args[0]
    assert diagnostic.scope_unverified and diagnostic.activity_id == 11
    driver.recovery_identity.assert_not_awaited()
    driver.recovery_reboot.assert_not_awaited()


@pytest.mark.asyncio
async def test_qualified_actual_firmware_is_saved_before_the_only_reboot(session):
    runtime, _, trigger, current, saved, driver, _ = session
    assert await recovery.advance_recovery(runtime, 11, trigger) is False
    state = current[0]
    assert state["phase"] == "confirming" and state["reboots"] == 1
    assert state["fingerprint"] == driver.recovery_identity.return_value["identity"][1]
    assert state["last_identity"] == driver.recovery_identity.return_value
    assert saved[0][0]["checks"] == saved[0][0]["reboots"] == 0
    driver.recovery_identity.assert_awaited_once()
    driver.recovery_reboot.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_actual_firmware_is_preserved_and_never_rebooted(session):
    runtime, _, trigger, current, _, driver, _ = session
    driver.recovery_identity.return_value = {"identity": ["boot-before", "android-build-fingerprint/v1:unverified", None], "call": {"exit_code": 0}}
    assert await recovery.advance_recovery(runtime, 11, trigger) is False
    assert current[0]["phase"] == "isolated" and current[0]["reboots"] == 0
    assert current[0]["last_identity"] == driver.recovery_identity.return_value
    diagnostic = runtime.record_recovery_diagnostic.call_args.args[0]
    assert diagnostic.fingerprint == "android-build-fingerprint/v1:unverified"
    assert "该固件尚未验证自动重启恢复" in diagnostic.message
    driver.recovery_reboot.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_frozen_recovery_continues_when_configuration_is_disabled(session):
    runtime, owner, trigger, current, saved, driver, _ = session
    await recovery.advance_recovery(runtime, 11, trigger)
    original = deepcopy(current[0])
    original.update(fingerprint="frozen-original-fingerprint", phase="confirming", max_checks=9)
    current[0] = deepcopy(original)
    saved.clear()
    owner["type"] = 3
    driver.recovery_qualification.reset_mock()
    driver.recovery_reboot.reset_mock()
    runtime.software_recovery_config = recovery.SoftwareRecoveryConfig(enabled=False, max_checks=1)
    runtime.wall_us = lambda: 4_000_000
    driver.recovery_identity.return_value = {"identity": ["boot-after", original["fingerprint"], None], "call": {"exit_code": 0}}
    assert await recovery.advance_recovery(runtime, 11, trigger) is False
    assert current[0]["phase"] == "preparing" and current[0]["boot_after"] == "boot-after"
    for field in ("fingerprint", "deadline_at", "max_checks", "max_reboots", "reboots"):
        assert current[0][field] == original[field]
    assert saved[-1][1]["ended"] is True
    driver.recovery_qualification.assert_not_called()
    driver.recovery_reboot.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_intent_save_is_observed_without_reboot_dispatch(session, monkeypatch):
    runtime, _, trigger, current, _, driver, save = session

    def uncertain(runtime, activity, state, **kwargs):
        result = save(runtime, activity, state, **kwargs)
        if state["phase"] == "intent":
            raise ConsistencyError("意图实际保存，回执未知")
        return result

    monkeypatch.setattr(recovery, "_save", create_autospec(_SAVE_PORT, side_effect=uncertain))
    with pytest.raises(ConsistencyError):
        await recovery.advance_recovery(runtime, 11, trigger)
    assert current[0]["phase"] == "intent" and current[0]["reboots"] == 1
    driver.recovery_reboot.assert_not_awaited()
    monkeypatch.setattr(recovery, "_save", create_autospec(_SAVE_PORT, side_effect=save))
    runtime.wall_us = lambda: 4_000_000
    await recovery.advance_recovery(runtime, 11, trigger)
    assert driver.recovery_identity.await_count == 2
    driver.recovery_reboot.assert_not_awaited()


@pytest.mark.asyncio
async def test_entry_gate_preserves_existing_responsibility_when_disabled(session, monkeypatch):
    runtime, _, trigger, current, _, _, _ = session
    runtime.software_recovery_config = recovery.SoftwareRecoveryConfig(enabled=False)
    monkeypatch.setattr(recovery, "held_candidates", create_autospec(recovery.held_candidates, return_value=[(11,)]))
    current[0] = {"phase": "confirming"}
    advance = create_autospec(recovery.advance_recovery, return_value=False)
    monkeypatch.setattr(recovery, "advance_recovery", advance)
    assert await recovery.pass_software_recovery_gate(runtime, trigger) is False
    advance.assert_awaited_once_with(runtime, 11, trigger, on_progress=None)


@pytest.mark.asyncio
async def test_recovery_wait_before_saved_deadline_does_not_report_progress(session):
    runtime, _, trigger, current, _, _, _ = session
    await recovery.advance_recovery(runtime, 11, trigger)
    assert current[0]["next_check_at"] > runtime.wall_us()
    notices = []

    await recovery.advance_recovery(runtime, 11, trigger, on_progress=lambda: notices.append(True))

    assert notices == []


@pytest.mark.asyncio
async def test_recovery_actual_save_reports_progress(session):
    runtime, _, trigger, _, saved, _, _ = session
    notices = []

    await recovery.advance_recovery(runtime, 11, trigger, on_progress=lambda: notices.append(True))

    assert saved and notices

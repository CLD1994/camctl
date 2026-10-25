"""原终态活动的有界软件重启责任，不复用原 START、STOP 或 RESULTS。"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryCursor, DirectoryRequest
from camctl.operations.owned_calls import owned_tool_call, check_call_interruption
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.transaction import row_facts
from camctl.scheduling.rules import WindowPhase, window_phase


from camctl.contracts.enums import enum_for


#: 普通 phase 列的编号由组件枚举登记维护，内存只派生成员名的小写文本。
RecoveryPhase = StrEnum("RecoveryPhase", {member.name: member.name.lower()
    for member in enum_for("software_recoveries.phase")})


def recovery_state(row):
    return {**row["state_json"], "phase": enum_for("software_recoveries.phase")(row["phase"]).name.lower()}


@dataclass(frozen=True)
class SoftwareRecoveryConfig:
    enabled: bool = False
    max_reboots: int = 1
    timeout_s: Decimal = Decimal("10")
    deadline_s: Decimal = Decimal("180")
    retry_interval_s: Decimal = Decimal("3")
    max_checks: int = 64
    max_pages: int = 256


@dataclass(frozen=True)
class SoftwareRecoveryDiagnostic:
    """新恢复资格不足的本地说明；不改变原动作或设备占用。"""

    device_id: str
    activity_id: int
    fingerprint: str | None = None
    scope_unverified: bool = False

    @property
    def message(self):
        detail = "该固件尚未验证自动重启恢复"
        if self.scope_unverified:
            detail += "，原拍摄类型或结果模式没有适用的验证记录"
        return detail


def recovery_of(activity, connection):
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='software_recoveries'").fetchone() is None:
        return None
    row = connection.execute("SELECT id FROM software_recoveries WHERE activity_id=?", (activity["id"],)).fetchone()
    return None if row is None else recovery_state(row_facts(connection, "software_recoveries", row[0]))


def held_candidates(connection, device_id):
    return connection.execute(
        "SELECT d.id FROM device_activities d JOIN actions a ON a.id=d.action_id"
        " WHERE a.device_id=? AND a.status IN (4,6) AND d.occupancy_state=1"
        " AND d.dispatch_state IN (2,3) ORDER BY d.id LIMIT 2", (device_id,)).fetchall()


def recovery_blocker(connection, activity, trigger_id):
    """全部本机责任实际收场后才允许进入排他恢复。"""
    owner = row_facts(connection, "actions", activity["action_id"])
    device = owner["device_id"]
    if connection.execute(
        "SELECT 1 FROM device_activities d JOIN actions a ON a.id=d.action_id"
        " WHERE a.device_id=? AND d.id<>? AND d.occupancy_state=1 LIMIT 1",
        (device, activity["id"])).fetchone():
        return "other_activity"
    if connection.execute(
        "SELECT 1 FROM file_copies WHERE slot_device_id=? LIMIT 1", (device,)).fetchone():
        return "file_holder_unsettled"
    if connection.execute(
        "SELECT 1 FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
        " JOIN actions a ON a.id=r.action_id"
        " LEFT JOIN device_activities target ON target.id=r.activity_id"
        " LEFT JOIN actions target_owner ON target_owner.id=target.action_id"
        " LEFT JOIN file_copies copy ON copy.id=r.copy_id"
        " LEFT JOIN cleanup_items cleanup ON cleanup.id=r.cleanup_item_id"
        " LEFT JOIN obtain_items obtain ON obtain.id=r.obtain_item_id"
        " LEFT JOIN outputs output ON output.id=COALESCE(cleanup.output_id,obtain.requested_output_id)"
        " LEFT JOIN device_files file ON file.id=COALESCE(copy.source_device_file_id,output.device_file_id)"
        " LEFT JOIN actions file_owner ON file_owner.id=file.observer_action_id"
        " WHERE t.status=1 AND COALESCE(target_owner.device_id,file_owner.device_id,a.device_id)=? LIMIT 1",
        (device,)).fetchone():
        return "calls_unsettled"
    if connection.execute(
        "SELECT 1 FROM baseline_read_calls b JOIN actions a ON a.id=b.action_id"
        " WHERE a.device_id=? AND b.state=1 LIMIT 1", (device,)).fetchone():
        return "directory_call_unsettled"
    if connection.execute(
        "SELECT 1 FROM operation_runs r JOIN actions a ON a.id=r.action_id"
        " WHERE a.device_id=? AND a.id NOT IN (?,?) AND a.status IN (1,2)"
        " AND r.kind=1 AND r.status IN (1,2) AND r.attempts_used>0 LIMIT 1",
        (device, activity["action_id"], trigger_id)).fetchone():
        return "start_reserved"
    return None


def _eligible(runtime, trigger):
    return (trigger["status"] in (1, 2) and not trigger["cancel_requested"]
            and window_phase(runtime.window_of(trigger), runtime.wall_us()) is WindowPhase.IN_WINDOW)


def _save(runtime, activity, state, *, evidence=None, ended=False):
    from camctl.persistence.repositories.software_recovery import RecoverySave, save_recovery
    request = RecoverySave(activity["id"], recovery_of(activity, runtime.owned.connection), state,
                           runtime.wall_us(), evidence or {}, ended)
    receipt = save_recovery(request, new_operation_key(), runtime.owned)
    if receipt.kind is not DbOutcomeKind.COMPLETED:
        raise ConsistencyError(f"软件恢复事实尚未可靠保存: {receipt.kind.value}: {receipt.error}")
    return row_facts(runtime.owned.connection, "device_activities", activity["id"])


def _isolate(runtime, activity, reason, details=None):
    state = dict(recovery_of(activity, runtime.owned.connection))
    state.update(phase=RecoveryPhase.ISOLATED.value, reason=reason)
    if details is not None:
        state["reason_details"] = str(details)
    _save(runtime, activity, state)


class RecoveryStop:
    async def requested(self):
        import asyncio
        await asyncio.Future()


@owned_tool_call
async def advance_recovery(runtime, activity_id, trigger=None):
    """一次推进一个阶段；保存未知时停止，不用第二次命令猜测原结果。"""
    connection = runtime.owned.connection
    activity = row_facts(connection, "device_activities", activity_id)
    owner = runtime.action(activity["action_id"])
    config = runtime.software_recovery_config
    driver = runtime.software_recovery_driver
    state = recovery_of(activity, runtime.owned.connection)
    if state is None:
        if runtime.owned.metadata.format_version < 3:
            return False
        if trigger is None or not _eligible(runtime, trigger) or driver is None or config is None or not config.enabled:
            return False
        if activity["activity_state"] == 3:
            return False
        if runtime.binding_check is not None:
            from camctl.devices.bindings import BindingStatus
            if runtime.binding_check(DeviceBinding(owner["device_id"], owner["driver_id"])).status is not BindingStatus.MATCHED:
                return False
        if recovery_blocker(connection, activity, trigger["id"]):
            return False
        # 只有驱动未提供核实路径，或原有限收场已结束时进入兜底。
        if runtime.state_query is not None:
            original = connection.execute(
                "SELECT status FROM operation_runs WHERE kind=8 AND activity_id=?"
                " ORDER BY id DESC LIMIT 1", (activity_id,)).fetchone()
            if original is None or original[0] in (1, 2, 3):
                return False
        qualification = driver.recovery_qualification(
            DeviceBinding(owner["device_id"], owner["driver_id"]), action_type=owner["type"],
            video_result_mode=owner["execution_spec_json"].get("video_result_mode"))
        if qualification is None:
            runtime.record_recovery_diagnostic(SoftwareRecoveryDiagnostic(
                owner["device_id"], activity_id, scope_unverified=True))
            return False
        serial = driver.recovery_serial(DeviceBinding(owner["device_id"], owner["driver_id"]))
        now = runtime.wall_us()
        state = {"version": 1, "phase": RecoveryPhase.CHECKING.value,
            "trigger_id": trigger["id"], "origin_trigger_id": trigger["id"], "device_id": owner["device_id"],
            "driver_id": owner["driver_id"], "serial": serial,
            "fingerprint": qualification.fingerprint,
            "created_at": now, "deadline_at": now + int(config.deadline_s * 1000000),
            "timeout_s": str(config.timeout_s), "retry_interval_s": str(config.retry_interval_s),
            "max_reboots": config.max_reboots, "max_checks": config.max_checks,
            "max_pages": config.max_pages, "reboots": 0, "checks": 0, "pages": 0,
            "next_check_at": now, "boot_before": None, "boot_after": None,
            "baseline_first": activity["baseline_first_event_id"],
            "baseline_last": activity["baseline_last_event_id"],
            "cursor": None, "scope_finalized": False, "prepared": False, "reported_serial": None,
            "scope_pages": 0, "scope_members": 0, "scope_first_event_id": None, "scope_last_event_id": None}
        activity = _save(runtime, activity, state)
    phase = RecoveryPhase(state["phase"])
    if (phase is RecoveryPhase.ABANDONED and state["reboots"] == 0 and trigger is not None
            and _eligible(runtime, trigger) and runtime.wall_us() < state["deadline_at"]):
        state = {**state, "trigger_id": trigger["id"], "phase": RecoveryPhase.CHECKING.value,
                 "next_check_at": runtime.wall_us()}
        activity = _save(runtime, activity, state)
        phase = RecoveryPhase.CHECKING
    if phase in (RecoveryPhase.ISOLATED, RecoveryPhase.ABANDONED):
        return False
    if driver is None:
        _isolate(runtime, activity, "recovery_driver_unavailable")
        return False
    driver.hold_recovery(state["device_id"])
    binding = DeviceBinding(state["device_id"], state["driver_id"])
    try:
        actual_serial = driver.recovery_serial(binding)
    except Exception as error:
        from camctl.devices.bindings import DeviceConfigurationError
        if not isinstance(error, DeviceConfigurationError):
            raise
        _isolate(runtime, activity, "binding_unavailable")
        return False
    if (owner["device_id"] != state["device_id"] or owner["driver_id"] != state["driver_id"]
            or actual_serial != state["serial"]):
        _isolate(runtime, activity, "binding_changed")
        return False
    if phase is RecoveryPhase.READY:
        from camctl.capture.models import ActivityReleaseSave
        receipt = runtime.capture.release_occupancy(
            ActivityReleaseSave(owner["id"], runtime.wall_us()), new_operation_key(), runtime.owned)
        if receipt.kind is not DbOutcomeKind.COMPLETED:
            raise ConsistencyError("恢复后的占用释放尚未可靠保存")
        released = row_facts(connection, "device_activities", activity_id)["occupancy_state"] == 2
        if released:
            driver.release_recovery(state["device_id"])
        return released
    if runtime.wall_us() >= state["deadline_at"]:
        _isolate(runtime, activity, "deadline_exhausted")
        return False
    if runtime.wall_us() < state["next_check_at"]:
        return False
    trigger = runtime.action(state["trigger_id"])
    if phase in (RecoveryPhase.CHECKING, RecoveryPhase.INTENT) and state["reboots"] == 0 and not _eligible(runtime, trigger):
        state = {**state, "phase": RecoveryPhase.ABANDONED.value, "reason": "trigger_ended"}
        _save(runtime, activity, state)
        return False
    blocker = recovery_blocker(connection, activity, state["trigger_id"])
    if blocker:
        _isolate(runtime, activity, blocker)
        return False
    timeout = min(Decimal(state["timeout_s"]), Decimal(state["deadline_at"] - runtime.wall_us()) / 1000000)
    state = dict(state)
    state["next_check_at"] = runtime.wall_us() + int(Decimal(state["retry_interval_s"]) * 1000000)
    check_call_interruption()
    if phase in (RecoveryPhase.CHECKING, RecoveryPhase.INTENT, RecoveryPhase.CONFIRMING):
        if state["checks"] >= state["max_checks"]:
            _isolate(runtime, activity, "checks_exhausted")
            return False
        state["checks"] += 1
        # 核实预算也在实际调用之前可靠保存。
        activity = _save(runtime, activity, state)
        actual = await driver.recovery_identity(binding, timeout_s=timeout)
        state["last_identity"] = actual
        state["last_identity_check"] = state["checks"]
        activity = _save(runtime, activity, state, evidence={"identity_call": actual})
        observed = actual["identity"]
        if observed is None:
            return False
        boot, fingerprint, serial = observed
        if (fingerprint != state["fingerprint"] or (state["reported_serial"] is not None
                and serial is not None and state["reported_serial"] != serial)):
            if fingerprint != state["fingerprint"]:
                runtime.record_recovery_diagnostic(SoftwareRecoveryDiagnostic(
                    state["device_id"], activity_id, fingerprint))
            _isolate(runtime, activity, "identity_mismatch")
            return False
        if phase is RecoveryPhase.CHECKING:
            if not _eligible(runtime, runtime.action(state["trigger_id"])):
                return False
            if state["reboots"] >= state["max_reboots"]:
                _isolate(runtime, activity, "reboot_budget_exhausted")
                return False
            state.update(boot_before=boot, reported_serial=serial, phase=RecoveryPhase.INTENT.value,
                         reboots=state["reboots"] + 1)
            activity = _save(runtime, activity, state)
            # 意图之后任何中断都按可能已派发恢复；从不恢复补发。
            if not _eligible(runtime, runtime.action(state["trigger_id"])):
                state.update(phase=RecoveryPhase.ABANDONED.value, reason="trigger_ended_before_dispatch")
                _save(runtime, activity, state)
                return False
            check_call_interruption()
            remaining = min(Decimal(state["timeout_s"]), Decimal(state["deadline_at"] - runtime.wall_us()) / 1000000)
            if remaining <= 0:
                _isolate(runtime, activity, "deadline_exhausted_before_dispatch")
                return False
            raw = await driver.recovery_reboot(binding, timeout_s=remaining)
            state.update(phase=RecoveryPhase.CONFIRMING.value)
            _save(runtime, activity, state, evidence={"call": raw})
            return False
        if boot == state["boot_before"]:
            return False
        state.update(boot_after=boot, phase=RecoveryPhase.PREPARING.value)
        _save(runtime, activity, state, evidence={"identity": list(observed)}, ended=True)
        return False
    if phase is RecoveryPhase.PREPARING:
        if state["checks"] >= state["max_checks"]:
            _isolate(runtime, activity, "checks_exhausted")
            return False
        state["checks"] += 1
        activity = _save(runtime, activity, state)
        prepared = await driver.recovery_prepare(binding, owner["effective_params_json"],
            activity_id=activity_id, directories=tuple(activity["output_scope_json"].get("directories", ())), timeout_s=timeout)
        state["last_preparation"] = prepared
        if not prepared["ready"]:
            _save(runtime, activity, state, evidence={"preparation": prepared})
            return False
        state.update(prepared=True, phase=RecoveryPhase.SCANNING.value)
        _save(runtime, activity, state, evidence={"preparation": prepared})
        return False
    if phase is RecoveryPhase.SCANNING:
        if (activity["ownership_mode"] != 2 or activity["baseline_state"] != 3
                or activity["baseline_first_event_id"] != state["baseline_first"]
                or activity["baseline_last_event_id"] != state["baseline_last"]):
            _isolate(runtime, activity, "baseline_unconfirmed")
            return False
        if state["pages"] >= state["max_pages"]:
            _isolate(runtime, activity, "pages_exhausted")
            return False
        state["pages"] += 1
        activity = _save(runtime, activity, state)
        request = DirectoryRequest(binding, tuple(activity["output_scope_json"]["directories"]),
            None if state["cursor"] is None else DirectoryCursor.from_json(state["cursor"]), 128, timeout)
        page = await driver.recovery_directory(request)
        if page.error is not None:
            state["last_directory_error"] = {"code": page.error.code, "stage": page.error.stage,
                                             "details": dict(page.error.details)}
            state["last_directory_return"] = state["pages"]
            _save(runtime, activity, state, evidence={"directory_error": state["last_directory_error"]})
            return False
        if any(identity.binding != binding or not any(identity.path.startswith(root + "/") for root in request.directories)
               for identity in page.page.items):
            _isolate(runtime, activity, "scope_identity_mismatch")
            return False
        state["cursor"] = None if page.page.next_cursor is None else page.page.next_cursor.as_json()
        if state["cursor"] is None:
            state.update(scope_finalized=True, phase=RecoveryPhase.READY.value)
        from camctl.persistence.repositories.software_recovery import classify_scope_entries
        try:
            classify_scope_entries(connection, activity, [identity.as_json() for identity in page.page.items], state["max_pages"])
        except ConsistencyError as error:
            _isolate(runtime, activity, "baseline_scope_unconfirmed", error)
            return False
        _save(runtime, activity, state, evidence={"directory": {
            "cursor": None if request.cursor is None else request.cursor.as_json(),
            "next_cursor": state["cursor"], "entries": [identity.as_json() for identity in page.page.items]}})
        return False
    return False


async def pass_software_recovery_gate(runtime, trigger):
    candidates = held_candidates(runtime.owned.connection, trigger["device_id"])
    if not candidates:
        return None
    if len(candidates) != 1:
        return False
    activity = row_facts(runtime.owned.connection, "device_activities", candidates[0][0])
    state = recovery_of(activity, runtime.owned.connection)
    driver = getattr(runtime, "software_recovery_driver", None)
    config = getattr(runtime, "software_recovery_config", None)
    if state is None:
        if driver is None or config is None or not config.enabled:
            return None
        if runtime.state_query is not None:
            prior = runtime.owned.connection.execute("SELECT status FROM operation_runs WHERE kind=8 AND activity_id=? ORDER BY id DESC LIMIT 1", (activity["id"],)).fetchone()
            if prior is None or prior[0] in (1, 2, 3):
                return None
    return await advance_recovery(runtime, candidates[0][0], trigger)


def recovery_locked(connection, device_id):
    """持久化恢复独占包含已结束但尚未释放的准备、范围和隔离责任。"""
    if not device_id or connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='software_recoveries'").fetchone() is None:
        return False
    return connection.execute("SELECT 1 FROM software_recoveries r"
        " JOIN device_activities d ON d.id=r.activity_id JOIN actions a ON a.id=r.action_id"
        " WHERE a.device_id=? AND d.occupancy_state=1 LIMIT 1", (device_id,)).fetchone() is not None


def intent_device(connection, intent):
    """普通意图沿真实设备文件来源定位设备，主机源不受相机恢复限制。"""
    target = intent.target
    if target.activity_id is not None:
        row = connection.execute("SELECT a.device_id FROM device_activities d JOIN actions a ON a.id=d.action_id WHERE d.id=?", (target.activity_id,)).fetchone()
    elif target.copy_id is not None:
        row = connection.execute("SELECT a.device_id FROM file_copies c JOIN device_files f ON f.id=c.source_device_file_id"
            " JOIN actions a ON a.id=f.observer_action_id WHERE c.id=?", (target.copy_id,)).fetchone()
    elif target.cleanup_item_id is not None:
        row = connection.execute("SELECT a.device_id FROM cleanup_items c JOIN outputs o ON o.id=c.output_id"
            " JOIN device_files f ON f.id=o.device_file_id JOIN actions a ON a.id=f.observer_action_id WHERE c.id=?", (target.cleanup_item_id,)).fetchone()
    elif target.obtain_item_id is not None:
        row = connection.execute("SELECT a.device_id FROM obtain_items i JOIN outputs o ON o.id=i.requested_output_id"
            " JOIN device_files f ON f.id=o.device_file_id JOIN actions a ON a.id=f.observer_action_id WHERE i.id=?", (target.obtain_item_id,)).fetchone()
    else:
        row = connection.execute("SELECT device_id FROM actions WHERE id=?", (intent.action_id,)).fetchone()
    return None if row is None else row[0]


def intent_recovery_locked(connection, intent):
    return recovery_locked(connection, intent_device(connection, intent))

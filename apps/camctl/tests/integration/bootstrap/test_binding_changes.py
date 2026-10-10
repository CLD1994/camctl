"""真实受理、拍摄装配和执行在后续配置变化时保持原设备绑定。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.ports import ParameterDefinition
from camctl.acceptance.service import CommandMode
from camctl.bootstrap.capture_assembly import session_capture_assembly
from camctl.bootstrap.flows import cancel_flow, capture_flow
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.contracts.values import new_operation_key
from camctl.contracts.workflow_errors import registered_error
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import DeviceCallResult, DriverDeclaration
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import (
    AcceptanceRepository, ProcessInput, register_acceptance_guards,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..capture.test_capture_contract import ResultsDouble
from ..persistence.test_runtime import _create_valid_database
from .test_obtain_flow import _CAMERA_SCHEMA, _EVIDENCE, _photo_entry

_NOW = int(datetime(2026, 1, 15, 9, tzinfo=timezone.utc).timestamp() * 1_000_000)


class _Catalog:
    def action_types(self):
        return frozenset({"camera_take_photo", "cancel_task"})

    def device_exists(self, device_id):
        return device_id in {"cam-a", "cam-b"}

    def driver_id(self, device_id):
        return "camctl-adb" if self.device_exists(device_id) else None

    def device_supports(self, device_id, action_type):
        return self.device_exists(device_id) and action_type == "camera_take_photo"

    def parameter_definition(self, device_id, action_type, parameter_type):
        if self.device_supports(device_id, action_type) and parameter_type == "single_shot":
            return ParameterDefinition(schema=_CAMERA_SCHEMA, defaults={})
        return None


class _Driver:
    """控制端口替身，观察身份对应两项真实授予的活动。"""

    def __init__(self):
        self.calls = []

    async def control(self, request):
        self.calls.append(request.binding)
        return DeviceCallResult(
            observations=(DeviceObservation(
                type="photo_taken", version=1,
                data={"activity_id": "1" if request.binding.device_id == "cam-b" else "2"}),),
            error=None,
        )


def _registry(driver):
    declaration = DriverDeclaration(
        control_supported=True, stop_supported=False, query_supported=False,
        result_supported=False, read_supported=False, digest_supported=False,
        delete_supported=False,
    )
    return DriverRegistry(tuple(
        DriverEntry(
            driver_id=driver_id, driver=driver, declaration=declaration,
            evidence=_EVIDENCE, status=DriverStatus.SOFTWARE_CONTRACT_VERIFIED,
        )
        for driver_id in ("camctl-adb", "alternate-camera")
    ))


@pytest.fixture
def environment(tmp_path):
    from camctl.persistence.repositories.capture import register_capture_guards
    from camctl.persistence.repositories.cancellation import register_cancellation_guards
    from camctl.persistence.repositories.operations import register_operation_guards
    from camctl.persistence.repositories.motor import register_motor_guards
    from camctl.persistence.repositories.outputs import register_outputs_guards
    from camctl.persistence.repositories.scheduling import register_window_guard
    from camctl.persistence.repositories.timelapse import register_timelapse_guards
    from camctl.reporting.policy import register_report_guards
    from camctl.session.clock import register_clock_guard

    register_acceptance_guards()
    register_clock_guard()
    register_capture_guards()
    register_cancellation_guards()
    register_operation_guards()
    register_outputs_guards()
    register_window_guard()
    register_timelapse_guards()
    register_report_guards()
    register_motor_guards()
    path = tmp_path / "state.db"
    _create_valid_database(path)
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    context = SimpleNamespace(
        open_connection=lambda: open_existing(path, DbOpenMode.EXISTING_RW, DbConfig()),
        clock=SimpleNamespace(utc_micros=lambda: _NOW),
    )
    try:
        yield owned, context, tmp_path
    finally:
        owned.connection.close()


def _register(owned, body):
    outcome = AcceptanceRepository().process_input(
        ProcessInput(ParsedInput("plan.json", body), _Catalog(), CommandMode.RUN, _NOW),
        new_operation_key(), owned,
    )
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error


def _photos(owned, state):
    schedule = {
        "future": "2026-01-15 10:00:00",
        "expired": "2026-01-15 08:00:00",
    }.get(state, "2026-01-15 09:00:00")
    _register(owned, {
        "request_id": "1", "created_at": "2026-01-15 08:00:00", "name": "双设备",
        "actions": [
            {"name": "正常设备", "type": "camera_take_photo", "device_id": "cam-b",
             "scheduled_at": "2026-01-15 09:00:00", "params": {"type": "single_shot"},
             "policy": {"max_delay_ms": 1000}},
            {"name": "原绑定设备", "type": "camera_take_photo", "device_id": "cam-a",
             "scheduled_at": schedule, "params": {"type": "single_shot"},
             "policy": {"max_delay_ms": 1000}},
        ],
    })
    return {row[0]: row[1] for row in owned.connection.execute("SELECT device_id, id FROM actions")}


def _capture(devices, driver, home):
    factory = session_capture_assembly(
        devices=devices, drivers=_registry(driver), staging=home / "staging",
        results=ResultsDouble({1: (_photo_entry("photo-b", "b.jpg"),),
                               2: (_photo_entry("photo-a", "a.jpg"),)}),
        wall_us=lambda: _NOW, monotonic_ns=lambda: 0,
        wait_config=lambda action: CaptureWaitConfig(target_duration_ms=1000, driver_margin_ms=0),
    )
    return capture_flow(factory)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "mismatch"])
@pytest.mark.parametrize("state", ["due", "future", "expired", "terminal", "canceled"])
async def test_changed_binding_respects_execution_eligibility(environment, change, state):
    owned, context, home = environment
    ids = _photos(owned, state)
    if state == "terminal":
        original = _capture({key: {"driver": "camctl-adb"} for key in ids}, _Driver(), home)
        await original(context)
        await original.settle()
    if state == "canceled":
        _register(owned, {
            "request_id": "2", "created_at": "2026-01-15 08:00:00", "name": "取消",
            "actions": [{"name": "取消原设备", "type": "cancel_task",
                         "params": {"target": {"action_instance_id": str(ids["cam-a"])}}}],
        })
        await cancel_flow(ready=home / "ready", processing=home / "processing")(context)
    devices = {"cam-b": {"driver": "camctl-adb"}}
    if change == "mismatch":
        devices["cam-a"] = {"driver": "alternate-camera"}
    driver = _Driver()

    capture = _capture(devices, driver, home)
    await capture(context)
    await capture.settle()

    row = owned.connection.execute(
        "SELECT status, execution_started, cancel_requested, driver_id, error_code, error_details_json"
        " FROM actions WHERE id = ?", (ids["cam-a"],),
    ).fetchone()
    assert row[:4] == {
        "due": (4, 1, 0, "camctl-adb"), "future": (1, 0, 0, "camctl-adb"),
        "expired": (5, 0, 0, "camctl-adb"), "terminal": (3, 1, 0, "camctl-adb"),
        "canceled": (6, 0, 1, "camctl-adb"),
    }[state]
    if state == "due":
        assert row[4] == registered_error("device_binding_unavailable")["action_error_id"]
        expected = {"device_id": "cam-a", "expected_driver_id": "camctl-adb", "reason": change}
        if change == "mismatch":
            expected["actual_driver_id"] = "alternate-camera"
        assert json.loads(row[5]) == expected
    assert not any(binding.device_id == "cam-a" for binding in driver.calls)
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM operation_attempts t JOIN operation_runs r ON r.id = t.run_id"
        " WHERE r.action_id = ?", (ids["cam-a"],),
    ).fetchone()[0] == (2 if state == "terminal" else 0)
    assert owned.connection.execute(
        "SELECT status FROM actions WHERE id = ?", (ids["cam-b"],),
    ).fetchone() == (3,)


class _RecoveryDriver:
    """控制与停止均受设备端口契约约束，记录不允许发生的新调用。"""

    def __init__(self):
        self.calls = []

    async def control(self, request):
        self.calls.append(request)
        return DeviceCallResult(observations=(), error=None)

    async def stop(self, request):
        self.calls.append(request)
        return DeviceCallResult(observations=(), error=None)


def _recovering_runtime(owned, home, change, driver, results, *,
                        recovery_boundary=None, recovery_max_event_id=None):
    from ..capture.test_capture_contract import _EVIDENCE as contracts, _NOW as now
    from camctl.devices.evidence import EvidenceContract, EvidenceRegistry

    declaration = DriverDeclaration(
        control_supported=True, stop_supported=True, query_supported=False,
        result_supported=False, read_supported=False, digest_supported=False,
        delete_supported=False,
    )
    original_declaration = declaration
    original_contracts = contracts
    if recovery_boundary is not None:
        from dataclasses import replace
        original_declaration = replace(
            declaration, adb_foreground_recovery_operations=frozenset({"control"}))
        original_contracts = EvidenceRegistry((EvidenceContract(
            type="adb_foreground_recovery", version=1, operation="control", fields=frozenset()),))
    registry = DriverRegistry((
        DriverEntry(driver_id="camctl-adb", driver=driver, declaration=original_declaration,
                    evidence=original_contracts, status=DriverStatus.SOFTWARE_CONTRACT_VERIFIED),
        DriverEntry(driver_id="alternate-camera", driver=driver, declaration=declaration,
                    evidence=contracts, status=DriverStatus.SOFTWARE_CONTRACT_VERIFIED),
    ))
    factory = session_capture_assembly(
        devices={} if change == "missing" else {"cam-1": {"driver": "alternate-camera"}},
        drivers=registry, staging=home / "staging", results=results,
        wall_us=lambda: now, monotonic_ns=lambda: 0,
        wait_config=lambda action: CaptureWaitConfig(target_duration_ms=1000, driver_margin_ms=0),
        **({"recovery_boundary": recovery_boundary,
            "recovery_max_event_id": lambda: recovery_max_event_id}
           if recovery_boundary is not None else {}),
    )
    return factory(owned, "cam-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "mismatch"])
@pytest.mark.parametrize("action_id,kind,canceled", [
    (11, "camera_take_photo", False), (12, "camera_record", False),
    (13, "camera_timelapse", False), (12, "camera_record", True),
    (13, "camera_timelapse", True),
])
async def test_changed_binding_preserves_unknown_capture_effect(
    environment, change, action_id, kind, canceled,
):
    from camctl.capture.handlers import capture_handler
    from camctl.operations.models import (
        AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue,
        Settlement, SettlementBasis,
    )
    from ..capture.test_capture_contract import _environment, _runtime
    from ..capture.test_cancel_late_start import _cancel

    _, _, home = environment
    recovery = home / "recovery"
    recovery.mkdir()
    action_type = {"camera_take_photo": 1, "camera_record": 2, "camera_timelapse": 3}[kind]
    owned = _environment(recovery, ((action_id, action_type),))
    try:
        initial = _runtime(owned)
        ticket, reason = initial.grant(initial.action(action_id))
        assert ticket is not None, reason
        initial.finish(ticket, CallOutcome(
            status=AttemptStatus.UNKNOWN, effect=EffectState.UNKNOWN,
            error=ErrorValue("transport_timeout", "transport"), observations=(),
            settlement=Settlement(SettlementBasis.OBSERVED,
                                  EvidenceValue("operation_returned", 1, {})),
        ))
        if canceled:
            _cancel(owned, action_id)
        activity_before = owned.connection.execute(
            "SELECT activity_state, occupancy_state, dispatch_state, started_at, sent_at"
            " FROM device_activities WHERE action_id = ?", (action_id,),
        ).fetchone()
        attempts_before = tuple(owned.connection.execute("SELECT * FROM operation_attempts"))
        driver = _RecoveryDriver()
        results = ResultsDouble({})
        runtime = _recovering_runtime(owned, recovery, change, driver, results)

        await capture_handler(kind)(action_id, runtime)

        assert owned.connection.execute(
            "SELECT status, cancel_requested, driver_id FROM actions WHERE id = ?", (action_id,),
        ).fetchone() == (6 if canceled else 4, int(canceled), "camctl-adb")
        assert driver.calls == []
        assert results.calls == []
        assert tuple(owned.connection.execute("SELECT * FROM operation_attempts")) == attempts_before
        assert owned.connection.execute(
            "SELECT activity_state, occupancy_state, dispatch_state, started_at, sent_at"
            " FROM device_activities WHERE action_id = ?", (action_id,),
        ).fetchone() == activity_before
        if canceled:
            assert owned.connection.execute(
                "SELECT status, attempts_used FROM operation_runs WHERE responsibility_key = ?",
                (f"stop/{action_id}",),
            ).fetchone() == (4, 0)
            from camctl.cancellation.settlement import TargetSettlement
            from camctl.persistence.repositories.cancellation import CancellationRepository

            settled = await TargetSettlement(
                owned, None, CancellationRepository(), lambda _: "unknown", runtime.wall_us,
            ).settle(action_id)
            assert settled.complete and settled.failed
        else:
            assert owned.connection.execute(
                "SELECT status, attempts_used FROM operation_runs WHERE responsibility_key = ?",
                (f"start/{action_id}",),
            ).fetchone() == (4, 1)
        before = tuple(owned.connection.iterdump())
        await capture_handler(kind)(action_id, runtime)
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "mismatch"])
@pytest.mark.parametrize("action_id,kind", [
    (11, "camera_take_photo"), (12, "camera_record"), (13, "camera_timelapse"),
])
async def test_unsettled_intent_without_recovery_boundary_preserves_responsibility(
        environment, change, action_id, kind):
    from camctl.capture.handlers import capture_handler
    from ..capture.test_capture_contract import _environment, _runtime

    _, _, home = environment
    recovery = home / "unsettled"
    recovery.mkdir()
    action_type = {"camera_take_photo": 1, "camera_record": 2, "camera_timelapse": 3}[kind]
    owned = _environment(recovery, ((action_id, action_type),))
    try:
        initial = _runtime(owned)
        ticket, reason = initial.grant(initial.action(action_id))
        assert ticket is not None, reason
        driver = _RecoveryDriver()
        results = ResultsDouble({})
        runtime = _recovering_runtime(owned, recovery, change, driver, results)
        before = tuple(owned.connection.iterdump())

        await capture_handler(kind)(action_id, runtime)

        assert driver.calls == []
        assert results.calls == []
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "mismatch"])
@pytest.mark.parametrize("action_id,kind,canceled", [
    (11, "camera_take_photo", False), (12, "camera_record", False),
    (13, "camera_timelapse", False), (12, "camera_record", True),
    (13, "camera_timelapse", True),
])
async def test_reliable_old_call_boundary_finishes_original_intent_before_binding_failure(
        environment, change, action_id, kind, canceled):
    from camctl.capture.handlers import capture_handler
    from camctl.capture.recovery import RecoveryBoundary
    from camctl.contracts.enums import enum_for
    from ..capture.test_capture_contract import _environment, _runtime
    from ..capture.test_cancel_late_start import _cancel

    _, _, home = environment
    recovery = home / "confirmed-boundary"
    recovery.mkdir()
    action_type = {"camera_take_photo": 1, "camera_record": 2, "camera_timelapse": 3}[kind]
    owned = _environment(recovery, ((action_id, action_type),))
    try:
        original = _runtime(owned)
        ticket, reason = original.grant(original.action(action_id))
        assert ticket is not None, reason
        if canceled:
            _cancel(owned, action_id)
        fixed_h = owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0]
        old_activity = owned.connection.execute(
            "SELECT * FROM device_activities WHERE action_id = ?", (action_id,)).fetchone()
        driver = _RecoveryDriver()
        results = ResultsDouble({})
        runtime = _recovering_runtime(
            owned, recovery, change, driver, results,
            recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=fixed_h)

        await capture_handler(kind)(action_id, runtime)

        assert driver.calls == []
        assert results.calls == []
        assert owned.connection.execute(
            "SELECT status, driver_id FROM actions WHERE id = ?", (action_id,)).fetchone() == (
                6 if canceled else 4, "camctl-adb")
        rows = owned.connection.execute(
            "SELECT attempt_no, status, effect_state, result_json, error_json FROM operation_attempts"
            " WHERE run_id = ?", (ticket.run_id,),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][:3] == (ticket.attempt_id,
                              int(enum_for("operation_attempts.status").UNKNOWN),
                              int(enum_for("operation_attempts.effect_state").UNKNOWN))
        assert json.loads(rows[0][3]) == {
            "format_version": 1,
            "settlement": {"basis": "assumed", "evidence": {
                "type": "adb_foreground_recovery", "version": 1, "data": {}}},
            "observations": [],
        }
        assert json.loads(rows[0][4]) == {"code": "result_not_saved", "stage": "recovery", "details": {}}
        assert owned.connection.execute(
            "SELECT * FROM device_activities WHERE action_id = ?", (action_id,)).fetchone() == old_activity
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts WHERE status = 1").fetchone() == (0,)
    finally:
        owned.connection.close()

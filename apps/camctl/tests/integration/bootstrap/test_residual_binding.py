"""原驱动残留收场及其真实调用参数在后续会话继续沿原责任处理。"""

from decimal import Decimal
import json

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode
from camctl.bootstrap.capture_assembly import session_capture_assembly
from camctl.bootstrap.flows import cancel_flow, capture_flow
from camctl.capture.handlers import _binding, _operation_outcome
from camctl.capture.residual import (
    _advance_winddown, _unfinished_winddown, pass_residual_gate, residual_candidates,
)
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.contracts.values import new_operation_key
from camctl.contracts.workflow_errors import registered_error
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import ControlRequest, DeviceCallResult, DriverDeclaration
from camctl.operations.attempts import (
    AttemptConfig, AttemptIntent, AttemptTarget, OperationKind, RunOutcome,
)
from camctl.operations.models import ErrorValue
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput
from camctl.persistence.repositories.capture import (
    FinishCapture, OutputCatalogFacts, RecordingFailure,
)
from camctl.persistence.repositories.history import HistoryRepository

from ..capture.test_capture_contract import ResultsDouble
from .test_binding_changes import environment, _NOW
from .test_residual_winddown import _EVIDENCE, _WinddownCatalog


class _Catalog(_WinddownCatalog):
    def __init__(self, identity):
        self.identity = identity

    def driver_id(self, device_id):
        return self.identity if self.device_exists(device_id) else None


class _Driver:
    """活动 1 保持执行；停止返回场景指定的已结束调用结果。"""

    def __init__(self, stop_error=False):
        self.stop_error = stop_error
        self.calls = []

    async def control(self, request):
        self.calls.append(("control", request))
        return DeviceCallResult(observations=(DeviceObservation(
            type="start_confirmed", version=1, data={"activity_id": "1"}),), error=None)

    async def query_state(self, request):
        self.calls.append(("query", request))
        return DeviceCallResult(observations=(DeviceObservation(
            type="activity_status", version=1, data={"activity_id": "1"}),), error=None)

    async def stop(self, request):
        self.calls.append(("stop", request))
        return DeviceCallResult(
            observations=(), error={"code": "device_error"} if self.stop_error else None)


def _factory(home, identity, driver):
    declaration = DriverDeclaration(
        control_supported=True, stop_supported=True, query_supported=True,
        result_supported=False, read_supported=False, digest_supported=False,
        delete_supported=False,
    )
    registry = DriverRegistry(tuple(DriverEntry(
        driver_id=driver_id, driver=driver, declaration=declaration,
        evidence=_EVIDENCE, status=DriverStatus.SOFTWARE_CONTRACT_VERIFIED,
    ) for driver_id in ("camctl-adb", "alternate-camera")))
    assembly = session_capture_assembly(
        devices={"cam-1": {"driver": identity}}, drivers=registry,
        staging=home / "staging", results=ResultsDouble({}),
        wall_us=lambda: _NOW, monotonic_ns=lambda: 0,
        wait_config=lambda action: CaptureWaitConfig(
            target_duration_ms=1000, driver_margin_ms=0),
    )

    def factory(owned, device_id):
        runtime = assembly(owned, device_id)
        runtime.query_config = AttemptConfig(3, Decimal("17"), Decimal("0"))
        runtime.residual_config = AttemptConfig(3, Decimal("13"), Decimal("0"))
        return runtime

    return factory


def _accept(owned, request_id, action, driver_id="camctl-adb"):
    body = {"request_id": str(request_id), "created_at": "2026-01-15 08:00:00",
            "name": f"plan-{request_id}", "actions": [action]}
    outcome = AcceptanceRepository().process_input(
        ProcessInput(ParsedInput("plan.json", body), _Catalog(driver_id),
                     CommandMode.RUN, _NOW), new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    return owned.connection.execute("SELECT MAX(id) FROM actions").fetchone()[0]


def _action(action_type, name):
    return {"name": name, "type": action_type, "device_id": "cam-1",
            "scheduled_at": "2026-01-15 09:00:00",
            "params": {"type": "video" if action_type == "camera_record" else "single_shot"},
            "policy": {"max_delay_ms": 1000}}


async def _residual(environment, *, stop_error=False, cancel_trigger=False):
    owned, context, home = environment
    driver = _Driver(stop_error)
    factory = _factory(home, "camctl-adb", driver)
    old_action = _accept(owned, 1, _action("camera_record", "原录像"))
    capture = capture_flow(factory)
    await capture(context)
    await capture.settle()
    runtime = factory(owned, "cam-1")
    activity = owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id = ?", (old_action,)).fetchone()[0]
    begin = runtime.operations.begin_attempt(AttemptIntent(
        operation="stop", action_id=old_action, kind=OperationKind.STOP,
        target=AttemptTarget(activity_id=activity), query_purpose=None,
        config=AttemptConfig(1, Decimal("7")), occurred_at=_NOW),
        new_operation_key(), owned)
    assert begin.kind is DbOutcomeKind.COMPLETED, begin.error
    ticket = begin.value.ticket
    error = ErrorValue(code="recording_stop_failed", stage="device_stop",
                       details={"activity_id": str(activity), "operation_run_id": str(ticket.run_id)})
    response = await runtime.stopper.stop(ControlRequest(
        operation="stop_recording", binding=_binding(runtime.action(old_action)),
        params=runtime.action(old_action)["effective_params_json"],
        ticket=ticket, timeout_s=Decimal("7")))
    outcome, _ = _operation_outcome(response, "stop_confirmed", evidence_type="stop_returned")
    runtime.finish(ticket, outcome, end_run=RunOutcome.FAILED, run_error=error)
    finish = runtime.capture.finish_capture(FinishCapture(
        action_id=old_action, drafts=(), catalog_facts=OutputCatalogFacts(old_action, True),
        occurred_at=_NOW, failure=RecordingFailure(code=error.code, details=error.details)),
        new_operation_key(), owned)
    assert finish.kind is DbOutcomeKind.COMPLETED, finish.error
    trigger = _accept(owned, 2, _action("camera_take_photo", "原收场触发"))
    assert not await pass_residual_gate(runtime, runtime.action(trigger))
    flow = _unfinished_winddown(owned.connection, activity)
    assert flow is not None and flow[1:] == (trigger, 1)
    if cancel_trigger:
        _accept(owned, 3, {"name": "取消原触发", "type": "cancel_task",
                          "params": {"target": {"action_instance_id": str(trigger)}}})
        await cancel_flow(ready=home / "ready", processing=home / "processing")(context)
        assert runtime.action(trigger)["status"] == 6
        assert _unfinished_winddown(owned.connection, activity) == flow
    return runtime, driver, factory, old_action, trigger, activity, flow


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,expected_timeout", [("query", "17"), ("stop", "13")])
async def test_residual_first_call_carries_attempt_ticket_and_timeout(
        environment, operation, expected_timeout):
    runtime, driver, _, _, trigger, activity, flow = await _residual(environment)
    request = [request for call, request in driver.calls if call == operation][-1]

    assert request.ticket is not None
    assert request.ticket.responsibility_key == (
        f"query/preflight/{trigger}" if operation == "query" else f"followup/{trigger}/{activity}")
    assert request.timeout_s == Decimal(expected_timeout)
    assert request.ticket.run_id == runtime.owned.connection.execute(
        "SELECT id FROM operation_runs WHERE responsibility_key = ?",
        (request.ticket.responsibility_key,)).fetchone()[0]


@pytest.mark.asyncio
async def test_residual_confirmation_carries_attempt_ticket_and_timeout(environment):
    runtime, driver, _, _, trigger, activity, flow = await _residual(environment)
    candidate = residual_candidates(runtime.owned.connection, "cam-1")[0]

    await _advance_winddown(runtime, candidate, flow)

    request = [request for call, request in driver.calls if call == "query"][-1]
    assert request.ticket is not None
    assert request.ticket.responsibility_key == f"query/residual/{trigger}/{activity}"
    assert request.timeout_s == Decimal("17")


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_error", [False, True])
async def test_new_driver_trigger_preserves_old_driver_residual(environment, stop_error):
    runtime, _, _, old_action, old_trigger, activity, flow = await _residual(
        environment, stop_error=stop_error, cancel_trigger=True)
    owned, context, home = environment
    trigger = _accept(owned, 4, _action("camera_take_photo", "新驱动触发"), "alternate-camera")
    before_activity = owned.connection.execute(
        "SELECT * FROM device_activities WHERE id = ?", (activity,)).fetchone()
    before_attempts = owned.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall()
    before_old = dict(runtime.action(old_action))
    history = HistoryRepository(home / "state.db")
    frozen_boundary = history.current_boundary()
    frozen_old = history.restore_entity("action", old_action, frozen_boundary)
    current_driver = _Driver(stop_error)

    capture = capture_flow(_factory(home, "alternate-camera", current_driver))
    await capture(context)
    await capture.settle()

    assert current_driver.calls == []
    assert owned.connection.execute(
        "SELECT * FROM device_activities WHERE id = ?", (activity,)).fetchone() == before_activity
    assert owned.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall() == before_attempts
    after_old = dict(runtime.action(old_action))
    assert {key: value for key, value in after_old.items() if key not in ("last_event_id", "change_count")} == {
        key: value for key, value in before_old.items() if key not in ("last_event_id", "change_count")}
    original_links = owned.connection.execute(
        "SELECT l.event_id, l.change_count, e.event_type, e.body_json"
        " FROM entity_event_links l JOIN history_events e ON e.id = l.event_id"
        " WHERE l.entity_type = 1 AND l.entity_id = ? AND l.event_id > ? ORDER BY l.event_id",
        (old_action, frozen_boundary.last_event_id),
    ).fetchall()
    assert len(original_links) == 1
    event_id, change_count, event_type, body = original_links[0]
    assert (event_type, change_count, after_old["last_event_id"], after_old["change_count"]) == (
        10, before_old["change_count"] + 1, event_id, change_count)
    event = json.loads(body)
    assert event["reason"] == 3
    assert [(row["table"], row["id"]) for row in event["rows"]] == [("operation_runs", flow[0])]
    assert owned.connection.execute(
        "SELECT entity_type, entity_id FROM entity_event_links WHERE event_id = ?", (event_id,),
    ).fetchall() == [(1, old_action)]
    assert history.restore_entity("action", old_action, frozen_boundary) == frozen_old
    failed_flow = owned.connection.execute(
        "SELECT status, attempts_used, error_json FROM operation_runs WHERE id = ?", (flow[0],)).fetchone()
    assert failed_flow[:2] == (4, 1)
    assert json.loads(failed_flow[2]) == {
        "code": "device_binding_unavailable", "stage": "execution", "details": {
            "device_id": "cam-1", "expected_driver_id": "camctl-adb",
            "actual_driver_id": "alternate-camera", "reason": "mismatch"}}
    result = runtime.action(trigger)
    assert (result["status"], result["execution_started"], result["driver_id"], result["error_code"]) == (
        4, 1, "alternate-camera", registered_error("device_activity_unresolved")["action_error_id"])
    assert result["error_details_json"] == {"activity_id": str(activity), "device_id": "cam-1"}
    assert runtime.action(old_trigger)["status"] == 6

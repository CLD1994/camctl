"""应急补记的公开活动错误及固定历史报告重建。"""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode
from camctl.capture.media import RecordingFailure
from camctl.capture.handlers import capture_handler
from camctl.capture.recovery import EmergencyOutcome, EmergencyRecord
from camctl.contracts.pages import Page
from camctl.contracts.schemas import validate_document
from camctl.contracts.values import new_operation_key
from camctl.devices.directory import DirectoryRead
from camctl.devices.ports import DeviceCallResult, DriverDeclaration
from camctl.operations.models import (
    AttemptStatus, CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis,
)
from camctl.outputs.catalog import OutputCatalogFacts
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput
from camctl.persistence.repositories.capture import CaptureRepository, FinishCapture
from camctl.persistence.repositories.scheduling import (
    ObserveWindowRequest, SchedulingRepository, StartActionRequest,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.reporting.generation import generate_report_file

from ..bootstrap.test_media_assembly import _config
from ..capture.public_assembly import assemble_capture
from ..capture.result_consumer_fixtures import RESULT_EVIDENCE, ResultCatalog, returned
from ..capture.test_capture_contract import ResultsDouble, _NOW
from ..capture.test_emergency import _attempt, _stop_observation
from .test_result_error_generation import _freeze, _spec

pytestmark = pytest.mark.asyncio


class RecordingDriver:
    def __init__(self, activity_case):
        self.activity_case = activity_case
        self.calls = []

    async def read_directory(self, request, *, stop):
        return DirectoryRead(None, None, Page((), None))

    async def control(self, request):
        self.calls.append(request.operation)
        if self.activity_case == "unknown":
            return DeviceCallResult.from_outcome(CallOutcome(
                status=AttemptStatus.SUCCEEDED, error=None, effect=EffectState.UNKNOWN,
                settlement=Settlement(SettlementBasis.OBSERVED,
                                      EvidenceValue("operation_returned", 1, {})),
                observations=()))
        return DeviceCallResult.from_outcome(returned("operation", "start_confirmed", 1))

    async def stop(self, request):
        self.calls.append(request.operation)
        return DeviceCallResult.from_outcome(returned("stop", "stop_confirmed", 1))


async def recording_world(tmp_path, activity_case):
    cfg = _config(tmp_path)
    path = Path(cfg.paths.state_db)
    assert initialize_state(cfg, path).outcome is InitOutcome.CREATED
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    database = None
    try:
        instant = datetime.fromtimestamp(_NOW // 1_000_000, timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S")
        result = AcceptanceRepository().process_input(ProcessInput(ParsedInput("report.json", {
            "request_id": "1", "created_at": instant, "name": "报告",
            "actions": [
                {"name": "报告", "type": "report_status", "params": {"scope": "full"}},
                {"name": "拍摄", "type": "camera_record", "device_id": "cam-1",
                 "scheduled_at": instant, "params": {"type": "video"},
                 "policy": {"max_delay_ms": 5000}},
            ]}), ResultCatalog(), CommandMode.RUN, _NOW), new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        scheduling = SchedulingRepository()
        for save, request in ((scheduling.observe_window, ObserveWindowRequest(2, _NOW, _NOW)),
                              (scheduling.start_action, StartActionRequest(2, _NOW, _NOW))):
            result = save(request, new_operation_key(), owned)
            assert result.kind is DbOutcomeKind.COMPLETED, result.error
        driver = RecordingDriver(activity_case)
        declaration = DriverDeclaration(True, True, False, False, False, False, False,
                                        directory_supported=True)
        clock = {"wall": _NOW, "monotonic": 5_000_000_000}
        runtime, database = await assemble_capture(
            owned, driver=driver, evidence=RESULT_EVIDENCE, declaration=declaration,
            results=ResultsDouble({}), staging=Path(cfg.paths.staging),
            wall_us=lambda: clock["wall"], monotonic_ns=lambda: clock["monotonic"],
        )
        await capture_handler("camera_record")(2, runtime)
        if activity_case == "ended":
            clock.update(wall=_NOW + 60_000_000, monotonic=65_000_000_000)
            await capture_handler("camera_record")(2, runtime)
        return owned, database, runtime, driver, 2
    except BaseException:
        if database is not None:
            await database.close()
        owned.connection.close()
        raise


@pytest.mark.parametrize("outcome, code, attempts", [
    (EmergencyOutcome.NOT_ATTEMPTED, "emergency_not_attempted", ()),
    (EmergencyOutcome.UNCONFIRMED, "emergency_stop_unconfirmed", (
        _attempt(status=4, error={"code": "timeout", "stage": "transport"}),)),
])
@pytest.mark.parametrize("activity_case", ["active", "unknown", "ended"])
async def test_emergency_error_report_keeps_actual_target_reason_and_original_action(
        tmp_path, outcome, code, attempts, activity_case):
    owned, database, runtime, driver, action_id = await recording_world(tmp_path, activity_case)
    reopened = None
    try:
        activity_id, = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
        assert activity_id != action_id
        assert owned.connection.execute(
            "SELECT activity_state FROM device_activities WHERE id=?",
            (activity_id,)).fetchone() == ({"unknown": 1, "active": 2, "ended": 3}[activity_case],)
        assert driver.calls == (["start_recording", "stop_recording"] if activity_case == "ended"
                                else ["start_recording"])
        if activity_case == "unknown":
            assert owned.connection.execute(
                "SELECT dispatch_state,occupancy_state,sent_at IS NOT NULL,started_at"
                " FROM device_activities WHERE id=?", (activity_id,)).fetchone() == (3, 1, 1, None)
        final = CaptureRepository().finish_capture(FinishCapture(
            action_id, (), OutputCatalogFacts(action_id, ownership_confirmed=True),
            runtime.wall_us(), failure=RecordingFailure("capture_result_unconfirmed", {
                "activity_id": str(activity_id),
                "reason": "start_unknown" if activity_case == "unknown" else "outputs_unknown"})),
            new_operation_key(), owned)
        assert final.kind is DbOutcomeKind.COMPLETED, final.error
        original_action = owned.connection.execute(
            "SELECT status,error_code,error_details_json FROM actions WHERE id=?",
            (action_id,)).fetchone()
        reason = '原停止条件未确认；设备说明："不可用"\n本次保留原原因'
        saved = CaptureRepository().save_emergency(
            session_key="a" * 32, action_id=action_id, activity_id=activity_id,
            record=EmergencyRecord(outcome, len(attempts), 3, reason=reason),
            attempts=attempts, occurred_at=runtime.wall_us(), key=new_operation_key(),
            owned=owned, timeout_s=Decimal("1"), retry_interval_s=Decimal("0"))
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        expected = {"code": code, "stage": "emergency", "details": {
            "activity_id": str(activity_id), "reason": reason}}
        report = _freeze(owned, runtime.wall_us())
        spec = _spec(owned, report, "emergency-before.json")
        original_history = owned.connection.execute(
            "SELECT * FROM history_events WHERE id<=? ORDER BY id",
            (report.boundary.last_event_id,)).fetchall()
        generated = generate_report_file(spec)
        payload = generated.path.read_bytes()
        document = json.loads(payload)
        validate_document("protocol/status-report.schema.json", document)
        action, = [action for plan in document["plans"] for action in plan["actions"]
                   if action["action_instance_id"] == str(action_id)]
        assert action["status"] == "failed"
        assert action["error"] == {"code": "capture_result_unconfirmed", "stage": "execution",
                                   "details": {"activity_id": str(activity_id),
                                               "reason": "start_unknown" if activity_case == "unknown"
                                               else "outputs_unknown"}}
        if activity_case == "ended":
            assert "device_execution" not in action
        else:
            status = "still_running" if activity_case == "active" else "end_unconfirmed"
            assert action["device_execution"] == {"status": status, "error": expected}
        calls = tuple(driver.calls)
        owned.connection.close()
        reopened = open_existing(spec.db_path, DbOpenMode.EXISTING_RW, DbConfig())
        later = CaptureRepository().save_emergency(
            session_key="b" * 32, action_id=action_id, activity_id=activity_id,
            record=EmergencyRecord(EmergencyOutcome.STOPPED, 0, 3,
                                   stop_observation=_stop_observation(activity_id)),
            attempts=(), occurred_at=runtime.wall_us() + 1, key=new_operation_key(),
            owned=reopened, timeout_s=Decimal("1"), retry_interval_s=Decimal("0"))
        assert later.kind is DbOutcomeKind.COMPLETED, later.error
        rebuilt = generate_report_file(replace(spec, staging_path=spec.staging_path.with_name(
            "emergency-after.json")))
        assert rebuilt.path.read_bytes() == payload
        assert (rebuilt.sha256, rebuilt.size_bytes) == (generated.sha256, generated.size_bytes)
        assert reopened.connection.execute(
            "SELECT * FROM history_events WHERE id<=? ORDER BY id",
            (report.boundary.last_event_id,)).fetchall() == original_history
        assert reopened.connection.execute(
            "SELECT status,error_code,error_details_json FROM actions WHERE id=?",
            (action_id,)).fetchone() == original_action
        assert tuple(driver.calls) == calls
    finally:
        await database.close()
        if reopened is not None:
            reopened.connection.close()
        else:
            owned.connection.close()

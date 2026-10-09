"""有限 RESULTS 耗尽的真实历史生成合法报告，并按固定 H 重建。"""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode
from camctl.capture.handlers import capture_handler
from camctl.capture.results import FileKind
from camctl.contracts.enums import enum_for
from camctl.contracts.schemas import validate_document
from camctl.contracts.values import new_operation_key
from camctl.contracts.workflow_errors import registered_error
from camctl.operations.attempts import AttemptConfig
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.reporting.generation import GenerationSpec, generate_report_file
from camctl.reporting.policy import (
    ReportDecisionKind, ReportingRepository, register_report_guards, register_sync_guard,
)

from ..capture.result_consumer_fixtures import ResultCatalog, consumer_world
from ..capture.test_capture_contract import ResultsDouble, _entry

register_outputs_guards()
register_report_guards()
register_sync_guard()
pytestmark = pytest.mark.asyncio


async def _exhausted_world(tmp_path, consumer, independent_activity):
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, consumer, independent_activity=independent_activity)
    try:
        activity_id, = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
        assert (action_id != activity_id) is independent_activity
        entry = replace(_entry("auxiliary", size=41, kind=FileKind.OTHER),
                        original_name="auxiliary.bin", media_type="application/octet-stream")
        results = ResultsDouble({activity_id: (entry,)})
        runtime.results = results
        runtime.check_config = AttemptConfig(2, Decimal("1.25"), Decimal(0))
        advance = capture_handler(handler)
        for _ in range(2):
            await advance(action_id, runtime)
            assert runtime.action(action_id)["status"] == int(enum_for("actions.status").RUNNING)
        attempts = owned.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall()
        await advance(action_id, runtime)
        assert runtime.action(action_id)["status"] == int(enum_for("actions.status").FAILED)
        run_status, count, raw_error = owned.connection.execute(
            "SELECT status,attempts_used,error_json FROM operation_runs WHERE responsibility_key=?",
            (f"results/{activity_id}",)).fetchone()
        assert (run_status, count) == (int(enum_for("operation_runs.status").UNCONFIRMED), 2)
        error = {"code": "capture_result_unconfirmed",
                 "stage": registered_error("capture_result_unconfirmed")["stage"],
                 "details": {"activity_id": str(activity_id), "reason": "outputs_unknown"}}
        assert json.loads(raw_error) == error
        capture, last_error = owned.connection.execute(
            "SELECT capture_json,last_error_json FROM device_activities WHERE id=?",
            (activity_id,)).fetchone()
        assert json.loads(capture) == {"status": "unconfirmed", "error": error}
        assert json.loads(last_error) == error
        action_code, details = owned.connection.execute(
            "SELECT error_code,error_details_json FROM actions WHERE id=?", (action_id,)).fetchone()
        assert action_code == registered_error("capture_result_unconfirmed")["action_error_id"]
        assert json.loads(details) == error["details"]
        assert owned.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall() == attempts
        assert results.calls == [activity_id, activity_id]
        assert runtime.driver.control.await_count == 1
        return owned, runtime, action_id, error
    except BaseException:
        owned.connection.close()
        raise


def _freeze(owned, occurred_at):
    saved = ReportingRepository().freeze_report(new_operation_key(), owned, occurred_at=occurred_at)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    assert saved.value.kind is ReportDecisionKind.GENERATE
    return saved.value.report


def _spec(owned, report, name):
    db_path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    return GenerationSpec(
        db_path=db_path, report_id=report.report_id,
        from_wm=report.from_wm, to_wm=report.to_wm,
        frozen_event_id=report.boundary.last_event_id,
        staging_path=Path(owned.metadata.staging_path) / "reports" / name,
    )


def _assert_document(generated, report, action_id, expected_error, execution_status):
    payload = generated.path.read_bytes()
    assert generated.size_bytes == len(payload)
    assert generated.sha256 == hashlib.sha256(payload).hexdigest()
    document = json.loads(payload)
    validate_document("protocol/status-report.schema.json", document)
    assert document["report_id"] == str(report.report_id)
    assert (document["from_wm"], document["to_wm"]) == (report.from_wm, report.to_wm)
    action, = [action for plan in document["plans"] for action in plan["actions"]
               if action["action_instance_id"] == str(action_id)]
    assert action["status"] == "failed"
    assert action["error"] == expected_error
    assert action["device_execution"] == {"status": execution_status, "error": expected_error}
    return payload


@pytest.mark.parametrize("consumer,independent_activity,execution_status", [
    ("photo", False, "still_running"),
    ("timelapse", True, "end_unconfirmed"),
])
async def test_exhausted_results_generate_schema_valid_public_errors(
        tmp_path, consumer, independent_activity, execution_status):
    owned, runtime, action_id, error = await _exhausted_world(
        tmp_path, consumer, independent_activity)
    try:
        report = _freeze(owned, runtime.wall_us())
        calls = tuple(runtime.results.calls), runtime.driver.control.await_count
        generated = generate_report_file(_spec(owned, report, "result-error.json"))
        _assert_document(generated, report, action_id, error, execution_status)
        assert (tuple(runtime.results.calls), runtime.driver.control.await_count) == calls
    finally:
        owned.connection.close()


async def test_exhausted_result_report_rebuilds_same_frozen_history_after_reopen(tmp_path):
    owned, runtime, action_id, error = await _exhausted_world(tmp_path, "timelapse", True)
    reopened = None
    try:
        report = _freeze(owned, runtime.wall_us())
        first_spec = _spec(owned, report, "before-reopen.json")
        history = owned.connection.execute(
            "SELECT * FROM history_events WHERE id<=? ORDER BY id",
            (report.boundary.last_event_id,)).fetchall()
        calls = tuple(runtime.results.calls), runtime.driver.control.await_count
        first = generate_report_file(first_spec)
        first_bytes = _assert_document(first, report, action_id, error, "end_unconfirmed")
        owned.connection.close()
        reopened = open_existing(first_spec.db_path, DbOpenMode.EXISTING_RW, DbConfig())
        instant = datetime.fromtimestamp(runtime.wall_us() // 1_000_000, timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S")
        accepted = AcceptanceRepository().process_input(ProcessInput(ParsedInput("later.json", {
            "request_id": "2", "created_at": instant, "name": "后续拍摄",
            "actions": [{"name": "照片", "type": "camera_take_photo", "device_id": "cam-1",
                         "scheduled_at": instant, "params": {"type": "photo"},
                         "policy": {"max_delay_ms": 5000}}],
        }), ResultCatalog(), CommandMode.RUN, runtime.wall_us() + 1), new_operation_key(), reopened)
        assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
        assert reopened.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0] > report.boundary.last_event_id
        second = generate_report_file(_spec(reopened, report, "after-reopen.json"))
        assert _assert_document(second, report, action_id, error, "end_unconfirmed") == first_bytes
        assert (second.size_bytes, second.sha256) == (first.size_bytes, first.sha256)
        assert reopened.connection.execute(
            "SELECT * FROM history_events WHERE id<=? ORDER BY id",
            (report.boundary.last_event_id,)).fetchall() == history
        assert (tuple(runtime.results.calls), runtime.driver.control.await_count) == calls
    finally:
        if reopened is not None:
            reopened.connection.close()
        else:
            owned.connection.close()

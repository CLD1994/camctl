"""实际失败的错误目标是设备活动，动作与原调用历史各自保持。"""

from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode
from camctl.capture.handlers import (
    _control_call, _finish_listing_result, _listing_round, _register_listing,
    capture_handler,
)
from camctl.capture.models import ResultSetPhase, ResultSetSave, WaitCompletedSave
from camctl.capture.timelapse import WaitKind, WaitPlan
from camctl.contracts.values import new_operation_key
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import ControlDriver, DeviceCallResult
from camctl.operations.models import (
    AttemptStatus, CallInfo, CallOutcome, EffectState, ErrorValue,
    EvidenceValue, Settlement, SettlementBasis,
)
from camctl.operations.attempts import RunOutcome
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import (
    AcceptanceRepository, ProcessInput, register_acceptance_guards,
)
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.operations import register_operation_guards
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.repositories.scheduling import (
    ObserveWindowRequest, SchedulingRepository, StartActionRequest,
    register_window_guard,
)
from camctl.persistence.repositories.timelapse import ScheduleWait, register_timelapse_guards
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..bootstrap.test_media_assembly import _config
from .result_consumer_fixtures import ResultCatalog, RESULT_EVIDENCE, consumer_world
from .test_capture_contract import _NOW, _runtime
from .test_result_consumer_saves import _result_port

pytestmark = pytest.mark.asyncio
register_acceptance_guards()
register_window_guard()
register_operation_guards()
register_capture_guards()
register_outputs_guards()
register_timelapse_guards()


def _start_outcome(*, failed):
    return CallOutcome(
        status=AttemptStatus.FAILED if failed else AttemptStatus.SUCCEEDED,
        error=ErrorValue("transport_timeout", "transport", {"received_bytes": 23})
        if failed else None,
        effect=EffectState.UNKNOWN,
        settlement=Settlement(SettlementBasis.OBSERVED,
                              EvidenceValue("operation_returned", 1, {})),
        observations=(), call_info=CallInfo(local_exit_code=7 if failed else 0),
    )


def _file_outcome(kind):
    return CallOutcome(
        status=AttemptStatus.SUCCEEDED, error=None, effect=EffectState.CONFIRMED,
        settlement=Settlement(SettlementBasis.OBSERVED,
                              EvidenceValue("results_returned", 1, {})),
        observations=(DeviceObservation("result_files_listed", 1, {
            "activity_id": "1", "entries": [{
                "identity": "original", "locator": {"path": "/DCIM/original"},
                "complete": True, "size_bytes": 41, "kind": kind,
                "original_name": "original", "media_type": "image/jpeg"
                if kind == "photo" else "video/mp4", "paired_identity": None,
            }],
        }),), call_info=CallInfo(local_exit_code=0),
    )


async def _failed_start_world(tmp_path, consumer, actual):
    """只在新动作授予后调用一次实际 START，不更改已保存结果。"""
    cfg = _config(tmp_path)
    path = Path(cfg.paths.state_db)
    assert initialize_state(cfg, path).outcome is InitOutcome.CREATED
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    instant = datetime.fromtimestamp(_NOW // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    handler = "camera_take_photo" if consumer == "photo" else "camera_timelapse"
    operation = "take_photo" if consumer == "photo" else "start_timelapse"
    observation = "photo_taken" if consumer == "photo" else "timelapse_sent"
    try:
        accepted = AcceptanceRepository().process_input(ProcessInput(ParsedInput("identity.json", {
            "request_id": "1", "created_at": instant, "name": "失败目标身份",
            "actions": [
                {"name": "报告", "type": "report_status", "params": {"scope": "full"}},
                {"name": "拍摄", "type": handler, "device_id": "cam-1",
                 "scheduled_at": instant, "params": {"type": consumer},
                 "policy": {"max_delay_ms": 5000}},
            ],
        }), ResultCatalog(), CommandMode.RUN, _NOW), new_operation_key(), owned)
        assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
        scheduling = SchedulingRepository()
        for save, command in (
            (scheduling.observe_window, ObserveWindowRequest(2, _NOW, _NOW)),
            (scheduling.start_action, StartActionRequest(2, _NOW, _NOW)),
        ):
            receipt = save(command, new_operation_key(), owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        assert owned.connection.execute(
            "SELECT id,action_id FROM device_activities WHERE action_id=2"
        ).fetchone() == (1, 2)
        driver = create_autospec(ControlDriver, instance=True)
        driver.control.return_value = DeviceCallResult.from_outcome(actual)
        runtime = _runtime(owned, driver=driver)
        runtime.evidence = RESULT_EVIDENCE
        await _control_call(runtime, runtime.action(2), operation, observation,
                            activity_facts=lambda _confirmed: {})
        assert runtime.action(2)["status"] == 2
        assert owned.connection.execute(
            "SELECT sent_at FROM device_activities WHERE id=1").fetchone() == (None,)
        return owned, runtime, handler
    except BaseException:
        owned.connection.close()
        raise


def _attempts(owned):
    return owned.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall()


def _history(owned):
    return owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()


def _reopen(owned, previous):
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    metadata = owned.metadata
    owned.connection.close()
    fresh = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    assert fresh.metadata == metadata
    runtime = _runtime(fresh, driver=previous.driver, results=previous.results,
                       wall=previous.wall_us())
    runtime.evidence = previous.evidence
    return fresh, runtime


def _assert_start_input(owned, *, failed):
    row = owned.connection.execute(
        "SELECT t.status,t.effect_state,t.result_json,t.error_json FROM operation_attempts t"
        " JOIN operation_runs r ON r.id=t.run_id WHERE r.responsibility_key='start/2'"
    ).fetchone()
    assert row[:2] == (3 if failed else 2, 1)
    assert json.loads(row[2]) == {
        "format_version": 1,
        "settlement": {"basis": "observed", "evidence": {
            "type": "operation_returned", "version": 1, "data": {}}},
        "observations": [], "call_info": {"local_exit": {"exit_code": 7 if failed else 0}},
    }
    assert (json.loads(row[3]) if row[3] else None) == (
        {"code": "transport_timeout", "stage": "transport", "details": {"received_bytes": 23}}
        if failed else None)


def _assert_failure(owned, *, code, reason):
    status, error_code, details = owned.connection.execute(
        "SELECT status,error_code,error_details_json FROM actions WHERE id=2").fetchone()
    assert (status, error_code) == (4, code)
    assert json.loads(details) == {"activity_id": "1", "reason": reason}


@pytest.mark.parametrize("failed,code,reason,start_status", [
    (True, 13, "device_failed", 4),
    (False, 12, "start_unknown", 6),
], ids=["failed-start-without-sent-at", "unknown-effect-without-sent-at"])
async def test_start_without_sent_at_reports_actual_activity(
    tmp_path, failed, code, reason, start_status,
):
    actual = _start_outcome(failed=failed)
    owned, runtime, handler = await _failed_start_world(tmp_path, "timelapse", actual)
    try:
        _assert_start_input(owned, failed=failed)
        original_attempts, original_history = _attempts(owned), _history(owned)
        result_driver = _result_port(runtime, _file_outcome("video"))
        result_driver.list_results.side_effect = AssertionError("无发送时刻收场不能查询设备")
        owned, resumed = _reopen(owned, runtime)
        await capture_handler(handler)(2, resumed)

        _assert_failure(owned, code=code, reason=reason)
        assert _attempts(owned) == original_attempts
        assert _history(owned)[:len(original_history)] == original_history
        assert owned.connection.execute(
            "SELECT status,attempts_used FROM operation_runs WHERE responsibility_key='start/2'"
        ).fetchone() == (start_status, 1)
        assert owned.connection.execute(
            "SELECT sent_at,occupancy_state FROM device_activities WHERE id=1"
        ).fetchone() == (None, 1)
        runtime.driver.control.assert_awaited_once()
        result_driver.list_results.assert_not_awaited()
        terminal_history = _history(owned)
        await capture_handler(handler)(2, resumed)
        assert _history(owned) == terminal_history
        _assert_start_input(owned, failed=failed)
        runtime.driver.control.assert_awaited_once()
        result_driver.list_results.assert_not_awaited()
    finally:
        owned.connection.close()


async def test_failed_photo_response_keeps_file_with_actual_activity_error(tmp_path):
    owned, runtime, handler = await _failed_start_world(tmp_path, "photo", _start_outcome(failed=True))
    actual = _file_outcome("photo")
    driver = _result_port(runtime, actual)
    try:
        start_attempts, original_history = _attempts(owned), _history(owned)
        await capture_handler(handler)(2, runtime)

        _assert_failure(owned, code=13, reason="device_failed")
        assert _attempts(owned)[:1] == start_attempts
        assert _history(owned)[:len(original_history)] == original_history
        _assert_start_input(owned, failed=True)
        assert owned.connection.execute(
            "SELECT o.source_action_id,f.source_action_id,f.completion_state,f.size_bytes"
            " FROM outputs o JOIN device_files f ON f.id=o.device_file_id"
        ).fetchall() == [(2, 2, 3, 41)]
        saved = owned.connection.execute(
            "SELECT t.result_json FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
            " WHERE r.responsibility_key='results/1'"
        ).fetchone()
        assert json.loads(saved[0])["observations"] == [{
            "type": "result_files_listed", "version": 1, "data": dict(actual.observations[0].data),
        }]
        driver.list_results.assert_awaited_once()
        assert driver.list_results.call_args.args[0].ticket.target_id == "1"
        runtime.driver.control.assert_awaited_once()
        terminal_attempts, terminal_history = _attempts(owned), _history(owned)
        owned, resumed = _reopen(owned, runtime)
        await capture_handler(handler)(2, resumed)
        assert _attempts(owned) == terminal_attempts and _history(owned) == terminal_history
        driver.list_results.assert_awaited_once()
        runtime.driver.control.assert_awaited_once()
    finally:
        owned.connection.close()


async def test_closed_unsatisfied_timelapse_reports_actual_activity_without_query(tmp_path):
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, "timelapse", independent_activity=True)
    assert action_id == 2
    actual = _file_outcome("video")
    driver = _result_port(runtime, actual)
    try:
        scheduled = runtime.timelapse.schedule_wait(ScheduleWait(
            2, WaitPlan(WaitKind.WAIT_THEN_CHECK, check_at_utc=_NOW + 600_000_000),
            driver_margin_ms=0, extra_wait_ms=0, occurred_at=_NOW), new_operation_key(), owned)
        assert scheduled.kind is DbOutcomeKind.COMPLETED, scheduled.error
        waited = runtime.timelapse.complete_wait(
            WaitCompletedSave(2, runtime.wall_us()), new_operation_key(), owned)
        assert waited.kind is DbOutcomeKind.COMPLETED, waited.error
        listing = await _listing_round(runtime, 2)
        _register_listing(runtime, 2, listing)
        _finish_listing_result(runtime, listing, end_run=RunOutcome.SUCCEEDED,
            result_set=ResultSetSave(
                2, listing.occurred_at, ResultSetPhase.UNSATISFIED,
                contract="task_scope_files", observation={"reason": "known_failure"},
                capture={"status": "failed", "error": {"code": "capture_unsatisfied"}},
                evidence={"method": "known_failure", "observation": {"reason": "known_failure"}},
            ))
        assert runtime.action(2)["status"] == 2
        assert owned.connection.execute(
            "SELECT id,action_id,result_set_state,completion_basis FROM device_activities"
        ).fetchone() == (1, 2, 3, 4)
        original_attempts, original_history = _attempts(owned), _history(owned)
        driver.list_results.side_effect = AssertionError("正式结论恢复不能再次查询设备")
        owned, resumed = _reopen(owned, runtime)
        await capture_handler(handler)(2, resumed)

        _assert_failure(owned, code=13, reason="no_outputs")
        assert _attempts(owned) == original_attempts
        assert _history(owned)[:len(original_history)] == original_history
        assert owned.connection.execute(
            "SELECT source_action_id FROM outputs").fetchall() == [(2,)]
        assert owned.connection.execute(
            "SELECT status,attempts_used FROM operation_runs WHERE responsibility_key='results/1'"
        ).fetchone() == (3, 1)
        assert owned.connection.execute(
            "SELECT occupancy_state FROM device_activities WHERE id=1").fetchone() == (1,)
        terminal_history = _history(owned)
        await capture_handler(handler)(2, resumed)
        assert _history(owned) == terminal_history
        driver.list_results.assert_awaited_once()
        runtime.driver.control.assert_awaited_once()
    finally:
        owned.connection.close()

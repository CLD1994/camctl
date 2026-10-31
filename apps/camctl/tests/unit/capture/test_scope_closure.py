"""后续准备仅推进独立范围责任，原失败结果保持不变。"""

from decimal import Decimal
from unittest.mock import create_autospec
import sqlite3

import pytest

from camctl.capture.handlers import CaptureRuntime, ListingPhase, ListingRound
from camctl.capture.preparation import RuntimePreparationCache
from camctl.capture.scope_closure import advance_ended_scope
from camctl.operations.attempts import AttemptConfig, BeginAttemptResult, BeginDisposition, OperationKind, RunOutcome
from camctl.devices.evidence import DeviceObservation
from camctl.operations.models import (
    AttemptStatus, AttemptTicket, CallOutcome, EffectState, ErrorValue,
    EvidenceValue, Settlement, SettlementBasis,
)
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.result_sources import ResultSource, ResultSourceKind
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from support.capture_database import capture_database


def _world(mocker, *, old_scope=True, error=None, finalized_event_id=None):
    runtime = create_autospec(CaptureRuntime, instance=True)
    old = {"id": 21, "status": 4, "device_id": "camera", "driver_id": "dji-osmo360-ii",
        "execution_spec_json": {"video_result_mode": "segmented"}, "error_code": "capture_failed"}
    trigger = {"id": 22, "status": 1, "cancel_requested": 0, "device_id": "camera",
        "scheduled_at": 1_000_000, "max_delay_ms": 3000}
    runtime.action.side_effect = lambda identity: dict(old if identity == 21 else trigger)
    runtime.prepare_lead_s = Decimal("10")
    runtime.preparation_cache = RuntimePreparationCache()
    runtime.wall_us = mocker.Mock(return_value=1_000_000)
    runtime.binding_check = None
    runtime.pending_result_scans = {}
    runtime.check_config = AttemptConfig(3, Decimal("10"), Decimal("3"))
    runtime.retry_wait_remaining.return_value = None
    runtime.last_attempt.return_value = None
    runtime.results = object()
    connection = create_autospec(sqlite3.Connection, instance=True)

    def execute(sql, parameters=()):
        cursor = create_autospec(sqlite3.Cursor, instance=True)
        if sql.startswith("SELECT d.id,d.action_id"):
            cursor.fetchone.return_value = (7, 21, finalized_event_id) if old_scope else None
        elif sql.startswith("SELECT id,status FROM operation_runs") or sql.startswith("SELECT r.id, a.attempt_no"):
            cursor.fetchone.return_value = None
        else:
            raise AssertionError(sql)
        return cursor

    connection.execute.side_effect = execute
    owned = OwnedConnection(connection, DatabaseMetadata(
        "camctl", "a" * 32, 6, "/staging", "/ready", "/processing"))
    runtime.database = capture_database(owned)
    runtime.operations = create_autospec(OperationRepository, instance=True)
    ticket = AttemptTicket(1, "result", "7", "scope/7", 9)
    runtime.operations.begin_attempt.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        BeginAttemptResult(BeginDisposition.GRANTED, ticket=ticket))
    mocker.patch("camctl.capture.handlers.row_facts", autospec=True,
        return_value={"output_scope_json": {"directories": ["/mnt/media_rw/emulated/DCIM"]}})
    mocker.patch("camctl.capture.handlers._result_source", autospec=True,
        return_value=ResultSource(ResultSourceKind.DIRECT, 10))
    actual = CallOutcome(status=AttemptStatus.SUCCEEDED if error is None else AttemptStatus.FAILED,
        effect=EffectState.CONFIRMED, error=error,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("result_returned", 1)),
        observations=(DeviceObservation("result_files_listed", 2,
            {"activity_id": "7", "entries": [], "cursor": None, "next_cursor": None,
             "set_finalized": error is None, "completion_evidence": None}),))
    listing = ListingRound(ListingPhase.LISTED, (), ticket, actual, 2000, 3000,
        scan_complete=error is None, set_finalized=error is None)
    read = mocker.patch("camctl.capture.handlers._continue_result_scan", autospec=True, return_value=listing)
    register = mocker.patch("camctl.capture.handlers._register_listing", autospec=True)
    finish = mocker.patch("camctl.capture.handlers._finish_listing_result", autospec=True)
    release = mocker.patch("camctl.capture.segment_completion.release_segment_occupancy", autospec=True)
    return runtime, old, trigger, read, register, finish, release


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger_status", [1, 2], ids=["pending", "running_before_start"])
async def test_scope_success_releases_old_owner_without_changing_original_failure(mocker, trigger_status):
    runtime, old, trigger, read, register, finish, release = _world(mocker)
    trigger["status"] = trigger_status

    assert await advance_ended_scope(runtime, trigger) is True

    intent = runtime.operations.begin_attempt.call_args.args[0]
    assert intent.kind is OperationKind.RESOLVE_OUTPUT_SCOPE
    assert intent.action_id == old["id"] and intent.target.activity_id == 7
    assert intent.config == runtime.check_config
    assert read.call_args.args[1].scope_only is True
    assert register.call_args.args[1] == old["id"]
    assert finish.call_args.kwargs == {"end_run": RunOutcome.SUCCEEDED, "finalize_output_set": True}
    release.assert_awaited_once_with(runtime, old["id"])
    assert old["status"] == 4 and old["error_code"] == "capture_failed"


@pytest.mark.asyncio
async def test_scope_read_error_keeps_occupancy_and_independent_retry(mocker):
    error = ErrorValue("adb_directory_failed", "device_file_access", {"reason": "timeout"})
    runtime, _, trigger, _, _, finish, release = _world(mocker, error=error)

    assert await advance_ended_scope(runtime, trigger) is False

    assert finish.call_args.args[1].outcome.error is error
    assert finish.call_args.kwargs == {"retry_wait": True}
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_saved_scope_resumes_occupancy_release_without_another_read_or_attempt(mocker):
    runtime, old, trigger, read, _, _, release = _world(mocker, finalized_event_id=80)

    assert await advance_ended_scope(runtime, trigger) is True

    release.assert_awaited_once_with(runtime, old["id"])
    read.assert_not_awaited()
    runtime.operations.begin_attempt.assert_not_called()


@pytest.mark.asyncio
async def test_no_ended_scope_continues_preparation_without_new_attempt(mocker):
    runtime, _, trigger, read, _, _, _ = _world(mocker, old_scope=False)

    assert await advance_ended_scope(runtime, trigger) is True

    runtime.operations.begin_attempt.assert_not_called()
    read.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_session_setup_allows_scope_closure_in_the_extra_preparation_period(mocker):
    runtime, _, trigger, _, _, _, _ = _world(mocker, old_scope=False)
    trigger["driver_id"] = "dji-osmo360-ii"
    runtime.session_setup_budget = mocker.Mock(return_value=Decimal("30"))
    runtime.wall_us.return_value = trigger["scheduled_at"] - 40_000_000

    assert await advance_ended_scope(runtime, trigger) is True

    runtime.operations.begin_attempt.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"cancel_requested": 1}, {"status": 5}, {"scheduled_at": -10_000_000}])
async def test_ineligible_trigger_does_not_start_scope_read(mocker, change):
    runtime, _, trigger, read, _, _, _ = _world(mocker)
    trigger.update(change)

    assert await advance_ended_scope(runtime, trigger) is False

    runtime.operations.begin_attempt.assert_not_called()
    read.assert_not_awaited()

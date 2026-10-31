"""可靠结果待收尾与真正需要下一轮检查的状态分别恢复。"""

from dataclasses import replace
from decimal import Decimal
import json
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.capture.handlers import CaptureRuntime, ListingPhase, ResultFilesPort, _listing_round_input
from camctl.capture.result_pages import ResultPageRef, SavedResultPage
from camctl.contracts.enums import enum_for
from camctl.contracts.pages import Page
from camctl.contracts.values import ConsistencyError
from camctl.operations.attempts import AttemptConfig, BeginAttemptResult, BeginDisposition, RetryWaitGate
from camctl.operations.models import EffectState, ErrorValue
from camctl.operations.result_format import error_document, result_document
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.result_sources import ResultSource, ResultSourceKind
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from camctl.persistence.transaction import TransactionError
from support.capture_database import capture_database

from .test_result_page_saves import _request
from .test_result_pages import _TICKET, _page

pytestmark = pytest.mark.asyncio


def _world(mocker, *, pages=True, retry=False, running=False, success=False):
    runtime = create_autospec(CaptureRuntime, instance=True)
    connection = create_autospec(sqlite3.Connection, instance=True)
    owned = OwnedConnection(connection,
        DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing"))
    runtime.database = capture_database(owned)
    runtime.capture = create_autospec(CaptureRepository, instance=True)
    runtime.operations = create_autospec(OperationRepository, instance=True)
    runtime.results = create_autospec(ResultFilesPort, instance=True)
    runtime.pending_start_results = {}
    runtime.pending_result_scans = {}
    runtime.check_config = AttemptConfig(3, Decimal("10"), Decimal("3"))
    runtime.retry_gate = RetryWaitGate()
    clock = [0]
    runtime.monotonic_ns = mocker.Mock(side_effect=lambda: clock[0])
    runtime.wall_us = mocker.Mock(return_value=2000)
    async def remaining(*args, **kwargs):
        return await CaptureRuntime.retry_wait_remaining(runtime, *args, **kwargs)

    runtime.retry_wait_remaining.side_effect = remaining
    runtime._attempt_recovery_evidence.return_value = None
    runtime.action.return_value = {"id": 21, "type": 2, "execution_spec_json": {}}
    error = None if success else ErrorValue("adb_metadata_failed", "device_file_access",
        {"reason": "call_failed", "transport_error": "timeout"})
    actual = _request(error=error).outcome.outcome
    if not pages:
        actual = replace(actual, effect=EffectState.UNKNOWN, observations=())
    status = int(enum_for("operation_attempts.status")[actual.status.name])
    effect = int(enum_for("operation_attempts.effect_state")[actual.effect.name])
    runtime.last_attempt.return_value = (1 if running else status, effect, 1000)
    original = [9, 21, 71, 7, 1, status, effect,
        json.dumps(result_document(actual)), json.dumps(error_document(actual.error)), 1000]
    if pages:
        page = _page(actual)
        last = SavedResultPage(ResultPageRef(_TICKET, 1, 10, page.next_cursor), page, 1000)
        runtime.capture.read_last_result_page.return_value = last
        runtime.capture.read_result_pages.return_value = Page((last,), None)
    else:
        runtime.capture.read_last_result_page.return_value = None
    runtime.capture.read_result_source.return_value = ResultSource(ResultSourceKind.DIRECT, 1)

    def execute(sql, parameters=()):
        cursor = create_autospec(sqlite3.Cursor, instance=True)
        if sql.startswith("SELECT id FROM device_activities"):
            value = (71,)
        elif sql.startswith("SELECT r.id, a.attempt_no"):
            value = (9, 1, 71)
        elif sql.startswith("SELECT 1 FROM operation_attempts"):
            value = (1,) if pages else None
        elif sql.startswith("SELECT retry_wait_required,"):
            value = (int(retry), int(pages))
        elif sql.startswith("SELECT attempts_used, retry_wait_required"):
            value = (1, int(retry), 3)
        elif sql.startswith("SELECT status,attempts_used"):
            value = (2, 1)
        elif sql.startswith("SELECT r.id,r.action_id"):
            value = tuple(original)
        elif sql.startswith("SELECT attempt_no FROM operation_attempts"):
            value = None
        else:
            raise AssertionError(f"未声明的读取：{sql}")
        cursor.fetchone.return_value = value
        return cursor

    connection.execute.side_effect = execute
    if retry:
        runtime.operations.begin_attempt.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
            BeginAttemptResult(BeginDisposition.REJECTED, reason="run_ended"))
    else:
        runtime.operations.begin_attempt.return_value = DbOutcome(DbOutcomeKind.ROLLED_BACK,
            error=TransactionError("后续尝试要求先建立重试等待"))
    # 文件登记拥有独立的事实保存契约，此处只验证原结果的阶段分派。
    registration = mocker.patch("camctl.capture.handlers._register_listing", autospec=True)
    return runtime, clock, original, registration


@pytest.mark.parametrize("pages", [False, True], ids=["without-pages", "saved-page"])
@pytest.mark.parametrize("device_available", [False, True], ids=["no-device-port", "device-port"])
async def test_saved_failure_without_retry_returns_original_closed_result(mocker, pages, device_available):
    runtime, _, _, registration = _world(mocker, pages=pages)
    results = runtime.results
    if not device_available:
        runtime.results = None

    listing = await _listing_round_input(runtime, 21)

    assert listing.phase is ListingPhase.CLOSED and listing.already_saved
    assert listing.ticket == _TICKET and listing.occurred_at == 1000
    assert listing.outcome.error.code == "adb_metadata_failed"
    assert listing.outcome.error.details["transport_error"] == "timeout"
    runtime.operations.begin_attempt.assert_not_called()
    assert results.mock_calls == []
    assert registration.await_count == int(pages)


@pytest.mark.parametrize("success", [False, True], ids=["failed", "successful-incomplete"])
async def test_saved_incomplete_round_waits_then_uses_original_retry_budget(mocker, success):
    runtime, clock, _, _ = _world(mocker, retry=True, success=success)

    first = await _listing_round_input(runtime, 21)
    assert first.phase is ListingPhase.RETRY_WAIT
    runtime.operations.begin_attempt.assert_not_called()
    clock[0] = 2_999_999_999
    assert (await _listing_round_input(runtime, 21)).phase is ListingPhase.RETRY_WAIT
    clock[0] = 3_000_000_000
    assert (await _listing_round_input(runtime, 21)).phase is ListingPhase.CLOSED
    runtime.operations.begin_attempt.assert_called_once()
    intent = runtime.operations.begin_attempt.call_args.args[0]
    assert intent.action_id == 21 and intent.target.activity_id == 71
    assert intent.config == runtime.check_config


async def test_running_attempt_stays_with_original_owner(mocker):
    runtime, _, _, registration = _world(mocker, running=True)

    assert (await _listing_round_input(runtime, 21)).phase is ListingPhase.IN_FLIGHT
    runtime.operations.begin_attempt.assert_not_called()
    registration.assert_not_awaited()
    assert runtime.results.mock_calls == []


async def test_closed_recovery_rejects_original_result_for_another_action(mocker):
    runtime, _, original, _ = _world(mocker, pages=False)
    original[1] = 22

    with pytest.raises(ConsistencyError):
        await _listing_round_input(runtime, 21)

    runtime.operations.begin_attempt.assert_not_called()


async def test_original_file_registration_error_stops_closed_recovery(mocker):
    runtime, _, _, registration = _world(mocker)
    failure = OSError("原文件事实尚未保存")
    registration.side_effect = failure

    with pytest.raises(OSError) as raised:
        await _listing_round_input(runtime, 21)

    assert raised.value is failure
    runtime.operations.begin_attempt.assert_not_called()

"""旧检查轮次实际中断后，分别恢复原页和原尝试收场。"""

import asyncio
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from camctl.bootstrap.capture_assembly import DriverResultListing
from camctl.capture.handlers import ListingPhase, _listing_round, capture_handler
from camctl.capture.recovery import RecoveryBoundary, RecoveryBlockedReason
from camctl.contracts.values import ConsistencyError
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.devices.bindings import BindingResult, BindingStatus
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import DeviceCallResult, ResultDriver
from camctl.operations.attempts import AttemptConfig
from camctl.contracts.enums import enum_for
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.contracts.workflow_errors import registered_error

from .result_consumer_fixtures import consumer_world
from .test_capture_contract import _runtime
from .test_result_page_history import _actual, _entry, _BINDING, _CURSOR, _REGISTRY
from .test_result_device_completion import DeviceCompletionCatalog, _COMPLETION

pytestmark = pytest.mark.asyncio
_RECOVERY = EvidenceRegistry((
    _REGISTRY.contract("result_files_listed", 2),
    _REGISTRY.contract("results_returned", 1),
    _COMPLETION,
    EvidenceContract("adb_foreground_recovery", 1, "result", frozenset()),
))


def _resumed(owned, runtime, horizon):
    resumed = _runtime(owned, check_config=runtime.check_config)
    resumed.evidence = runtime.evidence
    resumed.recovery_boundary = RecoveryBoundary.HOST_LOCAL_SETTLED
    resumed.recovery_max_event_id = horizon
    resumed.recovery_evidence_for = lambda binding, operation: (
        _RECOVERY if binding == _BINDING and operation == "result" else None)
    resumed.wall_us = lambda: runtime.wall_us() + 5_000_000
    return resumed


async def _interrupted(tmp_path, *, maximum=1, terminal=False,
                       consumer="record", completion=False, first_entries=None):
    owned, runtime, action_id, handler = await consumer_world(tmp_path, consumer,
        catalog=DeviceCompletionCatalog() if completion else None)
    runtime.check_config = AttemptConfig(maximum, Decimal("2"), Decimal("0"))
    driver = create_autospec(ResultDriver, instance=True)
    entered = asyncio.Event()
    entries = first_entries if first_entries is not None else [
        {**_entry("a"), "kind": "photo" if consumer == "photo" else "video"}]
    async def read(request, batch):
        if "cursor" in request.params:
            entered.set()
            await asyncio.Future()
        actual = _actual(request.ticket, entries=entries,
            next_cursor=None if terminal else _CURSOR, finalized=terminal)
        if completion:
            observed, = actual.observations
            actual = replace(actual, observations=(DeviceObservation(observed.type, observed.version,
                {**observed.data, "completion_evidence": {"type": _COMPLETION.type,
                    "version": 1, "data": {"activity_id": request.ticket.target_id}}}),))
        return DeviceCallResult.from_outcome(actual)
    driver.list_results.side_effect = read
    runtime.evidence = _RECOVERY
    runtime.results = DriverResultListing(driver, _BINDING, runtime.evidence)
    try:
        if terminal:
            def crash(*args, **kwargs):
                raise ConsistencyError("测试在实际末页可靠保存后中断")
            runtime.hold_call_result = crash
            with pytest.raises(ConsistencyError, match="测试"):
                await capture_handler(handler)(action_id, runtime)
        else:
            task = asyncio.create_task(capture_handler(handler)(action_id, runtime))
            try:
                await asyncio.wait_for(entered.wait(), 2)
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        pending, = runtime.pending_result_scans.values()
        original = runtime.capture.read_result_page(pending.last_ref, owned)
        assert owned.connection.execute("SELECT source_action_id FROM device_files").fetchall() == [(None,)] * len(entries)
        path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
        horizon = owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0]
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        resumed = _resumed(owned, runtime, horizon)
        resumed.results = DriverResultListing(driver, _BINDING, runtime.evidence)
        return owned, resumed, action_id, handler, driver, original
    except BaseException:
        owned.connection.close()
        raise


async def test_interrupted_partial_scan_exhausts_without_losing_complete_file(tmp_path):
    owned, runtime, action_id, handler, driver, original = await _interrupted(tmp_path)
    try:
        await capture_handler(handler)(action_id, runtime)
        status, raw_error, result = owned.connection.execute(
            "SELECT status,error_json,result_json FROM operation_attempts WHERE run_id=?",
            (original.ref.ticket.run_id,)).fetchone()
        assert status == int(enum_for("operation_attempts.status").UNKNOWN)
        assert json.loads(raw_error) == {"code": "result_not_saved", "stage": "recovery", "details": {}}
        assert json.loads(result)["observations"] == []
        assert runtime.capture.read_result_page(original.ref, owned) == original
        assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE id=?",
            (original.ref.ticket.run_id,)).fetchone() == (1,)
        action_status, details = owned.connection.execute("SELECT status,error_details_json FROM actions WHERE id=?",
            (action_id,)).fetchone()
        assert action_status == 4
        assert json.loads(details) == {"activity_id": "1", "reason": "outputs_unknown"}
        assert owned.connection.execute("SELECT source_action_id,completion_state FROM device_files").fetchall() == [(action_id, 3)]
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs WHERE source_action_id=?", (action_id,)).fetchone() == (1,)
        assert driver.list_results.await_count == 2
    finally:
        owned.connection.close()


@pytest.mark.parametrize("consumer", ["photo", "timelapse", "cancel"])
async def test_partial_scan_recovery_preserves_complete_files_for_shared_consumers(tmp_path, consumer):
    owned, runtime, action_id, handler, driver, original = await _interrupted(tmp_path, consumer=consumer)
    try:
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (
            int(enum_for("actions.status").CANCELED) if consumer == "cancel" else 4,)
        assert owned.connection.execute("SELECT source_action_id,completion_state FROM device_files").fetchone() == (action_id, 3)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs WHERE source_action_id=?", (action_id,)).fetchone() == (1,)
        assert runtime.capture.read_result_page(original.ref, owned) == original
        assert driver.list_results.await_count == 2
    finally:
        owned.connection.close()


async def test_partial_scan_keeps_actual_device_end_separate_from_unknown_set(tmp_path):
    owned, runtime, action_id, handler, driver, original = await _interrupted(tmp_path,
        consumer="timelapse", completion=True)
    try:
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (4,)
        assert owned.connection.execute("SELECT activity_state,occupancy_state FROM device_activities").fetchone() == (3, 2)
        end = runtime.capture.read_result_completion(action_id, owned)
        assert end["observation"]["result_page_event_id"] == original.ref.event_id
        assert owned.connection.execute("SELECT t.status,e.occurred_at FROM operation_attempts t JOIN history_events e"
            " ON e.id=t.result_event_id WHERE t.run_id=?", (original.ref.ticket.run_id,)).fetchone() == (
                int(enum_for("operation_attempts.status").UNKNOWN), runtime.wall_us())
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (1,)
        assert driver.list_results.await_count == 2
    finally:
        owned.connection.close()


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("binding", [BindingStatus.DEVICE_MISSING, BindingStatus.DRIVER_MISMATCH])
@pytest.mark.parametrize("consumer", ["photo", "record", "timelapse"])
async def test_binding_failure_preserves_old_pages_before_business_failure(tmp_path, terminal, binding, consumer):
    owned, runtime, action_id, handler, driver, original = await _interrupted(tmp_path,
        consumer=consumer, completion=consumer == "timelapse", terminal=terminal)
    runtime.evidence = None
    runtime.results = None
    runtime.binding_check = lambda saved: BindingResult(binding, saved,
        "other" if binding is BindingStatus.DRIVER_MISMATCH else None)
    try:
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT status,error_code FROM actions WHERE id=?", (action_id,)).fetchone() == (
            4, registered_error("device_binding_unavailable")["action_error_id"])
        assert owned.connection.execute("SELECT source_action_id,completion_state FROM device_files").fetchone() == (action_id, 3)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (1,)
        if consumer == "timelapse":
            assert runtime.capture.read_result_completion(action_id, owned)["observation"]["result_page_event_id"] == original.ref.event_id
        assert runtime.capture.read_result_page(original.ref, owned) == original
        assert driver.list_results.await_count == (1 if terminal else 2)
    finally:
        owned.connection.close()


async def test_partial_scan_retains_complete_original_when_preview_pair_is_unread(tmp_path):
    preview = {**_entry("preview"), "kind": "other", "paired_identity": "/DCIM/later.mp4"}
    owned, runtime, action_id, handler, driver, original = await _interrupted(tmp_path,
        first_entries=[_entry("a"), preview])
    try:
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT status FROM actions").fetchone() == (4,)
        assert owned.connection.execute("SELECT source_action_id,completion_state FROM device_files ORDER BY id").fetchall() == [(action_id, 3), (None, 1)]
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (1,)
        assert runtime.capture.read_result_page(original.ref, owned) == original
        assert driver.list_results.await_count == 2
    finally:
        owned.connection.close()


@pytest.mark.parametrize("committed", [False, True])
async def test_unknown_recovery_save_keeps_original_key_and_input_across_connection(tmp_path, committed):
    class UnknownRecovery(OperationRepository):
        def __init__(self):
            self.requests = []
        def finish_attempt(self, request, key, owned):
            self.requests.append((request, key))
            if committed:
                receipt = super().finish_attempt(request, key, owned)
                assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=ConsistencyError("原恢复回执未知"))
    owned, runtime, action_id, handler, driver, original = await _interrupted(tmp_path)
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    repository = UnknownRecovery()
    runtime.operations = repository
    try:
        with pytest.raises(ConsistencyError):
            await capture_handler(handler)(action_id, runtime)
        assert len(repository.requests) == 2 and repository.requests[0] == repository.requests[1]
        pending, = runtime.pending_start_results.values()
        assert pending.result_disposition_ready
        assert pending.finish.outcome.outcome.observations == ()
        if committed:
            changed = replace(pending.finish, occurred_at=pending.finish.occurred_at + 1)
            assert OperationRepository().finish_attempt(changed, pending.key, owned).kind is DbOutcomeKind.ROLLED_BACK
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        resumed = _resumed(owned, runtime, runtime.recovery_max_event_id)
        resumed.pending_start_results = runtime.pending_start_results
        resumed.wall_us = lambda: pending.finish.occurred_at + 8_000_000
        await capture_handler(handler)(action_id, resumed)
        assert owned.connection.execute("SELECT status FROM actions").fetchone() == (4,)
        assert owned.connection.execute("SELECT occurred_at FROM history_events WHERE id=(SELECT result_event_id"
            " FROM operation_attempts WHERE run_id=?)", (original.ref.ticket.run_id,)).fetchone() == (pending.finish.occurred_at,)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (1,)
        assert runtime.capture.read_result_page(original.ref, owned) == original
        assert driver.list_results.await_count == 2
    finally:
        owned.connection.close()


@pytest.mark.parametrize("committed", [False, True])
async def test_unknown_partial_completion_save_keeps_same_end_reference_and_key(tmp_path, committed):
    class UnknownCompletion(CaptureRepository):
        def __init__(self):
            self.requests = []
        def finish_result_check(self, finish, confirm, key, owned, *, completion_page=None):
            self.requests.append((finish, confirm, key, completion_page))
            if committed:
                receipt = super().finish_result_check(finish, confirm, key, owned,
                    completion_page=completion_page)
                assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=ConsistencyError("原恢复与结束回执未知"))
    owned, runtime, action_id, handler, driver, original = await _interrupted(tmp_path,
        consumer="timelapse", completion=True)
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    repository = UnknownCompletion()
    runtime.capture = repository
    try:
        with pytest.raises(ConsistencyError):
            await capture_handler(handler)(action_id, runtime)
        assert len(repository.requests) == 2 and repository.requests[0] == repository.requests[1]
        pending, = runtime.pending_start_results.values()
        assert pending.completion_page == original.ref and pending.result_set is None
        assert pending.finish.outcome.outcome.observations == ()
        if committed:
            assert OperationRepository().finish_attempt(pending.finish, pending.key,
                owned).kind is DbOutcomeKind.ROLLED_BACK
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        resumed = _resumed(owned, runtime, runtime.recovery_max_event_id)
        resumed.pending_start_results = runtime.pending_start_results
        resumed.wall_us = lambda: pending.finish.occurred_at + 8_000_000
        await capture_handler(handler)(action_id, resumed)
        result_txn, = owned.connection.execute(
            "SELECT e.transaction_id FROM operation_attempts t JOIN history_events e"
            " ON e.id=t.result_event_id WHERE t.run_id=?", (original.ref.ticket.run_id,)).fetchone()
        ends = [(when, txn) for when, txn, body in owned.connection.execute(
            "SELECT occurred_at,transaction_id,body_json FROM history_events WHERE event_type=13")
            if json.loads(body)["evidence"].get("observation", {}).get("result_page_event_id") == original.ref.event_id]
        assert ends == [(original.occurred_at, result_txn)]
        assert owned.connection.execute("SELECT status FROM actions").fetchone() == (4,)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (1,)
        assert driver.list_results.await_count == 2
    finally:
        owned.connection.close()


async def test_interrupted_scan_starts_new_round_with_own_cursor_and_same_budget(tmp_path):
    owned, runtime, action_id, handler, driver, original = await _interrupted(tmp_path, maximum=2)
    async def complete(request, batch):
        assert "cursor" not in request.params
        assert request.ticket.run_id == original.ref.ticket.run_id and request.ticket.attempt_id == 2
        assert owned.connection.execute("SELECT source_action_id,completion_state FROM device_files").fetchone() == (action_id, 3)
        return DeviceCallResult.from_outcome(_actual(request.ticket,
            entries=[_entry("a"), _entry("b")], finalized=True))
    driver.list_results.side_effect = complete
    try:
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT attempt_no,status FROM operation_attempts WHERE run_id=? ORDER BY attempt_no",
            (original.ref.ticket.run_id,)).fetchall() == [(1, int(enum_for("operation_attempts.status").UNKNOWN)), (2, 2)]
        assert runtime.capture.read_result_page(original.ref, owned) == original
        assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE id=?",
            (original.ref.ticket.run_id,)).fetchone() == (2,)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (3,)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs WHERE source_action_id=?", (action_id,)).fetchone() == (2,)
        assert driver.list_results.await_count == 3
    finally:
        owned.connection.close()


@pytest.mark.parametrize("case,reason", [
    ("boundary", RecoveryBlockedReason.UNCONFIRMED_BOUNDARY),
    ("horizon", RecoveryBlockedReason.MISSING_HORIZON),
    ("lookup", RecoveryBlockedReason.MISSING_EVIDENCE_LOOKUP),
    ("evidence", RecoveryBlockedReason.EVIDENCE_UNAVAILABLE),
    ("late_intent", RecoveryBlockedReason.INTENT_OUTSIDE_HORIZON),
])
async def test_missing_recovery_qualification_preserves_running_scan(tmp_path, case, reason):
    owned, runtime, action_id, _, driver, original = await _interrupted(tmp_path)
    if case == "boundary":
        runtime.recovery_boundary = RecoveryBoundary.UNCONFIRMED
    elif case == "horizon":
        runtime.recovery_max_event_id = None
    elif case == "lookup":
        runtime.recovery_evidence_for = None
    elif case == "evidence":
        runtime.recovery_evidence_for = lambda binding, operation: None
    else:
        runtime.recovery_max_event_id = 0
    try:
        assert (await _listing_round(runtime, action_id)).phase is ListingPhase.IN_FLIGHT
        assert runtime.last_recovery_diagnostic.reason is reason
        assert owned.connection.execute("SELECT status,result_event_id FROM operation_attempts WHERE run_id=?",
            (original.ref.ticket.run_id,)).fetchone() == (1, None)
        assert runtime.capture.read_result_page(original.ref, owned) == original
        assert driver.list_results.await_count == 2
    finally:
        owned.connection.close()


async def test_saved_terminal_page_reuses_actual_return_and_time_without_query(tmp_path):
    owned, runtime, action_id, handler, driver, original = await _interrupted(tmp_path, terminal=True)
    try:
        await capture_handler(handler)(action_id, runtime)
        status, result, occurred = owned.connection.execute(
            "SELECT t.status,t.result_json,e.occurred_at FROM operation_attempts t"
            " JOIN history_events e ON e.id=t.result_event_id WHERE t.run_id=?",
            (original.ref.ticket.run_id,)).fetchone()
        assert status == 2 and occurred == original.occurred_at
        assert json.loads(result)["observations"][0]["data"]["set_finalized"] is True
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (3,)
        assert runtime.capture.read_result_page(original.ref, owned) == original
        assert driver.list_results.await_count == 1
    finally:
        owned.connection.close()

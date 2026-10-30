"""旧检查轮次实际中断后，分别恢复原页和原尝试收场。"""

import asyncio
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from support.repository_ports import repository_port

from camctl.bootstrap.capture_assembly import DriverResultListing
from camctl.capture.handlers import capture_handler
from ..support.result_cases import result_case, result_case_for, current_result_ticket
from camctl.capture.recovery import RecoveryBoundary, RecoveryBlockedReason
from camctl.contracts.values import ConsistencyError
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.devices.bindings import BindingStatus
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import DeviceCallResult, ResultDriver
from camctl.operations.attempts import AttemptConfig
from camctl.contracts.enums import enum_for
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.contracts.workflow_errors import registered_error

from .test_result_page_history import _actual, _entry, _BINDING, _CURSOR, _REGISTRY
from .test_result_device_completion import DeviceCompletionCatalog, _COMPLETION

pytestmark = pytest.mark.asyncio
_RECOVERY = EvidenceRegistry((
    _REGISTRY.contract("result_files_listed", 2),
    _REGISTRY.contract("results_returned", 1),
    _COMPLETION,
    EvidenceContract("adb_foreground_recovery", 1, "result", frozenset()),
))


async def _resumed(owned, runtime, horizon, *, warm=False):
    case = result_case_for()
    case.clock.wall += 5_000_000
    resumed = await case.reopen(owned, cold=not warm, assembly_options=None if warm else {
        "recovery_boundary": RecoveryBoundary.HOST_LOCAL_SETTLED,
        "recovery_max_event_id": lambda: horizon, "result_recovery": True})
    return resumed


async def _interrupted(tmp_path, *, maximum=1, terminal=False, finalized=None,
                       consumer="record", completion=False, first_entries=None):
    owned, runtime, action_id, handler = await result_case_for().world(tmp_path, consumer,
        catalog=DeviceCompletionCatalog() if completion else None,
        prepare_start_only=completion,
        assembly_options={"check_config": AttemptConfig(maximum, Decimal("2"), Decimal("0"))},
        evidence_contracts=(_COMPLETION, _RECOVERY.contract("adb_foreground_recovery", 1)))
    driver = create_autospec(ResultDriver, instance=True)
    entered = asyncio.Event()
    entries = first_entries if first_entries is not None else [
        {**_entry("a"), "kind": "photo" if consumer == "photo" else "video"}]
    async def read(request, batch):
        if "cursor" in request.params:
            entered.set()
            await asyncio.Future()
        actual = _actual(request.ticket, entries=entries,
            next_cursor=None if terminal else _CURSOR,
            finalized=terminal if finalized is None else finalized)
        if completion:
            observed, = actual.observations
            actual = replace(actual, observations=(DeviceObservation(observed.type, observed.version,
                {**observed.data, "completion_evidence": {"type": _COMPLETION.type,
                    "version": 1, "data": {"activity_id": request.ticket.target_id}}}),))
        return DeviceCallResult.from_outcome(actual)
    driver.list_results.side_effect = read
    result_case_for().results.delegate = DriverResultListing(driver, _BINDING, _RECOVERY)
    try:
        if terminal:
            class InterruptedPage(CaptureRepository):
                hits = 0
                def save_result_page(self, request, key, owned):
                    saved = super().save_result_page(request, key, owned)
                    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
                    self.hits += 1
                    return DbOutcome(DbOutcomeKind.UNKNOWN,
                        error=ConsistencyError("测试在实际末页可靠保存后中断"))
            observer = InterruptedPage()
            with repository_port(CaptureRepository, observer, ("save_result_page",),
                    matches=lambda _name, args, _kwargs: args[0].ticket.responsibility_key == f"results/{action_id}"):
                with pytest.raises(ConsistencyError, match="测试"):
                    await capture_handler(handler)(action_id, runtime)
            assert observer.hits > 0
        else:
            task = asyncio.create_task(capture_handler(handler)(action_id, runtime))
            try:
                await asyncio.wait_for(entered.wait(), 2)
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        ticket = current_result_ticket(owned, action_id)
        original = CaptureRepository().read_last_result_page(ticket, owned)
        assert original is not None
        assert owned.connection.execute("SELECT source_action_id FROM device_files").fetchall() == [(None,)] * len(entries)
        path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
        horizon = owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0]
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        resumed = (await _resumed(owned, runtime, horizon))
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
    runtime = await result_case_for().reopen(owned, cold=True, assembly_options={
        "devices": {} if binding is BindingStatus.DEVICE_MISSING else {"cam-1": {"driver": "other"}}})
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
    try:
        with repository_port(OperationRepository, repository, ("finish_attempt",),
                matches=lambda _name, args, _kwargs: args[0].ticket == original.ref.ticket):
            with pytest.raises(ConsistencyError):
                await capture_handler(handler)(action_id, runtime)
        assert len(repository.requests) == 2 and repository.requests[0] == repository.requests[1]
        finish, key = repository.requests[0]
        assert finish.outcome.outcome.observations == ()
        if committed:
            changed = replace(finish, occurred_at=finish.occurred_at + 1)
            assert OperationRepository().finish_attempt(changed, key, owned).kind is DbOutcomeKind.ROLLED_BACK
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        resumed = await _resumed(owned, runtime, None, warm=True)
        result_case_for().clock.wall = finish.occurred_at + 8_000_000
        await capture_handler(handler)(action_id, resumed)
        assert owned.connection.execute("SELECT status FROM actions").fetchone() == (4,)
        assert owned.connection.execute("SELECT occurred_at FROM history_events WHERE id=(SELECT result_event_id"
            " FROM operation_attempts WHERE run_id=?)", (original.ref.ticket.run_id,)).fetchone() == (finish.occurred_at,)
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
        def finish_result_check(self, finish, confirm, key, owned, *, completion_page=None, **options):
            self.requests.append((finish, confirm, key, completion_page))
            if committed:
                receipt = super().finish_result_check(finish, confirm, key, owned,
                    completion_page=completion_page, **options)
                assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=ConsistencyError("原恢复与结束回执未知"))
    owned, runtime, action_id, handler, driver, original = await _interrupted(tmp_path,
        consumer="timelapse", completion=True)
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    repository = UnknownCompletion()
    try:
        with repository_port(CaptureRepository, repository, ("finish_result_check",),
                matches=lambda _name, args, _kwargs: args[0].ticket == original.ref.ticket):
            with pytest.raises(ConsistencyError):
                await capture_handler(handler)(action_id, runtime)
        assert len(repository.requests) == 2 and repository.requests[0] == repository.requests[1]
        finish, result_set, key, completion_page = repository.requests[0]
        assert completion_page == original.ref and result_set is None
        assert finish.outcome.outcome.observations == ()
        if committed:
            assert OperationRepository().finish_attempt(finish, key,
                owned).kind is DbOutcomeKind.ROLLED_BACK
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        resumed = await _resumed(owned, runtime, None, warm=True)
        result_case_for().clock.wall = finish.occurred_at + 8_000_000
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
    ("evidence", RecoveryBlockedReason.EVIDENCE_UNAVAILABLE),
    ("late_intent", RecoveryBlockedReason.INTENT_OUTSIDE_HORIZON),
])
async def test_missing_recovery_qualification_preserves_running_scan(tmp_path, case, reason):
    owned, runtime, action_id, handler, driver, original = await _interrupted(tmp_path)
    settings = {"recovery_boundary": RecoveryBoundary.HOST_LOCAL_SETTLED,
                "result_recovery": True}
    if case == "boundary":
        settings["recovery_boundary"] = RecoveryBoundary.UNCONFIRMED
    elif case == "horizon":
        settings["recovery_max_event_id"] = None
    elif case == "evidence":
        settings["result_recovery"] = False
    else:
        settings["recovery_max_event_id"] = lambda: 0
    runtime = await result_case_for().reopen(owned, cold=True, assembly_options=settings)
    try:
        await capture_handler(handler)(action_id, runtime)
        assert result_case_for().diagnostics[-1].reason is reason
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


@pytest.mark.parametrize("first,consumer", [
    ("partial", "photo"), ("partial", "record"), ("partial", "timelapse"), ("partial", "cancel"),
    ("returned", "photo"), ("returned", "record"), ("returned", "timelapse"),
])
async def test_new_round_interrupted_before_first_return_keeps_previous_v2_files(tmp_path, first, consumer):
    owned, runtime, action_id, handler, driver, original = await _interrupted(tmp_path,
        consumer=consumer, maximum=2, terminal=first == "returned", finalized=False)
    entered = asyncio.Event()
    async def unread(request, batch):
        assert request.ticket.run_id == original.ref.ticket.run_id
        assert request.ticket.attempt_id == 2 and "cursor" not in request.params
        entered.set()
        await asyncio.Future()
    driver.list_results.side_effect = unread
    async def advance():
        # 完整实际返回但集合未定的旧轮先交还消费者；下一次推进开新轮。
        for _ in range(2):
            await capture_handler(handler)(action_id, runtime)
            assert (await runtime.action(action_id))["status"] == 2
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    try:
        task = asyncio.create_task(advance())
        try:
            await asyncio.wait_for(entered.wait(), 2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert owned.connection.execute("SELECT attempt_no,result_first_page_event_id FROM operation_attempts"
            " WHERE run_id=? ORDER BY attempt_no", (original.ref.ticket.run_id,)).fetchall() == [
                (1, original.ref.event_id), (2, None)]
        horizon = owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0]
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        resumed = (await _resumed(owned, runtime, horizon))
        await capture_handler(handler)(action_id, resumed)
        assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE id=?",
            (original.ref.ticket.run_id,)).fetchone() == (2,)
        assert owned.connection.execute("SELECT attempt_no,status FROM operation_attempts WHERE run_id=? ORDER BY attempt_no",
            (original.ref.ticket.run_id,)).fetchall() == [
                (1, int(enum_for("operation_attempts.status").UNKNOWN) if first == "partial" else 2),
                (2, int(enum_for("operation_attempts.status").UNKNOWN))]
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (
            int(enum_for("actions.status").CANCELED) if consumer == "cancel" else 4,)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs WHERE source_action_id=?", (action_id,)).fetchone() == (1,)
        assert owned.connection.execute("SELECT source_action_id,completion_state FROM device_files").fetchone() == (action_id, 3)
        assert resumed.capture.read_result_page(original.ref, owned) == original
        assert driver.list_results.await_count == (3 if first == "partial" else 2)
    finally:
        owned.connection.close()


async def test_committed_attempt_without_file_facts_restores_pages_before_new_query(tmp_path):
    class InterruptedFileSave(CaptureRepository):
        hits = 0
        def save_file_ownership(self, request, key, owned):
            self.hits += 1
            raise ConsistencyError("原文件事实保存前中断")
    owned, runtime, action_id, handler, driver, original = await _interrupted(tmp_path, maximum=2)
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    try:
        observer = InterruptedFileSave()
        with repository_port(CaptureRepository, observer, ("save_file_ownership",),
                matches=lambda _name, args, _kwargs: args[0].source_action_id == action_id):
            with pytest.raises(ConsistencyError):
                await asyncio.wait_for(capture_handler(handler)(action_id, runtime), 5)
        assert observer.hits > 0
        assert owned.connection.execute("SELECT status FROM operation_attempts WHERE run_id=?",
            (original.ref.ticket.run_id,)).fetchone() == (int(enum_for("operation_attempts.status").UNKNOWN),)
        assert owned.connection.execute("SELECT source_action_id FROM device_files").fetchone() == (None,)
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        resumed = (await _resumed(owned, runtime, result_case_for().options["recovery_max_event_id"]()))
        async def complete(request, batch):
            assert request.ticket.attempt_id == 2
            assert owned.connection.execute("SELECT source_action_id,completion_state FROM device_files").fetchone() == (action_id, 3)
            return DeviceCallResult.from_outcome(_actual(request.ticket, entries=[_entry("a")], finalized=True))
        driver.list_results.side_effect = complete
        result_case_for().results.delegate = DriverResultListing(driver, _BINDING, _RECOVERY)
        await capture_handler(handler)(action_id, resumed)
        assert owned.connection.execute("SELECT status FROM actions").fetchone() == (3,)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (1,)
        assert resumed.capture.read_result_page(original.ref, owned) == original
        assert driver.list_results.await_count == 3
    finally:
        owned.connection.close()

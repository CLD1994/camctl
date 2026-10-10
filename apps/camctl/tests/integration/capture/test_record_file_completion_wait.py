"""录像停止与文件完成等待分别保存；恢复复用原 RESULT 页事实。"""
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from camctl.bootstrap.capture_assembly import DriverResultListing
from camctl.capture.handlers import (
    SessionRecordingState, _finish_listing_result, _listing_round,
    _stop_call, _stop_confirmed_at, capture_handler,
)
from camctl.capture.result_inputs import RESULT_PAGE_CONTRACT
from camctl.contracts.values import ConsistencyError
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult, ResultDriver, StopDriver
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import ErrorValue, EvidenceValue, Settlement, SettlementBasis
from camctl.persistence.repositories.history import HistoryRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .result_consumer_fixtures import RESULT_EVIDENCE, ResultCatalog, consumer_world, returned
from .test_capture_contract import _NOW, _runtime
from .test_result_page_history import _actual, _entry, _BINDING, _CURSOR

pytestmark = pytest.mark.asyncio

_REGISTRY = EvidenceRegistry((RESULT_PAGE_CONTRACT,
    EvidenceContract("results_returned", 1, "result", frozenset({
        "file_completion", "file_completion_source_page_event_id"})),
    EvidenceContract("adb_foreground_recovery", 1, "result", frozenset()),
    *(RESULT_EVIDENCE.contract(name, 1) for name in (
        "operation_returned", "start_confirmed", "stop_returned", "stop_confirmed")),
))


class WaitCatalog(ResultCatalog):
    def parameter_definition(self, device_id, action_type, parameter_type):
        definition = super().parameter_definition(device_id, action_type, parameter_type)
        if action_type == "camera_record" and definition is not None:
            factory = definition.task_factory
            return replace(definition, task_factory=lambda params: replace(
                factory(params), file_completion_wait_s=Decimal("5")))
        return definition


async def _wait_world(tmp_path, *, start=True):
    owned, runtime, action_id, handler = await consumer_world(tmp_path, "record",
        catalog=WaitCatalog(), start=start)
    runtime.check_config = AttemptConfig(3, Decimal("10"), Decimal("2"))
    return owned, runtime, action_id, handler


def _stop_context(owned, action_id):
    activity_id, = owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
    event_id, occurred_at, status, effect, error, result = owned.connection.execute(
        "SELECT e.id,e.occurred_at,a.status,a.effect_state,a.error_json,a.result_json"
        " FROM operation_runs r JOIN operation_attempts a ON a.run_id=r.id"
        " JOIN history_events e ON e.id=a.result_event_id"
        " WHERE r.responsibility_key=? ORDER BY a.id DESC LIMIT 1",
        (f"stop/{action_id}",)).fetchone()
    return {"activity_id": str(activity_id), "stop_result_event_id": event_id,
        "stop_returned_at_us": occurred_at, "stop_response": {
            "status": status, "effect_state": effect,
            "error": None if error is None else json.loads(error), "result": json.loads(result)},
        "file_completion_wait_ms": 5000, "prior_completion": None}


def _completion(context, **changes):
    return {"method": "stop_return_and_wait", "version": 1,
        "activity_id": context["activity_id"],
        "stop_result_event_id": context["stop_result_event_id"],
        "required_wait_ms": 5000, "observed_wait_ns": 5_000_000_000,
        "completed": True, **changes}


def _result_port(runtime, read):
    driver = create_autospec(ResultDriver, instance=True)
    driver.list_results.side_effect = read
    runtime.evidence = _REGISTRY
    runtime.results = DriverResultListing(driver, _BINDING, _REGISTRY)
    return driver


def _completed_result(ticket, context, *, entries=(), cursor=None,
                      next_cursor=None, finalized=False, error=None, completion=None,
                      source_page_event_id=None):
    actual = _actual(ticket, entries=entries, cursor=cursor,
        next_cursor=next_cursor, finalized=finalized, error=error)
    return DeviceCallResult.from_outcome(replace(actual, settlement=Settlement(
        SettlementBasis.OBSERVED, EvidenceValue("results_returned", 1,
            {"file_completion": _completion(context) if completion is None else completion,
             "file_completion_source_page_event_id": source_page_event_id}))))


async def test_confirmed_stop_keeps_files_unconfirmed_until_reliable_wait_page(tmp_path):
    owned, runtime, action_id, _ = await _wait_world(tmp_path)
    try:
        state = SessionRecordingState(runtime).recording_state(action_id)
        assert state.stop_confirmed and not state.file_complete_guaranteed
        assert owned.connection.execute(
            "SELECT activity_state,occupancy_state,output_set_finalized_event_id"
            " FROM device_activities").fetchone() == (3, 2, None)
    finally:
        owned.connection.close()


async def test_legacy_record_stop_still_guarantees_file_completion(tmp_path):
    owned, runtime, action_id, _ = await consumer_world(tmp_path, "record")
    try:
        state = SessionRecordingState(runtime).recording_state(action_id)
        assert state.stop_confirmed and state.file_complete_guaranteed
    finally:
        owned.connection.close()


async def test_verify_file_completion_decides_control_before_result_wait_without_extra_record_time(tmp_path):
    owned, runtime, action_id, handler = await _wait_world(tmp_path, start=False)
    try:
        await capture_handler(handler)(action_id, runtime)
        activity_id, = owned.connection.execute("SELECT id FROM device_activities").fetchone()
        runtime.wall_us = lambda: _NOW + 60_000_000
        runtime.monotonic_ns = lambda: 65_000_000_000
        stop = create_autospec(StopDriver, instance=True)
        stop.stop.return_value = DeviceCallResult.from_outcome(
            returned("stop", "stop_confirmed", activity_id))
        runtime.stopper = stop
        await _stop_call(runtime, runtime.action(action_id), "stop_recording")
        context = _stop_context(owned, action_id)
        assert owned.connection.execute("SELECT check_decision FROM recording_processing").fetchone() == (1,)

        async def read(request, batch):
            assert request.params["completion_context"] == context
            assert owned.connection.execute("SELECT check_decision FROM recording_processing").fetchone() == (2,)
            runtime.wall_us = lambda: _NOW + 65_000_000
            runtime.monotonic_ns = lambda: 70_000_000_000
            return _completed_result(request.ticket, context, entries=[_entry("a")], finalized=True)

        driver = _result_port(runtime, read)
        await capture_handler(handler)(action_id, runtime)

        driver.list_results.assert_awaited_once()
        stop.stop.assert_awaited_once()
        assert runtime.action(action_id)["status"] == 3
        assert _stop_confirmed_at(runtime, action_id) == _NOW + 60_000_000
        started_at, = owned.connection.execute("SELECT started_at FROM device_activities").fetchone()
        assert _stop_confirmed_at(runtime, action_id) - started_at == 60_000_000
        assert owned.connection.execute("SELECT check_decision,repair_state FROM recording_processing").fetchone() == (2, 1)
        assert SessionRecordingState(runtime).recording_state(action_id).file_complete_guaranteed
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (1,)
    finally:
        owned.connection.close()


async def test_result_wait_error_keeps_known_stop_and_does_not_repeat_stop(tmp_path):
    owned, runtime, action_id, handler = await _wait_world(tmp_path)
    runtime.check_config = AttemptConfig(3, Decimal("2"), Decimal("0"))
    context = _stop_context(owned, action_id)
    error = ErrorValue("completion_wait_timeout", "result", {"observed_wait_ns": 2_000_000_000})

    async def read(request, batch):
        return _completed_result(request.ticket, context, error=error,
            completion=_completion(context, completed=False, observed_wait_ns=2_000_000_000))

    driver = _result_port(runtime, read)
    try:
        await capture_handler(handler)(action_id, runtime)
        await capture_handler(handler)(action_id, runtime)
        runtime.stopper.stop.assert_awaited_once()
        assert driver.list_results.await_count == 2
        state = SessionRecordingState(runtime).recording_state(action_id)
        assert state.stop_confirmed and not state.file_complete_guaranteed
        assert runtime.action(action_id)["status"] == 2
    finally:
        owned.connection.close()


@pytest.mark.parametrize("restart", [False, True], ids=["same-session", "reopened"])
async def test_later_result_round_reuses_completed_wait_from_metadata_failure(tmp_path, restart):
    owned, runtime, action_id, _ = await _wait_world(tmp_path)
    runtime.check_config = AttemptConfig(3, Decimal("10"), Decimal("0"))
    context = _stop_context(owned, action_id)
    error = ErrorValue("metadata_failed", "result", {"path": "/DCIM/a.mp4"})

    async def failed_read(request, batch):
        assert request.params["completion_context"] == context
        return _completed_result(request.ticket, context, error=error)

    first_driver = _result_port(runtime, failed_read)
    try:
        original = await _listing_round(runtime, action_id)
        _finish_listing_result(runtime, original, retry_wait=True)
        saved = runtime.capture.read_last_result_page(original.ticket, owned)
        prior = {"result_page_event_id": saved.ref.event_id,
            "evidence": _completion(context)}
        assert SessionRecordingState(runtime).recording_state(action_id).file_complete_guaranteed
        assert owned.connection.execute("SELECT output_set_finalized_event_id FROM device_activities").fetchone() == (None,)

        path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
        history = HistoryRepository(path)
        restored = history.restore_entity("action", action_id, history.current_boundary())
        attempt_id, result_json = owned.connection.execute(
            "SELECT id,result_json FROM operation_attempts WHERE run_id=?",
            (original.ticket.run_id,)).fetchone()
        assert restored[("operation_attempts", attempt_id)]["result_json"] == json.loads(result_json)

        if restart:
            owned.connection.close()
            owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
            runtime = _runtime(owned, check_config=runtime.check_config)

        async def resumed_read(request, batch):
            assert request.params["completion_context"] == {**context, "prior_completion": prior}
            return _completed_result(request.ticket, context, entries=[_entry("a")], finalized=True)

        second_driver = _result_port(runtime, resumed_read)
        listing = await _listing_round(runtime, action_id)
        assert listing.set_finalized
        assert runtime.capture.read_result_page(saved.ref, owned) == saved
        first_driver.list_results.assert_awaited_once()
        second_driver.list_results.assert_awaited_once()
        assert _stop_confirmed_at(runtime, action_id) == context["stop_returned_at_us"]
    finally:
        owned.connection.close()


@pytest.mark.parametrize("started", [False, True], ids=["start-unconfirmed", "stop-unconfirmed"])
async def test_result_without_reliable_start_and_stop_has_no_completion_context(tmp_path, started):
    owned, runtime, action_id, handler = await _wait_world(tmp_path, start=False)
    try:
        if started:
            await capture_handler(handler)(action_id, runtime)

        async def read(request, batch):
            assert request.params.get("completion_context") is None
            return DeviceCallResult.from_outcome(_actual(request.ticket))

        driver = _result_port(runtime, read)
        await _listing_round(runtime, action_id)
        driver.list_results.assert_awaited_once()
    finally:
        owned.connection.close()


@pytest.mark.parametrize("change", [
    {"activity_id": "2"}, {"stop_result_event_id": 9999},
    {"required_wait_ms": 4000}, {"completed": False},
])
async def test_saved_mismatched_wait_page_cannot_guarantee_current_record_files(tmp_path, change):
    owned, runtime, action_id, _ = await _wait_world(tmp_path)
    context = _stop_context(owned, action_id)

    async def read(request, batch):
        return _completed_result(request.ticket, context, completion=_completion(context, **change))

    _result_port(runtime, read)
    try:
        await _listing_round(runtime, action_id)
        state = SessionRecordingState(runtime).recording_state(action_id)
        assert state.stop_confirmed and not state.file_complete_guaranteed
    finally:
        owned.connection.close()


@pytest.mark.parametrize("restart", [False, True], ids=["same-session", "reopened"])
async def test_repeated_metadata_failures_keep_the_actual_wait_source_page(tmp_path, restart):
    owned, runtime, action_id, _ = await _wait_world(tmp_path)
    runtime.check_config = AttemptConfig(3, Decimal("10"), Decimal("0"))
    context = _stop_context(owned, action_id)
    error = ErrorValue("metadata_failed", "result", {"path": "/DCIM/a.mp4"})

    async def actual_wait(request, batch):
        return _completed_result(request.ticket, context, error=error)

    _result_port(runtime, actual_wait)
    try:
        first = await _listing_round(runtime, action_id)
        _finish_listing_result(runtime, first, retry_wait=True)
        original = runtime.capture.read_last_result_page(first.ticket, owned)
        prior = {"result_page_event_id": original.ref.event_id, "evidence": _completion(context)}

        async def copied_wait(request, batch):
            assert request.params["completion_context"]["prior_completion"] == prior
            return _completed_result(request.ticket, context, error=error,
                source_page_event_id=original.ref.event_id)

        _result_port(runtime, copied_wait)
        second = await _listing_round(runtime, action_id)
        _finish_listing_result(runtime, second, retry_wait=True)
        copied = runtime.capture.read_last_result_page(second.ticket, owned)
        assert copied.ref.event_id > original.ref.event_id
        path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
        if restart:
            owned.connection.close()
            owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
            runtime = _runtime(owned, check_config=runtime.check_config)

        async def final_read(request, batch):
            assert request.params["completion_context"]["prior_completion"] == prior
            return _completed_result(request.ticket, context, entries=[_entry("a")], finalized=True,
                source_page_event_id=original.ref.event_id)

        driver = _result_port(runtime, final_read)
        final = await _listing_round(runtime, action_id)
        assert final.set_finalized
        driver.list_results.assert_awaited_once()
        assert runtime.capture.read_result_page(original.ref, owned) == original
        assert _stop_confirmed_at(runtime, action_id) == context["stop_returned_at_us"]
    finally:
        owned.connection.close()


@pytest.mark.parametrize("case", ["future", "wrong_event", "different_fact"])
async def test_saved_copy_wait_rejects_an_invalid_actual_source(tmp_path, case):
    owned, runtime, action_id, _ = await _wait_world(tmp_path)
    runtime.check_config = AttemptConfig(3, Decimal("10"), Decimal("0"))
    context = _stop_context(owned, action_id)
    error = ErrorValue("metadata_failed", "result", {"path": "/DCIM/a.mp4"})

    async def actual_wait(request, batch):
        return _completed_result(request.ticket, context, error=error)

    _result_port(runtime, actual_wait)
    try:
        first = await _listing_round(runtime, action_id)
        _finish_listing_result(runtime, first, retry_wait=True)
        original = runtime.capture.read_last_result_page(first.ticket, owned)
        reference = (original.ref.event_id + 1000 if case == "future" else
            context["stop_result_event_id"] if case == "wrong_event" else original.ref.event_id)
        completion = _completion(context,
            observed_wait_ns=5_000_000_001 if case == "different_fact" else 5_000_000_000)

        async def copied_wait(request, batch):
            return _completed_result(request.ticket, context, error=error,
                completion=completion, source_page_event_id=reference)

        _result_port(runtime, copied_wait)
        with pytest.raises(ConsistencyError):
            await _listing_round(runtime, action_id)
        runtime.stopper.stop.assert_awaited_once()
        assert _stop_confirmed_at(runtime, action_id) == context["stop_returned_at_us"]
    finally:
        owned.connection.close()

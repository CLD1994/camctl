"""录像停止与文件完成等待分别保存；恢复复用原 RESULT 页事实。"""
import asyncio
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

from .result_consumer_fixtures import RESULT_EVIDENCE, ResultCatalog, returned
from .test_capture_contract import _NOW
from .test_result_page_history import _actual, _entry, _BINDING, _CURSOR
from ..support.result_cases import ResultCase, saved_result_pages
from .result_consumer_fixtures import result_driver

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


async def _stop_before_result_page(tmp_path, capture_resources, catalog, *, wait_required):
    case = ResultCase(capture_resources)
    owned, runtime, action_id, handler = await case.world(tmp_path, "record", start=False,
        catalog=catalog, evidence_contracts=(_REGISTRY.contract("results_returned", 1),),
        assembly_options={"check_config": AttemptConfig(3, Decimal("10"), Decimal("2"))})
    entered = asyncio.Event()
    release = asyncio.Event()
    requests = []

    async def read(request, batch):
        requests.append(request)
        entered.set()
        await release.wait()
        return DeviceCallResult.from_outcome(_actual(request.ticket,
            entries=[] if wait_required else [_entry("a")], finalized=not wait_required))

    result_driver(case, side_effect=read)
    task = None
    try:
        await capture_handler(handler)(action_id, runtime)
        case.clock.wall = _NOW + 60_000_000
        case.clock.monotonic = 65_000_000_000
        task = asyncio.create_task(capture_handler(handler)(action_id, runtime))
        await asyncio.wait_for(entered.wait(), 2)
        case.stopper.stop.assert_awaited_once()
        assert owned.connection.execute(
            "SELECT activity_state,occupancy_state,output_set_finalized_event_id"
            " FROM device_activities").fetchone() == (3, 2, None)
        assert owned.connection.execute("SELECT COUNT(*) FROM result_page_files").fetchone() == (0,)
        if wait_required:
            assert requests[0].params["completion_context"] == _stop_context(owned, action_id)
        else:
            assert requests[0].params.get("completion_context") is None
            spec, = owned.connection.execute("SELECT execution_spec_json FROM actions WHERE id=?", (action_id,)).fetchone()
            assert "file_completion_wait_ms" not in json.loads(spec)
        release.set()
        await task
        if not wait_required:
            assert (await runtime.action(action_id))["status"] == 3
            assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (1,)
    finally:
        release.set()
        if task is not None and not task.done():
            await task
        await case.close()
        owned.connection.close()


async def test_confirmed_stop_keeps_files_unconfirmed_until_reliable_wait_page(tmp_path, capture_resources):
    await _stop_before_result_page(tmp_path, capture_resources, WaitCatalog(), wait_required=True)


async def test_legacy_record_stop_still_guarantees_file_completion(tmp_path, capture_resources):
    await _stop_before_result_page(tmp_path, capture_resources, ResultCatalog(), wait_required=False)


async def test_verify_file_completion_decides_control_before_result_wait_without_extra_record_time(tmp_path, capture_resources):
    case = ResultCase(capture_resources)
    owned, runtime, action_id, handler = await case.world(tmp_path, "record", start=False,
        catalog=WaitCatalog(), evidence_contracts=(_REGISTRY.contract("results_returned", 1),),
        assembly_options={"check_config": AttemptConfig(3, Decimal("10"), Decimal("2"))})
    try:
        async def read(request, batch):
            context = _stop_context(owned, action_id)
            assert request.params["completion_context"] == context
            assert owned.connection.execute("SELECT check_decision FROM recording_processing").fetchone() == (2,)
            case.clock.wall = _NOW + 65_000_000
            case.clock.monotonic = 70_000_000_000
            return _completed_result(request.ticket, context, entries=[_entry("a")], finalized=True)

        driver = result_driver(case, side_effect=read)
        await capture_handler(handler)(action_id, runtime)
        case.clock.wall = _NOW + 60_000_000
        case.clock.monotonic = 65_000_000_000
        await capture_handler(handler)(action_id, runtime)

        driver.list_results.assert_awaited_once()
        case.stopper.stop.assert_awaited_once()
        assert (await runtime.action(action_id))["status"] == 3
        context = _stop_context(owned, action_id)
        assert context["stop_returned_at_us"] == _NOW + 60_000_000
        started_at, = owned.connection.execute("SELECT started_at FROM device_activities").fetchone()
        assert context["stop_returned_at_us"] - started_at == 60_000_000
        assert owned.connection.execute("SELECT check_decision,repair_state FROM recording_processing").fetchone() == (2, 1)
        result_json, = owned.connection.execute(
            "SELECT a.result_json FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
            " WHERE r.kind=7").fetchone()
        completion = json.loads(result_json)["settlement"]["evidence"]["data"]["file_completion"]
        assert completion == _completion(context)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (1,)
    finally:
        await case.close()
        owned.connection.close()


async def test_result_wait_error_keeps_known_stop_and_does_not_repeat_stop(tmp_path, capture_resources):
    case = ResultCase(capture_resources)
    owned, runtime, action_id, handler = await case.world(tmp_path, "record", start=False,
        catalog=WaitCatalog(), evidence_contracts=(_REGISTRY.contract("results_returned", 1),),
        assembly_options={"check_config": AttemptConfig(3, Decimal("2"), Decimal("0"))})
    error = ErrorValue("completion_wait_timeout", "result", {"observed_wait_ns": 2_000_000_000})

    async def read(request, batch):
        context = _stop_context(owned, action_id)
        assert request.params["completion_context"] == context
        return _completed_result(request.ticket, context, error=error,
            completion=_completion(context, completed=False, observed_wait_ns=2_000_000_000))

    driver = result_driver(case, side_effect=read)
    try:
        await capture_handler(handler)(action_id, runtime)
        case.clock.wall = _NOW + 60_000_000
        case.clock.monotonic = 65_000_000_000
        await capture_handler(handler)(action_id, runtime)
        await capture_handler(handler)(action_id, runtime)
        case.stopper.stop.assert_awaited_once()
        assert driver.list_results.await_count == 2
        assert (await runtime.action(action_id))["status"] == 2
        assert owned.connection.execute("SELECT activity_state,occupancy_state,output_set_finalized_event_id FROM device_activities").fetchone() == (3, 2, None)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
        results = owned.connection.execute(
            "SELECT a.error_json,a.result_json FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
            " WHERE r.kind=7 ORDER BY a.id").fetchall()
        assert len(results) == 2
        for error_json, result_json in results:
            assert json.loads(error_json)["code"] == "completion_wait_timeout"
            completion = json.loads(result_json)["settlement"]["evidence"]["data"]["file_completion"]
            assert completion == _completion(_stop_context(owned, action_id), completed=False,
                observed_wait_ns=2_000_000_000)
    finally:
        await case.close()
        owned.connection.close()


@pytest.mark.parametrize("restart", [False, True], ids=["same-session", "reopened"])
async def test_later_result_round_reuses_completed_wait_from_metadata_failure(tmp_path, restart, capture_resources):
    case = ResultCase(capture_resources)
    owned, runtime, action_id, handler = await case.world(tmp_path, "record", start=False,
        catalog=WaitCatalog(), evidence_contracts=(_REGISTRY.contract("results_returned", 1),),
        assembly_options={"check_config": AttemptConfig(3, Decimal("10"), Decimal("0"))})
    error = ErrorValue("metadata_failed", "result", {"path": "/DCIM/a.mp4"})

    async def failed_read(request, batch):
        context = _stop_context(owned, action_id)
        assert request.params["completion_context"] == context
        return _completed_result(request.ticket, context, error=error)

    first_driver = result_driver(case, side_effect=failed_read)
    try:
        await capture_handler(handler)(action_id, runtime)
        case.clock.wall = _NOW + 60_000_000
        case.clock.monotonic = 65_000_000_000
        await capture_handler(handler)(action_id, runtime)
        saved, = saved_result_pages(owned, action_id)
        context = _stop_context(owned, action_id)
        prior = {"result_page_event_id": saved.ref.event_id, "evidence": _completion(context)}
        assert owned.connection.execute("SELECT output_set_finalized_event_id FROM device_activities").fetchone() == (None,)

        path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
        history = HistoryRepository(path)
        restored = history.restore_entity("action", action_id, history.current_boundary())
        attempt_id, result_json = owned.connection.execute(
            "SELECT id,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (saved.ref.ticket.run_id, saved.ref.ticket.attempt_id)).fetchone()
        actual_result = json.loads(result_json)
        assert restored[("operation_attempts", attempt_id)]["result_json"] == actual_result
        assert actual_result["settlement"]["evidence"]["data"]["file_completion"] == prior["evidence"]

        if restart:
            runtime = await case.reopen(owned, cold=True)

        async def resumed_read(request, batch):
            assert request.params["completion_context"] == {**context, "prior_completion": prior}
            return _completed_result(request.ticket, context, entries=[_entry("a")], finalized=True)

        second_driver = result_driver(case, side_effect=resumed_read)
        await capture_handler(handler)(action_id, runtime)
        assert (await runtime.action(action_id))["status"] == 3
        assert runtime.capture.read_result_page(saved.ref, owned) == saved
        first_driver.list_results.assert_awaited_once()
        second_driver.list_results.assert_awaited_once()
        assert _stop_context(owned, action_id)["stop_returned_at_us"] == context["stop_returned_at_us"]
    finally:
        await case.close()
        owned.connection.close()


@pytest.mark.parametrize("change", [
    {"activity_id": "2"}, {"stop_result_event_id": 9999},
    {"required_wait_ms": 4000}, {"completed": False},
])
async def test_saved_mismatched_wait_page_cannot_guarantee_current_record_files(tmp_path, change, capture_resources):
    case = ResultCase(capture_resources)
    owned, runtime, action_id, handler = await case.world(tmp_path, "record", start=False,
        catalog=WaitCatalog(), evidence_contracts=(_REGISTRY.contract("results_returned", 1),),
        assembly_options={"check_config": AttemptConfig(3, Decimal("10"), Decimal("2"))})

    async def read(request, batch):
        context = _stop_context(owned, action_id)
        return _completed_result(request.ticket, context, completion=_completion(context, **change))

    result_driver(case, side_effect=read)
    try:
        await capture_handler(handler)(action_id, runtime)
        case.clock.wall = _NOW + 60_000_000
        case.clock.monotonic = 65_000_000_000
        await capture_handler(handler)(action_id, runtime)
        seen = []

        async def next_read(request, batch):
            seen.append(request.params["completion_context"])
            return _completed_result(request.ticket, _stop_context(owned, action_id),
                completion=_completion(_stop_context(owned, action_id), **change))

        result_driver(case, side_effect=next_read)
        case.clock.monotonic += 3_000_000_000
        await capture_handler(handler)(action_id, runtime)
        assert seen == [_stop_context(owned, action_id)]
        assert owned.connection.execute("SELECT output_set_finalized_event_id FROM device_activities").fetchone() == (None,)
        case.stopper.stop.assert_awaited_once()
    finally:
        await case.close()
        owned.connection.close()


@pytest.mark.parametrize("restart", [False, True], ids=["same-session", "reopened"])
async def test_repeated_metadata_failures_keep_the_actual_wait_source_page(tmp_path, restart, capture_resources):
    case = ResultCase(capture_resources)
    owned, runtime, action_id, handler = await case.world(tmp_path, "record", start=False,
        catalog=WaitCatalog(), evidence_contracts=(_REGISTRY.contract("results_returned", 1),),
        assembly_options={"check_config": AttemptConfig(3, Decimal("10"), Decimal("0"))})
    error = ErrorValue("metadata_failed", "result", {"path": "/DCIM/a.mp4"})

    async def actual_wait(request, batch):
        context = _stop_context(owned, action_id)
        return _completed_result(request.ticket, context, error=error)

    result_driver(case, side_effect=actual_wait)
    try:
        await capture_handler(handler)(action_id, runtime)
        case.clock.wall = _NOW + 60_000_000
        case.clock.monotonic = 65_000_000_000
        await capture_handler(handler)(action_id, runtime)
        original, = saved_result_pages(owned, action_id)
        context = _stop_context(owned, action_id)
        prior = {"result_page_event_id": original.ref.event_id, "evidence": _completion(context)}

        async def copied_wait(request, batch):
            assert request.params["completion_context"]["prior_completion"] == prior
            return _completed_result(request.ticket, context, error=error,
                source_page_event_id=original.ref.event_id)

        result_driver(case, side_effect=copied_wait)
        await capture_handler(handler)(action_id, runtime)
        copied, = saved_result_pages(owned, action_id)
        assert copied.ref.event_id > original.ref.event_id
        if restart:
            runtime = await case.reopen(owned, cold=True)

        async def final_read(request, batch):
            assert request.params["completion_context"]["prior_completion"] == prior
            return _completed_result(request.ticket, context, entries=[_entry("a")], finalized=True,
                source_page_event_id=original.ref.event_id)

        driver = result_driver(case, side_effect=final_read)
        await capture_handler(handler)(action_id, runtime)
        driver.list_results.assert_awaited_once()
        assert runtime.capture.read_result_page(original.ref, owned) == original
        assert _stop_context(owned, action_id)["stop_returned_at_us"] == context["stop_returned_at_us"]
        assert (await runtime.action(action_id))["status"] == 3
    finally:
        await case.close()
        owned.connection.close()


@pytest.mark.parametrize("case", ["future", "wrong_event", "different_fact"])
async def test_saved_copy_wait_rejects_an_invalid_actual_source(tmp_path, case, capture_resources):
    session = ResultCase(capture_resources)
    owned, runtime, action_id, handler = await session.world(tmp_path, "record", start=False,
        catalog=WaitCatalog(), evidence_contracts=(_REGISTRY.contract("results_returned", 1),),
        assembly_options={"check_config": AttemptConfig(3, Decimal("10"), Decimal("0"))})
    error = ErrorValue("metadata_failed", "result", {"path": "/DCIM/a.mp4"})

    async def actual_wait(request, batch):
        context = _stop_context(owned, action_id)
        return _completed_result(request.ticket, context, error=error)

    result_driver(session, side_effect=actual_wait)
    try:
        await capture_handler(handler)(action_id, runtime)
        session.clock.wall = _NOW + 60_000_000
        session.clock.monotonic = 65_000_000_000
        await capture_handler(handler)(action_id, runtime)
        original, = saved_result_pages(owned, action_id)
        context = _stop_context(owned, action_id)
        reference = (original.ref.event_id + 1000 if case == "future" else
            context["stop_result_event_id"] if case == "wrong_event" else original.ref.event_id)
        completion = _completion(context,
            observed_wait_ns=5_000_000_001 if case == "different_fact" else 5_000_000_000)

        async def copied_wait(request, batch):
            return _completed_result(request.ticket, context, error=error,
                completion=completion, source_page_event_id=reference)

        result_driver(session, side_effect=copied_wait)
        with pytest.raises(ConsistencyError):
            await capture_handler(handler)(action_id, runtime)
        session.stopper.stop.assert_awaited_once()
        assert _stop_context(owned, action_id)["stop_returned_at_us"] == context["stop_returned_at_us"]
    finally:
        await session.close()
        owned.connection.close()

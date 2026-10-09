"""拍摄终态恢复必须保留实际读取结束后尚未收尾的 READ 责任。"""

from dataclasses import replace
from decimal import Decimal
import json

import pytest

from camctl.bootstrap import lifecycle
from camctl.capture.handlers import capture_handler
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import saved_transaction_events

from ..capture.media_retry_fixtures import media_pipeline as pipeline  # noqa: F401
from ..capture.test_capture_contract import ResultsDouble, _entry
from ..capture.test_input_copy import _CONTENT, _NOW
from ..capture.test_record_media_result_settlement import _TrackedCommitFailure, _WaitingProbe, _attempts
from .test_read_default_consumers import (
    _BeforeBusinessCandidates, _CandidateClock, _default_world, _snapshot,
)
from .test_recording_results_cancellation import (
    _BeforeCandidates, _CandidateBoundary, _apply_cancellation,
)


pytestmark = pytest.mark.asyncio
_ENTRANCES = ("normal", "residual", "restricted", "normal-cancel", "restricted-cancel")


async def _failed_read_world(pipeline, monkeypatch):
    """真实失败 READ 已结束，预算尚有余量，取消只保存业务事实。"""
    owned = pipeline[0]
    world = await _default_world(pipeline, monkeypatch)
    deps, factories, _context, reader, _driver, ends, _wall, _calls = world
    try:
        reader.fail = True
        runtime = factories[0](owned, "cam-1")
        runtime.results = ResultsDouble({1: (replace(
            _entry("original-recording", size=len(_CONTENT)),
            locator={"path": "/DCIM/original.mp4"}, original_name="original.mp4"),)})
        tools = _WaitingProbe(Decimal("61"))
        runtime.media.tools = tools
        await capture_handler("camera_record")(1, runtime)
        assert len(reader.requests) == len(ends) == 1
        ticket = reader.requests[0][0]
        assert ticket is not None and ticket.operation == "read"
        assert ends[0].stopped is True and ends[0].error is not None
        assert ends[0].bytes_read == 0
        attempt = owned.connection.execute(
            "SELECT status,error_json,result_json FROM operation_attempts"
            " WHERE run_id=? AND attempt_no=?", (ticket.run_id, ticket.attempt_id)).fetchone()
        assert attempt[0] == int(enum_for("operation_attempts.status").FAILED)
        assert json.loads(attempt[1])["code"] == "device_error"
        assert attempt[2] is not None
        before_run = _snapshot(owned, "operation_runs", ticket.run_id)
        assert before_run["status"] == int(enum_for("operation_runs.status").ACTIVE)
        assert before_run["retry_wait_required"] == 1
        assert before_run["attempts_used"] == 1 < before_run["max_attempts_used"]
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts WHERE status=1").fetchone() == (0,)
        assert tools.calls == [] and runtime.results.calls == [1]
        _apply_cancellation(owned, terminal=False)
        assert _snapshot(owned, "operation_runs", ticket.run_id) == before_run
        return world, runtime, tools, ticket, before_run
    except BaseException:
        lifecycle.close_runtime(deps)
        raise


def _select(context, entrance):
    return {
        "normal": context.flows["scheduling"],
        "residual": context.flows["residual"],
        "restricted": context.restricted_flows["winddown"],
        "normal-cancel": context.flows["cancel"],
        "restricted-cancel": context.restricted_flows["cancel"],
    }[entrance]


def _assert_original_facts(owned, attempts, history, reader, driver, tools, runtime, calls):
    assert _attempts(owned) == attempts
    assert owned.connection.execute(
        "SELECT * FROM history_events ORDER BY id").fetchall()[:len(history)] == history
    assert len(reader.requests) == 1
    assert tuple(driver.calls) == calls
    assert runtime.results.calls == [1] and tools.calls == []
    assert owned.connection.execute("SELECT COUNT(*) FROM outputs WHERE source_action_id=1").fetchone() == (0,)


def _assert_canceled_read(owned, ticket, before_run):
    assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (
        int(enum_for("actions.status").CANCELED),)
    assert _snapshot(owned, "operation_runs", ticket.run_id) == {
        **before_run, "status": int(enum_for("operation_runs.status").CANCELED),
        "retry_wait_required": 0,
    }


async def _before_candidates(context, reopened, entrance, saved):
    original_open, opened = context.open_connection, []

    def open_connection():
        current = original_open()
        assert current.metadata == reopened.metadata
        assert current.connection is not reopened.connection
        opened.append(current)
        return replace(current, connection=_CandidateBoundary(current.connection, saved))

    context.open_connection = open_connection
    context.clock = _CandidateClock(saved)
    try:
        with pytest.raises((_BeforeCandidates, _BeforeBusinessCandidates)):
            await _select(context, entrance)(context)
        assert len(opened) == 1
    finally:
        context.open_connection = original_open


@pytest.mark.parametrize("entrance", _ENTRANCES)
@pytest.mark.parametrize("after_commit", [False, True], ids=["commit-before", "commit-after"])
async def test_primary_unknown_restores_original_input_read_settlement(
        pipeline, monkeypatch, entrance, after_commit):
    owned, roots, _source_id = pipeline
    world, runtime, tools, ticket, before_run = await _failed_read_world(pipeline, monkeypatch)
    deps, _factories, context, reader, driver, _ends, wall, factory_calls = world
    actual_save = CaptureRepository.finish_canceled_capture
    inputs, outcomes = [], []
    attempts = _attempts(owned)
    history = owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()
    calls = tuple(driver.calls)
    reopened = None

    def save(repository, request, key, current):
        inputs.append((request, key))
        if len(inputs) == 1:
            fault = _TrackedCommitFailure(current.connection, after_commit)
            result = actual_save(repository, request, key, replace(current, connection=fault))
            assert result.kind is DbOutcomeKind.UNKNOWN and fault.commit_calls == 1
        else:
            result = actual_save(repository, request, key, current)
        outcomes.append(result)
        return result

    monkeypatch.setattr(CaptureRepository, "finish_canceled_capture", save)
    try:
        wall[0] = _NOW + 200_000_000
        with pytest.raises(ConsistencyError):
            await capture_handler("camera_record")(1, runtime)
        assert len(inputs) == 1
        request, key = inputs[0]
        assert request.occurred_at == wall[0]
        owned.connection.close()
        reopened = open_existing(roots.staging.parent / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
        assert (saved_transaction_events(reopened.connection, key) is not None) is after_commit
        assert _snapshot(reopened, "operation_runs", ticket.run_id) == before_run
        first_events = saved_transaction_events(reopened.connection, key)

        def saved():
            assert inputs == [(request, key), (request, key)]
            assert outcomes[-1].kind is DbOutcomeKind.COMPLETED
            _assert_canceled_read(reopened, ticket, before_run)
            _assert_original_facts(reopened, attempts, history, reader, driver, tools, runtime, calls)
            events = saved_transaction_events(reopened.connection, key)
            assert events is not None
            assert all(event["occurred_at"] == request.occurred_at for event in events)
            if first_events is not None:
                assert events == first_events
            assert reopened.connection.execute(
                "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(key),)).fetchone() == (1,)
            assert len(factory_calls) == 1

        wall[0] += 100_000_000
        await _before_candidates(context, reopened, entrance, saved)
        saved()
    finally:
        if reopened is not None:
            reopened.connection.close()
        lifecycle.close_runtime(deps)


@pytest.mark.parametrize("after_commit", [False, True], ids=["commit-before", "commit-after"])
async def test_input_read_settlement_unknown_preserves_original_request_key_and_time(
        pipeline, monkeypatch, after_commit):
    owned, roots, _source_id = pipeline
    world, runtime, tools, ticket, before_run = await _failed_read_world(pipeline, monkeypatch)
    deps, _factories, context, reader, driver, _ends, wall, factory_calls = world
    actual_primary = CaptureRepository.finish_canceled_capture
    actual_settle = OperationRepository.finish_stale_runs
    primary, inputs, outcomes = [], [], []
    attempts = _attempts(owned)
    history = owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()
    calls = tuple(driver.calls)
    reopened = None

    def save(repository, request, key, current):
        primary.append((request, key))
        result = actual_primary(repository, request, key, current)
        assert result.kind is DbOutcomeKind.COMPLETED
        return result

    def settle(repository, request, key, current):
        if f"read/{int(ticket.target_id)}" not in request.responsibility_keys:
            return actual_settle(repository, request, key, current)
        inputs.append((request, key))
        if len(inputs) == 1:
            fault = _TrackedCommitFailure(current.connection, after_commit)
            result = actual_settle(repository, request, key, replace(current, connection=fault))
            assert result.kind is DbOutcomeKind.UNKNOWN and fault.commit_calls == 1
        else:
            result = actual_settle(repository, request, key, current)
        outcomes.append(result)
        return result

    monkeypatch.setattr(CaptureRepository, "finish_canceled_capture", save)
    monkeypatch.setattr(OperationRepository, "finish_stale_runs", settle)
    try:
        wall[0] = _NOW + 200_000_000
        with pytest.raises((ConsistencyError, AssertionError)):
            await capture_handler("camera_record")(1, runtime)
        assert len(primary) == len(inputs) == 1
        request, key = inputs[0]
        assert request.occurred_at == wall[0]
        assert request.status.name == "CANCELED" and request.error is None
        owned.connection.close()
        reopened = open_existing(roots.staging.parent / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
        primary_events = saved_transaction_events(reopened.connection, primary[0][1])
        assert primary_events is not None
        assert (saved_transaction_events(reopened.connection, key) is not None) is after_commit
        if after_commit:
            _assert_canceled_read(reopened, ticket, before_run)
        else:
            assert _snapshot(reopened, "operation_runs", ticket.run_id) == before_run
        first_events = saved_transaction_events(reopened.connection, key)

        def saved():
            assert inputs == [(request, key), (request, key)]
            assert outcomes[-1].kind is DbOutcomeKind.COMPLETED
            _assert_canceled_read(reopened, ticket, before_run)
            _assert_original_facts(reopened, attempts, history, reader, driver, tools, runtime, calls)
            assert saved_transaction_events(reopened.connection, primary[0][1]) == primary_events
            events = saved_transaction_events(reopened.connection, key)
            assert events is not None and all(event["occurred_at"] == request.occurred_at for event in events)
            if first_events is not None:
                assert events == first_events
            for operation_key in (primary[0][1], key):
                assert reopened.connection.execute(
                    "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
                    (str(operation_key),)).fetchone() == (1,)
            assert len(factory_calls) == 1

        wall[0] += 100_000_000
        await _before_candidates(context, reopened, "normal", saved)
        saved()
    finally:
        if reopened is not None:
            reopened.connection.close()
        lifecycle.close_runtime(deps)

"""正式拍摄终态恢复保留实际失败读取结束后尚未收尾的 READ 用途。"""

from dataclasses import replace
import json

import pytest

from camctl.capture.handlers import capture_handler
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.transactions import saved_transaction_events

from ..capture.media_retry_fixtures import media_pipeline as pipeline  # noqa: F401
from ..capture.test_input_copy import _NOW
from ..capture.test_record_media_result_settlement import _attempts
from ..support.bootstrap_consumers import recording_world, apply_cancellation, TransactionFault
from .test_read_default_consumers import _snapshot

pytestmark = pytest.mark.asyncio
_ENTRANCES = ("normal", "residual", "restricted", "normal-cancel", "restricted-cancel")


async def _failed_read_world(pipeline, resources):
    world = await recording_world(pipeline, resources, failed_read=True)
    owned = world.owned
    ticket = world.reader.requests[0][0]
    assert ticket.operation == "read"
    assert world.ends[0].stopped and world.ends[0].error is not None and world.ends[0].bytes_read == 0
    attempt = owned.connection.execute(
        "SELECT status,error_json,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
        (ticket.run_id, ticket.attempt_id)).fetchone()
    assert attempt[0] == int(enum_for("operation_attempts.status").FAILED)
    assert json.loads(attempt[1])["code"] == "device_error" and attempt[2] is not None
    before_run = _snapshot(owned, "operation_runs", ticket.run_id)
    assert before_run["status"] == int(enum_for("operation_runs.status").ACTIVE)
    assert before_run["retry_wait_required"] == 1
    assert before_run["attempts_used"] == 1 < before_run["max_attempts_used"]
    assert owned.connection.execute("SELECT COUNT(*) FROM operation_attempts WHERE status=1").fetchone() == (0,)
    await apply_cancellation(world, terminal=False)
    assert _snapshot(owned, "operation_runs", ticket.run_id) == before_run
    return world, ticket, before_run


def _assert_original_facts(world, attempts, history, calls):
    owned = world.owned
    assert _attempts(owned) == attempts
    assert owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()[:len(history)] == history
    assert len(world.reader.requests) == 1 and tuple(world.driver.calls) == calls
    assert world.results.calls == [1] and world.tools() == []
    assert owned.connection.execute("SELECT COUNT(*) FROM outputs WHERE source_action_id=1").fetchone() == (0,)


def _assert_canceled_read(owned, ticket, before_run):
    assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (int(enum_for("actions.status").CANCELED),)
    assert _snapshot(owned, "operation_runs", ticket.run_id) == {
        **before_run, "status": int(enum_for("operation_runs.status").CANCELED), "retry_wait_required": 0}


@pytest.mark.parametrize("entrance", _ENTRANCES)
@pytest.mark.parametrize("after_commit", [False, True], ids=["commit-before", "commit-after"])
async def test_primary_unknown_restores_original_input_read_settlement(
        pipeline, capture_resources, monkeypatch, entrance, after_commit):
    world, ticket, before_run = await _failed_read_world(pipeline, capture_resources)
    actual_save = CaptureRepository.finish_canceled_capture
    inputs, outcomes = [], []
    attempts = _attempts(world.owned)
    history = world.owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()
    calls = tuple(world.driver.calls)

    def save(repository, request, key, current):
        inputs.append((request, key))
        if len(inputs) == 1:
            fault = TransactionFault(current.connection, after_commit=after_commit)
            result = actual_save(repository, request, key, replace(current, connection=fault))
            assert result.kind is DbOutcomeKind.UNKNOWN and fault.commit_calls == 1
        else:
            result = actual_save(repository, request, key, current)
        outcomes.append(result)
        return result

    monkeypatch.setattr(CaptureRepository, "finish_canceled_capture", save)
    try:
        world.wall[0] = _NOW + 200_000_000
        with pytest.raises(ConsistencyError):
            await capture_handler("camera_record")(1, world.runtime)
        assert len(inputs) == 1
        request, key = inputs[0]
        assert request.occurred_at == world.wall[0]
        await world.reconnect()
        assert (saved_transaction_events(world.owned.connection, key) is not None) is after_commit
        assert _snapshot(world.owned, "operation_runs", ticket.run_id) == before_run
        first_events = saved_transaction_events(world.owned.connection, key)
        world.wall[0] += 100_000_000
        world.prime_clock()
        await world.replay(entrance, kind="completion")
        assert inputs == [(request, key), (request, key)] and outcomes[-1].kind is DbOutcomeKind.COMPLETED
        _assert_canceled_read(world.owned, ticket, before_run)
        _assert_original_facts(world, attempts, history, calls)
        events = saved_transaction_events(world.owned.connection, key)
        assert events is not None and all(event["occurred_at"] == request.occurred_at for event in events)
        if first_events is not None:
            assert events == first_events
        assert world.owned.connection.execute("SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(key),)).fetchone() == (1,)
    finally:
        await world.close()


@pytest.mark.parametrize("after_commit", [False, True], ids=["commit-before", "commit-after"])
async def test_input_read_settlement_unknown_preserves_original_request_key_and_time(
        pipeline, capture_resources, monkeypatch, after_commit):
    world, ticket, before_run = await _failed_read_world(pipeline, capture_resources)
    actual_primary, actual_settle = CaptureRepository.finish_canceled_capture, OperationRepository.finish_stale_runs
    primary, inputs, outcomes = [], [], []
    attempts = _attempts(world.owned)
    history = world.owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()
    calls = tuple(world.driver.calls)

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
            fault = TransactionFault(current.connection, after_commit=after_commit)
            result = actual_settle(repository, request, key, replace(current, connection=fault))
            assert result.kind is DbOutcomeKind.UNKNOWN and fault.commit_calls == 1
        else:
            result = actual_settle(repository, request, key, current)
        outcomes.append(result)
        return result

    monkeypatch.setattr(CaptureRepository, "finish_canceled_capture", save)
    monkeypatch.setattr(OperationRepository, "finish_stale_runs", settle)
    try:
        world.wall[0] = _NOW + 200_000_000
        with pytest.raises(ConsistencyError):
            await capture_handler("camera_record")(1, world.runtime)
        assert len(primary) == len(inputs) == 1
        request, key = inputs[0]
        assert request.occurred_at == world.wall[0] and request.status.name == "CANCELED" and request.error is None
        await world.reconnect()
        primary_events = saved_transaction_events(world.owned.connection, primary[0][1])
        assert primary_events is not None
        assert (saved_transaction_events(world.owned.connection, key) is not None) is after_commit
        if after_commit:
            _assert_canceled_read(world.owned, ticket, before_run)
        else:
            assert _snapshot(world.owned, "operation_runs", ticket.run_id) == before_run
        first_events = saved_transaction_events(world.owned.connection, key)
        world.wall[0] += 100_000_000
        world.prime_clock()
        await world.replay("normal", kind="completion")
        assert len(primary) == 1 and inputs == [(request, key), (request, key)]
        assert outcomes[-1].kind is DbOutcomeKind.COMPLETED
        _assert_canceled_read(world.owned, ticket, before_run)
        _assert_original_facts(world, attempts, history, calls)
        assert saved_transaction_events(world.owned.connection, primary[0][1]) == primary_events
        events = saved_transaction_events(world.owned.connection, key)
        assert events is not None and all(event["occurred_at"] == request.occurred_at for event in events)
        if first_events is not None:
            assert events == first_events
        for operation_key in (primary[0][1], key):
            assert world.owned.connection.execute("SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(operation_key),)).fetchone() == (1,)
    finally:
        await world.close()

"""默认入口先核实原有限 RESULTS 耗尽申请，再取得本轮业务资格。"""

from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.bootstrap import lifecycle
from camctl.capture.handlers import capture_handler
from camctl.capture.models import ResultSetPhase
from camctl.contracts.values import ConsistencyError
from camctl.contracts.workflow_errors import registered_error
from camctl.devices.evidence import DeviceObservation
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import (
    AttemptStatus, CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import saved_transaction_events
from camctl.session.service import StateDbFailure

from ..capture.result_consumer_fixtures import consumer_world
from ..capture.test_record_media_result_settlement import _TrackedCommitFailure
from ..capture.test_result_consumer_saves import _result_port
from .test_file_fact_consumers import _default_context
from .test_recording_results_saved_consumers import _UnavailableOriginalKey


pytestmark = pytest.mark.asyncio
_ENTRANCES = ("normal", "residual", "restricted", "cancel", "restricted_cancel")
_CANDIDATE_QUERIES = {
    "residual": "SELECT t.id, t.attempt_no, r.id, r.kind",
    "restricted": "SELECT id FROM actions WHERE type = 2 AND status = 2",
    "restricted_cancel": "SELECT id, status, input_fields_json FROM actions WHERE type = 6",
}


class _BeforeBusinessCandidates(Exception):
    """原保存已核实；本用例在本轮业务资格判定之前截停。"""


class _CandidateClock:
    def __init__(self, check):
        self.check = check
        self.calls = 0

    def utc_micros(self):
        self.calls += 1
        self.check()
        raise _BeforeBusinessCandidates


class _CandidateQuery:
    """透传真实 SQLite；在选取本轮业务候选之前检查原保存门。"""

    def __init__(self, connection, prefix, check):
        self.connection, self.prefix, self.check = connection, prefix, check
        self.hits = 0

    def execute(self, sql, parameters=()):
        if " ".join(sql.split()).startswith(self.prefix):
            self.hits += 1
            self.check()
            raise _BeforeBusinessCandidates
        return self.connection.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self.connection, name)


def _flow(context, entrance):
    if entrance == "restricted":
        return context.restricted_flows["winddown"]
    if entrance == "restricted_cancel":
        return context.restricted_flows["cancel"]
    return context.flows[{"normal": "scheduling", "residual": "residual", "cancel": "cancel"}[entrance]]


def _candidate_boundary(world, entrance, check):
    original_open, opened, queries = world.context.open_connection, [], []

    def open_connection():
        current = original_open()
        assert current.metadata == world.owned.metadata
        assert current.connection is not world.owned.connection
        if entrance in _CANDIDATE_QUERIES:
            query = _CandidateQuery(current.connection, _CANDIDATE_QUERIES[entrance], check)
            queries.append(query)
            current = replace(current, connection=query)
        opened.append(current)
        return current

    world.context.open_connection = open_connection
    clock = _CandidateClock(check)
    world.context.clock = clock
    return opened, queries, clock


@asynccontextmanager
async def _held_exhaustion(tmp_path, monkeypatch, consumer, after):
    """真实输入形成原 G；旧连接关闭以后才确定是否存在可靠 F。"""
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, consumer, independent_activity=True)
    deps = reopened = None
    try:
        deps, context, factory_calls = await _default_context(tmp_path, monkeypatch)
        # 责任在首次真实保存之前就属于该会话，不从故障后的请求另造缓存。
        runtime.pending_result_closes = deps.capture_result_closes
        runtime.pending_start_results = deps.capture_call_results
        runtime.pending_file_observations = deps.capture_file_observations
        runtime.retry_gate = deps.capture_retry_gate
        activity_id, = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
        assert activity_id != action_id
        actual = CallOutcome(status=AttemptStatus.SUCCEEDED, effect=EffectState.CONFIRMED,
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("results_returned", 1, {})),
            observations=(DeviceObservation("result_files_listed", 1, {
                "activity_id": str(activity_id), "entries": [{"identity": "auxiliary",
                    "kind": "other", "complete": True, "size_bytes": 41,
                    "locator": {"path": "/DCIM/auxiliary"},
                    "original_name": "auxiliary.bin", "media_type": "application/octet-stream"}]}),))
        driver = _result_port(runtime, actual)
        runtime.check_config = AttemptConfig(1, Decimal("1.25"), Decimal(0))
        advance = capture_handler(handler)
        await advance(action_id, runtime)
        original_attempts = owned.connection.execute(
            "SELECT * FROM operation_attempts ORDER BY id").fetchall()
        original_files = owned.connection.execute("SELECT * FROM device_files ORDER BY id").fetchall()
        assert len(original_files) == 1
        run_id, status, used, retry = owned.connection.execute(
            "SELECT id,status,attempts_used,retry_wait_required FROM operation_runs"
            " WHERE responsibility_key=?", (f"results/{activity_id}",)).fetchone()
        assert (status, used, retry) == (2, 1, 1)
        formed_at = runtime.wall_us() + 1_000_000
        runtime.wall_us = lambda: formed_at
        original_save = CaptureRepository.close_result_check_unconfirmed
        inputs, outcomes, faults, connections, unavailable = [], [], [], [], []
        state = {"confirmation": "saved"}

        def save(repository, request, key, current):
            held = deps.capture_result_closes[action_id]
            assert (held.request, held.key) == (request, key)
            inputs.append((request, key))
            connections.append(current.connection)
            if len(inputs) == 1:
                fault = _TrackedCommitFailure(current.connection, after)
                faults.append(fault)
                current = replace(current, connection=fault)
            elif state["confirmation"] != "saved":
                fault = _UnavailableOriginalKey(
                    current.connection, key, state["confirmation"] == "unknown")
                unavailable.append(fault)
                current = replace(current, connection=fault)
            outcome = original_save(repository, request, key, current)
            outcomes.append(outcome)
            return outcome

        monkeypatch.setattr(CaptureRepository, "close_result_check_unconfirmed", save)
        with pytest.raises(ConsistencyError):
            await advance(action_id, runtime)
        assert len(inputs) == len(outcomes) == len(faults) == 1
        assert faults[0].commit_calls == 1 and outcomes[0].kind is DbOutcomeKind.UNKNOWN
        request, key = inputs[0]
        expected_error = {"code": "capture_result_unconfirmed",
            "stage": registered_error("capture_result_unconfirmed")["stage"],
            "details": {"activity_id": str(activity_id), "reason": "outputs_unknown"}}
        assert request.action_id == action_id and request.occurred_at == formed_at
        assert request.phase is ResultSetPhase.UNCONFIRMED
        assert request.contract == "task_scope_files"
        assert request.observation == {"reason": "attempts_exhausted"}
        assert request.capture == {"status": "unconfirmed", "error": expected_error}
        assert request.error == expected_error
        path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
        metadata = owned.metadata
        owned.connection.close()
        reopened = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        assert reopened.metadata == metadata
        assert (saved_transaction_events(reopened.connection, key) is not None) is after
        assert reopened.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (2,)
        assert reopened.connection.execute(
            "SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE id=?",
            (run_id,)).fetchone() == ((6, 1, 0) if after else (2, 1, 1))
        assert reopened.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall() == original_attempts
        assert reopened.connection.execute("SELECT * FROM device_files ORDER BY id").fetchall() == original_files
        before = tuple(reopened.connection.iterdump())
        history = reopened.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()
        held = deps.capture_result_closes[action_id]
        assert (held.request, held.key) == (request, key)
        yield SimpleNamespace(owned=reopened, runtime=runtime, deps=deps, context=context,
            action_id=action_id, activity_id=activity_id, run_id=run_id, driver=driver,
            factory_calls=factory_calls, inputs=inputs, outcomes=outcomes,
            connections=connections, request=request, key=key, formed_at=formed_at,
            before=before, history=history, attempts=original_attempts, files=original_files,
            state=state, unavailable=unavailable, held=held)
    finally:
        owned.connection.close()
        if reopened is not None:
            reopened.connection.close()
        if deps is not None:
            lifecycle.close_runtime(deps)


def _original_input_and_external_calls(world, opened):
    assert world.inputs == [(world.request, world.key), (world.request, world.key)]
    assert world.request.occurred_at == world.formed_at
    assert len(opened) == 1 and world.connections[1] is opened[0].connection
    assert world.connections[1] is not world.connections[0]
    assert world.factory_calls == []
    assert world.driver.list_results.await_count == 1
    world.runtime.driver.control.assert_awaited_once()
    assert world.owned.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall() == world.attempts
    assert world.owned.connection.execute("SELECT * FROM device_files ORDER BY id").fetchall() == world.files
    assert world.owned.connection.execute(
        "SELECT status FROM actions WHERE id=?", (world.action_id,)).fetchone() == (2,)


@pytest.mark.parametrize("entrance", _ENTRANCES)
@pytest.mark.parametrize("consumer", ["photo", "timelapse"])
@pytest.mark.parametrize("after", [False, True], ids=["commit-before", "commit-after"])
async def test_default_prefix_confirms_original_exhaustion_before_candidates(
        tmp_path, monkeypatch, entrance, consumer, after):
    async with _held_exhaustion(tmp_path, monkeypatch, consumer, after) as world:
        def before_candidates():
            _original_input_and_external_calls(world, opened)
            assert world.outcomes[1].kind is DbOutcomeKind.COMPLETED
            assert not world.deps.capture_result_closes
            assert saved_transaction_events(world.owned.connection, world.key) is not None
            assert world.owned.connection.execute(
                "SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE id=?",
                (world.run_id,)).fetchone() == (6, 1, 0)
            capture, error = world.owned.connection.execute(
                "SELECT capture_json,last_error_json FROM device_activities WHERE id=?",
                (world.activity_id,)).fetchone()
            assert json.loads(capture) == world.request.capture
            assert json.loads(error) == world.request.error
            assert world.owned.connection.execute(
                "SELECT * FROM history_events ORDER BY id").fetchall()[:len(world.history)] == world.history
            if after:
                assert tuple(world.owned.connection.iterdump()) == world.before

        opened, queries, clock = _candidate_boundary(world, entrance, before_candidates)
        with pytest.raises(_BeforeBusinessCandidates):
            await _flow(world.context, entrance)(world.context)
        if entrance in _CANDIDATE_QUERIES:
            assert clock.calls == 0 and len(queries) == 1 and queries[0].hits == 1
        else:
            assert clock.calls == 1 and queries == []
        assert world.owned.connection.execute(
            "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
            (str(world.key),)).fetchone() == (1,)


@pytest.mark.parametrize("entrance", _ENTRANCES)
@pytest.mark.parametrize("confirmation", ["unknown", "rolled-back"])
async def test_default_prefix_retains_original_exhaustion_when_confirmation_fails(
        tmp_path, monkeypatch, entrance, confirmation):
    async with _held_exhaustion(tmp_path, monkeypatch, "photo", True) as world:
        world.state["confirmation"] = confirmation

        def forbidden_candidates():
            raise AssertionError("原耗尽申请尚未可靠核实，不能读取业务钟或选取业务候选")

        opened, queries, clock = _candidate_boundary(world, entrance, forbidden_candidates)
        with pytest.raises(StateDbFailure):
            await _flow(world.context, entrance)(world.context)

        _original_input_and_external_calls(world, opened)
        expected = DbOutcomeKind.UNKNOWN if confirmation == "unknown" else DbOutcomeKind.ROLLED_BACK
        assert world.outcomes[1].kind is expected
        assert len(world.unavailable) == 1 and world.unavailable[0].key_reads == 1
        assert world.deps.capture_result_closes[world.action_id] is world.held
        assert (world.held.request, world.held.key) == (world.request, world.key)
        assert clock.calls == 0 and all(query.hits == 0 for query in queries)
        assert tuple(world.owned.connection.iterdump()) == world.before

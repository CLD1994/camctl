"""录像耗尽的原流程申请在实际默认入口先保存，再判定业务资格。"""

from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.bootstrap import lifecycle
from camctl.capture.handlers import capture_handler
from camctl.capture.models import ResultRunClose
from camctl.contracts.values import ConsistencyError
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
from .test_result_exhaustion_default_recovery import (
    _BeforeBusinessCandidates, _CANDIDATE_QUERIES, _ENTRANCES,
    _candidate_boundary, _flow,
)


pytestmark = pytest.mark.asyncio


def _rows(owned, table):
    return owned.connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()


@asynccontextmanager
async def _held_run_close(tmp_path, monkeypatch, after, *, with_file=True):
    """公开 START/STOP 和真实 v1 轮次先完成，首个 G 真正取得 UNKNOWN。"""
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, "record", independent_activity=True)
    deps = reopened = None
    try:
        deps, context, factory_calls = await _default_context(tmp_path, monkeypatch)
        runtime.pending_result_closes = deps.capture_result_closes
        runtime.pending_start_results = deps.capture_call_results
        runtime.pending_recording_results = deps.capture_recording_results
        runtime.pending_file_observations = deps.capture_file_observations
        runtime.retry_gate = deps.capture_retry_gate
        activity_id, = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
        assert activity_id != action_id
        entries = [{"identity": "auxiliary", "kind": "other", "complete": True,
            "size_bytes": 41, "locator": {"path": "/DCIM/auxiliary"},
            "original_name": "auxiliary.bin", "media_type": "application/octet-stream"}]
        actual = CallOutcome(status=AttemptStatus.SUCCEEDED, effect=EffectState.CONFIRMED,
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("results_returned", 1, {})),
            observations=(DeviceObservation("result_files_listed", 1, {
                "activity_id": str(activity_id), "entries": entries if with_file else []}),))
        driver = _result_port(runtime, actual)
        runtime.check_config = AttemptConfig(1, Decimal("1.25"), Decimal(0))
        advance = capture_handler(handler)
        await advance(action_id, runtime)
        responsibility = f"results/{activity_id}"
        run_id, status, used, retry = owned.connection.execute(
            "SELECT id,status,attempts_used,retry_wait_required FROM operation_runs"
            " WHERE responsibility_key=?", (responsibility,)).fetchone()
        assert (status, used, retry) == (2, 1, 1)
        assert runtime.action(action_id)["status"] == 2
        assert owned.connection.execute(
            "SELECT activity_state,occupancy_state,capture_json,last_error_json,result_check_json"
            " FROM device_activities WHERE id=?", (activity_id,)).fetchone() == (3, 2, None, None, None)
        attempts, files = _rows(owned, "operation_attempts"), _rows(owned, "device_files")
        assert len(files) == int(with_file)
        assert responsibility in runtime.retry_gate.anchors
        original_anchors = dict(runtime.retry_gate.anchors)
        formed_at = runtime.wall_us() + 1_000_000
        runtime.wall_us = lambda: formed_at
        original_save = CaptureRepository.close_unconfirmed_result_run
        inputs, outcomes, faults, connections, unavailable = [], [], [], [], []
        state = {"confirmation": "saved"}

        def save(repository, request, key, current):
            # 首写不要求尚未实现的 holder；真实 COMMIT 故障必须先命中。
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
            result = original_save(repository, request, key, current)
            outcomes.append(result)
            return result

        monkeypatch.setattr(CaptureRepository, "close_unconfirmed_result_run", save)
        with pytest.raises((ConsistencyError, AssertionError)):
            await advance(action_id, runtime)
        assert len(inputs) == len(outcomes) == len(faults) == 1
        assert faults[0].commit_calls == 1
        assert outcomes[0].kind is DbOutcomeKind.UNKNOWN
        request, key = inputs[0]
        assert isinstance(request, ResultRunClose)
        assert (request.action_id, request.occurred_at) == (action_id, formed_at)
        assert dict(runtime.retry_gate.anchors) == original_anchors
        path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
        metadata = owned.metadata
        owned.connection.close()
        reopened = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        assert reopened.metadata == metadata
        assert (saved_transaction_events(reopened.connection, key) is not None) is after
        assert reopened.connection.execute(
            "SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE id=?",
            (run_id,)).fetchone() == ((6, 1, 0) if after else (2, 1, 1))
        assert reopened.connection.execute(
            "SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (2,)
        assert _rows(reopened, "operation_attempts") == attempts
        assert _rows(reopened, "device_files") == files
        # UNKNOWN 后记录 optional，缺失应在真实恢复行为边界证伪。
        held = deps.capture_result_closes.get(action_id)
        yield SimpleNamespace(owned=reopened, runtime=runtime, deps=deps, context=context,
            action_id=action_id, activity_id=activity_id, responsibility=responsibility,
            run_id=run_id, driver=driver, factory_calls=factory_calls,
            inputs=inputs, outcomes=outcomes, connections=connections,
            request=request, key=key, formed_at=formed_at, attempts=attempts, files=files,
            before=tuple(reopened.connection.iterdump()), history=_rows(reopened, "history_events"),
            anchors=original_anchors, state=state, unavailable=unavailable, held=held,
            advance=advance)
    finally:
        owned.connection.close()
        if reopened is not None:
            reopened.connection.close()
        if deps is not None:
            lifecycle.close_runtime(deps)


def _unchanged_actual_facts(world):
    assert _rows(world.owned, "operation_attempts") == world.attempts
    assert _rows(world.owned, "device_files") == world.files
    assert world.driver.list_results.await_count == 1
    world.runtime.driver.control.assert_awaited_once()
    world.runtime.stopper.stop.assert_awaited_once()
    assert world.factory_calls == []


def _original_resend(world):
    assert world.inputs == [(world.request, world.key), (world.request, world.key)]
    assert world.inputs[1][0] is world.request
    assert world.request.occurred_at == world.formed_at
    assert world.held is not None
    assert (world.held.request, world.held.key) == (world.request, world.key)


def _reliably_closed(world):
    _original_resend(world)
    assert world.outcomes[1].kind is DbOutcomeKind.COMPLETED
    assert world.outcomes[1].value is None
    assert not world.deps.capture_result_closes
    assert world.responsibility not in world.runtime.retry_gate.anchors
    group = saved_transaction_events(world.owned.connection, world.key)
    assert group is not None and len(group) == 1
    assert group[0]["occurred_at"] == world.formed_at
    values = group[0]["body"]["rows"][0]["after"]["values"]
    assert values["status"] == 6 and values["retry_wait_required"] == 0
    assert json.loads(world.owned.connection.execute(
        "SELECT error_json FROM operation_runs WHERE id=?", (world.run_id,)).fetchone()[0]) == {
            "code": "capture_result_unconfirmed", "stage": "execution",
            "details": {"activity_id": str(world.activity_id), "reason": "outputs_unknown"}}
    assert world.owned.connection.execute(
        "SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE id=?",
        (world.run_id,)).fetchone() == (6, 1, 0)
    assert world.owned.connection.execute(
        "SELECT capture_json,last_error_json,result_check_json FROM device_activities WHERE id=?",
        (world.activity_id,)).fetchone() == (None, None, None)
    assert _rows(world.owned, "history_events")[:len(world.history)] == world.history
    assert world.owned.connection.execute(
        "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
        (str(world.key),)).fetchone() == (1,)


@pytest.mark.parametrize("entrance", _ENTRANCES)
@pytest.mark.parametrize("after", [False, True], ids=["commit-before", "commit-after"])
async def test_default_prefix_confirms_original_record_run_close_before_candidates(
        tmp_path, monkeypatch, entrance, after):
    async with _held_run_close(tmp_path, monkeypatch, after) as world:
        def before_candidates():
            _reliably_closed(world)
            _unchanged_actual_facts(world)
            assert len(opened) == 1 and world.connections[1] is opened[0].connection
            assert world.connections[1] is not world.connections[0]
            assert world.owned.connection.execute(
                "SELECT status FROM actions WHERE id=?", (world.action_id,)).fetchone() == (2,)
            if after:
                assert tuple(world.owned.connection.iterdump()) == world.before

        opened, queries, clock = _candidate_boundary(world, entrance, before_candidates)
        with pytest.raises(_BeforeBusinessCandidates):
            await _flow(world.context, entrance)(world.context)
        if entrance in _CANDIDATE_QUERIES:
            assert clock.calls == 0 and len(queries) == 1 and queries[0].hits == 1
        else:
            assert clock.calls == 1 and queries == []


@pytest.mark.parametrize("entrance", _ENTRANCES)
@pytest.mark.parametrize("confirmation", ["unknown", "rolled-back"])
async def test_default_prefix_blocks_candidates_while_original_record_run_close_unreliable(
        tmp_path, monkeypatch, entrance, confirmation):
    async with _held_run_close(tmp_path, monkeypatch, True) as world:
        world.state["confirmation"] = confirmation

        def forbidden_candidates():
            raise AssertionError("原录像耗尽申请尚未可靠核实，不能读取业务钟或选候选")

        opened, queries, clock = _candidate_boundary(world, entrance, forbidden_candidates)
        with pytest.raises(StateDbFailure):
            await _flow(world.context, entrance)(world.context)
        _original_resend(world)
        _unchanged_actual_facts(world)
        assert len(opened) == 1 and world.connections[1] is opened[0].connection
        expected = DbOutcomeKind.UNKNOWN if confirmation == "unknown" else DbOutcomeKind.ROLLED_BACK
        assert world.outcomes[1].kind is expected
        assert len(world.unavailable) == 1 and world.unavailable[0].key_reads == 1
        assert world.deps.capture_result_closes[world.action_id] is world.held
        assert dict(world.runtime.retry_gate.anchors) == world.anchors
        assert clock.calls == 0 and all(query.hits == 0 for query in queries)
        assert tuple(world.owned.connection.iterdump()) == world.before


@pytest.mark.parametrize("after", [False, True], ids=["commit-before", "commit-after"])
async def test_record_handler_resends_original_run_close_before_business_finish(
        tmp_path, monkeypatch, after):
    async with _held_run_close(tmp_path, monkeypatch, after) as world:
        original_finish = CaptureRepository.finish_capture
        finishes = []

        def finish(repository, request, key, current):
            _reliably_closed(world)
            assert world.runtime.action(world.action_id)["status"] == 2
            finishes.append((request, key))
            return original_finish(repository, request, key, current)

        monkeypatch.setattr(CaptureRepository, "finish_capture", finish)
        world.runtime.owned = world.owned
        world.runtime.wall_us = lambda: world.formed_at + 2_000_000
        await world.advance(world.action_id, world.runtime)
        assert len(finishes) == 1
        assert world.runtime.action(world.action_id)["status"] == 4
        _unchanged_actual_facts(world)
        assert world.inputs[1][0] is world.request


@pytest.mark.parametrize("with_file", [False, True], ids=["empty", "complete-other"])
async def test_reopened_record_without_session_request_consumes_durable_unconfirmed_run(
        tmp_path, monkeypatch, with_file):
    async with _held_run_close(tmp_path, monkeypatch, True, with_file=with_file) as world:
        # 新消费者没有旧会话 holder；只使用已提交流程和真实 v1 文件事实。
        reopened_runtime = replace(world.runtime, owned=world.owned,
            pending_result_closes={}, pending_recording_results={}, pending_start_results={},
            pending_file_observations={}, listing_cache=None, recording_state=None)
        reopened_runtime.wall_us = lambda: world.formed_at + 2_000_000
        await world.advance(world.action_id, reopened_runtime)
        assert len(world.inputs) == 1, "持久 UNCONFIRMED 不制造或伪装旧 key 重送"
        assert reopened_runtime.action(world.action_id)["status"] == 4
        assert reopened_runtime.owned.connection.execute(
            "SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE id=?",
            (world.run_id,)).fetchone() == (6, 1, 0)
        assert saved_transaction_events(world.owned.connection, world.key)[0]["occurred_at"] == world.formed_at
        _unchanged_actual_facts(world)

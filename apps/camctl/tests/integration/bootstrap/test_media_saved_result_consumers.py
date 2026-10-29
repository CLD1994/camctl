"""默认三工厂在原动作终态后仍须保存已取得的媒体结果。"""

from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap import capture_assembly, lifecycle
from camctl.capture.media_flow import run_recording_media
from camctl.capture.processing import CheckPhase
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError
from camctl.history.events import business_columns
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.transaction import row_facts, saved_transaction_events

from ..capture.media_retry_fixtures import _Catalog, media_pipeline as pipeline  # noqa: F401
from ..capture.test_capture_contract import ResultsDouble
from ..capture.test_input_copy import _CONTENT, _NOW


pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("entrance", ["normal", "residual", "restricted"])
@pytest.mark.parametrize("branch", ["check", "decision", "repair_failed", "repair_complete"])
async def test_default_consumers_save_original_media_after_target_terminal(pipeline, capture_resources, monkeypatch, entrance, branch):
    from ..support.bootstrap_consumers import consumer_world, TransactionFault, cancel_original

    world = await consumer_world(pipeline, capture_resources, branch=branch,
        collections={"pending_media_results": {}})
    method = {"check": "save_check_result", "decision": "save_repair_decision",
        "repair_failed": "save_repair_result", "repair_complete": "complete_repair_output"}[branch]
    original_save = getattr(CaptureRepository, method)
    inputs = []

    def save(repository, command, key, current):
        if branch == "check" and command.phase is CheckPhase.RUNNING:
            return original_save(repository, command, key, current)
        inputs.append((command, key))
        if len(inputs) == 1:
            fault = TransactionFault(current.connection, after_commit=True)
            result = original_save(repository, command, key, replace(current, connection=fault))
            assert result.kind is DbOutcomeKind.UNKNOWN and fault.commit_calls == 1
            return result
        return original_save(repository, command, key, current)

    monkeypatch.setattr(CaptureRepository, method, save)
    try:
        with pytest.raises(ConsistencyError):
            await run_recording_media(world.runtime.media, 1, 1, world.source_id)
        expected_tools = ["probe"] + (["repair"] if branch.startswith("repair_") else [])
        assert len(inputs) == 1 and world.tools() == expected_tools
        await world.reconnect()
        assert saved_transaction_events(world.owned.connection, inputs[0][1]) is not None
        await cancel_original(world)
        world.prime_clock()
        columns = business_columns("actions")
        original_action = {name: row_facts(world.owned.connection, "actions", 1)[name] for name in columns}
        processing_columns = business_columns("recording_processing")
        original_processing = {name: row_facts(world.owned.connection, "recording_processing", 1)[name]
            for name in processing_columns}
        await world.replay(entrance, kind="media")
        assert inputs == [inputs[0], inputs[0]], "终态动作仍须按原媒体输入和原 key 核实"
        assert world.tools() == expected_tools and len(world.reader.requests) == 1
        assert {name: row_facts(world.owned.connection, "actions", 1)[name] for name in columns} == original_action
        assert {name: row_facts(world.owned.connection, "recording_processing", 1)[name]
            for name in processing_columns} == original_processing
        state = world.owned.connection.execute(
            "SELECT check_state,media_json,repair_state,repair_basis_json,repair_error_json,repair_output_file_id"
            " FROM recording_processing WHERE id=1").fetchone()
        assert state[0] == 3 and parse_exact_json(state[1])["duration"]["seconds"] == Decimal("75.125")
        if branch == "decision":
            assert state[2] == 3 and parse_exact_json(state[3]) == inputs[0][0].basis.as_json()
        elif branch == "repair_failed":
            assert state[2] == 6 and parse_exact_json(state[4]) == inputs[0][0].error.as_json()
        elif branch == "repair_complete":
            assert state[2] == 5 and state[5] == inputs[0][0].output_file_id
            path = world.owned.connection.execute("SELECT relative_path FROM intermediate_files WHERE id=?",
                (state[5],)).fetchone()[0]
            assert (world.roots.staging / path).read_bytes() == b"original complete repaired bytes"
    finally:
        await world.close()
        world.owned.connection.close()


async def test_original_media_result_and_key_retained_across_runtimes(pipeline, capture_resources, monkeypatch):
    """全冷新工厂只读可靠媒体事实，不继承上次会话的保存集合。"""
    from ..support.bootstrap_consumers import consumer_world

    world = await consumer_world(pipeline, capture_resources)
    try:
        await run_recording_media(world.runtime.media, 1, 1, world.source_id)
        assert world.tools() == ["probe", "repair"] and len(world.reader.requests) == 1
        columns = business_columns("recording_processing")
        saved_processing = {name: row_facts(world.owned.connection, "recording_processing", 1)[name]
            for name in columns}
        entity_events = world.owned.connection.execute(
            "SELECT COUNT(*) FROM entity_event_links WHERE entity_type=1 AND entity_id=1").fetchone()[0]
        outputs = world.owned.connection.execute(
            "SELECT COUNT(*) FROM outputs WHERE source_action_id=1").fetchone()[0]
        await world.close()
        await world.reconnect()
        cold = await consumer_world((world.owned, world.roots, world.source_id), capture_resources)
        try:
            await run_recording_media(cold.runtime.media, 1, 1, cold.source_id)
            assert cold.tools() == ["probe", "repair"] and cold.reader.requests == []
            assert {name: row_facts(cold.owned.connection, "recording_processing", 1)[name]
                for name in columns} == saved_processing
            assert cold.owned.connection.execute(
                "SELECT COUNT(*) FROM entity_event_links WHERE entity_type=1 AND entity_id=1").fetchone()[0] == entity_events
            assert cold.owned.connection.execute(
                "SELECT COUNT(*) FROM outputs WHERE source_action_id=1").fetchone()[0] == outputs
        finally:
            await cold.close()
    finally:
        await world.close()
        world.owned.connection.close()


async def test_default_session_consumes_saved_recording_media(pipeline, capture_resources, monkeypatch):
    """flows=None 从实际保存的录像前置接手，完成 READ、媒体和报告收场。"""
    from types import SimpleNamespace
    from ..support.bootstrap_consumers import consumer_world
    from ..support.session import run_default_session
    from ..capture.test_capture_contract import _entry

    results = ResultsDouble({1: (replace(_entry("original-recording", size=len(_CONTENT)),
        locator={"path": "/DCIM/original.mp4"}, original_name="original.mp4"),)})
    world = await consumer_world(pipeline, capture_resources, results=results)
    actual_factory = capture_assembly.session_capture_assembly

    def factory(**options):
        return actual_factory(**options, wall_us=lambda: world.wall[0],
            monotonic_ns=lambda: 7_000_000_000, probe_request=world.probe_request,
            repair_request=world.repair_request)

    monkeypatch.setattr(capture_assembly, "session_capture_assembly", factory)
    monkeypatch.setattr(lifecycle, "SystemClock", lambda: SimpleNamespace(utc_micros=lambda: world.wall[0]))
    deps = lifecycle.build_runtime(CommandMode.RUN, world.config, catalog=_Catalog())
    try:
        assert deps.startup_error is None
        outcome = await run_default_session(deps, None, world.entry)
        assert outcome.succeeded, outcome
        assert world.owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (3,)
        assert world.owned.connection.execute("SELECT check_state,repair_state FROM recording_processing WHERE id=1").fetchone() == (3, 5)
        assert len(world.reader.requests) == 1 and world.tools() == ["probe", "repair"]
        assert results.calls == [1]
        assert world.owned.connection.execute("SELECT COUNT(*) FROM outputs WHERE source_action_id=1").fetchone()[0] >= 1
    finally:
        lifecycle.close_runtime(deps)
        await world.close()

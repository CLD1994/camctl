"""三个会话 flow 核实原 READ 保存；另验证默认装配与设备执行门禁。"""

import hashlib

import pytest

from camctl.contracts.enums import enum_for
from camctl.history.events import business_columns
from camctl.persistence.repositories.transactions import row_facts

from ..capture.media_retry_fixtures import media_pipeline as pipeline  # noqa: F401


pytestmark = pytest.mark.asyncio


def _snapshot(owned, table, row_id):
    facts = row_facts(owned.connection, table, row_id)
    return {name: facts[name] for name in business_columns(table)}


def _read_saved(owned, ticket, ends, finishes, verifications, stage):
    from camctl.persistence.repositories.read_end import load_read_end
    from ..capture.test_input_copy import _CONTENT

    inputs = {"finish": finishes, "end": ends, "verification": verifications}[stage]
    assert len(inputs) == 2 and inputs[1] == inputs[0]
    assert len(ends) == (2 if stage == "end" else 1)
    assert len(finishes) == (2 if stage == "finish" else 0)
    assert len(verifications) == (0 if stage == "end" else 2 if stage == "verification" else 1)
    if stage != "finish":
        end = load_read_end(int(ticket.target_id), owned)
        assert end[0] == ticket and end[1] == ends[0][0].end
        assert end[2:] == (ends[0][0].occurred_at, None)
    assert owned.connection.execute(
        "SELECT t.status,t.error_json,r.status,r.attempts_used FROM operation_attempts t"
        " JOIN operation_runs r ON r.id=t.run_id WHERE r.id=? AND t.attempt_no=?",
        (ticket.run_id, ticket.attempt_id)).fetchone() == (
            int(enum_for("operation_attempts.status").SUCCEEDED if stage == "finish" else enum_for("operation_attempts.status").RUNNING), None,
            int(enum_for("operation_runs.status").SUCCEEDED if stage == "finish" else enum_for("operation_runs.status").ACTIVE), ticket.attempt_id)
    assert owned.connection.execute("SELECT slot_device_id FROM file_copies WHERE id=?",
        (int(ticket.target_id),)).fetchone() == (None,)
    if stage != "end":
        digest = hashlib.sha256(_CONTENT).hexdigest()
        assert owned.connection.execute(
            "SELECT source_sha256,target_sha256,verification_state FROM file_copies WHERE id=?",
            (int(ticket.target_id),)).fetchone() == (digest, digest,
                int(enum_for("file_copies.verification_state").MATCHED))
    for _command, key in (ends[0], *finishes[:1], *verifications[:1]):
        assert owned.connection.execute("SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
            (str(key),)).fetchone() == (1,)


@pytest.mark.parametrize("entrance", ["normal", "residual", "restricted"])
@pytest.mark.parametrize("after_commit", [False, True])
@pytest.mark.parametrize("stage", ["finish", "verification", "end"])
async def test_flow_replays_original_read_save(pipeline, capture_resources, monkeypatch, entrance, after_commit, stage):
    from ..support.bootstrap_consumers import held_default_read

    world, ticket, ends, finishes, verifications = await held_default_read(pipeline, capture_resources, monkeypatch, stage, after_commit)
    owned = world.owned
    before_action = _snapshot(owned, "actions", 1)
    before_processing = _snapshot(owned, "recording_processing", 1)
    before_copy = _snapshot(owned, "file_copies", int(ticket.target_id))
    before_calls = tuple(world.driver.calls)
    try:
        await world.replay(entrance)
        _read_saved(owned, ticket, ends, finishes, verifications, stage)
        assert len(world.reader.requests) == 1 and tuple(world.driver.calls) == before_calls
        assert world.tools() == []
        assert _snapshot(owned, "actions", 1) == before_action
        assert _snapshot(owned, "recording_processing", 1) == before_processing
        after_copy = _snapshot(owned, "file_copies", int(ticket.target_id))
        assert {**after_copy, "slot_device_id": before_copy["slot_device_id"],
            "read_end_event_id": before_copy["read_end_event_id"],
            "source_sha256": before_copy["source_sha256"],
            "target_sha256": before_copy["target_sha256"],
            "verification_state": before_copy["verification_state"]} == before_copy
    finally:
        await world.close()
        owned.connection.close()


@pytest.mark.parametrize("entrance", ["normal", "residual", "restricted"])
async def test_flow_confirms_committed_verification_after_canceled_terminal(pipeline, capture_resources, monkeypatch, entrance):
    from ..support.bootstrap_consumers import held_default_read, cancel_original

    world, ticket, ends, finishes, verifications = await held_default_read(pipeline, capture_resources, monkeypatch, "verification", True)
    owned = world.owned
    try:
        await cancel_original(world)
        before = {table: _snapshot(owned, table, identity) for table, identity in (
            ("actions", 1), ("recording_processing", 1), ("file_copies", int(ticket.target_id)),
            ("operation_runs", ticket.run_id))}
        history_count = owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()
        before_calls = tuple(world.driver.calls)
        await world.replay(entrance)
        _read_saved(owned, ticket, ends, finishes, verifications, "verification")
        assert len(world.reader.requests) == 1 and tuple(world.driver.calls) == before_calls
        assert world.tools() == []
        assert before == {table: _snapshot(owned, table, identity) for table, identity in (
            ("actions", 1), ("recording_processing", 1), ("file_copies", int(ticket.target_id)),
            ("operation_runs", ticket.run_id))}
        assert owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone() == history_count
    finally:
        await world.close()
        owned.connection.close()


async def test_default_session_stops_on_unknown_verification_commit(pipeline, capture_resources, monkeypatch):
    """实际默认装配在校验保存回执未知时退出，不继续后续媒体工作。"""
    from dataclasses import replace
    from types import SimpleNamespace
    from camctl.acceptance.service import CommandMode
    from camctl.bootstrap import capture_assembly, lifecycle
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.outputs import OutputsRepository
    from camctl.persistence.repositories.transactions import saved_transaction_events
    from ..capture.media_retry_fixtures import _Catalog
    from ..capture.test_capture_contract import ResultsDouble, _entry
    from ..capture.test_input_copy import _CONTENT
    from ..support.bootstrap_consumers import TransactionFault, consumer_world
    from ..support.session import run_default_session

    results = ResultsDouble({1: (replace(_entry("original-recording", size=len(_CONTENT)),
        locator={"path": "/DCIM/original.mp4"}, original_name="original.mp4"),)})
    world = await consumer_world(pipeline, capture_resources, results=results)
    actual_factory = capture_assembly.session_capture_assembly
    actual_save = OutputsRepository.save_prepared
    saves = []

    def factory(**options):
        return actual_factory(**options, wall_us=lambda: world.wall[0],
            monotonic_ns=lambda: 7_000_000_000, probe_request=world.probe_request,
            repair_request=world.repair_request)

    def save(repository, request, key, current):
        saves.append((request, key, tuple(world.driver.calls), len(world.reader.requests), tuple(world.tools())))
        assert len(saves) == 1
        before = current.connection.execute(
            "SELECT read_end_event_id,source_sha256,slot_device_id FROM file_copies WHERE id=?",
            (request.copy_id,)).fetchone()
        assert before[0] is not None and before[1:] == (None, "cam-1")
        fault = TransactionFault(current.connection, after_commit=True)
        result = actual_save(repository, request, key, replace(current, connection=fault))
        assert result.kind is DbOutcomeKind.UNKNOWN and fault.commit_calls == 1
        return result

    monkeypatch.setattr(capture_assembly, "session_capture_assembly", factory)
    monkeypatch.setattr(lifecycle, "SystemClock", lambda: SimpleNamespace(utc_micros=lambda: world.wall[0]))
    monkeypatch.setattr(OutputsRepository, "save_prepared", save)
    deps = lifecycle.build_runtime(CommandMode.RUN, world.config, catalog=_Catalog())
    try:
        assert deps.startup_error is None
        outcome = await run_default_session(deps, None, world.entry)
        assert outcome.reason == "state_db_error", outcome
        assert len(saves) == 1
        request, key, calls, reads, tools = saves[0]
        assert reads == 1 and len(world.reader.requests) == reads
        assert tuple(world.driver.calls) == calls and tuple(world.tools()) == tools == ()
        assert saved_transaction_events(world.owned.connection, key) is not None
        digest = hashlib.sha256(_CONTENT).hexdigest()
        assert world.owned.connection.execute(
            "SELECT source_sha256,target_sha256,verification_state,slot_device_id FROM file_copies WHERE id=?",
            (request.copy_id,)).fetchone() == (digest, digest,
                int(enum_for("file_copies.verification_state").MATCHED), None)
        assert world.owned.connection.execute("SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
            (str(key),)).fetchone() == (1,)
    finally:
        lifecycle.close_runtime(deps)
        await world.close()


async def test_capture_flow_requires_original_verification_before_device_execution(pipeline, capture_resources, monkeypatch):
    """正式拍摄 flow 在保存失败时不调用设备工厂，重送后才允许进入。"""
    from types import SimpleNamespace
    from camctl.bootstrap.flows import capture_flow
    from camctl.capture.media_flow import resume_prepared_internal_reads
    from camctl.persistence.repositories.transactions import saved_transaction_events
    from camctl.session.service import StateDbFailure
    from ..support.bootstrap_consumers import StorageGate, held_default_read

    world, ticket, _ends, _finishes, verifications = await held_default_read(
        pipeline, capture_resources, monkeypatch, "verification", False)
    request, key = verifications[0]
    gate = StorageGate(table="file_copies", column="slot_device_id")
    gate.install(monkeypatch, key)
    calls = []

    async def reads(database):
        await resume_prepared_internal_reads(database, **{name: world.collections[name] for name in (
            "pending_read_results", "pending_read_business", "pending_read_ends",
            "continuing_read_tickets", "pending_segment_inspection_results")},
            occurred_at=lambda: world.wall[0])

    async def factory(database, device_id):
        assert saved_transaction_events(world.owned.connection, key) is not None
        calls.append(device_id)
        return None

    context = SimpleNamespace(database=world.database, clock=SimpleNamespace(utc_micros=lambda: world.wall[0]))
    try:
        flow = capture_flow(factory, resume_read_results=reads)
        with pytest.raises(StateDbFailure):
            await flow(context)
        assert gate.hits == 1 and calls == []
        gate.enabled = False
        flow = capture_flow(factory, resume_read_results=reads)
        await flow(context)
        await flow.settle()
        assert calls == ["cam-1"]
        assert verifications == [(request, key)] * 3
        assert len(world.reader.requests) == 1 and world.tools() == []
    finally:
        await world.close()
        world.owned.connection.close()

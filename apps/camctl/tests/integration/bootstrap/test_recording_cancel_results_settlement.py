"""录像拥有者完成取消时，原文件核实责任也必须结束。"""

from dataclasses import replace

import pytest

from camctl.capture.handlers import capture_handler
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.transactions import saved_transaction_events

from ..capture.media_retry_fixtures import media_pipeline as pipeline  # noqa: F401
from ..capture.test_input_copy import _CONTENT, _NOW
from ..capture.test_record_media_result_settlement import _attempts
from .test_read_default_consumers import _snapshot


pytestmark = pytest.mark.asyncio
_RUN_STATUS = enum_for("operation_runs.status")


def _original_results(owned):
    row = owned.connection.execute(
        "SELECT id FROM operation_runs WHERE responsibility_key='results/1'"
    ).fetchone()
    assert row is not None
    facts = _snapshot(owned, "operation_runs", row[0])
    assert (facts["action_id"], facts["activity_id"], facts["kind"]) == (1, 1, 7)
    return row[0], facts


@pytest.mark.parametrize("entrance", ["handler", "normal", "restricted"])
@pytest.mark.parametrize("held_decision", [False, True], ids=["no-holder", "uncommitted-holder"])
async def test_recording_cancel_owner_finishes_original_results_before_cancel_success(
        pipeline, capture_resources, monkeypatch, entrance, held_decision):
    from ..support.bootstrap_consumers import recording_world, apply_cancellation, TransactionFault

    world = await recording_world(pipeline, capture_resources)
    owned = world.owned
    inputs, outcomes = [], []
    try:
        run_id, first_run = _original_results(owned)
        assert (first_run["status"], first_run["attempts_used"], first_run["retry_wait_required"]) == (2, 1, 1)
        original_attempts = _attempts(owned)
        assert len(original_attempts) == 4
        assert world.results.calls == [1] and len(world.reader.requests) == len(world.ends) == 1
        assert world.tools() == []
        if held_decision:
            actual_save = CaptureRepository.finish_recording_results

            def save(repository, request, key, current):
                inputs.append((request, key))
                if len(inputs) == 1:
                    fault = TransactionFault(current.connection, after_commit=False)
                    receipt = actual_save(repository, request, key, replace(current, connection=fault))
                    assert fault.commit_calls == 1
                else:
                    receipt = actual_save(repository, request, key, current)
                outcomes.append(receipt)
                return receipt

            monkeypatch.setattr(CaptureRepository, "finish_recording_results", save)
            world.wall[0] = _NOW + 200_000_000
            with pytest.raises(ConsistencyError):
                await capture_handler("camera_record")(1, world.runtime)
            assert len(inputs) == 1 and outcomes[0].kind is DbOutcomeKind.UNKNOWN
            request, original_key = inputs[0]
            assert request.run_id == run_id and request.capture.occurred_at == world.wall[0]
            await world.reconnect()
            owned = world.owned
            assert saved_transaction_events(owned.connection, original_key) is None
            assert _attempts(owned) == original_attempts and _original_results(owned) == (run_id, first_run)
            assert world.tools() == ["probe"]
        before_run = _original_results(owned)[1]
        before = {table: owned.connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in ("recording_processing", "device_files", "file_copies", "intermediate_files")}
        history = owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()
        calls, tools = tuple(world.driver.calls), world.tools()
        assert any(name == "digest" for name, _ in calls)
        world.wall[0] = _NOW + 300_000_000
        origin = await apply_cancellation(world, terminal=False)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (origin,)).fetchone() == (2,)
        world.prime_clock()
        if entrance == "handler":
            runtime = await world.factory(world.database, "cam-1")
            await capture_handler("camera_record")(1, runtime)
        else:
            await world.replay(entrance, kind="round")
        assert owned.connection.execute("SELECT status,cancel_requested FROM actions WHERE id=1").fetchone() == (6, 1)
        if held_decision:
            assert inputs == [(request, original_key), (request, original_key)]
            assert outcomes[1].kind is DbOutcomeKind.COMPLETED and outcomes[1].value.disposition.value == "retired"
            assert saved_transaction_events(owned.connection, original_key) is None
        await world.replay("normal-cancel", kind="round")
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (origin,)).fetchone() == (3,)
        assert owned.connection.execute("SELECT status,outcome FROM cancel_items WHERE action_id=?", (origin,)).fetchall() == [(3, 1)]
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs WHERE source_action_id=1").fetchone() == (0,)
        assert _attempts(owned) == original_attempts
        assert owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()[:len(history)] == history
        for table, rows in before.items():
            assert owned.connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() == rows
        assert world.results.calls == [1] and len(world.reader.requests) == len(world.ends) == 1
        assert tuple(world.driver.calls) == calls and world.tools() == tools
        assert _original_results(owned) == (run_id, {**before_run, "status": int(_RUN_STATUS.CANCELED), "retry_wait_required": 0})
    finally:
        await world.close()

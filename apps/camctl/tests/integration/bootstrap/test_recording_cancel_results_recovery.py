"""录像取消的完整申请在 UNKNOWN 后由默认入口先核原事务。"""

from dataclasses import replace

import pytest

from camctl.capture.handlers import capture_handler
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository, FinishCanceledCapture
from camctl.persistence.transaction import saved_transaction_events

from ..capture.media_retry_fixtures import media_pipeline as pipeline  # noqa: F401
from ..capture.test_input_copy import _CONTENT, _NOW
from ..capture.test_record_media_result_settlement import _attempts
from .test_read_default_consumers import _snapshot
from .test_recording_cancel_results_settlement import _original_results


pytestmark = pytest.mark.asyncio


def _assert_joint_cancellation(owned, request, key, run_id, action_id):
    events = saved_transaction_events(owned.connection, key)
    assert events is not None and all(event["occurred_at"] == request.occurred_at for event in events)
    canceled_actions, canceled_runs = [], []
    for event in events:
        for row in event["body"]["rows"]:
            after = row["after"]["values"]
            if (row["table"] == "actions" and row["id"] == action_id
                    and after.get("status") == int(enum_for("actions.status").CANCELED)):
                canceled_actions.append(event)
            if (row["table"] == "operation_runs" and row["id"] == run_id
                    and after.get("status") == int(enum_for("operation_runs.status").CANCELED)):
                canceled_runs.append(event)
    assert len(canceled_actions) == len(canceled_runs) == 1
    assert canceled_actions[0]["transaction"].txn_id == canceled_runs[0]["transaction"].txn_id
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(key),)).fetchone() == (1,)
    return events


@pytest.mark.parametrize("entrance", [
    "normal", "residual", "restricted", "normal-cancel", "restricted-cancel",
])
@pytest.mark.parametrize("after_commit", [False, True])
async def test_default_entry_retries_original_recording_cancel_after_unknown(
        pipeline, capture_resources, monkeypatch, entrance, after_commit):
    from ..support.bootstrap_consumers import recording_world, apply_cancellation, TransactionFault

    world = await recording_world(pipeline, capture_resources)
    owned = world.owned
    run_id, before_run = _original_results(owned)
    assert (before_run["status"], before_run["attempts_used"], before_run["retry_wait_required"]) == (2, 1, 1)
    original_attempts = _attempts(owned)
    assert len(original_attempts) == 4
    assert all(end.stopped and end.error is None and end.bytes_read == len(_CONTENT) for end in world.ends)
    await apply_cancellation(world, terminal=False)
    before_rows = {table: owned.connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
        for table in ("recording_processing", "device_files", "file_copies", "intermediate_files")}
    before_history = owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()
    calls = tuple(world.driver.calls)
    path = owned.connection.execute("SELECT relative_path FROM intermediate_files WHERE purpose=?",
        (int(enum_for("intermediate_files.purpose").RECORDING_INPUT),)).fetchone()[0]
    target = world.roots.staging / path
    assert target.read_bytes() == _CONTENT
    actual_save = CaptureRepository.finish_canceled_capture
    inputs, receipts = [], []

    def save(repository, request, key, current):
        assert isinstance(request, FinishCanceledCapture)
        inputs.append((request, key))
        if len(inputs) == 1:
            fault = TransactionFault(current.connection, after_commit=after_commit)
            receipt = actual_save(repository, request, key, replace(current, connection=fault))
            assert receipt.kind is DbOutcomeKind.UNKNOWN and fault.commit_calls == 1
            assert current.connection.in_transaction is not after_commit
        else:
            receipt = actual_save(repository, request, key, current)
        receipts.append(receipt)
        return receipt

    monkeypatch.setattr(CaptureRepository, "finish_canceled_capture", save)
    try:
        world.wall[0] = _NOW + 200_000_000
        with pytest.raises(ConsistencyError):
            await capture_handler("camera_record")(1, world.runtime)
        assert len(inputs) == len(receipts) == 1
        request, key = inputs[0]
        assert request.action_id == 1 and request.occurred_at == world.wall[0]
        assert not request.unstarted and not request.drafts and request.catalog_facts is None
        await world.reconnect()
        owned = world.owned
        assert (saved_transaction_events(owned.connection, key) is not None) is after_commit
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (6 if after_commit else 2,)
        expected_run = {**before_run, "status": 5, "retry_wait_required": 0}
        assert _snapshot(owned, "operation_runs", run_id) == (expected_run if after_commit else before_run)
        first_events = _assert_joint_cancellation(owned, request, key, run_id, 1) if after_commit else None
        world.wall[0] = _NOW + 300_000_000
        world.prime_clock()
        await world.replay(entrance, kind="completion")
        assert inputs == [(request, key), (request, key)]
        assert receipts[1].kind is DbOutcomeKind.COMPLETED, receipts[1].error
        assert _snapshot(owned, "operation_runs", run_id) == expected_run
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (6,)
        events = _assert_joint_cancellation(owned, request, key, run_id, 1)
        if first_events is not None:
            assert events == first_events
        assert _attempts(owned) == original_attempts
        for table, rows in before_rows.items():
            assert owned.connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() == rows
        assert owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()[:len(before_history)] == before_history
        assert target.read_bytes() == _CONTENT and tuple(world.driver.calls) == calls
        assert world.results.calls == [1] and world.tools() == [] and len(world.reader.requests) == 1
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs WHERE source_action_id=1").fetchone() == (0,)
    finally:
        await world.close()

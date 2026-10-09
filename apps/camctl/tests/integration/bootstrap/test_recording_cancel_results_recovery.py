"""录像取消的完整申请在 UNKNOWN 后由默认入口先核原事务。"""

from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.bootstrap import lifecycle
from camctl.capture.handlers import capture_handler
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository, FinishCanceledCapture
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import saved_transaction_events

from ..capture.media_retry_fixtures import media_pipeline as pipeline  # noqa: F401
from ..capture.test_capture_contract import ResultsDouble, _entry
from ..capture.test_input_copy import _CONTENT, _NOW
from ..capture.test_record_media_result_settlement import _TrackedCommitFailure, _WaitingProbe, _attempts
from .test_read_default_consumers import _CandidateClock, _default_world, _snapshot
from .test_recording_cancel_results_settlement import _original_results
from .test_recording_results_cancellation import _apply_cancellation, _BeforeCandidates, _CandidateBoundary


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
        pipeline, monkeypatch, entrance, after_commit):
    owned, roots, _source_id = pipeline
    world = await _default_world(pipeline, monkeypatch)
    deps, factories, context, reader, driver, ends, wall, factory_calls = world
    reopened = None
    try:
        runtime = factories[0](owned, "cam-1")
        runtime.results = ResultsDouble({1: (replace(
            _entry("original-recording", size=len(_CONTENT)),
            locator={"path": "/DCIM/original.mp4"}, original_name="original.mp4"),)})
        tools = _WaitingProbe(Decimal("61"))
        runtime.media.tools = tools
        await capture_handler("camera_record")(1, runtime)
        run_id, before_run = _original_results(owned)
        assert (before_run["status"], before_run["attempts_used"], before_run["retry_wait_required"]) == (
            int(enum_for("operation_runs.status").ACTIVE), 1, 1)
        assert not runtime.pending_start_results and not runtime.pending_recording_results
        assert runtime.results.calls == [1] and tools.calls == ["probe"]
        assert len(reader.requests) == len(ends) == 1
        assert all(end.stopped is True and end.error is None and end.bytes_read == len(_CONTENT) for end in ends)
        original_attempts = _attempts(owned)
        assert len(original_attempts) == 4
        _apply_cancellation(owned, terminal=False)
        action_id = before_run["action_id"]
        assert owned.connection.execute("SELECT status,cancel_requested FROM actions WHERE id=?", (action_id,)).fetchone() == (
            int(enum_for("actions.status").RUNNING), 1)
        before_rows = {table: owned.connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in ("recording_processing", "device_files", "file_copies", "intermediate_files")}
        before_history = owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()
        before_driver, before_tool, before_queries = tuple(driver.calls), tuple(tools.calls), tuple(runtime.results.calls)
        input_file = owned.connection.execute(
            "SELECT relative_path FROM intermediate_files WHERE purpose=?",
            (int(enum_for("intermediate_files.purpose").RECORDING_INPUT),)).fetchone()[0]
        target_path = roots.staging / input_file
        assert target_path.read_bytes() == _CONTENT
        actual_save = CaptureRepository.finish_canceled_capture
        inputs, receipts, faults = [], [], []

        def save(repository, request, key, current):
            pending = runtime.pending_recording_results[action_id]
            assert (pending.request, pending.key) == (request, key)
            assert isinstance(request, FinishCanceledCapture)
            inputs.append((request, key))
            if len(inputs) == 1:
                fault = _TrackedCommitFailure(current.connection, after_commit)
                faults.append(fault)
                receipt = actual_save(repository, request, key, replace(current, connection=fault))
                assert receipt.kind is DbOutcomeKind.UNKNOWN and fault.commit_calls == 1
                assert current.connection.in_transaction is not after_commit
            else:
                receipt = actual_save(repository, request, key, current)
            receipts.append(receipt)
            return receipt

        monkeypatch.setattr(CaptureRepository, "finish_canceled_capture", save)
        wall[0] = _NOW + 200_000_000
        with pytest.raises(ConsistencyError):
            await capture_handler("camera_record")(action_id, runtime)
        assert len(inputs) == len(receipts) == len(faults) == 1
        request, key = inputs[0]
        assert request.action_id == action_id and request.occurred_at == wall[0]
        assert request.unstarted is False and not request.drafts and request.catalog_facts is None
        original_holder = runtime.pending_recording_results[action_id]
        assert (original_holder.request, original_holder.key) == (request, key)
        owned.connection.close()
        reopened = open_existing(roots.staging.parent / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
        assert reopened.metadata == owned.metadata
        assert (saved_transaction_events(reopened.connection, key) is not None) is after_commit
        assert reopened.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (
            int(enum_for("actions.status").CANCELED) if after_commit else int(enum_for("actions.status").RUNNING),)
        expected_run = ({**before_run, "status": int(enum_for("operation_runs.status").CANCELED),
            "retry_wait_required": 0} if after_commit else before_run)
        assert _snapshot(reopened, "operation_runs", run_id) == expected_run
        first_events = _assert_joint_cancellation(reopened, request, key, run_id, action_id) if after_commit else None

        def preserved():
            assert _attempts(reopened) == original_attempts
            for table, rows in before_rows.items():
                assert reopened.connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() == rows
            assert reopened.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()[:len(before_history)] == before_history
            assert target_path.read_bytes() == _CONTENT
            assert tuple(driver.calls) == before_driver and tuple(tools.calls) == before_tool
            assert tuple(runtime.results.calls) == before_queries
            assert len(reader.requests) == len(ends) == len(factory_calls) == 1
            assert reopened.connection.execute("SELECT COUNT(*) FROM outputs WHERE source_action_id=?", (action_id,)).fetchone() == (0,)

        preserved()
        original_open, opened = context.open_connection, []

        def saved():
            assert inputs == [(request, key), (request, key)]
            assert receipts[1].kind is DbOutcomeKind.COMPLETED, receipts[1].error
            assert not runtime.pending_recording_results
            assert reopened.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (
                int(enum_for("actions.status").CANCELED),)
            assert _snapshot(reopened, "operation_runs", run_id) == {
                **before_run, "status": int(enum_for("operation_runs.status").CANCELED), "retry_wait_required": 0}
            events = _assert_joint_cancellation(reopened, request, key, run_id, action_id)
            if first_events is not None:
                assert events == first_events
            preserved()

        def open_connection():
            current = original_open()
            assert current.metadata == reopened.metadata and current.connection is not reopened.connection
            opened.append(current)
            return replace(current, connection=_CandidateBoundary(current.connection, saved))

        context.open_connection = open_connection
        context.clock = _CandidateClock(saved)
        wall[0] = _NOW + 300_000_000
        selected = {
            "normal": context.flows["scheduling"],
            "residual": context.flows["residual"],
            "restricted": context.restricted_flows["winddown"],
            "normal-cancel": context.flows["cancel"],
            "restricted-cancel": context.restricted_flows["cancel"],
        }[entrance]
        # normal 在候选钟抛同一公开保存边界异常；restricted 在真正候选查询前停止。
        from .test_read_default_consumers import _BeforeBusinessCandidates
        with pytest.raises((_BeforeCandidates, _BeforeBusinessCandidates)):
            await selected(context)
        assert len(opened) == 1
        reopened.connection.close()
        reopened = open_existing(roots.staging.parent / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
        saved()
    finally:
        if reopened is not None:
            reopened.connection.close()
        lifecycle.close_runtime(deps)

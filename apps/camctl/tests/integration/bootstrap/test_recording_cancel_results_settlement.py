"""录像拥有者完成取消时，原文件核实责任也必须结束。"""

from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.bootstrap import lifecycle
from camctl.capture.handlers import capture_handler
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import saved_transaction_events

from ..capture.media_retry_fixtures import media_pipeline as pipeline  # noqa: F401
from ..capture.test_capture_contract import ResultsDouble, _entry
from ..capture.test_input_copy import _CONTENT, _NOW
from ..capture.test_record_media_result_settlement import _TrackedCommitFailure, _WaitingProbe, _attempts
from .test_read_default_consumers import _default_world, _snapshot
from .test_recording_results_cancellation import _apply_cancellation


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
        pipeline, monkeypatch, entrance, held_decision):
    owned, roots, _source_id = pipeline
    deps, factories, context, reader, driver, ends, wall, _factory_calls = await _default_world(
        pipeline, monkeypatch)
    reopened = None
    listed = []
    actual_list = ResultsDouble.list_files

    async def list_files(results, action_id):
        listed.append(action_id)
        return await actual_list(results, action_id)

    monkeypatch.setattr(ResultsDouble, "list_files", list_files)
    try:
        runtime = factories[0](owned, "cam-1")
        runtime.results = ResultsDouble({1: (replace(
            _entry("original-recording", size=len(_CONTENT)),
            locator={"path": "/DCIM/original.mp4"}, original_name="original.mp4"),)})
        tools = _WaitingProbe(Decimal("61"))
        runtime.media.tools = tools
        await capture_handler("camera_record")(1, runtime)

        run_id, first_run = _original_results(owned)
        assert (first_run["status"], first_run["attempts_used"],
                first_run["retry_wait_required"]) == (int(_RUN_STATUS.ACTIVE), 1, 1)
        original_attempts = _attempts(owned)
        assert len(original_attempts) == 4  # 已结束的 START、STOP、RESULTS 和实际 READ。
        assert not runtime.pending_start_results
        assert not runtime.pending_recording_results
        assert listed == [1] and len(reader.requests) == len(ends) == 1
        assert tools.calls == ["probe"]

        inputs, outcomes = [], []
        original_key = None
        if held_decision:
            actual_save = CaptureRepository.finish_recording_results

            def save(repository, request, key, current):
                inputs.append((request, key))
                if len(inputs) == 1:
                    fault = _TrackedCommitFailure(current.connection, False)
                    receipt = actual_save(repository, request, key,
                        replace(current, connection=fault))
                    assert fault.commit_calls == 1
                else:
                    receipt = actual_save(repository, request, key, current)
                outcomes.append(receipt)
                return receipt

            monkeypatch.setattr(CaptureRepository, "finish_recording_results", save)
            wall[0] = _NOW + 200_000_000
            with pytest.raises(ConsistencyError):
                await capture_handler("camera_record")(1, runtime)
            assert len(inputs) == 1 and outcomes[0].kind is DbOutcomeKind.UNKNOWN
            request, original_key = inputs[0]
            assert request.run_id == run_id and request.capture.occurred_at == wall[0]
            held = runtime.pending_recording_results[1]
            assert (held.request, held.key) == (request, original_key)
            assert owned.connection.in_transaction
            owned.connection.close()
            reopened = open_existing(roots.staging.parent / "state.db",
                DbOpenMode.EXISTING_RW, DbConfig())
            assert reopened.metadata == owned.metadata
            assert saved_transaction_events(reopened.connection, original_key) is None
            owned = reopened
            assert _attempts(owned) == original_attempts
            assert _original_results(owned) == (run_id, first_run)
            assert tools.calls == ["probe", "probe"]

        before_run = _original_results(owned)[1]
        before_media = owned.connection.execute("SELECT * FROM recording_processing ORDER BY id").fetchall()
        before_files = owned.connection.execute("SELECT * FROM device_files ORDER BY id").fetchall()
        before_copies = owned.connection.execute("SELECT * FROM file_copies ORDER BY id").fetchall()
        before_host_files = owned.connection.execute("SELECT * FROM intermediate_files ORDER BY id").fetchall()
        before_history = owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()
        before_driver_calls, before_tools = tuple(driver.calls), tuple(tools.calls)
        assert any(name == "digest" for name, _operation in before_driver_calls)
        wall[0] = _NOW + 300_000_000
        _apply_cancellation(owned, terminal=False)
        origin = owned.connection.execute("SELECT id FROM actions WHERE type=6").fetchone()[0]
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (origin,)).fetchone() == (2,)

        if entrance == "handler":
            if reopened is not None:
                runtime = factories[0](owned, "cam-1")
            await capture_handler("camera_record")(1, runtime)
        elif entrance == "normal":
            await context.flows["scheduling"](context)
        else:
            await context.restricted_flows["winddown"](context)

        assert owned.connection.execute("SELECT status,cancel_requested FROM actions WHERE id=1").fetchone() == (6, 1)
        assert not deps.capture_recording_results
        if held_decision:
            assert inputs == [(request, original_key), (request, original_key)]
            assert outcomes[1].kind is DbOutcomeKind.COMPLETED, outcomes[1].error
            assert outcomes[1].value.disposition.value == "retired"
            assert saved_transaction_events(owned.connection, original_key) is None
        await context.flows["cancel"](context)

        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (origin,)).fetchone() == (3,)
        assert owned.connection.execute(
            "SELECT status,outcome FROM cancel_items WHERE action_id=?", (origin,)
        ).fetchall() == [(3, 1)]
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs WHERE source_action_id=1").fetchone() == (0,)
        assert _attempts(owned) == original_attempts
        assert owned.connection.execute(
            "SELECT * FROM history_events WHERE id<=? ORDER BY id", (before_history[-1][0],)
        ).fetchall() == before_history
        assert owned.connection.execute("SELECT * FROM recording_processing ORDER BY id").fetchall() == before_media
        assert owned.connection.execute("SELECT * FROM device_files ORDER BY id").fetchall() == before_files
        assert owned.connection.execute("SELECT * FROM file_copies ORDER BY id").fetchall() == before_copies
        assert owned.connection.execute("SELECT * FROM intermediate_files ORDER BY id").fetchall() == before_host_files
        assert listed == [1] and len(reader.requests) == len(ends) == 1
        assert tuple(driver.calls) == before_driver_calls and tuple(tools.calls) == before_tools
        expected = {**before_run, "status": int(_RUN_STATUS.CANCELED), "retry_wait_required": 0}
        assert _original_results(owned) == (run_id, expected), (
            "录像拥有者已保存 CANCELED，取消请求及成员已成功，原 RESULTS 必须同时结束")
    finally:
        if reopened is not None:
            reopened.connection.close()
        lifecycle.close_runtime(deps)

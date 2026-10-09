"""动作取消历史已保存后，默认入口独立发现未结束的录像核实用途。"""

from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.bootstrap import lifecycle
from camctl.capture.handlers import capture_handler
from camctl.contracts.values import new_operation_key
from camctl.devices.evidence import DeviceObservation
from camctl.operations.attempts import AttemptFinish, AttemptIntent, AttemptTarget, OperationKind
from camctl.operations.models import AttemptStatus, CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis
from camctl.operations.validation import validate_outcome
from camctl.persistence.repositories.capture import FinishCanceledCapture, FinishCaptureCommand
from camctl.persistence.transaction import commit_operation

from ..capture.media_retry_fixtures import media_pipeline as pipeline  # noqa: F401
from ..capture.test_capture_contract import ResultsDouble, _entry
from ..capture.test_input_copy import _CONTENT, _NOW
from ..capture.test_record_media_result_settlement import _WaitingProbe, _attempts
from .test_read_default_consumers import _default_world, _snapshot
from .test_recording_cancel_results_settlement import _original_results
from .test_recording_results_cancellation import _apply_cancellation, _BeforeCandidates, _CandidateBoundary


pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("entrance", [
    "normal", "residual", "restricted", "normal-cancel", "restricted-cancel",
])
@pytest.mark.parametrize("late_result", [False, True], ids=["cancel-is-latest", "later-owned-result"])
async def test_default_discovers_canceled_recording_results_without_session_holder(
        pipeline, monkeypatch, entrance, late_result):
    owned, _roots, _source_id = pipeline
    deps, factories, context, reader, driver, ends, _wall, factory_calls = await _default_world(
        pipeline, monkeypatch)
    try:
        runtime = factories[0](owned, "cam-1")
        runtime.results = ResultsDouble({1: (replace(
            _entry("original-recording", size=len(_CONTENT)),
            locator={"path": "/DCIM/original.mp4"}, original_name="original.mp4"),)})
        tools = _WaitingProbe(Decimal("61"))
        runtime.media.tools = tools
        await capture_handler("camera_record")(1, runtime)
        run_id, before_run = _original_results(owned)
        assert (before_run["status"], before_run["retry_wait_required"]) == (2, 1)
        if late_result:
            started = runtime.operations.begin_attempt(AttemptIntent(
                operation="result", action_id=1, kind=OperationKind.CHECK_CAPTURE_RESULTS,
                target=AttemptTarget(activity_id=1), query_purpose=None,
                config=runtime.check_config, occurred_at=_NOW + 50), new_operation_key(), owned)
            assert started.kind.value == "completed", started.error
            assert started.value.disposition.value == "granted"
        _apply_cancellation(owned, terminal=False)
        # 公共历史内核保存独立的动作取消事实；用途收尾拥有自己的责任。
        key = new_operation_key()
        receipt = commit_operation(FinishCaptureCommand(
            FinishCanceledCapture(1, _NOW + 100), key, canceled=True), key, owned)
        assert receipt.kind == "completed", receipt.error
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (6,)
        if late_result:
            ticket = started.value.ticket
            actual = validate_outcome(ticket, CallOutcome(
                status=AttemptStatus.SUCCEEDED, effect=EffectState.UNKNOWN,
                settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("results_returned", 1, {})),
                observations=(DeviceObservation("result_files_listed", 1,
                    {"activity_id": "1", "entries": []}),)), runtime.evidence)
            saved = runtime.operations.finish_attempt(AttemptFinish(ticket, actual, _NOW + 200),
                new_operation_key(), owned)
            assert saved.kind.value == "completed", saved.error
            assert owned.connection.execute(
                "SELECT e.event_type FROM actions a JOIN history_events e ON e.id=a.last_event_id"
                " WHERE a.id=1").fetchone() == (12,)
            _same_id, before_run = _original_results(owned)
        assert not deps.capture_recording_results
        assert _original_results(owned) == (run_id, before_run)
        attempts = _attempts(owned)
        history = owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()
        action = _snapshot(owned, "actions", 1)
        media = owned.connection.execute("SELECT * FROM recording_processing ORDER BY id").fetchall()
        files = owned.connection.execute("SELECT * FROM device_files ORDER BY id").fetchall()
        calls = tuple(driver.calls), tuple(tools.calls), tuple(factory_calls)
        owned.connection.close()

        def checked():
            observer = context.open_connection_original()
            try:
                assert _original_results(observer) == (
                    run_id, {**before_run, "status": 5, "retry_wait_required": 0})
                assert _snapshot(observer, "actions", 1) == action
                assert _attempts(observer) == attempts
                assert observer.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()[:len(history)] == history
                assert observer.connection.execute("SELECT * FROM recording_processing ORDER BY id").fetchall() == media
                assert observer.connection.execute("SELECT * FROM device_files ORDER BY id").fetchall() == files
                assert not deps.capture_recording_results
                assert (tuple(driver.calls), tuple(tools.calls), tuple(factory_calls)) == calls
                assert len(reader.requests) == len(ends) == 1
            finally:
                observer.connection.close()

        context.open_connection_original = context.open_connection

        def open_connection():
            current = context.open_connection_original()
            return replace(current, connection=_CandidateBoundary(current.connection, checked))

        context.open_connection = open_connection
        selected = (context.flows["scheduling"] if entrance == "normal" else
            context.flows["residual"] if entrance == "residual" else
            context.restricted_flows["winddown"] if entrance == "restricted" else
            context.flows["cancel"] if entrance == "normal-cancel" else
            context.restricted_flows["cancel"])
        with pytest.raises(_BeforeCandidates):
            await selected(context)
        checked()
    finally:
        owned.connection.close()
        lifecycle.close_runtime(deps)

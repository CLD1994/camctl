"""录像核实用途取消后，取消汇总仍等待原调用结果可靠保存。"""

import pytest

from camctl.cancellation.settlement import TargetSettlement
from camctl.contracts.values import new_operation_key
from camctl.operations.attempts import AttemptFinish, AttemptIntent, AttemptTarget, OperationKind
from camctl.operations.models import AttemptStatus, CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis
from camctl.operations.validation import validate_outcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository, FinishCanceledCapture, FinishCaptureCommand
from camctl.persistence.transaction import commit_operation

from ..capture.media_retry_fixtures import media_pipeline as pipeline  # noqa: F401
from ..capture.test_input_copy import _NOW
from .test_read_default_consumers import _default_world
from .test_recording_results_cancellation import _apply_cancellation


pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("closed_purpose", [False, True])
async def test_cancel_settlement_waits_for_actual_recording_result_save(
        pipeline, monkeypatch, closed_purpose):
    owned, _roots, _source_id = pipeline
    deps, factories, _context, _reader, _driver, _ends, _wall, _calls = await _default_world(
        pipeline, monkeypatch)
    try:
        runtime = factories[0](owned, "cam-1")
        allocated = runtime.operations.begin_attempt(AttemptIntent(
            operation="result", action_id=1, kind=OperationKind.CHECK_CAPTURE_RESULTS,
            target=AttemptTarget(activity_id=1), query_purpose=None,
            config=runtime.check_config, occurred_at=_NOW + 1), new_operation_key(), owned)
        assert allocated.kind is DbOutcomeKind.COMPLETED, allocated.error
        assert allocated.value.disposition.value == "granted"
        ticket = allocated.value.ticket
        _apply_cancellation(owned, terminal=False)
        request, key = FinishCanceledCapture(1, _NOW + 100), new_operation_key()
        if closed_purpose:
            receipt = CaptureRepository().finish_canceled_capture(request, key, owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        else:
            receipt = commit_operation(FinishCaptureCommand(request, key, canceled=True), key, owned)
            assert receipt.kind == "completed", receipt.error
        attempts = owned.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall()
        settlement = TargetSettlement(owned, None, None, lambda _identity: "absent", lambda: _NOW + 200)
        assert (await settlement.settle(1)).complete is False
        assert owned.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall() == attempts
        actual = validate_outcome(ticket, CallOutcome(
            status=AttemptStatus.SUCCEEDED, effect=EffectState.UNKNOWN,
            settlement=Settlement(SettlementBasis.OBSERVED,
                EvidenceValue("results_returned", 1, {}))), runtime.evidence)
        saved = runtime.operations.finish_attempt(AttemptFinish(ticket, actual, _NOW + 300),
            new_operation_key(), owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        if not closed_purpose:
            assert (await settlement.settle(1)).complete is False
            ended = CaptureRepository().finish_canceled_capture(
                FinishCanceledCapture(1, _NOW + 100), new_operation_key(), owned)
            assert ended.kind is DbOutcomeKind.COMPLETED, ended.error
        assert (await settlement.settle(1)).complete is True
    finally:
        from camctl.bootstrap.lifecycle import close_runtime
        close_runtime(deps)

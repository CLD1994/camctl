"""录像用途取消后，公开取消收场仍等待原调用的实际结果可靠保存。"""

import pytest

from camctl.cancellation.settlement import TargetSettlement
from camctl.contracts.values import new_operation_key
from camctl.operations.attempts import AttemptFinish, AttemptIntent, AttemptTarget, OperationKind
from camctl.operations.models import AttemptStatus, CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis
from camctl.operations.validation import validate_outcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository, FinishCanceledCapture
from camctl.persistence.repositories.cancellation import CancellationRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import OutputsRepository

from ..capture.media_retry_fixtures import media_pipeline as pipeline  # noqa: F401
from ..capture.test_input_copy import _NOW
from ..support.bootstrap_consumers import consumer_world, apply_cancellation, save_legacy_capture_cancel

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("closed_purpose", [False, True])
async def test_cancel_settlement_waits_for_actual_recording_result_save(
        pipeline, capture_resources, closed_purpose):
    world = await consumer_world(pipeline, capture_resources)
    operations = OperationRepository()
    try:
        key = new_operation_key()
        allocated = await world.database.write(lambda owned: operations.begin_attempt(AttemptIntent(
            operation="result", action_id=1, kind=OperationKind.CHECK_CAPTURE_RESULTS,
            target=AttemptTarget(activity_id=1), query_purpose=None,
            config=world.runtime.check_config, occurred_at=_NOW + 1), key, owned), key=key)
        assert allocated.kind is DbOutcomeKind.COMPLETED, allocated.error
        assert allocated.value.disposition.value == "granted"
        ticket = allocated.value.ticket
        await apply_cancellation(world, terminal=False)
        request, key = FinishCanceledCapture(1, _NOW + 100), new_operation_key()
        if closed_purpose:
            receipt = await world.database.write(lambda owned: CaptureRepository().finish_canceled_capture(
                request, key, owned), key=key)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        else:
            save_legacy_capture_cancel(world, occurred_at=_NOW + 100)
        attempts = world.owned.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall()

        async def absent(_identity):
            return "absent"

        settlement = TargetSettlement(world.database, OutputsRepository(), CancellationRepository(), absent,
            lambda: _NOW + 200)
        assert (await settlement.settle(1)).complete is False
        assert world.owned.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall() == attempts
        actual = validate_outcome(ticket, CallOutcome(
            status=AttemptStatus.SUCCEEDED, effect=EffectState.UNKNOWN,
            settlement=Settlement(SettlementBasis.OBSERVED,
                EvidenceValue("results_returned", 1, {}))), world.entry.evidence)
        key = new_operation_key()
        saved = await world.database.write(lambda owned: operations.finish_attempt(
            AttemptFinish(ticket, actual, _NOW + 300), key, owned), key=key)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        if not closed_purpose:
            assert (await settlement.settle(1)).complete is False
            key = new_operation_key()
            ended = await world.database.write(lambda owned: CaptureRepository().finish_canceled_capture(
                FinishCanceledCapture(1, _NOW + 100), key, owned), key=key)
            assert ended.kind is DbOutcomeKind.COMPLETED, ended.error
        assert (await settlement.settle(1)).complete is True
    finally:
        await world.close()

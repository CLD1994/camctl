"""残留首停在排队后复核资格，续停保留已经形成的收场责任。"""

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import pytest

from camctl.capture.residual import ResidualCandidate, _stop_residual
from camctl.operations.attempts import AttemptConfig, BeginAttemptResult, BeginDisposition, RunOutcome
from camctl.operations.models import AttemptTicket, SettlementBasis, EffectState
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.scheduling.rules import LaunchWindow

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("winddown", [False, True])
async def test_binding_failure_waiting_for_running_call_does_not_report_progress(mocker, winddown):
    from camctl.capture.residual import pass_residual_gate, _advance_winddown

    candidate = ResidualCandidate(7, 20, 1, 1)
    trigger = {"id": 21, "device_id": "camera", "status": 2, "cancel_requested": 0}
    database = create_autospec(BusinessDatabase, instance=True)
    runtime = SimpleNamespace(database=database, wall_us=lambda: 150,
        window_of=lambda _: LaunchWindow(100, 200),
        action=AsyncMock(return_value=trigger), capture=object())
    details = {"reason": "original_binding_unavailable"}
    if winddown:
        database.read.side_effect = [(21, 7, 2, 1), (3, 3), ("followup/21/7",), (1,)]
        mocker.patch("camctl.capture.residual._original_binding_failure", autospec=True, return_value=details)
    else:
        database.read.side_effect = [(candidate,), (30, 21, 2), ("followup/21/7",), (1,)]
        mocker.patch("camctl.capture.residual._original_binding_failure", autospec=True,
                     side_effect=[None, details])
        mocker.patch("camctl.capture.software_recovery.pass_software_recovery_gate", autospec=True,
                     return_value=None)
    notices = []
    if winddown:
        await _advance_winddown(runtime, candidate, (30, 21, 2), on_progress=lambda: notices.append(True))
    else:
        assert await pass_residual_gate(runtime, trigger, on_progress=lambda: notices.append(True)) is False
    assert notices == []
    database.write.assert_not_awaited()


@pytest.mark.parametrize("stage", ["intent", "facts"])
@pytest.mark.parametrize("change", ["canceled", "terminal", "expired"])
@pytest.mark.parametrize("already_dispatched", [False, True])
async def test_first_stop_rechecks_queued_qualification_and_existing_stop_continues(
    mocker, stage, change, already_dispatched,
):
    database = create_autospec(BusinessDatabase, instance=True)
    trigger = {"id": 21, "status": 2, "cancel_requested": 0}
    owner = {"id": 20, "device_id": "camera", "driver_id": "dji-action6", "effective_params_json": {}}
    now = [150]
    entered, release = asyncio.Event(), asyncio.Event()
    ticket = AttemptTicket(2 if already_dispatched else 1, "control", "7", "followup/21/7", 30)
    outcome = object()
    candidate = ResidualCandidate(7, 20, 1, 1)
    runtime = SimpleNamespace(database=database,
        operations=create_autospec(OperationRepository, instance=True),
        action=AsyncMock(side_effect=lambda identity: dict(owner if identity == 20 else trigger)), wall_us=lambda: now[0],
        window_of=lambda _: LaunchWindow(100, 200),
        residual_config=AttemptConfig(3, Decimal("1"), Decimal("1")),
        stopper=SimpleNamespace(stop=AsyncMock(return_value=object())), finish=AsyncMock())
    reads = 0

    async def read(callback, **kwargs):
        nonlocal reads
        reads += 1
        if reads == 1:
            return (30, 2, 1, 0) if already_dispatched else None
        if stage == "facts":
            entered.set()
            await release.wait()
        return owner, dict(trigger)

    async def write(callback, **kwargs):
        if stage == "intent":
            entered.set()
            await release.wait()
        return DbOutcome(DbOutcomeKind.COMPLETED, BeginAttemptResult(BeginDisposition.GRANTED, ticket))

    database.read.side_effect = read
    database.write.side_effect = write
    mocker.patch("camctl.capture.handlers._operation_outcome", return_value=(outcome, False))
    task = asyncio.create_task(_stop_residual(runtime, 21, candidate))
    await entered.wait()
    if change == "canceled":
        trigger["cancel_requested"] = 1
    elif change == "terminal":
        trigger["status"] = 3
    else:
        now[0] = 201
    release.set()
    await task

    if already_dispatched:
        runtime.stopper.stop.assert_awaited_once()
        runtime.finish.assert_awaited_once_with(ticket, outcome, retry_wait=True)
    else:
        runtime.stopper.stop.assert_not_awaited()
        saved = runtime.finish.call_args.args[1]
        assert saved.effect is EffectState.NO_EFFECT
        assert saved.settlement.basis is SettlementBasis.NOT_DISPATCHED
        assert runtime.finish.call_args.kwargs["end_run"] is (
            RunOutcome.EXPIRED if change == "expired" else RunOutcome.CANCELED)

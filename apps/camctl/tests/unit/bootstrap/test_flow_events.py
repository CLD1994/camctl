"""业务流程仅为真实报告任务和首次保存的取消阶段记录 INFO。"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import pytest

from camctl.bootstrap.flows import cancel_flow
from camctl.cancellation.models import (
    CancelActionDisposition, CancelActionFinished, CancelItemProgress, CancelProgress,
    CancelStartDisposition, CancelStartResult, CancelTargetsDisposition, CancelTargetsSaved,
    FixCancelTargets, FixedCancelSet, TargetResolution,
)
from camctl.cancellation.rules import DispatchPhase, EligibilityFacts, LoadedEligibility
from camctl.devices.start_entry import StartEntryCoordinator
from camctl.logging_runtime.events import event_logging
from camctl.logging_runtime.service import LogChannel
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.cancellation import CancellationRepository
from camctl.reporting.policy import ReportingRepository, ReportDecisionKind
from camctl.reporting.publication import ReportFileIdentity, ReportLocations, Sighting
from camctl.reporting.supervisor import GenerationOutcomeKind, WorkerGeneration
from camctl.session.service import StateDbFailure, _drive_flows

from test_report_background_progress import _ReportState

pytestmark = pytest.mark.asyncio


def _records(channel):
    return [json.loads(call.args[0].rendered_message()) for call in channel.slog.call_args_list]


async def test_report_records_generation_and_publication_with_the_same_job(mocker):
    state = _ReportState(mocker)
    state.supervisor.generate.side_effect = state.generated
    channel = create_autospec(LogChannel, instance=True, spec_set=True)

    with event_logging(channel):
        await state.flow(state.context)

    records = _records(channel)
    assert [(record["stage"], record["event"]) for record in records] == [
        ("report.generate", "started"), ("report.generate", "returned"),
        ("report.generate", "result"), ("report.publish", "started"),
        ("report.publish", "returned"), ("report.publish", "result")]
    job = state.supervisor.generate.call_args.args[0]
    assert all(record["job_id"] == job.job_id and record["report_id"] == 1
               and record["database_id"] == state.owned.metadata.instance_id
               and record["level"] == "INFO" for record in records)
    assert records[2]["generation_kind"] == "success"
    assert records[-1]["publication_outcome"] == "published"


@pytest.mark.parametrize("failure", [ValueError("original failure"), asyncio.CancelledError()])
async def test_report_stage_preserves_actual_exception_classification(mocker, failure):
    state = _ReportState(mocker)
    state.supervisor.generate.side_effect = failure
    mocker.patch("camctl.reporting.policy.record_report_failure", autospec=True,
        return_value=DbOutcome(DbOutcomeKind.COMPLETED))
    channel = create_autospec(LogChannel, instance=True, spec_set=True)

    with event_logging(channel), pytest.raises(type(failure)) as caught:
        await state.flow(state.context)

    assert caught.value is failure
    records = _records(channel)
    assert [record["event"] for record in records] == [
        "started", "cancelled" if isinstance(failure, asyncio.CancelledError) else "raised"]
    assert records[-1]["exception_type"] == type(failure).__name__
    state.delivery.assert_not_awaited()


async def test_returned_failed_generation_is_not_logged_as_success(mocker):
    state = _ReportState(mocker)
    state.supervisor.generate.side_effect = lambda job: WorkerGeneration(
        GenerationOutcomeKind.TIMEOUT, job.job_id, detail="no worker response")
    mocker.patch("camctl.reporting.policy.record_report_failure", autospec=True,
        return_value=DbOutcome(DbOutcomeKind.COMPLETED))
    channel = create_autospec(LogChannel, instance=True, spec_set=True)

    with event_logging(channel):
        await state.flow(state.context)

    records = _records(channel)
    assert [record["event"] for record in records] == ["started", "returned", "result"]
    assert records[-1]["generation_kind"] == "timeout"
    state.delivery.assert_not_awaited()


async def test_unchanged_report_polling_has_no_stage_events(mocker):
    state = _ReportState(mocker)
    state.pending = False
    state.context.report_change_wm = AsyncMock(return_value=5)
    repository = create_autospec(ReportingRepository, instance=True, spec_set=True)
    repository.freeze_report.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        SimpleNamespace(kind=ReportDecisionKind.SKIP, report=None, checked_to_wm=5))
    mocker.patch("camctl.reporting.policy.ReportingRepository", return_value=repository)
    channel = create_autospec(LogChannel, instance=True, spec_set=True)

    with event_logging(channel):
        for _ in range(3):
            await _drive_flows(state.context)
            await state.local.settle()

    assert _records(channel) == []


@pytest.mark.parametrize("confirmed", [True, False])
async def test_report_recovery_event_requires_confirmed_publication_save(mocker, confirmed):
    state = _ReportState(mocker)
    def execute(query, parameters=()):
        cursor = state._execute(query, parameters)
        if query.startswith("SELECT status, to_wm FROM reports"):
            cursor.fetchone.return_value = (2, 1)
            cursor.fetchall.return_value = [(2, 1)]
        return cursor
    state.connection.execute.side_effect = execute
    mocker.patch("camctl.reporting.publication.observe_report_locations", autospec=True,
        return_value=ReportLocations(Sighting((ReportFileIdentity(1, "b" * 64),), None),
            Sighting((), None), Sighting((), None)))
    outcome = DbOutcome(DbOutcomeKind.COMPLETED if confirmed else DbOutcomeKind.UNKNOWN,
        error=None if confirmed else RuntimeError("unconfirmed publication"))
    mocker.patch("camctl.reporting.policy.record_recovered_publication", autospec=True,
        return_value=outcome)
    channel = create_autospec(LogChannel, instance=True, spec_set=True)

    with event_logging(channel):
        if confirmed:
            await state.flow(state.context)
        else:
            with pytest.raises(StateDbFailure):
                await state.flow(state.context)

    records = _records(channel)
    assert [(record["stage"], record["event"]) for record in records] == (
        [("report.publish", "recovered")] if confirmed else [])
    state.supervisor.generate.assert_not_awaited()
    state.delivery.assert_not_awaited()


@pytest.mark.parametrize("disposition", [CancelStartDisposition.SAVED, CancelStartDisposition.ALREADY])
async def test_cancel_action_records_only_first_saved_start_and_finish(mocker, disposition):
    state = _ReportState(mocker)
    repository = create_autospec(CancellationRepository, instance=True, spec_set=True)
    repository.start_cancel_action.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        CancelStartResult(disposition))
    finish_disposition = (CancelActionDisposition.SAVED
        if disposition is CancelStartDisposition.SAVED else CancelActionDisposition.ALREADY)
    repository.finish_cancel_action.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        CancelActionFinished(disposition=finish_disposition,
            action_status=3, plan_status=3, succeeded=1, failed=0))
    mocker.patch("camctl.persistence.repositories.cancellation.CancellationRepository", return_value=repository)
    mocker.patch("camctl.bootstrap.flows._due_cancel_actions", autospec=True,
        return_value=[(3, 1, "{}")])
    mocker.patch("camctl.bootstrap.flows._fixed_cancel_items", autospec=True, return_value=None)
    mocker.patch("camctl.bootstrap.flows._resolve_and_fix", autospec=True, return_value=(1,))
    mocker.patch("camctl.cancellation.service.apply_cancel", autospec=True,
        return_value=CancelProgress(items=(CancelItemProgress(item_id=1, target_action_id=2, status=3),)))
    channel = create_autospec(LogChannel, instance=True, spec_set=True)

    with event_logging(channel):
        await cancel_flow(ready=Path("/ready"), processing=Path("/processing"))(state.context)

    records = _records(channel)
    assert [record["event"] for record in records] == (
        ["started", "result"] if disposition is CancelStartDisposition.SAVED else [])
    assert all(record["action_id"] == 3 and record["stage"] == "cancel.action" for record in records)


async def test_resending_original_cancel_fix_does_not_repeat_stage(mocker):
    state = _ReportState(mocker)
    entries = StartEntryCoordinator()
    request = FixCancelTargets(3, FixedCancelSet(), 1000)
    entries.pending_cancels[3] = (request, "a" * 32)
    repository = create_autospec(CancellationRepository, instance=True, spec_set=True)
    repository.fix_cancel_targets.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        CancelTargetsSaved(CancelTargetsDisposition.SAVED, (1,)))
    mocker.patch("camctl.persistence.repositories.cancellation.CancellationRepository", return_value=repository)
    mocker.patch("camctl.bootstrap.flows._due_cancel_actions", autospec=True, return_value=[])
    channel = create_autospec(LogChannel, instance=True, spec_set=True)

    with event_logging(channel):
        await cancel_flow(ready=Path("/ready"), processing=Path("/processing"),
            start_entries=entries)(state.context)

    repository.fix_cancel_targets.assert_called_once()
    assert entries.pending_cancels == {}
    assert _records(channel) == []


@pytest.mark.parametrize("disposition", [CancelTargetsDisposition.SAVED, CancelTargetsDisposition.ALREADY])
async def test_cancel_target_fix_records_only_first_save(mocker, disposition):
    state = _ReportState(mocker)
    repository = create_autospec(CancellationRepository, instance=True, spec_set=True)
    repository.fix_cancel_targets.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        CancelTargetsSaved(disposition, (1,)))
    repository.finish_cancel_action.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        CancelActionFinished(disposition=CancelActionDisposition.ALREADY,
            action_status=3, plan_status=3, succeeded=1, failed=0))
    mocker.patch("camctl.persistence.repositories.cancellation.CancellationRepository", return_value=repository)
    mocker.patch("camctl.bootstrap.flows._due_cancel_actions", autospec=True,
        return_value=[(3, 2, json.dumps({"params": {"target": {"action_instance_id": "7"}}}))])
    mocker.patch("camctl.bootstrap.flows._fixed_cancel_items", autospec=True, return_value=None)
    mocker.patch("camctl.cancellation.targets.resolve_cancel_target", autospec=True,
        return_value=TargetResolution(action_ids=(7,)))
    mocker.patch("camctl.cancellation.rules.load_eligibility_inputs", autospec=True,
        return_value=LoadedEligibility({"type": 4, "status": 3, "cancel_requested": 0},
            EligibilityFacts(terminal=True, cancel_applied=False,
                dispatch=DispatchPhase.NOT_STARTED, stop_supported=False)))
    mocker.patch("camctl.persistence.repositories.cancellation.sqlite_auto_candidates",
        autospec=True, return_value=())
    mocker.patch("camctl.cancellation.service.apply_cancel", autospec=True,
        return_value=CancelProgress(items=(CancelItemProgress(item_id=1, target_action_id=7, status=3),)))
    channel = create_autospec(LogChannel, instance=True, spec_set=True)

    with event_logging(channel):
        await cancel_flow(ready=Path("/ready"), processing=Path("/processing"))(state.context)

    records = _records(channel)
    assert [(record["stage"], record["event"]) for record in records] == (
        [("cancel.targets", "fixed")] if disposition is CancelTargetsDisposition.SAVED else [])
    if records:
        assert records[0]["item_ids"] == [1]
        assert records[0]["action_id"] == 3

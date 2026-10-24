"""报告生成等待由独立拥有者承担，错误仍经报告边界交付。"""

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest

from camctl.bootstrap import background_flow
from camctl.acceptance.service import CommandMode
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.flows import report_flow
from camctl.bootstrap.lifecycle import RuntimeDeps, _local_work, _report_assembly
from camctl.contracts.clock import ClockPort
from camctl.contracts.history_values import HistoryBoundary
from camctl.devices.bindings import DeviceConfigurationError
from camctl.persistence.repositories.history import HistoryRepository, _FrozenRegistration
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.runtime import DatabaseMetadata, DirectoryBindingError, OwnedConnection
from camctl.host_files.handoff import PublishResult, PublishStage
from camctl.host_files.io import DirectorySyncStage
from camctl.reporting.messages import ErrorKind, ResultFailureMessage, ResultSuccessMessage
from camctl.reporting.policy import ReportingRepository
from camctl.reporting.publication import (DeliveryOutcome, DeliveryResult, PublicationSession,
                                         deliver_staged_report as publish_report_file)
from camctl.reporting.supervisor import GenerationOutcomeKind, WorkerGeneration, WorkerSupervisor
from camctl.session.service import StateDbFailure, _drive_flows, _exception_details

pytestmark = pytest.mark.asyncio


async def test_production_assembly_does_not_await_report_before_scheduling(mocker):
    entered, release = asyncio.Event(), asyncio.Event()

    async def report(context):
        entered.set()
        await release.wait()

    mocker.patch("camctl.bootstrap.flows.report_flow", autospec=True, return_value=report)
    deps = RuntimeDeps(CommandMode.RUN, load_config({}, ConfigDefaults()),
                       Path("/state.db"), Path("/session.lock"), Path("/admission.lock"), None)
    flows, _ = _report_assembly(deps, None)
    called = []

    async def scheduling(context):
        called.append("scheduling")

    context = SimpleNamespace(flows={"report": flows["report"], "scheduling": scheduling},
                              local_work=_local_work(deps, flows))
    drive = asyncio.create_task(_drive_flows(context))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert called == ["scheduling"]
        assert context.local_work.required_settlements() == 1
    finally:
        release.set()
        await asyncio.wait_for(drive, timeout=1)
        await context.local_work.settle()


class _ReportState:
    def __init__(self, mocker):
        self.pending = True
        self.failed = False
        self.report_id = 1
        self.connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
        self.connection.execute.side_effect = self._execute
        self.owned = OwnedConnection(self.connection, DatabaseMetadata(
            "camctl", "a" * 32, 1, "/staging", "/ready", "/processing"))
        self.clock = create_autospec(ClockPort, instance=True, spec_set=True)
        self.clock.utc_micros.return_value = 1000
        self.supervisor = create_autospec(WorkerSupervisor, instance=True, spec_set=True)
        history = create_autospec(HistoryRepository, instance=True, spec_set=True)
        history.frozen_registration.return_value = _FrozenRegistration(0, 1, HistoryBoundary(1, 1))
        mocker.patch("camctl.persistence.repositories.history.HistoryRepository", return_value=history)
        self.delivery = mocker.patch("camctl.reporting.publication.deliver_staged_report", autospec=True,
                                     side_effect=self._publish)
        self.failure_log = mocker.patch("camctl.session.service._trigger_failure_log", autospec=True)
        self.device = Mock()

        async def scheduling(context):
            self.device()

        flow = report_flow(
            state_db=Path("/state.db"), staging=Path("/staging"), ready=Path("/ready"),
            processing=Path("/processing"), history=SimpleNamespace(entity_batch_size=10, event_batch_size=10),
            database=SimpleNamespace(busy_timeout_ms=100), supervisor=self.supervisor,
            on_failure=self.failure_log)
        self.report = background_flow.ReportBackgroundFlow(flow)
        self.local = background_flow.CombinedLocalWork((self.report,))
        self.context = SimpleNamespace(
            open_connection=Mock(return_value=self.owned), clock=self.clock,
            flows={"report": self.report, "scheduling": scheduling}, local_work=self.local)

    def _execute(self, query, parameters=()):
        cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
        if query.startswith("SELECT status FROM reports"):
            cursor.fetchone.return_value = (1,)
            rows = []
        elif query.startswith("SELECT id FROM reports WHERE status IN"):
            rows = [(self.report_id,)] if self.pending else []
        elif query.startswith("SELECT id FROM reports WHERE status = 5"):
            rows = [(self.report_id,)] if self.failed else []
        else:
            rows = []
        cursor.fetchall.return_value = rows
        return cursor

    def generated(self, job):
        success = ResultSuccessMessage(job.job_id, job.instance_id,
            f"/staging/reports/status-report-{job.report_id}-" + "b" * 64 + ".json", 42, "b" * 64)
        return WorkerGeneration(GenerationOutcomeKind.SUCCESS, job.job_id, success=success)

    async def _publish(self, *args, **kwargs):
        self.pending = False
        return DeliveryResult(DeliveryOutcome.PUBLISHED)


async def test_report_generation_wait_allows_following_scheduling_rounds(mocker):
    state = _ReportState(mocker)
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(job):
        entered.set()
        await release.wait()
        return state.generated(job)

    state.supervisor.generate.side_effect = generate
    try:
        assert await asyncio.wait_for(_drive_flows(state.context), timeout=1) is None
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert state.device.call_count == 1
        assert state.local.required_settlements() == 1
        assert await _drive_flows(state.context) is None
        assert state.device.call_count == 2
        state.supervisor.generate.assert_awaited_once()
        state.connection.close.assert_not_called()
        release.set()
        await state.local.settle()
        assert not state.pending
        state.connection.close.assert_called_once()
    finally:
        release.set()
        await state.local.settle()


async def test_report_failure_waits_new_changes_then_maintains_next_opportunity(mocker):
    state = _ReportState(mocker)
    changed = False

    def freeze(*args, **kwargs):
        nonlocal changed
        if changed:
            state.report_id = 2
            state.pending = True
            changed = False
        return DbOutcome(DbOutcomeKind.COMPLETED)

    repository = create_autospec(ReportingRepository, instance=True, spec_set=True)
    repository.freeze_report.side_effect = freeze
    mocker.patch("camctl.reporting.policy.ReportingRepository", return_value=repository)

    def record_failure(*args, **kwargs):
        state.pending = False
        state.failed = True
        return DbOutcome(DbOutcomeKind.COMPLETED)

    mocker.patch("camctl.reporting.policy.record_report_failure", autospec=True, side_effect=record_failure)

    async def generate(job):
        if job.report_id == 1:
            failure = ResultFailureMessage(job.job_id, job.instance_id, ErrorKind.REPORT,
                                           "write_failed", "report file write failed")
            return WorkerGeneration(GenerationOutcomeKind.REPORT_FAILURE, job.job_id, failure=failure)
        return state.generated(job)

    state.supervisor.generate.side_effect = generate
    assert await _drive_flows(state.context) is None
    await state.local.settle()
    assert state.failed
    state.failure_log.assert_awaited_once()
    for _ in range(2):
        assert await _drive_flows(state.context) is None
        await state.local.settle()
    state.supervisor.generate.assert_awaited_once()

    changed = True
    assert await _drive_flows(state.context) is None
    await state.local.settle()
    assert [call.args[0].report_id for call in state.supervisor.generate.await_args_list] == [1, 2]
    assert not state.pending


@pytest.mark.parametrize("failure", [
    RuntimeError("report content failed"), StateDbFailure("report state failed"),
    DirectoryBindingError("report directory changed"), DeviceConfigurationError("device config changed"),
])
async def test_report_background_result_keeps_original_error_boundary(mocker, failure):
    state = _ReportState(mocker)
    release = asyncio.Event()

    async def generate(job):
        await release.wait()
        raise failure

    state.supervisor.generate.side_effect = generate
    assert await _drive_flows(state.context) is None
    release.set()
    task = state.report.unconsumed_task
    await asyncio.wait_for(asyncio.shield(task), timeout=1)
    state.failure_log.assert_awaited_once_with(state.context, failure)
    # 此轮只交付已完成任务；下一轮报告机会由持久化维护规则另行决定。
    state.context.flows = {"scheduling": state.context.flows["scheduling"]}
    if isinstance(failure, (DirectoryBindingError, DeviceConfigurationError)):
        with pytest.raises(type(failure)) as caught:
            await _drive_flows(state.context)
        assert caught.value is failure
    else:
        result = await _drive_flows(state.context)
        assert result == (str(failure) if isinstance(failure, StateDbFailure) else None)
    assert state.device.call_count == (2 if type(failure) is RuntimeError else 1)
    state.failure_log.assert_awaited_once()
    await state.local.settle()


@pytest.mark.parametrize("failure", [StateDbFailure("report state failed"),
                                     DirectoryBindingError("report directory changed"),
                                     DeviceConfigurationError("device config changed")])
@pytest.mark.parametrize("stopped", [False, True])
async def test_report_fatal_stops_scheduling_before_failure_log_wait_finishes(mocker, failure, stopped):
    state = _ReportState(mocker)
    entered, release = asyncio.Event(), asyncio.Event()

    async def log_failure(context, error):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
            raise

    state.failure_log.side_effect = log_failure
    state.supervisor.generate.side_effect = failure
    waiter = None
    try:
        if isinstance(failure, StateDbFailure):
            assert await _drive_flows(state.context) == str(failure)
        else:
            with pytest.raises(type(failure)) as caught:
                await _drive_flows(state.context)
            assert caught.value is failure
        await asyncio.wait_for(entered.wait(), timeout=1)
        state.device.assert_not_called()
        assert state.local.required_settlements() == 1
        if stopped:
            state.local.stop_new_work()
        waiter = asyncio.create_task(state.local.settle())
        await asyncio.sleep(0)
        assert not waiter.done()
        release.set()
        with pytest.raises(type(failure)) as caught:
            await asyncio.wait_for(waiter, timeout=1)
        assert caught.value is failure
        state.failure_log.assert_awaited_once_with(state.context, failure)
    finally:
        release.set()
        if waiter is not None:
            await asyncio.gather(waiter, return_exceptions=True)
        else:
            await asyncio.gather(state.local.settle(), return_exceptions=True)


async def test_normal_report_settlement_waits_for_generation_and_connection_close(mocker):
    state = _ReportState(mocker)
    release = asyncio.Event()

    async def generate(job):
        await release.wait()
        return state.generated(job)

    state.supervisor.generate.side_effect = generate
    await _drive_flows(state.context)
    waiter = asyncio.create_task(state.local.settle())
    await asyncio.sleep(0)
    assert not waiter.done()
    assert state.local.required_settlements() == 1
    state.connection.close.assert_not_called()
    release.set()
    await asyncio.wait_for(waiter, timeout=1)
    state.delivery.assert_awaited_once()
    state.connection.close.assert_called_once()


async def test_report_stop_waits_for_supervisor_actual_cleanup_before_connection_close(mocker):
    state = _ReportState(mocker)
    cleaning, release = asyncio.Event(), asyncio.Event()

    async def generate(job):
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cleaning.set()
            await release.wait()
            raise

    state.supervisor.generate.side_effect = generate
    await _drive_flows(state.context)
    state.local.stop_new_work()
    waiter = asyncio.create_task(state.local.settle())
    await asyncio.wait_for(cleaning.wait(), timeout=1)
    assert not waiter.done() and state.local.required_settlements() == 1
    state.connection.close.assert_not_called()
    release.set()
    await asyncio.wait_for(waiter, timeout=1)
    state.connection.close.assert_called_once()
    state.delivery.assert_not_awaited()
    assert state.local.required_settlements() == 0


async def test_report_publication_allows_scheduling_and_stop_waits_actual_file_end(mocker):
    state = _ReportState(mocker)
    state.supervisor.generate.side_effect = state.generated
    state.delivery.side_effect = publish_report_file
    session = create_autospec(PublicationSession, instance=True, spec_set=True)
    session.prepare.return_value = session.record_intent.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    mocker.patch("camctl.reporting.publication.DbPublicationSession", autospec=True, return_value=session)
    mocker.patch("camctl.reporting.publication._list_stale_ready", autospec=True, return_value=((), None))

    async def in_process(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    mocker.patch("camctl.reporting.publication.asyncio.to_thread", autospec=True, side_effect=in_process)
    entered, release, moved = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def move(*args):
        entered.set()
        await release.wait()
        moved.set()
        return PublishResult(PublishStage.MOVED, DirectorySyncStage.SYNCED, True, None)

    mocker.patch("camctl.reporting.publication._move_staged", autospec=True, side_effect=move)
    await _drive_flows(state.context)
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert await _drive_flows(state.context) is None
    assert state.device.call_count == 2
    state.local.stop_new_work()
    waiter = asyncio.create_task(state.local.settle())
    try:
        await asyncio.sleep(0)
        assert not waiter.done() and state.local.required_settlements() == 1
        state.connection.close.assert_not_called()
        release.set()
        await asyncio.wait_for(waiter, timeout=1)
        assert moved.is_set()
        assert state.report.file_step_result.value.stage is PublishStage.MOVED
        assert any("moved" in note for note in state.report.interrupted_error.__notes__)
        state.connection.close.assert_called_once()
        session.save_publication.assert_not_called()
        assert state.local.required_settlements() == 0
    finally:
        release.set()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.parametrize("failure", [DirectoryBindingError("directory changed"),
                                     DeviceConfigurationError("device config changed")])
async def test_report_configuration_error_observed_only_during_settlement_keeps_classification(mocker, failure):
    state = _ReportState(mocker)
    release = asyncio.Event()

    async def generate(job):
        await release.wait()
        raise failure

    state.supervisor.generate.side_effect = generate
    await _drive_flows(state.context)
    waiter = asyncio.create_task(state.local.settle())
    release.set()
    with pytest.raises(type(failure)) as caught:
        await asyncio.wait_for(waiter, timeout=1)
    assert caught.value is failure
    assert _exception_details(caught.value, stage="shutdown")[0] == "configuration_error"
    state.failure_log.assert_awaited_once_with(state.context, failure)

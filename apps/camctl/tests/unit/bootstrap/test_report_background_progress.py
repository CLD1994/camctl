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
from camctl.persistence.runtime import DatabaseMetadata, DirectoryBindingError, OwnedConnection
from camctl.reporting.messages import ResultSuccessMessage
from camctl.reporting.publication import DeliveryOutcome, DeliveryResult
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
            database=SimpleNamespace(busy_timeout_ms=100), supervisor=self.supervisor)
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
            rows = [(1,)] if self.pending else []
        else:
            rows = []
        cursor.fetchall.return_value = rows
        return cursor

    def generated(self, job):
        success = ResultSuccessMessage(job.job_id, job.instance_id,
            "/staging/reports/status-report-1-" + "b" * 64 + ".json", 42, "b" * 64)
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

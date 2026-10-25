"""报告生成等待由独立拥有者承担，错误仍经报告边界交付。"""

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock, create_autospec
from support.business_database import inline_business_database

import pytest

from camctl.bootstrap import background_flow
from camctl.acceptance.service import CommandMode
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.flows import report_flow
from camctl.bootstrap.lifecycle import RuntimeDeps, _local_work, _report_assembly
from camctl.contracts.clock import ClockPort
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.history_values import BoundaryError
from camctl.contracts.json_values import JsonParseError
from camctl.contracts.public_projection import PublicProjectionError
from camctl.contracts.schemas import SchemaRuleError
from camctl.devices.bindings import DeviceConfigurationError
from camctl.history.events import HistoryEventError
from camctl.history.replay import ReplayError
from camctl.persistence.repositories.history import HistoryRepository, _FrozenRegistration
from camctl.persistence.models import DbOutcome, DbOutcomeKind, DbEnqueueTimeoutError
from camctl.persistence.runtime import DatabaseMetadata, DirectoryBindingError, OwnedConnection
from camctl.persistence.runtime import StateDatabaseError
from camctl.contracts.values import ConsistencyError
from camctl.host_files.handoff import PublishResult, PublishStage
from camctl.host_files.io import DirectorySyncStage
from camctl.reporting.messages import ErrorKind, ResultFailureMessage, ResultSuccessMessage
from camctl.reporting.policy import ReportingRepository, ReportDecisionKind
from camctl.reporting.publication import (DeliveryOutcome, DeliveryResult, PublicationSession,
                                         deliver_staged_report as publish_report_file)
from camctl.reporting.supervisor import GenerationOutcomeKind, WorkerGeneration, WorkerSupervisor
from camctl.session.service import (SessionContext, SessionPaths, StateDbFailure,
                                    _drive_flows, _exception_details, run_session)
from camctl.session.clock import ClockBecameUntrusted, ClockCheckInput
from camctl.session.work import WorkFacts, classify_work, WorkDecisionKind
from camctl.persistence.repositories.session import CloseDecision, SessionRepository

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
    def __init__(self, mocker, *, on_recovered=None):
        self.pending = True
        self.failed = False
        self.report_id = 1
        self.latest_wm = 5
        self.connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
        self.connection.execute.side_effect = self._execute
        self.owned = OwnedConnection(self.connection, DatabaseMetadata(
            "camctl", "a" * 32, 1, "/staging", "/ready", "/processing"))
        self.clock = create_autospec(ClockPort, instance=True, spec_set=True)
        self.clock.utc_micros.return_value = 1000
        self.supervisor = create_autospec(WorkerSupervisor, instance=True, spec_set=True)
        mocker.patch("camctl.persistence.repositories.history.read_frozen_registration", autospec=True,
            return_value=_FrozenRegistration(0, 1, HistoryBoundary(1, 1)))
        self.delivery = mocker.patch("camctl.reporting.publication.deliver_staged_report", autospec=True,
                                     side_effect=self._publish)
        self.failure_log = mocker.patch("camctl.session.service._trigger_failure_log", autospec=True)
        self.device = Mock()

        async def scheduling(context):
            self.device()

        self.flow = report_flow(
            state_db=Path("/state.db"), staging=Path("/staging"), ready=Path("/ready"),
            processing=Path("/processing"), history=SimpleNamespace(entity_batch_size=10, event_batch_size=10),
            database=SimpleNamespace(busy_timeout_ms=100), supervisor=self.supervisor,
            on_failure=self.failure_log, on_recovered=on_recovered)
        self.report = background_flow.ReportBackgroundFlow(self.flow)
        self.local = background_flow.CombinedLocalWork((self.report,))
        self.context = SimpleNamespace(
            database=inline_business_database(self.owned), clock=self.clock,
            flows={"report": self.report, "scheduling": scheduling}, local_work=self.local)

    def _execute(self, query, parameters=()):
        cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
        if query.startswith("SELECT status, to_wm FROM reports"):
            failed_ids = getattr(self, "failed_ids", ())
            cursor.fetchone.return_value = (5 if parameters[0] in failed_ids else 1, 1)
            rows = [cursor.fetchone.return_value]
        elif query == "SELECT MAX(change_seq) FROM history_events":
            cursor.fetchone.return_value = (self.latest_wm,)
            rows = []
        elif query.startswith("SELECT MIN("):
            cursor.fetchone.return_value = (getattr(self, "next_report_due", None),)
            rows = [cursor.fetchone.return_value]
        elif query.startswith("SELECT id FROM reports WHERE status IN"):
            pending_ids = getattr(self, "pending_ids", (self.report_id,) if self.pending else ())
            rows = [(report_id,) for report_id in pending_ids]
        elif query.startswith("SELECT id FROM reports WHERE status = 5"):
            failed_ids = getattr(self, "failed_ids", (self.report_id,) if self.failed else ())
            rows = [(report_id,) for report_id in failed_ids]
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
        state.connection.close.assert_not_called()
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


async def test_unchanged_report_boundary_does_not_reopen_local_work(mocker):
    state = _ReportState(mocker)
    state.pending = False
    state.context.report_change_wm = AsyncMock(return_value=5)
    repository = create_autospec(ReportingRepository, instance=True, spec_set=True)
    repository.freeze_report.return_value = DbOutcome(
        DbOutcomeKind.COMPLETED,
        SimpleNamespace(kind=ReportDecisionKind.SKIP, report=None, checked_to_wm=5))
    mocker.patch("camctl.reporting.policy.ReportingRepository", return_value=repository)

    for _ in range(4):
        assert await _drive_flows(state.context) is None
        await state.local.settle()

    state.connection.close.assert_not_called()
    repository.freeze_report.assert_called_once()
    assert state.local.required_settlements() == 0


async def test_report_action_deadline_reopens_maintenance_without_new_commit(mocker):
    state = _ReportState(mocker)
    state.pending = False
    state.next_report_due = 2000
    state.context.report_change_wm = AsyncMock(return_value=5)
    repository = create_autospec(ReportingRepository, instance=True, spec_set=True)
    repository.freeze_report.return_value = DbOutcome(
        DbOutcomeKind.COMPLETED,
        SimpleNamespace(kind=ReportDecisionKind.SKIP, report=None, checked_to_wm=5))
    mocker.patch("camctl.reporting.policy.ReportingRepository", return_value=repository)
    for now in (1000, 1999, 2000):
        state.clock.utc_micros.return_value = now
        assert await _drive_flows(state.context) is None
        await state.local.settle()

    state.connection.close.assert_not_called()
    assert repository.freeze_report.call_count == 2


async def test_change_committed_during_report_failure_remains_a_trigger(mocker):
    state = _ReportState(mocker)
    latest = 1
    state.context.report_change_wm = AsyncMock(side_effect=lambda: latest)

    async def generate(job):
        nonlocal latest
        latest = 2
        state.pending = False
        state.failed = True
        return WorkerGeneration(GenerationOutcomeKind.REPORT_FAILURE, job.job_id,
            failure=ResultFailureMessage(job.job_id, job.instance_id, ErrorKind.REPORT,
                                         "write_failed", "write failed"))

    state.supervisor.generate.side_effect = generate
    mocker.patch("camctl.reporting.policy.record_report_failure", autospec=True,
                 return_value=DbOutcome(DbOutcomeKind.COMPLETED))
    repository = create_autospec(ReportingRepository, instance=True, spec_set=True)
    repository.freeze_report.return_value = DbOutcome(
        DbOutcomeKind.COMPLETED,
        SimpleNamespace(kind=ReportDecisionKind.SKIP, report=None, checked_to_wm=2))
    mocker.patch("camctl.reporting.policy.ReportingRepository", return_value=repository)

    assert await _drive_flows(state.context) is None
    await state.local.settle()
    assert await _drive_flows(state.context) is None
    await state.local.settle()
    assert await _drive_flows(state.context) is None
    await state.local.settle()

    state.connection.close.assert_not_called()
    repository.freeze_report.assert_called_once()


@pytest.mark.parametrize("failed_stage", ["generate", "deliver"])
async def test_ordinary_report_exception_is_saved_once_and_waits_new_change(mocker, failed_stage):
    state = _ReportState(mocker)
    latest = 1
    state.context.report_change_wm = AsyncMock(side_effect=lambda: latest)
    original = RuntimeError(f"{failed_stage} failed")
    state.supervisor.generate.side_effect = state.generated
    failed_call = state.supervisor.generate if failed_stage == "generate" else state.delivery
    failed_call.side_effect = original

    def fail(*args, **kwargs):
        state.pending, state.failed = False, True
        return DbOutcome(DbOutcomeKind.COMPLETED)

    record = mocker.patch("camctl.reporting.policy.record_report_failure", autospec=True,
                          side_effect=fail)
    assert await _drive_flows(state.context) is None
    await state.local.settle()
    assert await _drive_flows(state.context) is None
    await state.local.settle()
    record.assert_called_once()
    assert record.call_args.args[2] == 1
    assert str(original) in record.call_args.args[3]["error"]
    state.failure_log.assert_awaited_once_with(state.context, original)
    state.supervisor.generate.assert_awaited_once()

    latest = 2

    def freeze(*args, **kwargs):
        state.pending, state.report_id = True, 2
        return DbOutcome(DbOutcomeKind.COMPLETED,
            SimpleNamespace(kind=ReportDecisionKind.GENERATE,
                            checked_to_wm=2, report=object()))

    repository = create_autospec(ReportingRepository, instance=True, spec_set=True)
    repository.freeze_report.side_effect = freeze
    mocker.patch("camctl.reporting.policy.ReportingRepository", return_value=repository)
    state.supervisor.generate.side_effect = state.generated
    state.delivery.side_effect = state._publish
    assert await _drive_flows(state.context) is None
    await state.local.settle()
    assert [call.args[0].report_id for call in state.supervisor.generate.await_args_list] == [1, 2]
    record.assert_called_once()


@pytest.mark.parametrize("failed_stage", ["generate", "deliver"])
@pytest.mark.parametrize("last_fails", [False, True])
@pytest.mark.parametrize("previously_failed", [False, True])
async def test_ordinary_failure_keeps_other_unattempted_reports_runnable(
        mocker, failed_stage, last_fails, previously_failed):
    state = _ReportState(mocker)
    state.pending_ids = [] if previously_failed else [1, 2]
    state.failed_ids = {1, 2} if previously_failed else set()
    state.context.report_change_wm = AsyncMock(return_value=1)
    repository = create_autospec(ReportingRepository, instance=True, spec_set=True)
    repository.freeze_report.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        SimpleNamespace(kind=ReportDecisionKind.REUSE, report=object(), checked_to_wm=1))
    mocker.patch("camctl.reporting.policy.ReportingRepository", return_value=repository)

    def generate(job):
        if failed_stage == "generate" and (job.report_id == 1 or last_fails):
            raise RuntimeError("worker startup failed")
        return state.generated(job)

    def fail(key, owned, report_id, error, *, occurred_at):
        if report_id in state.pending_ids:
            state.pending_ids.remove(report_id)
        state.failed_ids.add(report_id)
        return DbOutcome(DbOutcomeKind.COMPLETED)

    async def publish(report_id, *args, **kwargs):
        if failed_stage == "deliver" and (report_id == 1 or last_fails):
            raise RuntimeError("file settlement failed")
        if report_id in state.pending_ids:
            state.pending_ids.remove(report_id)
        state.failed_ids.discard(report_id)
        return DeliveryResult(DeliveryOutcome.PUBLISHED)

    state.supervisor.generate.side_effect = generate
    state.delivery.side_effect = publish
    record = mocker.patch("camctl.reporting.policy.record_report_failure", autospec=True,
                          side_effect=fail)

    for _ in range(3):
        assert await _drive_flows(state.context) is None
        await state.local.settle()

    assert state.pending_ids == []
    assert [call.args[0].report_id for call in state.supervisor.generate.await_args_list] == [1, 2]
    assert record.call_count == (2 if last_fails else 1)
    assert state.local.required_settlements() == 0


@pytest.mark.parametrize("failed_stage", ["generate", "deliver"])
@pytest.mark.parametrize("failure", [StateDatabaseError("state failed"),
                                     sqlite3.OperationalError("read failed"),
                                     ConsistencyError("history failed"),
                                     PublicProjectionError("projection failed"),
                                     BoundaryError("boundary failed"),
                                     HistoryEventError("event failed"),
                                     ReplayError("replay failed"),
                                     JsonParseError("stored json failed"),
                                     SchemaRuleError("schema failed")])
async def test_report_database_exception_remains_fatal_without_report_failure(
        mocker, failed_stage, failure):
    state = _ReportState(mocker)
    state.context.report_change_wm = AsyncMock(return_value=1)
    state.supervisor.generate.side_effect = state.generated
    failed_call = state.supervisor.generate if failed_stage == "generate" else state.delivery
    failed_call.side_effect = failure
    record = mocker.patch("camctl.reporting.policy.record_report_failure", autospec=True,
                          return_value=DbOutcome(DbOutcomeKind.COMPLETED))

    assert await _drive_flows(state.context) is None
    await asyncio.wait_for(asyncio.shield(state.report.unconsumed_task), timeout=1)
    scheduled_before_failure = state.device.call_count
    assert "状态库错误" in (await _drive_flows(state.context)).details["error"]
    record.assert_not_called()
    assert state.device.call_count == scheduled_before_failure


@pytest.mark.parametrize("failed_stage", ["generate", "deliver"])
@pytest.mark.parametrize("failure", [
    StateDbFailure("state prerequisite failed"),
    DirectoryBindingError("directory prerequisite failed"),
    DeviceConfigurationError("device prerequisite failed"),
    ClockBecameUntrusted("clock prerequisite failed"),
    asyncio.CancelledError("cancelled"),
    BaseException("control interruption"),
])
async def test_report_prerequisite_and_control_exceptions_keep_identity(
        mocker, failed_stage, failure):
    state = _ReportState(mocker)
    state.supervisor.generate.side_effect = state.generated
    failed_call = state.supervisor.generate if failed_stage == "generate" else state.delivery
    failed_call.side_effect = failure
    record = mocker.patch("camctl.reporting.policy.record_report_failure", autospec=True)

    with pytest.raises(type(failure)) as caught:
        await state.flow(state.context)

    assert caught.value is failure
    record.assert_not_called()
    state.connection.close.assert_not_called()


@pytest.mark.parametrize("outcome", [DeliveryOutcome.UNKNOWN, DeliveryOutcome.STORE_FAILED])
async def test_unconfirmed_publication_stays_fatal_without_saving_report_failure(mocker, outcome):
    state = _ReportState(mocker)
    state.supervisor.generate.side_effect = state.generated
    state.delivery.side_effect = None
    state.delivery.return_value = DeliveryResult(outcome, error="publication store unconfirmed")
    record = mocker.patch("camctl.reporting.policy.record_report_failure", autospec=True)

    with pytest.raises(StateDbFailure):
        await state.flow(state.context)

    record.assert_not_called()
    state.connection.close.assert_not_called()


@pytest.mark.parametrize("failed_stage", ["generate", "deliver"])
@pytest.mark.parametrize("record_result", [
    DbOutcome(DbOutcomeKind.ROLLED_BACK, error=RuntimeError("rolled back")),
    DbOutcome(DbOutcomeKind.UNKNOWN, error=RuntimeError("commit unconfirmed")),
    RuntimeError("failure store interrupted"),
])
async def test_unsaved_report_failure_stops_device_scheduling(
        mocker, failed_stage, record_result):
    state = _ReportState(mocker)
    state.supervisor.generate.side_effect = state.generated
    failed_call = state.supervisor.generate if failed_stage == "generate" else state.delivery
    failed_call.side_effect = RuntimeError("report operation failed")
    record = mocker.patch("camctl.reporting.policy.record_report_failure", autospec=True)
    if isinstance(record_result, Exception):
        record.side_effect = record_result
    else:
        record.return_value = record_result

    assert await _drive_flows(state.context) is None
    await asyncio.wait_for(asyncio.shield(state.report.unconsumed_task), timeout=1)
    scheduled_before_failure = state.device.call_count
    result = await _drive_flows(state.context)

    assert result is not None
    assert "报告失败记录" in result.details["error"]
    record.assert_called_once()
    assert state.device.call_count == scheduled_before_failure
    state.failure_log.assert_awaited_once()


@pytest.mark.parametrize("failure", [sqlite3.OperationalError("read failed"),
                                     DbEnqueueTimeoutError("queue full"),
                                     StateDbFailure("missing watermark")])
async def test_report_probe_failure_keeps_fatal_report_boundary(mocker, failure):
    state = _ReportState(mocker)
    state.context.report_change_wm = AsyncMock(side_effect=failure)

    assert (await _drive_flows(state.context)).details["error"] == (
        "missing watermark" if isinstance(failure, StateDbFailure)
        else f"报告维护无法可靠观察业务水位: {failure}")
    state.connection.execute.assert_not_called()
    state.device.assert_not_called()
    state.failure_log.assert_awaited_once()
    await state.local.settle()
    assert state.local.required_settlements() == 0


@pytest.mark.parametrize("method", ["read", "write"])
async def test_report_database_queue_failure_stops_after_actual_failure(mocker, method):
    state = _ReportState(mocker)
    state.pending = False
    error = DbEnqueueTimeoutError("business queue full")
    mocker.patch.object(state.context.database, method, side_effect=error)

    fatal = await _drive_flows(state.context)
    if fatal is None:
        await asyncio.wait_for(asyncio.shield(state.report.unconsumed_task), timeout=1)
        scheduled = state.device.call_count
        fatal = await _drive_flows(state.context)
        assert state.device.call_count == scheduled
    else:
        state.device.assert_not_called()
    assert fatal.details["error"] == "报告维护无法可靠读取或保存状态库事实: business queue full"
    state.failure_log.assert_awaited_once()


@pytest.mark.parametrize("new_at_close", [False, True])
async def test_completed_reports_close_session_and_final_check_keeps_new_changes(mocker, new_at_close):
    async def recovered():
        # 正常恢复入口确实让出执行，不能靠一次任务恰好同步结束来收尾。
        await asyncio.sleep(0)

    state = _ReportState(mocker, on_recovered=recovered)
    state.pending = False
    attempted = 0
    repository = create_autospec(ReportingRepository, instance=True, spec_set=True)

    def freeze(*args, **kwargs):
        nonlocal attempted
        attempted = state.latest_wm
        return DbOutcome(DbOutcomeKind.COMPLETED,
            SimpleNamespace(kind=ReportDecisionKind.REUSE, report=object(), checked_to_wm=attempted))

    repository.freeze_report.side_effect = freeze
    mocker.patch("camctl.reporting.policy.ReportingRepository", return_value=repository)
    session_repository = create_autospec(SessionRepository, instance=True, spec_set=True)
    admission, session = Mock(), Mock()

    def acquire(command, *_):
        command.acquire_and_register()
        return DbOutcome(DbOutcomeKind.COMPLETED)

    def facts(connection):
        return WorkFacts(0, 0, state.latest_wm > attempted, False,
                         False, False, True, False)

    close_calls = 0

    def close(command, _, owned):
        nonlocal close_calls
        close_calls += 1
        if new_at_close and close_calls == 1:
            state.latest_wm += 1
        decision = classify_work(command.facts_query(owned.connection))
        closed = decision.kind is WorkDecisionKind.CAN_EXIT_SUCCESS
        if closed:
            command.release_admission()
        return DbOutcome(DbOutcomeKind.COMPLETED, CloseDecision(closed, decision))

    session_repository.acquire_admission.side_effect = acquire
    session_repository.close_admission.side_effect = close
    context = SessionContext(
        mode=CommandMode.RUN, catalog=None, clock=state.clock,
        open_database=AsyncMock(return_value=state.context.database),
        acceptance_repository=Mock(), session_repository=session_repository,
        paths=SessionPaths("/session", "/admission"),
        clock_policy=lambda _: ClockCheckInput(1000, 0, 0, 1),
        acquire_session=Mock(return_value=session),
        acquire_admission=Mock(return_value=admission), facts_query=facts,
        flows=state.context.flows, local_work=state.local)

    async def wait(*args, **kwargs):
        await asyncio.sleep(0)

    mocker.patch("camctl.session.service._wait_for_more_work", side_effect=wait)
    outcome = await asyncio.wait_for(run_session(context, None), timeout=1)

    assert outcome.succeeded
    assert repository.freeze_report.call_count == (2 if new_at_close else 1)
    assert close_calls == (2 if new_at_close else 1)
    assert context.report_change_wm is None
    assert state.local.required_settlements() == 0


@pytest.mark.parametrize("failure", [
    RuntimeError("report content failed"), StateDbFailure("report state failed"),
    DirectoryBindingError("report directory changed"), DeviceConfigurationError("device config changed"),
])
async def test_report_background_result_keeps_original_error_boundary(mocker, failure):
    state = _ReportState(mocker)
    mocker.patch("camctl.reporting.policy.record_report_failure", autospec=True,
                 return_value=DbOutcome(DbOutcomeKind.COMPLETED))
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
        if isinstance(failure, StateDbFailure):
            assert result.details["error"] == str(failure)
        else:
            assert result is None
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
        assert await _drive_flows(state.context) is None
        await asyncio.wait_for(entered.wait(), timeout=1)
        scheduled_before_failure = state.device.call_count
        if isinstance(failure, StateDbFailure):
            assert (await _drive_flows(state.context)).details["error"] == str(failure)
        else:
            with pytest.raises(type(failure)) as caught:
                await _drive_flows(state.context)
            assert caught.value is failure
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert state.device.call_count == scheduled_before_failure
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


async def test_normal_report_settlement_waits_for_generation_without_closing_shared_database(mocker):
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
    state.connection.close.assert_not_called()


async def test_report_stop_waits_for_supervisor_actual_cleanup_without_closing_shared_database(mocker):
    state = _ReportState(mocker)
    cleaning, release, started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def generate(job):
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cleaning.set()
            await release.wait()
            raise

    state.supervisor.generate.side_effect = generate
    await _drive_flows(state.context)
    await asyncio.wait_for(started.wait(), timeout=1)
    state.local.stop_new_work()
    waiter = asyncio.create_task(state.local.settle())
    await asyncio.wait_for(cleaning.wait(), timeout=1)
    assert not waiter.done() and state.local.required_settlements() == 1
    state.connection.close.assert_not_called()
    release.set()
    await asyncio.wait_for(waiter, timeout=1)
    state.connection.close.assert_not_called()
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
        state.connection.close.assert_not_called()
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

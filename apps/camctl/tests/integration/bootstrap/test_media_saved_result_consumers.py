"""默认三工厂在原动作终态后仍须保存已取得的媒体结果。"""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import create_autospec

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode
from camctl.bootstrap import capture_assembly, lifecycle
from camctl.bootstrap.flows import _resolve_and_fix
from camctl.cancellation.models import ApplyCancelTarget, CancelApplyMode, StartCancelAction
from camctl.capture.media_flow import DriverReadSessions, run_recording_media
from camctl.capture.processing import CheckPhase
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.ports import DriverDeclaration
from camctl.history.events import business_columns
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput
from camctl.persistence.repositories.cancellation import CancellationRepository
from camctl.persistence.repositories.capture import CaptureRepository, FinishCanceledCapture
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import row_facts, saved_transaction_events
from camctl.session.outcome import SessionOutcome

from ..capture.media_retry_fixtures import _Catalog, _EVIDENCE, media_pipeline as pipeline  # noqa: F401
from ..capture.test_capture_contract import ResultsDouble
from ..capture.test_input_copy import _CONTENT, _NOW
from ..capture.test_media_flow import ReadDriverDouble
from ..capture.test_media_result_retry import _ActualTools
from .test_binding_transactions import _CommitFailure
from .test_media_assembly import _SessionDriver, _config


pytestmark = pytest.mark.asyncio


async def _default_factories(pipeline, monkeypatch):
    """保留默认装配，仅隔离会话循环及实际设备与工具。"""
    config = _config(pipeline[1].staging.parent)
    driver = _SessionDriver(_CONTENT)
    entry = DriverEntry("camctl-adb", driver, DriverDeclaration(
        control_supported=True, stop_supported=True, query_supported=False,
        result_supported=False, read_supported=True, digest_supported=True, delete_supported=False),
        _EVIDENCE, DriverStatus.SOFTWARE_CONTRACT_VERIFIED)
    from camctl.devices.drivers import runtime as driver_runtime
    monkeypatch.setattr(driver_runtime, "current_registry", lambda: DriverRegistry((entry,)))
    factories, contexts = [], []
    original_factory = capture_assembly.session_capture_assembly

    def observed_factory(**kwargs):
        factory = original_factory(**kwargs, results=ResultsDouble({}),
            wall_us=lambda: _NOW + 20, monotonic_ns=lambda: 7_000_000_000)
        factories.append(factory)
        return factory

    async def session(context, _source):
        contexts.append(context)
        return SessionOutcome(succeeded=True)

    monkeypatch.setattr(capture_assembly, "session_capture_assembly", observed_factory)
    monkeypatch.setattr(lifecycle, "run_session", create_autospec(lifecycle.run_session, side_effect=session))
    deps = lifecycle.build_runtime(CommandMode.RUN, config, catalog=_Catalog())
    try:
        assert deps.startup_error is None
        outcome = await lifecycle.execute_command(deps, None)
        assert outcome.succeeded
        assert len(factories) == 3 and len(contexts) == 1
        return deps, factories, contexts[0]
    except BaseException:
        lifecycle.close_runtime(deps)
        raise


def _observe_reopened_flow_connection(context, reopened):
    """流程通过正式打开端口取得独立、同身份的恢复连接。"""
    original_open = context.open_connection
    observed = []

    def open_connection():
        current = original_open()
        assert current.metadata == reopened.metadata
        assert current.connection is not reopened.connection
        observed.append(current)
        return current

    context.open_connection = open_connection
    return observed


def _cancel_and_end_original(owned):
    instant = datetime.fromtimestamp(_NOW // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    accepted = AcceptanceRepository().process_input(ProcessInput(ParsedInput("cancel.json", {
        "request_id": "2", "created_at": instant, "name": "取消原媒体动作",
        "actions": [{"name": "取消", "type": "cancel_task",
            "params": {"target": {"action_instance_id": "1"}}}]}),
        _Catalog(), CommandMode.RUN, _NOW + 10), new_operation_key(), owned)
    assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
    origin, raw = owned.connection.execute("SELECT id,input_fields_json FROM actions WHERE type=6").fetchone()
    repository = CancellationRepository()
    started = repository.start_cancel_action(StartCancelAction(origin, _NOW + 10), new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    items = _resolve_and_fix(owned, repository, origin, raw, lambda: _NOW + 10)
    assert len(items) == 1
    applied = repository.apply_cancel_target(ApplyCancelTarget(
        items[0], CancelApplyMode.WITH_STOP, _NOW + 10), new_operation_key(), owned)
    assert applied.kind is DbOutcomeKind.COMPLETED, applied.error
    ended = CaptureRepository().finish_canceled_capture(
        FinishCanceledCapture(1, _NOW + 10), new_operation_key(), owned)
    assert ended.kind is DbOutcomeKind.COMPLETED, ended.error
    assert owned.connection.execute("SELECT status,cancel_requested FROM actions WHERE id=1").fetchone() == (6, 1)


@pytest.mark.parametrize("entrance", ["normal", "residual", "restricted"])
@pytest.mark.parametrize("branch", ["check", "decision", "repair_failed", "repair_complete"])
async def test_default_consumers_save_original_media_after_target_terminal(pipeline, monkeypatch, entrance, branch):
    owned, roots, source_id = pipeline
    deps, factories, context = await _default_factories(pipeline, monkeypatch)
    driver, tools = ReadDriverDouble(_CONTENT), _ActualTools(branch)
    flow = factories[0](owned, "cam-1").media
    assert flow is not None
    flow.sessions = DriverReadSessions(owned, driver, ticket=None)
    flow.tools = tools
    method = {"check": "save_check_result", "decision": "save_repair_decision",
        "repair_failed": "save_repair_result", "repair_complete": "complete_repair_output"}[branch]
    original_save = getattr(CaptureRepository, method)
    inputs, failed = [], False

    def save(repository, command, key, current):
        nonlocal failed
        if branch == "check" and command.phase is CheckPhase.RUNNING:
            return original_save(repository, command, key, current)
        inputs.append((command, key))
        if not failed:
            failed = True
            return original_save(repository, command, key,
                replace(current, connection=_CommitFailure(current.connection, True)))
        return original_save(repository, command, key, current)

    monkeypatch.setattr(CaptureRepository, method, save)
    reopened = None
    try:
        with pytest.raises(ConsistencyError):
            await run_recording_media(flow, 1, 1, source_id)
        assert len(inputs) == 1 and tools.probes == 1
        assert tools.repairs == (1 if branch.startswith("repair_") else 0)
        owned.connection.close()
        reopened = open_existing(roots.staging.parent / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
        assert reopened.metadata == owned.metadata
        assert saved_transaction_events(reopened.connection, inputs[0][1]) is not None
        flow_connections = _observe_reopened_flow_connection(context, reopened)
        _cancel_and_end_original(reopened)
        columns = business_columns("actions")
        original_action = {name: row_facts(reopened.connection, "actions", 1)[name] for name in columns}
        processing_columns = business_columns("recording_processing")
        original_processing = {name: row_facts(reopened.connection, "recording_processing", 1)[name]
            for name in processing_columns}
        if entrance == "normal":
            await context.flows["scheduling"](context)
        elif entrance == "residual":
            await context.flows["residual"](context)
        else:
            await context.restricted_flows["winddown"](context)
        assert len(flow_connections) == 1
        assert len(inputs) == 2, "终态动作过滤不能丢失已取得的原媒体保存责任"
        assert inputs[1] == inputs[0]
        assert tools.probes == 1 and tools.repairs == (1 if branch.startswith("repair_") else 0)
        assert len(driver.opens) == 1
        assert {name: row_facts(reopened.connection, "actions", 1)[name] for name in columns} == original_action
        assert {name: row_facts(reopened.connection, "recording_processing", 1)[name]
            for name in processing_columns} == original_processing
        state = reopened.connection.execute(
            "SELECT check_state,media_json,repair_state,repair_basis_json,repair_error_json,repair_output_file_id"
            " FROM recording_processing WHERE id=1").fetchone()
        assert state[0] == 3 and parse_exact_json(state[1])["duration"]["seconds"] == Decimal("75.125")
        if branch == "decision":
            assert state[2] == 3 and parse_exact_json(state[3]) == inputs[0][0].basis.as_json()
        elif branch == "repair_failed":
            assert state[2] == 6 and parse_exact_json(state[4]) == inputs[0][0].error.as_json()
        elif branch == "repair_complete":
            assert state[2] == 5 and state[5] == inputs[0][0].output_file_id
        snapshots = [factory(reopened, "cam-1") for factory in factories]
        assert snapshots[2].media is None
        assert snapshots[0].pending_media_results is snapshots[1].pending_media_results is snapshots[2].pending_media_results
        assert not snapshots[0].pending_media_results
    finally:
        if reopened is not None:
            reopened.connection.close()
        lifecycle.close_runtime(deps)

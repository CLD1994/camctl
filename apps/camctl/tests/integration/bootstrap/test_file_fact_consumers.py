"""default 流程在业务筛选和取消生效前接手原文件保存责任。"""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode
from camctl.bootstrap import capture_assembly, lifecycle
from camctl.capture.handlers import _finish_listing_result, _listing_round, _register_listing
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.drivers.registry import DriverRegistry
from camctl.history.events import business_columns
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput
from camctl.persistence.repositories.cancellation import CancellationRepository
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import row_facts, saved_transaction_events
from camctl.session.outcome import SessionOutcome
from camctl.session.service import StateDbFailure

from ..capture.result_consumer_fixtures import ResultCatalog, consumer_world
from ..capture.test_result_consumer_saves import _actual, _result_port
from .test_binding_transactions import _CommitFailure
from .test_media_assembly import _config
from .test_media_saved_result_consumers import (
    _cancel_and_end_original, _observe_reopened_flow_connection,
)


pytestmark = pytest.mark.asyncio

_METHODS = {
    "discovery": "save_file_observation",
    "presence": "save_file_presence",
    "ownership": "save_file_ownership",
    "completion": "save_file_completion",
}


async def _default_context(tmp_path, monkeypatch):
    """只隔离会话循环，保留默认流程和三个真实工厂，驱动登记为空。"""
    from camctl.devices.drivers import runtime as driver_runtime

    monkeypatch.setattr(driver_runtime, "current_registry", lambda: DriverRegistry(()))
    contexts, factory_calls = [], []
    original = capture_assembly.session_capture_assembly

    def factory(**kwargs):
        real = original(**kwargs)

        def observed(owned, device_id):
            factory_calls.append(device_id)
            return real(owned, device_id)

        return observed

    async def session(context, _source):
        contexts.append(context)
        return SessionOutcome(succeeded=True)

    monkeypatch.setattr(capture_assembly, "session_capture_assembly", factory)
    monkeypatch.setattr(lifecycle, "run_session",
                        create_autospec(lifecycle.run_session, side_effect=session))
    deps = lifecycle.build_runtime(CommandMode.RUN, _config(tmp_path), catalog=ResultCatalog())
    try:
        assert deps.startup_error is None
        outcome = await lifecycle.execute_command(deps, None)
        assert outcome.succeeded
        assert len(contexts) == 1
        return deps, contexts[0], factory_calls
    except BaseException:
        lifecycle.close_runtime(deps)
        raise


def _accept_cancel(owned, occurred_at, request_id):
    instant = datetime.fromtimestamp(occurred_at // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    result = AcceptanceRepository().process_input(ProcessInput(ParsedInput("cancel-file.json", {
        "request_id": str(request_id), "created_at": instant, "name": "取消原拍摄",
        "actions": [{"name": "取消", "type": "cancel_task", "params": {
            "target": {"action_instance_id": "1"}}}],
    }), ResultCatalog(), CommandMode.RUN, occurred_at), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    return owned.connection.execute("SELECT MAX(id) FROM actions WHERE type=6").fetchone()[0]


async def _hold_original_file_stage(tmp_path, monkeypatch, stage, commit_after):
    owned, runtime, action_id, _ = await consumer_world(tmp_path, "record")
    deps, context, factory_calls = await _default_context(tmp_path, monkeypatch)
    runtime.pending_start_results = deps.capture_call_results
    runtime.pending_file_observations = deps.capture_file_observations
    runtime.retry_gate = deps.capture_retry_gate
    actual = _actual(action_id, with_files=True, complete=True)
    driver = _result_port(runtime, actual)
    method = _METHODS[stage]
    original_save = getattr(CaptureRepository, method)
    inputs, order = [], []
    state = {"preparing": True, "confirmation": "saved"}

    def save(repository, command, key, current):
        inputs.append((command, key))
        if state["preparing"]:
            if len(inputs) == 1:
                return original_save(repository, command, key, replace(current,
                    connection=_CommitFailure(current.connection, commit_after)))
            return DbOutcome(kind=DbOutcomeKind.UNKNOWN,
                             error=sqlite3.OperationalError("原事实核实尚未取得响应"))
        order.append("file_confirmation")
        if state["confirmation"] != "saved":
            return DbOutcome(kind=(DbOutcomeKind.UNKNOWN if state["confirmation"] == "unknown"
                                   else DbOutcomeKind.ROLLED_BACK),
                             error=ConsistencyError("原文件申请仍未可靠核实"))
        return original_save(repository, command, key, current)

    monkeypatch.setattr(CaptureRepository, method, save)
    reopened = None
    try:
        listing = await _listing_round(runtime, action_id)
        with pytest.raises(ConsistencyError):
            _register_listing(runtime, action_id, listing)
        assert len(inputs) == 2 and inputs[1] == inputs[0]
        assert inputs[0][0].occurred_at == listing.occurred_at
        # 真实返回含 transport_timeout 和可靠文件，原结果处于真实等待分区。
        _finish_listing_result(runtime, listing, retry_wait=True)
        held = deps.capture_file_observations[(action_id, "original")]
        assert held.entries == listing.entries
        assert held.command.occurred_at == listing.occurred_at
        path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
        owned.connection.close()
        reopened = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        assert reopened.metadata == owned.metadata
        assert (saved_transaction_events(reopened.connection, inputs[0][1]) is not None) is commit_after
        state["preparing"] = False
        opened = _observe_reopened_flow_connection(context, reopened)
        return (reopened, runtime, deps, context, factory_calls, driver, inputs,
                order, state, held, opened)
    except BaseException:
        owned.connection.close()
        if reopened is not None:
            reopened.connection.close()
        lifecycle.close_runtime(deps)
        raise


class _ClockAfterFileSave:
    def __init__(self, pending, value):
        self.pending, self.value = pending, value

    def utc_micros(self):
        assert not self.pending, "default 入口必须先保存原文件责任，再读取业务墙钟"
        return self.value


def _business_state(owned):
    return tuple(row_facts(owned.connection, "actions", 1)[name]
                 for name in business_columns("actions"))


def _result_state(owned):
    return owned.connection.execute(
        "SELECT r.status,r.attempts_used,r.retry_wait_required,t.attempt_no,t.status,"
        "t.result_json,t.error_json FROM operation_runs r JOIN operation_attempts t ON t.run_id=r.id"
        " WHERE r.responsibility_key='results/1' ORDER BY t.attempt_no").fetchall()


def _file_complete(owned):
    return owned.connection.execute(
        "SELECT source_action_id,presence_state,completion_state,size_bytes FROM device_files"
        " WHERE observer_action_id=1").fetchone() == (1, 2, 3, 41)


def _flow(context, entrance):
    if entrance == "restricted":
        return context.restricted_flows["winddown"]
    if entrance == "restricted_cancel":
        return context.restricted_flows["cancel"]
    return context.flows[{"normal": "scheduling", "residual": "residual", "cancel": "cancel"}[entrance]]


@pytest.mark.parametrize("entrance,commit_after", [
    ("normal", True), ("residual", True), ("restricted", True),
    ("cancel", True), ("cancel", False),
    ("restricted_cancel", True), ("restricted_cancel", False),
])
@pytest.mark.parametrize("stage", tuple(_METHODS))
async def test_default_file_consumers_confirm_original_stage_before_filters(
        tmp_path, monkeypatch, entrance, commit_after, stage):
    (owned, runtime, deps, context, factory_calls, driver, inputs, order,
     state, held, opened) = await _hold_original_file_stage(tmp_path, monkeypatch, stage, commit_after)
    try:
        if commit_after:
            _cancel_and_end_original(owned)
        before_action, before_result = _business_state(owned), _result_state(owned)
        cancel_id = None
        if entrance in ("cancel", "restricted_cancel"):
            cancel_id = _accept_cancel(owned, held.command.occurred_at + 30, 3 if commit_after else 2)
            original_apply = CancellationRepository.apply_cancel_target

            def apply(repository, command, key, current):
                assert _file_complete(current), "Apply 前须可靠保存全部原文件事实"
                order.append("cancel_apply")
                return original_apply(repository, command, key, current)

            monkeypatch.setattr(CancellationRepository, "apply_cancel_target", apply)
        context.clock = _ClockAfterFileSave(deps.capture_file_observations,
                                           held.command.occurred_at + 5_000_000)

        await _flow(context, entrance)(context)

        assert opened and all(current.metadata == owned.metadata for current in opened)
        assert len(inputs) == 3 and inputs[-1] == inputs[0]
        assert order[0] == "file_confirmation"
        assert not deps.capture_file_observations
        assert not factory_calls, "原文件保存不得依赖当前缺失驱动的 capture_factory"
        assert _file_complete(owned)
        assert _result_state(owned) == before_result
        driver.list_results.assert_awaited_once()
        if commit_after:
            assert _business_state(owned) == before_action
        if entrance in ("cancel", "restricted_cancel"):
            assert "cancel_apply" in order
            assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (cancel_id,)).fetchone()[0] in (2, 3)
            if not commit_after:
                assert owned.connection.execute("SELECT cancel_requested FROM actions WHERE id=1").fetchone() == (1,)
        if stage == "discovery":
            first = saved_transaction_events(owned.connection, inputs[0][1])
            assert first is not None and first[0]["occurred_at"] == inputs[0][0].occurred_at
    finally:
        owned.connection.close()
        lifecycle.close_runtime(deps)


@pytest.mark.parametrize("stage", tuple(_METHODS))
@pytest.mark.parametrize("confirmation", ["unknown", "rejected"])
@pytest.mark.parametrize("entrance", ["cancel", "restricted_cancel"])
async def test_default_cancel_keeps_original_file_responsibility_when_confirmation_fails(
        tmp_path, monkeypatch, stage, confirmation, entrance):
    (owned, runtime, deps, context, factory_calls, driver, inputs, order,
     state, held, opened) = await _hold_original_file_stage(tmp_path, monkeypatch, stage, False)
    try:
        cancel_id = _accept_cancel(owned, held.command.occurred_at + 30, 2)
        before_action, before_result = _business_state(owned), _result_state(owned)
        state["confirmation"] = confirmation
        original_apply = CancellationRepository.apply_cancel_target

        def apply(repository, command, key, current):
            order.append("cancel_apply")
            return original_apply(repository, command, key, current)

        monkeypatch.setattr(CancellationRepository, "apply_cancel_target", apply)
        context.clock = _ClockAfterFileSave(deps.capture_file_observations,
                                           held.command.occurred_at + 5_000_000)

        with pytest.raises(StateDbFailure):
            await _flow(context, entrance)(context)

        assert opened
        assert len(inputs) == 4 and all(value == inputs[0] for value in inputs)
        assert order == ["file_confirmation", "file_confirmation"]
        assert deps.capture_file_observations[(1, "original")] is held
        assert _business_state(owned) == before_action and _result_state(owned) == before_result
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (cancel_id,)).fetchone() == (1,)
        assert not factory_calls
        driver.list_results.assert_awaited_once()
    finally:
        owned.connection.close()
        lifecycle.close_runtime(deps)

"""默认五个入口在业务候选之前核实原 STOP 完整收尾申请。

真实 lifecycle 装配三个工厂和五个 flow，同会话的内存申请由新可靠
连接接手；本文件不覆盖两个 CLI 会话或跨进程重建内存申请。
"""

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from camctl.bootstrap import lifecycle
from camctl.capture.handlers import StopCloseRequest, capture_handler
from camctl.contracts.values import ConsistencyError
from camctl.contracts.workflow_errors import registered_error
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import saved_transaction_events
from camctl.session.service import StateDbFailure

from ..capture.test_start_close_save_recovery import _history
from ..capture.test_stop_close_save_recovery import (
    StopCloseFault, _activity, _assert_original_group, _attempts, stop_close_world,
)
from . import test_capture_completion_default_recovery as completion_defaults
from .test_capture_completion_default_recovery import _default_completion_world, _selected_flow
from .test_read_default_consumers import _BeforeBusinessCandidates, _CandidateClock
from .test_recording_results_cancellation import _BeforeCandidates, _CandidateBoundary
from .test_recording_results_saved_consumers import _UnavailableOriginalKey


pytestmark = pytest.mark.asyncio
_MODES = (("ordinary", "stop"), ("ordinary", "action"), ("canceled", "stop"))
_ENTRANCES = ("normal", "residual", "restricted", "normal-cancel", "restricted-cancel")


async def _default_stop_close_world(tmp_path, monkeypatch, consumer):
    """首次申请形成前，把实际生产者接入默认会话的空责任集合。"""
    world = await stop_close_world(tmp_path, consumer)
    actual_config = completion_defaults._config

    def bound_config(home):
        config = actual_config(home)
        return replace(config, paths=replace(config.paths,
            staging=world.metadata.staging_path, ready=world.metadata.ready_path,
            processing=world.metadata.processing_path))

    # 三个目录属于原数据库身份；默认配置必须采用相同绑定。
    monkeypatch.setattr(completion_defaults, "_config", bound_config)
    try:
        deps, factories, context, methods, now, runtime_calls = await _default_completion_world(
            world, monkeypatch, binding=True)
    except BaseException:
        world.owned.connection.close()
        raise
    try:
        assert deps.capture_completions == world.runtime.pending_capture_completions == {}
        for factory in factories:
            runtime = factory(world.owned, "cam-1")
            assert runtime is not None
            assert runtime.pending_capture_completions is deps.capture_completions
            assert runtime.retry_gate is deps.capture_retry_gate
        assert len(runtime_calls) == 3
        deps.capture_retry_gate.anchors.update(world.runtime.retry_gate.anchors)
        world.runtime.retry_gate = deps.capture_retry_gate
        world.runtime.pending_capture_completions = deps.capture_completions
        return world, deps, context, methods, now, runtime_calls
    except BaseException:
        world.owned.connection.close()
        lifecycle.close_runtime(deps)
        raise


def _record_saves(monkeypatch, *, unavailable_stage=None, confirmation_unknown=False):
    """透传真实仓储，记录机器协议输入；可只使第二次原键查询失败。"""
    state = SimpleNamespace(
        stop_inputs=[], stop_receipts=[], stop_owned=[], action_inputs=[],
        action_receipts=[], action_owned=[], unavailable=[])
    actual_stop = OperationRepository.finish_stale_runs
    actual_action = CaptureRepository.finish_capture

    def save_stop(repository, request, key, current):
        relevant = request.responsibility_keys == ("stop/12",)
        if relevant:
            state.stop_inputs.append((request, key))
            state.stop_owned.append(current)
            if unavailable_stage == "stop" and len(state.stop_inputs) == 2:
                fault = _UnavailableOriginalKey(current.connection, key, confirmation_unknown)
                state.unavailable.append(fault)
                current = replace(current, connection=fault)
        receipt = actual_stop(repository, request, key, current)
        if relevant:
            state.stop_receipts.append(receipt)
        return receipt

    def save_action(repository, request, key, current):
        relevant = (request.action_id == 12 and request.failure is not None
                    and request.failure.code == "recording_stop_failed")
        if relevant:
            state.action_inputs.append((request, key))
            state.action_owned.append(current)
            if unavailable_stage == "action" and len(state.action_inputs) == 2:
                fault = _UnavailableOriginalKey(current.connection, key, confirmation_unknown)
                state.unavailable.append(fault)
                current = replace(current, connection=fault)
        receipt = actual_action(repository, request, key, current)
        if relevant:
            state.action_receipts.append(receipt)
        return receipt

    monkeypatch.setattr(OperationRepository, "finish_stale_runs", save_stop)
    monkeypatch.setattr(CaptureRepository, "finish_capture", save_action)
    return state


async def _form_unknown_request(world, deps, saves, stage, after_commit):
    fault = StopCloseFault(world.owned.connection, world, stage,
        "commit_after" if after_commit else "commit_before")
    world.runtime.owned = replace(world.owned, connection=fault)
    with pytest.raises(ConsistencyError):
        await capture_handler("camera_record")(12, world.runtime)
    assert fault.targeted and fault.failed and fault.commit_calls == 1
    receipt = saves.stop_receipts[0] if stage == "stop" else saves.action_receipts[0]
    assert receipt.kind is DbOutcomeKind.UNKNOWN
    pending = deps.capture_completions[12]
    request = pending.request
    assert isinstance(request, StopCloseRequest)
    finish, stop_key = saves.stop_inputs[0]
    assert pending.key == stop_key and request.finish is finish and request.action_id == 12
    assert finish.occurred_at == world.formed_at
    assert finish.responsibility_keys == ("stop/12",)
    assert finish.error.code == "recording_stop_failed"
    expected_details = {"activity_id": "12", "operation_run_id": str(world.stop_id)}
    assert dict(finish.error.details) == expected_details
    if world.consumer == "canceled":
        assert request.action_finish is request.action_key is None
        assert saves.action_inputs == []
    else:
        assert request.action_finish.action_id == 12 and request.action_finish.drafts == ()
        assert request.action_finish.occurred_at == world.formed_at
        assert request.action_finish.failure.code == "recording_stop_failed"
        assert dict(request.action_finish.failure.details) == expected_details
        assert request.action_key is not None and request.action_key != stop_key
        assert saves.action_inputs == (
            [(request.action_finish, request.action_key)] if stage == "action" else [])
    assert world.runtime.pending_capture_completions is deps.capture_completions
    return pending


def _assert_frozen_saves(saves, pending, consumer, stage, *, action_reached=True):
    request = pending.request
    assert saves.stop_inputs == [(request.finish, pending.key), (request.finish, pending.key)]
    assert all(finish is request.finish for finish, _key in saves.stop_inputs)
    if consumer == "canceled" or not action_reached:
        assert saves.action_inputs == []
    else:
        count = 2 if stage == "action" else 1
        assert saves.action_inputs == [(request.action_finish, request.action_key)] * count
        assert all(finish is request.action_finish for finish, _key in saves.action_inputs)


def _assert_no_new_device_call(world, methods, runtime_calls):
    assert len(world.runtime.driver.calls) == len(world.stopper.calls) == 1
    assert len(runtime_calls) == 3
    for method in methods:
        method.assert_not_called()


def _change_current_config(world, deps, now):
    deps.config = replace(deps.config, devices={"cam-1": {"driver": "alternate-camera"}})
    world.runtime.stop_config = replace(world.runtime.stop_config, max_attempts=9,
        timeout_s=Decimal("8"), retry_interval_s=Decimal("30"))
    world.runtime.query_config = replace(world.runtime.query_config, max_attempts=9)
    world.wall[0] += 100_000_000
    world.mono[0] += 100_000_000_000
    now[0] += 100_000_000


@pytest.mark.parametrize("consumer,stage", _MODES)
@pytest.mark.parametrize("after_commit", [False, True], ids=["commit-before", "commit-after"])
@pytest.mark.parametrize("entrance", _ENTRANCES)
async def test_default_entry_confirms_original_stop_close_before_candidates(
        tmp_path, monkeypatch, consumer, stage, after_commit, entrance):
    world, deps, context, methods, now, runtime_calls = await _default_stop_close_world(
        tmp_path, monkeypatch, consumer)
    owned, reopened = world.owned, None
    before_attempts, before_history, before_activity = _attempts(owned), _history(owned), _activity(owned)
    saves = _record_saves(monkeypatch)
    try:
        pending = await _form_unknown_request(world, deps, saves, stage, after_commit)
        request = pending.request
        owned.connection.close()
        reopened = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
        assert reopened.metadata == world.metadata
        first_stop_events = saved_transaction_events(reopened.connection, pending.key)
        first_action_events = (None if request.action_key is None else
            saved_transaction_events(reopened.connection, request.action_key))
        assert (first_stop_events is not None) is (stage == "action" or after_commit)
        assert (first_action_events is not None) is (stage == "action" and after_commit)
        assert reopened.connection.execute("SELECT status FROM actions WHERE id=12").fetchone() == (
            4 if stage == "action" and after_commit else 2,)
        anchors = dict(deps.capture_retry_gate.anchors)
        _change_current_config(world, deps, now)
        opened = []

        def saved():
            _assert_frozen_saves(saves, pending, consumer, stage)
            assert saves.stop_receipts[1].kind is DbOutcomeKind.COMPLETED
            assert saves.stop_owned[1].connection is opened[0].connection
            assert world.runtime.pending_capture_completions is deps.capture_completions
            assert deps.capture_completions == {}
            assert deps.capture_retry_gate.anchors == {
                responsibility: anchor for responsibility, anchor in anchors.items()
                if responsibility != "stop/12"}
            stop_events = _assert_original_group(
                reopened, pending.key, world.formed_at, "operation_runs", world.stop_id, 6)
            if first_stop_events is not None:
                assert stop_events == first_stop_events
            assert reopened.connection.execute(
                "SELECT status,attempts_used,retry_wait_required FROM operation_runs"
                " WHERE id=?", (world.stop_id,)).fetchone() == (6, 1, 0)
            if consumer == "ordinary":
                assert saves.action_receipts[-1].kind is DbOutcomeKind.COMPLETED
                assert saves.action_owned[-1].connection is opened[0].connection
                action_events = _assert_original_group(reopened, request.action_key,
                    world.formed_at, "actions", 12, 4)
                assert stop_events[0]["transaction"].txn_id != action_events[0]["transaction"].txn_id
                if first_action_events is not None:
                    assert action_events == first_action_events
                assert reopened.connection.execute(
                    "SELECT status,error_code FROM actions WHERE id=12").fetchone() == (
                    4, registered_error("recording_stop_failed")["action_error_id"])
            else:
                # 默认保存前置只关闭 STOP；取消终态由业务 handler 随后推进。
                assert reopened.connection.execute(
                    "SELECT status,cancel_requested,error_code FROM actions WHERE id=12"
                ).fetchone() == (2, 1, None)
            assert _attempts(reopened) == before_attempts and _activity(reopened) == before_activity
            assert _history(reopened)[:len(before_history)] == before_history
            assert reopened.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
            _assert_no_new_device_call(world, methods, runtime_calls)

        original_open = context.open_connection

        def open_connection():
            current = original_open()
            assert current.metadata == world.metadata
            assert current.connection is not reopened.connection
            current = replace(current, connection=_CandidateBoundary(current.connection, saved))
            opened.append(current)
            return current

        context.open_connection = open_connection
        context.clock = _CandidateClock(saved)
        with pytest.raises((_BeforeCandidates, _BeforeBusinessCandidates)):
            await _selected_flow(context, entrance)(context)
        assert len(opened) == 1
        saved()
        if consumer == "canceled":
            stop_events = saved_transaction_events(reopened.connection, pending.key)
            world.runtime.owned = reopened
            await capture_handler("camera_record")(12, world.runtime)
            assert reopened.connection.execute(
                "SELECT status,cancel_requested,error_code FROM actions WHERE id=12"
            ).fetchone() == (6, 1, None)
            assert saves.action_inputs == []
            assert saved_transaction_events(reopened.connection, pending.key) == stop_events
            assert _attempts(reopened) == before_attempts and _activity(reopened) == before_activity
            _assert_no_new_device_call(world, methods, runtime_calls)
    finally:
        (reopened if reopened is not None else owned).connection.close()
        lifecycle.close_runtime(deps)


@pytest.mark.parametrize("consumer,stage", _MODES)
@pytest.mark.parametrize("confirmation_unknown", [False, True], ids=["rolled-back", "unknown"])
@pytest.mark.parametrize("entrance", ["normal", "restricted-cancel"])
async def test_default_entry_keeps_stop_close_when_original_key_is_unavailable(
        tmp_path, monkeypatch, consumer, stage, confirmation_unknown, entrance):
    world, deps, context, methods, now, runtime_calls = await _default_stop_close_world(
        tmp_path, monkeypatch, consumer)
    owned, reopened = world.owned, None
    saves = _record_saves(monkeypatch, unavailable_stage=stage,
        confirmation_unknown=confirmation_unknown)
    try:
        pending = await _form_unknown_request(world, deps, saves, stage, True)
        request = pending.request
        owned.connection.close()
        reopened = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
        assert reopened.metadata == world.metadata
        assert saved_transaction_events(reopened.connection, pending.key) is not None
        if stage == "action":
            assert saved_transaction_events(reopened.connection, request.action_key) is not None
        assert reopened.connection.execute("SELECT status FROM actions WHERE id=12").fetchone() == (
            4 if stage == "action" else 2,)
        before = tuple(reopened.connection.iterdump())
        anchors = dict(deps.capture_retry_gate.anchors)
        _change_current_config(world, deps, now)
        original_open, opened = context.open_connection, []

        def open_connection():
            current = original_open()
            assert current.metadata == world.metadata
            assert current.connection is not reopened.connection
            current = replace(current, connection=_CandidateBoundary(current.connection,
                lambda: pytest.fail("原 STOP 完整申请未核实前不得读取候选")))
            opened.append(current)
            return current

        context.open_connection = open_connection
        context.clock = _CandidateClock(lambda: pytest.fail("原 STOP 完整申请未核实前不得取得时刻"))
        with pytest.raises(StateDbFailure):
            await _selected_flow(context, entrance)(context)
        assert len(opened) == 1
        _assert_frozen_saves(saves, pending, consumer, stage, action_reached=stage == "action")
        assert saves.stop_owned[1].connection is opened[0].connection
        if stage == "action":
            assert saves.action_owned[1].connection is opened[0].connection
            assert saves.stop_receipts[1].kind is DbOutcomeKind.COMPLETED
        fault, = saves.unavailable
        assert fault.key_reads == 1
        receipt = saves.stop_receipts[1] if stage == "stop" else saves.action_receipts[1]
        assert receipt.kind is (
            DbOutcomeKind.UNKNOWN if confirmation_unknown else DbOutcomeKind.ROLLED_BACK)
        assert deps.capture_completions[12] is pending
        assert world.runtime.pending_capture_completions is deps.capture_completions
        assert deps.capture_retry_gate.anchors == anchors
        assert tuple(reopened.connection.iterdump()) == before
        _assert_no_new_device_call(world, methods, runtime_calls)
    finally:
        (reopened if reopened is not None else owned).connection.close()
        lifecycle.close_runtime(deps)

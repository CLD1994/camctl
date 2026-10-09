"""默认五个入口在候选之前核实原 START 完整收场申请。"""

from dataclasses import replace

import pytest

from camctl.bootstrap import lifecycle
from camctl.capture.handlers import capture_handler
from camctl.contracts.values import ConsistencyError
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import saved_transaction_events
from camctl.session.service import StateDbFailure

from ..capture.test_record_media_result_settlement import _TrackedCommitFailure
from ..capture.test_start_close_save_recovery import (
    _assert_complete_group, _attempts, _history, start_close_world,
)
from . import test_capture_completion_default_recovery as completion_defaults
from .test_capture_completion_default_recovery import _default_completion_world, _selected_flow
from .test_read_default_consumers import _BeforeBusinessCandidates, _CandidateClock
from .test_recording_results_cancellation import _BeforeCandidates, _CandidateBoundary
from .test_recording_results_saved_consumers import _UnavailableOriginalKey


pytestmark = pytest.mark.asyncio
_MODES = ("unknown", "lowered", "query_exhausted")
_ENTRANCES = ("normal", "residual", "restricted", "normal-cancel", "restricted-cancel")


async def _default_start_close_world(tmp_path, monkeypatch, mode):
    """在首次复合申请前，把实际生产者接入默认会话的空责任集合。

    默认装配缺少设备声明时，其工厂先处理绑定失败。原 START/QUERY
    已由真实 handler 保存，因此外层申请由原生产者形成；三个默认
    工厂分别构建，实际五个 flow 接手核实，不借工厂取得新设备资格。
    """
    world = await start_close_world(tmp_path, mode)
    world.mode = mode
    actual_config = completion_defaults._config

    def bound_config(home):
        config = actual_config(home)
        return replace(config, paths=replace(config.paths,
            staging=world.metadata.staging_path, ready=world.metadata.ready_path,
            processing=world.metadata.processing_path))

    # 原 fixture 的部署目录属于数据库身份；只让装配配置使用相同绑定。
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
        # 原等待锚点来自已保存的真实调用结果，与完整申请分别交接。
        deps.capture_retry_gate.anchors.update(world.runtime.retry_gate.anchors)
        world.runtime.retry_gate = deps.capture_retry_gate
        world.runtime.pending_capture_completions = deps.capture_completions
        return world, deps, context, methods, now, runtime_calls
    except BaseException:
        world.owned.connection.close()
        lifecycle.close_runtime(deps)
        raise


def _assert_no_new_device_call(world, methods, query_calls, runtime_calls):
    assert len(world.driver.calls) == 1
    assert (0 if world.query is None else len(world.query.calls)) == query_calls
    assert len(runtime_calls) == 3
    for method in methods:
        method.assert_not_called()


def _assert_original_inputs(inputs, finish, action_finish, key):
    assert inputs == [(finish, action_finish, key), (finish, action_finish, key)]
    assert inputs[1][0] is finish and inputs[1][1] is action_finish


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("after_commit", [False, True], ids=["commit-before", "commit-after"])
@pytest.mark.parametrize("entrance", _ENTRANCES)
async def test_default_entry_confirms_original_start_close_before_candidates(
        tmp_path, monkeypatch, mode, after_commit, entrance):
    world, deps, context, methods, now, runtime_calls = await _default_start_close_world(
        tmp_path, monkeypatch, mode)
    owned, reopened = world.owned, None
    before_attempts, before_history = _attempts(owned), _history(owned)
    query_calls = 0 if world.query is None else len(world.query.calls)
    actual_save = CaptureRepository.close_start
    inputs, receipts, faults = [], [], []

    def save(repository, finish, action_finish, key, current):
        inputs.append((finish, action_finish, key))
        if len(inputs) == 1:
            fault = _TrackedCommitFailure(current.connection, after_commit)
            faults.append(fault)
            current = replace(current, connection=fault)
        receipt = actual_save(repository, finish, action_finish, key, current)
        receipts.append(receipt)
        return receipt

    monkeypatch.setattr(CaptureRepository, "close_start", save)
    try:
        with pytest.raises(ConsistencyError):
            await capture_handler("camera_record")(12, world.runtime)
        assert len(inputs) == len(receipts) == len(faults) == 1
        assert receipts[0].kind is DbOutcomeKind.UNKNOWN and faults[0].commit_calls == 1
        finish, action_finish, key = inputs[0]
        pending = deps.capture_completions[12]
        assert pending.request.finish is finish and pending.request.action_finish is action_finish
        assert pending.key == key
        assert finish.occurred_at == action_finish.occurred_at == world.formed_at
        owned.connection.close()
        reopened = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
        assert reopened.metadata == world.metadata
        first_events = saved_transaction_events(reopened.connection, key)
        assert (first_events is not None) is after_commit
        assert reopened.connection.execute("SELECT status FROM actions WHERE id=12").fetchone() == (
            4 if after_commit else 2,)
        anchors = dict(deps.capture_retry_gate.anchors)
        deps.config = replace(deps.config, devices={"cam-1": {"driver": "alternate-camera"}})
        world.runtime.start_config = replace(world.runtime.start_config, max_attempts=9)
        world.runtime.query_config = replace(world.runtime.query_config, max_attempts=9)
        world.wall[0] += 100_000_000
        world.mono[0] += 100_000_000_000
        now[0] += 100_000_000
        opened = []

        def saved():
            _assert_original_inputs(inputs, finish, action_finish, key)
            assert receipts[1].kind is DbOutcomeKind.COMPLETED and receipts[1].value is None
            assert world.runtime.pending_capture_completions is deps.capture_completions
            assert deps.capture_completions == {}
            assert deps.capture_retry_gate.anchors == {
                responsibility: anchor for responsibility, anchor in anchors.items()
                if responsibility not in finish.responsibility_keys}
            events = _assert_complete_group(world, reopened, finish, key)
            if first_events is not None:
                assert events == first_events
            assert _attempts(reopened) == before_attempts
            assert _history(reopened)[:len(before_history)] == before_history
            _assert_no_new_device_call(world, methods, query_calls, runtime_calls)

        original_open = context.open_connection

        def open_connection():
            current = original_open()
            assert current.metadata == world.metadata
            assert current.connection is not reopened.connection
            opened.append(current)
            return replace(current, connection=_CandidateBoundary(current.connection, saved))

        context.open_connection = open_connection
        context.clock = _CandidateClock(saved)
        with pytest.raises((_BeforeCandidates, _BeforeBusinessCandidates)):
            await _selected_flow(context, entrance)(context)
        assert len(opened) == 1
        saved()
    finally:
        (reopened if reopened is not None else owned).connection.close()
        lifecycle.close_runtime(deps)


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("confirmation_unknown", [False, True], ids=["rolled-back", "unknown"])
@pytest.mark.parametrize("entrance", ["normal", "restricted-cancel"])
async def test_default_entry_keeps_start_close_when_original_key_is_unavailable(
        tmp_path, monkeypatch, mode, confirmation_unknown, entrance):
    world, deps, context, methods, now, runtime_calls = await _default_start_close_world(
        tmp_path, monkeypatch, mode)
    owned, reopened = world.owned, None
    actual_save = CaptureRepository.close_start
    inputs, receipts, faults = [], [], []
    query_calls = 0 if world.query is None else len(world.query.calls)

    def save(repository, finish, action_finish, key, current):
        inputs.append((finish, action_finish, key))
        fault = (_TrackedCommitFailure(current.connection, True) if len(inputs) == 1
                 else _UnavailableOriginalKey(current.connection, key, confirmation_unknown))
        faults.append(fault)
        receipt = actual_save(repository, finish, action_finish, key,
                              replace(current, connection=fault))
        receipts.append(receipt)
        return receipt

    monkeypatch.setattr(CaptureRepository, "close_start", save)
    try:
        with pytest.raises(ConsistencyError):
            await capture_handler("camera_record")(12, world.runtime)
        assert len(inputs) == len(receipts) == len(faults) == 1
        assert receipts[0].kind is DbOutcomeKind.UNKNOWN and faults[0].commit_calls == 1
        finish, action_finish, key = inputs[0]
        pending = deps.capture_completions[12]
        assert pending.request.finish is finish and pending.request.action_finish is action_finish
        assert pending.key == key
        owned.connection.close()
        reopened = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
        assert reopened.metadata == world.metadata
        assert saved_transaction_events(reopened.connection, key) is not None
        assert reopened.connection.execute("SELECT status FROM actions WHERE id=12").fetchone() == (4,)
        before = tuple(reopened.connection.iterdump())
        anchors = dict(deps.capture_retry_gate.anchors)
        deps.config = replace(deps.config, devices={"cam-1": {"driver": "alternate-camera"}})
        world.runtime.start_config = replace(world.runtime.start_config, max_attempts=9)
        world.runtime.query_config = replace(world.runtime.query_config, max_attempts=9)
        world.wall[0] += 100_000_000
        world.mono[0] += 100_000_000_000
        now[0] += 100_000_000
        original_open = context.open_connection

        def open_connection():
            current = original_open()
            return replace(current, connection=_CandidateBoundary(current.connection,
                lambda: pytest.fail("原 START 完整申请未核实前不得读取候选")))

        context.open_connection = open_connection
        context.clock = _CandidateClock(lambda: pytest.fail("原 START 完整申请未核实前不得取得时刻"))
        with pytest.raises(StateDbFailure):
            await _selected_flow(context, entrance)(context)
        _assert_original_inputs(inputs, finish, action_finish, key)
        assert faults[1].key_reads == 1
        assert receipts[1].kind is (
            DbOutcomeKind.UNKNOWN if confirmation_unknown else DbOutcomeKind.ROLLED_BACK)
        assert deps.capture_completions[12] is pending
        assert deps.capture_retry_gate.anchors == anchors
        assert tuple(reopened.connection.iterdump()) == before
        _assert_no_new_device_call(world, methods, query_calls, runtime_calls)
    finally:
        (reopened if reopened is not None else owned).connection.close()
        lifecycle.close_runtime(deps)

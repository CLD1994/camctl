"""默认五个入口先核普通拍摄完整申请，终态和绑定变化不跳过原键。"""

from dataclasses import replace
from pathlib import Path

import pytest

from camctl.bootstrap import capture_assembly, lifecycle
from camctl.acceptance.service import CommandMode
from camctl.capture.handlers import capture_handler
from camctl.contracts.values import ConsistencyError
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import saved_transaction_events
from camctl.session.outcome import SessionOutcome
from camctl.session.service import StateDbFailure

from ..capture.test_capture_completion_save_recovery import completion_method, completion_world
from ..capture.test_record_media_result_settlement import _TrackedCommitFailure
from .test_closed_result_binding_recovery import _unavailable_device_ports
from ..capture.media_retry_fixtures import _Catalog
from .test_media_assembly import _config
from .test_read_default_consumers import _BeforeBusinessCandidates, _CandidateClock
from .test_recording_results_cancellation import _BeforeCandidates, _CandidateBoundary
from .test_recording_results_saved_consumers import _UnavailableOriginalKey


pytestmark = pytest.mark.asyncio
_ENTRANCES = ("normal", "residual", "restricted", "normal-cancel", "restricted-cancel")
_CONSUMERS = ("photo", "timelapse", "canceled_timelapse", "binding")


async def _default_completion_world(world, monkeypatch, *, binding):
    """实际 lifecycle 装配三个工厂及所有 flow，仅隔离主循环和设备边界。"""
    from camctl.devices.drivers import runtime as driver_runtime

    config = _config(world.path.parent)
    assert Path(config.paths.state_db) == world.path
    if binding:
        config = replace(config, devices={})
    registry, methods = _unavailable_device_ports()
    monkeypatch.setattr(driver_runtime, "current_registry", lambda: registry)
    now = [world.formed_at + 5_000_000]
    factories, contexts, runtime_calls = [], [], []
    original_factory = capture_assembly.session_capture_assembly

    def capture_factory(**kwargs):
        actual = original_factory(**kwargs, wall_us=lambda: now[0],
            monotonic_ns=lambda: 99_000_000_000)

        def factory(current, device_id):
            runtime_calls.append((current, device_id))
            return actual(current, device_id)

        factories.append(factory)
        return factory

    async def session(context, _source):
        contexts.append(context)
        return SessionOutcome(succeeded=True)

    monkeypatch.setattr(capture_assembly, "session_capture_assembly", capture_factory)
    monkeypatch.setattr(lifecycle, "run_session", session)
    deps = lifecycle.build_runtime(CommandMode.RUN, config, catalog=_Catalog())
    try:
        assert deps.startup_error is None
        assert (await lifecycle.execute_command(deps, None)).succeeded
        assert len(factories) == 3 and len(contexts) == 1
        return deps, factories, contexts[0], methods, now, runtime_calls
    except BaseException:
        lifecycle.close_runtime(deps)
        raise


def _selected_flow(context, entrance):
    return {
        "normal": context.flows["scheduling"],
        "residual": context.flows["residual"],
        "restricted": context.restricted_flows["winddown"],
        "normal-cancel": context.flows["cancel"],
        "restricted-cancel": context.restricted_flows["cancel"],
    }[entrance]


@pytest.mark.parametrize("consumer", _CONSUMERS)
@pytest.mark.parametrize("after_commit", [False, True])
@pytest.mark.parametrize("entrance", _ENTRANCES)
async def test_default_entry_confirms_full_capture_before_candidates(
        tmp_path, monkeypatch, consumer, after_commit, entrance):
    # 候选读取提前、原终态被过滤后不核原键，或工厂不共享集合都会失败。
    world = await completion_world(tmp_path, monkeypatch, consumer)
    deps, factories, context, methods, now, runtime_calls = await _default_completion_world(
        world, monkeypatch, binding=consumer == "binding")
    owned = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    reopened = None
    try:
        runtime = factories[0](owned, "cam-1")
        assert runtime is not None
        before_attempts = owned.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall()
        before_files = owned.connection.execute("SELECT * FROM device_files ORDER BY id").fetchall()
        method = completion_method(consumer)
        actual_save = getattr(CaptureRepository, method)
        inputs, receipts, faults = [], [], []

        def save(repository, request, key, current):
            inputs.append((request, key))
            if len(inputs) == 1:
                fault = _TrackedCommitFailure(current.connection, after_commit)
                faults.append(fault)
                result = actual_save(repository, request, key, replace(current, connection=fault))
            else:
                result = actual_save(repository, request, key, current)
            receipts.append(result)
            return result

        monkeypatch.setattr(CaptureRepository, method, save)
        with pytest.raises(ConsistencyError):
            await capture_handler(world.handler)(world.action_id, runtime)
        assert len(inputs) == len(receipts) == len(faults) == 1
        assert receipts[0].kind is DbOutcomeKind.UNKNOWN and faults[0].commit_calls == 1
        request, key = inputs[0]
        assert runtime.pending_capture_completions is deps.capture_completions
        assert deps.capture_completions[world.action_id].request is request
        owned.connection.close()
        reopened = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
        first_events = saved_transaction_events(reopened.connection, key)
        assert (first_events is not None) is after_commit
        # 后续配置不能改变原绑定错误、草稿或决定时刻；也不取得新驱动资格。
        deps.config = replace(deps.config, devices={"cam-1": {"driver": "alternate-camera"}})
        now[0] += 100_000_000
        opened = []

        def saved():
            assert inputs == [(request, key), (request, key)] and inputs[1][0] is request
            assert receipts[1].kind is DbOutcomeKind.COMPLETED, receipts[1].error
            assert deps.capture_completions == runtime.pending_capture_completions == {}
            events = saved_transaction_events(reopened.connection, key)
            assert events is not None and all(event["occurred_at"] == request.occurred_at for event in events)
            if first_events is not None:
                assert events == first_events
            assert reopened.connection.execute(
                "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(key),)).fetchone() == (1,)
            status = 6 if consumer in ("binding", "canceled_timelapse") else 4
            assert reopened.connection.execute("SELECT status FROM actions WHERE id=?", (world.action_id,)).fetchone() == (status,)
            assert reopened.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (1,)
            assert reopened.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall() == before_attempts
            assert reopened.connection.execute("SELECT * FROM device_files ORDER BY id").fetchall() == before_files
            assert saved_transaction_events(reopened.connection, world.key) == world.close_events
            assert len(runtime_calls) == 1
            for method in methods:
                method.assert_not_called()

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


@pytest.mark.parametrize("consumer", _CONSUMERS)
@pytest.mark.parametrize("confirmation_unknown", [False, True])
@pytest.mark.parametrize("entrance", ["normal", "restricted-cancel"])
async def test_default_entry_keeps_capture_when_original_key_cannot_be_read(
        tmp_path, monkeypatch, consumer, confirmation_unknown, entrance):
    world = await completion_world(tmp_path, monkeypatch, consumer)
    deps, factories, context, methods, _now, runtime_calls = await _default_completion_world(
        world, monkeypatch, binding=consumer == "binding")
    owned = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    reopened = None
    try:
        runtime = factories[0](owned, "cam-1")
        method = completion_method(consumer)
        actual_save = getattr(CaptureRepository, method)
        inputs, receipts, failures = [], [], []

        def save(repository, request, key, current):
            inputs.append((request, key))
            fault = (_TrackedCommitFailure(current.connection, True) if len(inputs) == 1
                else _UnavailableOriginalKey(current.connection, key, confirmation_unknown))
            failures.append(fault)
            result = actual_save(repository, request, key, replace(current, connection=fault))
            receipts.append(result)
            return result

        monkeypatch.setattr(CaptureRepository, method, save)
        with pytest.raises(ConsistencyError):
            await capture_handler(world.handler)(world.action_id, runtime)
        assert receipts[0].kind is DbOutcomeKind.UNKNOWN and failures[0].commit_calls == 1
        request, key = inputs[0]
        pending = deps.capture_completions[world.action_id]
        owned.connection.close()
        reopened = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
        before = tuple(reopened.connection.iterdump())
        original_open = context.open_connection

        def open_connection():
            current = original_open()
            return replace(current, connection=_CandidateBoundary(current.connection,
                lambda: pytest.fail("原申请未核实前不得查询候选")))

        context.open_connection = open_connection
        context.clock = _CandidateClock(lambda: pytest.fail("原申请未核实前不得取得候选时刻"))
        with pytest.raises(StateDbFailure):
            await _selected_flow(context, entrance)(context)
        assert inputs == [(request, key), (request, key)]
        assert failures[1].key_reads == 1
        assert receipts[1].kind is (DbOutcomeKind.UNKNOWN if confirmation_unknown else DbOutcomeKind.ROLLED_BACK)
        assert deps.capture_completions[world.action_id] is pending
        assert tuple(reopened.connection.iterdump()) == before and len(runtime_calls) == 1
        for method in methods:
            method.assert_not_called()
    finally:
        (reopened if reopened is not None else owned).connection.close()
        lifecycle.close_runtime(deps)

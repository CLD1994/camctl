"""取消流程在保存新成功之前必须核实本会话已取得的媒体申请。"""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.bootstrap.flows import cancel_flow, capture_flow
from camctl.capture.media_flow import run_recording_media, resume_media_saves
from camctl.capture.processing import CheckPhase
from camctl.contracts.values import ConsistencyError
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.cancellation import CancellationRepository
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.transactions import saved_transaction_events
from camctl.session.service import run_session

from ..capture.media_retry_fixtures import _Catalog, media_pipeline as pipeline  # noqa: F401
from ..support.bootstrap_consumers import consumer_world, TransactionFault, cancel_original
from ..support.session import accept_plan, session_context

pytestmark = pytest.mark.asyncio


async def _one_session_step(world, flow):
    """真实会话完成一次公开消费者后，在下一业务轮之前结束专项。"""
    class StepComplete(BaseException):
        pass

    async def step(context):
        await flow(context)
        raise StepComplete()

    context = session_context(world.path,
        clock=SimpleNamespace(utc_micros=lambda: world.wall[0]),
        catalog=_Catalog(), flows={"consumer": step})
    try:
        return await run_session(context, None)
    except StepComplete:
        return None


@pytest.mark.parametrize("confirmation", ["saved", "rejected"])
@pytest.mark.parametrize("commit_after", [False, True], ids=["uncommitted", "committed"])
async def test_default_cancel_confirms_original_media_before_new_cancel_success(
        pipeline, capture_resources, monkeypatch, confirmation, commit_after):
    world = await consumer_world(pipeline, capture_resources, branch="check")
    original_save = CaptureRepository.save_check_result
    inputs, order = [], []

    def save(repository, command, key, current):
        if command.phase is CheckPhase.RUNNING:
            return original_save(repository, command, key, current)
        inputs.append((command, key))
        if len(inputs) == 1:
            fault = TransactionFault(current.connection, after_commit=commit_after)
            receipt = original_save(repository, command, key, replace(current, connection=fault))
            assert receipt.kind is DbOutcomeKind.UNKNOWN and fault.commit_calls == 1
            return receipt
        order.append("media_confirmation")
        if confirmation == "rejected":
            return DbOutcome(DbOutcomeKind.ROLLED_BACK,
                error=ConsistencyError("original media history unavailable"))
        return original_save(repository, command, key, current)

    monkeypatch.setattr(CaptureRepository, "save_check_result", save)
    try:
        with pytest.raises(ConsistencyError):
            await run_recording_media(world.runtime.media, 1, 1, world.source_id)
        assert len(inputs) == 1 and world.tools() == ["probe"]
        await world.reconnect()
        original_facts = saved_transaction_events(world.owned.connection, inputs[0][1])
        assert (original_facts is not None) is commit_after
        if commit_after:
            await cancel_original(world)
        world.wall[0] += 30
        instant = datetime.fromtimestamp(world.wall[0] // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        await accept_plan(world.database, world.path.parent, _Catalog(), {
            "request_id": "3", "created_at": instant, "name": "取消原媒体动作",
            "actions": [{"name": "新取消", "type": "cancel_task", "params": {
                "target": {"action_instance_id": "1"}}}]},
            SimpleNamespace(utc_micros=lambda: world.wall[0]))
        newest, = world.owned.connection.execute("SELECT MAX(id) FROM actions WHERE type=6").fetchone()
        assert world.owned.connection.execute("SELECT status FROM actions WHERE id=?", (newest,)).fetchone() == (1,)
        for method in ("start_cancel_action", "apply_cancel_target", "finish_cancel_action"):
            original = getattr(CancellationRepository, method)

            def observed(repository, command, key, current, *, actual=original, name=method, **options):
                order.append(name)
                return actual(repository, command, key, current, **options)

            monkeypatch.setattr(CancellationRepository, method, observed)

        async def media(database):
            await resume_media_saves(database, world.collections["pending_media_results"], executor=world.executor)

        cancel = cancel_flow(staging=Path(world.config.paths.staging), ready=Path(world.config.paths.ready),
            processing=Path(world.config.paths.processing), resume_media_results=media)
        outcome = await _one_session_step(world, cancel)
        if confirmation == "rejected":
            assert outcome.reason == "state_db_error"
            assert world.owned.connection.execute("SELECT status FROM actions WHERE id=?", (newest,)).fetchone() == (1,)
            assert order == ["media_confirmation"]
        else:
            assert outcome is None, outcome.details["error"]
            assert order[0] == "media_confirmation"
            if not commit_after:
                assert world.owned.connection.execute("SELECT status FROM actions WHERE id=?", (newest,)).fetchone() == (2,)
                assert world.owned.connection.execute("SELECT status,cancel_requested FROM actions WHERE id=1").fetchone() == (2, 1)
                scheduling = capture_flow(world.factory)

                async def finish_target(context):
                    await scheduling(context)
                    await scheduling.settle()

                await _one_session_step(world, finish_target)
                assert world.owned.connection.execute("SELECT status,cancel_requested FROM actions WHERE id=1").fetchone() == (6, 1)
                assert await _one_session_step(world, cancel) is None
            assert "start_cancel_action" in order and "finish_cancel_action" in order
            assert world.owned.connection.execute("SELECT status FROM actions WHERE id=?", (newest,)).fetchone() == (3,)
        assert len(inputs) == 2 and inputs[1] == inputs[0]
        assert world.tools() == ["probe"] and len(world.reader.requests) == 1
        assert world.owned.connection.execute("SELECT status,cancel_requested FROM actions WHERE id=1").fetchone() == (
            (6, 1) if commit_after or confirmation == "saved" else (2, 0))
    finally:
        await world.close()
        world.owned.connection.close()

"""普通拍摄终态在真实提交未知后保留完整申请，重入先核原键。"""

from dataclasses import replace

import pytest

from camctl.capture.handlers import capture_handler
from camctl.contracts.values import ConsistencyError
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import saved_transaction_events

from .test_cancel_timelapse_exhaustion_files import _apply_public_cancel
from .test_closed_result_local_consumption import closed_consumer_world, fresh_closed_runtime
from .test_record_media_result_settlement import _TrackedCommitFailure


pytestmark = pytest.mark.asyncio


async def completion_world(tmp_path, monkeypatch, consumer):
    """经真实调用、原结果及文件保存准备申请，返回关闭连接后的业务前置。"""
    world = await closed_consumer_world(tmp_path, monkeypatch,
        consumer="timelapse" if consumer == "binding" else consumer,
        history="latest_complete")
    if consumer == "binding":
        current = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            _apply_public_cancel(current, world.action_id, world.formed_at + 10)
            assert current.connection.execute(
                "SELECT status,cancel_requested FROM actions WHERE id=?", (world.action_id,)).fetchone() == (2, 1)
        finally:
            current.connection.close()
    return world


def completion_method(consumer):
    return ("finish_binding_failure" if consumer == "binding" else
            "finish_canceled_capture" if consumer == "canceled_timelapse" else "finish_capture")


@pytest.mark.parametrize("consumer", ["photo", "timelapse", "canceled_timelapse", "binding"])
@pytest.mark.parametrize("after_commit", [False, True])
async def test_completion_unknown_keeps_full_request_for_fresh_owned(
        tmp_path, monkeypatch, consumer, after_commit):
    # 删除保存前持有、在终态早退前跳过核验，或重建 key/T1 均破坏本契约。
    world = await completion_world(tmp_path, monkeypatch, consumer)
    owned = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    reopened = None
    try:
        runtime, methods = fresh_closed_runtime(owned, world, "missing")
        before_attempts = owned.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall()
        before_files = owned.connection.execute("SELECT * FROM device_files ORDER BY id").fetchall()
        before_history = owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()
        method = completion_method(consumer)
        actual_save = getattr(CaptureRepository, method)
        inputs, receipts, faults = [], [], []

        def save(repository, request, key, current):
            inputs.append((request, key))
            if len(inputs) == 1:
                fault = _TrackedCommitFailure(current.connection, after_commit)
                faults.append(fault)
                receipt = actual_save(repository, request, key, replace(current, connection=fault))
            else:
                receipt = actual_save(repository, request, key, current)
            receipts.append(receipt)
            return receipt

        monkeypatch.setattr(CaptureRepository, method, save)
        advance = capture_handler(world.handler)
        with pytest.raises((ConsistencyError, AssertionError)):
            await advance(world.action_id, runtime)
        assert len(inputs) == len(receipts) == len(faults) == 1
        assert receipts[0].kind is DbOutcomeKind.UNKNOWN and faults[0].commit_calls == 1
        request, key = inputs[0]
        assert request.action_id == world.action_id and request.occurred_at == world.formed_at + 5_000_000
        assert len(request.drafts) == 1 and request.drafts[0].file.device_file_id == world.file_id
        pending = getattr(runtime, "pending_capture_completions", {}).get(world.action_id)
        assert pending is not None, "提交未知后必须继续持有原完整拍摄终态申请"
        assert pending.request is request and pending.key == key
        owned.connection.close()
        reopened = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
        assert reopened.metadata == world.metadata
        first_events = saved_transaction_events(reopened.connection, key)
        assert (first_events is not None) is after_commit
        expected_status = 6 if consumer in ("canceled_timelapse", "binding") else 4
        assert reopened.connection.execute("SELECT status FROM actions WHERE id=?", (world.action_id,)).fetchone() == (
            expected_status if after_commit else 2,)
        assert reopened.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (int(after_commit),)
        # 同会话的共有责任保持；换 Owned 不复制或重新构造完整申请。
        runtime.owned = reopened
        runtime.wall_us = lambda: world.formed_at + 100_000_000
        await advance(world.action_id, runtime)
        assert inputs == [(request, key), (request, key)]
        assert inputs[1][0] is request
        assert receipts[1].kind is DbOutcomeKind.COMPLETED, receipts[1].error
        assert runtime.pending_capture_completions == {}
        events = saved_transaction_events(reopened.connection, key)
        assert events is not None and all(event["occurred_at"] == request.occurred_at for event in events)
        if first_events is not None:
            assert events == first_events
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(key),)).fetchone() == (1,)
        assert reopened.connection.execute("SELECT status FROM actions WHERE id=?", (world.action_id,)).fetchone() == (expected_status,)
        output_id, device_file_id, output_txn = reopened.connection.execute(
            "SELECT o.id,o.device_file_id,e.transaction_id FROM outputs o"
            " JOIN history_events e ON e.id=o.created_event_id").fetchone()
        assert output_id > 0 and device_file_id == world.file_id
        assert output_txn == events[0]["transaction"].txn_id
        assert reopened.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall() == before_attempts
        assert reopened.connection.execute("SELECT * FROM device_files ORDER BY id").fetchall() == before_files
        assert reopened.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()[:len(before_history)] == before_history
        for device_method in methods:
            device_method.assert_not_called()
        completed = tuple(reopened.connection.iterdump())
        await advance(world.action_id, runtime)
        assert len(inputs) == 2 and tuple(reopened.connection.iterdump()) == completed
    finally:
        if reopened is not None:
            reopened.connection.close()
        else:
            owned.connection.close()

"""默认入口形成的实际READ和所属绑定失败申请跨UNKNOWN保留原身份。"""

from dataclasses import replace
import json

import pytest

from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.transactions import saved_transaction_events

from ..capture.media_retry_fixtures import media_pipeline as pipeline  # noqa: F401
from ..support.bootstrap_consumers import TransactionFault
from .test_read_default_consumers import _snapshot
from .test_read_default_raw_binding import _raw_world, _binding_port, _assert_raw_preserved

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("entrance", ["normal", "residual"])
@pytest.mark.parametrize("stage", ["finish", "child"])
@pytest.mark.parametrize("after_commit", [False, True])
async def test_default_raw_binding_unknown_retries_original_inputs(
        pipeline, capture_resources, monkeypatch, entrance, stage, after_commit):
    raw = await _raw_world(pipeline, capture_resources, monkeypatch)
    world, ticket = raw.world, raw.ticket
    finishes, children, returns, faults = [], [], [], []
    actual_finish, actual_child = OperationRepository.finish_binding_read, CaptureRepository.finish_binding_failure
    formed_at = world.wall[0]

    def submit(actual, request, key, current, operation, inputs):
        inputs.append((request, key))
        if operation == stage and len(inputs) == 1:
            fault = TransactionFault(current.connection, after_commit=after_commit)
            faults.append(fault)
            result = actual(request, key, replace(current, connection=fault))
            returns.append(result)
            assert result.kind is DbOutcomeKind.UNKNOWN and fault.commit_calls == 1
            assert current.connection.in_transaction is not after_commit
            return result
        return actual(request, key, current)

    def finish(repository, request, key, current, *, current_identity=None):
        assert request.ticket == ticket
        return submit(lambda *args: actual_finish(repository, *args, current_identity=current_identity), request, key,
            current, "finish", finishes)

    def child(repository, request, key, current):
        assert saved_transaction_events(current.connection, finishes[0][1]) is not None
        return submit(lambda *args: actual_child(repository, *args), request, key,
            current, "child", children)

    monkeypatch.setattr(OperationRepository, "finish_binding_read", finish)
    monkeypatch.setattr(CaptureRepository, "finish_binding_failure", child)
    try:
        await world.replay(entrance, failure=True, binding=_binding_port("missing"))
        assert len(faults) == len(returns) == len(finishes) == 1
        assert len(children) == (0 if stage == "finish" else 1)
        original_finish, finish_key = finishes[0]
        assert original_finish.occurred_at == raw.ended_at and formed_at > raw.ended_at
        if children:
            assert children[0][0].occurred_at == formed_at and children[0][1] != finish_key
        assert (saved_transaction_events(world.owned.connection, finish_key) is not None) is (
            stage == "child" or after_commit)
        if children:
            assert (saved_transaction_events(world.owned.connection, children[0][1]) is not None) is after_commit
        assert world.owned.connection.execute(
            "SELECT status FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone() == (
                1 if stage == "finish" and not after_commit else 2,)
        assert world.owned.connection.execute(
            "SELECT status FROM actions WHERE id=?", (raw.action_id,)).fetchone() == (
                4 if stage == "child" and after_commit else 2,)
        _assert_raw_preserved(raw, released=stage == "child" and after_commit)

        # 原申请先于本次已恢复的设备资格，不能重算时间、键或所属结论。
        world.wall[0] += 5000
        await world.replay(entrance, binding=_binding_port("matched"))
        assert len(finishes) == (2 if stage == "finish" else 1)
        assert all(pair == (original_finish, finish_key) for pair in finishes)
        assert len(children) == (1 if stage == "finish" else 2)
        original_child, child_key = children[0]
        assert all(pair == (original_child, child_key) for pair in children)
        assert original_child.occurred_at == formed_at and original_finish.occurred_at == raw.ended_at
        assert child_key != finish_key
        for key in (finish_key, child_key):
            assert world.owned.connection.execute(
                "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(key),)).fetchone() == (1,)
        status, error, result = world.owned.connection.execute(
            "SELECT status,error_json,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone()
        assert (status, error) == (2, None)
        assert json.loads(result)["settlement"] == {
            "basis": "observed", "evidence": {"type": "read_returned", "version": 1, "data": {}}}
        details = {"device_id": "cam-1", "expected_driver_id": "camctl-adb", "reason": "missing"}
        expected = {"code": "device_binding_unavailable", "stage": "execution", "details": details}
        assert _snapshot(world.owned, "operation_runs", ticket.run_id)["error_json"] == expected
        assert world.owned.connection.execute("SELECT status FROM actions WHERE id=?", (raw.action_id,)).fetchone() == (4,)
        check, media = world.owned.connection.execute(
            "SELECT check_state,media_json FROM recording_processing WHERE id=?", (raw.processing_id,)).fetchone()
        assert check == 4 and json.loads(media)["error"] == expected
        _assert_raw_preserved(raw, released=True)
    finally:
        await world.close()
        world.owned.connection.close()

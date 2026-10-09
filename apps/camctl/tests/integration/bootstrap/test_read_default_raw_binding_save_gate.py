"""新形成的实际 READ／绑定失败申请持续失去保存依据时阻止业务推进。"""

from dataclasses import replace
import sqlite3

import pytest

from camctl.bootstrap import lifecycle
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories import capture as capture_repository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.transaction import saved_transaction_events
from camctl.session.service import StateDbFailure

from ..capture.media_retry_fixtures import media_pipeline as pipeline  # noqa: F401
from .test_read_default_consumers import _snapshot
from .test_read_default_raw_binding_recovery import _assert_preserved, _fresh, _prepare_raw_unknown
from .test_read_default_save_gate import _NoBusinessClock, _ReadGateConnection


pytestmark = pytest.mark.asyncio


class _BindingRollbackConnection(_ReadGateConnection):
    """完整 child 的实际检查投影失败，真实 ROLLBACK 必须恢复整个事务。"""

    def execute(self, sql, parameters=()):
        normalized = " ".join(sql.split())
        if normalized.startswith("UPDATE recording_processing SET ") and "check_state = ?" in normalized:
            self.projection_hits += 1
            self.projection_error = sqlite3.OperationalError("绑定失败 child 的检查投影保存失败")
            raise self.projection_error
        return super().execute(sql, parameters)


@pytest.mark.parametrize("entrance", ["normal", "residual"])
@pytest.mark.parametrize("branch", ["finish_unknown", "child_rollback"])
async def test_raw_binding_save_gate_preserves_original_requests(pipeline, monkeypatch, entrance, branch):
    stage = "finish" if branch == "finish_unknown" else "child"
    state = await _prepare_raw_unknown(pipeline, monkeypatch, entrance, stage, stage == "finish")
    try:
        pending = state.prepared[0]
        key = pending.key if stage == "finish" else pending.business.key
        original_holder = (state.flow.pending_read_results[(state.ticket.run_id, state.ticket.attempt_id)]
            if stage == "finish" else state.flow.pending_read_business[(state.action_id, "capture_binding_failure")])
        before = {table: _snapshot(state.observer, table, identity) for table, identity in (
            ("actions", state.action_id), ("recording_processing", state.processing_id),
            ("file_copies", state.copy_id), ("operation_runs", state.ticket.run_id))}
        history = state.observer.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()
        receipts, attempts = [], []
        if stage == "finish":
            previous = OperationRepository.finish_attempt

            def save(repository, request, operation_key, current):
                receipt = previous(repository, request, operation_key, current)
                receipts.append(receipt)
                return receipt

            monkeypatch.setattr(OperationRepository, "finish_attempt", save)
        else:
            # child 已持有原 bound method；在该方法实际使用的事务边界观察，
            # 保留其原 save callback、完整 request 和 key。
            previous_commit = capture_repository.commit_operation

            def commit(command, operation_key, current):
                assert operation_key == key
                receipt = previous_commit(command, operation_key, current)
                receipts.append(receipt)
                return receipt

            monkeypatch.setattr(capture_repository, "commit_operation", commit)
        clock = _NoBusinessClock()
        state.context.clock = clock

        def open_connection():
            current = state.original_open()
            fault = (_ReadGateConnection(current.connection, slot_rollback=False, finish_key=key)
                if stage == "finish" else _BindingRollbackConnection(current.connection, slot_rollback=False))
            attempts.append(fault)
            return replace(current, connection=fault)

        state.context.open_connection = open_connection
        # 同一原责任在两轮仍无法可靠保存；不得退役、换键或让候选进入下一阶段。
        for iteration in range(2):
            state.time[0] += 1000
            with pytest.raises(StateDbFailure):
                await state.selected(state.context)
            assert len(attempts) == len(receipts) == iteration + 1
            fault = attempts[-1]
            assert fault.rollback_hits == 1 and fault.candidate_queries == []
            if stage == "finish":
                assert receipts[-1].kind is DbOutcomeKind.UNKNOWN
                assert fault.verification_hits == 1 and fault.projection_hits == 0
                assert state.flow.pending_read_results[(state.ticket.run_id, state.ticket.attempt_id)] == original_holder
                assert state.flow.pending_read_business == {} and state.children == []
                assert all(pair == (pending.finish, pending.key) for pair in state.finishes)
            else:
                assert receipts[-1].kind == DbOutcomeKind.ROLLED_BACK.value
                assert receipts[-1].error is fault.projection_error
                assert fault.projection_hits == 1 and fault.verification_hits == 0
                assert state.flow.pending_read_results == {}
                assert state.flow.pending_read_business[(state.action_id, "capture_binding_failure")] == original_holder
                assert len(state.finishes) == 1
                assert all(pair == (pending.business.request, pending.business.key) for pair in state.children)
            assert state.clock_reads == [pending.finish.occurred_at] and clock.calls == 0
            current = _fresh(state)
            assert (saved_transaction_events(current.connection, key) is not None) is (stage == "finish")
            assert before == {table: _snapshot(current, table, identity) for table, identity in (
                ("actions", state.action_id), ("recording_processing", state.processing_id),
                ("file_copies", state.copy_id), ("operation_runs", state.ticket.run_id))}
            assert current.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall() == history
            _assert_preserved(state, released=False)
    finally:
        state.observer.connection.close()
        lifecycle.close_runtime(state.deps)

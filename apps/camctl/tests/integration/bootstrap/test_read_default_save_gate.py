"""原 Finish 核实持续未知或原 slot 保存回滚时，默认入口保留责任并停止筛选。"""

from dataclasses import replace
import sqlite3

import pytest

from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.transaction import saved_transaction_events
from camctl.session.service import StateDbFailure
from camctl.bootstrap import lifecycle

from ..capture.media_retry_fixtures import media_pipeline as pipeline  # noqa: F401
from .test_read_default_consumers import _held_read, _snapshot


pytestmark = pytest.mark.asyncio


class _ReadGateConnection:
    """透传真实查询；故障和禁止继续业务分别在真实 SQLite 边界观测。"""

    def __init__(self, connection, *, slot_rollback, finish_key=None):
        self.connection = connection
        self.slot_rollback = slot_rollback
        self.finish_key = finish_key
        self.projection_hits = 0
        self.verification_hits = 0
        self.rollback_hits = 0
        self.candidate_queries = []

    def execute(self, sql, parameters=()):
        normalized = " ".join(sql.split())
        if (self.finish_key is not None
                and normalized.startswith("SELECT id FROM history_transactions WHERE operation_key = ?")
                and tuple(parameters) == (str(self.finish_key),)):
            self.verification_hits += 1
            raise sqlite3.OperationalError("原 READ 历史核实读取失败")
        if normalized == "ROLLBACK":
            self.rollback_hits += 1
            result = self.connection.execute(sql, parameters)
            if self.verification_hits:
                # 实际回滚后响应丢失，仓储不能声称已确认回滚或完成原 F 核实。
                raise sqlite3.OperationalError("核实事务回滚响应丢失")
            return result
        if self.slot_rollback and normalized.startswith("UPDATE file_copies SET slot_device_id ="):
            self.projection_hits += 1
            raise sqlite3.OperationalError("原 slot 投影保存失败")
        if (normalized.startswith("SELECT id FROM actions WHERE type = 2 AND status = 2")
                or normalized.startswith("SELECT t.id, t.attempt_no, r.id, r.kind")):
            self.candidate_queries.append(normalized)
            raise AssertionError("原 READ 保存未可靠完成时不能查询新的业务候选")
        return self.connection.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self.connection, name)


class _NoBusinessClock:
    def __init__(self):
        self.calls = 0

    def utc_micros(self):
        self.calls += 1
        raise AssertionError("原 READ 保存未可靠完成时不能进入业务筛选")


@pytest.mark.parametrize("entrance", ["normal", "residual", "restricted"])
@pytest.mark.parametrize("branch", ["finish_unknown", "slot_rollback"])
async def test_default_read_save_failure_keeps_original_responsibility(pipeline, monkeypatch, entrance, branch):
    # Finish 已 COMMIT但仍未取得核实响应；slot 则第一次 COMMIT前故障无可靠 F。
    stage, committed = ("finish", True) if branch == "finish_unknown" else ("slot", False)
    world, reopened, original_flow, ticket, finishes, slots = await _held_read(pipeline, monkeypatch, stage, committed)
    deps, _factories, context, reader, driver, _ends, _wall, factory_calls = world
    inputs = finishes if stage == "finish" else slots
    original_request, original_key = inputs[0]
    holder = (original_flow.pending_read_results[(ticket.run_id, ticket.attempt_id)] if stage == "finish" else
              original_flow.pending_read_business[(int(ticket.target_id), "release_read_slot")])
    returned = []
    if stage == "finish":
        real_finish = OperationRepository.finish_attempt

        def unavailable(repository, request, key, owned):
            result = real_finish(repository, request, key, owned)
            if request.ticket.operation == "read":
                returned.append(result)
            return result

        monkeypatch.setattr(OperationRepository, "finish_attempt", unavailable)
    before = {table: _snapshot(reopened, table, identity) for table, identity in (
        ("actions", 1), ("recording_processing", 1),
        ("file_copies", int(ticket.target_id)), ("operation_runs", ticket.run_id))}
    history_count = reopened.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()
    before_calls = tuple(driver.calls)
    original_open, opened = context.open_connection, []
    clock = _NoBusinessClock()
    context.clock = clock

    def open_connection():
        current = original_open()
        assert current.metadata == reopened.metadata and current.connection is not reopened.connection
        fault = _ReadGateConnection(current.connection, slot_rollback=stage == "slot",
                                   finish_key=original_key if stage == "finish" else None)
        opened.append(fault)
        return replace(current, connection=fault)

    context.open_connection = open_connection
    try:
        selected = (context.flows["scheduling"] if entrance == "normal" else
                    context.flows["residual"] if entrance == "residual" else context.restricted_flows["winddown"])
        with pytest.raises(StateDbFailure):
            await selected(context)
        assert len(opened) == 1 and len(factory_calls) == 1
        assert clock.calls == 0 and opened[0].candidate_queries == []
        assert len(inputs) == 2 and inputs[1] == (original_request, original_key)
        if stage == "finish":
            assert len(returned) == 1 and returned[0].kind is DbOutcomeKind.UNKNOWN
            assert opened[0].verification_hits == opened[0].rollback_hits == 1
            assert original_flow.pending_read_results[(ticket.run_id, ticket.attempt_id)] == holder
            assert len(slots) == 0 and original_flow.pending_read_business == {}
        else:
            assert opened[0].projection_hits == opened[0].rollback_hits == 1
            assert original_flow.pending_read_business[(int(ticket.target_id), "release_read_slot")] == holder
            assert len(finishes) == 1 and original_flow.pending_read_results == {}
        # 在 fresh Owned 检查耐久 F，不读取曾经 COMMIT UNKNOWN 的原连接。
        assert (saved_transaction_events(reopened.connection, original_key) is not None) is committed
        assert before == {table: _snapshot(reopened, table, identity) for table, identity in (
            ("actions", 1), ("recording_processing", 1),
            ("file_copies", int(ticket.target_id)), ("operation_runs", ticket.run_id))}
        assert reopened.connection.execute("SELECT COUNT(*) FROM history_events").fetchone() == history_count
        assert len(reader.requests) == 1 and tuple(driver.calls) == before_calls
    finally:
        reopened.connection.close()
        lifecycle.close_runtime(deps)

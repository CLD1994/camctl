"""原 Finish 核实未知或摘要校验保存回滚时，入口保留原责任。"""

import pytest

from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.transactions import saved_transaction_events

from ..capture.media_retry_fixtures import media_pipeline as pipeline  # noqa: F401
from .test_read_default_consumers import _snapshot


pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("entrance", ["normal", "residual", "restricted"])
@pytest.mark.parametrize("branch", ["finish_unknown", "verification_rollback"])
async def test_default_read_save_failure_keeps_original_responsibility(pipeline, capture_resources, monkeypatch, entrance, branch):
    from ..support.bootstrap_consumers import StorageGate, held_default_read

    stage, committed = ("finish", True) if branch == "finish_unknown" else ("verification", False)
    world, ticket, ends, finishes, verifications = await held_default_read(pipeline, capture_resources, monkeypatch, stage, committed)
    owned = world.owned
    inputs = finishes if stage == "finish" else verifications
    request, key = inputs[0]
    gate = StorageGate(original_key=stage == "finish", unknown=stage == "finish",
        table="file_copies", column="slot_device_id")
    gate.install(monkeypatch, key)
    before = {table: _snapshot(owned, table, identity) for table, identity in (
        ("actions", 1), ("recording_processing", 1),
        ("file_copies", int(ticket.target_id)), ("operation_runs", ticket.run_id))}
    history = owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()
    before_calls = tuple(world.driver.calls)
    try:
        await world.replay(entrance, failure=True)
        assert len(inputs) == 2 and inputs[1] == (request, key)
        expected = DbOutcomeKind.UNKNOWN if stage == "finish" else DbOutcomeKind.ROLLED_BACK
        assert len(gate.receipts) == 1 and gate.receipts[0].kind is expected
        assert gate.hits == gate.rollbacks == 1
        assert len(ends) == 1
        assert len(finishes) == (2 if stage == "finish" else 0)
        assert (saved_transaction_events(owned.connection, key) is not None) is committed
        assert before == {table: _snapshot(owned, table, identity) for table, identity in (
            ("actions", 1), ("recording_processing", 1),
            ("file_copies", int(ticket.target_id)), ("operation_runs", ticket.run_id))}
        assert owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall() == history
        assert len(world.reader.requests) == 1 and tuple(world.driver.calls) == before_calls
        assert world.tools() == []
        # 移除外部故障后，同一会话沿原申请恢复，证明失败没有丢失保存责任。
        gate.enabled = False
        await world.replay(entrance)
        assert inputs[-1] == (request, key)
        assert len(inputs) == 3
        assert saved_transaction_events(owned.connection, key) is not None
    finally:
        await world.close()
        owned.connection.close()

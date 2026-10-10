"""默认清理轮次在新运行时和新连接续接原 QUERY 保存责任。

每轮经 cleanup_flow 打开和关闭真实 OwnedConnection，同一个
session_cleanup_assembly 工厂创建新的运行时。故障只注入真实
QUERY 结果事务的 COMMIT，不替换运行时或直接写入状态投影。
当前绑定改变的用例只验证原结果保存的优先级；后续普通调用仍由
既有绑定规则决定，不在这里重新定义绑定失败的业务结果。
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from sqlite3 import ProgrammingError

import pytest

from camctl.bootstrap.cleanup_assembly import cleanup_flow, session_cleanup_assembly
from camctl.contracts.values import ConsistencyError
from camctl.operations.attempts import FinishDisposition
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository

from ..outputs.test_cleanup_query_commit_recovery import (
    _CommitFaultConnection, _assert_saved_result, _dependent_facts,
    _history, _query_row,
)
from ..outputs.test_cleanup_query_save_recovery import _MatrixDriver
from .test_output_binding_changes import (
    _NOW, _accept, _registry, _save_photos, environment,
)

pytestmark = pytest.mark.asyncio


class _DefaultDriver(_MatrixDriver):
    """首轮提供实际结果；后续轮次的原责任不得重新调用设备。"""

    def __init__(self, owned):
        super().__init__(owned)
        self.reject_new_calls = False

    async def delete(self, request):
        if self.reject_new_calls:
            raise AssertionError("原 QUERY 恢复轮次不得发起新的 DELETE")
        return await super().delete(request)

    async def query_state(self, request):
        if self.reject_new_calls:
            raise AssertionError("原 QUERY 恢复轮次不得重新查询设备")
        return await super().query_state(request)


class _DefaultQueryFault:
    """稳定仓储端口的 spy 与真实 QUERY COMMIT 故障注入。"""

    def __init__(self, original, action_id, output_id, *, after_commit):
        self.original = original
        self.action_id, self.output_id = action_id, output_id
        self.after_commit = after_commit
        self.calls, self.receipts, self.member_before_save = [], [], []
        self.fault = None
        self.history_prefix = None
        self.database_before = None
        self.dependencies_before = None

    def save(self, repository, finish, key, owned):
        if finish.ticket.operation != "query":
            return self.original(repository, finish, key, owned)
        item_id = int(finish.ticket.target_id)
        member = owned.connection.execute(
            "SELECT action_id,status,final_event_id FROM cleanup_items WHERE id=?",
            (item_id,)).fetchone()
        if member[0] != self.action_id:
            return self.original(repository, finish, key, owned)
        self.calls.append((repository, finish, key, owned))
        self.member_before_save.append(member)
        if self.fault is None:
            self.history_prefix = _history(owned.connection)
            self.database_before = tuple(owned.connection.iterdump())
            self.dependencies_before = _dependent_facts(
                owned.connection, item_id, self.action_id, self.output_id)
            self.fault = _CommitFaultConnection(
                owned.connection, after_commit=self.after_commit)
            receipt = self.original(
                repository, finish, key, replace(owned, connection=self.fault))
        else:
            receipt = self.original(repository, finish, key, owned)
        self.receipts.append(receipt)
        return receipt


def _assert_closed(owned):
    with pytest.raises(ProgrammingError):
        owned.connection.execute("SELECT 1")


@pytest.mark.parametrize("binding", ["matched", "missing", "mismatch"])
@pytest.mark.parametrize("after_commit", [False, True], ids=["before_commit", "after_commit"])
@pytest.mark.parametrize("present", [True, False, None], ids=["present", "absent", "unknown"])
async def test_default_cleanup_query_recovers_original_result(
        environment, monkeypatch, present, after_commit, binding):
    """同一公开工厂跨轮次保留原结果，当前绑定不阻止原键保存。"""
    cfg, owned, context, capture_driver = environment
    await _save_photos(cfg, owned, context, capture_driver)
    output_id = owned.connection.execute(
        "SELECT o.id FROM outputs o JOIN actions a ON a.id=o.source_action_id"
        " WHERE a.device_id='cam-a'").fetchone()[0]
    _accept(owned, "2", [{
        "name": "默认清理", "type": "delete_action_outputs",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"output_ids": [str(output_id)]},
    }])
    action_id = owned.connection.execute(
        "SELECT id FROM actions WHERE type=5").fetchone()[0]
    driver = _DefaultDriver(owned)
    driver.matrix_enabled, driver.present = True, present
    devices = {device: dict(declaration) for device, declaration in cfg.devices.items()}
    clock = {"wall": _NOW, "mono": 0}
    session_factory = session_cleanup_assembly(
        devices=devices, drivers=_registry(driver),
        max_delete_attempts=3, max_query_attempts=3,
        staging=Path(cfg.paths.staging), occurred_at=lambda: clock["wall"],
        monotonic_ns=lambda: clock["mono"],
    )
    runtimes = []

    def factory(current_owned):
        runtime = session_factory(current_owned)
        runtimes.append(runtime)
        return runtime

    fault = _DefaultQueryFault(
        OperationRepository.finish_attempt, action_id, output_id,
        after_commit=after_commit)

    def save_query(repository, finish, key, current_owned):
        return fault.save(repository, finish, key, current_owned)

    monkeypatch.setattr(OperationRepository, "finish_attempt", save_query)
    flow = cleanup_flow(factory)
    with pytest.raises(ConsistencyError):
        await flow(context)

    assert len(runtimes) == len(fault.calls) == len(fault.receipts) == 1
    _assert_closed(runtimes[0].owned)
    assert fault.receipts[0].kind is DbOutcomeKind.UNKNOWN
    assert fault.receipts[0].error is fault.fault.error
    assert fault.fault.commit_calls == 1
    assert fault.fault.actual_commits == int(after_commit)
    assert fault.fault.transaction_at_failure is (not after_commit)
    assert len(driver.deletes) == len(driver.queries) == 1
    _first_repository, first_finish, first_key, first_owned = fault.calls[0]
    ticket, item_id = first_finish.ticket, int(first_finish.ticket.target_id)
    assert first_finish.occurred_at == _NOW
    assert first_finish.outcome.outcome is driver.last_query_result.outcome
    assert _dependent_facts(owned.connection, item_id, action_id, output_id) == fault.dependencies_before
    first_history = _history(owned.connection)
    assert first_history[:len(fault.history_prefix)] == fault.history_prefix
    if after_commit:
        _assert_saved_result(owned.connection, ticket, present, _NOW, first_key)
        committed_row = _query_row(owned.connection, ticket)
    else:
        assert tuple(owned.connection.iterdump()) == fault.database_before
        assert _query_row(owned.connection, ticket) == (1, 1, None, None, None)
        committed_row = None

    # 本次配置可以变化；它不替换旧 QUERY 的原证据、时刻、输入和 key。
    devices["cam-a"]["cleanup"] = {
        "delete_timeout_s": Decimal("2"), "query_timeout_s": Decimal("4"),
        "delete_retry_interval_s": Decimal("1"), "query_retry_interval_s": Decimal("1"),
    }
    if binding == "missing":
        devices.pop("cam-a")
    elif binding == "mismatch":
        devices["cam-a"]["driver"] = "alternate-camera"
    clock.update(wall=_NOW + 5_000, mono=900)
    driver.reject_new_calls = True
    await flow(context)

    assert len(runtimes) == 2
    assert runtimes[0] is not runtimes[1]
    assert runtimes[0].owned is not runtimes[1].owned
    assert runtimes[0].owned.connection is not runtimes[1].owned.connection
    assert runtimes[0].owned.metadata.instance_id == runtimes[1].owned.metadata.instance_id
    _assert_closed(runtimes[1].owned)
    assert len(fault.calls) == len(fault.receipts) == 2
    _second_repository, second_finish, second_key, second_owned = fault.calls[1]
    assert second_owned is not first_owned
    assert second_finish == first_finish
    assert second_finish.outcome.outcome is first_finish.outcome.outcome
    assert second_finish.occurred_at == _NOW
    assert second_key == first_key
    assert fault.receipts[1].kind is DbOutcomeKind.COMPLETED, fault.receipts[1].error
    assert fault.receipts[1].value.disposition is FinishDisposition.SAVED
    assert fault.member_before_save == [(action_id, 3, None), (action_id, 3, None)]
    _assert_saved_result(owned.connection, ticket, present, _NOW, first_key)
    if committed_row is not None:
        assert _query_row(owned.connection, ticket) == committed_row
    restored_history = _history(owned.connection)
    assert restored_history[:len(first_history)] == first_history
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (first_key,)).fetchone() == (1,)
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM operation_attempts WHERE run_id=?", (ticket.run_id,)).fetchone() == (1,)
    assert owned.connection.execute(
        "SELECT attempts_used,max_attempts_used FROM operation_runs WHERE id=?",
        (ticket.run_id,)).fetchone() == (1, 3)
    assert len(driver.deletes) == len(driver.queries) == 1

    member = owned.connection.execute(
        "SELECT status,restriction_state,outcome,error_code,final_event_id"
        " FROM cleanup_items WHERE id=?", (item_id,)).fetchone()
    if present is False:
        assert member[:4] == (4, 4, 3, None)
        assert member[4] is not None
        assert member[4] > _query_row(owned.connection, ticket)[2]
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (3,)
        assert owned.connection.execute(
            "SELECT status,retry_wait_required FROM operation_runs"
            " WHERE cleanup_item_id=? ORDER BY id", (item_id,)).fetchall() == [(3, 0), (3, 0)]
        assert owned.connection.execute(
            "SELECT availability,cleanup_status FROM outputs WHERE id=?", (output_id,)).fetchone() == (3, 4)
    elif binding == "matched":
        assert member == (3, 4, None, None, None)
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (2,)
        assert owned.connection.execute(
            "SELECT status,retry_wait_required FROM operation_runs"
            " WHERE cleanup_item_id=? ORDER BY id", (item_id,)).fetchall() == [(2, 1), (2, 1)]
    # 非缺席时绑定变化只影响后续资格。本用例核对原结果优先保存及
    # 原文件事实保持；不重新定义后续绑定失败的成员终态。
    if present is not False:
        assert owned.connection.execute(
            "SELECT * FROM device_files WHERE id=(SELECT device_file_id FROM outputs WHERE id=?)",
            (output_id,)).fetchone() == fault.dependencies_before[3]

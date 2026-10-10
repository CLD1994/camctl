"""QUERY 结果事务提交未知后，在同一状态库的新连接保存原申请。

三个消费者均从真实公开历史进入首次查询。COMMIT 故障在真实
OperationRepository 事务内注入，随后关闭旧连接并复验原键。
恢复调用共同 _run_query 保存边界，尚不作为默认会话入口或成员
结果、本地续接、取消分类全部完成的证据。
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import json
from sqlite3 import OperationalError

import pytest

from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.evidence import EvidenceRegistry
from camctl.operations.attempts import AttemptConfig, FinishDisposition
from camctl.outputs.cleanup_flow import (
    FinishCleanupAction, _run_query, _verify_before_delete, delete_source_file,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import OutputsRepository

from ..bootstrap.test_output_binding_changes import _NOW, _save_photos, environment
from .test_cleanup_query_save_recovery import (
    _MatrixDriver, _apply_cleanup_cancel, _new_runtime, _original_member_rows,
    _start_cleanup,
)

pytestmark = pytest.mark.asyncio


class _CommitFaultConnection:
    """仅在本次真实 COMMIT 前或后丢失结果；其他连接操作透传。"""

    def __init__(self, connection, *, after_commit):
        self._connection = connection
        self.after_commit = after_commit
        self.commit_calls = 0
        self.actual_commits = 0
        self.transaction_at_failure = None
        self.error = OperationalError("QUERY 结果事务 COMMIT 完成状态未知")

    def execute(self, sql, parameters=()):
        if sql == "COMMIT":
            self.commit_calls += 1
            if self.after_commit:
                self._connection.execute(sql, parameters)
                self.actual_commits += 1
            self.transaction_at_failure = self._connection.in_transaction
            raise self.error
        return self._connection.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def _history(connection):
    return tuple(connection.execute("SELECT * FROM history_events ORDER BY id"))


def _dependent_facts(connection, item_id, action_id, output_id):
    """排除 QUERY 自身允许更新的结果与派生目录，核对业务副作用。"""
    return (
        connection.execute(
            "SELECT * FROM cleanup_items WHERE id=?", (item_id,)).fetchone(),
        connection.execute(
            "SELECT status,execution_started,cancel_requested,error_code,error_details_json"
            " FROM actions WHERE id=?", (action_id,)).fetchone(),
        connection.execute("SELECT * FROM outputs WHERE id=?", (output_id,)).fetchone(),
        connection.execute(
            "SELECT * FROM device_files WHERE id=(SELECT device_file_id FROM outputs WHERE id=?)",
            (output_id,)).fetchone(),
        tuple(connection.execute(
            "SELECT id,status,error_json FROM operation_runs WHERE cleanup_item_id=? ORDER BY id",
            (item_id,))),
        tuple(connection.execute(
            "SELECT a.* FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
            " WHERE r.cleanup_item_id=? AND r.kind=4 ORDER BY a.id", (item_id,))),
    )


class _CommitQueryPort(OperationRepository):
    """保存原输入的 spy；首次目标 QUERY 在真实 SQL COMMIT 处故障。"""

    def __init__(self, item_id, action_id, output_id, *, after_commit):
        self.item_id, self.action_id, self.output_id = item_id, action_id, output_id
        self.after_commit = after_commit
        self.calls, self.receipts = [], []
        self.fault = None
        self.history_prefix = None
        self.database_before = None
        self.dependencies_before = None

    def finish_attempt(self, finish, key, owned):
        if (finish.ticket.operation != "query"
                or finish.ticket.target_id != str(self.item_id)):
            return super().finish_attempt(finish, key, owned)
        self.calls.append((finish, key, owned.metadata.instance_id))
        if self.fault is None:
            self.history_prefix = _history(owned.connection)
            self.database_before = tuple(owned.connection.iterdump())
            self.dependencies_before = _dependent_facts(
                owned.connection, self.item_id, self.action_id, self.output_id)
            self.fault = _CommitFaultConnection(
                owned.connection, after_commit=self.after_commit)
            outcome = super().finish_attempt(
                finish, key, replace(owned, connection=self.fault))
        else:
            outcome = super().finish_attempt(finish, key, owned)
        self.receipts.append(outcome)
        return outcome


class _NoNewCalls:
    """新配置的驱动不参与解释、替换或重新获取旧实际结果。"""

    async def query_state(self, request):
        raise AssertionError("原结果恢复不得再次查询设备")

    async def delete(self, request):
        raise AssertionError("原结果保存责任尚未交接，不得删除设备文件")


def _query_row(connection, ticket):
    return connection.execute(
        "SELECT status,effect_state,result_event_id,result_json,error_json"
        " FROM operation_attempts WHERE run_id=? AND attempt_no=?",
        (ticket.run_id, ticket.attempt_id),
    ).fetchone()


def _assert_saved_result(connection, ticket, present, occurred_at, key):
    row = _query_row(connection, ticket)
    assert row[:2] == (
        int(enum_for("operation_attempts.status").FAILED),
        int(enum_for("operation_attempts.effect_state").UNKNOWN if present is None
            else enum_for("operation_attempts.effect_state").CONFIRMED),
    )
    assert row[2] is not None
    assert json.loads(row[3]) == {
        "format_version": 1,
        "settlement": {"basis": "observed", "evidence": {
            "type": "file_presence", "version": 1, "data": {}}},
        "observations": [] if present is None else [{
            "type": "file_presence", "version": 1,
            "data": {"cleanup_item_id": ticket.target_id, "present": present}}],
        "call_info": {"local_exit": {"exit_code": 7}, "remote_exit_code": 3},
    }
    assert json.loads(row[4]) == {
        "code": "device_error", "stage": "query", "details": {
            "reason": "调用返回错误，保留实际文件观察", "target": ticket.target_id,
            "diagnostic": {"remote_exit": 3}}}
    assert connection.execute(
        "SELECT e.occurred_at,e.event_type FROM history_events e"
        " JOIN history_transactions t ON t.id=e.transaction_id"
        " WHERE e.id=? AND t.operation_key=?", (row[2], key)).fetchone() == (occurred_at, 12)
    events = connection.execute(
        "SELECT e.event_type,e.occurred_at FROM history_events e"
        " JOIN history_transactions t ON t.id=e.transaction_id"
        " WHERE t.operation_key=? ORDER BY e.id", (key,)).fetchall()
    assert events == ([(12, occurred_at)] if present is False
                      else [(12, occurred_at), (6, occurred_at)])


@pytest.mark.parametrize("after_commit", [False, True], ids=["before_commit", "after_commit"])
@pytest.mark.parametrize("present", [True, False, None], ids=["present", "absent", "unknown"])
@pytest.mark.parametrize("consumer", ["before", "after", "cancel"])
async def test_query_result_commit_unknown(environment, consumer, present, after_commit):
    """提交前后 UNKNOWN 均先停止消费，再在 fresh 连接复验原结果。"""
    cfg, owned, context, capture_driver = environment
    await _save_photos(cfg, owned, context, capture_driver)
    output_id = owned.connection.execute(
        "SELECT o.id FROM outputs o JOIN actions a ON a.id=o.source_action_id"
        " WHERE a.device_id='cam-a'").fetchone()[0]
    driver = _MatrixDriver(owned)
    old_member = None
    if consumer == "before":
        old_action, old_item = _start_cleanup(owned, output_id, "2")
        previous = _new_runtime(cfg, owned, driver, query_attempts=1)
        assert (await delete_source_file(previous, old_item)).phase == "query_unknown"
        failed = await delete_source_file(previous, old_item)
        assert (failed.phase, failed.detail) == ("failed", "file_query_attempts_exhausted")
        finished = OutputsRepository().finish_cleanup_action(
            FinishCleanupAction(old_action, _NOW), new_operation_key(), owned)
        assert finished.kind is DbOutcomeKind.COMPLETED, finished.error
        old_member = (old_action, old_item, _original_member_rows(owned, old_action, old_item))
        action_id, item_id = _start_cleanup(owned, output_id, "3")
    else:
        action_id, item_id = _start_cleanup(owned, output_id, "2")
    base = _new_runtime(cfg, owned, driver)
    port = _CommitQueryPort(item_id, action_id, output_id, after_commit=after_commit)
    base.operations = port
    runtime = base.for_item(item_id)
    driver.matrix_enabled, driver.present = True, present
    queries_before, deletes_before = len(driver.queries), len(driver.deletes)
    if consumer == "cancel":
        driver.on_delete_return = lambda: _apply_cleanup_cancel(owned, action_id)

    with pytest.raises(ConsistencyError):
        if consumer == "before":
            await _verify_before_delete(runtime, item_id, action_id, output_id)
        else:
            await delete_source_file(runtime, item_id)
    assert len(port.calls) == len(port.receipts) == 1
    assert port.receipts[0].kind is DbOutcomeKind.UNKNOWN
    assert port.receipts[0].error is port.fault.error
    assert port.fault.commit_calls == 1
    assert port.fault.actual_commits == int(after_commit)
    assert port.fault.transaction_at_failure is (not after_commit)
    assert len(driver.queries) == queries_before + 1
    assert len(driver.deletes) == deletes_before + (consumer != "before")
    original_finish, original_key, instance_id = port.calls[0]
    ticket = original_finish.ticket
    assert original_finish.outcome.outcome is driver.last_query_result.outcome
    assert original_finish.occurred_at == _NOW

    # 未提交事务随旧连接关闭回滚；已实际提交的结果保持。恢复只用
    # 新连接读取可靠事实，不在未知的原事务内猜测是否提交。
    owned.connection.close()
    fresh = context.open_connection()
    try:
        assert fresh.metadata.instance_id == instance_id
        assert fresh.connection is not owned.connection
        assert _dependent_facts(fresh.connection, item_id, action_id, output_id) == port.dependencies_before
        before_recovery_history = _history(fresh.connection)
        assert before_recovery_history[:len(port.history_prefix)] == port.history_prefix
        if after_commit:
            _assert_saved_result(fresh.connection, ticket, present, _NOW, original_key)
            saved_row = _query_row(fresh.connection, ticket)
        else:
            assert tuple(fresh.connection.iterdump()) == port.database_before
            assert _query_row(fresh.connection, ticket) == (1, 1, None, None, None)
            saved_row = None

        # replace 保持原 runtime 全部数据字段及共享集合，替换连接和
        # 当前执行端口；原保存不使用新驱动、证据或时钟重新解释。
        resumed = replace(
            runtime, owned=fresh, driver=_NoNewCalls(), evidence=EvidenceRegistry(()),
            occurred_at=lambda: _NOW + 9_999_999, monotonic_ns=lambda: 900,
            query_config=AttemptConfig(1, Decimal("2"), Decimal("0")),
            delete_config=AttemptConfig(1, Decimal("2"), Decimal("0")),
            for_item=None,
        )
        observed = await _run_query(resumed, ticket, item_id)
        assert observed is present
        assert len(port.calls) == len(port.receipts) == 2
        restored_finish, restored_key, restored_instance = port.calls[1]
        assert restored_finish == original_finish
        assert restored_finish.outcome.outcome is original_finish.outcome.outcome
        assert restored_finish.ticket == ticket
        assert restored_finish.occurred_at == _NOW
        assert restored_key == original_key
        assert restored_instance == instance_id
        assert port.receipts[1].kind is DbOutcomeKind.COMPLETED, port.receipts[1].error
        assert port.receipts[1].value.disposition is FinishDisposition.SAVED
        _assert_saved_result(fresh.connection, ticket, present, _NOW, original_key)
        assert _dependent_facts(fresh.connection, item_id, action_id, output_id) == port.dependencies_before
        after = _history(fresh.connection)
        assert after[:len(port.history_prefix)] == port.history_prefix
        if after_commit:
            assert after == before_recovery_history
            assert _query_row(fresh.connection, ticket) == saved_row
        else:
            assert len(after) == len(before_recovery_history) + (1 if present is False else 2)
        assert fresh.connection.execute(
            "SELECT attempts_used,max_attempts_used FROM operation_runs WHERE id=?",
            (ticket.run_id,)).fetchone() == (1, 3)
        assert fresh.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts WHERE run_id=?", (ticket.run_id,)).fetchone() == (1,)
        assert fresh.connection.execute(
            "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (original_key,)).fetchone() == (1,)
        assert len(driver.queries) == queries_before + 1
        assert len(driver.deletes) == deletes_before + (consumer != "before")
        assert fresh.connection.in_transaction is False
        if old_member is not None:
            old_action, old_item, original_rows = old_member
            assert _original_member_rows(fresh, old_action, old_item) == original_rows
    finally:
        fresh.connection.close()

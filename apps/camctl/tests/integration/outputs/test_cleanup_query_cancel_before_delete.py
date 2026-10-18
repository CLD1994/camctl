"""删除前查询取得归属后的取消分类与首次建档共同事务。

C1 的原未知删除可靠终态化后，C2 接手并只执行自己的 QUERY；取消
在本项查询结果保存前后分别生效。本项可靠从未派发 DELETE 时无删
除错误取消并解除本项可撤销限制，另一成员的删除事实不进入本项分
类。首次处理归属、操作流程与第一次尝试在同一事务建档，回滚不留
下半套归属或尝试；删除意图提交后取消阻止派发时沿原尝试保存可靠
未派发结果。
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from camctl.contracts.enums import enum_for
from camctl.contracts.values import new_operation_key
from camctl.contracts.workflow_errors import item_error_id
from camctl.operations.attempts import BeginDisposition, FinishDisposition
from camctl.outputs.cleanup_flow import (
    FinishCleanupAction, delete_source_file)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import OutputsRepository

from ..bootstrap.test_output_binding_changes import _NOW, _save_photos, environment
from ..persistence.test_transactions import FailingConnection as _FaultConnection
from .test_cleanup_query_save_recovery import (
    _MatrixDriver, _apply_cleanup_cancel, _member_facts, _new_runtime,
    _start_cleanup)

pytestmark = pytest.mark.asyncio

_ITEM_CANCELED, _ITEM_RESTRICTION = 6, enum_for("cleanup_items.restriction_state")
_KIND = enum_for("operation_runs.kind")
_ATTEMPT = enum_for("operation_attempts.status")
_EFFECT = enum_for("operation_attempts.effect_state")


class _CancelQueryPort(OperationRepository):
    """在目标 QUERY 结果保存前后施加本动作取消；其余事务真实执行。"""

    def __init__(self, owned, item_id: int, action_id: int, when: str):
        self.owned, self.item_id = owned, item_id
        self.action_id, self.when = action_id, when
        self.saved = 0

    def finish_attempt(self, finish, key, owned):
        if (finish.ticket.operation != "query"
                or finish.ticket.target_id != str(self.item_id)):
            return super().finish_attempt(finish, key, owned)
        if self.when == "before":
            _apply_cleanup_cancel(self.owned, self.action_id)
        result = super().finish_attempt(finish, key, owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value.disposition is FinishDisposition.SAVED
        self.saved += 1
        if self.when == "after":
            _apply_cleanup_cancel(self.owned, self.action_id)
        return result


class _EntryPort(OutputsRepository):
    """包裹清理首次建档事务：授予后施加取消或注入提交回滚。"""

    def __init__(self, owned, item_id: int, action_id: int, mode: str):
        self.owned, self.item_id = owned, item_id
        self.action_id, self.mode = action_id, mode
        self.entries = []

    def begin_cleanup_attempt(self, intent, key, owned):
        if intent.target.cleanup_item_id != self.item_id:
            return super().begin_cleanup_attempt(intent, key, owned)
        if self.mode == "rolled_back":
            # 提交阶段注入连接故障：计划内全部事件随事务回滚。
            fault = _FaultConnection(
                owned.connection, "INSERT INTO history_events")
            return super().begin_cleanup_attempt(
                intent, key, replace(owned, connection=fault))
        result = super().begin_cleanup_attempt(intent, key, owned)
        self.entries.append((intent.operation, result.kind,
                             getattr(result.value, "disposition", None)))
        if (result.kind is DbOutcomeKind.COMPLETED
                and result.value.ticket is not None):
            _apply_cleanup_cancel(self.owned, self.action_id)
        return result


async def _c1_unknown_delete_terminal(cfg, owned, driver, output_id: int):
    """C1 实际删除未知、查询未知后预算耗尽，可靠保存失败终态。"""
    action_id, item_id = _start_cleanup(owned, output_id, "2")
    runtime = _new_runtime(cfg, owned, driver, query_attempts=1)
    first = await delete_source_file(runtime, item_id)
    assert first.phase == "query_unknown", first
    exhausted = await delete_source_file(runtime, item_id)
    assert (exhausted.phase, exhausted.detail) == (
        "failed", "file_query_attempts_exhausted"), exhausted
    finished = OutputsRepository().finish_cleanup_action(
        FinishCleanupAction(action_id, _NOW), new_operation_key(), owned)
    assert finished.kind is DbOutcomeKind.COMPLETED, finished.error
    return action_id, item_id


@pytest.mark.parametrize("present", [True, None], ids=["present", "unknown"])
@pytest.mark.parametrize("when", ["before", "after"], ids=["before_save", "after_save"])
async def test_query_only_cancel_releases_reversible_restriction(
        environment, when, present):
    """C2 仅有自己的 QUERY：取消生效时无删除错误解除本项限制。"""
    cfg, owned, context, capture_driver = environment
    await _save_photos(cfg, owned, context, capture_driver)
    output_id = owned.connection.execute(
        "SELECT o.id FROM outputs o JOIN actions a ON a.id=o.source_action_id"
        " WHERE a.device_id='cam-a'").fetchone()[0]
    driver = _MatrixDriver(owned)
    c1 = await _c1_unknown_delete_terminal(cfg, owned, driver, output_id)
    c1_rows = owned.connection.execute(
        "SELECT status,restriction_state,error_code FROM cleanup_items WHERE id=?",
        (c1[1],)).fetchone()

    action_id, item_id = _start_cleanup(owned, output_id, "3")
    runtime = _new_runtime(cfg, owned, driver)
    port = _CancelQueryPort(owned, item_id, action_id, when)
    runtime.operations = port
    session = runtime.for_item(item_id)
    driver.matrix_enabled, driver.present = True, present

    step = await delete_source_file(session, item_id)

    assert (step.phase, step.detail) == ("canceled", None), step
    assert port.saved == 1
    assert len(driver.deletes) == 1  # 只有 C1 的原未知删除。
    assert len(driver.queries) == 2  # C1 与 C2 各一次。
    facts = _member_facts(owned, item_id)
    assert facts[:4] == (int(_ITEM_CANCELED), int(_ITEM_RESTRICTION.RELEASED),
                         None, None)
    assert facts[4] is not None
    # C2 从未建立删除流程或删除尝试。
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM operation_runs WHERE cleanup_item_id=? AND kind=?",
        (item_id, int(_KIND.DELETE_FILE))).fetchone() == (0,)
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
        " WHERE r.cleanup_item_id=? AND r.kind=?",
        (item_id, int(_KIND.DELETE_FILE))).fetchone() == (0,)
    query_run = owned.connection.execute(
        "SELECT r.status, r.attempts_used FROM operation_runs r"
        " WHERE r.cleanup_item_id=? AND r.kind=?",
        (item_id, int(_KIND.CHECK_FILE_EXISTS))).fetchone()
    assert query_run == (5, 1)  # 查询流程随成员取消收场。
    # C1 的原终态、不可撤销限制与失败错误保持。
    assert owned.connection.execute(
        "SELECT status,restriction_state,error_code FROM cleanup_items WHERE id=?",
        (c1[1],)).fetchone() == c1_rows
    availability, cleanup_status, cleanup_error = owned.connection.execute(
        "SELECT availability,cleanup_status,cleanup_error_json FROM outputs WHERE id=?",
        (output_id,)).fetchone()
    assert availability == 2  # 仍受 C1 不可撤销限制。
    assert cleanup_status == int(enum_for('outputs.cleanup_status').INCOMPLETE)
    assert cleanup_error is not None


async def test_cancel_before_any_query_keeps_member_pending(environment):
    """取消先于 C2 处理生效：直接解除限制，不建立任何流程。"""
    cfg, owned, context, capture_driver = environment
    await _save_photos(cfg, owned, context, capture_driver)
    output_id = owned.connection.execute(
        "SELECT o.id FROM outputs o JOIN actions a ON a.id=o.source_action_id"
        " WHERE a.device_id='cam-a'").fetchone()[0]
    driver = _MatrixDriver(owned)
    await _c1_unknown_delete_terminal(cfg, owned, driver, output_id)
    action_id, item_id = _start_cleanup(owned, output_id, "3")
    _apply_cleanup_cancel(owned, action_id)

    step = await delete_source_file(_new_runtime(cfg, owned, driver), item_id)

    assert (step.phase, step.detail) == ("canceled", None), step
    facts = _member_facts(owned, item_id)
    assert facts[:4] == (int(_ITEM_CANCELED), int(_ITEM_RESTRICTION.RELEASED),
                         None, None)
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM operation_runs WHERE cleanup_item_id=?",
        (item_id,)).fetchone() == (0,)


async def test_first_entry_commits_ownership_flow_and_attempt_together(environment):
    """首次处理归属、操作流程与第一次尝试在同一事务共同提交。"""
    cfg, owned, context, capture_driver = environment
    await _save_photos(cfg, owned, context, capture_driver)
    output_id = owned.connection.execute(
        "SELECT o.id FROM outputs o JOIN actions a ON a.id=o.source_action_id"
        " WHERE a.device_id='cam-a'").fetchone()[0]
    driver = _MatrixDriver(owned)
    await _c1_unknown_delete_terminal(cfg, owned, driver, output_id)
    action_id, item_id = _start_cleanup(owned, output_id, "3")
    driver.matrix_enabled, driver.present = True, True

    step = await delete_source_file(_new_runtime(cfg, owned, driver), item_id)

    # 查询确认仍在后落入删除；本断言聚焦建档边界而非删除结果。
    assert step.phase in ("still_present", "delete_retry_wait", "canceled",
                          "succeeded"), step
    progress_txn = owned.connection.execute(
        "SELECT e.transaction_id FROM history_events e"
        " WHERE e.event_type=24 AND json_extract(e.body_json,'$.reason')=2"
        " AND json_extract(e.body_json,'$.rows[0].id')=?"
        " AND json_extract(e.body_json,'$.rows[0].before.values.status')=2",
        (item_id,)).fetchall()
    query_intent_txn = owned.connection.execute(
        "SELECT DISTINCT e.transaction_id FROM history_events e"
        " JOIN operation_attempts a ON a.intent_event_id=e.id"
        " JOIN operation_runs r ON r.id=a.run_id"
        " WHERE r.cleanup_item_id=? AND r.kind=? AND e.event_type=11"
        " ORDER BY e.id",
        (item_id, int(_KIND.CHECK_FILE_EXISTS))).fetchall()
    assert len(progress_txn) == 1
    # 首次查询的意图事件与处理归属事件同事务；后续尝试另立事务。
    assert query_intent_txn and query_intent_txn[0] == progress_txn[0]
    # 删除结果保存的同一事务保存不可撤销限制事实。
    delete_result_txn = owned.connection.execute(
        "SELECT DISTINCT e.transaction_id FROM history_events e"
        " JOIN operation_attempts a ON a.result_event_id=e.id"
        " JOIN operation_runs r ON r.id=a.run_id"
        " WHERE r.cleanup_item_id=? AND r.kind=?",
        (item_id, int(_KIND.DELETE_FILE))).fetchall()
    upgrade_txn = owned.connection.execute(
        "SELECT e.transaction_id FROM history_events e"
        " WHERE e.event_type=24 AND json_extract(e.body_json,'$.reason')=2"
        " AND json_extract(e.body_json,'$.rows[0].id')=?"
        " AND json_extract(e.body_json,'$.rows[0].after.values.restriction_state')=?",
        (item_id, int(_ITEM_RESTRICTION.IRREVERSIBLE))).fetchall()
    assert upgrade_txn and {row[0] for row in upgrade_txn} <= {
        row[0] for row in delete_result_txn}


async def test_first_entry_rollback_leaves_no_attempt(environment):
    """首次建档回滚后成员保持待删除，没有半套流程或尝试。"""
    cfg, owned, context, capture_driver = environment
    await _save_photos(cfg, owned, context, capture_driver)
    output_id = owned.connection.execute(
        "SELECT o.id FROM outputs o JOIN actions a ON a.id=o.source_action_id"
        " WHERE a.device_id='cam-a'").fetchone()[0]
    driver = _MatrixDriver(owned)
    await _c1_unknown_delete_terminal(cfg, owned, driver, output_id)
    action_id, item_id = _start_cleanup(owned, output_id, "3")
    runtime = _new_runtime(cfg, owned, driver)
    port = _EntryPort(owned, item_id, action_id, "rolled_back")
    runtime.outputs = port
    driver.matrix_enabled, driver.present = True, True

    step = await delete_source_file(runtime.for_item(item_id), item_id)

    assert step.phase == "query_rejected", step
    assert owned.connection.in_transaction is False
    facts = _member_facts(owned, item_id)
    assert facts[:2] == (2, int(_ITEM_RESTRICTION.ACTIVE))
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM operation_runs WHERE cleanup_item_id=?",
        (item_id,)).fetchone() == (0,)
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
        " WHERE r.cleanup_item_id=?", (item_id,)).fetchone() == (0,)
    assert len(driver.queries) == 1  # 只有 C1 的查询。


async def test_delete_intent_cancel_before_dispatch_saves_not_dispatched(environment):
    """取消阻止删除派发时沿原尝试保存可靠未派发并解除限制。"""
    cfg, owned, context, capture_driver = environment
    await _save_photos(cfg, owned, context, capture_driver)
    output_id = owned.connection.execute(
        "SELECT o.id FROM outputs o JOIN actions a ON a.id=o.source_action_id"
        " WHERE a.device_id='cam-a'").fetchone()[0]
    driver = _MatrixDriver(owned)
    action_id, item_id = _start_cleanup(owned, output_id, "2")
    runtime = _new_runtime(cfg, owned, driver)
    port = _EntryPort(owned, item_id, action_id, "cancel_after_grant")
    runtime.outputs = port

    step = await delete_source_file(runtime.for_item(item_id), item_id)

    assert (step.phase, step.detail) == ("canceled", None), step
    assert [entry[:2] for entry in port.entries] == [("delete", DbOutcomeKind.COMPLETED)]
    assert len(driver.deletes) == 0  # 取消阻止了实际派发。
    row = owned.connection.execute(
        "SELECT a.status,a.effect_state,a.result_json FROM operation_attempts a"
        " JOIN operation_runs r ON r.id=a.run_id WHERE r.cleanup_item_id=?",
        (item_id,)).fetchone()
    assert row[:2] == (int(_ATTEMPT.FAILED), int(_EFFECT.NO_EFFECT))
    result = json.loads(row[2])
    assert result["settlement"] == {
        "basis": "not_dispatched",
        "evidence": {"type": "dispatch_prevented", "version": 1, "data": {}}}
    facts = _member_facts(owned, item_id)
    assert facts[:4] == (int(_ITEM_CANCELED), int(_ITEM_RESTRICTION.RELEASED),
                         None, None)
    assert owned.connection.execute(
        "SELECT status FROM operation_runs WHERE cleanup_item_id=?",
        (item_id,)).fetchone() == (5,)  # 删除流程按取消收场。


async def test_query_only_absence_success_from_reversible_restriction(environment):
    """仅有查询的成员确认缺席：成功终态保存不可撤销完成事实。"""
    cfg, owned, context, capture_driver = environment
    await _save_photos(cfg, owned, context, capture_driver)
    output_id = owned.connection.execute(
        "SELECT o.id FROM outputs o JOIN actions a ON a.id=o.source_action_id"
        " WHERE a.device_id='cam-a'").fetchone()[0]
    driver = _MatrixDriver(owned)
    await _c1_unknown_delete_terminal(cfg, owned, driver, output_id)
    action_id, item_id = _start_cleanup(owned, output_id, "3")
    driver.matrix_enabled, driver.present = True, False

    step = await delete_source_file(_new_runtime(cfg, owned, driver), item_id)

    assert (step.phase, step.detail) == ("succeeded", "ABSENCE_CONFIRMED"), step
    facts = _member_facts(owned, item_id)
    assert facts[:4] == (4, int(_ITEM_RESTRICTION.IRREVERSIBLE), 3, None)
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM operation_runs WHERE cleanup_item_id=? AND kind=?",
        (item_id, int(_KIND.DELETE_FILE))).fetchone() == (0,)


class _CancelAfterQueryDriver(_MatrixDriver):
    """查询实际返回后、结果保存前施加本动作取消；DELETE 真实执行。"""

    def __init__(self, owned):
        super().__init__(owned)
        self.on_query_return = None

    async def query_file(self, request):
        result = await super().query_file(request)
        if self.on_query_return is not None:
            callback, self.on_query_return = self.on_query_return, None
            callback()
        return result


@pytest.mark.parametrize("present,phase,detail,status,outcome,code,settled", [
    (True, "canceled", "file_delete_failed", 6, None, "file_delete_failed", 4),
    (None, "canceled", "delete_unconfirmed", 6, None, "delete_unconfirmed", 6),
    (False, "succeeded", "ABSENCE_CONFIRMED", 4, 3, None, 3),
], ids=["present", "unknown", "absent"])
async def test_cancel_after_query_return_settles_by_matrix(
        environment, present, phase, detail, status, outcome, code, settled):
    """本项 DELETE 已发出：取消在查询返回后生效，按矩阵收场且零新增删除。"""
    cfg, owned, context, capture_driver = environment
    await _save_photos(cfg, owned, context, capture_driver)
    output_id = owned.connection.execute(
        "SELECT o.id FROM outputs o JOIN actions a ON a.id=o.source_action_id"
        " WHERE a.device_id='cam-a'").fetchone()[0]
    driver = _CancelAfterQueryDriver(owned)
    action_id, item_id = _start_cleanup(owned, output_id, "2")
    driver.matrix_enabled, driver.present = True, present
    driver.on_query_return = lambda: _apply_cleanup_cancel(owned, action_id)

    step = await delete_source_file(_new_runtime(cfg, owned, driver).for_item(item_id), item_id)

    assert (step.phase, step.detail) == (phase, detail), step
    # 取消生效后不再发起任何设备调用。
    assert len(driver.deletes) == 1 and len(driver.queries) == 1
    facts = _member_facts(owned, item_id)
    assert facts[:4] == (status, int(_ITEM_RESTRICTION.IRREVERSIBLE), outcome,
                         None if code is None else item_error_id("cleanup_items", code))
    assert facts[4] is not None
    assert owned.connection.execute(
        "SELECT cancel_requested FROM actions WHERE id=?", (action_id,)).fetchone() == (1,)
    # 流程收束与取消错误一致：仍在为 FAILED，未确认为 UNCONFIRMED，
    # 确认缺席按 SUCCEEDED；两条流程都不再等待重试。
    runs = owned.connection.execute(
        "SELECT kind,status,retry_wait_required FROM operation_runs"
        " WHERE cleanup_item_id=? ORDER BY kind", (item_id,)).fetchall()
    assert runs == [(int(_KIND.DELETE_FILE), settled, 0),
                    (int(_KIND.CHECK_FILE_EXISTS), settled, 0)]

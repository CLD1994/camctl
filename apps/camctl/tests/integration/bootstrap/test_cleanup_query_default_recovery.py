"""默认清理轮次在新运行时和新连接续接原 QUERY 保存责任。

每轮经 cleanup_flow 打开和关闭真实 OwnedConnection，同一个
session_cleanup_assembly 工厂创建新的运行时。故障只注入真实
QUERY 结果事务的 COMMIT，不替换运行时或直接写入状态投影。
当前绑定改变的用例只验证原结果保存的优先级；后续普通调用仍由
既有绑定规则决定，不在这里重新定义绑定失败的业务结果。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from sqlite3 import ProgrammingError

import pytest

from camctl.bootstrap.cleanup_assembly import (
    HostArtifacts, cleanup_flow, session_cleanup_assembly,
)
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.contracts.workflow_errors import item_error_id
from camctl.devices.ports import DeviceCallResult
from camctl.operations.attempts import FinishDisposition, RunOutcome, StaleRunFinish
from camctl.outputs.cleanup_flow import PendingCleanupCompanion, delete_source_file
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository

from ..outputs.test_cleanup_query_commit_recovery import (
    _CommitFaultConnection, _assert_saved_result, _dependent_facts,
    _history, _query_row,
)
from ..outputs.test_cleanup_query_save_recovery import (
    _MatrixDriver, _apply_cleanup_cancel, _member_facts, _start_cleanup,
)
from .test_cleanup_flow import _seed_repaired_artifact
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

    async def query_file(self, request):
        if self.reject_new_calls:
            raise AssertionError("原 QUERY 恢复轮次不得重新查询设备")
        return await super().query_file(request)


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


class _DefaultCompanionFault:
    """目标成员伴随收场事务注入真实 COMMIT 前故障；其余事务真实执行。"""

    def __init__(self, original, item_id):
        self.original, self.item_id = original, item_id
        self.fault = None
        self.calls = 0

    def save(self, repository, request, key, owned):
        if f"exists/{self.item_id}" not in request.responsibility_keys:
            return self.original(repository, request, key, owned)
        self.calls += 1
        if self.fault is None:
            self.fault = _CommitFaultConnection(
                owned.connection, after_commit=False)
            return self.original(
                repository, request, key, replace(owned, connection=self.fault))
        return self.original(repository, request, key, owned)


async def test_default_round_settles_companion_after_member_result(
        environment, monkeypatch):
    """成员结果已提交、伴随收场中断：新会话默认轮次补齐且零设备调用。"""
    cfg, owned, context, capture_driver = environment
    await _save_photos(cfg, owned, context, capture_driver)
    output_id = owned.connection.execute(
        "SELECT o.id FROM outputs o JOIN actions a ON a.id=o.source_action_id"
        " WHERE a.device_id='cam-a'").fetchone()[0]
    driver = _DefaultDriver(owned)
    action_id, item_id = _start_cleanup(owned, output_id, "2")
    fault = _DefaultCompanionFault(
        OperationRepository.finish_stale_runs, item_id)
    monkeypatch.setattr(
        OperationRepository, "finish_stale_runs",
        lambda repository, request, key, current_owned:
            fault.save(repository, request, key, current_owned))
    driver.matrix_enabled, driver.present = True, False
    session_factory = session_cleanup_assembly(
        devices={device: dict(declaration) for device, declaration in cfg.devices.items()},
        drivers=_registry(driver), max_delete_attempts=3, max_query_attempts=3,
        staging=Path(cfg.paths.staging), occurred_at=lambda: _NOW, monotonic_ns=lambda: 0)

    with pytest.raises(ConsistencyError):
        await cleanup_flow(session_factory)(context)

    assert fault.calls == 1 and fault.fault.commit_calls == 1
    member = _member_facts(owned, item_id)
    assert member[:4] == (4, 4, 3, None) and member[4] is not None
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM operation_runs WHERE cleanup_item_id=? AND status IN (1,2)",
        (item_id,)).fetchone() == (2,)
    calls_before = (len(driver.deletes), len(driver.queries))
    history_before = _history(owned.connection)

    await cleanup_flow(session_factory)(context)

    assert fault.calls == 2
    assert (len(driver.deletes), len(driver.queries)) == calls_before
    assert _member_facts(owned, item_id) == member
    assert owned.connection.execute(
        "SELECT status,retry_wait_required FROM operation_runs"
        " WHERE cleanup_item_id=? ORDER BY id", (item_id,)).fetchall() == [(3, 0), (3, 0)]
    assert owned.connection.execute(
        "SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (3,)
    assert _history(owned.connection)[:len(history_before)] == history_before


async def test_default_round_diagnoses_companion_without_terminal_member(environment):
    """伴随责任存在而成员缺可靠终态：默认轮次诊断，不造终态继续。"""
    cfg, owned, context, capture_driver = environment
    await _save_photos(cfg, owned, context, capture_driver)
    output_id = owned.connection.execute(
        "SELECT o.id FROM outputs o JOIN actions a ON a.id=o.source_action_id"
        " WHERE a.device_id='cam-a'").fetchone()[0]
    driver = _DefaultDriver(owned)
    _action_id, item_id = _start_cleanup(owned, output_id, "2")
    database_before = tuple(owned.connection.iterdump())
    session_factory = session_cleanup_assembly(
        devices={device: dict(declaration) for device, declaration in cfg.devices.items()},
        drivers=_registry(driver), max_delete_attempts=3, max_query_attempts=3,
        staging=Path(cfg.paths.staging), occurred_at=lambda: _NOW, monotonic_ns=lambda: 0,
        pending_companions={item_id: PendingCleanupCompanion(
            owned.metadata.instance_id,
            StaleRunFinish((f"exists/{item_id}",), RunOutcome.CANCELED, _NOW),
            new_operation_key())})

    with pytest.raises(ConsistencyError, match="待存伴随流程没有可靠成员终态"):
        await cleanup_flow(session_factory)(context)

    assert tuple(owned.connection.iterdump()) == database_before
    assert (len(driver.deletes), len(driver.queries)) == (0, 0)


class _GatedQueryDriver(_DefaultDriver):
    """首次 QUERY 挂起等待放行：取消在途施加，原拥有者仍完成保存。"""

    def __init__(self, owned):
        super().__init__(owned)
        self.dispatched = asyncio.Event()
        self.release = asyncio.Event()

    async def query_file(self, request):
        if self.reject_new_calls:
            raise AssertionError("原 QUERY 在途期间不得重新查询设备")
        result = await super().query_file(request)
        self.dispatched.set()
        await self.release.wait()
        return result


async def test_waiter_cancel_keeps_original_query_owner(environment):
    """等待者取消期间实际 QUERY 由原拥有者收场，结果不降级不重查。"""
    cfg, owned, context, capture_driver = environment
    await _save_photos(cfg, owned, context, capture_driver)
    output_id = owned.connection.execute(
        "SELECT o.id FROM outputs o JOIN actions a ON a.id=o.source_action_id"
        " WHERE a.device_id='cam-a'").fetchone()[0]
    driver = _GatedQueryDriver(owned)
    driver.matrix_enabled, driver.present = True, None
    action_id, item_id = _start_cleanup(owned, output_id, "2")
    session_factory = session_cleanup_assembly(
        devices={device: dict(declaration) for device, declaration in cfg.devices.items()},
        drivers=_registry(driver), max_delete_attempts=3, max_query_attempts=3,
        staging=Path(cfg.paths.staging), occurred_at=lambda: _NOW, monotonic_ns=lambda: 0)
    flow = cleanup_flow(session_factory)

    task = asyncio.create_task(flow(context))
    try:
        await asyncio.wait_for(driver.dispatched.wait(), timeout=10)
        canceling = context.open_connection()
        try:
            _apply_cleanup_cancel(canceling, action_id)
        finally:
            canceling.connection.close()
        driver.release.set()
        await asyncio.wait_for(task, timeout=10)
    finally:
        if not task.done():
            driver.release.set()
            await task

    assert (len(driver.deletes), len(driver.queries)) == (1, 1)
    member = _member_facts(owned, item_id)
    assert member[:4] == (6, 4, None, item_error_id("cleanup_items", "delete_unconfirmed"))
    assert member[4] is not None
    # 原拥有者保存了实际结果：在途查询尝试有可靠结果事件与完整错误。
    row = owned.connection.execute(
        "SELECT a.status,a.result_event_id,a.result_json FROM operation_attempts a"
        " JOIN operation_runs r ON r.id=a.run_id WHERE r.cleanup_item_id=? AND r.kind=5",
        (item_id,)).fetchone()
    attempt_failed = int(enum_for("operation_attempts.status").FAILED)
    assert row[0] == attempt_failed and row[1] is not None  # 已可靠保存。
    assert json.loads(row[2])["observations"] == []
    assert owned.connection.execute(
        "SELECT status,retry_wait_required FROM operation_runs"
        " WHERE cleanup_item_id=? ORDER BY id", (item_id,)).fetchall() == [(6, 0), (6, 0)]
    assert owned.connection.execute(
        "SELECT cancel_requested,status FROM actions WHERE id=?",
        (action_id,)).fetchone() == (1, 2)

    # 另一轮只确认事实：不依据 DB 流程状态重查或改写成员终态。
    driver.reject_new_calls = True
    await flow(context)
    assert (len(driver.deletes), len(driver.queries)) == (1, 1)
    assert _member_facts(owned, item_id) == member


class _LocalProbe(HostArtifacts):
    """记录本地调用；删除按类级开关失败以进入存在性查询。"""

    instances = []
    fail_delete = False

    def __init__(self, staging):
        super().__init__(staging)
        self.delete_calls, self.query_calls = [], []
        _LocalProbe.instances.append(self)

    async def delete(self, request):
        self.delete_calls.append(request)
        if _LocalProbe.fail_delete:
            return DeviceCallResult(
                observations=(), error={"code": "local_io_failed", "stage": "delete"})
        return await super().delete(request)

    async def query_file(self, request):
        self.query_calls.append(request)
        return await super().query_file(request)


async def test_host_artifact_query_recovers_after_save_fault(
        environment, monkeypatch):
    """主机派生成品的本地查询保存失败后新轮次恢复；目标不落入设备。"""
    cfg, owned, context, capture_driver = environment
    await _save_photos(cfg, owned, context, capture_driver)
    output_id = owned.connection.execute(
        "SELECT o.id FROM outputs o JOIN actions a ON a.id=o.source_action_id"
        " WHERE a.device_id='cam-a'").fetchone()[0]
    _seed_repaired_artifact(Path(cfg.paths.state_db), cfg, output_id)
    # 删除调用将失败，而目标文件确已不在：查询按真实缺席核实。
    (Path(cfg.paths.staging) / "derived" / "900.jpg").unlink()
    _accept(owned, "2", [{
        "name": "清理主机成品", "type": "delete_action_outputs",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"output_ids": [str(output_id)]},
    }])
    action_id = owned.connection.execute(
        "SELECT id FROM actions WHERE type=5").fetchone()[0]
    devices = {device: dict(declaration) for device, declaration in cfg.devices.items()}
    driver = _DefaultDriver(owned)
    clock = {"wall": _NOW, "mono": 0}

    def probe(staging):
        return _LocalProbe(staging)

    monkeypatch.setattr(
        "camctl.bootstrap.cleanup_assembly.HostArtifacts", probe)
    session_factory = session_cleanup_assembly(
        devices=devices, drivers=_registry(driver),
        max_delete_attempts=3, max_query_attempts=3,
        staging=Path(cfg.paths.staging), occurred_at=lambda: clock["wall"],
        monotonic_ns=lambda: clock["mono"])
    fault = _DefaultQueryFault(
        OperationRepository.finish_attempt, action_id, output_id, after_commit=True)
    monkeypatch.setattr(
        OperationRepository, "finish_attempt",
        lambda repository, finish, key, current_owned:
            fault.save(repository, finish, key, current_owned))
    _LocalProbe.fail_delete = True

    with pytest.raises(ConsistencyError):
        await cleanup_flow(session_factory)(context)

    first = _LocalProbe.instances[-1]
    assert len(first.delete_calls) == 1 and len(first.query_calls) == 1
    assert fault.receipts[0].kind is DbOutcomeKind.UNKNOWN
    _finish, first_key = fault.calls[0][1], fault.calls[0][2]
    item_id = int(fault.calls[0][1].ticket.target_id)
    member_before = _member_facts(owned, item_id)
    assert member_before[0] == 3

    # 设备目录不再包含原设备：主机目标不得被误分为设备目标。
    devices.pop("cam-a")
    clock.update(wall=_NOW + 5_000, mono=900)
    driver.reject_new_calls = True
    await cleanup_flow(session_factory)(context)

    second = _LocalProbe.instances[-1]
    assert second is not first
    assert len(second.delete_calls) == 0 and len(second.query_calls) == 0
    member = _member_facts(owned, item_id)
    assert member[:4] == (4, 4, 3, None) and member[4] is not None
    assert owned.connection.execute(
        "SELECT status,retry_wait_required FROM operation_runs"
        " WHERE cleanup_item_id=? ORDER BY id", (item_id,)).fetchall() == [(3, 0), (3, 0)]
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
        (str(first_key),)).fetchone() == (1,)
    assert owned.connection.execute(
        "SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (3,)
    assert (len(driver.deletes), len(driver.queries)) == (0, 0)

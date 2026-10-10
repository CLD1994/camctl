"""实际 QUERY 结果保存失败时，三个清理消费者均停止推进。

初始化、受理、拍摄产物、清理归属、DELETE 和取消均走真实公开
事务。本文件的保存矩阵只在 finish_attempt 端口注入数据库结果，
用于证明观察消费门槛；真实 COMMIT 前后故障及 fresh 连接恢复由
后续恢复用例单独验证，UNKNOWN 替身不冒充实际提交故障。
"""

from __future__ import annotations

import json
from pathlib import Path
from sqlite3 import OperationalError

import pytest

from camctl.bootstrap.cleanup_assembly import session_cleanup_assembly
from camctl.cancellation.models import (
    ApplyCancelTarget, CancelApplyMode, CancellationEffect, FixCancelTargets,
    FixedCancelSet, FixedTarget, SelectionBasis, StartCancelAction,
)
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.contracts.workflow_errors import item_error_id
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import DeviceCallResult
from camctl.operations.attempts import FinishDisposition
from camctl.operations.models import (
    AttemptStatus, CallInfo, CallOutcome, EffectState, ErrorValue, EvidenceValue,
    Settlement, SettlementBasis,
)
from camctl.outputs.cleanup_flow import (
    FinishCleanupAction, FixCleanupTargets, RestrictCleanupItem,
    StartCleanupAction, _verify_before_delete, delete_source_file,
)
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.cancellation import CancellationRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import OutputsRepository

from ..bootstrap.test_output_binding_changes import (
    Driver, _NOW, _accept, _registry, _save_photos, environment,
)

pytestmark = pytest.mark.asyncio


def _apply_cleanup_cancel(owned, target_action_id: int) -> None:
    """公开取消事务明确指向当前成员所属动作，不选第一条清理。"""
    before = {row[0] for row in owned.connection.execute(
        "SELECT id FROM actions WHERE type=6")}
    _accept(owned, "4", [{
        "name": "取消当前清理", "type": "cancel_task",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"target": {"action_instance_id": str(target_action_id)}},
    }])
    origins = {row[0] for row in owned.connection.execute(
        "SELECT id FROM actions WHERE type=6")} - before
    assert len(origins) == 1
    origin = origins.pop()
    repository = CancellationRepository()
    started = repository.start_cancel_action(
        StartCancelAction(origin, _NOW), new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    fixed = repository.fix_cancel_targets(FixCancelTargets(
        origin, FixedCancelSet((FixedTarget(
            target_action_id, SelectionBasis.DIRECT,
            CancellationEffect.NOT_APPLIED),)), _NOW),
        new_operation_key(), owned)
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    applied = repository.apply_cancel_target(ApplyCancelTarget(
        fixed.value.item_ids[0], CancelApplyMode.WITH_STOP, _NOW),
        new_operation_key(), owned)
    assert applied.kind is DbOutcomeKind.COMPLETED, applied.error


class _MatrixDriver(Driver):
    """按真实端口提供完整失败结果；可靠观察可以与调用错误并存。"""

    def __init__(self, owned):
        super().__init__(owned)
        self.unknown_delete = True
        self.unknown_query = True
        self.matrix_enabled = False
        self.present = None
        self.on_delete_return = None
        self.last_query_result = None

    async def delete(self, request):
        result = await super().delete(request)
        if self.on_delete_return is not None:
            callback, self.on_delete_return = self.on_delete_return, None
            callback()
        return result

    async def query_state(self, request):
        if not self.matrix_enabled:
            return await super().query_state(request)
        self.query_requests.append(request)
        self.queries.append(request.binding)
        observations = () if self.present is None else (DeviceObservation(
            type="file_presence", version=1,
            data={"cleanup_item_id": request.params["cleanup_item_id"],
                  "present": self.present}),)
        error = ErrorValue("device_error", "query", {
            "reason": "调用返回错误，保留实际文件观察",
            "target": request.params["cleanup_item_id"],
            "diagnostic": {"remote_exit": 3},
        })
        outcome = CallOutcome(
            status=AttemptStatus.FAILED, error=error,
            effect=EffectState.UNKNOWN if self.present is None else EffectState.CONFIRMED,
            settlement=Settlement(
                SettlementBasis.OBSERVED,
                EvidenceValue("file_presence", 1, {})),
            observations=observations,
            call_info=CallInfo(local_exit_code=7, remote_exit_code=3),
        )
        self.last_query_result = DeviceCallResult(
            observations=observations,
            error={"code": "device_error", "stage": "query", "details": dict(error.details)},
            outcome=outcome,
        )
        return self.last_query_result


class _QueryFinishPort(OperationRepository):
    """仅替换目标 QUERY 的结果保存；其余仓储事务真实执行。"""

    def __init__(self, kind, item_id, driver, retry_gate):
        self.kind, self.item_id = kind, item_id
        self.driver, self.retry_gate = driver, retry_gate
        self.finishes = []
        self.checkpoint = None
        self.error = OperationalError("原 QUERY 结果保存故障")

    def finish_attempt(self, finish, key, owned):
        if (finish.ticket.operation != "query"
                or finish.ticket.target_id != str(self.item_id)):
            return super().finish_attempt(finish, key, owned)
        self.finishes.append((finish, key))
        assert self.driver.last_query_result is not None
        assert finish.outcome.outcome is self.driver.last_query_result.outcome
        self.checkpoint = (
            tuple(owned.connection.iterdump()),
            dict(self.retry_gate.anchors),
            len(self.driver.deletes), len(self.driver.queries),
        )
        if self.kind is not DbOutcomeKind.COMPLETED:
            return DbOutcome(kind=self.kind, error=self.error)
        result = super().finish_attempt(finish, key, owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value.disposition is FinishDisposition.SAVED
        return result


def _new_runtime(cfg, owned, driver, *, query_attempts=3):
    factory = session_cleanup_assembly(
        devices=cfg.devices, drivers=_registry(driver),
        max_delete_attempts=3, max_query_attempts=query_attempts,
        staging=Path(cfg.paths.staging), occurred_at=lambda: _NOW,
        monotonic_ns=lambda: 0,
    )
    return factory(owned)


def _start_cleanup(owned, output_id: int, request_id: str):
    """受理、开始、固定成员和建立可撤销限制均由公开仓储执行。"""
    previous = {row[0] for row in owned.connection.execute(
        "SELECT id FROM actions WHERE type=5")}
    _accept(owned, request_id, [{
        "name": "清理目标照片", "type": "delete_action_outputs",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"output_ids": [str(output_id)]},
    }])
    created = {row[0] for row in owned.connection.execute(
        "SELECT id FROM actions WHERE type=5")} - previous
    assert len(created) == 1
    action_id = created.pop()
    repository = OutputsRepository()
    started = repository.start_cleanup_action(
        StartCleanupAction(action_id, _NOW), new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    fixed = repository.fix_cleanup_targets(
        FixCleanupTargets(action_id, _NOW), new_operation_key(), owned)
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    assert len(fixed.value.item_ids) == 1
    item_id = fixed.value.item_ids[0]
    restricted = repository.restrict_cleanup_item(
        RestrictCleanupItem(item_id, _NOW), new_operation_key(), owned)
    assert restricted.kind is DbOutcomeKind.COMPLETED, restricted.error
    return action_id, item_id


def _member_facts(owned, item_id):
    return owned.connection.execute(
        "SELECT status,restriction_state,outcome,error_code,final_event_id"
        " FROM cleanup_items WHERE id=?", (item_id,)).fetchone()


def _original_member_rows(owned, action_id, item_id):
    return (
        owned.connection.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone(),
        owned.connection.execute("SELECT * FROM cleanup_items WHERE id=?", (item_id,)).fetchone(),
        tuple(owned.connection.execute(
            "SELECT * FROM operation_runs WHERE cleanup_item_id=? ORDER BY id", (item_id,))),
        tuple(owned.connection.execute(
            "SELECT a.* FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
            " WHERE r.cleanup_item_id=? ORDER BY a.id", (item_id,))),
    )


@pytest.mark.parametrize("kind", [
    DbOutcomeKind.COMPLETED, DbOutcomeKind.ROLLED_BACK, DbOutcomeKind.UNKNOWN,
], ids=["saved", "rolled_back", "unknown"])
@pytest.mark.parametrize("present", [True, False, None], ids=["present", "absent", "unknown"])
@pytest.mark.parametrize("consumer", ["before", "after", "cancel"])
async def test_query_save_matrix(environment, consumer, present, kind):
    """未可靠保存的实际观察不允许删除、成功、取消或伴随收场。"""
    cfg, owned, context, capture_driver = environment
    await _save_photos(cfg, owned, context, capture_driver)
    output_id = owned.connection.execute(
        "SELECT o.id FROM outputs o JOIN actions a ON a.id=o.source_action_id"
        " WHERE a.device_id='cam-a'").fetchone()[0]
    driver = _MatrixDriver(owned)
    old_member = None
    if consumer == "before":
        # C1 实际删除未知、查询未知，随后自己的查询预算可靠耗尽。
        old_action, old_item = _start_cleanup(owned, output_id, "2")
        previous_runtime = _new_runtime(cfg, owned, driver, query_attempts=1)
        first = await delete_source_file(previous_runtime, old_item)
        assert first.phase == "query_unknown", first
        exhausted = await delete_source_file(previous_runtime, old_item)
        assert (exhausted.phase, exhausted.detail) == (
            "failed", "file_query_attempts_exhausted"), exhausted
        finished = OutputsRepository().finish_cleanup_action(
            FinishCleanupAction(old_action, _NOW), new_operation_key(), owned)
        assert finished.kind is DbOutcomeKind.COMPLETED, finished.error
        old_member = (old_action, old_item, _original_member_rows(owned, old_action, old_item))
        action_id, item_id = _start_cleanup(owned, output_id, "3")
    else:
        action_id, item_id = _start_cleanup(owned, output_id, "2")

    base = _new_runtime(cfg, owned, driver)
    port = _QueryFinishPort(kind, item_id, driver, base.retry_gate)
    base.operations = port
    runtime = base.for_item(item_id)
    driver.matrix_enabled, driver.present = True, present
    queries_before = len(driver.queries)
    if consumer == "cancel":
        # 本项 DELETE 已真正调用；在返回未知结果时可靠施加本动作取消。
        driver.on_delete_return = lambda: _apply_cleanup_cancel(owned, action_id)

    error, step = None, None
    try:
        if consumer == "before":
            # 公开前置已建立 C1 终态及 C2 责任；这里只测删除前消费者。
            step = await _verify_before_delete(runtime, item_id, action_id, output_id)
        else:
            step = await delete_source_file(runtime, item_id)
    except ConsistencyError as caught:
        error = caught

    assert len(driver.queries) == queries_before + 1
    assert len(port.finishes) == 1
    assert port.checkpoint is not None
    assert owned.connection.execute(
        "SELECT attempts_used FROM operation_runs WHERE responsibility_key=?",
        (f"exists/{item_id}",)).fetchone() == (1,)
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
        " WHERE r.responsibility_key=?", (f"exists/{item_id}",)).fetchone() == (1,)
    if old_member is not None:
        old_action, old_item, before = old_member
        assert _original_member_rows(owned, old_action, old_item) == before

    if kind is not DbOutcomeKind.COMPLETED:
        # checkpoint 在 QUERY 已返回、首次保存前取得：保留此前的意图、
        # 额度、归属及实际 DELETE；其后任何结果、收场、等待变化都禁止。
        database, anchors, deletes, queries = port.checkpoint
        assert tuple(owned.connection.iterdump()) == database
        assert runtime.retry_gate.anchors == anchors
        assert len(driver.deletes) == deletes
        assert len(driver.queries) == queries
        assert isinstance(error, ConsistencyError), step
        assert owned.connection.execute(
            "SELECT a.status,a.result_event_id,a.result_json FROM operation_attempts a"
            " JOIN operation_runs r ON r.id=a.run_id WHERE r.responsibility_key=?",
            (f"exists/{item_id}",)).fetchone() == (1, None, None)
        return

    assert error is None, error
    row = owned.connection.execute(
        "SELECT a.status,a.effect_state,a.result_json,a.error_json FROM operation_attempts a"
        " JOIN operation_runs r ON r.id=a.run_id WHERE r.responsibility_key=?",
        (f"exists/{item_id}",)).fetchone()
    assert row[:2] == (
        int(enum_for("operation_attempts.status").FAILED),
        int(enum_for("operation_attempts.effect_state").UNKNOWN if present is None
            else enum_for("operation_attempts.effect_state").CONFIRMED),
    )
    assert json.loads(row[2]) == {
        "format_version": 1,
        "settlement": {"basis": "observed", "evidence": {
            "type": "file_presence", "version": 1, "data": {}}},
        "observations": [] if present is None else [{
            "type": "file_presence", "version": 1,
            "data": {"cleanup_item_id": str(item_id), "present": present}}],
        "call_info": {"local_exit": {"exit_code": 7}, "remote_exit_code": 3},
    }
    assert json.loads(row[3]) == {
        "code": "device_error", "stage": "query", "details": {
            "reason": "调用返回错误，保留实际文件观察", "target": str(item_id),
            "diagnostic": {"remote_exit": 3}}}
    facts = _member_facts(owned, item_id)
    if present is False:
        assert (step.phase, step.detail) == ("succeeded", "ABSENCE_CONFIRMED")
        assert facts[:4] == (4, 4, 3, None)
        assert facts[4] is not None
        assert owned.connection.execute(
            "SELECT status,retry_wait_required FROM operation_runs"
            " WHERE cleanup_item_id=? ORDER BY id", (item_id,)).fetchall() == (
                [(3, 0)] if consumer == "before" else [(3, 0), (3, 0)])
        assert f"exists/{item_id}" not in runtime.retry_gate.anchors
        assert f"delete/{item_id}" not in runtime.retry_gate.anchors
    elif consumer == "cancel":
        code = "delete_unconfirmed" if present is None else "file_delete_failed"
        assert (step.phase, step.detail) == ("canceled", code)
        assert facts[:4] == (6, 4, None, item_error_id("cleanup_items", code))
        assert facts[4] is not None
    else:
        if present is True and consumer == "before":
            assert step is None
        else:
            assert step.phase == ("still_present" if present is True else "query_unknown")
        assert facts[0] == 3 and facts[4] is None
    # 删除前只消费查询，其他两分区只有在 QUERY 之前的原 DELETE。
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
        " WHERE r.responsibility_key=?", (f"delete/{item_id}",)).fetchone() == (
            0 if consumer == "before" else 1,)
    if consumer == "cancel":
        assert owned.connection.execute(
            "SELECT cancel_requested FROM actions WHERE id=?", (action_id,)).fetchone() == (1,)

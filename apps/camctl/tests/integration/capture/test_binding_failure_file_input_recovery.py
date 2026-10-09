"""绑定收场使用可靠原文件输入，并按原申请返回首次父计划结果。"""

from dataclasses import replace
from datetime import datetime, timezone
import sqlite3

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode
from camctl.cancellation.models import (
    ApplyCancelTarget, CancelApplyMode, CancellationEffect, FixedCancelSet,
    FixedTarget, FixCancelTargets, SelectionBasis, StartCancelAction,
)
from camctl.capture.handlers import capture_handler
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput
from camctl.persistence.repositories.cancellation import CancellationRepository
from camctl.persistence.repositories.capture import CaptureRepository, FinishDisposition
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import saved_transaction_events

from .result_consumer_fixtures import ResultCatalog
from .test_binding_failure_keeps_timelapse_files import (
    _BindingSaveSpy, _assert_complete_binding_finish, _assert_complete_original_request,
    _assert_original_facts, _public_canceled_world,
)
from .test_closed_result_local_boundaries import _ClosedInputFault
from .test_closed_result_local_consumption import fresh_closed_runtime


pytestmark = pytest.mark.asyncio


def _cancel_pending_sibling(owned, sibling_id, occurred_at):
    """通过公开受理和 PRE_START 取消结束同计划内未开始的报告动作。"""
    assert owned.connection.execute(
        "SELECT type,status,cancel_requested FROM actions WHERE id=?",
        (sibling_id,)).fetchone() == (7, 1, 0)
    instant = datetime.fromtimestamp(occurred_at // 1_000_000, timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S")
    accepted = AcceptanceRepository().process_input(ProcessInput(ParsedInput(
        "cancel-pending-sibling.json", {
            "request_id": "3", "created_at": instant, "name": "取消同计划报告",
            "actions": [{"name": "取消报告", "type": "cancel_task", "params": {
                "target": {"action_instance_id": str(sibling_id)}}}],
        }), ResultCatalog(), CommandMode.RUN, occurred_at), new_operation_key(), owned)
    assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
    assert accepted.value.plan_id is not None
    cancel_id, = owned.connection.execute(
        "SELECT id FROM actions WHERE plan_id=? AND type=6",
        (accepted.value.plan_id,)).fetchone()
    repository = CancellationRepository()
    started = repository.start_cancel_action(StartCancelAction(cancel_id, occurred_at),
        new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    fixed = repository.fix_cancel_targets(FixCancelTargets(cancel_id, FixedCancelSet((
        FixedTarget(sibling_id, SelectionBasis.DIRECT, CancellationEffect.NOT_APPLIED),
    )), occurred_at), new_operation_key(), owned)
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    item_id, = fixed.value.item_ids
    applied = repository.apply_cancel_target(ApplyCancelTarget(
        item_id, CancelApplyMode.PRE_START, occurred_at), new_operation_key(), owned)
    assert applied.kind is DbOutcomeKind.COMPLETED, applied.error
    assert owned.connection.execute(
        "SELECT status,cancel_requested FROM actions WHERE id=?",
        (sibling_id,)).fetchone() == (6, 1)


@pytest.mark.parametrize("binding", ["missing", "mismatch"])
async def test_original_results_read_failure_preserves_binding_finish_responsibility(
        tmp_path, monkeypatch, binding):
    world = await _public_canceled_world(
        tmp_path, monkeypatch, "prior_complete_latest_failed")
    owned = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        assert owned.metadata == world.metadata
        before = tuple(owned.connection.iterdump())
        assert before == world.canceled_dump
        fault = _ClosedInputFault(owned.connection, world.activity_id)
        fault_owned = replace(owned, connection=fault)
        runtime, methods = fresh_closed_runtime(fault_owned, world, binding)
        saves = _BindingSaveSpy()
        runtime.capture = saves
        advance = capture_handler(world.handler)

        with pytest.raises(sqlite3.OperationalError) as caught:
            await advance(world.action_id, runtime)

        assert caught.value is fault.error
        assert fault.hits == 1 and not fault.armed
        assert not owned.connection.in_transaction
        assert tuple(owned.connection.iterdump()) == before
        assert owned.connection.execute(
            "SELECT status,cancel_requested FROM actions WHERE id=?",
            (world.action_id,)).fetchone() == (2, 1)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_runs WHERE responsibility_key=?",
            (f"stop/{world.action_id}",)).fetchone() == (0,)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
        assert saves.calls == [] and saves.receipts == []
        _assert_original_facts(owned, world, methods)

        # 读取恢复后仍由原实际处理器形成完整文件申请，一次事务结束必要 STOP 和目标。
        await advance(world.action_id, runtime)

        (request, key, saved_owned), = saves.calls
        receipt, = saves.receipts
        assert saved_owned is fault_owned
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        assert receipt.value.disposition is FinishDisposition.SAVED
        _assert_complete_original_request(request, world, runtime, binding)
        _assert_complete_binding_finish(owned, world, request, key)
        _assert_original_facts(owned, world, methods)
        assert fault.hits == 1
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
            (str(key),)).fetchone() == (1,)
        after = tuple(owned.connection.iterdump())
        await advance(world.action_id, runtime)
        assert tuple(owned.connection.iterdump()) == after
        assert len(saves.calls) == len(saves.receipts) == 1
        _assert_original_facts(owned, world, methods)
    finally:
        owned.connection.close()


async def test_binding_finish_original_key_returns_original_parent_plan_result(
        tmp_path, monkeypatch):
    world = await _public_canceled_world(tmp_path, monkeypatch, "latest_complete")
    owned = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        plan_id, = owned.connection.execute(
            "SELECT plan_id FROM actions WHERE id=?", (world.action_id,)).fetchone()
        (sibling_id, sibling_type, sibling_status), = owned.connection.execute(
            "SELECT id,type,status FROM actions WHERE plan_id=? AND id!=? ORDER BY id",
            (plan_id, world.action_id)).fetchall()
        assert (sibling_type, sibling_status) == (7, 1)
        assert owned.connection.execute(
            "SELECT status FROM plans WHERE id=?", (plan_id,)).fetchone() == (2,)
        runtime, methods = fresh_closed_runtime(owned, world, "missing")
        saves = _BindingSaveSpy()
        runtime.capture = saves

        await capture_handler(world.handler)(world.action_id, runtime)

        (request, key, saved_owned), = saves.calls
        receipt, = saves.receipts
        assert saved_owned is owned and receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        first = receipt.value
        assert first.disposition is FinishDisposition.SAVED
        assert (first.action_status, first.plan_status) == (6, 2)
        assert len(first.output_ids) == 1 and first.output_ids[0] > 0
        _assert_complete_original_request(request, world, runtime, "missing")
        _assert_complete_binding_finish(owned, world, request, key)
        original_group = saved_transaction_events(owned.connection, key)
        assert original_group is not None
        prefix = owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()
        assert owned.connection.execute(
            "SELECT type,status FROM actions WHERE id=?", (sibling_id,)).fetchone() == (7, 1)

        _cancel_pending_sibling(owned, sibling_id, request.occurred_at + 1_000_000)

        assert owned.connection.execute(
            "SELECT status FROM plans WHERE id=?", (plan_id,)).fetchone() == (3,)
        assert owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()[
            :len(prefix)] == prefix
        assert saved_transaction_events(owned.connection, key) == original_group
        _assert_original_facts(owned, world, methods)
    finally:
        owned.connection.close()

    # 现父计划已完成；原键重送必须返回首次完整响应，不用后到的兄弟终态改写原结果。
    reopened = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        assert reopened.metadata == world.metadata
        before = tuple(reopened.connection.iterdump())

        recovered = CaptureRepository().finish_binding_failure(request, key, reopened)

        assert recovered.kind is DbOutcomeKind.COMPLETED, recovered.error
        assert recovered.value.disposition is FinishDisposition.ALREADY
        assert (recovered.value.action_status, recovered.value.plan_status,
                recovered.value.output_ids) == (first.action_status, first.plan_status, first.output_ids)
        assert tuple(reopened.connection.iterdump()) == before
        assert saved_transaction_events(reopened.connection, key) == original_group
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
            (str(key),)).fetchone() == (1,)
        _assert_original_facts(reopened, world, methods)
    finally:
        reopened.connection.close()


class _RecoveryReadFault:
    """透传真实连接，仅在固定原键或父计划目录读取时返回一次 SQLite 错误。"""

    def __init__(self, connection, statement, parameters):
        self.connection = connection
        self.statement = statement
        self.parameters = parameters
        self.armed = True
        self.hits = 0
        self.rollback_count = 0
        self.fault_transaction = False
        self.in_transaction_at_failure = None
        self.error = sqlite3.OperationalError("绑定原申请恢复读取失败")

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, statement, parameters=()):
        normalized = " ".join(statement.split())
        if self.armed and normalized == self.statement and tuple(parameters) == self.parameters:
            self.armed = False
            self.hits += 1
            self.fault_transaction = True
            self.in_transaction_at_failure = self.connection.in_transaction
            raise self.error
        result = self.connection.execute(statement, parameters)
        if normalized == "ROLLBACK" and self.fault_transaction:
            self.rollback_count += 1
            self.fault_transaction = False
        return result


async def _saved_binding_with_completed_parent(tmp_path, monkeypatch):
    """首次响应的父计划仍执行中，随后公开取消兄弟动作使父计划完成。"""
    world = await _public_canceled_world(tmp_path, monkeypatch, "latest_complete")
    owned = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        plan_id, = owned.connection.execute(
            "SELECT plan_id FROM actions WHERE id=?", (world.action_id,)).fetchone()
        (sibling_id, sibling_type, sibling_status), = owned.connection.execute(
            "SELECT id,type,status FROM actions WHERE plan_id=? AND id!=? ORDER BY id",
            (plan_id, world.action_id)).fetchall()
        assert (sibling_type, sibling_status) == (7, 1)
        runtime, methods = fresh_closed_runtime(owned, world, "missing")
        saves = _BindingSaveSpy()
        runtime.capture = saves

        await capture_handler(world.handler)(world.action_id, runtime)

        (request, key, saved_owned), = saves.calls
        receipt, = saves.receipts
        assert saved_owned is owned and receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        first = receipt.value
        assert first.disposition is FinishDisposition.SAVED
        assert (first.action_status, first.plan_status) == (6, 2)
        assert len(first.output_ids) == 1 and first.output_ids[0] > 0
        _assert_complete_original_request(request, world, runtime, "missing")
        _assert_complete_binding_finish(owned, world, request, key)
        original_group = saved_transaction_events(owned.connection, key)
        assert original_group is not None
        _cancel_pending_sibling(owned, sibling_id, request.occurred_at + 1_000_000)
        assert owned.connection.execute(
            "SELECT status FROM plans WHERE id=?", (plan_id,)).fetchone() == (3,)
        assert saved_transaction_events(owned.connection, key) == original_group
        _assert_original_facts(owned, world, methods)
        return world, request, key, first, original_group, plan_id, methods
    finally:
        owned.connection.close()


async def _assert_recovery_read_failure(tmp_path, monkeypatch, *, parent_history):
    world, request, key, first, original_group, plan_id, methods = (
        await _saved_binding_with_completed_parent(tmp_path, monkeypatch))
    owned = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        assert owned.metadata == world.metadata
        assert owned.connection.execute(
            "SELECT status FROM plans WHERE id=?", (plan_id,)).fetchone() == (3,)
        before = tuple(owned.connection.iterdump())
        if parent_history:
            # 限定原 G 末边界的父计划恢复，必须先核对真实 plans 对象的目录头。
            fault = _RecoveryReadFault(owned.connection,
                "SELECT last_event_id, change_count FROM plans WHERE id = ?", (plan_id,))
        else:
            fault = _RecoveryReadFault(owned.connection,
                "SELECT id FROM history_transactions WHERE operation_key = ?", (str(key),))
        fault_owned = replace(owned, connection=fault)
        repository = CaptureRepository()

        failed = repository.finish_binding_failure(request, key, fault_owned)

        assert fault.hits == 1 and not fault.armed
        assert fault.in_transaction_at_failure is True
        assert failed.kind is DbOutcomeKind.ROLLED_BACK
        assert failed.error is fault.error
        assert failed.value is None
        assert fault.rollback_count == 1 and not owned.connection.in_transaction
        assert tuple(owned.connection.iterdump()) == before
        assert saved_transaction_events(owned.connection, key) == original_group
        _assert_original_facts(owned, world, methods)

        # 解除同一连接读取故障后，只复核原完整申请和键，不返回晚到的父计划状态。
        recovered = repository.finish_binding_failure(request, key, fault_owned)

        assert recovered.kind is DbOutcomeKind.COMPLETED, recovered.error
        assert recovered.value.disposition is FinishDisposition.ALREADY
        assert (recovered.value.action_status, recovered.value.plan_status,
                recovered.value.output_ids) == (first.action_status, first.plan_status, first.output_ids)
        assert tuple(owned.connection.iterdump()) == before
        assert saved_transaction_events(owned.connection, key) == original_group
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
            (str(key),)).fetchone() == (1,)
        assert fault.hits == fault.rollback_count == 1
        assert not owned.connection.in_transaction
        _assert_original_facts(owned, world, methods)
    finally:
        owned.connection.close()


async def test_binding_original_key_sql_failure_rolls_back_before_response(
        tmp_path, monkeypatch):
    await _assert_recovery_read_failure(tmp_path, monkeypatch, parent_history=False)


async def test_binding_original_parent_history_sql_failure_rolls_back_before_response(
        tmp_path, monkeypatch):
    await _assert_recovery_read_failure(tmp_path, monkeypatch, parent_history=True)

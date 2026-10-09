"""取消延时摄影的必要 STOP 因绑定失败结束时仍登记可靠完整文件。"""

from dataclasses import replace
import json
import sqlite3

import pytest

from camctl.cancellation.models import FinishCancelAction
from camctl.cancellation.service import ApplyCancel, CancellationRuntime, apply_cancel
from camctl.cancellation.settlement import TargetSettlement
from camctl.capture.handlers import capture_handler
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.contracts.workflow_errors import registered_error
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.cancellation import CancellationRepository
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import saved_transaction_events
from camctl.outputs.catalog import FileReference, OutputCatalogFacts, OutputDraft, OutputKind

from .test_cancel_timelapse_exhaustion_files import _apply_public_cancel
from .test_closed_result_local_consumption import (
    _capture_facts, _rows, closed_consumer_world, fresh_closed_runtime,
)


pytestmark = pytest.mark.asyncio


class _BindingSaveSpy(CaptureRepository):
    """记录完整实际申请，实际事务和回执仍由真实仓储产生。"""

    def __init__(self):
        self.calls = []
        self.receipts = []

    def finish_binding_failure(self, request, key, owned):
        self.calls.append((request, key, owned))
        receipt = super().finish_binding_failure(request, key, owned)
        self.receipts.append(receipt)
        return receipt


def _assert_joint_binding_finish(owned, world, key, stop_id, output_id):
    """原键的权威终态事件确定事务身份，不依赖后续派生元数据。"""
    events = saved_transaction_events(owned.connection, key)
    assert events is not None
    target = [event for event in events if event["type"] == 8 and any(
        row["table"] == "actions" and row["id"] == world.action_id
        and row["after"]["values"].get("status") == 6 for row in event["body"]["rows"])]
    stop = [event for event in events if (event["type"], event["reason"]) == (10, 3) and any(
        row["table"] == "operation_runs" and row["id"] == stop_id
        and row["after"]["values"].get("status") == 4 for row in event["body"]["rows"])]
    assert len(target) == len(stop) == 1
    transaction_id, = owned.connection.execute(
        "SELECT id FROM history_transactions WHERE operation_key=?", (str(key),)).fetchone()
    output_transaction, = owned.connection.execute(
        "SELECT e.transaction_id FROM outputs o JOIN history_events e ON e.id=o.created_event_id"
        " WHERE o.id=?", (output_id,)).fetchone()
    assert output_transaction == transaction_id
    assert owned.connection.execute(
        "SELECT transaction_id FROM history_events WHERE id IN (?,?) ORDER BY id",
        (target[0]["event_id"], stop[0]["event_id"])).fetchall() == [(transaction_id,), (transaction_id,)]


async def _public_canceled_world(tmp_path, monkeypatch, history):
    world = await closed_consumer_world(tmp_path, monkeypatch,
        consumer="timelapse", history=history)
    preparing = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        assert preparing.metadata == world.metadata
        assert preparing.connection.execute(
            "SELECT activity_state,stop_supported FROM device_activities WHERE id=?",
            (world.activity_id,)).fetchone() == (1, 1)
        assert preparing.connection.execute(
            "SELECT COUNT(*) FROM operation_runs WHERE responsibility_key=?",
            (f"stop/{world.action_id}",)).fetchone() == (0,)
        _apply_public_cancel(preparing, world.action_id, world.formed_at + 10)
        assert preparing.connection.execute(
            "SELECT status,cancel_requested FROM actions WHERE id=?", (world.action_id,)).fetchone() == (2, 1)
        world.cancel_prefix = _rows(preparing, "history_events")
        world.cancel_transaction_prefix = _rows(preparing, "history_transactions")
        world.canceled_dump = tuple(preparing.connection.iterdump())
        return world
    finally:
        preparing.connection.close()


def _assert_complete_original_request(request, world, runtime, binding):
    assert request.action_id == world.action_id and request.canceled
    assert request.occurred_at == world.formed_at + 5_000_000
    assert request.responsibility_keys == ()
    assert request.stop_config == runtime.stop_config and request.check_config == runtime.check_config
    details = {"device_id": "cam-1", "expected_driver_id": "camctl-adb", "reason": binding}
    if binding == "mismatch":
        details["actual_driver_id"] = "alternate-camera"
    assert request.failure.code == "device_binding_unavailable" and request.failure.details == details
    assert request.drafts == (OutputDraft(OutputKind.ORIGINAL,
        FileReference(device_file_id=world.file_id), file_complete=True),)
    assert request.catalog_facts == OutputCatalogFacts(world.action_id, ownership_confirmed=True)


def _assert_original_facts(owned, world, methods):
    assert owned.metadata == world.metadata
    assert saved_transaction_events(owned.connection, world.key) == world.close_events
    assert _capture_facts(owned, world.action_id) == world.capture_facts
    assert _rows(owned, "device_files") == world.files
    assert _rows(owned, "operation_attempts") == world.attempts
    for run in world.runs:
        assert owned.connection.execute("SELECT * FROM operation_runs WHERE id=?", (run[0],)).fetchone() == run
    assert _rows(owned, "history_events")[:len(world.cancel_prefix)] == world.cancel_prefix
    assert _rows(owned, "history_transactions")[:len(world.cancel_transaction_prefix)] == world.cancel_transaction_prefix
    world.control_driver.control.assert_awaited_once()
    assert world.result_driver.list_results.await_count == world.used
    for method in methods:
        method.assert_not_called()


def _assert_complete_binding_finish(owned, world, request, key):
    assert owned.connection.execute(
        "SELECT status,cancel_requested FROM actions WHERE id=?", (world.action_id,)).fetchone() == (6, 1)
    stop_id, status, used, error = owned.connection.execute(
        "SELECT id,status,attempts_used,error_json FROM operation_runs WHERE responsibility_key=?",
        (f"stop/{world.action_id}",)).fetchone()
    assert (status, used) == (4, 0)
    assert json.loads(error) == {"code": "device_binding_unavailable", "stage": "execution",
                               "details": request.failure.details}
    outputs = owned.connection.execute(
        "SELECT o.id,o.source_action_id,o.kind,o.device_file_id,o.original_name,o.media_type,f.size_bytes"
        " FROM outputs o JOIN device_files f ON f.id=o.device_file_id WHERE o.source_action_id=?",
        (world.action_id,)).fetchall()
    assert len(outputs) == 1
    (output_id, *output), = outputs
    assert output_id > 0 and tuple(output) == world.expected_output
    _assert_joint_binding_finish(owned, world, key, stop_id, output_id)


@pytest.mark.parametrize("binding", ["missing", "mismatch"])
@pytest.mark.parametrize("history", ["latest_complete", "prior_complete_latest_failed"])
async def test_binding_failed_stop_keeps_complete_timelapse_files_atomically(
        tmp_path, monkeypatch, binding, history):
    world = await closed_consumer_world(tmp_path, monkeypatch,
        consumer="timelapse", history=history)
    preparing = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        assert preparing.metadata == world.metadata
        assert preparing.connection.execute(
            "SELECT activity_state,stop_supported FROM device_activities WHERE id=?",
            (world.activity_id,)).fetchone() == (1, 1)
        assert preparing.connection.execute(
            "SELECT COUNT(*) FROM operation_runs WHERE responsibility_key=?",
            (f"stop/{world.action_id}",)).fetchone() == (0,)
        _apply_public_cancel(preparing, world.action_id, world.formed_at + 10)
        assert preparing.connection.execute(
            "SELECT status,cancel_requested FROM actions WHERE id=?",
            (world.action_id,)).fetchone() == (2, 1)
        assert _capture_facts(preparing, world.action_id) == world.capture_facts
        assert _rows(preparing, "operation_attempts") == world.attempts
        assert _rows(preparing, "device_files") == world.files
        prefix = _rows(preparing, "history_events")
        transaction_prefix = _rows(preparing, "history_transactions")
        definition = preparing.connection.execute(
            "SELECT scheduled_at,max_delay_ms,execution_started,effective_params_json,execution_spec_json,"
            " device_id,driver_id FROM actions WHERE id=?", (world.action_id,)).fetchone()
        wait = preparing.connection.execute(
            "SELECT sent_at,started_at,expected_check_at,result_wait_margin_ms,extra_wait_ms_used,"
            " completion_evidence_json FROM device_activities WHERE id=?", (world.activity_id,)).fetchone()
        cancel_id, item_id = preparing.connection.execute(
            "SELECT action_id,id FROM cancel_items WHERE target_action_id=?",
            (world.action_id,)).fetchone()
    finally:
        preparing.connection.close()

    owned = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        assert owned.metadata == world.metadata
        runtime, methods = fresh_closed_runtime(owned, world, binding)
        saves = _BindingSaveSpy()
        runtime.capture = saves
        advance = capture_handler(world.handler)

        await advance(world.action_id, runtime)

        (request, key, saved_owned), = saves.calls
        receipt, = saves.receipts
        assert saved_owned is owned and receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        assert request.action_id == world.action_id and request.canceled
        assert request.occurred_at == world.formed_at + 5_000_000
        assert request.responsibility_keys == ()
        assert request.stop_config == runtime.stop_config
        assert request.check_config == runtime.check_config
        details = {"device_id": "cam-1", "expected_driver_id": "camctl-adb", "reason": binding}
        if binding == "mismatch":
            details["actual_driver_id"] = "alternate-camera"
        assert request.failure.code == "device_binding_unavailable"
        assert request.failure.details == details
        binding_events = saved_transaction_events(owned.connection, key)
        assert binding_events is not None
        assert owned.connection.execute(
            "SELECT status,cancel_requested,driver_id FROM actions WHERE id=?",
            (world.action_id,)).fetchone() == (6, 1, "camctl-adb")
        stop_id, stop_status, used, stop_error = owned.connection.execute(
            "SELECT id,status,attempts_used,error_json FROM operation_runs WHERE responsibility_key=?",
            (f"stop/{world.action_id}",)).fetchone()
        assert (stop_status, used) == (4, 0)
        assert json.loads(stop_error) == {"code": "device_binding_unavailable",
            "stage": "execution", "details": details}
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts WHERE run_id=?", (stop_id,)).fetchone() == (0,)
        assert _rows(owned, "operation_attempts") == world.attempts
        for original_run in world.runs:
            assert owned.connection.execute(
                "SELECT * FROM operation_runs WHERE id=?", (original_run[0],)).fetchone() == original_run
        assert owned.connection.execute(
            "SELECT * FROM operation_runs WHERE id=?", (world.run_id,)).fetchone() == next(
                row for row in world.runs if row[0] == world.run_id)
        assert saved_transaction_events(owned.connection, world.key) == world.close_events
        assert _capture_facts(owned, world.action_id) == world.capture_facts
        assert owned.connection.execute(
            "SELECT scheduled_at,max_delay_ms,execution_started,effective_params_json,execution_spec_json,"
            " device_id,driver_id FROM actions WHERE id=?", (world.action_id,)).fetchone() == definition
        assert owned.connection.execute(
            "SELECT sent_at,started_at,expected_check_at,result_wait_margin_ms,extra_wait_ms_used,"
            " completion_evidence_json FROM device_activities WHERE id=?", (world.activity_id,)).fetchone() == wait
        assert _rows(owned, "device_files") == world.files
        assert _rows(owned, "history_events")[:len(prefix)] == prefix
        assert _rows(owned, "history_transactions")[:len(transaction_prefix)] == transaction_prefix
        for method in methods:
            method.assert_not_called()
        world.control_driver.control.assert_awaited_once()
        assert world.result_driver.list_results.await_count == world.used

        # 目标 canceled 与发起者的处理失败分别保存，不能把目标终态当取消成功。
        cancellations = CancellationRepository()
        settlement = TargetSettlement(owned, None, cancellations, lambda _: "unknown", runtime.wall_us)
        settled = await settlement.settle(world.action_id)
        assert settled.complete and settled.failed
        progress = await apply_cancel(ApplyCancel(cancel_id, (item_id,)), CancellationRuntime(
            owned, cancellations, settlement, runtime.wall_us))
        item, = progress.items
        assert item.status == 4
        assert item.error_code == registered_error("target_cleanup_failed")["item_error_ids"]["cancel_items"]
        assert json.loads(item.error_details_json) == {"action_instance_id": str(world.action_id)}
        finished = cancellations.finish_cancel_action(
            FinishCancelAction(cancel_id, runtime.wall_us()), new_operation_key(), owned)
        assert finished.kind is DbOutcomeKind.COMPLETED, finished.error
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id=?", (cancel_id,)).fetchone() == (4,)

        outputs = owned.connection.execute(
            "SELECT o.id,o.source_action_id,o.kind,o.device_file_id,o.original_name,o.media_type,f.size_bytes"
            " FROM outputs o JOIN device_files f ON f.id=o.device_file_id WHERE o.source_action_id=?",
            (world.action_id,)).fetchall()
        assert len(outputs) == 1, "必要 STOP 失败仍须保留已可靠完成且归属明确的原文件"
        (output_id, *output), = outputs
        assert output_id > 0 and tuple(output) == world.expected_output
        _assert_joint_binding_finish(owned, world, key, stop_id, output_id)

        terminal = tuple(owned.connection.iterdump())
        await advance(world.action_id, runtime)
        assert tuple(owned.connection.iterdump()) == terminal
        assert len(saves.calls) == 1
        for method in methods:
            method.assert_not_called()
        world.control_driver.control.assert_awaited_once()
        assert world.result_driver.list_results.await_count == world.used
    finally:
        owned.connection.close()


@pytest.mark.parametrize("binding", ["missing", "mismatch"])
@pytest.mark.parametrize("history", ["latest_complete", "prior_complete_latest_failed"])
async def test_binding_file_finish_reuses_full_request_with_original_key_after_reopen(
        tmp_path, monkeypatch, binding, history):
    world = await _public_canceled_world(tmp_path, monkeypatch, history)
    owned = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    reopened = None
    try:
        runtime, methods = fresh_closed_runtime(owned, world, binding)
        saves = _BindingSaveSpy()
        runtime.capture = saves
        await capture_handler(world.handler)(world.action_id, runtime)
        (request, key, _owned), = saves.calls
        receipt, = saves.receipts
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        _assert_complete_original_request(request, world, runtime, binding)
        _assert_original_facts(owned, world, methods)
        _assert_complete_binding_finish(owned, world, request, key)
        reliable = tuple(owned.connection.iterdump())
        binding_events = saved_transaction_events(owned.connection, key)
        owned.connection.close()
        reopened = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())

        replay = CaptureRepository().finish_binding_failure(request, key, reopened)

        assert replay.kind is DbOutcomeKind.COMPLETED, replay.error
        assert tuple(reopened.connection.iterdump()) == reliable
        assert saved_transaction_events(reopened.connection, key) == binding_events
        _assert_original_facts(reopened, world, methods)
        _assert_complete_binding_finish(reopened, world, request, key)
        # 重新装配新会话只是核终态重入；仓储重送不依赖设备或新时刻。
        next_runtime, next_methods = fresh_closed_runtime(reopened, world, binding)
        await capture_handler(world.handler)(world.action_id, next_runtime)
        assert tuple(reopened.connection.iterdump()) == reliable
        for method in next_methods:
            method.assert_not_called()
    finally:
        owned.connection.close()
        if reopened is not None:
            reopened.connection.close()


class _BindingTransactionFault:
    """只在写入产物的绑定事务内注入真实投影或 COMMIT 错误。"""

    def __init__(self, connection, mode):
        self.connection, self.mode = connection, mode
        self.armed = True
        self.fault_transaction = False
        self.hits = 0
        self.commit_calls = 0
        self.rollback_count = 0
        self.partial_outputs = None
        self.outputs_after_rollback = None
        self.error = sqlite3.OperationalError("绑定终态事务保存故障")

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, statement, parameters=()):
        if self.armed and self.fault_transaction and statement == "COMMIT":
            self.armed = False
            self.hits += 1
            self.commit_calls += 1
            if self.mode == "commit_after":
                self.connection.execute(statement, parameters)
            raise self.error
        result = self.connection.execute(statement, parameters)
        if self.armed and statement.startswith("INSERT INTO outputs "):
            self.fault_transaction = True
            self.partial_outputs = self.connection.execute("SELECT COUNT(*) FROM outputs").fetchone()[0]
            if self.mode == "projection":
                self.armed = False
                self.hits += 1
                raise self.error
        if self.fault_transaction and statement == "ROLLBACK":
            self.rollback_count += 1
            self.outputs_after_rollback = self.connection.execute("SELECT COUNT(*) FROM outputs").fetchone()[0]
            self.fault_transaction = False
        return result


class _FaultingBindingSaveSpy(_BindingSaveSpy):
    """实际保存入口取得透明连接代理，其余 handler 读取不注入故障。"""

    def __init__(self, proxy):
        super().__init__()
        self.proxy = proxy

    def finish_binding_failure(self, request, key, owned):
        return super().finish_binding_failure(request, key, replace(owned, connection=self.proxy))


@pytest.mark.parametrize("binding", ["missing", "mismatch"])
@pytest.mark.parametrize("mode", ["projection", "commit_before", "commit_after"])
async def test_binding_file_transaction_fault_recovers_full_original_request_after_reopen(
        tmp_path, monkeypatch, binding, mode):
    world = await _public_canceled_world(tmp_path, monkeypatch, "prior_complete_latest_failed")
    owned = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    reopened = None
    try:
        runtime, methods = fresh_closed_runtime(owned, world, binding)
        fault = _BindingTransactionFault(owned.connection, mode)
        saves = _FaultingBindingSaveSpy(fault)
        runtime.capture = saves

        with pytest.raises(ConsistencyError):
            await capture_handler(world.handler)(world.action_id, runtime)

        (request, key, fault_owned), = saves.calls
        receipt, = saves.receipts
        assert fault_owned.connection is fault and fault.hits == 1 and not fault.armed
        assert fault.partial_outputs == 1
        assert receipt.error is fault.error
        assert receipt.kind is (DbOutcomeKind.ROLLED_BACK if mode == "projection" else DbOutcomeKind.UNKNOWN)
        _assert_complete_original_request(request, world, runtime, binding)
        if mode == "projection":
            assert fault.commit_calls == 0
            assert fault.rollback_count == 1 and fault.outputs_after_rollback == 0
            assert tuple(owned.connection.iterdump()) == world.canceled_dump
        else:
            assert fault.commit_calls == 1
        owned.connection.close()
        reopened = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
        _assert_original_facts(reopened, world, methods)
        already_reliable = mode == "commit_after"
        assert (saved_transaction_events(reopened.connection, key) is not None) is already_reliable
        if already_reliable:
            _assert_complete_binding_finish(reopened, world, request, key)
            original_saved_dump = tuple(reopened.connection.iterdump())
        else:
            assert tuple(reopened.connection.iterdump()) == world.canceled_dump
            assert reopened.connection.execute(
                "SELECT status,cancel_requested FROM actions WHERE id=?", (world.action_id,)).fetchone() == (2, 1)
            assert reopened.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
            assert reopened.connection.execute(
                "SELECT COUNT(*) FROM operation_runs WHERE responsibility_key=?",
                (f"stop/{world.action_id}",)).fetchone() == (0,)

        # 仅证明真实仓储恢复；原 request/key/T1 由 spy 保存，不声称 handler 已持有它们。
        recovered = CaptureRepository().finish_binding_failure(request, key, reopened)

        assert recovered.kind is DbOutcomeKind.COMPLETED, recovered.error
        if already_reliable:
            assert tuple(reopened.connection.iterdump()) == original_saved_dump
        _assert_original_facts(reopened, world, methods)
        _assert_complete_binding_finish(reopened, world, request, key)
        completed = tuple(reopened.connection.iterdump())
        again = CaptureRepository().finish_binding_failure(request, key, reopened)
        assert again.kind is DbOutcomeKind.COMPLETED, again.error
        assert tuple(reopened.connection.iterdump()) == completed
    finally:
        owned.connection.close()
        if reopened is not None:
            reopened.connection.close()

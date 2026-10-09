"""CLOSED 本地收尾的读取、原子保存及必要 STOP 资格边界。"""

from dataclasses import replace
import asyncio
import json
import sqlite3

import pytest

from camctl.capture.handlers import capture_handler
from camctl.contracts.values import ConsistencyError
from camctl.contracts.workflow_errors import registered_error
from camctl.devices.ports import DeviceCallResult
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import saved_transaction_events

from .result_consumer_fixtures import returned
from .test_cancel_timelapse_exhaustion_files import _apply_public_cancel
from .test_closed_result_local_consumption import (
    _assert_local_finish, _assert_preserved, _capture_facts, _rows,
    closed_consumer_world, fresh_closed_runtime,
)


pytestmark = pytest.mark.asyncio


class _ClosedInputFault:
    """其他连接操作透传，只让原 RESULTS 输入读取返回一次 SQLite 错误。"""

    def __init__(self, connection, activity_id):
        self.connection = connection
        self.activity_id = activity_id
        self.armed = True
        self.hits = 0
        self.error = sqlite3.OperationalError("原 RESULTS 输入读取失败")

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, statement, parameters=()):
        is_original_input = (
            "FROM operation_runs r" in statement
            and "JOIN operation_attempts t" in statement
            and "t.result_json" in statement
            and tuple(parameters) == (f"results/{self.activity_id}",))
        if self.armed and is_original_input:
            self.armed = False
            self.hits += 1
            raise self.error
        return self.connection.execute(statement, parameters)


class _TerminalProjectionFault:
    """真实投影写入产物后抛 SQLite 错误，事务执行层必须可靠回滚。"""

    def __init__(self, connection):
        self.connection = connection
        self.armed = True
        self.hits = 0
        self.fault_transaction = False
        self.rollback_count = 0
        self.output_count_before_rollback = None
        self.output_count_after_rollback = None
        self.error = sqlite3.OperationalError("产物投影写入后失败")

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, statement, parameters=()):
        result = self.connection.execute(statement, parameters)
        if self.armed and statement.startswith("INSERT INTO outputs "):
            self.armed = False
            self.hits += 1
            self.fault_transaction = True
            self.output_count_before_rollback = self.connection.execute(
                "SELECT COUNT(*) FROM outputs").fetchone()[0]
            raise self.error
        if statement == "ROLLBACK" and self.fault_transaction:
            self.rollback_count += 1
            self.output_count_after_rollback = self.connection.execute(
                "SELECT COUNT(*) FROM outputs").fetchone()[0]
            self.fault_transaction = False
        return result


class _TerminalReceiptSpy(CaptureRepository):
    """保留真实复合事务，仅记录公开保存入口的实际回执。"""

    def __init__(self):
        self.receipts = []

    def finish_capture(self, request, key, owned):
        receipt = super().finish_capture(request, key, owned)
        self.receipts.append(receipt)
        return receipt

    def finish_canceled_capture(self, request, key, owned):
        receipt = super().finish_canceled_capture(request, key, owned)
        self.receipts.append(receipt)
        return receipt


@pytest.mark.parametrize("consumer", ["photo", "timelapse", "canceled_timelapse"])
@pytest.mark.parametrize("binding", ["missing", "mismatch"])
async def test_closed_input_sqlite_error_preserves_local_responsibility(
        tmp_path, monkeypatch, consumer, binding):
    world = await closed_consumer_world(tmp_path, monkeypatch,
        consumer=consumer, history="prior_complete_latest_failed")
    owned = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        before = tuple(owned.connection.iterdump())
        fault = _ClosedInputFault(owned.connection, world.activity_id)
        runtime, methods = fresh_closed_runtime(replace(owned, connection=fault), world, binding)
        advance = capture_handler(world.handler)

        with pytest.raises(sqlite3.OperationalError) as raised:
            await advance(world.action_id, runtime)

        assert raised.value is fault.error
        assert fault.hits == 1 and not fault.armed
        assert tuple(owned.connection.iterdump()) == before
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id=?", (world.action_id,)).fetchone() == (2,)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
        _assert_preserved(owned, world, methods)

        await advance(world.action_id, runtime)
        _assert_local_finish(owned, world, methods)
        completed = tuple(owned.connection.iterdump())
        await advance(world.action_id, runtime)
        assert tuple(owned.connection.iterdump()) == completed
        _assert_local_finish(owned, world, methods)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("consumer", ["photo", "timelapse", "canceled_timelapse"])
@pytest.mark.parametrize("binding", ["missing", "mismatch"])
async def test_closed_terminal_projection_rolls_back_outputs_and_action_together(
        tmp_path, monkeypatch, consumer, binding):
    world = await closed_consumer_world(tmp_path, monkeypatch,
        consumer=consumer, history="latest_complete")
    owned = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        before = tuple(owned.connection.iterdump())
        fault = _TerminalProjectionFault(owned.connection)
        runtime, methods = fresh_closed_runtime(replace(owned, connection=fault), world, binding)
        receipts = _TerminalReceiptSpy()
        runtime.capture = receipts
        advance = capture_handler(world.handler)

        with pytest.raises(ConsistencyError):
            await advance(world.action_id, runtime)

        assert fault.hits == 1 and not fault.armed
        assert fault.output_count_before_rollback == 1
        assert fault.rollback_count == 1 and fault.output_count_after_rollback == 0
        assert not fault.fault_transaction
        failed_receipt, = receipts.receipts
        assert failed_receipt.kind is DbOutcomeKind.ROLLED_BACK
        assert failed_receipt.error is fault.error
        assert not owned.connection.in_transaction
        assert tuple(owned.connection.iterdump()) == before
        assert owned.connection.execute(
            "SELECT status FROM actions WHERE id=?", (world.action_id,)).fetchone() == (2,)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
        _assert_preserved(owned, world, methods)

        await advance(world.action_id, runtime)
        assert len(receipts.receipts) == 2
        assert receipts.receipts[-1].kind is DbOutcomeKind.COMPLETED
        _assert_local_finish(owned, world, methods)
        completed = tuple(owned.connection.iterdump())
        await advance(world.action_id, runtime)
        assert len(receipts.receipts) == 2
        assert tuple(owned.connection.iterdump()) == completed
        _assert_local_finish(owned, world, methods)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("binding", ["matched", "missing", "mismatch"])
async def test_canceled_closed_with_required_stop_keeps_device_qualification(
        tmp_path, monkeypatch, binding):
    world = await closed_consumer_world(tmp_path, monkeypatch,
        consumer="timelapse", history="latest_complete")
    owned = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        # 原 G 已可靠存在，但活动尚无停止依据；取消只通过公开事务生效。
        assert owned.connection.execute(
            "SELECT activity_state,stop_supported FROM device_activities WHERE id=?",
            (world.activity_id,)).fetchone() == (1, 1)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_runs WHERE responsibility_key=?",
            (f"stop/{world.action_id}",)).fetchone() == (0,)
        _apply_public_cancel(owned, world.action_id, world.formed_at + 10)
        history = _rows(owned, "history_events")
        results_before = owned.connection.execute(
            "SELECT * FROM operation_runs WHERE id=?", (world.run_id,)).fetchone()
        runtime, methods = fresh_closed_runtime(owned, world, binding)
        methods[1].side_effect = None
        methods[1].return_value = DeviceCallResult.from_outcome(
            returned("stop", "stop_confirmed", world.activity_id))

        await capture_handler(world.handler)(world.action_id, runtime)

        assert saved_transaction_events(owned.connection, world.key) == world.close_events
        assert owned.connection.execute(
            "SELECT * FROM operation_runs WHERE id=?", (world.run_id,)).fetchone() == results_before
        assert _rows(owned, "device_files") == world.files
        assert _rows(owned, "history_events")[:len(history)] == history
        original_attempts = owned.connection.execute(
            "SELECT * FROM operation_attempts WHERE id<=? ORDER BY id", (world.attempts[-1][0],)).fetchall()
        assert original_attempts == world.attempts
        world.control_driver.control.assert_awaited_once()
        world.result_driver.list_results.assert_awaited_once()
        stop_run_id, stop_status, used, error_json = owned.connection.execute(
            "SELECT id,status,attempts_used,error_json FROM operation_runs WHERE responsibility_key=?",
            (f"stop/{world.action_id}",)).fetchone()
        if binding == "matched":
            methods[1].assert_awaited_once()
            request, = methods[1].call_args.args
            assert request.operation == "stop_timelapse"
            assert request.binding.device_id == "cam-1" and request.binding.driver_id == "camctl-adb"
            assert request.ticket.responsibility_key == f"stop/{world.action_id}"
            assert request.ticket.target_id == str(world.activity_id)
            assert (stop_status, used, error_json) == (3, 1, None)
            assert _capture_facts(owned, world.action_id)[2:] == world.capture_facts[2:]
            assert owned.connection.execute(
                "SELECT activity_state,occupancy_state FROM device_activities WHERE id=?",
                (world.activity_id,)).fetchone() == (3, 2)
            outputs = owned.connection.execute(
                "SELECT o.source_action_id,o.kind,o.device_file_id,o.original_name,o.media_type,f.size_bytes"
                " FROM outputs o JOIN device_files f ON f.id=o.device_file_id WHERE o.source_action_id=?",
                (world.action_id,)).fetchall()
            assert outputs == [world.expected_output]
        else:
            methods[1].assert_not_called()
            assert (stop_status, used) == (4, 0)
            expected_details = {"device_id": "cam-1", "expected_driver_id": "camctl-adb", "reason": binding}
            if binding == "mismatch":
                expected_details["actual_driver_id"] = "alternate-camera"
            assert json.loads(error_json) == {"code": "device_binding_unavailable",
                "stage": registered_error("device_binding_unavailable")["stage"], "details": expected_details}
            assert owned.connection.execute(
                "SELECT COUNT(*) FROM operation_attempts WHERE run_id=?", (stop_run_id,)).fetchone() == (0,)
            assert _capture_facts(owned, world.action_id)[:2] == world.capture_facts[:2]
            # 此处不把空 outputs 当作合法结果；绑定失败保留正式产物另有待实施分区。
        for method in (methods[0], *methods[2:]):
            method.assert_not_called()
        assert owned.connection.execute(
            "SELECT status,cancel_requested FROM actions WHERE id=?", (world.action_id,)).fetchone() == (6, 1)
    finally:
        owned.connection.close()


async def test_canceled_closed_waits_for_actual_stop_owner_without_second_call(
        tmp_path, monkeypatch):
    world = await closed_consumer_world(tmp_path, monkeypatch,
        consumer="timelapse", history="latest_complete")
    owned = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    allow_return = asyncio.Event()
    owner = None
    try:
        _apply_public_cancel(owned, world.action_id, world.formed_at + 10)
        results_before = owned.connection.execute(
            "SELECT * FROM operation_runs WHERE id=?", (world.run_id,)).fetchone()
        history = _rows(owned, "history_events")
        runtime, methods = fresh_closed_runtime(owned, world, "matched")
        entered = asyncio.Event()

        async def stop_owned(request):
            assert request.operation == "stop_timelapse"
            assert request.binding.device_id == "cam-1" and request.binding.driver_id == "camctl-adb"
            assert request.ticket.responsibility_key == f"stop/{world.action_id}"
            assert request.ticket.target_id == str(world.activity_id)
            entered.set()
            await allow_return.wait()
            return DeviceCallResult.from_outcome(returned("stop", "stop_confirmed", world.activity_id))

        methods[1].side_effect = stop_owned
        advance = capture_handler(world.handler)
        owner = asyncio.create_task(advance(world.action_id, runtime))
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert not owner.done()
        assert owned.connection.execute(
            "SELECT t.status,t.result_event_id,t.result_json,r.status,r.attempts_used"
            " FROM operation_runs r JOIN operation_attempts t ON t.run_id=r.id"
            " WHERE r.responsibility_key=?", (f"stop/{world.action_id}",)).fetchone() == (
                1, None, None, 2, 1)
        in_flight = tuple(owned.connection.iterdump())

        await advance(world.action_id, runtime)

        assert not owner.done()
        assert tuple(owned.connection.iterdump()) == in_flight
        methods[1].assert_awaited_once()
        assert owned.connection.execute(
            "SELECT status,cancel_requested FROM actions WHERE id=?", (world.action_id,)).fetchone() == (2, 1)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
        assert saved_transaction_events(owned.connection, world.key) == world.close_events
        assert _rows(owned, "device_files") == world.files

        allow_return.set()
        await owner

        assert owned.connection.execute(
            "SELECT status,cancel_requested FROM actions WHERE id=?", (world.action_id,)).fetchone() == (6, 1)
        assert owned.connection.execute(
            "SELECT status,attempts_used,retry_wait_required,error_json FROM operation_runs"
            " WHERE responsibility_key=?", (f"stop/{world.action_id}",)).fetchone() == (3, 1, 0, None)
        stopped_status, result_event_id = owned.connection.execute(
            "SELECT status,result_event_id FROM operation_attempts WHERE run_id=("
            " SELECT id FROM operation_runs WHERE responsibility_key=?)",
            (f"stop/{world.action_id}",)).fetchone()
        assert stopped_status == 2 and result_event_id is not None
        assert owned.connection.execute(
            "SELECT * FROM operation_runs WHERE id=?", (world.run_id,)).fetchone() == results_before
        assert saved_transaction_events(owned.connection, world.key) == world.close_events
        assert _rows(owned, "device_files") == world.files
        assert _rows(owned, "history_events")[:len(history)] == history
        assert owned.connection.execute(
            "SELECT * FROM operation_attempts WHERE id<=? ORDER BY id",
            (world.attempts[-1][0],)).fetchall() == world.attempts
        assert _capture_facts(owned, world.action_id)[2:] == world.capture_facts[2:]
        (output_id, *output), = owned.connection.execute(
            "SELECT o.id,o.source_action_id,o.kind,o.device_file_id,o.original_name,o.media_type,f.size_bytes"
            " FROM outputs o JOIN device_files f ON f.id=o.device_file_id WHERE o.source_action_id=?",
            (world.action_id,)).fetchall()
        assert output_id > 0 and tuple(output) == world.expected_output
        output_transaction, terminal_transaction = owned.connection.execute(
            "SELECT oe.transaction_id,ae.transaction_id FROM outputs o"
            " JOIN history_events oe ON oe.id=o.created_event_id"
            " JOIN actions a ON a.id=o.source_action_id"
            " JOIN history_events ae ON ae.id=a.last_event_id WHERE o.id=?", (output_id,)).fetchone()
        assert output_transaction == terminal_transaction
        complete = tuple(owned.connection.iterdump())

        await advance(world.action_id, runtime)

        assert tuple(owned.connection.iterdump()) == complete
        methods[1].assert_awaited_once()
        for method in (methods[0], *methods[2:]):
            method.assert_not_called()
        world.control_driver.control.assert_awaited_once()
        world.result_driver.list_results.assert_awaited_once()
    finally:
        allow_return.set()
        try:
            if owner is not None:
                await asyncio.wait_for(owner, timeout=5)
        finally:
            owned.connection.close()

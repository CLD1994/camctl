"""仍需停止的取消延时摄影在绑定失效时，保留原完整文件。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.bootstrap.capture_assembly import execution_wait_config, session_capture_assembly
from camctl.bootstrap.flows import capture_flow
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import saved_transaction_events

from ..capture.test_cancel_timelapse_exhaustion_files import _apply_public_cancel
from ..capture.test_closed_result_local_consumption import closed_consumer_world
from .test_closed_result_binding_recovery import _unavailable_device_ports
from .test_media_assembly import _config


pytestmark = pytest.mark.asyncio


def _rows(owned, table):
    return owned.connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()


def _target_terminal_transaction(owned, action_id):
    """从实际终态事件取事务，不把动作最后的其他 owned 事件当终态。"""
    matches = []
    for transaction_id, body in owned.connection.execute(
            "SELECT transaction_id,body_json FROM history_events ORDER BY id"):
        for row in json.loads(body)["rows"]:
            if (row["table"] == "actions" and row["id"] == action_id
                    and row["after"]["exists"] and row["after"]["values"].get("status") == 6):
                matches.append(transaction_id)
    (transaction_id,) = matches
    return transaction_id


def _stop_terminal_transaction(owned, run_id):
    """沿 OPERATION_CONFIGURED.FINISH 的原行定位 STOP 失败事务。"""
    matches = []
    for transaction_id, raw_body in owned.connection.execute(
            "SELECT transaction_id,body_json FROM history_events WHERE event_type=10 ORDER BY id"):
        body = json.loads(raw_body)
        if body["reason"] != 3:
            continue
        for row in body["rows"]:
            if (row["table"] == "operation_runs" and row["id"] == run_id
                    and row["before"]["exists"] and row["after"]["exists"]
                    and row["after"]["values"].get("status") == 4):
                matches.append(transaction_id)
    (transaction_id,) = matches
    return transaction_id


@pytest.mark.parametrize("binding", ["missing", "mismatch"])
@pytest.mark.parametrize("history", ["latest_complete", "prior_complete_latest_failed"])
async def test_canceled_timelapse_binding_failure_registers_retained_files_atomically(
        tmp_path, monkeypatch, binding, history):
    world = await closed_consumer_world(
        tmp_path, monkeypatch, consumer="timelapse", history=history)
    preparing = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        assert preparing.metadata == world.metadata
        assert saved_transaction_events(preparing.connection, world.key) == world.close_events
        assert preparing.connection.execute(
            "SELECT status,cancel_requested FROM actions WHERE id=?", (world.action_id,)).fetchone() == (2, 0)
        activity_state, supported = preparing.connection.execute(
            "SELECT activity_state,stop_supported FROM device_activities WHERE id=?",
            (world.activity_id,)).fetchone()
        assert activity_state != 3 and supported == 1
        assert preparing.connection.execute(
            "SELECT id FROM operation_runs WHERE responsibility_key=?",
            (f"stop/{world.action_id}",)).fetchone() is None
        _apply_public_cancel(preparing, world.action_id, world.formed_at + 10)
        assert preparing.connection.execute(
            "SELECT status,cancel_requested FROM actions WHERE id=?", (world.action_id,)).fetchone() == (2, 1)
        assert preparing.connection.execute(
            "SELECT id FROM operation_runs WHERE responsibility_key=?",
            (f"stop/{world.action_id}",)).fetchone() is None
        assert _rows(preparing, "operation_attempts") == world.attempts
        assert _rows(preparing, "operation_runs") == world.runs
        assert _rows(preparing, "device_files") == world.files
        assert preparing.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
        prefix = _rows(preparing, "history_events")
        transactions = _rows(preparing, "history_transactions")
        original_activity = preparing.connection.execute(
            "SELECT activity_state,occupancy_state,result_set_state,result_check_json,capture_json,"
            " completion_basis,last_error_json,wait_completed_event_id"
            " FROM device_activities WHERE id=?", (world.activity_id,)).fetchone()
        assert original_activity == world.capture_facts
    finally:
        preparing.connection.close()

    config = _config(tmp_path)
    assert Path(config.paths.state_db) == world.path
    devices = {} if binding == "missing" else {"cam-1": {
        **config.devices["cam-1"], "driver": "alternate-camera"}}
    registry, methods = _unavailable_device_ports()
    now = world.formed_at + 5_000_000
    factory = session_capture_assembly(
        devices=devices, drivers=registry, staging=Path(config.paths.staging),
        wait_config=execution_wait_config, wall_us=lambda: now,
        monotonic_ns=lambda: 99_000_000_000)
    opened, runtimes = [], []

    def open_connection():
        owned = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
        assert owned.metadata == world.metadata
        assert owned.connection is not preparing.connection
        assert all(owned.connection is not earlier.connection for earlier in opened)
        opened.append(owned)
        return owned

    def observed_factory(owned, device_id):
        assert owned is opened[-1] and device_id == "cam-1"
        runtime = factory(owned, device_id)
        assert runtime is not None
        assert runtime.pending_start_results == runtime.pending_capture_completions == runtime.pending_result_closes == {}
        assert runtime.pending_file_observations == runtime.pending_media_results == {}
        assert runtime.pending_read_results == runtime.pending_read_business == runtime.pending_read_ends == {}
        assert runtime.continuing_read_tickets == {}
        assert runtime.listing_cache in (None, {})
        assert runtime.timelapse_deadlines == runtime.retry_gate.anchors == {}
        runtimes.append(runtime)
        return runtime

    context = SimpleNamespace(open_connection=open_connection,
        clock=SimpleNamespace(utc_micros=lambda: now))
    flow = capture_flow(observed_factory)
    await flow(context)
    await flow.settle()
    assert len(opened) == 2 and len(runtimes) == 1
    saved = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        assert saved.connection.execute(
            "SELECT status,cancel_requested FROM actions WHERE id=?", (world.action_id,)).fetchone() == (6, 1)
        stop = saved.connection.execute(
            "SELECT id,action_id,activity_id,kind,status,attempts_used,error_json"
            " FROM operation_runs WHERE responsibility_key=?", (f"stop/{world.action_id}",)).fetchone()
        assert stop is not None
        stop_id, stop_action, stop_activity, kind, status, used, error_json = stop
        assert (stop_action, stop_activity, kind, status, used) == (
            world.action_id, world.activity_id, 2, 4, 0)
        expected_details = {"device_id": "cam-1", "expected_driver_id": "camctl-adb", "reason": binding}
        if binding == "mismatch":
            expected_details["actual_driver_id"] = "alternate-camera"
        assert json.loads(error_json) == {"code": "device_binding_unavailable", "stage": "execution",
            "details": expected_details}
        assert saved.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts WHERE run_id=?", (stop_id,)).fetchone() == (0,)
        assert _rows(saved, "operation_attempts") == world.attempts
        assert _rows(saved, "device_files") == world.files
        for row in world.runs:
            assert saved.connection.execute("SELECT * FROM operation_runs WHERE id=?", (row[0],)).fetchone() == row
        assert len(_rows(saved, "operation_runs")) == len(world.runs) + 1
        assert saved_transaction_events(saved.connection, world.key) == world.close_events
        assert _rows(saved, "history_events")[:len(prefix)] == prefix
        assert _rows(saved, "history_transactions")[:len(transactions)] == transactions
        assert saved.connection.execute(
            "SELECT activity_state,occupancy_state,result_set_state,result_check_json,capture_json,"
            " completion_basis,last_error_json,wait_completed_event_id"
            " FROM device_activities WHERE id=?", (world.activity_id,)).fetchone() == original_activity
        assert world.control_driver.control.await_count == 1
        assert world.result_driver.list_results.await_count == world.used
        assert world.stop_driver is None
        for method in methods:
            method.assert_not_called()
        terminal_transaction = _target_terminal_transaction(saved, world.action_id)
        assert _stop_terminal_transaction(saved, stop_id) == terminal_transaction
        outputs = saved.connection.execute(
            "SELECT o.source_action_id,o.kind,o.device_file_id,o.original_name,o.media_type,f.size_bytes"
            " FROM outputs o JOIN device_files f ON f.id=o.device_file_id WHERE o.source_action_id=?",
            (world.action_id,)).fetchall()
        # 固定预期来自公开 fixture 原文件，不能靠第二轮终态推进补登记。
        assert outputs == [world.expected_output], "必要 STOP 绑定失败不消除已可靠取得的完整文件"
        assert saved.connection.execute(
            "SELECT e.transaction_id FROM outputs o JOIN history_events e ON e.id=o.created_event_id"
            " WHERE o.source_action_id=?", (world.action_id,)).fetchall() == [(terminal_transaction,)]
        before_repeat = tuple(saved.connection.iterdump())
    finally:
        saved.connection.close()

    await flow(context)
    await flow.settle()
    assert len(opened) == 3 and len(runtimes) == 1
    repeated = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        assert tuple(repeated.connection.iterdump()) == before_repeat
        for method in methods:
            method.assert_not_called()
        assert world.control_driver.control.await_count == 1
        assert world.result_driver.list_results.await_count == world.used
    finally:
        repeated.connection.close()

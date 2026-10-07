"""统一设备工作调度的组件集成测试。

真实 SQLite 投影装配：到时拍摄按设备分组并优先、占用中的拍摄活动
阻塞读取、持机会读取归属设备并让路、已取消动作的读取工作不进入普
通授予（取消联动由取消收场流程推进）；声明拍摄与读取并行的设备上
让路与占用阻塞都不再发生。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from camctl.outputs.dispatch import plan_device_work
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..persistence.test_runtime import _create_valid_database

_NOW = 1_750_000_000_000_000


@pytest.fixture
def pipeline(tmp_path: Path):
    target = tmp_path / "state.db"
    _create_valid_database(target)
    owned = open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "INSERT INTO history_transactions (id, operation_key, first_event_id, last_event_id)"
        " VALUES (1, ?, 1, 1)", ("f" * 32,))
    connection.execute(
        "INSERT INTO history_events (id, transaction_id, event_type, event_version,"
        " occurred_at, clock_status, change_seq, body_json)"
        " VALUES (1, 1, 2, 1, ?, 2, NULL, ?)",
        (_NOW, json.dumps({"reason": 1, "evidence": {}, "rows": []})))
    connection.execute(
        "INSERT INTO plans (id, request_id, name, created_at, status,"
        " created_event_id, last_event_id, change_count)"
        " VALUES (1, 4242, 'seed', ?, 1, 1, 1, 1)", (_NOW,))
    # 11：cam-1 到时拍摄（占用中活动）；12：cam-2 未到时拍摄；
    # 21：cam-1 已取消的取回；22：cam-2 执行中的取回。
    for action_id, kind, device, scheduled, status, canceled, index in (
            (11, 2, "cam-1", _NOW, 2, 0, 0),
            (12, 2, "cam-2", _NOW + 10_000_000, 2, 0, 1),
            (21, 4, None, _NOW, 2, 1, 2),
            (22, 4, None, _NOW, 2, 0, 3)):
        capture = kind in (1, 2, 3)
        connection.execute(
            "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
            " scheduled_at, group_name, input_fields_json, effective_params_json,"
            " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
            " cancel_requested, error_code, error_details_json,"
            " first_window_observed_at, expiration_reason, source_resolution_state,"
            " resolved_source_plan_id, target_selection_state, created_event_id,"
            " last_event_id, change_count)"
            " VALUES (?, 1, ?, ?, ?, ?, ?, NULL, '{}', ?, ?, ?, '{}', ?, 1, ?,"
            " NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, 1, 1)",
            (action_id, index, f"act-{action_id}", kind, device, scheduled,
             "{}" if capture else None,
             "camctl-adb" if capture else None,
             1000 if capture else None, status, canceled))
    # 拍摄动作 11 的占用中活动；12 未到时也有活动（占用中）。
    for action_id in (11, 12):
        connection.execute(
            "INSERT INTO device_activities (id, action_id, task_key,"
            " task_locator_json, state_query_supported, stop_supported,"
            " safe_repeat_stop, start_return_meaning, completion_mode,"
            " ownership_mode, output_scope_json, baseline_state,"
            " baseline_first_event_id, baseline_last_event_id, dispatch_state,"
            " activity_state, occupancy_state, sent_at, started_at,"
            " result_wait_margin_ms, extra_wait_ms_used, expected_check_at,"
            " wait_completed_event_id, capture_json, control_elapsed_ns,"
            " completion_basis, completion_evidence_json, result_set_state,"
            " last_error_json)"
            " VALUES (?, ?, ?, NULL, 1, 1, 1, 1, 1, 1, '{}', 1, NULL, NULL,"
            " 1, 1, 1, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,"
            " NULL, 1, NULL)",
            (action_id, action_id, f"{action_id:032x}"))
    # 设备文件与产物（两份拷贝的源）。
    for file_id in (501, 502):
        connection.execute(
            "INSERT INTO device_files (id, observer_action_id, source_action_id,"
            " identity_key, locator_json, ownership_evidence_json, role,"
            " presence_state, completion_state, completion_evidence_json,"
            " checksum_support, size_bytes, created_event_id, last_event_id,"
            " change_count)"
            f" VALUES ({file_id}, 11, 11, 'file-{file_id}', '{{}}', '{{}}', 2,"
            " 2, 3, '{}', 2, 10, 1, 1, 1)")
    for output_id, file_id in ((701, 501), (702, 502)):
        connection.execute(
            "INSERT INTO outputs (id, source_action_id, kind, device_file_id,"
            " availability, cleanup_status, media_json, created_event_id,"
            " last_event_id, change_count)"
            f" VALUES ({output_id}, 11, 1, {file_id}, 1, 1, '{{}}', 1, 1, 1)")
    # 拷贝 40：cam-1 上持机会的在途读取（属 22）；
    # 拷贝 41：未持机会的合格读取（属 22，源设备 cam-1）；
    # 拷贝 42：已取消取回 21 的读取（不进入普通授予）。
    for copy_id, delivery_id, action_id, file_id, slot in (
            (40, 301, 22, 501, "cam-1"),
            (41, 301, 22, 502, None),
            (42, 302, 21, 502, None)):
        if delivery_id == 301 and copy_id == 41:
            continue  # 同一交付只建一次，拷贝 41 与 40 同交付
    connection.execute(
        "INSERT INTO deliveries (id, action_id, output_id, file_name,"
        " display_name, status, publication_intent_event_id, published_event_id,"
        " error_json, withdrawal_state, withdrawal_error_json,"
        " created_event_id, last_event_id, change_count)"
        " VALUES (301, 22, 701, '301.mp4', 'd', 2, 1, 1, NULL, 1, NULL, 1, 1, 1)")
    for delivery_id, action_id in ((302, 21), (303, 22)):
        connection.execute(
            "INSERT INTO deliveries (id, action_id, output_id, file_name,"
            " display_name, status, publication_intent_event_id,"
            " published_event_id, error_json, withdrawal_state,"
            " withdrawal_error_json, created_event_id, last_event_id,"
            " change_count)"
            f" VALUES ({delivery_id}, {action_id}, 702, '{delivery_id}.mp4',"
            " 'd', 2, 1, 1, NULL, 1, NULL, 1, 1, 1)")
    for copy_id, delivery_id, file_id, slot in (
            (40, 301, 501, "cam-1"), (41, 303, 502, None),
            (42, 302, 502, None)):
        connection.execute(
            "INSERT INTO file_copies (id, delivery_id, processing_id,"
            " source_device_file_id, source_intermediate_file_id, target_file_id,"
            " source_size, source_sha256, round, recopies_used, max_recopies_used,"
            " committed_bytes, reset_state, slot_device_id, verification_state,"
            " target_sha256, verification_error_json)"
            " VALUES (?, ?, NULL, ?, NULL, ?, 10, NULL, 1, 0, 1, 0, 1, ?,"
            " 1, NULL, NULL)",
            (copy_id, delivery_id, file_id, 70 + copy_id, slot))
        connection.execute(
            "INSERT INTO intermediate_files (id, owner_action_id,"
            " owner_delivery_id, purpose, relative_path, size_bytes, sha256,"
            " retention_state, cleanup_state, last_error_json, created_event_id,"
            " last_event_id, change_count)"
            " VALUES (?, NULL, ?, 1, ?, 10, NULL, 1, 1, NULL, 1, 1, 1)",
            (70 + copy_id, delivery_id, f"deliveries/{70 + copy_id}.mp4"))
    connection.commit()
    yield owned
    owned.connection.close()


def test_plan_groups_devices_and_applies_yielding(pipeline):
    owned = pipeline
    works = plan_device_work(owned.connection, _NOW)
    by_device = {work.device_id: work for work in works}
    # cam-1：到时拍摄 11 优先，持机会读取 40 让路。
    cam1 = by_device["cam-1"]
    assert cam1.dispatch_captures == ()
    assert cam1.yield_reads == (40,)
    # cam-2：拍摄 12 未到时但活动占用中，读取等待。
    cam2 = by_device["cam-2"]
    assert cam2.dispatch_captures == () and cam2.grant_reads == ()
    assert cam2.yield_reads == ()
    # 已取消取回 21 的拷贝 42 不进入任何授予。
    all_reads = cam1.grant_reads + cam2.grant_reads
    assert 42 not in all_reads


def test_idle_device_after_capture_terminal_grants_reads(pipeline):
    owned = pipeline
    # 拍摄终态并释放占用、让路读取收场后：cam-1 空闲授予合格读取 41。
    owned.connection.execute(
        "UPDATE actions SET status = 3 WHERE id = 11")
    owned.connection.execute(
        "UPDATE device_activities SET occupancy_state = 2, activity_state = 3,"
        " dispatch_state = 3 WHERE action_id = 11")
    owned.connection.execute(
        "UPDATE file_copies SET slot_device_id = NULL WHERE id = 40")
    # 让路的读取按取消收场结束：交付取消后不再进入普通授予。
    owned.connection.execute(
        "UPDATE deliveries SET status = 7 WHERE id = 301")
    owned.connection.commit()
    works = plan_device_work(owned.connection, _NOW)
    cam1 = {work.device_id: work for work in works}["cam-1"]
    assert cam1.grant_reads == (41,)
    assert cam1.dispatch_captures == () and cam1.yield_reads == ()


def test_parallel_declaration_keeps_reads_alongside_captures(pipeline):
    owned = pipeline
    # cam-1 声明并行：到时拍摄与持机会读取同轮共存（不让路），cam-2
    # 未声明仍按占用阻塞。
    works = plan_device_work(
        owned.connection, _NOW, capture_read_parallel={"cam-1"})
    by_device = {work.device_id: work for work in works}
    cam1 = by_device["cam-1"]
    assert cam1.capture_read_parallel is True
    assert cam1.dispatch_captures == (11,)
    assert cam1.yield_reads == () and cam1.resume_reads == (40,)
    assert cam1.grant_reads == ()
    cam2 = by_device["cam-2"]
    assert cam2.capture_read_parallel is False
    assert cam2.grant_reads == () and cam2.yield_reads == ()

    # 拍摄未到时仍占用设备：并行声明下合格读取照常授予；同一投影
    # 未声明并行时仍被占用阻塞（对照）。
    owned.connection.execute(
        "UPDATE actions SET scheduled_at = ? WHERE id = 11",
        (_NOW + 10_000_000,))
    owned.connection.execute(
        "UPDATE file_copies SET slot_device_id = NULL WHERE id = 40")
    owned.connection.execute(
        "UPDATE deliveries SET status = 7 WHERE id = 301")
    owned.connection.commit()
    works = plan_device_work(
        owned.connection, _NOW, capture_read_parallel={"cam-1"})
    by_device = {work.device_id: work for work in works}
    assert by_device["cam-1"].grant_reads == (41,)
    assert by_device["cam-1"].dispatch_captures == ()
    assert by_device["cam-1"].yield_reads == ()
    serial = plan_device_work(owned.connection, _NOW)
    by_serial = {work.device_id: work for work in serial}
    assert by_serial["cam-1"].grant_reads == ()

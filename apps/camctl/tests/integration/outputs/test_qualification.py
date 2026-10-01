"""X3 读取和删除资格共同事务的组件集成测试。

真实 SQLite 与 P3 事务内核：候选按计划时间排序、同时间取回优先，
协程唤醒顺序不改变授予结果；已有读取保护、清理限制及删除处理者
分别拒绝；跨设备候选互不阻挡；授予时依赖、交付、拷贝、目标文件
及读取流程在同一事务建档，缺一即整笔拒绝；内部处理共用读取机会
不创建交付。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import register_acceptance_guards
from camctl.persistence.repositories.outputs import (
    OutputsRepository,
    register_outputs_guards,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.outputs.qualification import (
    FileCandidate,
    OperationConfig,
    QualificationOutcome,
)

from ..persistence.test_runtime import _create_valid_database

register_acceptance_guards()
register_outputs_guards()

_NOW = 1_750_000_000_000_000
_LATER = _NOW + 60_000_000


@dataclass(frozen=True)
class _Seedling:
    """一次授予测试的数据库事实集合（动作、文件、产物、选择与项）。"""

    action_id: int
    item_id: int | None
    output_id: int
    file_id: int
    processing_id: int | None = None


def _seed_environment(tmp_path: Path):
    target = tmp_path / "state.db"
    _create_valid_database(target)
    owned = open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "INSERT INTO history_transactions (id, operation_key, first_event_id, last_event_id)"
        " VALUES (1, ?, 1, 1)",
        ("f" * 32,),
    )
    connection.execute(
        "INSERT INTO history_events (id, transaction_id, event_type, event_version,"
        " occurred_at, clock_status, change_seq, body_json)"
        " VALUES (1, 1, 2, 1, ?, 2, NULL, ?)",
        (_NOW, json.dumps({"reason": 1, "evidence": {}, "rows": []})),
    )
    connection.commit()
    return target, owned


def _seed_plan(connection: sqlite3.Connection, plan_id: int) -> None:
    connection.execute(
        "INSERT INTO plans (id, request_id, name, created_at, status,"
        " created_event_id, last_event_id, change_count)"
        " VALUES (?, ?, ?, ?, 1, 1, 1, 1)",
        (plan_id, 1000 + plan_id, f"plan-{plan_id}", _NOW),
    )


def _seed_action(
    connection: sqlite3.Connection,
    action_id: int,
    plan_id: int,
    *,
    action_type: int,
    device_id: str | None = "cam-1",
    scheduled_at: int | None = _NOW,
    status: int = 2,
) -> None:
    started = 0 if status == 1 else 1
    if action_type in (1, 2, 3):
        connection.execute(
            "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
            " scheduled_at, group_name, input_fields_json, effective_params_json,"
            " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
            " cancel_requested, error_code, error_details_json, first_window_observed_at,"
            " expiration_reason, source_resolution_state, resolved_source_plan_id,"
            " target_selection_state, created_event_id, last_event_id, change_count)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, NULL, '{}', '{}', 'camctl-adb', 1000,"
            " '{}', ?, ?, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, 1, 1)",
            (action_id, plan_id, action_id, f"action-{action_id}", action_type,
             device_id, scheduled_at, status, started),
        )
        return
    spec = '{"selection_mode": 1}' if action_type == 4 else "{}"
    connection.execute(
        "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
        " scheduled_at, group_name, input_fields_json, effective_params_json,"
        " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
        " cancel_requested, error_code, error_details_json, first_window_observed_at,"
        " expiration_reason, source_resolution_state, resolved_source_plan_id,"
        " target_selection_state, created_event_id, last_event_id, change_count)"
        " VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, '{}', NULL, NULL, NULL, ?,"
        " ?, ?, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, 1, 1)",
        (action_id, plan_id, action_id, f"action-{action_id}", action_type,
         scheduled_at, spec, status, started),
    )


def _seed_device_file(
    connection: sqlite3.Connection,
    file_id: int,
    source_action_id: int,
    *,
    size: int = 4096,
    completion_state: int = 3,
    presence_state: int = 2,
) -> None:
    completion_evidence = "{}" if completion_state == 3 else None
    connection.execute(
        "INSERT INTO device_files (id, observer_action_id, source_action_id,"
        " identity_key, locator_json, ownership_evidence_json, original_name,"
        " media_type, role, original_device_file_id, pairing_evidence_json,"
        " presence_state, completion_state, completion_evidence_json, size_bytes,"
        " checksum_support, sha256, last_error_json, created_event_id,"
        " last_event_id, change_count)"
        " VALUES (?, ?, ?, ?, '{}', '{}', 'video.mp4', 'video/mp4', 1, NULL, NULL,"
        f" {presence_state}, {completion_state}, ?, ?, 3, NULL, NULL, 1, 1, 1)",
        (file_id, source_action_id, source_action_id, f"file-{file_id:04d}",
         completion_evidence, size if completion_state == 3 else None),
    )


def _seed_output(
    connection: sqlite3.Connection,
    output_id: int,
    source_action_id: int,
    file_id: int,
    *,
    availability: int = 1,
    size: int = 4096,
) -> None:
    cleanup = {1: 1, 2: 2, 3: 4, 4: 1, 5: 1}[availability]
    error_json = None if availability in (1, 2, 3) else {"reason": "seed"}
    connection.execute(
        "INSERT INTO outputs (id, source_action_id, kind, device_file_id,"
        " intermediate_file_id, original_name, media_type, availability,"
        " cleanup_status, cleanup_error_json, media_json, error_json,"
        " created_event_id, last_event_id, change_count)"
        " VALUES (?, ?, 1, ?, NULL, 'video.mp4', 'video/mp4', ?, ?, NULL, '{}', ?,"
        " 1, 1, 1)",
        (output_id, source_action_id, file_id, availability, cleanup,
         json.dumps(error_json) if error_json is not None else None),
    )


def _seed_selection_and_item(
    connection: sqlite3.Connection,
    *,
    dependency_id: int,
    selection_id: int,
    item_id: int,
    owner_action_id: int,
    source_action_id: int,
    output_id: int,
) -> None:
    """种下已固定选择与 SELECTED 项（X2 结果状态）。"""
    connection.execute(
        "INSERT INTO action_dependencies (id, action_id, depends_on_action_id)"
        " VALUES (?, ?, ?)",
        (dependency_id, owner_action_id, source_action_id),
    )
    connection.execute(
        "INSERT INTO obtain_source_selections (id, dependency_id, status,"
        " error_code, error_details_json) VALUES (?, ?, 2, NULL, NULL)",
        (selection_id, dependency_id),
    )
    connection.execute(
        "INSERT INTO obtain_items (id, selection_id, requested_output_id, output_id,"
        " basis, original_output_id, preview_output_id, preview_size, repaired_size,"
        " status, source_dependency, delivery_id, error_code, error_details_json)"
        " VALUES (?, ?, NULL, ?, 1, NULL, NULL, NULL, NULL, 2, 0, NULL, NULL, NULL)",
        (item_id, selection_id, output_id),
    )


def _seed_processing(
    connection: sqlite3.Connection,
    processing_id: int,
    action_id: int,
    file_id: int,
) -> None:
    connection.execute(
        "INSERT INTO recording_processing (id, action_id, source_device_file_id,"
        " check_state, check_decision, check_basis_json, media_json, repair_state,"
        " repair_basis_json, repair_output_file_id, repair_error_json, discard_state,"
        " discard_error_json)"
        " VALUES (?, ?, ?, 1, 1, NULL, '{}', 1, NULL, NULL, NULL, 1, NULL)",
        (processing_id, action_id, file_id),
    )


def _seed_cleanup_item(
    connection: sqlite3.Connection,
    item_id: int,
    action_id: int,
    output_id: int,
    *,
    status: int,
    restriction_state: int,
) -> None:
    connection.execute(
        "INSERT INTO cleanup_items (id, action_id, requested_output_id, output_id,"
        " status, restriction_state, outcome, final_event_id, error_code,"
        " error_details_json)"
        " VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)",
        (item_id, action_id, output_id, output_id, status, restriction_state),
    )


def _seed_active_copy(
    connection: sqlite3.Connection,
    *,
    copy_id: int,
    device_file_id: int,
    device_id: str,
    target_file_id: int,
    delivery_id: int | None = None,
    action_id: int,
    owner_action_id: int,
    output_id: int,
) -> None:
    """种下占用设备槽的既有活动拷贝（含 PENDING 读取流程）。"""
    connection.execute(
        "INSERT INTO intermediate_files (id, owner_action_id, owner_delivery_id,"
        " purpose, relative_path, retention_state, cleanup_state, size_bytes, sha256,"
        " last_error_json, created_event_id, last_event_id, change_count)"
        " VALUES (?, ?, NULL, 2, ?, 1, 1, NULL, NULL, NULL, 1, 1, 1)",
        (target_file_id, action_id, f"recording-inputs/{target_file_id}.part"),
    )
    if delivery_id is not None:
        connection.execute(
            "INSERT INTO file_copies (id, delivery_id, processing_id,"
            " source_device_file_id, source_intermediate_file_id, target_file_id,"
            " round, recopies_used, max_recopies_used, source_size, source_sha256,"
            " committed_bytes, reset_state, slot_device_id, verification_state,"
            " target_sha256, verification_error_json)"
            " VALUES (?, ?, NULL, ?, NULL, ?, 1, 0, 0, 4096, NULL, 0, 1, ?, 1,"
            " NULL, NULL)",
            (copy_id, delivery_id, device_file_id, target_file_id, device_id),
        )
    else:
        connection.execute(
            "INSERT INTO file_copies (id, delivery_id, processing_id,"
            " source_device_file_id, source_intermediate_file_id, target_file_id,"
            " round, recopies_used, max_recopies_used, source_size, source_sha256,"
            " committed_bytes, reset_state, slot_device_id, verification_state,"
            " target_sha256, verification_error_json)"
            " VALUES (?, NULL, NULL, ?, NULL, ?, 1, 0, 0, 4096, NULL, 0, 1, ?, 1,"
            " NULL, NULL)",
            (copy_id, device_file_id, target_file_id, device_id),
        )
    connection.execute(
        "INSERT INTO operation_runs (id, action_id, delivery_id, kind, query_purpose,"
        " responsibility_key, activity_id, copy_id, cleanup_item_id, session_key,"
        " status, attempts_used, max_attempts_used, timeout_s_json,"
        " retry_interval_s_json, retry_wait_required, error_json)"
        " VALUES (?, ?, NULL, 3, NULL, ?, NULL, ?, NULL, NULL, 1, 0, NULL, NULL,"
        " NULL, 0, NULL)",
        (1000 + copy_id, action_id, f"read/{copy_id}", copy_id),
    )


_CONFIG = OperationConfig(
    max_attempts=3,
    timeout_s=Decimal("10"),
    retry_interval_s=Decimal("3"),
)


def _candidate(
    seedling: _Seedling,
    *,
    suffix: str | None = None,
) -> FileCandidate:
    tag = suffix if suffix is not None else str(seedling.item_id or seedling.processing_id)
    return FileCandidate(
        action_id=seedling.action_id,
        item_id=seedling.item_id,
        processing_id=seedling.processing_id,
        output_id=seedling.output_id,
        source_device_file_id=seedling.file_id,
        target_relative_path=f"deliveries/{seedling.output_id}-{tag}.part"
        if seedling.processing_id is None
        else f"recording-inputs/{seedling.output_id}-{tag}.part",
        delivery_file_name=f"{seedling.output_id}-{tag}.mp4",
        delivery_display_name=f"产物 {seedling.output_id}",
        config=_CONFIG,
        occurred_at=_NOW,
    )


def _grant(owned, repository: OutputsRepository, candidate: FileCandidate):
    return repository.grant_file(candidate, new_operation_key(), owned)


def _granted(result) -> bool:
    return (
        result.kind is DbOutcomeKind.COMPLETED
        and result.value.outcome is QualificationOutcome.GRANTED
    )


def _rejected_reason(result) -> str:
    assert result.kind is DbOutcomeKind.COMPLETED
    return result.value.reason or ""


def _row(owned, sql: str, *params):
    return owned.connection.execute(sql, params).fetchone()


def test_qualification_uses_business_order(tmp_path: Path) -> None:
    """计划时间排序先于协程唤醒顺序；同时间取回优先。"""
    target, owned = _seed_environment(tmp_path)
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    _seed_plan(connection, 1)
    _seed_action(connection, 11, 1, action_type=2)  # 拍摄 cam-1 @NOW
    _seed_action(connection, 12, 1, action_type=2, device_id="cam-1",
                 scheduled_at=_NOW)  # 同设备第二个源
    _seed_action(connection, 31, 1, action_type=4, scheduled_at=_NOW)
    _seed_action(connection, 32, 1, action_type=4, scheduled_at=_LATER)
    _seed_device_file(connection, 501, 11)
    _seed_device_file(connection, 502, 12)
    _seed_output(connection, 701, 11, 501)
    _seed_output(connection, 702, 12, 502)
    _seed_selection_and_item(
        connection, dependency_id=1, selection_id=1, item_id=101,
        owner_action_id=31, source_action_id=11, output_id=701,
    )
    _seed_selection_and_item(
        connection, dependency_id=2, selection_id=2, item_id=102,
        owner_action_id=32, source_action_id=12, output_id=702,
    )
    connection.commit()

    repository = OutputsRepository()
    early = _Seedling(action_id=31, item_id=101, output_id=701, file_id=501)
    late = _Seedling(action_id=32, item_id=102, output_id=702, file_id=502)

    # 唤醒顺序颠倒：先申请较晚候选，按计划时间被拒。
    late_first = _grant(owned, repository, _candidate(late, suffix="b"))
    assert not _granted(late_first)
    early_first = _grant(owned, repository, _candidate(early, suffix="a"))
    assert _granted(early_first)

    # 授予后设备槽被占用，较晚候选仍不可开始。
    late_retry = _grant(owned, repository, _candidate(late, suffix="b"))
    assert not _granted(late_retry)
    owned.close()


def test_same_time_obtain_wins_over_processing(tmp_path: Path) -> None:
    """同一计划时间下取回候选优先于录像内部处理候选。"""
    target, owned = _seed_environment(tmp_path)
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    _seed_plan(connection, 1)
    _seed_plan(connection, 2)
    _seed_action(connection, 11, 1, action_type=2)  # cam-1 @NOW
    _seed_action(connection, 41, 1, action_type=4, scheduled_at=_NOW)
    _seed_action(connection, 42, 2, action_type=2, device_id="cam-1",
                 scheduled_at=_NOW)
    _seed_device_file(connection, 501, 11)
    _seed_output(connection, 701, 11, 501)
    _seed_selection_and_item(
        connection, dependency_id=1, selection_id=1, item_id=101,
        owner_action_id=41, source_action_id=11, output_id=701,
    )
    _seed_processing(connection, 5, 42, 501)
    connection.commit()

    repository = OutputsRepository()
    obtain = _Seedling(action_id=41, item_id=101, output_id=701, file_id=501)
    processing = _Seedling(
        action_id=42, item_id=None, output_id=701, file_id=501, processing_id=5
    )

    processing_first = _grant(owned, repository, _candidate(processing))
    assert not _granted(processing_first)
    obtain_first = _grant(owned, repository, _candidate(obtain))
    assert _granted(obtain_first)
    owned.close()


def test_existing_read_protection_preserves_original_copy(tmp_path: Path) -> None:
    """源文件已有活动拷贝时保留原拷贝，新申请不抢占。"""
    target, owned = _seed_environment(tmp_path)
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    _seed_plan(connection, 1)
    _seed_action(connection, 11, 1, action_type=2)
    _seed_action(connection, 31, 1, action_type=4, scheduled_at=_NOW)
    _seed_action(connection, 32, 1, action_type=4, scheduled_at=_LATER)
    _seed_device_file(connection, 501, 11)
    _seed_output(connection, 701, 11, 501)
    _seed_selection_and_item(
        connection, dependency_id=1, selection_id=1, item_id=101,
        owner_action_id=31, source_action_id=11, output_id=701,
    )
    _seed_active_copy(
        connection, copy_id=900, device_file_id=501, device_id="cam-1",
        target_file_id=800, action_id=11, owner_action_id=31, output_id=701,
    )
    connection.commit()

    repository = OutputsRepository()
    candidate = _Seedling(action_id=32, item_id=101, output_id=701, file_id=501)
    result = _grant(owned, repository, _candidate(candidate, suffix="c"))
    assert not _granted(result)
    # 原拷贝行保持不变。
    assert _row(owned, "SELECT committed_bytes FROM file_copies WHERE id = 900")[0] == 0
    assert _row(owned, "SELECT count(*) FROM file_copies")[0] == 1
    owned.close()


def test_cleanup_restriction_rejects_and_records(tmp_path: Path) -> None:
    """不可撤销清理限制：逐项保存最终失败，不建立任何读取档案。"""
    target, owned = _seed_environment(tmp_path)
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    _seed_plan(connection, 1)
    _seed_action(connection, 11, 1, action_type=2)
    _seed_action(connection, 31, 1, action_type=4, scheduled_at=_NOW)
    _seed_action(connection, 91, 1, action_type=5)  # DELETE 清理动作
    _seed_device_file(connection, 501, 11)
    _seed_output(connection, 701, 11, 501)
    _seed_selection_and_item(
        connection, dependency_id=1, selection_id=1, item_id=101,
        owner_action_id=31, source_action_id=11, output_id=701,
    )
    _seed_cleanup_item(
        connection, 601, 91, 701, status=2, restriction_state=2
    )  # PENDING_DELETE + ACTIVE 保护
    connection.commit()

    repository = OutputsRepository()
    candidate = _Seedling(action_id=31, item_id=101, output_id=701, file_id=501)
    result = _grant(owned, repository, _candidate(candidate))
    assert result.kind is DbOutcomeKind.COMPLETED
    assert result.value.outcome is QualificationOutcome.REJECTED_FINAL
    item = _row(
        owned,
        "SELECT status, error_code, source_dependency FROM obtain_items WHERE id = 101",
    )
    assert item[0] == 4  # FAILED
    assert item[1] == 3  # output_unavailable
    assert item[2] == 0
    assert _row(owned, "SELECT count(*) FROM file_copies")[0] == 0
    assert _row(owned, "SELECT count(*) FROM deliveries")[0] == 0
    assert _row(owned, "SELECT count(*) FROM intermediate_files")[0] == 0
    assert _row(owned, "SELECT count(*) FROM operation_runs WHERE kind = 3")[0] == 0
    owned.close()


def test_delete_in_progress_rejects_with_dedicated_code(tmp_path: Path) -> None:
    target, owned = _seed_environment(tmp_path)
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    _seed_plan(connection, 1)
    _seed_action(connection, 11, 1, action_type=2)
    _seed_action(connection, 31, 1, action_type=4, scheduled_at=_NOW)
    _seed_action(connection, 91, 1, action_type=5)
    _seed_device_file(connection, 501, 11)
    _seed_output(connection, 701, 11, 501)
    _seed_selection_and_item(
        connection, dependency_id=1, selection_id=1, item_id=101,
        owner_action_id=31, source_action_id=11, output_id=701,
    )
    _seed_cleanup_item(
        connection, 601, 91, 701, status=3, restriction_state=2
    )  # DELETING：唯一删除处理者持有目标
    connection.commit()

    repository = OutputsRepository()
    candidate = _Seedling(action_id=31, item_id=101, output_id=701, file_id=501)
    result = _grant(owned, repository, _candidate(candidate))
    assert result.value.outcome is QualificationOutcome.REJECTED_FINAL
    item = _row(owned, "SELECT status, error_code FROM obtain_items WHERE id = 101")
    assert item == (4, 4)  # FAILED + output_cleanup_started
    owned.close()


def test_cross_device_candidates_do_not_block(tmp_path: Path) -> None:
    """更早候选属于其他设备时不阻挡本设备授予。"""
    target, owned = _seed_environment(tmp_path)
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    _seed_plan(connection, 1)
    _seed_action(connection, 11, 1, action_type=2, device_id="cam-1")
    _seed_action(connection, 12, 1, action_type=2, device_id="cam-2")
    _seed_action(connection, 31, 1, action_type=4, scheduled_at=_LATER)
    _seed_action(connection, 32, 1, action_type=4, scheduled_at=_NOW)
    _seed_device_file(connection, 501, 11)
    _seed_device_file(connection, 502, 12)
    _seed_output(connection, 701, 11, 501)
    _seed_output(connection, 702, 12, 502)
    _seed_selection_and_item(
        connection, dependency_id=1, selection_id=1, item_id=101,
        owner_action_id=32, source_action_id=11, output_id=701,
    )
    connection.commit()

    repository = OutputsRepository()
    # cam-2 的更早取回动作 32 尚未具备候选（无 item）；本候选属于 cam-1。
    candidate = _Seedling(action_id=31, item_id=101, output_id=701, file_id=501)
    result = _grant(owned, repository, _candidate(candidate))
    assert _granted(result)
    owned.close()


def test_grant_creates_all_records_atomically(tmp_path: Path) -> None:
    target, owned = _seed_environment(tmp_path)
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    _seed_plan(connection, 1)
    _seed_action(connection, 11, 1, action_type=2)
    _seed_action(connection, 31, 1, action_type=4, scheduled_at=_NOW)
    _seed_device_file(connection, 501, 11)
    _seed_output(connection, 701, 11, 501)
    _seed_selection_and_item(
        connection, dependency_id=1, selection_id=1, item_id=101,
        owner_action_id=31, source_action_id=11, output_id=701,
    )
    connection.commit()

    repository = OutputsRepository()
    candidate = _Seedling(action_id=31, item_id=101, output_id=701, file_id=501)
    grant_key = new_operation_key()
    result = repository.grant_file(_candidate(candidate), grant_key, owned)
    assert _granted(result)
    value = result.value
    delivery = _row(
        owned,
        "SELECT status, withdrawal_state, action_id, output_id FROM deliveries"
        " WHERE id = ?",
        value.delivery_id,
    )
    assert delivery == (1, 1, 31, 701)
    copy = _row(
        owned,
        "SELECT delivery_id, processing_id, source_device_file_id, round,"
        " committed_bytes, reset_state, slot_device_id, verification_state,"
        " source_size FROM file_copies WHERE id = ?",
        value.copy_id,
    )
    assert copy == (
        value.delivery_id, None, 501, 1, 0, 1, "cam-1", 1, 4096,
    )
    run = _row(
        owned,
        "SELECT kind, delivery_id, copy_id, status, attempts_used,"
        " max_attempts_used, timeout_s_json, retry_interval_s_json,"
        " retry_wait_required FROM operation_runs WHERE id = ?",
        value.run_id,
    )
    assert run == (
        3, value.delivery_id, value.copy_id, 1, 0, 3, "10", "3", 0,
    )
    assert _row(
        owned, "SELECT responsibility_key FROM operation_runs WHERE id = ?",
        value.run_id,
    )[0] == f"read/{value.copy_id}"
    target_file = _row(
        owned,
        "SELECT purpose, owner_delivery_id, retention_state, cleanup_state"
        " FROM intermediate_files WHERE id = ?",
        value.target_file_id,
    )
    assert target_file == (1, value.delivery_id, 1, 1)
    item = _row(
        owned,
        "SELECT status, source_dependency, delivery_id FROM obtain_items"
        " WHERE id = 101",
    )
    assert item == (3, 1, value.delivery_id)

    # 同键重送复用原结果；同项新键不重复授予。
    repeat = repository.grant_file(_candidate(candidate), grant_key, owned)
    assert repeat.kind is DbOutcomeKind.COMPLETED
    assert repeat.value.copy_id == value.copy_id
    assert _row(owned, "SELECT count(*) FROM deliveries")[0] == 1
    again = _grant(owned, repository, _candidate(candidate, suffix="d"))
    assert not _granted(again)
    assert _row(owned, "SELECT count(*) FROM deliveries")[0] == 1
    owned.close()


def test_internal_processing_grant_skips_delivery(tmp_path: Path) -> None:
    """内部检查/修复共用读取机会：建档不含交付与取回项更新。"""
    target, owned = _seed_environment(tmp_path)
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    _seed_plan(connection, 1)
    _seed_action(connection, 11, 1, action_type=2)
    _seed_device_file(connection, 501, 11)
    _seed_output(connection, 701, 11, 501)
    _seed_processing(connection, 5, 11, 501)
    connection.commit()

    repository = OutputsRepository()
    candidate = _Seedling(
        action_id=11, item_id=None, output_id=701, file_id=501, processing_id=5
    )
    result = _grant(owned, repository, _candidate(candidate))
    assert _granted(result)
    value = result.value
    assert value.delivery_id is None
    assert _row(owned, "SELECT count(*) FROM deliveries")[0] == 0
    copy = _row(
        owned,
        "SELECT delivery_id, processing_id, slot_device_id FROM file_copies"
        " WHERE id = ?",
        value.copy_id,
    )
    assert copy == (None, 5, "cam-1")
    run = _row(
        owned, "SELECT delivery_id, kind FROM operation_runs WHERE id = ?",
        value.run_id,
    )
    assert run == (None, 3)
    target_file = _row(
        owned,
        "SELECT purpose, owner_action_id FROM intermediate_files WHERE id = ?",
        value.target_file_id,
    )
    assert target_file == (2, 11)
    owned.close()


def test_unavailable_output_rejects_without_records(tmp_path: Path) -> None:
    """产物不可用：逐项失败且不建立任何读取档案（整笔拒绝）。"""
    target, owned = _seed_environment(tmp_path)
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    _seed_plan(connection, 1)
    _seed_action(connection, 11, 1, action_type=2)
    _seed_action(connection, 31, 1, action_type=4, scheduled_at=_NOW)
    _seed_device_file(connection, 501, 11)
    _seed_output(connection, 701, 11, 501, availability=2)  # RESTRICTED
    _seed_selection_and_item(
        connection, dependency_id=1, selection_id=1, item_id=101,
        owner_action_id=31, source_action_id=11, output_id=701,
    )
    connection.commit()

    repository = OutputsRepository()
    candidate = _Seedling(action_id=31, item_id=101, output_id=701, file_id=501)
    result = _grant(owned, repository, _candidate(candidate))
    assert result.value.outcome is QualificationOutcome.REJECTED_FINAL
    item = _row(owned, "SELECT status, error_code FROM obtain_items WHERE id = 101")
    assert item == (4, 3)
    assert _row(owned, "SELECT count(*) FROM file_copies")[0] == 0
    assert _row(owned, "SELECT count(*) FROM deliveries")[0] == 0
    assert _row(owned, "SELECT count(*) FROM operation_runs WHERE kind = 3")[0] == 0
    owned.close()

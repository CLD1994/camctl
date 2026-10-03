"""读取资格共同建档、独立副本及清理限制的真实 SQLite 集成测试。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import register_acceptance_guards
from camctl.persistence.repositories.operations import register_operation_guards
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
register_operation_guards()
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


@pytest.fixture
def qualification_environment(tmp_path):
    _, owned = _seed_environment(tmp_path)
    try:
        yield owned
    finally:
        owned.connection.close()


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
        " VALUES (?, ?, ?, ?, '{}', '{}', 'video.mp4', 'video/mp4', 2, NULL, NULL,"
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
        "UPDATE actions SET source_resolution_state=2,"
        " resolved_source_plan_id=(SELECT plan_id FROM actions WHERE id=?) WHERE id=?",
        (source_action_id, owner_action_id),
    )
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



_CONFIG = OperationConfig(
    max_attempts=3,
    timeout_s=Decimal("10"),
    retry_interval_s=Decimal("3"),
)


def _candidate(seedling: _Seedling) -> FileCandidate:
    return FileCandidate(
        action_id=seedling.action_id,
        item_id=seedling.item_id,
        processing_id=seedling.processing_id,
        output_id=seedling.output_id if seedling.processing_id is None else None,
        source_device_file_id=seedling.file_id,
        target_extension="part",
        delivery_extension="mp4" if seedling.processing_id is None else None,
        delivery_display_name=f"产物 {seedling.output_id}" if seedling.processing_id is None else None,
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


@pytest.mark.parametrize("occurred_at", [_NOW, _LATER])
def test_different_products_prepare_independently_after_their_due_time(qualification_environment, occurred_at):
    """两份产物分别建档；未来动作等待自己的计划时间。"""
    owned = qualification_environment
    connection = owned.connection
    _seed_plan(connection, 1)
    for source, file_id, output_id, owner, item, when in (
        (11, 501, 701, 31, 101, _NOW), (12, 502, 702, 32, 102, _LATER),
    ):
        _seed_action(connection, source, 1, action_type=2, status=3)
        _seed_action(connection, owner, 1, action_type=4, scheduled_at=when)
        _seed_device_file(connection, file_id, source)
        _seed_output(connection, output_id, source, file_id)
        _seed_selection_and_item(connection, dependency_id=item, selection_id=item, item_id=item,
            owner_action_id=owner, source_action_id=source, output_id=output_id)
    connection.commit()
    repository = OutputsRepository()
    early = _candidate(_Seedling(action_id=31, item_id=101, output_id=701, file_id=501))
    late = replace(_candidate(_Seedling(action_id=32, item_id=102, output_id=702, file_id=502)),
                   occurred_at=occurred_at)
    before = tuple(connection.iterdump())
    result = _grant(owned, repository, late)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    if occurred_at == _NOW:
        assert result.value.outcome is QualificationOutcome.REJECTED
        assert result.value.reason == "not_due"
        assert tuple(connection.iterdump()) == before
    else:
        assert _granted(result), result.value
    first = _grant(owned, repository, replace(early, occurred_at=occurred_at))
    assert _granted(first), first.error or first.value
    assert connection.execute("SELECT slot_device_id FROM file_copies").fetchall() == (
        [(None,)] if occurred_at == _NOW else [(None,), (None,)])


@pytest.mark.parametrize("first_kind", ["obtain", "internal"])
@pytest.mark.parametrize("internal_time", [_NOW - 1_000_000, _NOW, _NOW + 1_000_000])
def test_internal_and_delivery_preparations_do_not_compete_for_camera_slot(
    qualification_environment, first_kind, internal_time,
):
    """两种用途先建立独立准备责任，相机机会另行竞争。"""
    owned = qualification_environment
    connection = owned.connection
    _seed_plan(connection, 1)
    _seed_action(connection, 11, 1, action_type=2, status=3)
    _seed_action(connection, 41, 1, action_type=4, scheduled_at=_NOW)
    _seed_action(connection, 42, 1, action_type=2, scheduled_at=internal_time)
    _seed_device_file(connection, 501, 11)
    _seed_device_file(connection, 502, 42)
    _seed_output(connection, 701, 11, 501)
    _seed_selection_and_item(connection, dependency_id=1, selection_id=1, item_id=101,
        owner_action_id=41, source_action_id=11, output_id=701)
    _seed_processing(connection, 5, 42, 502)
    connection.execute("UPDATE recording_processing SET check_decision=3, check_basis_json=? WHERE id=5",
        (json.dumps({"reason": "INSUFFICIENT_TIMING", "target_duration_ms": 1000}),))
    connection.commit()
    candidates = {
        "obtain": _candidate(_Seedling(action_id=41, item_id=101, output_id=701, file_id=501)),
        "internal": _candidate(_Seedling(action_id=42, item_id=None, output_id=0, file_id=502, processing_id=5)),
    }
    order = (first_kind, "internal" if first_kind == "obtain" else "obtain")
    repository = OutputsRepository()
    results = []
    for kind in order:
        result = _grant(owned, repository, replace(candidates[kind], occurred_at=_LATER))
        assert _granted(result), result.error or result.value
        results.append(result.value)
    assert results[0].copy_id != results[1].copy_id
    assert results[0].target_file_id != results[1].target_file_id
    assert connection.execute("SELECT slot_device_id FROM file_copies").fetchall() == [(None,), (None,)]


@pytest.mark.parametrize("slot", [None, "cam-1"])
def test_existing_read_protection_preserves_original_copy(qualification_environment, slot):
    """同产物的新取回建立独立副本，不改变旧保护、进度及相机机会。"""
    owned = qualification_environment
    connection = owned.connection
    _seed_plan(connection, 1)
    _seed_action(connection, 11, 1, action_type=2, status=3)
    _seed_device_file(connection, 501, 11)
    _seed_output(connection, 701, 11, 501)
    for owner, item, when in ((31, 101, _NOW), (32, 102, _LATER)):
        _seed_action(connection, owner, 1, action_type=4, scheduled_at=when)
        _seed_selection_and_item(connection, dependency_id=item, selection_id=item, item_id=item,
            owner_action_id=owner, source_action_id=11, output_id=701)
    connection.commit()
    repository = OutputsRepository()
    first = _grant(owned, repository, _candidate(_Seedling(31, 101, 701, 501)))
    assert _granted(first), first.error or first.value
    # 原持有状态及已保存进度是当前投影夹具，不代替机会与分段事务验收。
    connection.execute("UPDATE file_copies SET slot_device_id=?, committed_bytes=1024 WHERE id=?",
                       (slot, first.value.copy_id))
    connection.commit()
    previous = _row(owned, "SELECT * FROM file_copies WHERE id=?", first.value.copy_id)
    later = replace(_candidate(_Seedling(32, 102, 701, 501)), occurred_at=_LATER)
    second = _grant(owned, repository, later)
    assert _granted(second), second.error or second.value
    assert second.value.copy_id != first.value.copy_id
    assert second.value.delivery_id != first.value.delivery_id
    assert second.value.target_file_id != first.value.target_file_id
    assert _row(owned, "SELECT * FROM file_copies WHERE id=?", first.value.copy_id) == previous
    assert _row(owned, "SELECT slot_device_id FROM file_copies WHERE id=?", second.value.copy_id) == (None,)
    assert connection.execute("SELECT source_dependency FROM obtain_items ORDER BY id").fetchall() == [(1,), (1,)]


def test_cleanup_restriction_rejects_and_records(qualification_environment) -> None:
    """不可撤销清理限制：逐项保存最终失败，不建立任何读取档案。"""
    owned = qualification_environment
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    _seed_plan(connection, 1)
    _seed_action(connection, 11, 1, action_type=2, status=3)
    _seed_action(connection, 31, 1, action_type=4, scheduled_at=_NOW)
    _seed_action(connection, 91, 1, action_type=5)  # DELETE 清理动作
    _seed_device_file(connection, 501, 11)
    _seed_output(connection, 701, 11, 501, availability=2)
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
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is QualificationOutcome.REJECTED_FINAL
    item = _row(
        owned,
        "SELECT status, error_code, source_dependency FROM obtain_items WHERE id = 101",
    )
    assert item[0] == 4  # FAILED
    assert item[1] == 4  # output_cleanup_started
    assert item[2] == 0
    assert _row(owned, "SELECT count(*) FROM file_copies")[0] == 0
    assert _row(owned, "SELECT count(*) FROM deliveries")[0] == 0
    assert _row(owned, "SELECT count(*) FROM intermediate_files")[0] == 0
    assert _row(owned, "SELECT count(*) FROM operation_runs WHERE kind = 3")[0] == 0


def test_delete_in_progress_rejects_with_dedicated_code(qualification_environment) -> None:
    owned = qualification_environment
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    _seed_plan(connection, 1)
    _seed_action(connection, 11, 1, action_type=2, status=3)
    _seed_action(connection, 31, 1, action_type=4, scheduled_at=_NOW)
    _seed_action(connection, 91, 1, action_type=5)
    _seed_device_file(connection, 501, 11)
    _seed_output(connection, 701, 11, 501, availability=2)
    connection.execute("UPDATE outputs SET cleanup_status=3 WHERE id=701")
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
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is QualificationOutcome.REJECTED_FINAL
    item = _row(owned, "SELECT status, error_code FROM obtain_items WHERE id = 101")
    assert item == (4, 4)  # FAILED + output_cleanup_started


def test_cross_device_candidates_do_not_block(qualification_environment) -> None:
    """更早候选属于其他设备时不阻挡本设备授予。"""
    owned = qualification_environment
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    _seed_plan(connection, 1)
    _seed_action(connection, 11, 1, action_type=2, device_id="cam-1", status=3)
    _seed_action(connection, 12, 1, action_type=2, device_id="cam-2", status=3)
    _seed_action(connection, 31, 1, action_type=4, scheduled_at=_LATER)
    _seed_action(connection, 32, 1, action_type=4, scheduled_at=_NOW)
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
    # 两个相机各有已到期的合法候选；先申请计划时间较晚的 cam-1。
    candidate = _Seedling(action_id=31, item_id=101, output_id=701, file_id=501)
    result = _grant(owned, repository, replace(_candidate(candidate), occurred_at=_LATER))
    assert _granted(result), result.error


def test_grant_creates_all_records_atomically(qualification_environment) -> None:
    owned = qualification_environment
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
        value.delivery_id, None, 501, 1, 0, 1, None, 1, 4096,
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

    # 同键恢复首次结果，同项新键复用原准备责任且不写入新事实。
    repeat = repository.grant_file(_candidate(candidate), grant_key, owned)
    assert repeat.kind is DbOutcomeKind.COMPLETED
    assert repeat.value.copy_id == value.copy_id
    assert _row(owned, "SELECT count(*) FROM deliveries")[0] == 1
    before = tuple(connection.iterdump())
    again = _grant(owned, repository, _candidate(candidate))
    assert _granted(again), again.error
    assert (again.value.copy_id, again.value.run_id, again.value.delivery_id, again.value.target_file_id) == (
        value.copy_id, value.run_id, value.delivery_id, value.target_file_id)
    assert tuple(connection.iterdump()) == before
    assert _row(owned, "SELECT count(*) FROM deliveries")[0] == 1


def test_internal_processing_grant_skips_delivery(qualification_environment) -> None:
    """内部输入建档不含交付与取回项更新，初始读取机会为空。"""
    owned = qualification_environment
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
    assert copy == (None, 5, None)
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


def test_unavailable_output_rejects_without_records(qualification_environment) -> None:
    """产物不可用：逐项失败且不建立任何读取档案（整笔拒绝）。"""
    owned = qualification_environment
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    _seed_plan(connection, 1)
    _seed_action(connection, 11, 1, action_type=2, status=3)
    _seed_action(connection, 31, 1, action_type=4, scheduled_at=_NOW)
    _seed_device_file(connection, 501, 11, presence_state=3)
    _seed_output(connection, 701, 11, 501, availability=4)  # MISSING
    _seed_selection_and_item(
        connection, dependency_id=1, selection_id=1, item_id=101,
        owner_action_id=31, source_action_id=11, output_id=701,
    )
    connection.commit()

    repository = OutputsRepository()
    candidate = _Seedling(action_id=31, item_id=101, output_id=701, file_id=501)
    result = _grant(owned, repository, _candidate(candidate))
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is QualificationOutcome.REJECTED_FINAL
    item = _row(owned, "SELECT status, error_code FROM obtain_items WHERE id = 101")
    assert item == (4, 3)
    assert _row(owned, "SELECT count(*) FROM file_copies")[0] == 0
    assert _row(owned, "SELECT count(*) FROM deliveries")[0] == 0
    assert _row(owned, "SELECT count(*) FROM operation_runs WHERE kind = 3")[0] == 0


@pytest.mark.parametrize("cleanup_time", [_NOW - 1_000_000, _NOW, _NOW + 1_000_000])
@pytest.mark.parametrize("cleanup_status", [1, 2])
@pytest.mark.parametrize("selection_fixed", [False, True])
def test_earlier_cleanup_participates_before_restriction_is_saved(
    qualification_environment, cleanup_time, cleanup_status, selection_fixed,
):
    """已到期清理即使尚未保存限制，也参与同产物资格顺序。"""
    owned = qualification_environment
    connection = owned.connection
    _seed_plan(connection, 1)
    _seed_action(connection, 11, 1, action_type=2, status=3)
    _seed_action(connection, 31, 1, action_type=4, scheduled_at=_NOW)
    _seed_action(connection, 91, 1, action_type=5, scheduled_at=cleanup_time, status=cleanup_status)
    _seed_device_file(connection, 501, 11)
    _seed_output(connection, 701, 11, 501)
    _seed_selection_and_item(connection, dependency_id=1, selection_id=1, item_id=101,
        owner_action_id=31, source_action_id=11, output_id=701)
    connection.execute("UPDATE actions SET input_fields_json=?, target_selection_state=? WHERE id=91",
        (json.dumps({"params": {"output_ids": ["701"]}}), 2 if selection_fixed else 1))
    if selection_fixed:
        connection.execute(
            "INSERT INTO cleanup_items (id, action_id, requested_output_id, output_id, status, restriction_state)"
            " VALUES (601, 91, 701, NULL, 1, 1)")
    connection.commit()
    before = tuple(connection.iterdump())
    candidate = replace(_candidate(_Seedling(31, 101, 701, 501)), occurred_at=_LATER)
    result = _grant(owned, OutputsRepository(), candidate)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    if cleanup_time < _NOW:
        assert result.value.outcome is QualificationOutcome.REJECTED
        assert result.value.reason == "business_order"
        assert tuple(connection.iterdump()) == before
    else:
        assert _granted(result), result.value
        assert _row(owned, "SELECT source_dependency FROM obtain_items WHERE id=101") == (1,)


@pytest.mark.parametrize("state", ["future", "canceled", "terminal", "other_output"])
def test_ineligible_or_unrelated_cleanup_does_not_block_preparation(qualification_environment, state):
    """只有当前合格且针对同一产物的清理候选参与比较。"""
    owned = qualification_environment
    connection = owned.connection
    _seed_plan(connection, 1)
    _seed_action(connection, 11, 1, action_type=2, status=3)
    _seed_action(connection, 31, 1, action_type=4)
    _seed_action(connection, 91, 1, action_type=5,
        scheduled_at=_LATER + 1_000_000 if state == "future" else _NOW - 1_000_000,
        status=2)
    _seed_device_file(connection, 501, 11)
    _seed_action(connection, 12, 1, action_type=2, status=3)
    _seed_device_file(connection, 502, 12)
    _seed_output(connection, 701, 11, 501)
    _seed_output(connection, 702, 12, 502)
    _seed_selection_and_item(connection, dependency_id=1, selection_id=1, item_id=101,
        owner_action_id=31, source_action_id=11, output_id=701)
    connection.execute("UPDATE actions SET input_fields_json=?, target_selection_state=1, cancel_requested=?, status=? WHERE id=91",
        (json.dumps({"params": {"output_ids": ["702" if state == "other_output" else "701"]}}),
         int(state in ("canceled", "terminal")), 6 if state == "terminal" else 2))
    connection.commit()
    result = _grant(owned, OutputsRepository(), replace(_candidate(_Seedling(31, 101, 701, 501)), occurred_at=_LATER))
    assert _granted(result), result.error or result.value
    assert _row(owned, "SELECT slot_device_id FROM file_copies WHERE id=?", result.value.copy_id) == (None,)


@pytest.mark.parametrize("owner_status,selection_fixed", [(1, False), (2, False), (2, True)])
def test_earlier_obtain_participates_before_its_selection_is_saved(
    qualification_environment, owner_status, selection_fixed,
):
    """来源已结束且默认目标可确定时，保存选择的先后不能改变资格顺序。"""
    owned = qualification_environment
    connection = owned.connection
    _seed_plan(connection, 1)
    _seed_action(connection, 11, 1, action_type=2, status=3)
    _seed_device_file(connection, 501, 11)
    _seed_output(connection, 701, 11, 501)
    for owner, item, when, status in ((31, 101, _NOW, owner_status), (32, 102, _LATER, 2)):
        _seed_action(connection, owner, 1, action_type=4, scheduled_at=when, status=status)
        _seed_selection_and_item(connection, dependency_id=item, selection_id=item, item_id=item,
            owner_action_id=owner, source_action_id=11, output_id=701)
        connection.execute("UPDATE actions SET input_fields_json=? WHERE id=?",
            (json.dumps({"params": {"source": {"action_name": "action-11"}}}), owner))
    if not selection_fixed:
        connection.execute("DELETE FROM obtain_items WHERE id=101")
        connection.execute("UPDATE obtain_source_selections SET status=1 WHERE id=101")
    connection.commit()
    before = tuple(connection.iterdump())
    result = _grant(owned, OutputsRepository(), replace(_candidate(_Seedling(32, 102, 701, 501)), occurred_at=_LATER))
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is QualificationOutcome.REJECTED
    assert result.value.reason == "business_order"
    assert tuple(connection.iterdump()) == before

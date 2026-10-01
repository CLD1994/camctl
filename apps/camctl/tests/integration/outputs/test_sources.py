"""X2 来源固定与产物选择的组件集成测试。

真实 SQLite 与 P3 事务内核组合：执行期来源解析一次固定跨计划
成员并共同初始化 PENDING 选择，可靠失败保存动作终态；六种来源
引用形式按数据库事实解析；选择固定后重启、重送不重选、不扩大
集合；精确 ID 部分失败逐项保存且不创建交付。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.outputs.sources import (
    ResolutionState,
    SelectionMode,
    SourceResolution,
    SourceSpec,
    select_outputs,
    resolve_source,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import register_acceptance_guards
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.outputs import (
    FixSelection,
    OutputsRepository,
    ResolveSources,
    SqliteSourceLookup,
    load_selection,
    load_selection_facts,
    register_outputs_guards,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..persistence.test_runtime import _create_valid_database

register_acceptance_guards()
register_capture_guards()  # SOURCE_RESOLVED.FAIL 复用动作终态守卫
register_outputs_guards()

_NOW = 1_750_000_000_000_000

#: device_files.role 与产物种类的对应（按登记：1 原片/2 修复/3 预览）。
_ROLE_FOR_KIND = {1: 1, 2: 2, 3: 3}


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
    status: int = 2,
    group_name: str | None = None,
    name: str | None = None,
    source_resolution_state: int | None = None,
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
            " VALUES (?, ?, ?, ?, ?, 'cam-1', ?, ?, '{}', '{}', 'camctl-adb', 1000,"
            " '{}', ?, ?, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, 1, 1)",
            (
                action_id,
                plan_id,
                action_id,
                name if name is not None else f"action-{action_id}",
                action_type,
                _NOW,
                group_name,
                status,
                started,
            ),
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
        " VALUES (?, ?, ?, ?, ?, NULL, ?, ?, '{}', NULL, NULL, NULL, ?,"
        " ?, ?, 0, NULL, NULL, NULL, NULL, ?, NULL, NULL, 1, 1, 1)",
        (
            action_id,
            plan_id,
            action_id,
            name if name is not None else f"action-{action_id}",
            action_type,
            _NOW,
            group_name,
            spec,
            status,
            started,
            source_resolution_state,
        ),
    )


def _seed_device_file(
    connection: sqlite3.Connection,
    file_id: int,
    source_action_id: int,
    *,
    role: int,
    size: int = 1024,
) -> None:
    connection.execute(
        "INSERT INTO device_files (id, observer_action_id, source_action_id,"
        " identity_key, locator_json, ownership_evidence_json, original_name,"
        " media_type, role, original_device_file_id, pairing_evidence_json,"
        " presence_state, completion_state, completion_evidence_json, size_bytes,"
        " checksum_support, sha256, last_error_json, created_event_id,"
        " last_event_id, change_count)"
        " VALUES (?, ?, ?, ?, '{}', '{}', 'video.mp4', 'video/mp4', ?, NULL, NULL,"
        " 2, 3, '{}', ?, 3, NULL, NULL, 1, 1, 1)",
        (
            file_id,
            source_action_id,
            source_action_id,
            f"file-{file_id:04d}",
            role,
            size,
        ),
    )


def _seed_output(
    connection: sqlite3.Connection,
    output_id: int,
    source_action_id: int,
    kind: int,
    *,
    file_id: int | None = None,
    availability: int = 1,
    original_output_id: int | None = None,
    size: int = 1024,
) -> None:
    if file_id is None:
        file_id = 900 + output_id
    _seed_device_file(
        connection, file_id, source_action_id, role=_ROLE_FOR_KIND[kind], size=size
    )
    cleanup = {1: 1, 2: 2, 3: 4, 4: 1, 5: 1}[availability]
    error_json = None if availability in (1, 2, 3) else {"reason": "seed"}
    connection.execute(
        "INSERT INTO outputs (id, source_action_id, kind, device_file_id,"
        " intermediate_file_id, original_name, media_type, availability,"
        " cleanup_status, cleanup_error_json, media_json, error_json,"
        " created_event_id, last_event_id, change_count)"
        " VALUES (?, ?, ?, ?, NULL, 'video.mp4', 'video/mp4', ?, ?, NULL, '{}', ?,"
        " 1, 1, 1)",
        (
            output_id,
            source_action_id,
            kind,
            file_id,
            availability,
            cleanup,
            json.dumps(error_json) if error_json is not None else None,
        ),
    )
    if original_output_id is not None:
        connection.execute(
            "INSERT INTO output_origins (id, output_id, original_output_id)"
            " VALUES (?, ?, ?)",
            (output_id, output_id, original_output_id),
        )


def _value(owned_or_connection, sql: str, *params):
    connection = (
        owned_or_connection.connection
        if hasattr(owned_or_connection, "connection")
        else owned_or_connection
    )
    row = connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


def _six_form_environment(tmp_path: Path):
    """六种来源形式共享的数据库事实。

    计划 1（发起取回所属）：11 录像（组“上午”，名“录像A”）、
    12 report_status（组“上午”）、13 录像（组“下午”）、14 录像
    （无组）。计划 2：21 录像（组“晨拍”）、22 report_status。
    取回动作 30 属于计划 1，处于执行中且来源待解析。
    """
    target, owned = _seed_environment(tmp_path)
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    _seed_plan(connection, 1)
    _seed_plan(connection, 2)
    _seed_action(connection, 11, 1, action_type=2, group_name="上午", name="录像A")
    _seed_action(connection, 12, 1, action_type=7, group_name="上午")
    _seed_action(connection, 13, 1, action_type=2, group_name="下午")
    _seed_action(connection, 14, 1, action_type=2)
    _seed_action(connection, 21, 2, action_type=2, group_name="晨拍")
    _seed_action(connection, 22, 2, action_type=7)
    _seed_action(connection, 30, 1, action_type=4, source_resolution_state=1)
    connection.commit()
    return target, owned


def _fixed_resolution(members: tuple[int, ...]) -> SourceResolution:
    return SourceResolution(
        state=ResolutionState.FIXED,
        member_action_ids=members,
        source_plan_id=2,
    )


def test_all_six_source_forms_resolve_against_sqlite(tmp_path: Path) -> None:
    """六种形式按数据库事实解析：拍摄成员入选，非产物动作排除。"""
    _, owned = _six_form_environment(tmp_path)
    try:
        lookup = SqliteSourceLookup(owned.connection, 30)
        cases = [
            (SourceSpec(action_instance_id=21), (21,), 2),
            (SourceSpec(plan_instance_id=2, group="晨拍"), (21,), 2),
            (SourceSpec(action_name="录像A"), (11,), 1),
            (SourceSpec(group="上午"), (11,), 1),
            (SourceSpec(current_plan=True), (11, 13, 14), 1),
            (SourceSpec(plan_instance_id=2), (21,), 2),
        ]
        for spec, expected_members, expected_plan in cases:
            resolution = resolve_source(spec, lookup)
            assert resolution.state is ResolutionState.FIXED, spec
            assert resolution.member_action_ids == expected_members, spec
            assert resolution.source_plan_id == expected_plan, spec
    finally:
        owned.connection.close()


def test_resolve_sources_fixes_members_and_initializes_selections(
    tmp_path: Path,
) -> None:
    """执行期固定：成员、依赖与 PENDING 选择同一事务保存。"""
    _, owned = _six_form_environment(tmp_path)
    repository = OutputsRepository()
    try:
        outcome = repository.resolve_sources(
            ResolveSources(
                action_id=30,
                spec=SourceSpec(plan_instance_id=2),
                occurred_at=_NOW,
            ),
            new_operation_key(),
            owned,
        )
        assert outcome.kind is DbOutcomeKind.COMPLETED
        assert outcome.value.fixed is True
        assert outcome.value.member_action_ids == (21,)
        assert outcome.value.source_plan_id == 2
        assert len(outcome.value.selection_ids) == 1

        action = _value(
            owned,
            "SELECT source_resolution_state, resolved_source_plan_id, status"
            " FROM actions WHERE id = 30",
        )
        assert action == (2, 2, 2)
        dependency = _value(
            owned,
            "SELECT id, action_id, depends_on_action_id FROM action_dependencies",
        )
        assert dependency == (1, 30, 21)
        selection = _value(
            owned,
            "SELECT dependency_id, status, error_code, error_details_json"
            " FROM obtain_source_selections",
        )
        assert selection == (1, 1, None, None)
    finally:
        owned.connection.close()


def test_resolve_sources_failure_saves_terminal_action(tmp_path: Path) -> None:
    """可靠确认引用无效：动作失败终态与错误详情共同保存。"""
    _, owned = _six_form_environment(tmp_path)
    repository = OutputsRepository()
    try:
        outcome = repository.resolve_sources(
            ResolveSources(
                action_id=30,
                spec=SourceSpec(action_instance_id=99),
                occurred_at=_NOW,
            ),
            new_operation_key(),
            owned,
        )
        assert outcome.kind is DbOutcomeKind.COMPLETED
        assert outcome.value.fixed is False
        assert outcome.value.error_code == 20  # source_resolution_failed

        action = _value(
            owned,
            "SELECT source_resolution_state, status, error_code, error_details_json"
            " FROM actions WHERE id = 30",
        )
        assert action[0] == 3
        assert action[1] == 4
        assert action[2] == 20
        details = json.loads(action[3])
        assert details["reason"] == "action_not_found"
        assert details["source"] == {"action_instance_id": 99}
        assert _value(owned, "SELECT COUNT(*) FROM action_dependencies")[0] == 0
        assert (
            _value(owned, "SELECT COUNT(*) FROM obtain_source_selections")[0] == 0
        )
    finally:
        owned.connection.close()


def test_legal_empty_source_set_fixes_without_selections(tmp_path: Path) -> None:
    """计划范围没有拍摄成员：合法空集合固定，零条选择。"""
    target, owned = _six_form_environment(tmp_path)
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    # 用取消动作替身构造只剩非产物来源的计划范围场景：直接移除录像 21。
    connection.execute("DELETE FROM actions WHERE id = 21")
    connection.commit()
    repository = OutputsRepository()
    try:
        outcome = repository.resolve_sources(
            ResolveSources(
                action_id=30,
                spec=SourceSpec(plan_instance_id=2),
                occurred_at=_NOW,
            ),
            new_operation_key(),
            owned,
        )
        assert outcome.kind is DbOutcomeKind.COMPLETED
        assert outcome.value.fixed is True
        assert outcome.value.member_action_ids == ()
        assert outcome.value.selection_ids == ()
        action = _value(
            owned,
            "SELECT source_resolution_state, resolved_source_plan_id"
            " FROM actions WHERE id = 30",
        )
        assert action == (2, 2)
        assert _value(owned, "SELECT COUNT(*) FROM action_dependencies")[0] == 0
    finally:
        owned.connection.close()


def _prepared_selection(tmp_path: Path):
    """固定单一来源（计划 2 的动作 21）并把来源置为成功终态。"""
    target, owned = _six_form_environment(tmp_path)
    repository = OutputsRepository()
    outcome = repository.resolve_sources(
        ResolveSources(
            action_id=30,
            spec=SourceSpec(action_instance_id=21),
            occurred_at=_NOW,
        ),
        new_operation_key(),
        owned,
    )
    assert outcome.kind is DbOutcomeKind.COMPLETED
    selection_id = outcome.value.selection_ids[0]
    owned.connection.execute("BEGIN IMMEDIATE")
    owned.connection.execute("UPDATE actions SET status = 3 WHERE id = 21")
    owned.connection.commit()
    return target, owned, repository, selection_id


def test_fix_selection_default_replaces_original_with_repaired(
    tmp_path: Path,
) -> None:
    """默认方式：修复替代原片、预览不入选，条目与 FIXED 共同保存。"""
    _, owned, repository, selection_id = _prepared_selection(tmp_path)
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    _seed_output(connection, 501, 21, 1)
    _seed_output(connection, 502, 21, 3, original_output_id=501)
    _seed_output(connection, 503, 21, 2, original_output_id=501)
    connection.commit()
    try:
        facts = load_selection_facts(connection, 21)
        snapshot = select_outputs(_fixed_resolution((21,)), facts, SelectionMode.DEFAULT)
        outcome = repository.fix_selection(
            FixSelection(selection_id=selection_id, snapshot=snapshot, occurred_at=_NOW),
            new_operation_key(),
            owned,
        )
        assert outcome.kind is DbOutcomeKind.COMPLETED
        assert outcome.value.snapshot.selected_output_ids == (503,)

        selection = _value(
            owned,
            "SELECT status, error_code FROM obtain_source_selections WHERE id = ?",
            selection_id,
        )
        assert selection == (2, None)
        items = connection.execute(
            "SELECT basis, output_id, original_output_id, status, error_code,"
            " delivery_id FROM obtain_items ORDER BY id"
        ).fetchall()
        # 唯一原片 501 被修复成品 503 替代：仅一条修复条目，预览不入选。
        assert items == [(2, 503, 501, 2, None, None)]
    finally:
        connection.close()


def test_fix_selection_legal_empty_selection(tmp_path: Path) -> None:
    """来源终态无产物：合法空选择固定并保存来源级无产物错误。"""
    _, owned, repository, selection_id = _prepared_selection(tmp_path)
    connection = owned.connection
    try:
        facts = load_selection_facts(connection, 21)
        assert facts.source_completed is True
        snapshot = select_outputs(
            _fixed_resolution((21,)), facts, SelectionMode.DEFAULT
        )
        assert snapshot.is_fixed is True
        assert snapshot.items == ()

        outcome = repository.fix_selection(
            FixSelection(
                selection_id=selection_id, snapshot=snapshot, occurred_at=_NOW
            ),
            new_operation_key(),
            owned,
        )
        assert outcome.kind is DbOutcomeKind.COMPLETED
        selection = _value(
            owned,
            "SELECT status, error_code, error_details_json"
            " FROM obtain_source_selections WHERE id = ?",
            selection_id,
        )
        assert selection[0] == 2
        assert selection[1] == 1  # no_outputs
        assert json.loads(selection[2]) == {}
        assert _value(owned, "SELECT COUNT(*) FROM obtain_items")[0] == 0
    finally:
        connection.close()


def test_fix_selection_partial_failure_saves_per_item_results(
    tmp_path: Path,
) -> None:
    """精确 ID：不存在、归属不匹配、已清理逐项失败，合法项继续。"""
    _, owned, repository, selection_id = _prepared_selection(tmp_path)
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    _seed_output(connection, 501, 21, 1, availability=3)  # 已清理
    _seed_output(connection, 502, 21, 1)
    _seed_output(connection, 702, 11, 1)  # 属于其他来源
    connection.commit()
    try:
        facts = load_selection_facts(connection, 21)
        snapshot = select_outputs(
            _fixed_resolution((21,)),
            facts,
            SelectionMode.EXPLICIT_IDS,
            requested_output_ids=(901, 702, 501, 502),
        )
        outcome = repository.fix_selection(
            FixSelection(
                selection_id=selection_id, snapshot=snapshot, occurred_at=_NOW
            ),
            new_operation_key(),
            owned,
        )
        assert outcome.kind is DbOutcomeKind.COMPLETED
        rows = connection.execute(
            "SELECT requested_output_id, output_id, status, error_code,"
            " error_details_json, delivery_id FROM obtain_items ORDER BY id"
        ).fetchall()
        assert tuple(row[0] for row in rows) == (901, 702, 501, 502)
        assert rows[0][2] == 4 and rows[0][3] == 1  # output_not_found
        assert json.loads(rows[0][4]) == {"requested_output_id": 901}
        assert rows[1][3] == 2  # output_source_mismatch
        assert rows[1][1] is None
        assert rows[2][3] == 3  # output_unavailable
        assert json.loads(rows[2][4]) == {"output_id": 501, "availability": "cleaned"}
        assert rows[3][2] == 2 and rows[3][1] == 502
        assert all(row[5] is None for row in rows)
    finally:
        connection.close()


def test_fixed_selection_survives_restart_without_reselecting(
    tmp_path: Path,
) -> None:
    """重启后：同键重送与再次固定都沿用已保存选择，不因新产物重选。"""
    target, owned, repository, selection_id = _prepared_selection(tmp_path)
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    _seed_output(connection, 501, 21, 1)
    connection.commit()
    facts = load_selection_facts(connection, 21)
    snapshot = select_outputs(_fixed_resolution((21,)), facts, SelectionMode.DEFAULT)
    key = new_operation_key()
    first = repository.fix_selection(
        FixSelection(selection_id=selection_id, snapshot=snapshot, occurred_at=_NOW),
        key,
        owned,
    )
    assert first.kind is DbOutcomeKind.COMPLETED

    # 新产物出现：不得进入已固定选择。
    connection.execute("BEGIN IMMEDIATE")
    _seed_output(connection, 509, 21, 1)
    connection.commit()
    connection.close()

    reopened = open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        saved = load_selection(reopened.connection, selection_id)
        assert saved.is_fixed is True
        assert saved.selected_output_ids == (501,)

        same_key = repository.fix_selection(
            FixSelection(
                selection_id=selection_id, snapshot=snapshot, occurred_at=_NOW
            ),
            key,
            reopened,
        )
        assert same_key.kind is DbOutcomeKind.COMPLETED
        assert same_key.value.snapshot.selected_output_ids == (501,)

        again = repository.fix_selection(
            FixSelection(
                selection_id=selection_id, snapshot=snapshot, occurred_at=_NOW
            ),
            new_operation_key(),
            reopened,
        )
        assert again.kind is DbOutcomeKind.COMPLETED
        assert again.value.snapshot.selected_output_ids == (501,)
        count = reopened.connection.execute(
            "SELECT COUNT(*) FROM obtain_items WHERE selection_id = ?",
            (selection_id,),
        ).fetchone()[0]
        assert count == 1
    finally:
        reopened.connection.close()


def test_saved_selection_missing_output_record_is_consistency_error(
    tmp_path: Path,
) -> None:
    """已保存条目引用的产物记录缺失：状态库矛盾，不解释为不存在。"""
    _, owned, repository, selection_id = _prepared_selection(tmp_path)
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    _seed_output(connection, 502, 21, 1)
    connection.commit()
    facts = load_selection_facts(connection, 21)
    snapshot = select_outputs(_fixed_resolution((21,)), facts, SelectionMode.DEFAULT)
    outcome = repository.fix_selection(
        FixSelection(selection_id=selection_id, snapshot=snapshot, occurred_at=_NOW),
        new_operation_key(),
        owned,
    )
    assert outcome.kind is DbOutcomeKind.COMPLETED
    try:
        # 模拟权威记录缺失：条目仍保留原身份。
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM outputs WHERE id = 502")
        connection.commit()
        try:
            load_selection(connection, selection_id)
        except ConsistencyError as error:
            assert "502" in str(error)
        else:
            raise AssertionError("记录缺失必须按状态库矛盾拒绝")
    finally:
        connection.close()


def test_fix_selection_rejects_unfinished_source(tmp_path: Path) -> None:
    """来源未终态：固定拒绝并回滚，不保存任何条目。"""
    _, owned, repository, selection_id = _prepared_selection(tmp_path)
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("UPDATE actions SET status = 2 WHERE id = 21")
    connection.commit()
    try:
        facts = load_selection_facts(connection, 21)
        assert facts.source_completed is False
        pending_snapshot = select_outputs(
            _fixed_resolution((21,)), facts, SelectionMode.DEFAULT
        )
        assert pending_snapshot.is_fixed is False
        # 用视作完成的替代事实隔离验证仓储的独立终态核对。
        fabricated = replace(facts, source_completed=True)
        terminal_snapshot = select_outputs(
            _fixed_resolution((21,)), fabricated, SelectionMode.DEFAULT
        )
        outcome = repository.fix_selection(
            FixSelection(
                selection_id=selection_id,
                snapshot=terminal_snapshot,
                occurred_at=_NOW,
            ),
            new_operation_key(),
            owned,
        )
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        status = _value(
            owned,
            "SELECT status FROM obtain_source_selections WHERE id = ?",
            selection_id,
        )
        assert status[0] == 1
        assert _value(owned, "SELECT COUNT(*) FROM obtain_items")[0] == 0
    finally:
        connection.close()

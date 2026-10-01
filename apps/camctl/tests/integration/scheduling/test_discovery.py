"""Q2 有界工作发现及缓存扩展的组件集成测试。

真实 SQLite 分页查询：未完成动作、未结束操作流程与已结束流程下
尚未结束的尝试分别发现；有限批量不漏终态后责任，缓存只保留有界
候选且不改变父对象事实。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from camctl.scheduling.discovery import (
    BoundedCandidateCache,
    DiscoveryCursor,
    discover_work,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..persistence.test_runtime import _create_valid_database

_NOW = 1_750_000_000_000_000


def _seed_environment(tmp_path: Path):
    target = tmp_path / "state.db"
    _create_valid_database(target)
    owned = open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    _seed_history_frame(connection)
    connection.commit()
    return owned


def _seed_history_frame(connection: sqlite3.Connection) -> None:
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
    status: int,
    scheduled_at: int | None,
    input_index: int = 0,
) -> None:
    connection.execute(
        "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
        " scheduled_at, group_name, input_fields_json, effective_params_json,"
        " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
        " cancel_requested, error_code, error_details_json, first_window_observed_at,"
        " expiration_reason, source_resolution_state, resolved_source_plan_id,"
        " target_selection_state, created_event_id, last_event_id, change_count)"
        " VALUES (?, ?, ?, ?, 1, 'cam-1', ?, NULL, '{}', '{\"shots\": 1}',"
        " 'camctl-adb', 1000, '{}', ?, ?, 0, NULL, NULL, NULL, NULL, NULL, NULL,"
        " NULL, 1, 1, 1)",
        (action_id, plan_id, input_index, f"action-{action_id}", scheduled_at, status,
         0 if status == 1 else 1),
    )


def _seed_run(
    connection: sqlite3.Connection,
    run_id: int,
    action_id: int,
    *,
    status: int,
) -> None:
    connection.execute(
        "INSERT INTO operation_runs (id, action_id, delivery_id, kind, query_purpose,"
        " responsibility_key, activity_id, copy_id, cleanup_item_id, session_key,"
        " status, attempts_used, max_attempts_used, timeout_s_json,"
        " retry_interval_s_json, retry_wait_required, error_json)"
        " VALUES (?, ?, NULL, 6, 1, ?, NULL, NULL, NULL, NULL, ?, 1, 2, '5', '1',"
        " 0, NULL)",
        (run_id, action_id, f"query/preflight/{action_id}", status),
    )


def _seed_attempt(
    connection: sqlite3.Connection,
    attempt_id: int,
    run_id: int,
    *,
    status: int,
) -> None:
    connection.execute(
        "INSERT INTO operation_attempts (id, run_id, attempt_no, copy_round, status,"
        " intent_event_id, result_event_id, max_attempts_used, timeout_s_json,"
        " retry_interval_s_json, effect_state, result_json, error_json)"
        " VALUES (?, ?, 1, NULL, ?, 1, CASE WHEN ? = 1 THEN NULL ELSE 1 END, 2, '5', '1', 1,"
        " CASE WHEN ? = 1 THEN NULL ELSE '{\"format_version\": 1}' END, NULL)",
        (attempt_id, run_id, status, status, status),
    )


def _commit_seed(owned) -> None:
    owned.connection.commit()


def test_terminal_action_does_not_hide_call(tmp_path: Path) -> None:
    """动作已终态而尝试仍在运行：尝试责任被独立发现。"""
    owned = _seed_environment(tmp_path)
    connection = owned.connection
    try:
        connection.execute("BEGIN IMMEDIATE")
        _seed_plan(connection, 1)
        _seed_action(connection, 1, 1, status=3, scheduled_at=_NOW)
        _seed_run(connection, 1, 1, status=3)
        _seed_attempt(connection, 1, 1, status=1)
        _commit_seed(owned)

        page = discover_work(connection, limit=100)
        assert page.pending_actions == ()
        assert page.open_runs == ()
        assert [item["id"] for item in page.running_attempts] == [1]
        assert page.running_attempts[0]["run_id"] == 1
        assert page.exhausted is True
    finally:
        owned.connection.close()


def test_open_run_and_pending_action_are_discovered(tmp_path: Path) -> None:
    owned = _seed_environment(tmp_path)
    connection = owned.connection
    try:
        connection.execute("BEGIN IMMEDIATE")
        _seed_plan(connection, 1)
        _seed_action(connection, 1, 1, status=1, scheduled_at=_NOW)
        _seed_action(connection, 2, 1, status=3, scheduled_at=_NOW, input_index=1)
        _seed_run(connection, 1, 2, status=2)
        _seed_run(connection, 2, 1, status=3)
        _seed_attempt(connection, 1, 1, status=1)
        _seed_attempt(connection, 2, 2, status=2)
        _commit_seed(owned)

        page = discover_work(connection, limit=100)
        assert [item["id"] for item in page.pending_actions] == [1]
        assert [item["id"] for item in page.open_runs] == [1]
        # 已结束尝试与未结束尝试分别归属：仅运行中的进入责任页。
        assert [item["id"] for item in page.running_attempts] == [1]
    finally:
        owned.connection.close()


def test_pages_reach_candidates_beyond_first_batch(tmp_path: Path) -> None:
    """第一页只有未来动作时继续翻页，后续页发现更早计划时间的候选。"""
    owned = _seed_environment(tmp_path)
    connection = owned.connection
    try:
        connection.execute("BEGIN IMMEDIATE")
        future = _NOW + 10_000_000
        for plan_id in range(1, 4):
            _seed_plan(connection, plan_id)
        # 计划 1—2 只有未来动作；计划 3 有已到点动作（计划时间更早）。
        _seed_action(connection, 1, 1, status=1, scheduled_at=future)
        _seed_action(connection, 2, 2, status=1, scheduled_at=future)
        _seed_action(connection, 3, 3, status=1, scheduled_at=_NOW)
        _commit_seed(owned)

        first = discover_work(connection, limit=2)
        assert [item["id"] for item in first.pending_actions] == [1, 2]
        assert first.exhausted is False
        second = discover_work(connection, first.next, limit=2)
        assert [item["id"] for item in second.pending_actions] == [3]
        assert second.pending_actions[0]["scheduled_at"] < future
        assert second.exhausted is True
    finally:
        owned.connection.close()


def test_cursor_positions_advance_independently(tmp_path: Path) -> None:
    """三路游标互不干扰：动作页耗尽后流程页仍能继续。"""
    owned = _seed_environment(tmp_path)
    connection = owned.connection
    try:
        connection.execute("BEGIN IMMEDIATE")
        _seed_plan(connection, 1)
        for action_id in (1, 2, 3):
            _seed_action(
                connection, action_id, 1, status=2, scheduled_at=_NOW,
                input_index=action_id,
            )
            _seed_run(connection, action_id, action_id, status=2)
        _commit_seed(owned)

        first = discover_work(connection, limit=1)
        assert [item["id"] for item in first.pending_actions] == [1]
        assert [item["id"] for item in first.open_runs] == [1]
        assert first.exhausted is False
        second = discover_work(connection, first.next, limit=1)
        assert [item["id"] for item in second.pending_actions] == [2]
        assert [item["id"] for item in second.open_runs] == [2]
        third = discover_work(connection, second.next, limit=1)
        assert [item["id"] for item in third.pending_actions] == [3]
        assert [item["id"] for item in third.open_runs] == [3]
        fourth = discover_work(connection, third.next, limit=1)
        assert fourth.open_runs == ()
        assert fourth.exhausted is True
    finally:
        owned.connection.close()


def test_unrelated_history_is_not_loaded_whole(tmp_path: Path) -> None:
    """无关已完成历史只按有限批量读取，不整库常驻。"""
    owned = _seed_environment(tmp_path)
    connection = owned.connection
    try:
        connection.execute("BEGIN IMMEDIATE")
        for plan_id in range(1, 41):
            _seed_plan(connection, plan_id)
            _seed_action(connection, plan_id, plan_id, status=3, scheduled_at=_NOW)
        _commit_seed(owned)

        cursor = DiscoveryCursor()
        pages = 0
        total = 0
        while True:
            page = discover_work(connection, cursor, limit=7)
            pages += 1
            total += len(page.pending_actions) + len(page.open_runs) + len(page.running_attempts)
            cursor = page.next
            if page.exhausted:
                break
            assert pages <= 40, "分页应有限收敛"
        assert total == 0
        assert pages <= 6
    finally:
        owned.connection.close()


def test_cache_is_bounded_and_keeps_latest(tmp_path: Path) -> None:
    """缓存淘汰最旧候选，保留近期批次；容量有界。"""
    cache = BoundedCandidateCache(capacity=3)
    cache.extend([{"id": 1}, {"id": 2}])
    cache.extend([{"id": 3}, {"id": 4}])
    assert [item["id"] for item in cache.snapshot()] == [2, 3, 4]
    cache.extend([{"id": 5}, {"id": 6}])
    assert [item["id"] for item in cache.snapshot()] == [4, 5, 6]
    with pytest.raises(ValueError):
        BoundedCandidateCache(capacity=0)


def test_new_submit_does_not_mutate_cached_parents(tmp_path: Path) -> None:
    """新计划提交不改变缓存中的父对象事实；再查询能发现新动作。"""
    owned = _seed_environment(tmp_path)
    connection = owned.connection
    try:
        connection.execute("BEGIN IMMEDIATE")
        _seed_plan(connection, 1)
        _seed_action(connection, 1, 1, status=1, scheduled_at=_NOW)
        _commit_seed(owned)

        page = discover_work(connection, limit=10)
        cache = BoundedCandidateCache(capacity=10)
        cache.extend(page.pending_actions)
        before = cache.snapshot()

        connection.execute("BEGIN IMMEDIATE")
        _seed_plan(connection, 2)
        _seed_action(connection, 2, 2, status=1, scheduled_at=_NOW)
        _commit_seed(owned)

        assert cache.snapshot() == before
        refreshed = discover_work(connection, DiscoveryCursor(), limit=10)
        assert [item["id"] for item in refreshed.pending_actions] == [1, 2]
    finally:
        owned.connection.close()

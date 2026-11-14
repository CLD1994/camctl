"""对象恢复的查询形状与增长测量。

任务 7 的开发环境成本记录：延迟与峰值不作为目标主机性能承诺，
结构性断言（查询形状的索引、有界读取、每页短事务）才是门禁。
"""

import os
import sqlite3
import time
import tracemalloc
from contextlib import closing
from decimal import Decimal

import pytest

from camctl.capture.files import FileObservationSave, FilePresenceSave
from camctl.contracts.values import new_operation_key
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.operations.attempts import (
    AttemptConfig,
    AttemptFinish,
    AttemptIntent,
    AttemptTarget,
    BeginDisposition,
    OperationKind,
    QueryPurpose,
)
from camctl.operations.models import (
    AttemptStatus,
    CallOutcome,
    EffectState,
    ErrorValue,
    EvidenceValue,
    Settlement,
    SettlementBasis,
)
from camctl.operations.validation import validate_outcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.history import HistoryRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.transactions import row_facts

from ..persistence.test_motor_transactions import NOW, owned
from .test_restore_paths import file_world
from ..support.history_rows import observe_readonly_rows


def _query_plan(connection: sqlite3.Connection, statement: str, parameters=()):
    rows = connection.execute(
        "EXPLAIN QUERY PLAN " + statement, parameters).fetchall()
    return " ".join(str(row[3]) for row in rows)


def test_representative_query_shapes_use_indexes(file_world):
    """手写的代表性查询形状按对象键使用索引。

    覆盖目录头与 H 位置、事件分页、快照候选及活动、流程、
    取回项和尝试的代表性成员查询；本项只解释这些手写 SQL，
    不证明实际恢复使用相同路径。
    """
    world = file_world
    connection = sqlite3.connect(world.history.path)
    try:
        connection.execute("ANALYZE")
        checks = {
            "对象目录头": (
                "SELECT event_id, change_count FROM entity_event_links"
                " WHERE entity_type = 9 AND entity_id = 1"
                " ORDER BY event_id DESC LIMIT 1", ()),
            "H 处目录位置": (
                "SELECT event_id, change_count FROM entity_event_links"
                " WHERE entity_type = 9 AND entity_id = 1 AND event_id <= 5"
                " ORDER BY event_id DESC LIMIT 1", ()),
            "事件分页": (
                "SELECT l.change_count, e.id FROM entity_event_links AS l"
                " LEFT JOIN history_events AS e ON e.id = l.event_id"
                " WHERE l.entity_type = 9 AND l.entity_id = 1 AND l.event_id > 0"
                " AND l.event_id <= 5 AND l.event_id < 5"
                " ORDER BY l.event_id DESC LIMIT 8", ()),
            "快照候选": (
                "SELECT id, boundary_event_id, change_count, format_version"
                " FROM entity_snapshots WHERE entity_type = 9 AND entity_id = 1"
                " AND boundary_event_id <= 5"
                " ORDER BY boundary_event_id DESC LIMIT 1", ()),
            "活动成员": (
                "SELECT id FROM device_activities WHERE action_id = 1 AND id > 0"
                " ORDER BY id LIMIT 128", ()),
            "流程成员分页": (
                "SELECT id FROM operation_runs WHERE action_id = 1 AND id > 0"
                " ORDER BY id LIMIT 128", ()),
            "取回项成员": (
                "SELECT id FROM obtain_items WHERE selection_id = 1 AND id > 0"
                " ORDER BY id LIMIT 128", ()),
            "尝试成员": (
                "SELECT id FROM operation_attempts WHERE run_id = 1 AND id > 0"
                " ORDER BY id LIMIT 128", ()),
        }
        for name, (statement, parameters) in checks.items():
            detail = _query_plan(connection, statement, parameters)
            assert " SCAN " not in f" {detail} ", f"{name}: {detail}"
    finally:
        connection.close()


def test_member_growth_restore_cost(file_world):
    """动作自身成员增长时的恢复成本与完整性。

    以真实重试链建立单流程多尝试成员；开发环境记录延迟，按需
    跟踪 Python 分配峰值，门禁是全部成员进入恢复结果（成员读取按稳定行身份
    分页，不预载完整清单）。
    """
    world = file_world
    operations = OperationRepository()
    contracts = EvidenceRegistry((
        EvidenceContract(type="query_failure_assumption", version=1,
                         operation="query", fields=frozenset()),))
    attempts = 240
    config = AttemptConfig(attempts + 1, Decimal("5"), Decimal("1"))
    ticket = None
    for index in range(attempts):
        intent = AttemptIntent(
            "query", 1, OperationKind.QUERY_ACTIVITY,
            AttemptTarget(), QueryPurpose.BEFORE_EXECUTION,
            config, NOW + 1 + index)
        begun = operations.begin_attempt(intent, new_operation_key(), world.owned)
        assert begun.kind is DbOutcomeKind.COMPLETED, begun.error
        assert begun.value.disposition is BeginDisposition.GRANTED
        ticket = begun.value.ticket
        failed = validate_outcome(ticket, CallOutcome(
            status=AttemptStatus.FAILED,
            error=ErrorValue(code="transport_timeout", stage="transport"),
            effect=EffectState.UNKNOWN,
            settlement=Settlement(
                basis=SettlementBasis.ASSUMED,
                evidence=EvidenceValue(
                    type="query_failure_assumption", version=1, data={})),
            observations=()), contracts)
        finish = operations.finish_attempt(
            AttemptFinish(ticket, failed, NOW + 1 + index, retry_wait=True),
            new_operation_key(), world.owned)
        assert finish.kind is DbOutcomeKind.COMPLETED, finish.error
    assert ticket is not None
    with closing(world.owned.connection.execute(
            "SELECT id FROM operation_attempts WHERE run_id = ?",
            (ticket.run_id,))) as cursor:
        grown_ids = {int(row[0]) for row in cursor.fetchall()}
    assert len(grown_ids) == attempts
    diagnostics = os.environ.get("CAMCTL_HISTORY_GROWTH_DIAGNOSTICS") == "1"
    if diagnostics:
        tracemalloc.start()
    started = time.perf_counter()
    try:
        rows = world.history.restore_entity("action", 1, _current_boundary(world))
        elapsed = time.perf_counter() - started
        peak = tracemalloc.get_traced_memory()[1] if diagnostics else None
    finally:
        if diagnostics:
            tracemalloc.stop()
    restored = sum(1 for (table, row_id) in rows
                   if table == "operation_attempts" and row_id in grown_ids)
    assert restored == attempts
    diagnostic = f"，跟踪峰值 {peak}B" if diagnostics else ""
    print(f"\n成员增长 {attempts} 尝试: 恢复 {len(rows)} 行 {elapsed:.3f}s{diagnostic}")


def test_event_growth_restore_pages_cost(file_world, monkeypatch):
    """单对象多页历史的恢复成本与每页短事务。

    开发环境记录延迟；门禁是小批量恢复到早期边界时每页事件在
    独立短读事务中读取，种子事务数量为常数。
    """
    world = file_world
    expected = row_facts(world.owned.connection, "device_files", 1)
    boundary = _current_boundary(world)
    batch = 8
    changes = 400
    for index in range(changes):
        _presence_change(world)
    event_ids = [row[0] for row in world.owned.connection.execute(
        "SELECT id FROM history_events WHERE id > ? ORDER BY id", (boundary.last_event_id,))]
    assert len(event_ids) == changes
    views = []
    active = {}

    def record(cursor, row):
        names = [column[0] for column in cursor.description]
        if "body_json" in names:
            assert names.count("id") == 1
            assert cursor.connection.in_transaction
            active.setdefault(cursor.connection, []).append(row[names.index("id")])

    def end_view(connection):
        assert not connection.in_transaction
        views.append(active.pop(connection, []))

    opened = observe_readonly_rows(monkeypatch, world.history.path, record, end_view)
    diagnostics = os.environ.get("CAMCTL_HISTORY_GROWTH_DIAGNOSTICS") == "1"
    if diagnostics:
        tracemalloc.start()
    started = time.perf_counter()
    try:
        rows = world.history.restore_entity(
            "device_file", 1, boundary, event_batch_size=batch)
        elapsed = time.perf_counter() - started
        peak = tracemalloc.get_traced_memory()[1] if diagnostics else None
    finally:
        if diagnostics:
            tracemalloc.stop()
    assert rows == {("device_files", 1): expected}
    pages = -(-changes // batch)
    # 种子一个读事务加每页一个；事件页全部读取自身目录，无空页。
    assert opened and not active and len(views) == 1 + pages
    assert views[0] == []
    assert all(len(view) == batch for view in views[1:])
    assert [event for view in views[1:] for event in view] == list(reversed(event_ids))
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
    diagnostic = f"，跟踪峰值 {peak}B" if diagnostics else ""
    print(f"\n事件增长 {changes} 次: 恢复 {elapsed:.3f}s{diagnostic}，读事务 {len(views)}")


def test_unrelated_growth_keeps_target_restore_stable(file_world, monkeypatch):
    """无关对象历史增长不改变目标对象的恢复结果。

    开发环境记录增长前后的恢复延迟；本项核对无关增长前后结果
    一致，代表性手写查询形状由前面的用例单独核对。
    """
    world = file_world
    _presence_change(world)
    target = row_facts(world.owned.connection, "device_files", 1)
    boundary = _current_boundary(world)

    started = time.perf_counter()
    baseline = world.history.restore_entity("device_file", 1, boundary)
    baseline_elapsed = time.perf_counter() - started

    unrelated = 1500
    for index in range(unrelated):
        outcome = world.capture.save_file_observation(
            FileObservationSave(
                1, f"unrelated-{index}", {"entry": index}, NOW + index + 1),
            new_operation_key(), world.owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    with closing(world.owned.connection.execute(
            "SELECT COUNT(*) FROM history_events")) as cursor:
        events = int(cursor.fetchone()[0])

    started = time.perf_counter()
    grown = world.history.restore_entity("device_file", 1, boundary)
    grown_elapsed = time.perf_counter() - started
    assert grown == baseline == {("device_files", 1): target}
    print(f"\n无关增长 {unrelated} 行（全局事件 {events}）:"
          f" 目标恢复 {baseline_elapsed:.3f}s → {grown_elapsed:.3f}s")


def _current_boundary(world):
    return world.history.current_boundary()


def _presence_change(world):
    current = row_facts(world.owned.connection, "device_files", 1)["presence_state"]
    outcome = world.capture.save_file_presence(
        FilePresenceSave(1, 3 if current == 2 else 2, NOW),
        new_operation_key(), world.owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error

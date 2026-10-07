"""P6 缓存与查询成本的组件集成测试：代表性负载下的索引使用与
不可变内容缓存的同一性。

真实状态库种代表性规模历史后执行 ANALYZE，捕获仓储真实执行的
每条查询并以 EXPLAIN QUERY PLAN 核实历史表读取走索引定位而非
全表扫描。包级不变量缓存（枚举登记、事件登记、权威结构表名）返
回同一对象：缓存身份来自包资源而非某个状态数据库，未提交数据
不进入缓存。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from camctl.contracts.enums import load_registry as load_enum_registry
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.values import new_operation_key
from camctl.history.events import load_event_registry
from camctl.persistence.repositories.history import HistoryRepository
from camctl.persistence.runtime import expected_table_names
from camctl.persistence.transaction import commit_operation

from .test_repositories import _seed_transactions
from .test_transactions import _open, plan_guards

#: 历史读取链会触碰的体量表：这些表上出现 SCAN（全表或全索引扫
#: 描）代表读取量随库增长而失控，必须以索引定位。
_VOLUME_TABLES = (
    "history_events", "history_transactions", "entity_event_links",
    "report_entity_changes", "plans",
)

_SEED_TRANSACTION_COUNT = 30


class TestQueryPlanUnderRepresentativeLoad:
    def test_history_reads_locate_rows_through_indexes(
            self, tmp_path, plan_guards, monkeypatch) -> None:
        _seed_transactions(tmp_path, _SEED_TRANSACTION_COUNT)
        with _open(tmp_path).connection as connection:
            connection.execute("ANALYZE")

        repository = HistoryRepository(tmp_path / "state.db")
        captured: list[str] = []
        original_connect = repository._connect

        def traced_connect() -> sqlite3.Connection:
            connection = original_connect()

            def record(statement: str) -> None:
                if statement.lstrip().upper().startswith("SELECT"):
                    captured.append(statement)

            connection.set_trace_callback(record)
            return connection

        monkeypatch.setattr(repository, "_connect", traced_connect)

        boundary = repository.current_boundary()
        repository.read_events(
            _ascending_scope(boundary.last_event_id, batch_limit=8), boundary)
        repository.select_report_entities(
            entity_type=4, from_wm=0, to_wm=boundary.last_event_id * 10,
            after_id=0, limit=5)
        repository.restore_entity("plan", 1, HistoryBoundary(1, 1))

        assert captured, "未捕获任何查询语句，trace 未生效"
        scans: list[str] = []
        explain_connection = sqlite3.connect(f"file:{tmp_path / 'state.db'}?mode=ro", uri=True)
        try:
            for statement in captured:
                plan = explain_connection.execute(
                    f"EXPLAIN QUERY PLAN {statement}").fetchall()
                for row in plan:
                    detail = str(row[3])
                    for table in _VOLUME_TABLES:
                        if detail.startswith("SCAN") and table in detail:
                            scans.append(f"{detail} <- {statement[:120]}")
        finally:
            explain_connection.close()
        assert scans == [], f"体量表出现全表扫描: {scans}"


class TestImmutableContentCache:
    def test_package_invariant_cache_reuses_the_same_object(self) -> None:
        """包资源不变量的进程内缓存返回同一对象，与数据库无关。

        缓存身份来自随包分发的登记资源，不依赖任何状态数据库；
        同一进程内重复加载必须命中同一份不可变内容。
        """
        enums = load_enum_registry()
        assert load_enum_registry() is enums
        events = load_event_registry()
        assert load_event_registry() is events
        tables = expected_table_names()
        assert expected_table_names() is tables
        assert isinstance(tables, frozenset) and tables


def _ascending_scope(upper: int, *, batch_limit: int):
    from camctl.contracts.history_values import ReadOrder, ReadScope

    return ReadScope(
        order=ReadOrder.ASCENDING, previous_position=None,
        lower_position=1, upper_position=upper,
        batch_limit=batch_limit, cursor_position=lambda cursor: int(cursor),
    )

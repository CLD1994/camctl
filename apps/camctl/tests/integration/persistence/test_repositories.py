"""P5 仓储批处理的组件集成测试：批分离、错误语义与提交路径审计。

真实状态库与替身命令组合：全局事件批返回后读连接已归还——新读
事务可见新提交、固定 H 批次不被后续写入污染；读取错误按一致性
错误传播而不是空批；仓储目录静态审计没有自提交函数——写提交统
一经事务内核，事务控制语句只属于读事务生命周期与快照维护窄仓
储的执行函数。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import camctl
from camctl.contracts.history_values import HistoryBoundary, ReadOrder, ReadScope
from camctl.contracts.pages import Page
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.persistence.repositories.history import HistoryRepository
from camctl.persistence.transaction import commit_operation

from .test_transactions import PlanCreateCommand, _open, plan_guards
from .test_runtime import _create_valid_database

_REPOSITORIES = Path(camctl.__file__).resolve().parent / "persistence" / "repositories"

#: 事务控制字面量允许的直接责任者：读事务生命周期与快照维护窄
#: 仓储经数据库执行器执行的函数。其他仓储自开写事务属于违规。
_ALLOWED_TRANSACTION_OWNERS = {"HistoryRepository", "SqliteSnapshotStore"}


def _seed_transactions(tmp_path: Path, count: int) -> None:
    """用替身命令以每事务一个计划创建事件的方式种若干完整事务。"""
    _create_valid_database(tmp_path / "state.db")
    owned = _open(tmp_path)
    try:
        for plan_id in range(1, count + 1):
            receipt = commit_operation(
                PlanCreateCommand((plan_id,)), new_operation_key(), owned
            )
            assert receipt.kind == "completed"
    finally:
        owned.connection.close()


def _scope(previous: int | None, limit: int) -> ReadScope[int]:
    return ReadScope(
        order=ReadOrder.ASCENDING, previous_position=previous,
        lower_position=1, upper_position=None,
        batch_limit=limit, cursor_position=lambda cursor: int(cursor),
    )


class TestDetachedBatch:
    def test_batch_is_detached_and_read_connection_is_released(
            self, tmp_path, plan_guards) -> None:
        _seed_transactions(tmp_path, 3)
        repository = HistoryRepository(tmp_path / "state.db")
        frozen = repository.current_boundary()
        assert frozen == HistoryBoundary(3, 3)

        first = repository.read_events(_scope(None, 2), frozen)
        assert isinstance(first, Page)
        assert [event.event_id for event in first.items] == [1, 2]
        assert first.next_cursor == 2

        # 第一批返回后读连接必须已归还：新读事务立即可见新提交；
        # 若 read_events 跨调用持有读事务，快照会遮住第 4 个事务。
        owned = _open(tmp_path)
        try:
            receipt = commit_operation(
                PlanCreateCommand((4,)), new_operation_key(), owned
            )
            assert receipt.kind == "completed"
        finally:
            owned.connection.close()
        assert repository.current_boundary() == HistoryBoundary(4, 4)

        # 固定 H 的续批只含冻结范围内的剩余事件并就此耗尽。
        second = repository.read_events(_scope(first.next_cursor, 2), frozen)
        assert [event.event_id for event in second.items] == [3]
        assert second.exhausted

        # 批数据独立拥有：全部后续读写完成后，第一批内容不变且可
        # 完整访问；批次集合是不可变元组，读取方不能修改它。
        snapshot = tuple(
            (event.event_id, event.transaction_id, event.event_type,
             tuple(change.table for change in event.rows))
            for event in first.items
        )
        assert isinstance(first.items, tuple)
        assert snapshot == (
            (1, 1, 1, ("plans",)),
            (2, 2, 1, ("plans",)),
        )

    def test_read_error_is_propagated_not_an_empty_batch(
            self, tmp_path, plan_guards) -> None:
        _seed_transactions(tmp_path, 1)
        repository = HistoryRepository(tmp_path / "state.db")
        # 伪造的 H 不是事务 1 的完整结束位置：必须按一致性错误传
        # 播，不得静默返回空批或部分批。
        false_boundary = HistoryBoundary(1, 2)
        with pytest.raises(ConsistencyError):
            repository.read_events(_scope(None, 10), false_boundary)


class TestCommitPathAudit:
    def test_repositories_commit_only_through_the_transaction_kernel(self) -> None:
        """静态审计：仓储目录没有自提交函数或直连数据库。

        写提交统一经事务内核；``.commit()`` 方法调用与
        ``sqlite3.connect`` 直连都不得出现。事务控制字面量只允许
        出现在白名单责任者的函数内。
        """
        commit_calls: list[str] = []
        direct_connections: list[str] = []
        transaction_owners: set[str] = set()

        for path in sorted(_REPOSITORIES.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr == "commit":
                    commit_calls.append(f"{path.name}:{node.lineno}")
                if (node.func.attr == "connect"
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "sqlite3"):
                    direct_connections.append(f"{path.name}:{node.lineno}")
                if (node.func.attr == "execute" and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and node.args[0].value in (
                            "BEGIN", "BEGIN IMMEDIATE", "COMMIT", "ROLLBACK")):
                    transaction_owners.add(_statement_owner(tree, node))

        assert commit_calls == [], f"仓储绕过事务内核自提交: {commit_calls}"
        assert direct_connections == [], f"仓储直连数据库: {direct_connections}"
        assert transaction_owners <= _ALLOWED_TRANSACTION_OWNERS, (
            "仓储目录出现白名单外的事务控制: "
            f"{sorted(transaction_owners - _ALLOWED_TRANSACTION_OWNERS)}"
        )


def _statement_owner(tree: ast.Module, node: ast.AST) -> str:
    """定位事务控制语句的直接责任者：最外层所属函数的类名或模块。"""
    for container in ast.walk(tree):
        if not isinstance(container, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node in ast.walk(container):
            name = _owning_class(tree, container)
            return name if name else f"module:{container.name}"
    return "module"


def _owning_class(tree: ast.Module, func: ast.AST) -> str | None:
    for scope in ast.walk(tree):
        if isinstance(scope, ast.ClassDef) and func in ast.walk(scope):
            return scope.name
    return None

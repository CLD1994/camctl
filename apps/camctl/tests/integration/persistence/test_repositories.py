"""P5 仓储批处理的组件集成测试：批分离、错误语义与提交路径审计。

真实状态库与替身命令组合：全局事件批返回后读连接已归还——新读
事务可见新提交、固定 H 批次不被后续写入污染；读取错误按一致性
错误传播而不是空批；仓储目录静态审计禁止 sqlite3 直连，并限制
事务控制语句的责任者。提交与回滚的实际行为由事务测试验证。
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

from .test_transactions import PlanCreateCommand, _open, plan_guards
from .test_runtime import _create_valid_database

_REPOSITORIES = Path(camctl.__file__).resolve().parent / "persistence" / "repositories"

#: 事务控制字面量允许的直接责任者：读事务生命周期与快照维护窄
#: 仓储经数据库执行器执行的函数。其他仓储自开写事务属于违规。
_ALLOWED_TRANSACTION_OWNERS = {"HistoryRepository", "SqliteSnapshotStore"}

# 电机事实读取，以及电机、主机拍摄和分段的原键核实，只建立一致读视图。
# 许可限定到函数，不允许仓储的其他入口自开事务，也不允许BEGIN IMMEDIATE。
_READ_TRANSACTION_FUNCTIONS = {
    "module:read_motor_facts",
    "module:_load_fixed_file",
    "module:verify_segment_finish",
    "module:read_segment_members",
    "module:read_segment_finish_candidates",
    "module:read_segment_finish_scope",
    "module:segment_output_candidate",
    "module:read_result_file_input_from_reader",
    "module:read_pairing_fact",
    "module:verify_ownership_from_result_page",
    "module:verify_result_file_pairing",
    "module:_read_transaction",
    "MotorRepository.verify_operation",
    "CancellationRepository.verify_motor_operation",
    "HostCaptureRepository.verify_operation",
}


def _seed_transactions(tmp_path: Path, count: int, plan_guards) -> None:
    """用替身命令以每事务一个计划创建事件的方式种若干完整事务。"""
    _create_valid_database(tmp_path / "state.db")
    owned = _open(tmp_path)
    try:
        for plan_id in range(1, count + 1):
            receipt = plan_guards.commit_operation(
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
        _seed_transactions(tmp_path, 3, plan_guards)
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
            receipt = plan_guards.commit_operation(
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
        _seed_transactions(tmp_path, 1, plan_guards)
        repository = HistoryRepository(tmp_path / "state.db")
        # 伪造的 H 不是事务 1 的完整结束位置：必须按一致性错误传
        # 播，不得静默返回空批或部分批。
        false_boundary = HistoryBoundary(1, 2)
        with pytest.raises(ConsistencyError):
            repository.read_events(_scope(None, 10), false_boundary)


class TestCommitPathAudit:
    def test_repositories_restrict_direct_connections_and_transaction_sql(self) -> None:
        """静态检查 sqlite3 直连、显式连接提交及事务控制语句。

        只读核实事务按函数级白名单登记；其余仓储不得使用这些
        字面量事务控制语句。本检查不推断连接别名与动态调用；
        事务测试验证实际提交与回滚行为。
        """
        direct_connections: list[str] = []
        connection_commits: list[str] = []
        transaction_owners: set[str] = set()

        for path in sorted(_REPOSITORIES.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)):
                    continue
                if (node.func.attr == "commit"
                        and isinstance(node.func.value, ast.Attribute)
                        and node.func.value.attr == "connection"):
                    connection_commits.append(f"{path.name}:{node.lineno}")
                if (node.func.attr == "connect"
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "sqlite3"):
                    direct_connections.append(f"{path.name}:{node.lineno}")
                if (node.func.attr == "execute" and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and node.args[0].value in (
                            "BEGIN", "BEGIN IMMEDIATE", "COMMIT", "ROLLBACK")):
                    owner = _statement_owner(tree, node)
                    if owner in _READ_TRANSACTION_FUNCTIONS:
                        assert node.args[0].value != "BEGIN IMMEDIATE", (
                            f"只读核实入口自开写事务: {path.name}:{node.lineno}"
                        )
                    else:
                        transaction_owners.add(owner.split(".")[0])

        assert direct_connections == [], f"仓储直连数据库: {direct_connections}"
        assert connection_commits == [], f"仓储直接提交连接: {connection_commits}"
        assert transaction_owners <= _ALLOWED_TRANSACTION_OWNERS, (
            "仓储目录出现白名单外的事务控制: "
            f"{sorted(transaction_owners - _ALLOWED_TRANSACTION_OWNERS)}"
        )


def _statement_owner(tree: ast.Module, node: ast.AST) -> str:
    """定位事务控制语句的最外层所属函数，保留类名与方法名。"""
    for container in ast.walk(tree):
        if not isinstance(container, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node in ast.walk(container):
            name = _owning_class(tree, container)
            return f"{name}.{container.name}" if name else f"module:{container.name}"
    return "module"


def _owning_class(tree: ast.Module, func: ast.AST) -> str | None:
    for scope in ast.walk(tree):
        if isinstance(scope, ast.ClassDef) and func in ast.walk(scope):
            return scope.name
    return None


@pytest.mark.parametrize('owner', sorted(_READ_TRANSACTION_FUNCTIONS))
@pytest.mark.parametrize('statement', ['BEGIN', 'ROLLBACK', 'BEGIN IMMEDIATE', 'COMMIT'])
def test_read_transaction_allowance_rejects_write_lock(tmp_path, monkeypatch, owner, statement):
    import sys
    if owner.startswith('module:'):
        source = f'def {owner.split(":", 1)[1]}():\n    connection.execute({statement!r})\n'
    else:
        cls, function = owner.split('.')
        source = f'class {cls}:\n    def {function}(self):\n        connection.execute({statement!r})\n'
    (tmp_path / 'repository.py').write_text(source, encoding='utf-8')
    monkeypatch.setattr(sys.modules[__name__], '_REPOSITORIES', tmp_path)
    audit = TestCommitPathAudit().test_repositories_restrict_direct_connections_and_transaction_sql
    if statement in ('BEGIN', 'ROLLBACK', 'COMMIT'):
        audit()
    else:
        with pytest.raises(AssertionError):
            audit()


def test_repository_connection_commit_is_rejected_as_direct_connection(tmp_path, monkeypatch):
    import sys

    source = "import sqlite3\ndef bypass():\n    sqlite3.connect('state.db').commit()\n"
    (tmp_path / "repository.py").write_text(source, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "_REPOSITORIES", tmp_path)
    audit = TestCommitPathAudit().test_repositories_restrict_direct_connections_and_transaction_sql
    with pytest.raises(AssertionError, match="仓储直连数据库"):
        audit()


@pytest.mark.parametrize("source,accepted", [
    ("def save(owned):\n    owned.connection.commit()\n", False),
    ("def save(atomic_transaction):\n    atomic_transaction.commit()\n", True),
])
def test_repository_commit_audit_checks_explicit_connection(tmp_path, monkeypatch, source, accepted):
    import sys

    (tmp_path / "repository.py").write_text(source, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "_REPOSITORIES", tmp_path)
    audit = TestCommitPathAudit().test_repositories_restrict_direct_connections_and_transaction_sql
    if accepted:
        audit()
    else:
        with pytest.raises(AssertionError, match="仓储直接提交连接"):
            audit()

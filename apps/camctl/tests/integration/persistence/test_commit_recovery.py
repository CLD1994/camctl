"""P4 提交未知身份核实的组件集成测试。

真实 SQLite 与 P3 事务组合：已提交操作按 operation_key 复用原
结果；失效连接停用后的可靠不存在允许检查原资格；原连接仍在途
时不能重做。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from camctl.contracts.values import new_operation_key
from camctl.persistence.recovery import (
    CommitFinding,
    CommitFindingKind,
    OperationIdentity,
    RecoveryKind,
    resolve_commit,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import commit_operation

from ..persistence.test_runtime import _create_valid_database
from .test_transactions import PlanCreateCommand, plan_guards


class SqliteCommitLookup:
    """在新连接上按 operation_key 核实受理事务的查询端口实现。

    late_commit_possible 由调用方按原连接是否已停用提供：核实查
    询本身无法从数据库推断迟到提交的可能性。
    """

    def __init__(self, path: Path, *, late_commit_possible: bool, identity_stage: str = "accept") -> None:
        self._path = path
        self._late_commit_possible = late_commit_possible
        self._identity_stage = identity_stage

    def lookup(self, key):
        try:
            connection = sqlite3.connect(self._path)
            try:
                row = connection.execute(
                    "SELECT t.id FROM history_transactions t WHERE t.operation_key = ?",
                    (str(key),),
                ).fetchone()
                if row is not None:
                    txn_id = int(row[0])
                    plan = connection.execute(
                        "SELECT request_id FROM plans ORDER BY id LIMIT 1"
                    ).fetchone()
                    return CommitFinding(
                        kind=CommitFindingKind.PRESENT,
                        value={"txn_id": txn_id},
                        identity=OperationIdentity(
                            inputs={"request_id": int(plan[0]) if plan else None},
                            stage=self._identity_stage,
                            target="plans",
                        ),
                    )
            finally:
                connection.close()
        except sqlite3.Error as error:
            return CommitFinding(kind=CommitFindingKind.LOOKUP_FAILED, error=error)
        if self._late_commit_possible:
            return CommitFinding(kind=CommitFindingKind.IN_FLIGHT)
        return CommitFinding(kind=CommitFindingKind.ABSENT)


class TestRealVerification:
    def test_committed_operation_is_reused_by_key(self, tmp_path, plan_guards) -> None:
        _create_valid_database(tmp_path / "state.db")
        owned = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
        key = new_operation_key()
        try:
            receipt = commit_operation(PlanCreateCommand((1,)), key, owned)
            assert receipt.kind == "completed"
        finally:
            owned.connection.close()
        # 提交结果未知后的核实：在新可靠连接上查询。
        lookup = SqliteCommitLookup(tmp_path / "state.db", late_commit_possible=False)
        decision = resolve_commit(
            key,
            OperationIdentity(inputs={"request_id": 101}, stage="accept", target="plans"),
            lookup,
        )
        assert decision.kind is RecoveryKind.REUSE
        assert decision.value == {"txn_id": 1}

    def test_same_key_with_different_inputs_is_rejected(self, tmp_path, plan_guards) -> None:
        _create_valid_database(tmp_path / "state.db")
        owned = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
        key = new_operation_key()
        try:
            assert commit_operation(PlanCreateCommand((1,)), key, owned).kind == "completed"
        finally:
            owned.connection.close()
        lookup = SqliteCommitLookup(tmp_path / "state.db", late_commit_possible=False)
        decision = resolve_commit(
            key,
            OperationIdentity(inputs={"request_id": 999}, stage="accept", target="plans"),
            lookup,
        )
        assert decision.kind is RecoveryKind.ERROR

    def test_absent_after_connection_stopped_allows_eligibility_check(
        self, tmp_path, plan_guards
    ) -> None:
        _create_valid_database(tmp_path / "state.db")
        owned = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
        owned.connection.close()
        # 原连接已停用：迟到提交不可能，可靠不存在允许检查原资格。
        lookup = SqliteCommitLookup(tmp_path / "state.db", late_commit_possible=False)
        decision = resolve_commit(
            new_operation_key(),
            OperationIdentity(inputs={"request_id": 1}, stage="accept", target="plans"),
            lookup,
        )
        assert decision.kind is RecoveryKind.RETRY
        assert decision.can_retry is True

    def test_original_connection_still_alive_blocks_retry(self, tmp_path, plan_guards) -> None:
        _create_valid_database(tmp_path / "state.db")
        owned = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
        try:
            # 原连接仍在（可能在途提交）：查询为空也不能重做。
            lookup = SqliteCommitLookup(tmp_path / "state.db", late_commit_possible=True)
            decision = resolve_commit(
                new_operation_key(),
                OperationIdentity(inputs={"request_id": 1}, stage="accept", target="plans"),
                lookup,
            )
            assert decision.kind is RecoveryKind.WAIT
            assert decision.can_retry is False
        finally:
            owned.connection.close()

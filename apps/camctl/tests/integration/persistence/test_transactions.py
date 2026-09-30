"""P3 完整事件事务的组件集成测试。

真实 SQLite 与事务内核组合：编号范围、H1/H2 校验派生、整组提交
与逐阶段回滚。业务守卫以测试替身注册，正式守卫随受理任务接入。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import new_operation_key
from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.history.validators import register_guard
from camctl.persistence.runtime import DbConfig, DbOpenMode, OwnedConnection, open_existing
from camctl.persistence.transaction import commit_operation

from .test_runtime import _create_valid_database

_CREATED_AT = 1_700_000_000_000_000


@pytest.fixture()
def plan_guards():
    """注册计划事件所需的业务守卫替身；测试后恢复原状。"""
    from camctl.history import validators

    saved = {}
    for name in ("admission", "plan_aggregate"):
        saved[name] = validators.NAMED_GUARDS.get(name)
        register_guard(name, lambda event, context: None)
    yield
    for name, guard in saved.items():
        if guard is None:
            validators.NAMED_GUARDS.pop(name, None)
        else:
            validators.NAMED_GUARDS[name] = guard


def _open(tmp_path: Path) -> OwnedConnection:
    return open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())


def _plan_create_envelope(event_id: int, txn_id: int, plan_id: int, request_id: int) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        transaction_id=txn_id,
        event_type=1,
        event_version=1,
        occurred_at=_CREATED_AT,
        clock_status=2,
        change_seq=None,
        reason=1,
        evidence={},
        rows=(
            RowChange(
                table="plans",
                row_id=plan_id,
                before=RowImage(exists=False, values={}),
                after=RowImage(
                    exists=True,
                    values={
                        "request_id": request_id,
                        "name": f"plan-{request_id}",
                        "created_at": _CREATED_AT,
                        "status": 1,
                    },
                ),
            ),
        ),
    )


def _plan_start_envelope(event_id: int, txn_id: int, plan_id: int, before_status: int, after_status: int = 2) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        transaction_id=txn_id,
        event_type=9,
        event_version=1,
        occurred_at=_CREATED_AT + 1,
        clock_status=2,
        change_seq=None,
        reason=1 if after_status == 2 else 2,
        evidence={},
        rows=(
            RowChange(
                table="plans",
                row_id=plan_id,
                before=RowImage(exists=True, values={"status": before_status}),
                after=RowImage(exists=True, values={"status": after_status}),
            ),
        ),
    )


class PlanCreateCommand:
    """创建一个或多个计划的命令（测试替身，受接口契约约束）。"""

    def __init__(self, plan_ids: tuple[int, ...]) -> None:
        self.plan_ids = plan_ids

    def plan(self, scope):
        from camctl.persistence.transaction import CommandPlan

        allocation = scope.allocate(len(self.plan_ids))
        events = tuple(
            _plan_create_envelope(
                allocation.first_event_id + index,
                allocation.txn_id,
                plan_id,
                100 + plan_id,
            )
            for index, plan_id in enumerate(self.plan_ids)
        )
        owners = {("plans", plan_id): ("plan", plan_id) for plan_id in self.plan_ids}
        return CommandPlan(
            events=events,
            owners=owners,
            state_rows={"plans": {}},
            result={"created": len(self.plan_ids)},
        )


class PlanStartCommand:
    """把指定计划推进到下一状态的命令（before 由调用方给定）。"""

    def __init__(self, plan_id: int, before_status: int, after_status: int = 2, current_rows=None) -> None:
        self.plan_id = plan_id
        self.before_status = before_status
        self.after_status = after_status
        self.current_rows = current_rows

    def plan(self, scope):
        from camctl.persistence.transaction import CommandPlan

        allocation = scope.allocate(1)
        event = _plan_start_envelope(
            allocation.first_event_id, allocation.txn_id, self.plan_id,
            self.before_status, self.after_status,
        )
        return CommandPlan(
            events=(event,),
            owners={("plans", self.plan_id): ("plan", self.plan_id)},
            state_rows=self.current_rows if self.current_rows is not None else {"plans": {}},
            result={"started": self.plan_id},
        )


class FailingConnection:
    """在首个匹配前缀的语句上失败的连接替身；其余语句透传。"""

    def __init__(self, connection: sqlite3.Connection, fail_prefix: str) -> None:
        self._connection = connection
        self._fail_prefix = fail_prefix

    def execute(self, sql, parameters=()):
        if sql.startswith(self._fail_prefix):
            raise sqlite3.OperationalError(f"injected failure at: {sql[:40]}")
        return self._connection.execute(sql, parameters)


def _owned_with(connection) -> OwnedConnection:
    return OwnedConnection(connection=connection, metadata=None)


def _row(connection: sqlite3.Connection, sql: str, parameters=()):
    return connection.execute(sql, parameters).fetchone()


def _count(connection: sqlite3.Connection, table: str) -> int:
    return int(_row(connection, f"SELECT COUNT(*) FROM {table}")[0])


class TestCommittedTransaction:
    def test_committed_transaction_saves_everything_together(self, tmp_path, plan_guards) -> None:
        _create_valid_database(tmp_path / "state.db")
        owned = _open(tmp_path)
        connection = owned.connection
        try:
            key = new_operation_key()
            receipt = commit_operation(PlanCreateCommand((1,)), key, owned)
            assert receipt.kind == "completed"
            assert (receipt.boundary.txn_id, receipt.boundary.last_event_id) == (1, 1)
            assert receipt.result == {"created": 1}

            txn = _row(connection, "SELECT id, operation_key, first_event_id, last_event_id FROM history_transactions")
            assert txn == (1, key, 1, 1)
            event = _row(
                connection,
                "SELECT id, transaction_id, event_type, event_version, occurred_at, clock_status, change_seq"
                " FROM history_events",
            )
            assert event == (1, 1, 1, 1, _CREATED_AT, 2, 1)
            body = parse_exact_json(_row(connection, "SELECT body_json FROM history_events")[0])
            assert body["reason"] == 1
            assert body["rows"][0]["table"] == "plans"
            assert body["rows"][0]["id"] == 1
            assert body["rows"][0]["after"]["values"]["request_id"] == 101

            assert _row(connection, "SELECT entity_type, entity_id, event_id, change_count FROM entity_event_links") == (4, 1, 1, 1)
            assert _row(
                connection,
                "SELECT entity_type, entity_id, event_id, change_seq FROM report_entity_changes",
            ) == (4, 1, 1, 1)
            assert _row(
                connection,
                "SELECT entity_type, entity_id, current_change_count, snapshot_change_count, latest_snapshot_id"
                " FROM entity_snapshot_progress",
            ) == (4, 1, 1, 0, None)
            plan = _row(
                connection,
                "SELECT request_id, name, status, created_event_id, last_event_id, change_count FROM plans",
            )
            assert plan == (101, "plan-101", 1, 1, 1, 1)
        finally:
            connection.close()

    def test_multi_event_transaction_uses_continuous_span(self, tmp_path, plan_guards) -> None:
        _create_valid_database(tmp_path / "state.db")
        owned = _open(tmp_path)
        connection = owned.connection
        try:
            receipt = commit_operation(PlanCreateCommand((1, 2)), new_operation_key(), owned)
            assert receipt.kind == "completed"
            assert (receipt.boundary.txn_id, receipt.boundary.last_event_id) == (1, 2)
            assert _row(connection, "SELECT first_event_id, last_event_id FROM history_transactions") == (1, 2)
            events = connection.execute(
                "SELECT id, change_seq FROM history_events ORDER BY id"
            ).fetchall()
            assert events == [(1, 1), (2, 2)]
            links = connection.execute(
                "SELECT entity_id, event_id, change_count FROM entity_event_links ORDER BY event_id"
            ).fetchall()
            assert links == [(1, 1, 1), (2, 2, 1)]
            derived = connection.execute(
                "SELECT id, created_event_id, last_event_id, change_count FROM plans ORDER BY id"
            ).fetchall()
            assert derived == [(1, 1, 1, 1), (2, 2, 2, 1)]
        finally:
            connection.close()

    def test_second_transaction_allocates_after_committed_history(self, tmp_path, plan_guards) -> None:
        _create_valid_database(tmp_path / "state.db")
        owned = _open(tmp_path)
        connection = owned.connection
        try:
            first = commit_operation(PlanCreateCommand((1,)), new_operation_key(), owned)
            second = commit_operation(PlanCreateCommand((2,)), new_operation_key(), owned)
            assert (first.boundary.txn_id, first.boundary.last_event_id) == (1, 1)
            assert (second.boundary.txn_id, second.boundary.last_event_id) == (2, 2)
            assert _row(connection, "SELECT MAX(id) FROM history_events")[0] == 2
            assert _row(
                connection,
                "SELECT change_count FROM entity_event_links WHERE entity_id = 2",
            )[0] == 1
        finally:
            connection.close()


class TestCurrentStateRecheck:
    def test_write_uses_current_transaction_state(self, tmp_path, plan_guards) -> None:
        _create_valid_database(tmp_path / "state.db")
        owned = _open(tmp_path)
        connection = owned.connection
        try:
            # 事实基线：计划创建并推进到 RUNNING。
            assert commit_operation(PlanCreateCommand((1,)), new_operation_key(), owned).kind == "completed"
            assert commit_operation(
                PlanStartCommand(1, before_status=1), new_operation_key(), owned
            ).kind == "completed"
            # 竞争者持事务外旧状态（认为仍是 1）提交：内核按当前值拒绝。
            stale = commit_operation(
                PlanStartCommand(1, before_status=1), new_operation_key(), owned
            )
            assert stale.kind == "rolled_back"
            assert "旧值与当前状态不符" in str(stale.error)
            # 已提交的动作事实保留。
            assert _row(connection, "SELECT status FROM plans")[0] == 2

            # 重新读取当前状态后按原资格重做：成功且保留全部既有事实。
            fresh = commit_operation(
                PlanStartCommand(1, before_status=2, after_status=3), new_operation_key(), owned
            )
            assert fresh.kind == "completed"
            row = _row(connection, "SELECT request_id, name, status, change_count FROM plans")
            assert row == (101, "plan-101", 3, 3)
            # 回滚的事务不留任何分组或事件痕迹。
            assert _count(connection, "history_transactions") == 3
            assert _count(connection, "history_events") == 3
            assert _count(connection, "entity_event_links") == 3
        finally:
            connection.close()

    def test_create_then_update_in_one_transaction_allowed(self, tmp_path, plan_guards) -> None:
        _create_valid_database(tmp_path / "state.db")
        owned = _open(tmp_path)
        connection = owned.connection
        try:
            from camctl.persistence.transaction import CommandPlan

            class CreateThenStart:
                def plan(self, scope):
                    allocation = scope.allocate(2)
                    create = _plan_create_envelope(
                        allocation.first_event_id, allocation.txn_id, 7, 77
                    )
                    start = _plan_start_envelope(
                        allocation.first_event_id + 1, allocation.txn_id, 7, 1
                    )
                    return CommandPlan(
                        events=(create, start),
                        owners={("plans", 7): ("plan", 7)},
                        state_rows={"plans": {}},
                    )

            receipt = commit_operation(CreateThenStart(), new_operation_key(), owned)
            assert receipt.kind == "completed"
            row = _row(
                connection,
                "SELECT status, created_event_id, last_event_id, change_count FROM plans",
            )
            assert row == (2, 1, 2, 2)
        finally:
            connection.close()


class TestAllocationContract:
    def test_event_count_mismatch_rejected(self, tmp_path, plan_guards) -> None:
        from camctl.persistence.transaction import CommandPlan

        _create_valid_database(tmp_path / "state.db")
        owned = _open(tmp_path)
        connection = owned.connection
        try:
            class WrongCount:
                def plan(self, scope):
                    allocation = scope.allocate(2)
                    events = (
                        _plan_create_envelope(allocation.first_event_id, allocation.txn_id, 1, 41),
                        _plan_create_envelope(allocation.first_event_id + 1, allocation.txn_id, 2, 42),
                        _plan_create_envelope(allocation.first_event_id + 2, allocation.txn_id, 3, 43),
                    )
                    return CommandPlan(events=events, owners={}, state_rows={"plans": {}})

            receipt = commit_operation(WrongCount(), new_operation_key(), owned)
            assert receipt.kind == "rolled_back"
            assert "不符" in str(receipt.error)
            assert _count(connection, "history_events") == 0
        finally:
            connection.close()

    def test_non_adjacent_event_ids_rejected(self, tmp_path, plan_guards) -> None:
        from camctl.persistence.transaction import CommandPlan

        _create_valid_database(tmp_path / "state.db")
        owned = _open(tmp_path)
        connection = owned.connection
        try:
            class SkippedIds:
                def plan(self, scope):
                    allocation = scope.allocate(1)
                    event = _plan_create_envelope(
                        allocation.first_event_id + 5, allocation.txn_id, 1, 41
                    )
                    return CommandPlan(events=(event,), owners={("plans", 1): ("plan", 1)}, state_rows={"plans": {}})

            receipt = commit_operation(SkippedIds(), new_operation_key(), owned)
            assert receipt.kind == "rolled_back"
            assert _count(connection, "history_transactions") == 0
        finally:
            connection.close()

    def test_missing_allocation_rejected(self, tmp_path, plan_guards) -> None:
        from camctl.persistence.transaction import CommandPlan

        _create_valid_database(tmp_path / "state.db")
        owned = _open(tmp_path)
        connection = owned.connection
        try:
            class NoAllocation:
                def plan(self, scope):
                    return CommandPlan(
                        events=(_plan_create_envelope(1, 1, 1, 41),),
                        owners={("plans", 1): ("plan", 1)},
                        state_rows={"plans": {}},
                    )

            receipt = commit_operation(NoAllocation(), new_operation_key(), owned)
            assert receipt.kind == "rolled_back"
            assert "没有分配" in str(receipt.error)
        finally:
            connection.close()


class TestAtomicRollback:
    @pytest.mark.parametrize(
        "fail_prefix",
        [
            "INSERT INTO history_transactions",
            "INSERT INTO history_events",
            "INSERT INTO entity_event_links",
            "INSERT INTO report_entity_changes",
            "INSERT INTO entity_snapshot_progress",
        ],
    )
    def test_catalog_stage_failure_rolls_back_create(self, tmp_path, plan_guards, fail_prefix) -> None:
        _create_valid_database(tmp_path / "state.db")
        real = sqlite3.connect(tmp_path / "state.db")
        try:
            real.execute("PRAGMA journal_mode=wal")
            failing = _owned_with(FailingConnection(real, fail_prefix))
            receipt = commit_operation(PlanCreateCommand((1,)), new_operation_key(), failing)
            assert receipt.kind == "rolled_back"
            assert isinstance(receipt.error, sqlite3.OperationalError)
            check = sqlite3.connect(tmp_path / "state.db")
            try:
                for table in (
                    "history_transactions",
                    "history_events",
                    "entity_event_links",
                    "report_entity_changes",
                    "entity_snapshot_progress",
                    "plans",
                ):
                    assert _count(check, table) == 0, table
            finally:
                check.close()
        finally:
            real.close()

    @pytest.mark.parametrize(
        "fail_prefix",
        [
            "INSERT INTO plans",
            "UPDATE plans SET",
        ],
    )
    def test_projection_stage_failure_rolls_back(self, tmp_path, plan_guards, fail_prefix) -> None:
        _create_valid_database(tmp_path / "state.db")
        # 先提交一次创建，让更新路径分段（投影 UPDATE 与派生 UPDATE）可达。
        seed = _open(tmp_path)
        try:
            assert commit_operation(PlanCreateCommand((1,)), new_operation_key(), seed).kind == "completed"
        finally:
            seed.connection.close()
        real = sqlite3.connect(tmp_path / "state.db")
        try:
            real.execute("PRAGMA journal_mode=wal")
            failing = _owned_with(FailingConnection(real, fail_prefix))
            command = (
                PlanStartCommand(1, before_status=1)
                if fail_prefix.startswith("UPDATE")
                else PlanCreateCommand((2,))
            )
            receipt = commit_operation(command, new_operation_key(), failing)
            assert receipt.kind == "rolled_back"
            assert isinstance(receipt.error, sqlite3.OperationalError)
            check = sqlite3.connect(tmp_path / "state.db")
            try:
                # 既有事实保持；本次操作没有留下任何部分保存。
                assert _count(check, "history_transactions") == 1
                assert _count(check, "history_events") == 1
                assert _count(check, "entity_event_links") == 1
                assert _count(check, "report_entity_changes") == 1
                assert _count(check, "entity_snapshot_progress") == 1
                assert _count(check, "plans") == 1
                assert _row(check, "SELECT status, change_count FROM plans") == (1, 1)
            finally:
                check.close()
        finally:
            real.close()

    def test_commit_failure_is_unknown(self, tmp_path, plan_guards) -> None:
        _create_valid_database(tmp_path / "state.db")
        real = sqlite3.connect(tmp_path / "state.db")
        try:
            real.execute("PRAGMA journal_mode=wal")
            failing = _owned_with(FailingConnection(real, "COMMIT"))
            receipt = commit_operation(PlanCreateCommand((1,)), new_operation_key(), failing)
            assert receipt.kind == "unknown"
            assert isinstance(receipt.error, sqlite3.OperationalError)
        finally:
            real.close()

    def test_registry_violation_rejected_even_if_sql_would_accept(self, tmp_path, plan_guards) -> None:
        # 状态模型不允许 1→3 的“首次开始”；内核拒绝提交。
        from camctl.persistence.transaction import CommandPlan

        _create_valid_database(tmp_path / "state.db")
        owned = _open(tmp_path)
        connection = owned.connection
        try:
            assert commit_operation(PlanCreateCommand((1,)), new_operation_key(), owned).kind == "completed"

            class IllegalTransition:
                def plan(self, scope):
                    allocation = scope.allocate(1)
                    event = EventEnvelope(
                        event_id=allocation.first_event_id,
                        transaction_id=allocation.txn_id,
                        event_type=9,
                        event_version=1,
                        occurred_at=_CREATED_AT,
                        clock_status=2,
                        change_seq=None,
                        reason=1,
                        evidence={},
                        rows=(
                            RowChange(
                                table="plans",
                                row_id=1,
                                before=RowImage(exists=True, values={"status": 1}),
                                after=RowImage(exists=True, values={"status": 3}),
                            ),
                        ),
                    )
                    return CommandPlan(
                        events=(event,),
                        owners={("plans", 1): ("plan", 1)},
                        state_rows={"plans": {}},
                    )

            receipt = commit_operation(IllegalTransition(), new_operation_key(), owned)
            assert receipt.kind == "rolled_back"
            assert _row(connection, "SELECT status FROM plans")[0] == 1
            assert _count(connection, "history_events") == 1
        finally:
            connection.close()

    def test_unimplemented_business_guard_rejects_write(self, tmp_path) -> None:
        # 受理守卫已随 A4 正式注册；用文件事件展示未接入守卫（F 系列）
        # 的明确拒绝：即使行内 SQL 可以接受也不能提交。
        from camctl.persistence.transaction import CommandPlan

        _create_valid_database(tmp_path / "state.db")
        owned = _open(tmp_path)
        connection = owned.connection
        try:
            class FileCompleteCommand:
                def plan(self, scope):
                    allocation = scope.allocate(1)
                    event = EventEnvelope(
                        event_id=allocation.first_event_id,
                        transaction_id=allocation.txn_id,
                        event_type=17,
                        event_version=1,
                        occurred_at=_CREATED_AT,
                        clock_status=2,
                        change_seq=None,
                        reason=3,
                        evidence={},
                        rows=(
                            RowChange(
                                table="device_files",
                                row_id=3,
                                before=RowImage(
                                    exists=True,
                                    values={
                                        "completion_state": 1,
                                        "completion_evidence_json": None,
                                        "size_bytes": None,
                                        "locator_json": None,
                                        "original_name": None,
                                        "media_type": None,
                                        "last_error_json": None,
                                    },
                                ),
                                after=RowImage(
                                    exists=True,
                                    values={
                                        "completion_state": 2,
                                        "completion_evidence_json": {},
                                        "size_bytes": 10,
                                        "locator_json": {},
                                        "original_name": "a.mp4",
                                        "media_type": "video/mp4",
                                        "last_error_json": None,
                                    },
                                ),
                            ),
                        ),
                    )
                    return CommandPlan(
                        events=(event,),
                        owners={("device_files", 3): ("device_file", 3)},
                        state_rows={"device_files": {}, "outputs": {}},
                    )

            from camctl.history import validators

            assert "device_file" not in validators.NAMED_GUARDS
            receipt = commit_operation(FileCompleteCommand(), new_operation_key(), owned)
            assert receipt.kind == "rolled_back"
            assert "具名校验未接入" in str(receipt.error)
            assert _count(connection, "device_files") == 0
        finally:
            connection.close()

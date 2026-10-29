"""产物存储专项通过公开仓储与实际 SQLite 观察当前事实。"""

from contextlib import closing
from dataclasses import replace
from decimal import Decimal
import sqlite3

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.outputs.qualification import (
    FileCandidate,
    OperationConfig,
    QualificationOutcome,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.runtime import OwnedConnection

from .output_planning import capture_operation, event_case


def grant_candidate(owned, *, action_id=31, output_id=701, occurred_at):
    file_id, host_file_id = owned.connection.execute(
        "SELECT device_file_id,intermediate_file_id FROM outputs WHERE id=?",
        (output_id,),
    ).fetchone()
    (item,) = owned.connection.execute(
        "SELECT i.id FROM obtain_items i "
        "JOIN obtain_source_selections s ON s.id=i.selection_id "
        "JOIN action_dependencies d ON d.id=s.dependency_id "
        "WHERE d.action_id=? AND i.output_id=? ORDER BY i.id",
        (action_id, output_id),
    ).fetchone()
    return FileCandidate(
        action_id=action_id,
        item_id=item,
        processing_id=None,
        output_id=output_id,
        source_device_file_id=file_id,
        source_intermediate_file_id=host_file_id,
        target_extension="part",
        delivery_extension="mp4",
        delivery_display_name=f"产物 {output_id}",
        config=(
            None
            if host_file_id is not None
            else OperationConfig(3, Decimal("10"), Decimal("3"))
        ),
        occurred_at=occurred_at,
    )


def observe_product_competition(
    owned, *, action_id=31, output_id=701, occurred_at, observer=None
):
    """独立副本实际申请资格，返回公开的业务顺序阻挡事实。"""
    request = grant_candidate(
        owned, action_id=action_id, output_id=output_id, occurred_at=occurred_at
    )
    result = observed_operation(
        owned,
        lambda target: OutputsRepository().grant_file(
            request, new_operation_key(), target
        ),
        observer=observer,
    )
    if result.kind is not DbOutcomeKind.COMPLETED:
        raise result.error
    if result.value.reason not in (None, "business_order"):
        raise ConsistencyError(
            f"本次申请尚未形成可观察的产物竞争结果: {result.value.reason}"
        )
    assert (
        result.value.outcome is QualificationOutcome.GRANTED
        or result.value.reason == "business_order"
    )
    return result.value.reason == "business_order"


def observed_operation(owned, operation, *, observer=None):
    """公开操作在独立真实SQLite副本执行，观察不改变原库。"""
    assert not owned.connection.in_transaction
    with closing(sqlite3.connect(":memory:")) as connection:
        owned.connection.backup(connection)
        (foreign_keys,) = owned.connection.execute("PRAGMA foreign_keys").fetchone()
        connection.execute(f"PRAGMA foreign_keys={foreign_keys}")
        view = connection if observer is None else observer(connection)
        return operation(OwnedConnection(view, owned.metadata))


def product_proposal(owned, request):
    """实际公开授予生成完整事件组，H1仅消费GRANT之前的已保存前序。"""
    proposal = capture_operation(
        owned,
        lambda actual: OutputsRepository().grant_file(
            request, new_operation_key(), actual
        ),
    )
    event, current = event_case(proposal, proposal.events[-1].event_type)
    assert event.event_id == proposal.events[-1].event_id
    return event, current


def observed_rows(proposal, table, column, identity):
    assert proposal.read_coverage.covers(table, column, identity)
    return {
        row_id: row
        for row_id, row in proposal.state_rows.get(table, {}).items()
        if row.get(column) == identity
    }


class QueryProbe:
    """按SQLite授权列与游标字段识别查询，真实读取与关闭均执行。"""

    def __init__(self, connection, select, *, failure=None, fail_at=None):
        self.connection, self.select = connection, select
        self.failure, self.fail_at = failure, fail_at
        self.error = sqlite3.OperationalError("产物存储读取失败")
        self.cursors = []
        self.sizes = []
        self.returned = []
        self.query_plan = None
        self.queries = []
        self.matches = 0
        self.batches = 0

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, parameters=()):
        from unittest.mock import create_autospec
        from .outputs import observed_query

        cursor, columns, reads = observed_query(self.connection, sql, parameters)
        self.queries.append((columns, reads, parameters))
        if not self.select(columns, reads, parameters):
            return cursor
        self.matches += 1
        selected = self.fail_at is None or self.matches == self.fail_at
        if self.failure == "execute" and selected:
            cursor.close()
            raise self.error
        if self.query_plan is None:
            with closing(
                self.connection.execute("EXPLAIN QUERY PLAN " + sql, parameters)
            ) as explanation:
                self.query_plan = explanation.fetchall()
        probe = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
        probe.description = cursor.description
        probe.close.side_effect = cursor.close

        def fetch(kind, *args):
            self.batches += 1
            if (
                self.failure == "fetch"
                and selected
                or type(self.failure) is int
                and self.batches == self.failure
            ):
                raise self.error
            result = getattr(cursor, kind)(*args)
            self.returned.append(
                int(result is not None) if kind == "fetchone" else len(result)
            )
            if kind == "fetchmany":
                self.sizes.append(args[0])
            return result

        probe.fetchone.side_effect = lambda: fetch("fetchone")
        probe.fetchall.side_effect = lambda: fetch("fetchall")
        probe.fetchmany.side_effect = lambda size: fetch("fetchmany", size)
        self.cursors.append(probe)
        return probe


class WriteFault:
    """指定真实SQLite表操作失败，保留测试拥有的原OperationalError。"""

    def __init__(self, connection, target):
        from .output_planning import storage_fault

        self.fault = storage_fault(connection, target)
        self.error = sqlite3.OperationalError("产物存储写入失败")

    def __getattr__(self, name):
        return getattr(self.fault, name)

    def execute(self, sql, parameters=()):
        try:
            return self.fault.execute(sql, parameters)
        except sqlite3.DatabaseError:
            if self.fault.triggered:
                raise self.error
            raise


def capture_scoped_operation(owned, repository_module, operation):
    """在正式AtomicCommand交付边界观察提案范围，内核仍实际提交副本。"""
    from copy import deepcopy
    from unittest.mock import patch

    observed = []
    commit = repository_module.commit_operation

    def inspect(command, key, target):
        class Command:
            def plan(self, scope):
                plan = command.plan(scope)
                observed.append(
                    (
                        deepcopy(plan.state_rows),
                        plan.read_coverage,
                        plan.product_read_scope,
                        plan.plan_read_scope,
                    )
                )
                return plan

        return commit(Command(), key, target)

    with patch.object(repository_module, "commit_operation", inspect):
        proposal = capture_operation(owned, operation)
    assert len(observed) == 1
    state, coverage, product_scope, plan_scope = observed[0]
    return replace(
        proposal,
        state_rows=state,
        read_coverage=coverage,
        product_read_scope=product_scope,
        plan_read_scope=plan_scope,
    )

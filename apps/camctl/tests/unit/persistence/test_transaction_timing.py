"""事务计时沿真实命令与连接端口观察阶段，不执行真实 SQLite。"""

from contextlib import contextmanager
import json
import sqlite3
import sys
from typing import Protocol
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import new_operation_key
from camctl.history.changes import ChangeSet, EntityEventLink, ProgressUpdate, ReportEntityChange
from camctl.history.events import EventEnvelope, RowChange, RowImage
from camctl.history.validators import EventContext, ValidatedEvent
from camctl.logging_runtime.performance import performance_logging
from camctl.logging_runtime.service import LogChannel
from camctl.persistence import transaction as tx
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection


class _AdmissionFinalChecks(Protocol):
    def finish(self, db, transaction, accepted, state): ...


class _SelectionFinalChecks(Protocol):
    def finish(self, state): ...


class _SourceFinalChecks(Protocol):
    def finish_stream(self, state): ...


class _ObservedEvents:
    def __init__(self, events, active, *, generated=None, failure=None):
        self.events, self.active = events, active
        self.generated, self.failure = generated, failure
        self.index = 0

    def __iter__(self):
        assert self.active == ["event_generation"]
        return self

    def __next__(self):
        assert self.active == ["event_generation"]
        if self.index == len(self.events):
            if self.failure is not None:
                raise self.failure
            raise StopIteration
        event = self.events[self.index]
        self.index += 1
        if self.generated is not None:
            self.generated.append(("generated", event.event_id))
        return event


@pytest.fixture
def transaction_ports(monkeypatch):
    active, observed, generated = [], [], []
    original_phase = tx.db_phase

    @contextmanager
    def phase(value):
        assert not active, "主阶段不得包裹另一个主阶段"
        with original_phase(value):
            active.append(value.value)
            try:
                yield
            finally:
                active.pop()

    monkeypatch.setattr(tx, "db_phase", phase)
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    change_counts = {}

    def execute(sql, parameters=()):
        observed.append((sql.split()[0], active[-1] if active else None))
        cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
        cursor.rowcount = 1 if sql.startswith("INSERT") else -1
        if "MAX(change_count)" in sql:
            cursor.fetchone.return_value = (change_counts.get(tuple(parameters)),)
        else:
            cursor.fetchone.return_value = (0,)
        for table in ("entity_event_links", "report_entity_changes", "entity_snapshot_progress"):
            if sql.startswith("INSERT INTO " + table):
                observed.append((table, active[-1]))
                if table == "entity_event_links":
                    change_counts[tuple(parameters[:2])] = parameters[3]
        return cursor

    connection.execute.side_effect = execute
    owned = OwnedConnection(connection,
        DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing"))

    def bind(context, *args, **kwargs):
        observed.append(("bind", active[-1]))
        return context, None

    def validate(event, context):
        observed.append(("validate", active[-1]))
        return ValidatedEvent(event, "PLAN_STATUS_CHANGED", "RUNNING", (), {
            (row.table, row.row_id): (4, row.row_id) for row in event.rows})

    def targets(event, working):
        observed.append(("targets", active[-1]))
        return {(4, row.row_id) for row in event.rows}

    def derive(events, before, after):
        observed.append(("derive", active[-1]))
        counts, links, reports = dict(before.change_counts), [], []
        touched = set()
        for accepted in events:
            for ref in sorted(set(accepted.row_owners.values())):
                touched.add(ref)
                counts[ref] = counts.get(ref, 0) + 1
                links.append(EntityEventLink(*ref, accepted.envelope.event_id, counts[ref]))
                reports.append(ReportEntityChange(*ref, accepted.envelope.event_id, accepted.envelope.change_seq))
        assert {ref: counts[ref] for ref in touched} == dict(after.change_counts)
        return ChangeSet(tuple(links), tuple(reports), {ref: counts[ref] for ref in touched},
            tuple(ProgressUpdate(*ref, counts[ref]) for ref in sorted(touched)))

    def projection(connection, events, derived):
        observed.append(("projection", active[-1]))
        generated.append(("written", events[-1].event_id))

    monkeypatch.setattr(tx, "_bind_event_reads", create_autospec(tx._bind_event_reads, side_effect=bind))
    monkeypatch.setattr(tx, "_seal_event_reads", create_autospec(tx._seal_event_reads,
        side_effect=lambda *args: observed.append(("seal", active[-1]))))
    monkeypatch.setattr(tx, "validate_event", create_autospec(tx.validate_event, side_effect=validate))
    monkeypatch.setattr(tx, "event_report_targets", create_autospec(tx.event_report_targets,
        side_effect=targets))
    monkeypatch.setattr(tx, "derive_changes", create_autospec(tx.derive_changes,
        side_effect=derive))
    monkeypatch.setattr(tx, "_derived_map", create_autospec(tx._derived_map, return_value={}))
    monkeypatch.setattr(tx, "_write_projections", create_autospec(tx._write_projections, side_effect=projection))
    monkeypatch.setattr(tx, "_write_derived_history", create_autospec(tx._write_derived_history))
    # 三个签名分别对应实际封口端口；隔离模块导入，避免加载其登记及注册协作者。
    from camctl.persistence import repositories
    for name, contract in (("admission_sources", _AdmissionFinalChecks),
                           ("segment_selections", _SelectionFinalChecks),
                           ("source_transactions", _SourceFinalChecks)):
        port = create_autospec(contract, instance=True, spec_set=True)
        monkeypatch.setitem(sys.modules, f"camctl.persistence.repositories.{name}", port)
        monkeypatch.setattr(repositories, name, port, raising=False)
    # 仅约束被测调用阶段；生产源码初次导入不属于本夹具的隔离范围。
    from camctl import resources
    monkeypatch.setattr(resources.importlib_resources, "files", create_autospec(
        resources.importlib_resources.files, side_effect=AssertionError("被测调用不能读取真实包资源")))
    yield owned, active, observed, generated
    assert not active


def _event(identity, before, *, additional_row=False):
    rows = (
        RowChange("plans", 1, RowImage(True, {"status": before}),
                  RowImage(True, {"status": before + 1})),)
    if additional_row:
        rows += (RowChange("plans", 2, RowImage(True, {"status": 1}),
                           RowImage(True, {"status": 2})),)
    return EventEnvelope(identity, 1, 9, 1, 1, 2, None, 1, {}, rows)


def _summary(channel):
    assert channel.slog.call_count == 1
    record = channel.slog.call_args.args[0]
    payload = json.loads(record.message.split(" ", 1)[1])
    assert payload["stage"] == "db_transaction"
    return payload


@pytest.mark.parametrize("bounded", [False, True])
def test_transaction_phases_preserve_complete_event_order(transaction_ports, bounded):
    owned, active, observed, generated = transaction_ports
    events = (_event(1, 1), _event(2, 2, additional_row=True))
    command = create_autospec(tx.AtomicCommand, instance=True, spec_set=True)

    def plan(scope):
        assert active == ["plan"]
        scope.allocate(2)
        if not bounded:
            return tx.CommandPlan(events, {("plans", 1): ("plan", 1), ("plans", 2): ("plan", 2)},
                {"plans": {1: {"id": 1, "status": 1}, 2: {"id": 2, "status": 1}}}, result="saved")

        def context(event, transaction):
            assert active == ["validation_context"]
            return EventContext(transaction, {("plans", 1): ("plan", 1), ("plans", 2): ("plan", 2)},
                {"plans": {1: {"id": 1, "status": event.event_id}, 2: {"id": 2, "status": 1}}})

        def complete(connection):
            assert active == ["final_checks"]
            observed.append(("complete", active[-1]))

        return tx.BoundedCommandPlan(_ObservedEvents(events, active, generated=generated), context, "saved", complete)

    command.plan.side_effect = plan
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    with performance_logging(channel):
        result = tx.commit_operation(command, new_operation_key(), owned)
    assert result.kind == "completed" and result.result == "saved"
    labels = {"BEGIN": "begin_write", "COMMIT": "commit", "bind": "validation_context",
              "validate": "event_validation", "seal": "validation_context", "targets": "report_targets",
              "derive": "change_derivation", "projection": "sql_writes"}
    expected = (["BEGIN"] + ["bind", "targets", "validate", "seal", "derive", "projection"] * 2 + ["COMMIT"]
        if bounded else ["BEGIN", "targets", "targets", "bind", "validate", "seal", "bind",
                            "validate", "seal", "derive", "projection", "COMMIT"])
    assert [(name, phase) for name, phase in observed if name in labels] == [
        (name, labels[name]) for name in expected]
    if bounded:
        assert generated == [("generated", 1), ("written", 1), ("generated", 2), ("written", 2)]
    summary = _summary(channel)
    assert summary["counts"]["events"] == 2
    assert summary["counts"]["row_changes"] == 3
    assert observed.count(("entity_event_links", "sql_writes")) == 3
    assert observed.count(("report_entity_changes", "sql_writes")) == 3
    assert observed.count(("entity_snapshot_progress", "sql_writes")) == (3 if bounded else 2)
    assert {"begin_write", "read_state", "plan", "validation_context", "report_targets",
            "event_validation", "change_derivation", "sql_writes", "final_checks", "commit"} <= summary["phases"].keys()
    if bounded:
        assert summary["phases"]["event_generation"]["calls"] == 4
    owned.connection.close.assert_not_called()


def test_read_only_result_keeps_completed_receipt_and_times_rollback(transaction_ports):
    owned, active, observed, _ = transaction_ports
    command = create_autospec(tx.AtomicCommand, instance=True, spec_set=True)

    def complete(connection, value):
        assert active == ["final_checks"]
        return value

    command.plan.return_value = tx.CommandPlan((), {}, {}, "reused", read_only=True, complete_result=complete)
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    with performance_logging(channel):
        result = tx.commit_operation(command, new_operation_key(), owned)
    assert result.kind == "completed" and result.result == "reused"
    assert ("ROLLBACK", "rollback") in observed
    assert "commit" not in _summary(channel)["phases"]


@pytest.mark.parametrize("failure", [tx.TransactionError("planning"), KeyboardInterrupt("canceled")])
def test_planning_failure_keeps_original_failure_and_one_rollback(transaction_ports, failure):
    owned, _, observed, _ = transaction_ports
    command = create_autospec(tx.AtomicCommand, instance=True, spec_set=True)
    command.plan.side_effect = failure
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    with performance_logging(channel):
        if isinstance(failure, Exception):
            result = tx.commit_operation(command, new_operation_key(), owned)
            assert result.kind == "rolled_back" and result.error is failure
        else:
            with pytest.raises(type(failure)) as raised:
                tx.commit_operation(command, new_operation_key(), owned)
            assert raised.value is failure
            assert tx.write_receipt_for(failure).kind == "rolled_back"
    assert observed.count(("ROLLBACK", "rollback")) == 1
    assert "rollback" in _summary(channel)["phases"]


def _one_event_command(*, active, bounded=False, generate_error=None):
    command = create_autospec(tx.AtomicCommand, instance=True, spec_set=True)

    def plan(scope):
        scope.allocate(1)
        event = _event(1, 1)
        if not bounded:
            return tx.CommandPlan((event,), {("plans", 1): ("plan", 1)},
                {"plans": {1: {"id": 1, "status": 1}}}, "saved")

        return tx.BoundedCommandPlan(_ObservedEvents((event,), active, failure=generate_error),
            lambda event, transaction: EventContext(transaction,
                {("plans", 1): ("plan", 1)}, {"plans": {1: {"id": 1, "status": 1}}}),
            "saved", lambda connection: None)

    command.plan.side_effect = plan
    return command


@pytest.mark.parametrize("bounded", [False, True])
def test_commit_unknown_preserves_original_error_without_rollback(transaction_ports, bounded):
    owned, active, observed, _ = transaction_ports
    execute = owned.connection.execute.side_effect
    error = sqlite3.OperationalError("commit result unknown")

    def uncertain(sql, parameters=()):
        result = execute(sql, parameters)
        if sql == "COMMIT":
            raise error
        return result

    owned.connection.execute.side_effect = uncertain
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    with performance_logging(channel):
        result = tx.commit_operation(_one_event_command(active=active, bounded=bounded), new_operation_key(), owned)
    assert result.kind == "unknown" and result.error is error
    assert ("COMMIT", "commit") in observed
    assert not any(name == "ROLLBACK" for name, _ in observed)
    assert _summary(channel)["outcome"] == "unknown"


def test_lazy_generation_error_is_timed_and_rolls_back_once(transaction_ports):
    owned, active, observed, generated = transaction_ports
    error = tx.TransactionError("next event unavailable")
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    with performance_logging(channel):
        result = tx.commit_operation(_one_event_command(active=active, bounded=True, generate_error=error),
                                     new_operation_key(), owned)
    assert result.kind == "rolled_back" and result.error is error
    assert generated == [("written", 1)]
    assert observed.count(("ROLLBACK", "rollback")) == 1
    summary = _summary(channel)
    assert summary["phases"]["event_generation"]["calls"] == 3
    assert summary["counts"]["events"] == 1


@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.parametrize("port,phase", [("_bind_event_reads", "validation_context"),
    ("validate_event", "event_validation"), ("_seal_event_reads", "validation_context")])
def test_validation_boundary_error_keeps_phase_and_rollback(transaction_ports, bounded, port, phase):
    owned, active, observed, _ = transaction_ports
    error = tx.TransactionError("original event rejected")

    def rejected(*args, **kwargs):
        assert active == [phase]
        raise error

    getattr(tx, port).side_effect = rejected
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    with performance_logging(channel):
        result = tx.commit_operation(_one_event_command(active=active, bounded=bounded), new_operation_key(), owned)
    assert result.kind == "rolled_back" and result.error is error
    assert observed.count(("ROLLBACK", "rollback")) == 1
    assert phase in _summary(channel)["phases"]


def test_begin_error_does_not_add_rollback(transaction_ports):
    owned, _, observed, _ = transaction_ports
    error = sqlite3.OperationalError("begin failed")
    owned.connection.execute.side_effect = error
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    command = create_autospec(tx.AtomicCommand, instance=True, spec_set=True)
    with performance_logging(channel):
        result = tx.commit_operation(command, new_operation_key(), owned)
    assert result.kind == "rolled_back" and result.error is error
    command.plan.assert_not_called()
    assert observed == []
    assert "rollback" not in _summary(channel)["phases"]


def test_rollback_error_keeps_unknown_result_and_both_errors(transaction_ports):
    owned, _, observed, _ = transaction_ports
    execute = owned.connection.execute.side_effect
    original = tx.TransactionError("planning failed")
    rollback = sqlite3.OperationalError("rollback unavailable")

    def execute_with_rollback_error(sql, parameters=()):
        result = execute(sql, parameters)
        if sql == "ROLLBACK":
            raise rollback
        return result

    owned.connection.execute.side_effect = execute_with_rollback_error
    command = create_autospec(tx.AtomicCommand, instance=True, spec_set=True)
    command.plan.side_effect = original
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    with performance_logging(channel):
        result = tx.commit_operation(command, new_operation_key(), owned)
    assert result.kind == "unknown"
    assert result.error.exceptions == (original, rollback)
    assert observed.count(("ROLLBACK", "rollback")) == 1
    _summary(channel)


def test_disabled_timing_keeps_connection_and_original_sql_sequence(transaction_ports):
    owned, _, observed, _ = transaction_ports
    command = create_autospec(tx.AtomicCommand, instance=True, spec_set=True)

    def plan(scope):
        assert scope.connection is owned.connection
        return tx.CommandPlan((), {}, {}, "reused", read_only=True)

    command.plan.side_effect = plan
    result = tx.commit_operation(command, new_operation_key(), owned)
    assert result.kind == "completed" and result.result == "reused"
    assert [name for name, _ in observed] == ["BEGIN", "SELECT", "SELECT", "ROLLBACK"]
    owned.connection.close.assert_not_called()

"""真实输入事务结束已有同步责任。

报告由真实冻结入口登记。同步投影作为该消费者的既有状态准备；
同步开始、发布及取消生产者的组合由各自业务门禁验证。
"""

from __future__ import annotations

import pytest
import sqlite3
from dataclasses import replace
from copy import deepcopy

from camctl.acceptance.input import InputDiagnostic, InputStage, ParsedInput
from camctl.acceptance.service import AckDisposition, CommandMode, PlanDisposition, ProcessInput
from camctl.contracts.values import ConsistencyError
from camctl.persistence.repositories.acceptance import AcceptanceRepository
from camctl.persistence.transaction import row_facts
from camctl.contracts.enums import load_registry as load_enum_registry
from camctl.contracts.history_values import HistoryBoundary, ReadOrder, ReadScope, TransactionRange
from camctl.history.events import business_columns
from camctl.history.replay import EntityImage, apply_forward, apply_reverse
from camctl.history.validators import EventContext, EventValidationError, validate_event
from camctl.persistence.repositories.acceptance import ProcessInputCommand
from camctl.persistence.repositories.history import HistoryRepository
from camctl.persistence.transaction import commit_operation

from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import register_acceptance_guards
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.contracts.values import new_operation_key
from camctl.reporting.policy import (
    ReportingRepository, register_report_guards,
)

from ..acceptance.test_acceptance import Catalog, _NOW, _plan_body
from ..acceptance.test_atomicity import _process
from ..persistence.test_runtime import _create_valid_database
from ..persistence.test_transactions import FailingConnection


@pytest.fixture
def sync_environment(tmp_path):
    register_acceptance_guards()
    register_report_guards()
    path = tmp_path / "state.db"
    _create_valid_database(path)
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    connection = owned.connection
    action = {"name": "sync", "type": "report_status",
              "scheduled_at": "2026-01-15 09:00:00", "params": {"scope": "full"}}
    actions = [{**action, "name": f"sync-{index}"} for index in range(4)]
    assert _process(_plan_body(actions=actions), connection).kind is DbOutcomeKind.COMPLETED
    boundary = connection.execute("SELECT MAX(last_event_id) FROM history_transactions").fetchone()[0]
    connection.execute("UPDATE actions SET status = 2, execution_started = 1 WHERE id = 1")
    connection.execute("UPDATE plans SET status = 2 WHERE id = 1")
    connection.execute(
        "INSERT INTO state_syncs (id, action_id, mode, from_wm, started_boundary_event_id, status)"
        " VALUES (1, 1, 1, 0, ?, 1)", (boundary,),
    )
    report = _freeze(owned)
    yield owned, report
    connection.close()


def _freeze(owned):
    outcome = ReportingRepository().freeze_report(
        new_operation_key(), owned, occurred_at=1,
    )
    assert outcome.kind is DbOutcomeKind.COMPLETED
    return outcome.value.report


def _counts(connection):
    return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("plans", "actions", "plan_file_diagnostics", "history_transactions",
                          "history_events", "entity_event_links", "report_entity_changes")}


def _cumulative(connection):
    return connection.execute("SELECT acknowledged_wm, acknowledged_report_id FROM runtime_state").fetchone()


def _body(partition, report):
    if partition == "registered":
        body = _plan_body(request_id="100")
    elif partition == "reused":
        body = {"request_id": "42"}
    elif partition == "rejected":
        body = _plan_body(request_id="100", actions=[])
    else:
        body = _plan_body()
        del body["request_id"]
    body["last_report_id"] = str(report.report_id)
    return body


def _existing_ack(owned, relation):
    if relation == "advancing":
        return (0, None)
    if relation == "older":
        assert _process(_plan_body(request_id="43"), owned.connection).kind is DbOutcomeKind.COMPLETED
    else:
        # 新的同步开始历史要求产生同水位、不同身份的报告。
        _additional_sync(owned.connection)
    previous = _freeze(owned)
    if relation == "equal":
        _cancel_additional_sync(owned.connection)
    owned.connection.execute("UPDATE runtime_state SET acknowledged_wm = ?, acknowledged_report_id = ?",
                             (previous.to_wm, previous.report_id))
    return previous.to_wm, previous.report_id


def _additional_sync(connection, origin=None):
    from .test_freeze import _sync

    _sync(connection, 2, origin)


def _cancel_additional_sync(connection):
    ended = connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0]
    connection.execute("UPDATE state_syncs SET status = 3, ended_event_id = ? WHERE id = 2", (ended,))


def test_equal_watermark_ack_ends_sync_in_input_transaction(sync_environment):
    owned, report = sync_environment
    connection = owned.connection
    connection.execute("UPDATE runtime_state SET acknowledged_wm = ?, acknowledged_report_id = ?",
                       (report.to_wm, report.report_id))
    outcome = _process({"request_id": "42", "last_report_id": str(report.report_id)}, connection)
    assert outcome.kind is DbOutcomeKind.COMPLETED
    assert connection.execute(
        "SELECT status, ack_report_id, local_report_id FROM state_syncs WHERE id = 1"
    ).fetchone() == (2, report.report_id, None)


@pytest.mark.parametrize("partition", ["registered", "reused", "rejected", "missing_identity"])
@pytest.mark.parametrize("relation", ["advancing", "equal", "older"])
def test_every_input_partition_atomically_absorbs_sync(sync_environment, partition, relation):
    owned, report = sync_environment
    connection = owned.connection
    previous = _existing_ack(owned, relation)
    key = new_operation_key()
    outcome = _process(_body(partition, report), connection, key)
    assert outcome.kind is DbOutcomeKind.COMPLETED
    assert outcome.value.plan_disposition is {
        "registered": PlanDisposition.REGISTERED, "reused": PlanDisposition.REUSED,
        "rejected": PlanDisposition.REJECTED, "missing_identity": PlanDisposition.REJECTED,
    }[partition]
    assert outcome.value.ack_disposition is (AckDisposition.ABSORBED if relation == "advancing"
                                           else AckDisposition.VALID_NOT_ADVANCING)
    assert _cumulative(connection) == ((5, 1) if relation == "advancing" else previous)
    ended = connection.execute("SELECT ended_event_id FROM state_syncs WHERE id = 1").fetchone()[0]
    txn_id, first, last = connection.execute(
        "SELECT id, first_event_id, last_event_id FROM history_transactions WHERE operation_key = ?",
        (str(key),),
    ).fetchone()
    assert first <= ended <= last
    assert connection.execute("SELECT transaction_id, event_type, change_seq FROM history_events WHERE id = ?",
                              (ended,)).fetchone() == (txn_id, 29, None)
    assert connection.execute("SELECT status, ack_report_id FROM state_syncs WHERE id = 1").fetchone() == (2, 1)
    assert connection.execute("SELECT status, execution_started FROM actions WHERE id = 1").fetchone() == (2, 1)


@pytest.mark.parametrize("history_includes_start", [False, True])
@pytest.mark.parametrize("origin_covered", [False, True])
def test_report_history_and_origin_control_sync_end(sync_environment, history_includes_start, origin_covered):
    owned, report = sync_environment
    connection = owned.connection
    if not origin_covered:
        # 已有完整责任暂不生效，局部同步要求从原报告终点之后开始。
        ended = connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0]
        connection.execute("UPDATE state_syncs SET status = 3, ended_event_id = ? WHERE id = 1", (ended,))
        connection.execute("UPDATE runtime_state SET acknowledged_wm = ?, acknowledged_report_id = ?",
                           (report.to_wm, report.report_id))
        _additional_sync(connection, report)
        report = _freeze(owned)
        _cancel_additional_sync(connection)
        connection.execute("UPDATE state_syncs SET status = 1, ended_event_id = NULL WHERE id = 1")
    if not history_includes_start:
        later_boundary = connection.execute("SELECT MAX(last_event_id) FROM history_transactions").fetchone()[0]
        connection.execute("UPDATE state_syncs SET started_boundary_event_id = ?", (later_boundary,))
    outcome = _process(_body("reused", report), connection)
    assert outcome.kind is DbOutcomeKind.COMPLETED
    expected = (2, report.report_id) if history_includes_start and origin_covered else (1, None)
    assert connection.execute("SELECT status, ack_report_id FROM state_syncs WHERE id = 1").fetchone() == expected


def test_all_eligible_syncs_end_and_existing_endings_remain(sync_environment):
    owned, report = sync_environment
    connection = owned.connection
    boundary = report.boundary.last_event_id
    later = connection.execute("SELECT MAX(last_event_id) FROM history_transactions").fetchone()[0]
    connection.execute("UPDATE actions SET status = 2, execution_started = 1 WHERE id = 2")
    connection.execute("UPDATE actions SET status = 3, execution_started = 1 WHERE id = 3")
    connection.execute("UPDATE actions SET status = 6, execution_started = 1, cancel_requested = 1 WHERE id = 4")
    connection.executemany(
        "INSERT INTO state_syncs (id, action_id, mode, from_wm, started_boundary_event_id, status,"
        " local_report_id, ack_report_id, ended_event_id) VALUES (?, ?, 1, 0, ?, ?, ?, ?, ?)",
        [(2, 2, later, 1, None, None, None), (3, 3, boundary, 1, 1, None, None),
         (4, 4, boundary, 3, None, None, later)],
    )
    cancellation = row_facts(connection, "state_syncs", 4)
    outcome = _process(_body("reused", report), connection)
    assert outcome.kind is DbOutcomeKind.COMPLETED
    assert connection.execute("SELECT id, status, local_report_id, ack_report_id FROM state_syncs ORDER BY id").fetchall() == [
        (1, 2, None, 1), (2, 1, None, None), (3, 2, 1, 1), (4, 3, None, None),
    ]
    assert row_facts(connection, "state_syncs", 4) == cancellation


def test_repeated_ack_does_not_add_history_or_business_changes(sync_environment):
    owned, report = sync_environment
    connection = owned.connection
    business = connection.execute("SELECT * FROM report_entity_changes ORDER BY change_seq, entity_type, entity_id").fetchall()
    assert _process(_body("reused", report), connection).kind is DbOutcomeKind.COMPLETED
    before = _counts(connection)
    sync = row_facts(connection, "state_syncs", 1)
    assert _process(_body("reused", report), connection).kind is DbOutcomeKind.COMPLETED
    assert _counts(connection) == before
    assert row_facts(connection, "state_syncs", 1) == sync
    assert connection.execute("SELECT * FROM report_entity_changes ORDER BY change_seq, entity_type, entity_id").fetchall() == business


@pytest.mark.parametrize("ack", [None, False, "01", "404"])
def test_invalid_ack_keeps_sync(sync_environment, ack):
    owned, report = sync_environment
    connection = owned.connection
    outcome = _process({"request_id": "42", "last_report_id": ack}, connection)
    assert outcome.kind is DbOutcomeKind.COMPLETED
    assert outcome.value.ack_disposition is AckDisposition.INVALID
    assert _cumulative(connection) == (0, None)
    assert connection.execute("SELECT status, ack_report_id FROM state_syncs").fetchone() == (1, None)


def test_unparsed_input_cannot_end_sync(sync_environment):
    owned, _ = sync_environment
    command = ProcessInput(InputDiagnostic("bad.json", InputStage.PARSE, "输入不完整"),
                           Catalog(), CommandMode.RUN, _NOW)
    outcome = AcceptanceRepository().process_input(command, new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED
    assert outcome.value.ack_disposition is AckDisposition.NOT_PROCESSED
    assert owned.connection.execute("SELECT status FROM state_syncs").fetchone() == (1,)


@pytest.mark.parametrize("mutation", ["watermark", "boundary", "runtime_missing", "cumulative_identity", "origin_after_start"])
def test_unreliable_state_rolls_back_entire_input(sync_environment, mutation):
    owned, report = sync_environment
    connection = owned.connection
    if mutation == "watermark":
        connection.execute("UPDATE reports SET to_wm = to_wm + 1 WHERE id = 1")
    elif mutation == "boundary":
        connection.execute("UPDATE reports SET frozen_event_id = frozen_event_id - 1 WHERE id = 1")
    elif mutation == "runtime_missing":
        connection.execute("DELETE FROM runtime_state")
    elif mutation == "cumulative_identity":
        connection.execute("UPDATE runtime_state SET acknowledged_wm = 6, acknowledged_report_id = 1")
    else:
        connection.execute("UPDATE state_syncs SET mode = 2, after_report_id = 1, from_wm = 5")
    before = _counts(connection)
    outcome = _process(_body("registered", report), connection)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, ConsistencyError)
    assert _counts(connection) == before
    assert connection.execute("SELECT status FROM state_syncs").fetchone() == (1,)


@pytest.mark.parametrize("prefix", [
    "SELECT from_wm, to_wm", "SELECT id, action_id, from_wm", "UPDATE runtime_state",
    "UPDATE state_syncs", "INSERT INTO history_events", "INSERT INTO plans",
])
def test_read_or_write_failure_rolls_back_all_facts(sync_environment, prefix):
    owned, report = sync_environment
    connection = owned.connection
    before = _counts(connection)
    sync = row_facts(connection, "state_syncs", 1)
    outcome = _process(_body("registered", report), FailingConnection(connection, prefix))
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, sqlite3.OperationalError)
    assert _counts(connection) == before
    assert _cumulative(connection) == (0, None)
    assert row_facts(connection, "state_syncs", 1) == sync


def test_submit_handoff_failure_rolls_back_sync_confirmation(sync_environment):
    owned, report = sync_environment
    connection = owned.connection
    before = _counts(connection)

    class FailedHandoff:
        def needs_run(self, connection):
            assert connection.in_transaction
            assert connection.execute("SELECT status FROM state_syncs").fetchone() == (2,)
            raise OSError("接管事实不能可靠取得")

    command = ProcessInput(ParsedInput("plan.json", _body("registered", report)), Catalog(),
                           CommandMode.SUBMIT, _NOW, submit_handoff=FailedHandoff())
    outcome = AcceptanceRepository().process_input(command, new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert _counts(connection) == before
    assert _cumulative(connection) == (0, None)
    assert connection.execute("SELECT status FROM state_syncs").fetchone() == (1,)


def test_sync_and_ack_history_replay_preserves_independent_facts(sync_environment, tmp_path):
    owned, report = sync_environment
    connection = owned.connection
    before = {table: row_facts(connection, table, 1) for table in ("state_syncs", "runtime_state")}
    states = {table: {1: deepcopy(row)} for table, row in before.items()}
    states["reports"] = {1: row_facts(connection, "reports", 1)}
    key = new_operation_key()
    assert _process(_body("reused", report), connection, key).kind is DbOutcomeKind.COMPLETED
    txn, first, last = connection.execute(
        "SELECT id, first_event_id, last_event_id FROM history_transactions WHERE operation_key = ?", (str(key),)
    ).fetchone()
    page = HistoryRepository(tmp_path / "state.db").read_events(
        ReadScope(ReadOrder.ASCENDING, None, first, last, 8, lambda position: position),
        HistoryBoundary(txn, last),
    )
    assert [event.event_type for event in page.items] == [30, 29]
    owners = {("runtime_state", 1): ("runtime_state", 1), ("state_syncs", 1): ("state_sync", 1)}
    images = {}
    objects = load_enum_registry()["history_objects"]
    for table, name in (("runtime_state", "runtime_state"), ("state_syncs", "state_sync")):
        values = {column: before[table][column] for column in business_columns(table)}
        images[table] = EntityImage(objects[name]["id"], 1, True, {(table, 1): values}, first - 1, 0)
    originals = deepcopy(images)
    validated = []
    for event in page.items:
        value = validate_event(event, EventContext(TransactionRange(txn, first, last), owners, states))
        validated.append(value)
        images = {table: apply_forward(image, value) for table, image in images.items()}
        for row in event.rows:
            states[row.table][row.row_id].update(row.after.values)
    for table, image in images.items():
        facts = row_facts(connection, table, 1)
        assert image.rows[(table, 1)] == {column: facts[column] for column in business_columns(table)}
        assert set(image.rows[(table, 1)]) == business_columns(table)
        assert image.change_count == 1
    assert images["state_syncs"].rows[("state_syncs", 1)]["local_report_id"] is None
    for event in reversed(validated):
        images = {table: apply_reverse(image, event) for table, image in images.items()}
    for table, image in images.items():
        assert image.rows == originals[table].rows
        assert image.change_count == 0


@pytest.mark.parametrize("bad_fact", ["history", "origin", "missing_report", "type", "ended_event"])
def test_formal_guard_rejects_unqualified_confirmation(sync_environment, bad_fact):
    owned, report = sync_environment
    connection = owned.connection
    previous = _existing_ack(owned, "equal")
    before = _counts(connection)
    command = ProcessInput(ParsedInput("plan.json", _body("reused", report)), Catalog(), CommandMode.RUN, _NOW)

    class IncorrectConfirmation:
        def plan(self, scope):
            plan = ProcessInputCommand(command).plan(scope)
            states = deepcopy(plan.state_rows)
            events = list(plan.events)
            event = next(event for event in events if event.event_type == 29)
            if bad_fact == "history":
                states["reports"][1]["frozen_event_id"] = report.boundary.last_event_id - 1
            elif bad_fact == "origin":
                states["reports"][1]["from_wm"] = 1
            elif bad_fact == "missing_report":
                states["reports"] = {}
            elif bad_fact == "type":
                states["reports"][1]["frozen_event_id"] = True
            else:
                row = event.rows[0]
                after = replace(row.after, values={**row.after.values, "ended_event_id": event.event_id - 1})
                events[events.index(event)] = replace(event, rows=(replace(row, after=after),))
            return replace(plan, state_rows=states, events=tuple(events))

    receipt = commit_operation(IncorrectConfirmation(), new_operation_key(), owned)
    assert receipt.kind == "rolled_back"
    assert isinstance(receipt.error, EventValidationError)
    assert _counts(connection) == before
    assert _cumulative(connection) == previous
    assert connection.execute("SELECT status FROM state_syncs").fetchone() == (1,)


@pytest.mark.parametrize("commit_reached_database", [False, True])
def test_unknown_commit_preserves_atomic_sync_result(sync_environment, tmp_path, commit_reached_database):
    owned, report = sync_environment
    connection = owned.connection
    key = new_operation_key()

    class LostCommitResponse:
        def execute(self, statement, parameters=()):
            if statement == "COMMIT":
                if commit_reached_database:
                    connection.execute(statement, parameters)
                raise sqlite3.OperationalError("提交响应未取得")
            return connection.execute(statement, parameters)

    outcome = _process(_body("registered", report), LostCommitResponse(), key)
    assert outcome.kind is DbOutcomeKind.UNKNOWN
    assert outcome.value is None
    if connection.in_transaction:
        connection.rollback()
    with sqlite3.connect(tmp_path / "state.db") as check:
        txn = check.execute("SELECT id FROM history_transactions WHERE operation_key = ?", (str(key),)).fetchone()
        assert (txn is not None) is commit_reached_database
        assert _cumulative(check) == ((5, 1) if commit_reached_database else (0, None))
        assert check.execute("SELECT status FROM state_syncs").fetchone() == ((2,) if commit_reached_database else (1,))
        assert check.execute("SELECT COUNT(*) FROM plans WHERE request_id = 100").fetchone() == (int(commit_reached_database),)
        if commit_reached_database:
            ended = check.execute("SELECT ended_event_id, ack_report_id, local_report_id FROM state_syncs").fetchone()
            assert ended[1:] == (1, None)
            assert check.execute("SELECT transaction_id FROM history_events WHERE id = ?", (ended[0],)).fetchone() == txn
    retried = _process(_body("registered", report), connection)
    assert retried.kind is DbOutcomeKind.COMPLETED
    assert retried.value.plan_disposition is (PlanDisposition.REUSED if commit_reached_database else PlanDisposition.REGISTERED)
    assert connection.execute("SELECT COUNT(*) FROM history_events WHERE event_type = 29").fetchone() == (1,)

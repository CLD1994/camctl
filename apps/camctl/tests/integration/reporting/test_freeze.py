"""报告选择与真实 SQLite 事务。

同步投影作为已有状态准备；同步开始、取消及本地完成生产者另行组合验证。
报告记录均由真实范围选择与冻结产生，不指定外部报告范围。
"""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from dataclasses import replace
from pathlib import Path

import pytest

from camctl.acceptance.input import parse_input, read_input
from camctl.acceptance.service import AcceptanceContext, CommandMode, accept_input
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository, register_acceptance_guards
from camctl.persistence.runtime import DbConfig, DbOpenMode, OwnedConnection, open_existing
from camctl.reporting.policy import (
    ReportDecisionKind, ReportingRepository, _FreezeCommand, register_report_guards,
)
from camctl.persistence.transaction import commit_operation
from camctl.history.validators import EventValidationError

from ..acceptance.test_acceptance import Catalog
from ..acceptance.test_atomicity import _process
from ..persistence.test_runtime import _create_valid_database
from ..persistence.test_transactions import FailingConnection

register_acceptance_guards()
register_report_guards()
pytestmark = pytest.mark.asyncio


def _plan_body(request_id):
    return {
        "request_id": request_id, "created_at": "2026-01-15 08:00:00", "name": "plan",
        "actions": [{"name": "sync", "type": "report_status",
                     "scheduled_at": "2026-01-15 09:00:00", "params": {"scope": "full"}}],
    }


class _Reader:
    def read(self, path):
        return Path(path).read_bytes()


async def _submit(owned, tmp_path, request_id):
    target = tmp_path / f"plan-{request_id}.json"
    target.write_text(json.dumps(_plan_body(request_id)), encoding="utf-8")
    result = await accept_input(
        parse_input(await read_input(str(target), _Reader())),
        AcceptanceContext(mode=CommandMode.RUN, catalog=Catalog(), repository=AcceptanceRepository(),
                          clock=type("C", (), {"utc_micros": staticmethod(lambda: 1)})()),
        new_operation_key(), owned,
    )
    assert result.plan_id is not None


@pytest.fixture
def environment(tmp_path):
    _create_valid_database(tmp_path / "state.db")
    owned = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
    yield owned
    owned.connection.close()


def _freeze(owned):
    return ReportingRepository().freeze_report(new_operation_key(), owned, occurred_at=1)


def _counts(connection):
    return tuple(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                 for table in ("reports", "history_transactions", "history_events",
                               "entity_event_links", "report_entity_changes"))


def _ack(owned, report):
    outcome = _process({"request_id": "1", "last_report_id": str(report.report_id)}, owned.connection)
    assert outcome.kind is DbOutcomeKind.COMPLETED


def _sync(connection, action_id, origin=None, boundary=None):
    if boundary is None:
        boundary = connection.execute("SELECT MAX(last_event_id) FROM history_transactions").fetchone()[0]
    connection.execute("UPDATE actions SET status = 2, execution_started = 1 WHERE id = ?", (action_id,))
    connection.execute("UPDATE plans SET status = 2 WHERE id = (SELECT plan_id FROM actions WHERE id = ?)",
                       (action_id,))
    connection.execute(
        "INSERT INTO state_syncs (id, action_id, mode, after_report_id, from_wm,"
        " started_boundary_event_id, status) VALUES (?, ?, ?, ?, ?, ?, 1)",
        (action_id, action_id, 1 if origin is None else 2,
         None if origin is None else origin.report_id, 0 if origin is None else origin.to_wm, boundary),
    )


async def test_freeze_reads_current_ack(environment, tmp_path):
    await _submit(environment, tmp_path, "1")
    first = _freeze(environment).value.report
    _ack(environment, first)
    await _submit(environment, tmp_path, "2")
    outcome = _freeze(environment)
    assert outcome.kind is DbOutcomeKind.COMPLETED
    assert outcome.value.kind is ReportDecisionKind.GENERATE
    assert (outcome.value.report.from_wm, outcome.value.report.to_wm) == (2, 4)


async def test_freeze_uses_actual_watermark_after_new_submission(environment, tmp_path):
    await _submit(environment, tmp_path, "1")
    await _submit(environment, tmp_path, "2")
    outcome = _freeze(environment)
    assert outcome.kind is DbOutcomeKind.COMPLETED
    assert outcome.value.report.to_wm == 4


async def test_reuse_preserves_original_identity_boundary_and_history(environment, tmp_path):
    await _submit(environment, tmp_path, "1")
    first = _freeze(environment).value.report
    before = _counts(environment.connection)
    outcome = _freeze(environment)
    assert outcome.kind is DbOutcomeKind.COMPLETED
    assert outcome.value.kind is ReportDecisionKind.REUSE
    assert outcome.value.report == first
    assert _counts(environment.connection) == before


@pytest.mark.parametrize("initialized_only", [True, False])
async def test_empty_ordinary_range_skips_without_history(environment, tmp_path, initialized_only):
    if not initialized_only:
        await _submit(environment, tmp_path, "1")
        _ack(environment, _freeze(environment).value.report)
    before = _counts(environment.connection)
    outcome = _freeze(environment)
    assert outcome.kind is DbOutcomeKind.COMPLETED
    assert outcome.value.kind is ReportDecisionKind.SKIP
    assert outcome.value.report is None
    assert _counts(environment.connection) == before


async def test_freeze_takes_complete_boundary_excluding_own_transaction(environment, tmp_path):
    await _submit(environment, tmp_path, "1")
    boundary = environment.connection.execute(
        "SELECT id, last_event_id FROM history_transactions ORDER BY id DESC LIMIT 1").fetchone()
    outcome = _freeze(environment)
    report = outcome.value.report
    assert (report.boundary.txn_id, report.boundary.last_event_id) == boundary
    assert environment.connection.execute(
        "SELECT frozen_event_id, from_wm, to_wm, status FROM reports").fetchone() == (boundary[1], 0, 2, 1)
    assert environment.connection.execute("SELECT change_seq FROM history_events ORDER BY id DESC LIMIT 1").fetchone() == (None,)


async def test_frozen_report_excludes_later_changes(environment, tmp_path):
    await _submit(environment, tmp_path, "1")
    original = _freeze(environment).value.report
    before = environment.connection.execute("SELECT * FROM reports WHERE id = 1").fetchone()
    await _submit(environment, tmp_path, "2")
    second = _freeze(environment)
    assert second.value.kind is ReportDecisionKind.GENERATE
    assert second.value.report.to_wm == 4
    assert original.to_wm == 2
    assert environment.connection.execute("SELECT * FROM reports WHERE id = 1").fetchone() == before


@pytest.mark.parametrize("origin_id,want", [(None, 0), (1, 2), (2, 4)])
async def test_scope_combines_ack_with_sync_origin(environment, tmp_path, origin_id, want):
    await _submit(environment, tmp_path, "1")
    first = _freeze(environment).value.report
    await _submit(environment, tmp_path, "2")
    second = _freeze(environment).value.report
    _ack(environment, second)
    await _submit(environment, tmp_path, "3")
    origin = {None: None, 1: first, 2: second}[origin_id]
    _sync(environment.connection, 3, origin)
    outcome = _freeze(environment)
    assert (outcome.value.report.from_wm, outcome.value.report.to_wm) == (want, 6)


async def test_all_sync_origins_and_latest_start_are_combined(environment, tmp_path):
    await _submit(environment, tmp_path, "1")
    first = _freeze(environment).value.report
    await _submit(environment, tmp_path, "2")
    second = _freeze(environment).value.report
    _ack(environment, second)
    await _submit(environment, tmp_path, "3")
    first_created = environment.connection.execute("SELECT created_event_id FROM reports WHERE id = 1").fetchone()[0]
    _sync(environment.connection, 1, first, first_created)
    _sync(environment.connection, 2, second)
    outcome = _freeze(environment)
    assert (outcome.value.report.from_wm, outcome.value.report.to_wm) == (2, 6)


async def test_pending_sync_action_does_not_expand_report_scope(environment, tmp_path):
    await _submit(environment, tmp_path, "1")
    _ack(environment, _freeze(environment).value.report)
    await _submit(environment, tmp_path, "2")
    outcome = _freeze(environment)
    assert outcome.value.report.from_wm == 2


async def test_wider_registered_report_can_satisfy_later_narrower_scope(environment, tmp_path):
    await _submit(environment, tmp_path, "1")
    _freeze(environment)
    await _submit(environment, tmp_path, "2")
    origin = _freeze(environment).value.report
    await _submit(environment, tmp_path, "3")
    _sync(environment.connection, 3, origin)
    wide = _freeze(environment).value.report
    assert (wide.from_wm, wide.to_wm) == (0, 6)
    _ack(environment, origin)
    before = _counts(environment.connection)
    outcome = _freeze(environment)
    assert outcome.value.kind is ReportDecisionKind.REUSE
    assert outcome.value.report == wide
    assert _counts(environment.connection) == before


async def test_partial_sync_cannot_reference_report_registered_after_its_start(environment, tmp_path):
    await _submit(environment, tmp_path, "1")
    origin = _freeze(environment).value.report
    _sync(environment.connection, 1, origin, origin.boundary.last_event_id)
    before = _counts(environment.connection)
    outcome = _freeze(environment)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, ConsistencyError)
    assert _counts(environment.connection) == before


async def test_reuse_rejects_report_left_endpoint_inside_business_transaction(environment, tmp_path):
    await _submit(environment, tmp_path, "1")
    _ack(environment, _freeze(environment).value.report)
    await _submit(environment, tmp_path, "2")
    report = _freeze(environment).value.report
    environment.connection.execute("UPDATE reports SET from_wm = 1 WHERE id = ?", (report.report_id,))
    before = _counts(environment.connection)
    outcome = _freeze(environment)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, ConsistencyError)
    assert _counts(environment.connection) == before


@pytest.mark.parametrize("entry", ["cumulative_ack", "ack_input", "sync_origin"])
async def test_shared_readers_reject_same_invalid_left_boundary(environment, tmp_path, entry):
    await _submit(environment, tmp_path, "1")
    _ack(environment, _freeze(environment).value.report)
    await _submit(environment, tmp_path, "2")
    report = _freeze(environment).value.report
    connection = environment.connection
    connection.execute("UPDATE reports SET from_wm = 1 WHERE id = ?", (report.report_id,))
    if entry == "cumulative_ack":
        connection.execute("UPDATE runtime_state SET acknowledged_wm = 4, acknowledged_report_id = ?",
                           (report.report_id,))
    elif entry == "sync_origin":
        _sync(connection, 2, report)
    before = _counts(connection)
    outcome = (_process({"request_id": "1", "last_report_id": str(report.report_id)}, connection)
               if entry == "ack_input" else _freeze(environment))
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, ConsistencyError)
    assert _counts(connection) == before


async def test_business_boundary_allows_later_management_events_in_same_transaction(environment, tmp_path):
    await _submit(environment, tmp_path, "1")
    first = _freeze(environment).value.report
    body = _plan_body("2")
    body["last_report_id"] = str(first.report_id)
    assert _process(body, environment.connection).kind is DbOutcomeKind.COMPLETED
    second = _freeze(environment).value.report
    _ack(environment, second)
    await _submit(environment, tmp_path, "3")
    third = _freeze(environment).value.report
    outcome = _freeze(environment)
    assert outcome.value.kind is ReportDecisionKind.REUSE
    assert outcome.value.report == third
    assert third.from_wm == 4


async def test_later_sync_start_invalidates_full_coverage_candidate(environment, tmp_path):
    await _submit(environment, tmp_path, "1")
    origin = _freeze(environment).value.report
    await _submit(environment, tmp_path, "2")
    _sync(environment.connection, 1)
    covering = _freeze(environment).value.report
    _sync(environment.connection, 2, origin)
    outcome = _freeze(environment)
    assert covering.from_wm == 0 and covering.to_wm == 4
    assert outcome.value.kind is ReportDecisionKind.GENERATE
    assert outcome.value.report.report_id != covering.report_id
    assert (outcome.value.report.from_wm, outcome.value.report.to_wm) == (0, 4)


@pytest.mark.parametrize("freeze_first,want", [(True, (0, 4)), (False, (2, 6))])
async def test_concurrent_input_and_ack_are_read_at_one_complete_boundary(environment, tmp_path, freeze_first, want):
    await _submit(environment, tmp_path, "1")
    original = _freeze(environment).value.report
    await _submit(environment, tmp_path, "2")
    acquired = Event()
    contender = Event()

    def execute(is_freeze):
        owned = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
        connection = owned.connection

        class OrderedConnection:
            def execute(self, statement, parameters=()):
                if statement == "BEGIN IMMEDIATE":
                    if is_freeze == freeze_first:
                        result = connection.execute(statement, parameters)
                        acquired.set()
                        assert contender.wait(5)
                        return result
                    assert acquired.wait(5)
                    contender.set()
                return connection.execute(statement, parameters)

        try:
            ordered = OrderedConnection()
            if is_freeze:
                return _freeze(OwnedConnection(ordered, owned.metadata))
            body = _plan_body("3")
            body["last_report_id"] = str(original.report_id)
            return _process(body, ordered)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        frozen = pool.submit(execute, True)
        mutated = pool.submit(execute, False)
        outcome = frozen.result(timeout=20)
        assert mutated.result(timeout=20).kind is DbOutcomeKind.COMPLETED
    assert outcome.kind is DbOutcomeKind.COMPLETED
    report = outcome.value.report
    assert (report.from_wm, report.to_wm) == want
    assert environment.connection.execute(
        "SELECT MAX(change_seq) FROM history_events WHERE id <= ?",
        (report.boundary.last_event_id,),
    ).fetchone() == (want[1],)


async def test_old_report_cannot_satisfy_new_sync_even_without_business_changes(environment, tmp_path):
    await _submit(environment, tmp_path, "1")
    old = _freeze(environment).value.report
    _sync(environment.connection, 1)
    outcome = _freeze(environment)
    assert outcome.value.kind is ReportDecisionKind.GENERATE
    assert outcome.value.report.report_id == 2
    assert outcome.value.report.to_wm == old.to_wm == 2
    assert outcome.value.report.boundary.last_event_id > old.boundary.last_event_id


async def test_empty_partial_sync_range_still_selects_report_after_start(environment, tmp_path):
    await _submit(environment, tmp_path, "1")
    original = _freeze(environment).value.report
    _ack(environment, original)
    _sync(environment.connection, 1, original)
    outcome = _freeze(environment)
    assert outcome.value.kind is ReportDecisionKind.GENERATE
    assert (outcome.value.report.from_wm, outcome.value.report.to_wm) == (2, 2)
    before = _counts(environment.connection)
    reused = _freeze(environment)
    assert reused.value.kind is ReportDecisionKind.REUSE
    assert reused.value.report == outcome.value.report
    assert _counts(environment.connection) == before


@pytest.mark.parametrize("status", [2, 3])
async def test_ended_sync_does_not_expand_new_scope(environment, tmp_path, status):
    await _submit(environment, tmp_path, "1")
    original = _freeze(environment).value.report
    _ack(environment, original)
    _sync(environment.connection, 1)
    event = environment.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0]
    environment.connection.execute(
        "UPDATE state_syncs SET status = ?, ack_report_id = ?, ended_event_id = ?",
        (status, original.report_id if status == 2 else None, event),
    )
    await _submit(environment, tmp_path, "2")
    outcome = _freeze(environment)
    assert (outcome.value.report.from_wm, outcome.value.report.to_wm) == (2, 4)


@pytest.mark.parametrize("mutation", ["runtime_missing", "ack_mismatch", "sync_origin", "report_boundary", "report_watermark"])
async def test_unreliable_facts_fail_without_partial_registration(environment, tmp_path, mutation):
    await _submit(environment, tmp_path, "1")
    report = _freeze(environment).value.report
    connection = environment.connection
    if mutation == "runtime_missing":
        connection.execute("DELETE FROM runtime_state")
    elif mutation == "ack_mismatch":
        connection.execute("UPDATE runtime_state SET acknowledged_wm = 3, acknowledged_report_id = 1")
    elif mutation == "sync_origin":
        _sync(connection, 1, report)
        connection.execute("UPDATE state_syncs SET from_wm = 3")
    elif mutation == "report_boundary":
        connection.execute("UPDATE reports SET frozen_event_id = frozen_event_id - 1")
    else:
        connection.execute("UPDATE reports SET to_wm = 3")
    before = _counts(connection)
    outcome = _freeze(environment)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, ConsistencyError)
    assert _counts(connection) == before


@pytest.mark.parametrize("prefix", [
    "SELECT id, last_event_id", "SELECT acknowledged_wm", "SELECT id, action_id, from_wm",
    "SELECT id FROM reports", "INSERT INTO reports", "INSERT INTO history_events",
])
async def test_read_or_write_failure_rolls_back_freeze(environment, tmp_path, prefix):
    await _submit(environment, tmp_path, "1")
    before = _counts(environment.connection)
    faulty = OwnedConnection(FailingConnection(environment.connection, prefix), environment.metadata)
    outcome = _freeze(faulty)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, sqlite3.OperationalError)
    assert _counts(environment.connection) == before


@pytest.mark.parametrize("commit_reached_database", [False, True])
async def test_unknown_freeze_commit_is_resolved_by_original_operation_key(environment, tmp_path, commit_reached_database):
    await _submit(environment, tmp_path, "1")
    connection = environment.connection
    key = new_operation_key()

    class LostCommitResponse:
        def execute(self, statement, parameters=()):
            if statement == "COMMIT":
                if commit_reached_database:
                    connection.execute(statement, parameters)
                raise sqlite3.OperationalError("未取得提交响应")
            return connection.execute(statement, parameters)

    outcome = ReportingRepository().freeze_report(key, OwnedConnection(LostCommitResponse(), None))
    assert outcome.kind is DbOutcomeKind.UNKNOWN
    assert outcome.value is None
    if connection.in_transaction:
        connection.rollback()
    with sqlite3.connect(tmp_path / "state.db") as check:
        assert (check.execute("SELECT id FROM history_transactions WHERE operation_key = ?", (str(key),)).fetchone()
                is not None) is commit_reached_database
        assert check.execute("SELECT COUNT(*) FROM reports").fetchone() == (int(commit_reached_database),)
    retry = _freeze(environment)
    assert retry.value.kind is (ReportDecisionKind.REUSE if commit_reached_database else ReportDecisionKind.GENERATE)
    assert connection.execute("SELECT COUNT(*) FROM reports").fetchone() == (1,)


async def test_formal_freeze_guard_rejects_boundary_inside_own_transaction(environment, tmp_path):
    await _submit(environment, tmp_path, "1")

    class IncorrectBoundary:
        def plan(self, scope):
            plan = _FreezeCommand(1).plan(scope)
            event = plan.events[0]
            row = event.rows[0]
            after = replace(row.after, values={**row.after.values, "frozen_event_id": event.event_id})
            return replace(plan, events=(replace(event, rows=(replace(row, after=after),)),))

    before = _counts(environment.connection)
    receipt = commit_operation(IncorrectBoundary(), new_operation_key(), environment)
    assert receipt.kind == "rolled_back"
    assert isinstance(receipt.error, EventValidationError)
    assert _counts(environment.connection) == before

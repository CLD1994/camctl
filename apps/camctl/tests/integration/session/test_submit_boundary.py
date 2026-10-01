"""真实提交入口的输入保存、工作判断与接纳锁共同完成。"""

from __future__ import annotations

import sqlite3
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event

import pytest

from camctl.acceptance.input import InputDiagnostic, InputStage, ParsedInput
from camctl.acceptance.service import CommandMode, ProcessInput
from camctl.bootstrap import application
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.persistence.initialization import initialize_state
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository
from camctl.persistence.repositories.session import CloseAdmission, SessionRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, OwnedConnection, open_existing
from camctl.contracts.values import new_operation_key
from camctl.session import service
from camctl.session.locks import acquire_admission, probe_admission
from camctl.reporting.policy import (
    ReportDecision, ReportDecisionKind, ReportingRepository, register_report_guards,
)

from ..acceptance.test_acceptance import Catalog, _plan_body
from ..acceptance.test_atomicity import _process
from ..bootstrap.test_composition import _config_for


@pytest.fixture
def runtime(tmp_path: Path):
    config = _config_for(tmp_path)
    initialize_state(config, Path(config.paths.state_db))
    deps = build_runtime(CommandMode.SUBMIT, config, catalog=Catalog())
    yield deps
    close_runtime(deps)


def _saved_counts(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "plans", "actions", "plan_file_diagnostics", "history_transactions",
                "history_events", "entity_event_links", "report_entity_changes",
            )
        }


@pytest.mark.asyncio
async def test_submit_queries_updated_facts_inside_write_transaction(runtime, monkeypatch):
    observed = []
    query = application.query_work_facts

    def observe(connection):
        observed.append((connection.in_transaction, connection.execute(
            "SELECT COUNT(*) FROM actions"
        ).fetchone()[0]))
        return query(connection)

    monkeypatch.setattr(application, "query_work_facts", observe)
    outcome = await execute_command(runtime, ParsedInput("plan.json", _plan_body()))
    assert outcome.succeeded is True
    assert outcome.needs_run is True
    assert observed == [(True, 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_boundary", ["query", "probe"])
@pytest.mark.parametrize("partition", ["registered", "reused", "rejected", "diagnostic", "invalid_ack"])
async def test_submit_handoff_error_rolls_back_input(runtime, monkeypatch, failed_boundary, partition):
    source = _partition_source(runtime, partition)
    before = _saved_counts(runtime.state_db)

    def fail(*args):
        raise OSError("接管步骤未完成")

    if failed_boundary == "query":
        monkeypatch.setattr(application, "query_work_facts", fail)
    else:
        monkeypatch.setattr(service, "probe_admission", fail)
    outcome = await execute_command(runtime, source)
    assert outcome.succeeded is False
    assert outcome.reason == "state_db_error"
    assert outcome.needs_run is None
    assert _saved_counts(runtime.state_db) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("acceptor_present", [False, True])
async def test_submit_response_uses_transactional_admission_observation(runtime, acceptor_present):
    lease = acquire_admission(runtime.admission_lock) if acceptor_present else None
    try:
        outcome = await execute_command(runtime, ParsedInput("plan.json", _plan_body()))
        assert outcome.succeeded is True
        assert outcome.needs_run is (not acceptor_present)
        assert _saved_counts(runtime.state_db)["plans"] == 1
    finally:
        if lease is not None:
            lease.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("partition", ["reused", "rejected", "diagnostic", "invalid_ack"])
async def test_each_input_partition_finishes_handoff_in_original_transaction(runtime, monkeypatch, partition):
    source = _partition_source(runtime, partition)
    before = _saved_counts(runtime.state_db)
    query = application.query_work_facts
    observed = []

    def observe(connection):
        observed.append(connection.in_transaction)
        return query(connection)

    monkeypatch.setattr(application, "query_work_facts", observe)
    outcome = await execute_command(runtime, source)
    assert outcome.succeeded is True
    assert outcome.needs_run is True
    assert observed == [True]
    if partition == "reused":
        assert _saved_counts(runtime.state_db) == before
    elif partition in {"rejected", "diagnostic"}:
        saved = _saved_counts(runtime.state_db)
        assert saved["plans"] == saved["actions"] == 0
        assert saved["plan_file_diagnostics"] == 1


def _partition_source(runtime, partition):
    if partition == "reused":
        owned = open_existing(runtime.state_db, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            assert _process(_plan_body(), owned.connection).kind is DbOutcomeKind.COMPLETED
        finally:
            owned.connection.close()
        return ParsedInput("again.json", {"request_id": "42"})
    elif partition == "rejected":
        return ParsedInput("bad.json", {"request_id": "42"})
    elif partition == "diagnostic":
        return InputDiagnostic("missing.json", InputStage.OPEN, "输入文件不存在")
    elif partition == "invalid_ack":
        return ParsedInput("ack.json", _plan_body(ack="999"))
    return ParsedInput("plan.json", _plan_body())


def test_submit_without_handoff_port_cannot_commit(runtime):
    owned = open_existing(runtime.state_db, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        outcome = AcceptanceRepository().process_input(
            ProcessInput(ParsedInput("plan.json", _plan_body()), Catalog(), CommandMode.SUBMIT, 1),
            new_operation_key(), owned,
        )
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        assert outcome.value is None
        assert set(_saved_counts(runtime.state_db).values()) == {0}
    finally:
        owned.connection.close()


def test_concurrent_submits_are_serialized_with_probe(runtime):
    barrier = Barrier(2)

    def submit(request_id):
        barrier.wait(timeout=10)
        return asyncio.run(execute_command(runtime, ParsedInput("plan.json", _plan_body(request_id=request_id))))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit, identity) for identity in ("41", "42")]
        results = [future.result(timeout=20) for future in futures]
    assert [(result.succeeded, result.needs_run) for result in results] == [(True, True), (True, True)]
    assert _saved_counts(runtime.state_db)["plans"] == 2


class BeginObserver:
    """在真实 BEGIN 前给竞争者提供同步点，其余行为由真实连接执行。"""

    def __init__(self, connection, began):
        self.connection = connection
        self.began = began

    def execute(self, statement, parameters=()):
        if statement == "BEGIN IMMEDIATE":
            self.began.set()
        return self.connection.execute(statement, parameters)

    def close(self):
        self.connection.close()


def test_submit_before_close_keeps_acceptor_responsible(runtime, monkeypatch):
    lease = acquire_admission(runtime.admission_lock)
    queried, close_attempted = Event(), Event()
    query = application.query_work_facts

    def submit_query(connection):
        queried.set()
        assert close_attempted.wait(timeout=10)
        return query(connection)

    monkeypatch.setattr(application, "query_work_facts", submit_query)

    def close():
        owned = open_existing(runtime.state_db, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            return SessionRepository().close_admission(
                CloseAdmission(query, lease.close), new_operation_key(),
                OwnedConnection(BeginObserver(owned.connection, close_attempted), owned.metadata),
            )
        finally:
            owned.connection.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            submitted = pool.submit(lambda: asyncio.run(execute_command(
                runtime, ParsedInput("plan.json", _plan_body()),
            )))
            assert queried.wait(timeout=10)
            closed = pool.submit(close)
            result = submitted.result(timeout=20)
            closing = closed.result(timeout=20)
        assert result.succeeded is True
        assert result.needs_run is False
        assert closing.kind is DbOutcomeKind.COMPLETED
        assert closing.value.admission_closed is False
    finally:
        lease.close()


def test_close_before_submit_requests_later_run(runtime, monkeypatch):
    from camctl.bootstrap import lifecycle

    lease = acquire_admission(runtime.admission_lock)
    closing, submit_attempted = Event(), Event()
    original_open = lifecycle._open_connection

    def observe_submit(path):
        owned = original_open(path)
        return OwnedConnection(BeginObserver(owned.connection, submit_attempted), owned.metadata)

    monkeypatch.setattr(lifecycle, "_open_connection", observe_submit)

    def close_query(connection):
        closing.set()
        assert submit_attempted.wait(timeout=10)
        return application.query_work_facts(connection)

    def close():
        owned = open_existing(runtime.state_db, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            return SessionRepository().close_admission(
                CloseAdmission(close_query, lease.close), new_operation_key(), owned,
            )
        finally:
            owned.connection.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            closed = pool.submit(close)
            assert closing.wait(timeout=10)
            submitted = pool.submit(lambda: asyncio.run(execute_command(
                runtime, ParsedInput("plan.json", _plan_body()),
            )))
            closing_result = closed.result(timeout=20)
            result = submitted.result(timeout=20)
        assert closing_result.kind is DbOutcomeKind.COMPLETED
        assert closing_result.value.admission_closed is True
        assert result.succeeded is True
        assert result.needs_run is True
    finally:
        lease.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_reached_database", [False, True])
async def test_submit_unknown_commit_does_not_return_handoff_success(runtime, monkeypatch, commit_reached_database):
    from camctl.bootstrap import lifecycle

    original_open = lifecycle._open_connection

    class LostCommitResponse:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, statement, parameters=()):
            if statement == "COMMIT":
                if commit_reached_database:
                    self.connection.execute(statement, parameters)
                raise sqlite3.OperationalError("未取得提交结果")
            return self.connection.execute(statement, parameters)

        def close(self):
            self.connection.close()

    def open_fault(path):
        owned = original_open(path)
        return OwnedConnection(LostCommitResponse(owned.connection), owned.metadata)

    monkeypatch.setattr(lifecycle, "_open_connection", open_fault)
    outcome = await execute_command(runtime, ParsedInput("plan.json", _plan_body()))
    assert outcome.succeeded is False
    assert outcome.reason == "state_db_error"
    assert outcome.needs_run is None
    assert _saved_counts(runtime.state_db)["plans"] == int(commit_reached_database)


@pytest.mark.asyncio
@pytest.mark.parametrize("handoff_fails", [False, True])
async def test_ack_and_input_follow_handoff_transaction(runtime, monkeypatch, handoff_fails):
    register_report_guards()
    owned = open_existing(runtime.state_db, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        seeded = _process(_plan_body(request_id="41"), owned.connection)
        assert seeded.kind is DbOutcomeKind.COMPLETED
        latest_wm = owned.connection.execute("SELECT MAX(change_seq) FROM history_events").fetchone()[0]
        report = ReportingRepository().freeze_report(
            ReportDecision(ReportDecisionKind.GENERATE, from_wm=0, to_wm=latest_wm),
            new_operation_key(), owned, occurred_at=1,
        )
        assert report.kind is DbOutcomeKind.COMPLETED
        report_id = report.value.report_id
    finally:
        owned.connection.close()
    before = _saved_counts(runtime.state_db)
    query = application.query_work_facts
    observed = []

    def observe(connection):
        observed.append(connection.execute("SELECT acknowledged_wm FROM runtime_state").fetchone()[0])
        if handoff_fails:
            raise OSError("接管查询失败")
        return query(connection)

    monkeypatch.setattr(application, "query_work_facts", observe)
    outcome = await execute_command(runtime, ParsedInput("ack.json", _plan_body(ack=str(report_id))))
    assert observed == [latest_wm]
    assert outcome.succeeded is (not handoff_fails)
    with sqlite3.connect(runtime.state_db) as check:
        assert check.execute("SELECT acknowledged_wm FROM runtime_state").fetchone()[0] == (0 if handoff_fails else latest_wm)
    if handoff_fails:
        assert _saved_counts(runtime.state_db) == before
        assert outcome.needs_run is None


@pytest.mark.asyncio
async def test_ack_only_after_terminal_plan_skips_probe(runtime, monkeypatch):
    register_report_guards()
    owned = open_existing(runtime.state_db, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        body = _plan_body()
        body["actions"][0]["scheduled_at"] = "不是日期"
        assert _process(body, owned.connection).kind is DbOutcomeKind.COMPLETED
        latest_wm = owned.connection.execute("SELECT MAX(change_seq) FROM history_events").fetchone()[0]
        report = ReportingRepository().freeze_report(
            ReportDecision(ReportDecisionKind.GENERATE, from_wm=0, to_wm=latest_wm),
            new_operation_key(), owned, occurred_at=1,
        )
        assert report.kind is DbOutcomeKind.COMPLETED
    finally:
        owned.connection.close()

    def forbidden_probe(*args):
        pytest.fail("仅吸收 ACK 且没有其他工作时，不应探测锁")

    monkeypatch.setattr(service, "probe_admission", forbidden_probe)
    outcome = await execute_command(runtime, ParsedInput("ack.json", {
        "request_id": "42", "last_report_id": str(report.value.report_id),
    }))
    assert outcome.succeeded is True
    assert outcome.needs_run is False


def test_invalid_handoff_result_rolls_back_input(runtime):
    class InvalidHandoff:
        def needs_run(self, connection):
            return "false"

    owned = open_existing(runtime.state_db, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        outcome = AcceptanceRepository().process_input(
            ProcessInput(ParsedInput("plan.json", _plan_body()), Catalog(), CommandMode.SUBMIT, 1,
                         submit_handoff=InvalidHandoff()),
            new_operation_key(), owned,
        )
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        assert outcome.value is None
        assert set(_saved_counts(runtime.state_db).values()) == {0}
    finally:
        owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback_recovers, expected_kind", [
    (True, DbOutcomeKind.ROLLED_BACK), (False, DbOutcomeKind.UNKNOWN),
])
async def test_close_transaction_failure_keeps_released_admission_closed(runtime, rollback_recovers, expected_kind):
    lease = acquire_admission(runtime.admission_lock)
    owned = open_existing(runtime.state_db, DbOpenMode.EXISTING_RW, DbConfig())

    class EndTransactionFailure:
        failed_once = False

        def execute(self, statement, parameters=()):
            if statement == "ROLLBACK" and (not self.failed_once or not rollback_recovers):
                self.failed_once = True
                raise sqlite3.OperationalError("关闭事务未取得结束结果")
            return owned.connection.execute(statement, parameters)

    try:
        outcome = SessionRepository().close_admission(
            CloseAdmission(application.query_work_facts, lease.close), new_operation_key(),
            OwnedConnection(EndTransactionFailure(), owned.metadata),
        )
        assert outcome.kind is expected_kind
        assert outcome.value is None
        assert probe_admission(runtime.admission_lock).is_free
    finally:
        owned.connection.close()
        lease.close()
    # 即使结束事务失败，已释放的接纳资格也保持关闭。
    submitted = await execute_command(runtime, ParsedInput("later.json", _plan_body()))
    assert submitted.succeeded is True
    assert submitted.needs_run is True


@pytest.mark.asyncio
@pytest.mark.parametrize("ack_valid", [True, False])
@pytest.mark.parametrize("failed_boundary, handoff_fails", [
    (None, False), ("query", True), ("probe", True),
])
async def test_whole_rejection_ack_and_handoff_share_transaction(runtime, monkeypatch, ack_valid, failed_boundary, handoff_fails):
    register_report_guards()
    owned = open_existing(runtime.state_db, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        assert _process(_plan_body(request_id="41"), owned.connection).kind is DbOutcomeKind.COMPLETED
        latest_wm = owned.connection.execute("SELECT MAX(change_seq) FROM history_events").fetchone()[0]
        report = ReportingRepository().freeze_report(
            ReportDecision(ReportDecisionKind.GENERATE, from_wm=0, to_wm=latest_wm),
            new_operation_key(), owned, occurred_at=1,
        )
        assert report.kind is DbOutcomeKind.COMPLETED
    finally:
        owned.connection.close()
    before = _saved_counts(runtime.state_db)
    query = application.query_work_facts
    observed = []

    def observe(connection):
        observed.append(connection.execute("SELECT acknowledged_wm FROM runtime_state").fetchone()[0])
        if failed_boundary == "query":
            raise OSError("接管查询失败")
        return query(connection)

    monkeypatch.setattr(application, "query_work_facts", observe)
    if failed_boundary == "probe":
        def failed_probe(*args):
            raise OSError("接纳锁探测失败")
        monkeypatch.setattr(service, "probe_admission", failed_probe)
    outcome = await execute_command(runtime, ParsedInput("rejected.json", {
        "request_id": "42", "last_report_id": str(report.value.report_id) if ack_valid else "999",
    }))
    expected_wm = latest_wm if ack_valid else 0
    assert observed == [expected_wm]
    assert outcome.succeeded is (not handoff_fails)
    with sqlite3.connect(runtime.state_db) as check:
        assert check.execute("SELECT request_id FROM plans").fetchall() == [(41,)]
        assert check.execute("SELECT acknowledged_wm FROM runtime_state").fetchone()[0] == (0 if handoff_fails else expected_wm)
        diagnostics = check.execute("SELECT errors_json FROM plan_file_diagnostics").fetchall()
        if handoff_fails:
            assert outcome.reason == "state_db_error"
            assert diagnostics == []
            assert _saved_counts(runtime.state_db) == before
            assert outcome.needs_run is None
        else:
            assert outcome.needs_run is True
            assert len(diagnostics) == 1
            codes = [entry["code"] for entry in json.loads(diagnostics[0][0])]
            assert codes == (["plan_body_rejected"] if ack_valid else ["plan_body_rejected", "invalid_ack"])

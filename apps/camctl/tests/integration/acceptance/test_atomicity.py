"""受理身份、关联与 ACK 在真实事务中的一致性。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier
import json
import sqlite3

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import (
    AcceptanceResult, AckDisposition, CommandMode, PlanDisposition, ProcessInput,
)
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, OwnedConnection, open_existing

from .test_acceptance import Catalog, _NOW, _accept, _plan_body, _seed_report, environment
from ..persistence.test_runtime import _create_valid_database
from ..support.database import business_database, database_path


def _process(body, connection, key=None):
    return AcceptanceRepository().process_input(
        ProcessInput(ParsedInput("original.json", body), Catalog(), CommandMode.RUN, _NOW),
        key or new_operation_key(), OwnedConnection(connection, None),
    )


@pytest.mark.asyncio
async def test_invalid_identity_does_not_query_existing_request(environment, tmp_path):
    from camctl.acceptance.service import accept_input
    from .test_acceptance import _owned

    connection, context = environment
    await _accept(environment, tmp_path, _plan_body())
    reads = []

    def authorize(operation, table, column, *_):
        if operation == sqlite3.SQLITE_READ:
            reads.append((table, column))
        return sqlite3.SQLITE_OK

    async with business_database(database_path(_owned(environment))) as database:
        await database.read(lambda current: current.connection.set_authorizer(authorize))
        result = await accept_input(ParsedInput("invalid.json", {"request_id": "042"}),
            context, new_operation_key(), database)
        await database.read(lambda current: current.connection.set_authorizer(None))
    assert result.plan_disposition is PlanDisposition.REJECTED
    assert ("plans", "request_id") not in reads


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["9007199254740993", "9223372036854775807"])
async def test_exact_identity_survives_storage_and_retry(environment, tmp_path, identity):
    connection, _ = environment
    first = await _accept(environment, tmp_path, _plan_body(request_id=identity))
    again = await _accept(environment, tmp_path, {"request_id": identity})
    assert first.plan_disposition is PlanDisposition.REGISTERED
    assert again.plan_disposition is PlanDisposition.REUSED
    assert again.plan_id == first.plan_id
    assert connection.execute("SELECT request_id FROM plans").fetchall() == [(int(identity),)]


@pytest.mark.asyncio
async def test_rejected_request_can_later_be_registered(environment, tmp_path):
    connection, _ = environment
    rejected = await _accept(environment, tmp_path, {"request_id": "42"})
    accepted = await _accept(environment, tmp_path, _plan_body())
    assert rejected.plan_disposition is PlanDisposition.REJECTED
    assert accepted.plan_disposition is PlanDisposition.REGISTERED
    assert connection.execute("SELECT id, request_id FROM plans").fetchall() == [(1, 42)]


def test_concurrent_same_request_allocates_one_instance(tmp_path):
    path = tmp_path / "state.db"
    _create_valid_database(path)
    barrier = Barrier(2)

    def submit():
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            barrier.wait(timeout=10)
            return _process(_plan_body(), owned.connection)
        finally:
            owned.connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert all(result.kind is DbOutcomeKind.COMPLETED for result in results)
    assert {result.value.plan_disposition for result in results} == {
        PlanDisposition.REGISTERED, PlanDisposition.REUSED,
    }
    assert {result.value.plan_id for result in results} == {1}
    with closing(sqlite3.connect(path)) as check:
        assert check.execute("SELECT id, request_id FROM plans").fetchall() == [(1, 42)]
        assert check.execute("SELECT id, plan_id FROM actions").fetchall() == [(1, 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_table", ["actions", "action_dependencies", "plans", "runtime_state"])
async def test_registration_members_and_ack_roll_back_together(environment, tmp_path, failed_table):
    connection, _ = environment
    await _seed_report(environment, tmp_path, report_id=20)
    tables = ("plans", "actions", "action_dependencies", "auto_preview_links",
              "plan_file_diagnostics", "history_transactions", "history_events")
    before = {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    body = _plan_body(ack="20")
    body["actions"].append({"name": "fetch", "type": "obtain_action_outputs",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"source": {"action_name": "shoot"}, "purpose": "manual"}})
    def authorize(operation, table, *_):
        return (sqlite3.SQLITE_DENY if table == failed_table and
                operation in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE)
                else sqlite3.SQLITE_OK)
    connection.set_authorizer(authorize)
    try:
        result = _process(body, connection)
    finally:
        connection.set_authorizer(None)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, sqlite3.DatabaseError)
    assert {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables} == before
    assert connection.execute("SELECT acknowledged_wm FROM runtime_state").fetchone() == (0,)


@pytest.mark.asyncio
async def test_ack_error_diagnostic_rolls_back_with_registration(environment, tmp_path):
    connection, _ = environment
    def authorize(operation, table, *_):
        return (sqlite3.SQLITE_DENY if operation == sqlite3.SQLITE_INSERT and
                table == "plan_file_diagnostics" else sqlite3.SQLITE_OK)
    connection.set_authorizer(authorize)
    try:
        result = _process(_plan_body(ack="20"), connection)
    finally:
        connection.set_authorizer(None)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    for table in ("plans", "actions", "plan_file_diagnostics", "history_transactions", "history_events"):
        assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_reached_database", [False, True])
async def test_unknown_commit_is_checked_by_original_key_before_retry(environment, tmp_path, commit_reached_database):
    connection, _ = environment
    key = new_operation_key()

    class LostCommitResponse:
        @property
        def in_transaction(self):
            return connection.in_transaction

        @property
        def total_changes(self):
            return connection.total_changes

        def execute(self, statement, parameters=()):
            if statement == "COMMIT":
                if commit_reached_database:
                    connection.execute(statement, parameters)
                raise sqlite3.OperationalError("提交响应未取得")
            return connection.execute(statement, parameters)

    body = _plan_body()
    outcome = _process(body, LostCommitResponse(), key)
    # 结果未知时立即停用写连接；关闭也会撤销尚未提交的事务。
    connection.close()
    assert outcome.kind is DbOutcomeKind.UNKNOWN
    assert outcome.value is None

    tables = ("history_transactions", "history_events", "entity_event_links",
              "report_entity_changes", "entity_snapshot_progress", "plans", "actions")

    def assert_saved_facts(check):
        from camctl.contracts.input_fields import reconstruct_action_input

        txn_id, first, last = check.execute(
            "SELECT id, first_event_id, last_event_id FROM history_transactions WHERE operation_key = ?",
            (str(key),),
        ).fetchone()
        events = check.execute(
            "SELECT id, transaction_id, event_type, body_json FROM history_events ORDER BY id"
        ).fetchall()
        assert [(event[0], event[1]) for event in events] == [
            (event_id, txn_id) for event_id in range(first, last + 1)
        ]
        assert [event[2] for event in events] == [2, 1]
        for _, _, _, encoded in events:
            event = json.loads(encoded)
            assert len(event["rows"]) == 1
            change = event["rows"][0]
            assert change["before"] == {"exists": False, "values": {}}
            assert change["after"]["exists"] is True
            table = change["table"]
            assert table in {"plans", "actions"}
            cursor = check.execute(f"SELECT * FROM {table} WHERE id = ?", (change["id"],))
            row = dict(zip((column[0] for column in cursor.description), cursor.fetchone()))
            for column, value in change["after"]["values"].items():
                actual = row[column]
                if column.endswith("_json") and actual is not None:
                    actual = json.loads(actual)
                assert actual == value
        assert check.execute("SELECT id, request_id, name FROM plans").fetchall() == [(1, 42, body["name"])]
        cursor = check.execute("SELECT * FROM actions")
        rows = cursor.fetchall()
        assert len(rows) == 1
        action = dict(zip((column[0] for column in cursor.description), rows[0]))
        assert (action["id"], action["plan_id"]) == (1, 1)
        assert reconstruct_action_input(action) == body["actions"][0]

    uri = (tmp_path / "state.db").resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as check:
        found = check.execute(
            "SELECT id, first_event_id, last_event_id FROM history_transactions WHERE operation_key = ?",
            (str(key),),
        ).fetchall()
        assert len(found) == int(commit_reached_database)
        before = {table: check.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                  for table in tables}
        if commit_reached_database:
            assert_saved_facts(check)
        else:
            assert all(not rows for rows in before.values())

    recovered = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
    try:
        retried = _process(body, recovered.connection, key)
        assert retried.kind is DbOutcomeKind.COMPLETED
        after = {table: recovered.connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                 for table in tables}
        assert recovered.connection.execute(
            "SELECT COUNT(*) FROM history_transactions WHERE operation_key = ?", (str(key),)
        ).fetchone() == (1,)
        assert_saved_facts(recovered.connection)
        if commit_reached_database:
            assert after == before
        else:
            assert len(after["history_transactions"]) == 1
            assert after["history_events"]
        # 同一操作的原结果为首次登记；新操作键重复请求才返回 REUSED。
        assert retried.value == AcceptanceResult(
            plan_disposition=PlanDisposition.REGISTERED,
            plan_id=1,
            ack_disposition=AckDisposition.NOT_PROVIDED,
            ack_watermark=0,
        )
    finally:
        recovered.connection.close()


def test_same_key_checks_complete_original_request_and_new_key_reuses(environment):
    from copy import deepcopy

    connection, _ = environment
    key = new_operation_key()
    body = _plan_body()
    original = ProcessInput(ParsedInput("original.json", body), Catalog(), CommandMode.RUN, _NOW)
    repository = AcceptanceRepository()
    def process(command, operation_key):
        return repository.process_input(command, operation_key, OwnedConnection(connection, None))

    first = process(original, key)
    assert first.kind is DbOutcomeKind.COMPLETED
    changed = deepcopy(body)
    changed["actions"][0]["name"] = "changed"
    for altered in (
        ProcessInput(ParsedInput("original.json", changed), Catalog(), CommandMode.RUN, _NOW),
        ProcessInput(ParsedInput("other.json", body), Catalog(), CommandMode.RUN, _NOW),
        ProcessInput(ParsedInput("original.json", body), Catalog(), CommandMode.RUN, _NOW + 1),
        ProcessInput(ParsedInput("original.json", body), Catalog(), CommandMode.SUBMIT, _NOW,
                     submit_handoff=type("Handoff", (), {"needs_run": lambda self, connection: True})()),
    ):
        rejected = process(altered, key)
        assert rejected.kind is DbOutcomeKind.ROLLED_BACK
        assert "不一致" in str(rejected.error)
    assert process(original, key).value == first.value
    new_key = new_operation_key()
    assert process(original, new_key).value.plan_disposition is PlanDisposition.REUSED
    assert connection.execute("SELECT COUNT(*) FROM history_transactions").fetchone() == (1,)
    assert connection.execute("SELECT COUNT(*) FROM acceptance_receipts").fetchone() == (1,)


@pytest.mark.asyncio
async def test_original_submit_result_includes_ack_diagnostics_and_handoff(environment, tmp_path):
    connection, _ = environment
    await _seed_report(environment, tmp_path, report_id=20)
    body = _plan_body(ack="999")
    body["actions"][0]["scheduled_at"] = "invalid"
    class Handoff:
        def __init__(self):
            self.value = True
        def needs_run(self, connection):
            return self.value
    handoff = Handoff()
    command = ProcessInput(ParsedInput("original.json", body), Catalog(), CommandMode.SUBMIT, _NOW, handoff)
    key = new_operation_key()
    repo = AcceptanceRepository()
    first = repo.process_input(command, key, OwnedConnection(connection, None))
    assert first.kind is DbOutcomeKind.COMPLETED
    assert first.value.plan_disposition is PlanDisposition.REGISTERED
    assert first.value.ack_disposition is AckDisposition.INVALID
    assert first.value.diagnostic_id is not None
    assert len(first.value.diagnostics) >= 2
    assert first.value.needs_run is True
    handoff.value = False
    recovered = repo.process_input(command, key, OwnedConnection(connection, None))
    assert recovered.kind is DbOutcomeKind.COMPLETED
    assert recovered.value == first.value


def test_existing_history_without_receipt_cannot_reconstruct_original(environment):
    connection, _ = environment
    key = new_operation_key()
    first = _process(_plan_body(), connection, key)
    assert first.kind is DbOutcomeKind.COMPLETED
    connection.execute("DELETE FROM acceptance_receipts")
    retried = _process(_plan_body(), connection, key)
    assert retried.kind is DbOutcomeKind.ROLLED_BACK
    assert "缺少" in str(retried.error)
    assert _process(_plan_body(), connection).value.plan_disposition is PlanDisposition.REUSED


@pytest.mark.asyncio
async def test_absorbed_ack_restores_original_watermark_after_later_changes(environment, tmp_path):
    connection, _ = environment
    await _seed_report(environment, tmp_path, report_id=2)
    key = new_operation_key()
    first = _process(_plan_body(request_id="42", ack="2"), connection, key)
    assert first.kind is DbOutcomeKind.COMPLETED
    assert first.value.ack_disposition is AckDisposition.ABSORBED
    assert first.value.ack_watermark > 0
    later = _process(_plan_body(request_id="43", ack="2"), connection)
    assert later.kind is DbOutcomeKind.COMPLETED
    assert later.value.ack_disposition is AckDisposition.VALID_NOT_ADVANCING
    recovered = _process(_plan_body(request_id="42", ack="2"), connection, key)
    assert recovered.kind is DbOutcomeKind.COMPLETED
    assert recovered.value == first.value


def test_exact_request_identity_distinguishes_boolean_from_number(environment):
    from decimal import Decimal
    from copy import deepcopy
    connection, _ = environment
    body = _plan_body()
    body["actions"][0]["params"]["shots"] = Decimal("1.0000000000000001")
    key = new_operation_key()
    first = _process(body, connection, key)
    assert first.kind is DbOutcomeKind.COMPLETED
    assert _process(deepcopy(body), connection, key).value == first.value
    for value in (True, Decimal("1.0000000000000002")):
        changed = deepcopy(body)
        changed["actions"][0]["params"]["shots"] = value
        assert _process(changed, connection, key).kind is DbOutcomeKind.ROLLED_BACK


def test_old_database_without_receipt_table_allows_no_change_reuse(environment):
    connection, _ = environment
    assert _process(_plan_body(), connection).kind is DbOutcomeKind.COMPLETED
    connection.execute("DROP TABLE acceptance_receipts")
    before = connection.execute("SELECT COUNT(*) FROM history_transactions").fetchone()
    result = _process(_plan_body(), connection)
    assert result.kind is DbOutcomeKind.COMPLETED
    assert result.value.plan_disposition is PlanDisposition.REUSED
    assert connection.execute("SELECT COUNT(*) FROM history_transactions").fetchone() == before
    writing = _process(_plan_body(request_id="99"), connection)
    assert writing.kind is DbOutcomeKind.ROLLED_BACK
    assert "升级" in str(writing.error)
    assert connection.execute("SELECT COUNT(*) FROM history_transactions").fetchone() == before


def test_input_read_diagnostic_restores_and_distinguishes_original_source(environment):
    from camctl.acceptance.input import InputDiagnostic, InputStage
    connection, _ = environment
    key = new_operation_key()
    repository = AcceptanceRepository()
    source = InputDiagnostic("plan.json", InputStage.READ, "read failed")
    def process(source):
        return repository.process_input(ProcessInput(source, Catalog(), CommandMode.RUN, _NOW),
                                        key, OwnedConnection(connection, None))
    original = process(source)
    assert original.kind is DbOutcomeKind.COMPLETED
    assert original.value.diagnostic_id is not None
    assert original.value.diagnostics
    assert process(source).value == original.value
    assert process(InputDiagnostic("plan.json", InputStage.OPEN, "read failed")).kind is DbOutcomeKind.ROLLED_BACK
    assert process(ParsedInput("plan.json", {})).kind is DbOutcomeKind.ROLLED_BACK


def test_receipt_write_failure_rolls_back_business_changes(environment):
    connection, _ = environment
    def authorize(operation, table, *_):
        return (sqlite3.SQLITE_DENY if operation == sqlite3.SQLITE_INSERT and
                table == "acceptance_receipts" else sqlite3.SQLITE_OK)
    connection.set_authorizer(authorize)
    try:
        result = _process(_plan_body(), connection)
    finally:
        connection.set_authorizer(None)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    for table in ("plans", "actions", "history_transactions", "history_events", "acceptance_receipts"):
        assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)

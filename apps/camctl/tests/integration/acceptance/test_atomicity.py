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

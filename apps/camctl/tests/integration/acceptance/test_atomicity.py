"""受理身份、关联与 ACK 在真实事务中的一致性。"""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import sqlite3

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode, PlanDisposition, ProcessInput
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, OwnedConnection, open_existing

from .test_acceptance import Catalog, _NOW, _accept, _plan_body, _seed_report, environment
from ..persistence.test_runtime import _create_valid_database
from ..persistence.test_transactions import FailingConnection


def _process(body, connection, key=None):
    return AcceptanceRepository().process_input(
        ProcessInput(ParsedInput("original.json", body), Catalog(), CommandMode.RUN, _NOW),
        key or new_operation_key(), OwnedConnection(connection, None),
    )


@pytest.mark.asyncio
async def test_invalid_identity_does_not_query_existing_request(environment, tmp_path):
    connection, _ = environment
    await _accept(environment, tmp_path, _plan_body())
    statements = []
    connection.set_trace_callback(statements.append)
    try:
        result = await _accept(environment, tmp_path, {"request_id": "042"})
    finally:
        connection.set_trace_callback(None)
    assert result.plan_disposition is PlanDisposition.REJECTED
    assert not any("FROM plans WHERE request_id" in statement for statement in statements)


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
    with sqlite3.connect(path) as check:
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
    prefix = f"UPDATE {failed_table}" if failed_table == "runtime_state" else f"INSERT INTO {failed_table}"
    result = _process(body, FailingConnection(connection, prefix))
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, sqlite3.OperationalError)
    assert {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables} == before
    assert connection.execute("SELECT acknowledged_wm FROM runtime_state").fetchone() == (0,)


@pytest.mark.asyncio
async def test_ack_error_diagnostic_rolls_back_with_registration(environment, tmp_path):
    connection, _ = environment
    result = _process(_plan_body(ack="20"), FailingConnection(connection, "INSERT INTO plan_file_diagnostics"))
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    for table in ("plans", "actions", "plan_file_diagnostics", "history_transactions", "history_events"):
        assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_reached_database", [False, True])
async def test_unknown_commit_is_checked_by_original_key_before_retry(environment, tmp_path, commit_reached_database):
    connection, _ = environment
    key = new_operation_key()

    class LostCommitResponse:
        def execute(self, statement, parameters=()):
            if statement == "COMMIT":
                if commit_reached_database:
                    connection.execute(statement, parameters)
                raise sqlite3.OperationalError("提交响应未取得")
            return connection.execute(statement, parameters)

    outcome = _process(_plan_body(), LostCommitResponse(), key)
    assert outcome.kind is DbOutcomeKind.UNKNOWN
    assert outcome.value is None
    # 原连接停止后才可将查不到提交解释为未提交。
    if connection.in_transaction:
        connection.rollback()
    with sqlite3.connect(tmp_path / "state.db") as check:
        found = check.execute("SELECT id FROM history_transactions WHERE operation_key = ?", (str(key),)).fetchone()
        assert (found is not None) is commit_reached_database
        assert check.execute("SELECT COUNT(*) FROM plans").fetchone() == (int(commit_reached_database),)
    retried = _process(_plan_body(), connection)
    assert retried.kind is DbOutcomeKind.COMPLETED
    assert retried.value.plan_disposition is (PlanDisposition.REUSED if commit_reached_database else PlanDisposition.REGISTERED)
    assert connection.execute("SELECT id, request_id FROM plans").fetchall() == [(1, 42)]
    assert connection.execute("SELECT id, plan_id FROM actions").fetchall() == [(1, 1)]

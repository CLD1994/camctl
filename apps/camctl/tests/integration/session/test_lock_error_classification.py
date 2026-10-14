"""真实锁、事务与会话入口共同保留锁错误及数据库事实。"""

from __future__ import annotations

import asyncio
import errno
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Callable

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import AcceptanceStateError, CommandMode
from camctl.bootstrap import application
from camctl.bootstrap import lifecycle as lifecycle_module
from camctl.bootstrap.lifecycle import execute_command
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository
from camctl.persistence.runtime import (
    DbConfig,
    DbOpenMode,
    OwnedConnection,
    open_existing,
)
from camctl.reporting.policy import ReportingRepository, register_report_guards
from camctl.session import locks
from camctl.session import service as session_service
from camctl.session.service import run_session

from ..acceptance.test_acceptance import Catalog, _plan_body
from ..acceptance.test_atomicity import _process
from .test_session import FakeClock, _MIN, _facts, environment
from .test_submit_boundary import runtime


class _FaultingLockBackend:
    """仅在真实 LockBackend 的指定系统调用边界交付一次故障。"""

    def __init__(
        self,
        operation: str,
        failure: BaseException,
        *,
        on_try_lock: Callable[[], None] | None = None,
        close_failure: BaseException | None = None,
    ) -> None:
        self.delegate = locks.default_backend()
        self.operation = operation
        self.failure = failure
        self.on_try_lock = on_try_lock
        self.close_failure = close_failure
        self.calls = {"open": 0, "try_lock": 0, "unlock": 0, "close": 0}

    def open(self, path):
        self.calls["open"] += 1
        return self.delegate.open(path)

    def set_inheritable(self, fd, inheritable):
        return self.delegate.set_inheritable(fd, inheritable)

    def try_lock(self, fd):
        self.calls["try_lock"] += 1
        if self.operation == "try_lock":
            if self.on_try_lock is not None:
                self.on_try_lock()
            raise self.failure
        return self.delegate.try_lock(fd)

    def unlock(self, fd):
        self.calls["unlock"] += 1
        if self.operation == "unlock":
            raise self.failure
        return self.delegate.unlock(fd)

    def close(self, fd):
        self.calls["close"] += 1
        result = self.delegate.close(fd)
        if self.close_failure is not None:
            raise self.close_failure
        return result


class _ConnectionProxy:
    """透传真实 SQLite；可观测事务边界或使 ROLLBACK 返回错误。"""

    def __init__(
        self,
        connection,
        *,
        fail_rollbacks: int = 0,
        rollback_error: sqlite3.Error | None = None,
        fail_rollback_if: Callable[[], bool] | None = None,
    ) -> None:
        self.connection = connection
        self.fail_rollbacks = fail_rollbacks
        self.rollback_error = rollback_error
        self.fail_rollback_if = fail_rollback_if
        self.rollback_attempts = 0
        self.statements: list[str] = []

    def execute(self, statement, parameters=()):
        self.statements.append(statement)
        if statement == "ROLLBACK":
            self.rollback_attempts += 1
            if (self.rollback_attempts <= self.fail_rollbacks
                    or (self.fail_rollback_if is not None
                        and self.fail_rollback_if())):
                if self.rollback_error is None:
                    raise sqlite3.OperationalError("ROLLBACK 未可靠完成")
                raise self.rollback_error
        return self.connection.execute(statement, parameters)

    def close(self) -> None:
        self.connection.close()

    def __getattr__(self, name):
        return getattr(self.connection, name)


def _os_error(path: Path, error_number: int = errno.EACCES) -> OSError:
    return OSError(error_number, "受控锁系统调用错误", str(path))


def _assert_lock_outcome(
    outcome,
    path: Path,
    error_number: int,
    *,
    transaction_status: str | None,
    stage: str = "locking",
) -> None:
    assert outcome.succeeded is False
    assert outcome.reason == "lock_error"
    details = dict(outcome.details)
    assert details["stage"] == stage
    assert details["path"] == str(path)
    assert details["errno"] == error_number
    if transaction_status is None:
        assert "transaction_status" not in details
    else:
        assert details["transaction_status"] == transaction_status


def _database_snapshot(connection) -> tuple:
    """比较接纳故障前后的计划、动作、ACK 与权威历史事实。"""
    tables = (
        "plans",
        "actions",
        "auto_preview_links",
        "action_dependencies",
        "plan_file_diagnostics",
        "runtime_state",
        "history_transactions",
        "history_events",
        "entity_event_links",
        "report_entity_changes",
    )
    return tuple(
        (table, tuple(connection.execute(
            f"SELECT * FROM {table} ORDER BY rowid"
        ).fetchall()))
        for table in tables
    )


def _database_snapshot_at(path: Path) -> tuple:
    with closing(sqlite3.connect(path)) as connection:
        return _database_snapshot(connection)


@pytest.fixture
def lock_environment(environment):
    """给真实会话仓储提供其 facts_query 所需的 connection 签名。"""
    context, recorder, holder, tmp_path = environment
    holder["facts"] = lambda connection: _facts()
    return context, recorder, holder, tmp_path


def _seed_acknowledgeable_report(runtime) -> int:
    register_report_guards()
    owned = open_existing(runtime.state_db, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        seeded = _process(_plan_body(request_id="41"), owned.connection)
        assert seeded.kind is DbOutcomeKind.COMPLETED
        report = ReportingRepository().freeze_report(
            new_operation_key(), owned, occurred_at=1,
        )
        assert report.kind is DbOutcomeKind.COMPLETED
        return report.value.report.report_id
    finally:
        owned.connection.close()


def _watch_acceptance_error(monkeypatch) -> list[AcceptanceStateError]:
    original = session_service.accept_input
    seen: list[AcceptanceStateError] = []

    async def observe(*args, **kwargs):
        try:
            return await original(*args, **kwargs)
        except AcceptanceStateError as error:
            seen.append(error)
            raise

    monkeypatch.setattr(session_service, "accept_input", observe)
    return seen


def _receipt_for_control(control: BaseException, expected_kind: str):
    from camctl.persistence import transaction as transaction_module

    receipt_reader = getattr(transaction_module, "write_receipt_for", None)
    assert callable(receipt_reader)
    receipt = receipt_reader(control)
    assert receipt is not None
    assert receipt.kind == expected_kind
    assert receipt.error is control
    return receipt


def _wrap_runtime_connection(monkeypatch, factory):
    original = lifecycle_module._open_runtime_connection
    proxies: list[_ConnectionProxy] = []

    def open_wrapped(deps):
        owned = original(deps)
        proxy = factory(owned.connection)
        proxies.append(proxy)
        return OwnedConnection(proxy, owned.metadata)

    monkeypatch.setattr(lifecycle_module, "_open_runtime_connection", open_wrapped)
    return proxies


def _wrap_context_connection(context, holder, factory) -> list[_ConnectionProxy]:
    original = context.open_connection
    proxies: list[_ConnectionProxy] = []

    def open_wrapped():
        owned = original()
        proxy = factory(owned.connection)
        proxies.append(proxy)
        wrapped = OwnedConnection(proxy, owned.metadata)
        holder["connection"] = wrapped
        return wrapped

    context.open_connection = open_wrapped
    return proxies


def _acquire_tracked_admission(holder, path: Path, backend=None):
    lease = locks.acquire_admission(path, backend=backend)
    holder["admission_lease"] = lease
    return lease


@pytest.mark.asyncio
async def test_session_open_failure_is_lock_error(environment):
    context, _, holder, _ = environment
    path = context.paths.session_lock
    failure = _os_error(path)
    backend = _FaultingLockBackend("try_lock", failure)
    flows: list[str] = []

    context.acquire_session = lambda: locks.acquire_session(path, backend)
    context.flows = {"ordinary": lambda current: flows.append("ordinary")}

    outcome = await run_session(context, None)

    _assert_lock_outcome(outcome, path, errno.EACCES, transaction_status=None)
    assert flows == []
    assert holder["connection"] is None
    assert backend.calls["try_lock"] == backend.calls["close"] == 1


@pytest.mark.asyncio
async def test_session_acquire_double_error_keeps_all_facts(
    environment,
    monkeypatch,
):
    context, _, holder, _ = environment
    path = context.paths.session_lock
    primary_error = OSError(
        errno.EIO, "session acquisition primary marker", str(path),
    )
    close_error = OSError(
        errno.EBADF, "session acquisition close marker", str(path),
    )
    backend = _FaultingLockBackend(
        "try_lock", primary_error, close_failure=close_error,
    )
    lock_errors: list[BaseException] = []

    def acquire_session():
        try:
            return locks.acquire_session(path, backend=backend)
        except BaseException as error:
            lock_errors.append(error)
            raise

    context.acquire_session = acquire_session
    input_calls: list[str] = []
    startup_calls: list[str] = []
    flow_calls: list[str] = []
    emergency_calls: list[str] = []
    original_accept_input = session_service.accept_input

    async def observe_accept_input(*args, **kwargs):
        input_calls.append("accept_input")
        return await original_accept_input(*args, **kwargs)

    async def on_session_locked():
        startup_calls.append("session-locked")

    async def ordinary(current):
        flow_calls.append("ordinary")

    async def emergency_settlement():
        emergency_calls.append("emergency")
        return []

    monkeypatch.setattr(session_service, "accept_input", observe_accept_input)
    context.on_session_locked = on_session_locked
    context.flows = {"ordinary": ordinary}
    context.emergency_settlement = emergency_settlement
    outcome = await run_session(
        context, ParsedInput("plan.json", _plan_body()),
    )

    assert outcome.succeeded is False
    assert outcome.reason == "lock_error"
    assert outcome.needs_run is None
    details = dict(outcome.details)
    assert details["stage"] == "locking"
    assert details["path"] == str(path)
    assert details["errno"] == errno.EIO
    assert "transaction_status" not in details
    assert locks.LockOperation.TRY_LOCK.value in details["message"]
    secondary = details["secondary_errors"]
    assert len(secondary) == 1
    assert secondary[0]["reason"] == "lock_error"
    close_details = secondary[0]["details"]
    assert close_details["stage"] == "locking"
    assert close_details["path"] == str(path)
    assert close_details["errno"] == errno.EBADF
    assert locks.LockOperation.CLOSE.value in close_details["message"]
    assert str(close_error) in close_details["message"]
    assert "secondary_errors" not in close_details
    json.dumps(details)

    assert len(lock_errors) == 1
    lock_error = lock_errors[0]
    assert isinstance(lock_error, locks.LockError)
    assert lock_error.__cause__ is primary_error
    facts = locks.lock_error_facts(lock_error)
    assert facts is not None
    assert facts.operation is locks.LockOperation.TRY_LOCK
    assert facts.path == path
    assert facts.original_error is primary_error
    assert len(facts.secondary_errors) == 1
    close_facts = facts.secondary_errors[0]
    assert close_facts.operation is locks.LockOperation.CLOSE
    assert close_facts.path == path
    assert close_facts.original_error is close_error
    assert backend.calls == {"open": 1, "try_lock": 1, "unlock": 0, "close": 1}
    assert holder["connection"] is None
    assert input_calls == []
    assert startup_calls == []
    assert flow_calls == []
    assert emergency_calls == []
    session_lease = locks.acquire_session(path)
    session_lease.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "entry_point",
        "expected_stage",
        "expected_transaction_status",
        "expected_operation",
        "expected_calls",
    ),
    [
        (
            "session_acquire", "locking", None, locks.LockOperation.TRY_LOCK,
            (1, 0, 1),
        ),
        (
            "admission_acquire", "locking", "not_committed",
            locks.LockOperation.TRY_LOCK, (1, 0, 1),
        ),
        (
            "normal_close", "handoff", "not_committed",
            locks.LockOperation.UNLOCK, (1, 1, 1),
        ),
    ],
    ids=["session-acquire", "admission-acquire", "normal-close"],
)
async def test_run_lock_program_error_is_internal_error(
    lock_environment,
    entry_point,
    expected_stage,
    expected_transaction_status,
    expected_operation,
    expected_calls,
):
    context, _, holder, _ = lock_environment
    failure = RuntimeError(f"controlled {entry_point} program fault")
    failure.add_note("preserve original lock failure note")
    ordinary_calls: list[str] = []

    async def ordinary(current):
        ordinary_calls.append("ordinary")

    context.flows = {"ordinary": ordinary}
    if entry_point == "session_acquire":
        path = context.paths.session_lock
        backend = _FaultingLockBackend("try_lock", failure)
        context.acquire_session = lambda: locks.acquire_session(
            path, backend=backend,
        )
    elif entry_point == "admission_acquire":
        path = context.paths.admission_lock
        backend = _FaultingLockBackend("try_lock", failure)
        context.acquire_admission = lambda: _acquire_tracked_admission(
            holder, path, backend,
        )
    else:
        assert entry_point == "normal_close"
        path = context.paths.admission_lock
        backend = _FaultingLockBackend("unlock", failure)
        context.acquire_admission = lambda: _acquire_tracked_admission(
            holder, path, backend,
        )

    outcome = await run_session(context, None)

    assert outcome.succeeded is False
    assert outcome.reason == "internal_error"
    assert outcome.needs_run is None
    details = dict(outcome.details)
    assert details["stage"] == expected_stage
    assert details["path"] == str(path)
    assert "errno" not in details
    if expected_transaction_status is None:
        assert "transaction_status" not in details
    else:
        assert details["transaction_status"] == expected_transaction_status
    assert expected_operation.value in details["message"]
    assert details.get("secondary_errors", []) == []
    json.dumps(details)

    facts = locks.lock_error_facts(failure)
    assert facts is not None
    assert facts.operation is expected_operation
    assert facts.path == path
    assert facts.original_error is failure
    assert tuple(failure.__notes__) == ("preserve original lock failure note",)
    assert backend.calls["open"] == 1
    assert (
        backend.calls["try_lock"],
        backend.calls["unlock"],
        backend.calls["close"],
    ) == expected_calls
    assert ordinary_calls == (
        ["ordinary"] if entry_point == "normal_close" else []
    )

    if entry_point == "session_acquire":
        assert holder["connection"] is None
        assert holder["admission_lease"] is None
    else:
        assert holder["connection"] is not None
        if entry_point == "admission_acquire":
            assert holder["admission_lease"] is None
        else:
            assert holder["admission_lease"] is not None
        with pytest.raises(sqlite3.ProgrammingError):
            holder["connection"].connection.execute("SELECT 1")
    if entry_point == "normal_close":
        assert holder["admission_lease"] is not None
        assert locks.probe_admission(path).is_free
        assert backend.calls["unlock"] == expected_calls[1]
        assert backend.calls["close"] == expected_calls[2]

    session_lease = locks.acquire_session(context.paths.session_lock)
    session_lease.close()


@pytest.mark.asyncio
async def test_admission_acquire_failure_is_lock_error(lock_environment):
    context, _, _, _ = lock_environment
    path = context.paths.admission_lock
    failure = _os_error(path)
    backend = _FaultingLockBackend("try_lock", failure)
    flows: list[str] = []

    context.acquire_admission = lambda: locks.acquire_admission(path, backend)
    context.flows = {"ordinary": lambda current: flows.append("ordinary")}

    outcome = await run_session(context, None)

    _assert_lock_outcome(
        outcome, path, errno.EACCES, transaction_status="not_committed",
        stage="locking",
    )
    assert flows == []
    assert backend.calls["try_lock"] == backend.calls["close"] == 1
    assert locks.probe_admission(path).is_free


@pytest.mark.asyncio
async def test_admission_acquire_control_keeps_transaction_receipt(lock_environment):
    context, _, holder, tmp_path = lock_environment
    path = context.paths.admission_lock
    control = asyncio.CancelledError("synchronous admission acquire control")
    observed: list[tuple[bool, tuple]] = []
    sweep_cancellations: list[str] = []

    class StartupSweep:
        def cancel_startup_sweep(self):
            sweep_cancellations.append("cancelled")

    def observe_lock_attempt():
        connection = holder["connection"].connection
        observed.append((connection.in_transaction, _database_snapshot(connection)))

    backend = _FaultingLockBackend(
        "try_lock", control, on_try_lock=observe_lock_attempt,
    )
    context.logging_owner = StartupSweep()
    context.acquire_admission = lambda: locks.acquire_admission(path, backend)
    ordinary_calls: list[str] = []
    context.flows = {"ordinary": lambda current: ordinary_calls.append("ordinary")}
    proxies = _wrap_context_connection(
        context, holder, lambda connection: _ConnectionProxy(connection),
    )

    with pytest.raises(asyncio.CancelledError) as raised:
        await run_session(context, None)

    assert raised.value is control
    assert observed and observed[0][0] is True
    assert ordinary_calls == []
    assert sweep_cancellations == []
    facts = locks.lock_error_facts(control)
    assert facts is not None
    assert facts.operation is locks.LockOperation.TRY_LOCK
    assert facts.path == path
    assert facts.original_error is control
    receipt = _receipt_for_control(control, "rolled_back")
    assert receipt.secondary_errors == ()
    assert holder["admission_lease"] is None
    assert backend.calls["try_lock"] == backend.calls["close"] == 1
    assert backend.calls["unlock"] == 0
    assert proxies[0].rollback_attempts == 1
    assert _database_snapshot_at(tmp_path / "state.db") == observed[0][1]
    assert locks.probe_admission(path).is_free
    session_lease = locks.acquire_session(context.paths.session_lock)
    session_lease.close()
    with pytest.raises(sqlite3.ProgrammingError):
        holder["connection"].connection.execute("SELECT 1")


@pytest.mark.asyncio
async def test_submit_probe_failure_rolls_back_with_lock_error(
    runtime, monkeypatch,
):
    report_id = _seed_acknowledgeable_report(runtime)
    before = _database_snapshot_at(runtime.state_db)
    path = runtime.admission_lock
    failure = _os_error(path)
    backend = _FaultingLockBackend("try_lock", failure)
    seen = _watch_acceptance_error(monkeypatch)

    monkeypatch.setattr(
        session_service,
        "probe_admission",
        lambda lock_path: locks.probe_admission(lock_path, backend=backend),
    )
    outcome = await execute_command(
        runtime,
        ParsedInput("ack.json", _plan_body(request_id="42", ack=str(report_id))),
    )

    _assert_lock_outcome(
        outcome, path, errno.EACCES, transaction_status="not_committed",
        stage="handoff",
    )
    assert outcome.needs_run is None
    assert len(seen) == 1
    assert seen[0].outcome_kind is DbOutcomeKind.ROLLED_BACK
    cause = seen[0].__cause__
    assert isinstance(cause, locks.LockError)
    assert cause.__cause__ is failure
    facts = locks.lock_error_facts(cause)
    assert facts is not None
    assert facts.operation is locks.LockOperation.TRY_LOCK
    assert facts.path == path
    assert facts.errno == errno.EACCES
    assert _database_snapshot_at(runtime.state_db) == before
    assert backend.calls["try_lock"] == backend.calls["close"] == 1


@pytest.mark.asyncio
async def test_submit_probe_control_keeps_transaction_receipt(runtime, monkeypatch):
    report_id = _seed_acknowledgeable_report(runtime)
    before = _database_snapshot_at(runtime.state_db)
    path = runtime.admission_lock
    control = asyncio.CancelledError("synchronous submit probe control")
    backend = _FaultingLockBackend("try_lock", control)
    monkeypatch.setattr(
        session_service,
        "probe_admission",
        lambda lock_path: locks.probe_admission(lock_path, backend=backend),
    )
    ordinary_calls: list[str] = []

    async def ordinary(current):
        ordinary_calls.append("ordinary")

    with pytest.raises(asyncio.CancelledError) as raised:
        await execute_command(
            runtime,
            ParsedInput(
                "ack.json",
                _plan_body(request_id="42", ack=str(report_id)),
            ),
            flows={"ordinary": ordinary},
        )

    assert raised.value is control
    assert ordinary_calls == []
    facts = locks.lock_error_facts(control)
    assert facts is not None
    assert facts.operation is locks.LockOperation.TRY_LOCK
    assert facts.path == path
    assert facts.original_error is control
    receipt = _receipt_for_control(control, "rolled_back")
    assert receipt.secondary_errors == ()
    assert _database_snapshot_at(runtime.state_db) == before
    assert backend.calls["try_lock"] == backend.calls["close"] == 1
    assert backend.calls["unlock"] == 0
    assert locks.probe_admission(path).is_free


@pytest.mark.asyncio
async def test_submit_conflict_remains_success(runtime):
    lease = locks.acquire_admission(runtime.admission_lock)
    try:
        outcome = await execute_command(
            runtime, ParsedInput("plan.json", _plan_body()),
        )
        assert outcome.succeeded is True
        assert outcome.needs_run is False
        with closing(sqlite3.connect(runtime.state_db)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 1
    finally:
        lease.close()


@pytest.mark.asyncio
async def test_query_failure_remains_state_db_error(runtime, monkeypatch):
    report_id = _seed_acknowledgeable_report(runtime)
    before = _database_snapshot_at(runtime.state_db)
    seen = _watch_acceptance_error(monkeypatch)

    query_errors: list[sqlite3.Error] = []

    def fail_with_sqlite_error(connection):
        try:
            connection.execute(
                "SELECT absent_column FROM runtime_state"
            ).fetchone()
        except sqlite3.Error as error:
            query_errors.append(error)
            raise

    monkeypatch.setattr(application, "query_work_facts", fail_with_sqlite_error)
    outcome = await execute_command(
        runtime,
        ParsedInput("ack.json", _plan_body(request_id="42", ack=str(report_id))),
    )

    assert outcome.succeeded is False
    assert outcome.reason == "state_db_error"
    assert outcome.needs_run is None
    details = dict(outcome.details)
    assert details["transaction_status"] == "not_committed"
    assert details["stage"] == "handoff"
    assert len(seen) == 1
    assert seen[0].outcome_kind is DbOutcomeKind.ROLLED_BACK
    assert len(query_errors) == 1
    assert seen[0].__cause__ is query_errors[0]
    assert locks.lock_error_facts(seen[0].__cause__) is None
    assert _database_snapshot_at(runtime.state_db) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fail_rollback", "expected_transaction_status", "expected_kind"),
    [
        (False, "not_committed", DbOutcomeKind.ROLLED_BACK),
        (True, "unknown", DbOutcomeKind.UNKNOWN),
    ],
    ids=["rollback-confirmed", "rollback-unknown"],
)
async def test_submit_lock_program_error_keeps_cause_and_transaction(
    runtime,
    monkeypatch,
    fail_rollback,
    expected_transaction_status,
    expected_kind,
):
    report_id = _seed_acknowledgeable_report(runtime)
    before = _database_snapshot_at(runtime.state_db)
    path = runtime.admission_lock
    failure = RuntimeError("controlled submit probe program fault")
    failure.add_note("preserve original probe failure note")
    backend = _FaultingLockBackend("try_lock", failure)
    rollback_error = sqlite3.OperationalError(
        "submit probe rollback result not confirmed"
    )
    seen = _watch_acceptance_error(monkeypatch)
    proxies = _wrap_runtime_connection(
        monkeypatch,
        lambda connection: _ConnectionProxy(
            connection,
            fail_rollbacks=1 if fail_rollback else 0,
            rollback_error=rollback_error,
        ),
    )

    monkeypatch.setattr(
        session_service,
        "probe_admission",
        lambda lock_path: locks.probe_admission(lock_path, backend=backend),
    )
    outcome = await execute_command(
        runtime,
        ParsedInput("ack.json", _plan_body(request_id="42", ack=str(report_id))),
    )

    assert outcome.succeeded is False
    assert outcome.reason == "internal_error"
    assert outcome.needs_run is None
    details = dict(outcome.details)
    assert details["stage"] == "handoff"
    assert details["path"] == str(path)
    assert "errno" not in details
    assert details["transaction_status"] == expected_transaction_status
    assert locks.LockOperation.TRY_LOCK.value in details["message"]
    json.dumps(details)

    assert len(seen) == 1
    acceptance_error = seen[0]
    assert acceptance_error.outcome_kind is expected_kind
    cause = acceptance_error.__cause__
    if fail_rollback:
        assert isinstance(cause, ExceptionGroup)
        assert len(cause.exceptions) == 2
        assert cause.exceptions[0] is failure
        assert cause.exceptions[1] is rollback_error
        secondary = details["secondary_errors"]
        assert len(secondary) == 1
        assert secondary[0]["reason"] == "state_db_error"
        assert "secondary_errors" not in secondary[0]["details"]
        assert rollback_error.args[0] in secondary[0]["details"]["message"]
    else:
        assert cause is failure
        assert details.get("secondary_errors", []) == []
    facts = locks.lock_error_facts(failure)
    assert facts is not None
    assert facts.operation is locks.LockOperation.TRY_LOCK
    assert facts.path == path
    assert facts.original_error is failure
    assert facts.secondary_errors == ()
    assert tuple(failure.__notes__) == ("preserve original probe failure note",)
    assert backend.calls["open"] == backend.calls["try_lock"] == 1
    assert backend.calls["unlock"] == 0
    assert backend.calls["close"] == 1
    assert proxies[0].rollback_attempts == 1
    assert _database_snapshot_at(runtime.state_db) == before


@pytest.mark.asyncio
async def test_close_admission_lock_failure_is_not_database_failure(lock_environment):
    context, _, holder, _ = lock_environment
    path = context.paths.admission_lock
    failure = _os_error(path, errno.EIO)
    backend = _FaultingLockBackend("unlock", failure)
    context.acquire_admission = lambda: _acquire_tracked_admission(
        holder, path, backend,
    )

    async def ordinary(current):
        return None

    context.flows = {"ordinary": ordinary}
    outcome = await run_session(context, None)

    _assert_lock_outcome(
        outcome, path, errno.EIO, transaction_status="not_committed",
        stage="handoff",
    )
    details = dict(outcome.details)
    assert details.get("secondary_errors", []) == []
    assert backend.calls["unlock"] == backend.calls["close"] == 1
    assert holder["admission_lease"] is not None
    assert locks.probe_admission(path).is_free
    assert locks.acquire_session(context.paths.session_lock).close() is None
    with pytest.raises(sqlite3.ProgrammingError):
        holder["connection"].connection.execute("SELECT 1")


@pytest.mark.asyncio
async def test_lock_failure_and_rollback_failure_keep_unknown_transaction(
    runtime, monkeypatch,
):
    report_id = _seed_acknowledgeable_report(runtime)
    before = _database_snapshot_at(runtime.state_db)
    path = runtime.admission_lock
    failure = _os_error(path)
    rollback_error = sqlite3.OperationalError("rollback result not confirmed")
    backend = _FaultingLockBackend("try_lock", failure)
    seen = _watch_acceptance_error(monkeypatch)
    proxies = _wrap_runtime_connection(
        monkeypatch,
        lambda connection: _ConnectionProxy(
            connection, fail_rollbacks=1, rollback_error=rollback_error,
        ),
    )

    monkeypatch.setattr(
        session_service,
        "probe_admission",
        lambda lock_path: locks.probe_admission(lock_path, backend=backend),
    )
    outcome = await execute_command(
        runtime,
        ParsedInput("ack.json", _plan_body(request_id="42", ack=str(report_id))),
    )

    _assert_lock_outcome(
        outcome, path, errno.EACCES, transaction_status="unknown",
        stage="handoff",
    )
    assert outcome.needs_run is None
    assert len(seen) == 1
    acceptance_error = seen[0]
    assert acceptance_error.outcome_kind is DbOutcomeKind.UNKNOWN
    cause = acceptance_error.__cause__
    assert isinstance(cause, ExceptionGroup)
    assert len(cause.exceptions) == 2
    lock_error, saved_rollback_error = cause.exceptions
    assert isinstance(lock_error, locks.LockError)
    assert lock_error.__cause__ is failure
    assert saved_rollback_error is rollback_error
    secondary = dict(outcome.details).get("secondary_errors", [])
    assert len(secondary) == 1
    assert secondary[0]["reason"] == "state_db_error"
    assert "secondary_errors" not in secondary[0]["details"]
    assert proxies[0].rollback_attempts == 1
    assert _database_snapshot_at(runtime.state_db) == before


@pytest.mark.asyncio
async def test_close_errors_keep_order_before_rollback_error_and_not_duplicated(
    lock_environment,
):
    context, _, holder, tmp_path = lock_environment
    context.catalog = Catalog()
    context.clock = FakeClock([_MIN + 60_000_000] * 6)
    path = context.paths.admission_lock
    initial_snapshot = _database_snapshot_at(tmp_path / "state.db")
    unlock_error = _os_error(path, errno.EIO)
    unlock_error.add_note("preserve original unlock failure note")
    close_error = _os_error(path, errno.EBADF)
    close_error.add_note("preserve actual close failure note")
    rollback_error = sqlite3.OperationalError(
        "normal close rollback result not confirmed"
    )
    backend = _FaultingLockBackend(
        "unlock", unlock_error, close_failure=close_error,
    )
    proxies = _wrap_context_connection(
        context,
        holder,
        lambda connection: _ConnectionProxy(
            connection,
            rollback_error=rollback_error,
            fail_rollback_if=lambda: backend.calls["unlock"] > 0,
        ),
    )
    ordinary_calls: list[str] = []
    flow_snapshots: list[tuple] = []
    close_outcomes = []
    lease_errors: list[BaseException] = []
    repository = context.session_repository
    original_close_admission = repository.close_admission

    def observe_close_admission(command, key, owned):
        outcome = original_close_admission(command, key, owned)
        close_outcomes.append(outcome)
        return outcome

    repository.close_admission = observe_close_admission

    def acquire_admission():
        lease = _acquire_tracked_admission(holder, path, backend)
        close = lease.close

        def observe_lease_close():
            try:
                close()
            except BaseException as error:
                lease_errors.append(error)
                raise

        lease.close = observe_lease_close
        return lease

    context.acquire_admission = acquire_admission

    async def ordinary(current):
        ordinary_calls.append("ordinary")
        flow_snapshots.append(
            _database_snapshot(holder["connection"].connection)
        )

    context.flows = {"ordinary": ordinary}
    outcome = await run_session(
        context, ParsedInput("plan.json", _plan_body()),
    )

    assert outcome.succeeded is False
    assert outcome.reason == "lock_error"
    assert outcome.needs_run is None
    details = dict(outcome.details)
    assert details["stage"] == "handoff"
    assert details["path"] == str(path)
    assert details["errno"] == errno.EIO
    assert details["transaction_status"] == "unknown"
    assert locks.LockOperation.UNLOCK.value in details["message"]
    secondary = details["secondary_errors"]
    assert len(secondary) == 2
    assert [item["reason"] for item in secondary] == [
        "lock_error", "state_db_error",
    ]
    assert secondary[0]["details"]["stage"] == "handoff"
    assert secondary[0]["details"]["path"] == str(path)
    assert secondary[0]["details"]["errno"] == errno.EBADF
    assert str(close_error) in secondary[0]["details"]["message"]
    assert secondary[1]["details"]["stage"] == "handoff"
    assert str(rollback_error) in secondary[1]["details"]["message"]
    assert all("secondary_errors" not in item["details"] for item in secondary)
    json.dumps(details)

    assert ordinary_calls == ["ordinary"]
    assert len(close_outcomes) == 1
    close_outcome = close_outcomes[0]
    assert close_outcome.kind is DbOutcomeKind.UNKNOWN
    assert isinstance(close_outcome.error, ExceptionGroup)
    assert len(close_outcome.error.exceptions) == 2
    lock_error, saved_rollback_error = close_outcome.error.exceptions
    assert isinstance(lock_error, locks.LockError)
    assert saved_rollback_error is rollback_error
    facts = locks.lock_error_facts(lock_error)
    assert facts is not None
    assert facts.operation is locks.LockOperation.UNLOCK
    assert facts.path == path
    assert facts.original_error is unlock_error
    assert len(facts.secondary_errors) == 1
    close_facts = facts.secondary_errors[0]
    assert close_facts.operation is locks.LockOperation.CLOSE
    assert close_facts.path == path
    assert close_facts.original_error is close_error
    assert tuple(unlock_error.__notes__) == (
        "preserve original unlock failure note",
    )
    assert tuple(close_error.__notes__) == ("preserve actual close failure note",)

    lease = holder["admission_lease"]
    assert lease is not None
    assert len(lease_errors) == 2
    assert lease_errors[0] is lock_error
    assert lease_errors[1] is lock_error
    assert backend.calls == {"open": 1, "try_lock": 1, "unlock": 1, "close": 1}
    assert proxies[0].rollback_attempts == 2
    assert proxies[0].statements.count("ROLLBACK") == 2
    assert _database_snapshot_at(tmp_path / "state.db") == flow_snapshots[0]
    assert flow_snapshots[0] != initial_snapshot
    assert locks.probe_admission(path).is_free
    with closing(sqlite3.connect(tmp_path / "state.db")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 1
    with pytest.raises(sqlite3.ProgrammingError):
        holder["connection"].connection.execute("SELECT 1")
    session_lease = locks.acquire_session(context.paths.session_lock)
    session_lease.close()


@pytest.mark.asyncio
async def test_run_acquires_admission_inside_short_transaction(lock_environment):
    context, _, holder, tmp_path = lock_environment
    path = context.paths.admission_lock
    backend = _FaultingLockBackend("none", RuntimeError("unused fault"))
    observations: list[tuple[bool, int, int, bool]] = []
    snapshots: list[tuple] = []
    flow_snapshots: list[tuple] = []

    def acquire_inside_transaction():
        owned = holder["connection"]
        connection = owned.connection
        statements = connection.statements
        begins = [i for i, statement in enumerate(statements)
                  if statement == "BEGIN IMMEDIATE"]
        assert begins
        lease = _acquire_tracked_admission(holder, path, backend)
        observations.append((
            connection.in_transaction,
            len(begins),
            sum(statement == "ROLLBACK" for statement in statements),
            holder["admission_lease"] is lease,
        ))
        snapshots.append(_database_snapshot(connection))
        return lease

    context.acquire_admission = acquire_inside_transaction
    proxies = _wrap_context_connection(
        context, holder, lambda connection: _ConnectionProxy(connection),
    )

    async def ordinary(current):
        connection = holder["connection"].connection
        assert connection.in_transaction is False
        assert holder["admission_lease"] is not None
        begins = sum(statement == "BEGIN IMMEDIATE" for statement in connection.statements)
        rollbacks = sum(statement == "ROLLBACK" for statement in connection.statements)
        assert begins == observations[0][1]
        assert rollbacks > observations[0][2]
        flow_snapshots.append(_database_snapshot(connection))

    context.flows = {"ordinary": ordinary}
    outcome = await run_session(context, None)

    assert outcome.succeeded is True
    in_transaction, begins, rollbacks, lease_registered = observations[0]
    assert in_transaction is True
    assert begins >= 1
    assert rollbacks >= 0
    assert lease_registered is True
    assert len(proxies) == 1
    assert flow_snapshots == [snapshots[0]]
    assert _database_snapshot_at(tmp_path / "state.db") == snapshots[0]
    assert len(flow_snapshots) == 1
    assert backend.calls["try_lock"] == 1
    assert backend.calls["unlock"] == backend.calls["close"] == 1
    assert locks.probe_admission(path).is_free


@pytest.mark.asyncio
async def test_acquired_lease_is_closed_when_handoff_transaction_fails(
    lock_environment,
):
    context, _, holder, tmp_path = lock_environment
    context.catalog = Catalog()
    context.clock = FakeClock([_MIN + 60_000_000] * 6)
    path = context.paths.admission_lock
    backend = _FaultingLockBackend("none", RuntimeError("unused fault"))
    callback_snapshots: list[tuple] = []
    callback_transactions: list[bool] = []

    def acquire_and_record():
        owned = holder["connection"]
        callback_transactions.append(owned.connection.in_transaction)
        callback_snapshots.append(_database_snapshot(owned.connection))
        return _acquire_tracked_admission(holder, path, backend)

    context.acquire_admission = acquire_and_record
    proxies = _wrap_context_connection(
        context,
        holder,
        lambda connection: _ConnectionProxy(connection, fail_rollbacks=1),
    )
    ordinary_calls: list[str] = []

    async def ordinary(current):
        ordinary_calls.append("ordinary")

    context.flows = {"ordinary": ordinary}
    outcome = await run_session(
        context, ParsedInput("plan.json", _plan_body()),
    )

    assert outcome.succeeded is False
    assert outcome.reason == "state_db_error"
    assert outcome.needs_run is None
    details = dict(outcome.details)
    assert details["transaction_status"] == "unknown"
    assert ordinary_calls == []
    assert callback_transactions == [True]
    assert holder["admission_lease"] is not None
    assert backend.calls["unlock"] == backend.calls["close"] == 1
    assert locks.probe_admission(path).is_free
    assert proxies[0].rollback_attempts == 1
    assert _database_snapshot_at(tmp_path / "state.db") == callback_snapshots[0]
    with closing(sqlite3.connect(tmp_path / "state.db")) as connection:
        assert connection.execute("SELECT request_id FROM plans").fetchall() == [(42,)]
        assert connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 1
        assert connection.execute(
            "SELECT trusted_time_lower_bound FROM runtime_state WHERE id = 1"
        ).fetchone()[0] is not None


@pytest.mark.asyncio
async def test_transaction_control_keeps_original_object_and_closes_lease(lock_environment):
    context, _, holder, _ = lock_environment
    path = context.paths.admission_lock
    control = asyncio.CancelledError("synchronous admission unlock control")
    backend = _FaultingLockBackend("unlock", control)
    sweep_cancellations: list[str] = []

    class StartupSweep:
        def cancel_startup_sweep(self):
            sweep_cancellations.append("cancelled")

    context.logging_owner = StartupSweep()
    context.acquire_admission = lambda: _acquire_tracked_admission(
        holder, path, backend,
    )
    ordinary_calls: list[str] = []

    async def ordinary(current):
        ordinary_calls.append("ordinary")

    context.flows = {"ordinary": ordinary}
    with pytest.raises(asyncio.CancelledError) as raised:
        await run_session(context, None)

    assert raised.value is control
    assert ordinary_calls == ["ordinary"]
    assert sweep_cancellations == []
    lock_facts = locks.lock_error_facts(control)
    assert lock_facts is not None
    assert lock_facts.operation is locks.LockOperation.UNLOCK
    assert lock_facts.path == path
    receipt = _receipt_for_control(control, "rolled_back")
    assert receipt.secondary_errors == ()
    assert backend.calls["unlock"] == backend.calls["close"] == 1
    assert holder["admission_lease"] is not None
    assert locks.probe_admission(path).is_free
    session_lease = locks.acquire_session(context.paths.session_lock)
    session_lease.close()
    with pytest.raises(sqlite3.ProgrammingError):
        holder["connection"].connection.execute("SELECT 1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "admission_control_precedes_cleanup",
    [False, True],
    ids=["ordinary-success", "admission-control-first"],
)
async def test_final_lock_double_error_keeps_all_facts(
    lock_environment,
    admission_control_precedes_cleanup,
):
    context, _, holder, _ = lock_environment
    session_path = context.paths.session_lock
    admission_path = context.paths.admission_lock
    unlock_error = OSError(
        errno.EIO, "final session lease unlock marker", str(session_path),
    )
    close_error = OSError(
        errno.EBADF, "final session lease close marker", str(session_path),
    )
    session_backend = _FaultingLockBackend(
        "unlock", unlock_error, close_failure=close_error,
    )
    context.acquire_session = lambda: locks.acquire_session(
        session_path, backend=session_backend,
    )
    ordinary_calls: list[str] = []
    emergency_calls: list[str] = []
    transaction_observations: list[bool] = []

    async def ordinary(current):
        ordinary_calls.append("ordinary")

    async def emergency_settlement():
        emergency_calls.append("emergency")
        return []

    context.flows = {"ordinary": ordinary}
    context.emergency_settlement = emergency_settlement
    proxies: list[_ConnectionProxy] = []
    control: asyncio.CancelledError | None = None
    admission_backend: _FaultingLockBackend | None = None
    if admission_control_precedes_cleanup:
        control = asyncio.CancelledError("admission acquisition control marker")
        control.add_note("preserve original admission control note")

        def observe_admission_transaction():
            transaction_observations.append(
                holder["connection"].connection.in_transaction
            )

        admission_backend = _FaultingLockBackend(
            "try_lock", control, on_try_lock=observe_admission_transaction,
        )
        context.acquire_admission = lambda: locks.acquire_admission(
            admission_path, backend=admission_backend,
        )
        proxies = _wrap_context_connection(
            context, holder,
            lambda connection: _ConnectionProxy(connection),
        )

    if admission_control_precedes_cleanup:
        assert control is not None
        with pytest.raises(asyncio.CancelledError) as raised:
            await run_session(context, None)
        assert raised.value is control
        assert transaction_observations == [True]
        assert ordinary_calls == []
        assert holder["admission_lease"] is None
        assert admission_backend is not None
        assert admission_backend.calls == {
            "open": 1, "try_lock": 1, "unlock": 0, "close": 1,
        }
        assert proxies[0].rollback_attempts == 1
        receipt = _receipt_for_control(control, "rolled_back")
        assert receipt.secondary_errors == ()
        control_facts = locks.lock_error_facts(control)
        assert control_facts is not None
        assert control_facts.operation is locks.LockOperation.TRY_LOCK
        assert control_facts.path == admission_path
        assert control_facts.original_error is control
        assert tuple(control.__notes__)[0] == (
            "preserve original admission control note"
        )
        notes = "\n".join(control.__notes__)
        assert str(unlock_error) in notes
        assert str(close_error) in notes
    else:
        outcome = await run_session(context, None)
        assert outcome.succeeded is False
        assert outcome.reason == "lock_error"
        assert outcome.needs_run is None
        details = dict(outcome.details)
        assert details["stage"] == "shutdown"
        assert details["path"] == str(session_path)
        assert details["errno"] == errno.EIO
        assert "transaction_status" not in details
        assert locks.LockOperation.UNLOCK.value in details["message"]
        secondary = details["secondary_errors"]
        assert len(secondary) == 1
        assert secondary[0]["reason"] == "lock_error"
        close_details = secondary[0]["details"]
        assert close_details["stage"] == "shutdown"
        assert close_details["path"] == str(session_path)
        assert close_details["errno"] == errno.EBADF
        assert locks.LockOperation.CLOSE.value in close_details["message"]
        assert str(close_error) in close_details["message"]
        assert "secondary_errors" not in close_details
        json.dumps(details)
        assert ordinary_calls == ["ordinary"]

    assert emergency_calls == []
    assert session_backend.calls == {
        "open": 1, "try_lock": 1, "unlock": 1, "close": 1,
    }
    if not admission_control_precedes_cleanup:
        assert holder["admission_lease"] is not None
    assert locks.probe_admission(admission_path).is_free
    session_lease = locks.acquire_session(session_path)
    session_lease.close()
    assert holder["connection"] is not None
    with pytest.raises(sqlite3.ProgrammingError):
        holder["connection"].connection.execute("SELECT 1")

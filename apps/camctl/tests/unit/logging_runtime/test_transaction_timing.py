"""事务诊断聚合实际调用成本，不执行真实 SQLite 或文件操作。"""

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.logging_runtime import transaction_timing as timing
from camctl.logging_runtime.performance import performance_logging
from camctl.logging_runtime.service import LogChannel


@pytest.fixture
def clock(monkeypatch):
    value = SimpleNamespace(wall=0, cpu=0)
    monkeypatch.setattr(timing, "monotonic_ns", lambda: value.wall)
    monkeypatch.setattr(timing, "thread_time_ns", lambda: value.cpu)
    def advance(wall, cpu):
        value.wall += wall * 1_000_000
        value.cpu += cpu * 1_000_000
    return advance


def _channel():
    return create_autospec(LogChannel, instance=True, spec_set=True)


def _payload(channel):
    assert channel.slog.call_count == 1
    return json.loads(channel.slog.call_args.args[0].message.removeprefix("性能计时 "))


def test_phases_are_exclusive_and_details_are_inclusive(clock):
    channel = _channel()
    receipt = SimpleNamespace(kind="completed")
    @timing.trace_transaction
    def write(command, operation_key, owned):
        with timing.db_phase(timing.TransactionPhase.PLAN):
            clock(2, 1)
            with timing.db_phase(timing.TransactionPhase.VALIDATE):
                with timing.transaction_detail("outer"):
                    clock(1, 1)
                    with timing.transaction_detail("inner"):
                        clock(2, 1)
            timing.db_count("events", 3)
        return receipt
    with performance_logging(channel), timing.db_operation_context(action_id=15, method="finish"):
        assert write(object(), "original-key", None) is receipt
    result = _payload(channel)
    assert (result["stage"], result["elapsed_ms"], result["cpu_ms"]) == ("db_transaction", 5, 3)
    assert result["phases"]["plan"]["elapsed_ms"] == 2
    assert result["phases"]["event_validation"]["elapsed_ms"] == 3
    assert result["details"]["outer"]["elapsed_ms"] == 3
    assert result["details"]["inner"]["elapsed_ms"] == 2
    assert result["details_inclusive"] is True
    assert result["counts"] == {"events": 3}
    assert (result["operation_key"], result["action_id"], result["method"], result["outcome"]) == (
        "original-key", 15, "finish", "completed")


def test_disabled_diagnostics_keep_original_connection_and_do_not_read_clocks(monkeypatch):
    def forbidden():
        raise AssertionError("inactive timing read a clock")
    monkeypatch.setattr(timing, "monotonic_ns", forbidden)
    monkeypatch.setattr(timing, "thread_time_ns", forbidden)
    raw = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    @timing.trace_transaction
    def write(command, operation_key, owned):
        with timing.db_phase(timing.TransactionPhase.PLAN), timing.transaction_detail("history"):
            assert timing.timed_connection(raw) is raw
        return "original"
    assert write(None, "key", None) == "original"


@pytest.mark.parametrize("failure", [ValueError("original"), KeyboardInterrupt("original")])
def test_original_exception_and_failed_phase_survive(clock, failure):
    channel = _channel()
    @timing.trace_transaction
    def write(command, operation_key, owned):
        with timing.db_phase(timing.TransactionPhase.COMMIT):
            clock(4, 1)
            raise failure
    with performance_logging(channel), pytest.raises(type(failure)) as caught:
        write(None, "key", None)
    assert caught.value is failure
    assert _payload(channel)["phases"]["commit"]["elapsed_ms"] == 4


def test_sql_execute_and_fetch_are_forwarded_once_and_hide_statement_data(clock):
    channel = _channel()
    raw = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
    cursor.rowcount = -1
    row, remaining = (1,), [(2,), (3,)]
    def execute(*args):
        clock(3, 1)
        return cursor
    def fetchone():
        clock(2, 1)
        return row
    def fetchall():
        clock(4, 2)
        return remaining
    raw.execute.side_effect = execute
    cursor.fetchone.side_effect = fetchone
    cursor.fetchall.side_effect = fetchall
    @timing.trace_transaction
    def write(command, operation_key, owned):
        with timing.db_phase(timing.TransactionPhase.PLAN):
            measured = timing.timed_connection(raw)
            result = measured.execute("SELECT private_field WHERE token=?", ("private-value",))
            assert result.fetchone() is row
            assert result.fetchall() is remaining
            result.close()
        return SimpleNamespace(kind="completed")
    with performance_logging(channel):
        write(None, "key", None)
    raw.execute.assert_called_once_with("SELECT private_field WHERE token=?", ("private-value",))
    cursor.close.assert_called_once_with()
    result = _payload(channel)
    assert result["sql"]["execute_calls"] == 1
    assert result["sql"]["fetch_calls"] == 2
    assert result["sql"]["rows_read"] == 3
    assert (result["sql"]["elapsed_ms"], result["sql"]["cpu_ms"]) == (9, 4)
    assert result["phases"]["plan"]["sql"] == result["sql"]
    assert "private" not in channel.slog.call_args.args[0].message


def test_explicit_cursor_execute_and_attributes_preserve_sqlite_contract(clock):
    channel = _channel()
    raw = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
    cursor.rowcount = 2
    cursor.execute.return_value = cursor
    raw.cursor.return_value = cursor
    @timing.trace_transaction
    def write(command, operation_key, owned):
        result = timing.timed_connection(raw).cursor()
        result.arraysize = 7
        assert result.execute("UPDATE sample SET value=?", (1,)) is result
        assert cursor.arraysize == 7
        return SimpleNamespace(kind="completed")
    with performance_logging(channel):
        write(None, "key", None)
    assert _payload(channel)["sql"]["rows_written"] == 2


def test_logging_failure_does_not_replace_original_result(clock):
    channel = _channel()
    channel.slog.side_effect = RuntimeError("log unavailable")
    receipt = SimpleNamespace(kind="unknown")
    @timing.trace_transaction
    def write(command, operation_key, owned):
        return receipt
    with performance_logging(channel):
        assert write(None, "key", None) is receipt


def test_executemany_does_not_eagerly_consume_parameters(clock):
    channel = _channel()
    raw = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
    cursor.rowcount = 2
    produced, observed = [], []
    def parameters():
        for value in (1, 2):
            produced.append(value)
            yield (value,)
    original = parameters()
    def execute(statement, values):
        assert values is original and not produced
        observed.extend(values)
        clock(5, 2)
        return cursor
    raw.executemany.side_effect = execute
    @timing.trace_transaction
    def write(command, operation_key, owned):
        timing.timed_connection(raw).executemany("INSERT INTO sample VALUES(?)", original)
        return SimpleNamespace(kind="completed")
    with performance_logging(channel):
        write(None, "key", None)
    assert observed == [(1,), (2,)]
    sql = _payload(channel)["sql"]
    assert (sql["executemany_calls"], sql["rows_written"], sql["elapsed_ms"]) == (1, 2, 5)


def test_cursor_iteration_reads_only_when_requested(clock):
    channel = _channel()
    raw = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
    cursor.rowcount = -1
    raw.execute.return_value = cursor
    consumed = []
    def rows():
        for value in (1, 2):
            consumed.append(value)
            clock(2, 1)
            yield (value,)
    cursor.__iter__.side_effect = rows
    @timing.trace_transaction
    def write(command, operation_key, owned):
        result = timing.timed_connection(raw).execute("SELECT value FROM sample")
        assert not consumed
        assert list(result) == [(1,), (2,)]
        return SimpleNamespace(kind="completed")
    with performance_logging(channel):
        write(None, "key", None)
    sql = _payload(channel)["sql"]
    assert (sql["fetch_calls"], sql["rows_read"], sql["elapsed_ms"]) == (3, 2, 4)


def test_sql_failure_keeps_original_error_and_records_elapsed(clock):
    channel = _channel()
    raw = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    original = sqlite3.OperationalError("original")
    def execute(*args):
        clock(9, 1)
        raise original
    raw.execute.side_effect = execute
    @timing.trace_transaction
    def write(command, operation_key, owned):
        with timing.db_phase(timing.TransactionPhase.BEGIN):
            timing.timed_connection(raw).execute("BEGIN IMMEDIATE")
    with performance_logging(channel), pytest.raises(sqlite3.OperationalError) as caught:
        write(None, "key", None)
    assert caught.value is original
    result = _payload(channel)
    assert result["outcome"] == "raised"
    assert (result["sql"]["execute_calls"], result["sql"]["elapsed_ms"]) == (1, 9)

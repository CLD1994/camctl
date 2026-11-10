"""通用事务入口只执行调用方交付的工作并保留实际收场结果。"""

import sqlite3

import pytest

from camctl.persistence.atomic import TransactionError, execute_transaction, write_receipt_for


def test_work_runs_in_transaction_and_commits():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        connection.execute("CREATE TABLE sample(value INTEGER)")

        def work(transaction):
            assert connection.in_transaction
            connection.execute("INSERT INTO sample VALUES (7)")
            transaction.commit()
            return "saved"

        assert execute_transaction(connection, work) == "saved"
        assert connection.execute("SELECT value FROM sample").fetchall() == [(7,)]
        assert not connection.in_transaction
    finally:
        connection.close()


def test_work_rolls_back_original_error():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    error = ValueError("original")
    try:
        connection.execute("CREATE TABLE sample(value INTEGER)")

        def work(transaction):
            connection.execute("INSERT INTO sample VALUES (7)")
            return transaction.rollback_error(error)

        receipt = execute_transaction(connection, work)
        assert receipt.kind == "rolled_back" and receipt.error is error
        assert connection.execute("SELECT value FROM sample").fetchall() == []
        assert not connection.in_transaction
    finally:
        connection.close()


def test_control_exception_keeps_identity_and_rollback_receipt():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    control = KeyboardInterrupt("cancel")
    try:
        connection.execute("CREATE TABLE sample(value INTEGER)")

        def work(transaction):
            connection.execute("INSERT INTO sample VALUES (7)")
            raise control

        with pytest.raises(KeyboardInterrupt) as caught:
            execute_transaction(connection, work)
        assert caught.value is control
        assert write_receipt_for(control).kind == "rolled_back"
        assert connection.execute("SELECT value FROM sample").fetchall() == []
    finally:
        connection.close()


@pytest.mark.parametrize("error", [ValueError("work"), sqlite3.OperationalError("sql")])
def test_unhandled_work_error_rolls_back_and_preserves_original(error):
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        connection.execute("CREATE TABLE sample(value INTEGER)")

        def work(_transaction):
            connection.execute("INSERT INTO sample VALUES (7)")
            raise error

        with pytest.raises(type(error)) as caught:
            execute_transaction(connection, work)
        assert caught.value is error
        assert write_receipt_for(error).kind == "rolled_back"
        assert not connection.in_transaction
        assert connection.execute("SELECT value FROM sample").fetchall() == []
    finally:
        connection.close()


def test_unfinished_work_is_rejected_and_rolled_back():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        connection.execute("CREATE TABLE sample(value INTEGER)")

        def work(_transaction):
            connection.execute("INSERT INTO sample VALUES (7)")
            return "not settled"

        with pytest.raises(TransactionError) as caught:
            execute_transaction(connection, work)
        assert write_receipt_for(caught.value).kind == "rolled_back"
        assert not connection.in_transaction
        assert connection.execute("SELECT value FROM sample").fetchall() == []
    finally:
        connection.close()


class _FailExit:
    def __init__(self, connection, verb, error):
        self.connection, self.verb, self.error = connection, verb, error
        self.calls = []

    def execute(self, statement, *args):
        self.calls.append(statement)
        if statement == self.verb:
            raise self.error
        return self.connection.execute(statement, *args)


def test_commit_failure_keeps_original_error_and_unknown_receipt():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    failure = sqlite3.OperationalError("commit")
    faulty = _FailExit(connection, "COMMIT", failure)
    try:
        def work(transaction):
            transaction.commit()

        with pytest.raises(sqlite3.OperationalError) as caught:
            execute_transaction(faulty, work)
        assert caught.value is failure
        assert write_receipt_for(failure).kind == "unknown"
        assert faulty.calls == ["BEGIN IMMEDIATE", "COMMIT"]
    finally:
        connection.close()


def test_work_cannot_turn_failed_commit_into_success():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    failure = sqlite3.OperationalError("commit")
    faulty = _FailExit(connection, "COMMIT", failure)
    try:
        def work(transaction):
            try:
                transaction.commit()
            except sqlite3.OperationalError:
                return "saved"

        with pytest.raises(TransactionError) as caught:
            execute_transaction(faulty, work)
        assert write_receipt_for(caught.value).kind == "unknown"
        assert faulty.calls == ["BEGIN IMMEDIATE", "COMMIT"]
    finally:
        connection.close()


def test_work_cannot_return_success_after_starting_commit():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        def work(transaction):
            transaction.prepare_commit()
            return "saved"

        with pytest.raises(TransactionError) as caught:
            execute_transaction(connection, work)
        assert write_receipt_for(caught.value).kind == "unknown"
        assert connection.in_transaction
    finally:
        connection.close()


@pytest.mark.parametrize("operation,kind", [("commit", "completed"), ("rollback", "rolled_back")])
@pytest.mark.parametrize("control", [False, True])
def test_exception_after_reliable_exit_keeps_exit_fact_without_retry(operation, kind, control):
    connection = sqlite3.connect(":memory:", isolation_level=None)
    recorder = _FailExit(connection, "never", None)
    error = KeyboardInterrupt("after") if control else ValueError("after")
    try:
        def work(transaction):
            getattr(transaction, operation)()
            raise error

        with pytest.raises(type(error)) as caught:
            execute_transaction(recorder, work)
        assert caught.value is error
        assert write_receipt_for(error).kind == kind
        assert recorder.calls == ["BEGIN IMMEDIATE", operation.upper()]
        assert not connection.in_transaction
    finally:
        connection.close()


def test_rollback_failure_keeps_original_control_and_both_errors():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    failure = sqlite3.OperationalError("rollback")
    faulty = _FailExit(connection, "ROLLBACK", failure)
    control = KeyboardInterrupt("cancel")
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            execute_transaction(faulty, lambda _: (_ for _ in ()).throw(control))
        assert caught.value is control
        receipt = write_receipt_for(control)
        assert receipt.kind == "unknown" and receipt.secondary_errors == (failure,)
        assert faulty.calls == ["BEGIN IMMEDIATE", "ROLLBACK"]
    finally:
        connection.close()


def test_unfinished_work_with_rollback_failure_keeps_both_errors():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    failure = sqlite3.OperationalError("rollback")
    faulty = _FailExit(connection, "ROLLBACK", failure)
    try:
        with pytest.raises(TransactionError) as caught:
            execute_transaction(faulty, lambda _: "unfinished")
        receipt = write_receipt_for(caught.value)
        assert receipt.kind == "unknown"
        assert isinstance(receipt.error, ExceptionGroup)
        assert receipt.error.exceptions == (caught.value, failure)
        assert faulty.calls == ["BEGIN IMMEDIATE", "ROLLBACK"]
    finally:
        connection.close()

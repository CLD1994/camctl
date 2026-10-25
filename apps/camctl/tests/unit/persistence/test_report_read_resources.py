"""报告资源只在同份生成内复用，每页事务和对象责任独立结束。"""

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.contracts.values import ConsistencyError
from camctl.persistence.repositories import history
from camctl.persistence.runtime import (
    DatabaseMetadata, DatabaseUnsupportedFormatError, DbOpenMode,
    DirectoryBindingError, OwnedConnection,
)


@pytest.fixture
def database(mocker):
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    connection.in_transaction = False
    metadata = DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing")
    state = {"instance": metadata.instance_id, "queries": [], "failure": None}

    def execute(query, parameters=()):
        state["queries"].append(query)
        if query == state["failure"]:
            raise sqlite3.OperationalError(query)
        if query == "BEGIN":
            assert not connection.in_transaction
            connection.in_transaction = True
        elif query in ("COMMIT", "ROLLBACK"):
            assert connection.in_transaction
            connection.in_transaction = False
        cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
        if query.startswith("SELECT instance_id"):
            cursor.fetchone.return_value = (state["instance"],)
        elif query.startswith("SELECT from_wm"):
            cursor.fetchone.return_value = (0, 5, 9, 1)
        elif query.startswith("SELECT transaction_id"):
            cursor.fetchone.return_value = (1,)
        return cursor

    connection.execute.side_effect = execute
    opener = mocker.patch.object(history, "open_existing", autospec=True,
                                return_value=OwnedConnection(connection, metadata))
    return connection, metadata, state, opener


def test_report_scope_reuses_connection_but_releases_every_page(database):
    connection, metadata, state, opener = database
    repository = history.HistoryRepository(Path("/state.db"))

    with repository.report_read_scope() as actual:
        assert actual is metadata
        for _ in range(2):
            with repository._read_connection() as page:
                assert page is connection and page.in_transaction
            assert not connection.in_transaction
            connection.close.assert_not_called()

    assert state["queries"].count("BEGIN") == state["queries"].count("COMMIT") == 2
    opener.assert_called_once_with(Path("/state.db"), DbOpenMode.EXISTING_RO, repository._config)
    connection.close.assert_called_once()


def test_outside_report_scope_keeps_independent_connection_ownership(database):
    connection, _, _, opener = database
    repository = history.HistoryRepository(Path("/state.db"))
    for _ in range(2):
        with repository._read_connection():
            pass
    assert opener.call_count == connection.close.call_count == 2


def test_frozen_registration_uses_report_connection_and_short_transaction(database, mocker):
    connection, _, _, opener = database
    repository = history.HistoryRepository(Path("/state.db"))
    mocker.patch.object(history, "_transaction_range", autospec=True,
                        return_value=TransactionRange(1, 1, 9))

    with repository.report_read_scope():
        registration = repository.frozen_registration(7)
        assert (registration.from_wm, registration.to_wm) == (0, 5)
        assert not connection.in_transaction
        opener.assert_called_once()


@pytest.mark.parametrize("failure", [RuntimeError("read failed"),
                                    asyncio.CancelledError("cancelled"),
                                    BaseException("control")])
def test_report_read_failure_rolls_back_and_clears_bounded_caches(database, failure):
    connection, _, state, _ = database
    repository = history.HistoryRepository(Path("/state.db"))
    repository._validated_ranges[1] = TransactionRange(1, 1, 9)
    repository._current_range = TransactionRange(1, 1, 9)

    with pytest.raises(type(failure)) as caught:
        with repository.report_read_scope():
            with repository._read_connection():
                raise failure

    assert caught.value is failure
    assert state["queries"].count("ROLLBACK") == 1
    assert not connection.in_transaction
    assert repository._validated_ranges == {} and repository._current_range is None
    connection.close.assert_called_once()


@pytest.mark.parametrize("stage", ["COMMIT", "ROLLBACK"])
def test_transaction_cleanup_failure_is_not_hidden(database, stage):
    connection, _, state, _ = database
    repository = history.HistoryRepository(Path("/state.db"))
    state["failure"] = stage

    with pytest.raises(BaseException) as caught:
        with repository.report_read_scope():
            with repository._read_connection():
                if stage == "ROLLBACK":
                    raise RuntimeError("original failure")

    errors = (caught.value, caught.value.__cause__, caught.value.__context__)
    assert any(isinstance(error, sqlite3.OperationalError) for error in errors)
    connection.close.assert_called_once()


def test_each_report_rechecks_actual_connection_instance(database):
    connection, metadata, state, opener = database
    repository = history.HistoryRepository(Path("/state.db"), expected_instance_id=metadata.instance_id)
    with repository.report_read_scope():
        pass
    state["instance"] = "b" * 32

    with pytest.raises(ConsistencyError):
        with repository.report_read_scope():
            with repository._read_connection():
                pass

    assert opener.call_count == connection.close.call_count == 2


def test_report_scope_rejects_expected_instance_mismatch(database):
    connection, _, _, _ = database
    repository = history.HistoryRepository(Path("/state.db"), expected_instance_id="b" * 32)
    with pytest.raises(ConsistencyError):
        with repository.report_read_scope():
            pytest.fail("不同实例不能进入报告生成")
    connection.close.assert_called_once()


def test_report_scope_checks_directories_on_actual_generation_connection(database):
    connection, _, _, _ = database
    repository = history.HistoryRepository(Path("/state.db"),
        expected_directories=("/another-staging", "/ready", "/processing"))
    with pytest.raises(DirectoryBindingError):
        with repository.report_read_scope():
            pytest.fail("不同目录绑定不能进入报告生成")
    connection.close.assert_called_once()


def test_report_scope_preserves_format_failure(database):
    connection, _, _, opener = database
    original = DatabaseUnsupportedFormatError("unsupported database format")
    opener.side_effect = original
    repository = history.HistoryRepository(Path("/state.db"))
    with pytest.raises(DatabaseUnsupportedFormatError) as caught:
        with repository.report_read_scope():
            pass
    assert caught.value is original
    connection.close.assert_not_called()


def test_report_scope_closes_even_when_connection_close_fails(database):
    connection, _, _, _ = database
    connection.close.side_effect = sqlite3.OperationalError("close failed")
    repository = history.HistoryRepository(Path("/state.db"))
    with pytest.raises(sqlite3.OperationalError):
        with repository.report_read_scope():
            pass
    assert repository._report_connection is None


def test_report_scope_rejects_nested_reports(database):
    _, _, _, opener = database
    repository = history.HistoryRepository(Path("/state.db"))
    with repository.report_read_scope():
        with pytest.raises(ConsistencyError):
            with repository.report_read_scope():
                pass
    opener.assert_called_once()


def test_report_projection_validator_is_lazy_reused_and_cleared_per_object(database, mocker):
    repository = history.HistoryRepository(Path("/state.db"))
    validator_type = mocker.patch.object(history, "ProjectionRowValidator", autospec=True)
    validator = validator_type.return_value.__enter__.return_value
    validator.object_scope.return_value.__enter__.return_value = validator
    temporary_type = mocker.patch.object(history, "TemporaryDirectory", autospec=True)
    temporary_type.return_value.__enter__.return_value = "/report-resources"

    with repository.report_read_scope():
        validator_type.assert_not_called()
        for _ in range(2):
            with repository.projection_validator(storage_path=Path("/object/validation.db")) as actual:
                assert actual is validator
        validator_type.assert_called_once_with(storage_path=Path("/report-resources/validation.db"))
        assert validator.object_scope.call_count == 2
        validator_type.return_value.__exit__.assert_not_called()
    validator_type.return_value.__exit__.assert_called_once()
    temporary_type.return_value.__exit__.assert_called_once()


@pytest.mark.parametrize("failure", [RuntimeError("generation failed"),
                                    asyncio.CancelledError("cancelled")])
def test_failed_report_releases_all_resources_and_next_report_recreates_them(database, mocker, failure):
    connection, _, _, opener = database
    repository = history.HistoryRepository(Path("/state.db"))
    validator_type = mocker.patch.object(history, "ProjectionRowValidator", autospec=True)
    temporary_type = mocker.patch.object(history, "TemporaryDirectory", autospec=True)
    temporary_type.return_value.__enter__.return_value = "/report-resources"

    with pytest.raises(type(failure)) as caught:
        with repository.report_read_scope():
            with repository.projection_validator():
                raise failure

    assert caught.value is failure
    validator_type.return_value.__exit__.assert_called_once()
    temporary_type.return_value.__exit__.assert_called_once()
    connection.close.assert_called_once()
    with repository.report_read_scope():
        with repository.projection_validator():
            pass
    assert opener.call_count == validator_type.call_count == temporary_type.call_count == 2
    assert connection.close.call_count == 2


def test_projection_validator_outside_report_keeps_independent_storage(mocker):
    repository = history.HistoryRepository(Path("/state.db"))
    validator_type = mocker.patch.object(history, "ProjectionRowValidator", autospec=True)
    for _ in range(2):
        with repository.projection_validator(storage_path=Path("/object/validation.db")):
            pass
    assert validator_type.call_count == validator_type.return_value.__exit__.call_count == 2


def test_report_validator_failure_does_not_fall_back_to_a_new_structure(database, mocker):
    repository = history.HistoryRepository(Path("/state.db"))
    temporary_type = mocker.patch.object(history, "TemporaryDirectory", autospec=True)
    temporary_type.return_value.__enter__.return_value = "/report-resources"
    validator_type = mocker.patch.object(history, "ProjectionRowValidator", autospec=True)
    original = sqlite3.OperationalError("schema create failed")
    validator_type.return_value.__enter__.side_effect = original

    with pytest.raises(sqlite3.OperationalError) as caught:
        with repository.report_read_scope():
            with repository.projection_validator():
                pass

    assert caught.value is original
    validator_type.assert_called_once()
    temporary_type.return_value.__exit__.assert_called_once()

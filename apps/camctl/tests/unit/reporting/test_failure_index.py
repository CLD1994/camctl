"""报告私有索引的分页、损坏拒绝与资源责任，SQLite由接口替身隔离。"""

import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.reporting import failure_index


def _database():
    database = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
    database.execute.return_value = cursor
    return database, cursor


def _index():
    index = object.__new__(failure_index.FailureDeliveryIndex)
    index._db, cursor = _database()
    index._ready = False
    index.batch_size = 2
    return index, cursor


@pytest.mark.parametrize("reference", [None, (2, 51, 21, 1), (2, 99, 21, 0)])
def test_bind_rejects_missing_duplicate_or_mismatched_delivery(reference):
    index, cursor = _index()
    cursor.fetchone.return_value = reference
    with pytest.raises(ConsistencyError):
        index.bind_delivery(7, 51)
    cursor.close.assert_called_once()


def test_bind_returns_the_same_h_dependency_and_source():
    index, cursor = _index()
    cursor.fetchone.return_value = (2, 51, 21, 0)
    assert index.bind_delivery(7, 51) == (2, 21)
    assert cursor.close.call_count == 2


def test_duplicate_item_delivery_reference_is_a_consistency_error():
    index, _ = _index()
    error = sqlite3.IntegrityError("unique delivery")
    index._db.execute.side_effect = error
    with pytest.raises(ConsistencyError) as caught:
        index.add_reference(7, 2, 51, 21)
    assert caught.value.__cause__ is error


def test_missing_delivery_prevents_failure_iteration():
    index, cursor = _index()
    cursor.fetchone.return_value = (7,)
    with pytest.raises(ConsistencyError):
        index.finish()
    with pytest.raises(ConsistencyError):
        list(index.failures(2))


def test_failure_pages_decode_one_bounded_batch_and_release_cursors():
    index, _ = _index()
    index._ready = True
    pages = [[(2, '{"delivery_id":"2"}'), (10, '{"delivery_id":"10"}')],
             [(100, '{"delivery_id":"100"}')], []]
    cursors = []
    for page in pages:
        cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
        cursor.fetchmany.return_value = page
        cursors.append(cursor)
    index._db.execute.side_effect = cursors

    assert list(index.failures(4)) == [
        (2, {"delivery_id": "2"}), (10, {"delivery_id": "10"}),
        (100, {"delivery_id": "100"}),
    ]
    for cursor in cursors:
        cursor.fetchmany.assert_called_once_with(2)
        cursor.close.assert_called_once()
    assert [call.args[1] for call in index._db.execute.call_args_list] == [
        (4, 0, 2), (4, 10, 2), (4, 100, 2)]


@pytest.mark.parametrize("phase", ["connect", "configure", "schema"])
def test_failed_index_construction_closes_created_resources(monkeypatch, phase):
    temporary = create_autospec(failure_index.TemporaryDirectory, instance=True)
    temporary.name = "/private/report-failures"
    database, _ = _database()
    monkeypatch.setattr(failure_index, "TemporaryDirectory", lambda **kwargs: temporary)
    connect = create_autospec(sqlite3.connect, return_value=database)
    configure = create_autospec(failure_index.configure_derived_connection)
    monkeypatch.setattr(failure_index.sqlite3, "connect", connect)
    monkeypatch.setattr(failure_index, "configure_derived_connection", configure)
    fault = sqlite3.OperationalError(phase)
    if phase == "connect":
        connect.side_effect = fault
    elif phase == "configure":
        configure.side_effect = fault
    else:
        database.execute.side_effect = fault

    with pytest.raises(sqlite3.OperationalError) as caught:
        failure_index.FailureDeliveryIndex()

    assert caught.value is fault
    temporary.cleanup.assert_called_once()
    assert database.close.call_count == (phase != "connect")


def test_index_cleanup_runs_even_if_connection_close_raises():
    index, _ = _index()
    temporary = create_autospec(failure_index.TemporaryDirectory, instance=True)
    index._temporary = temporary
    index._db.close.side_effect = sqlite3.OperationalError("close")
    with pytest.raises(sqlite3.OperationalError):
        index.close()
    assert index._db is None
    temporary.cleanup.assert_called_once()

"""报告观察只读业务水位，缺失或无效事实不能解释为没有变化。"""

import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.session.service import StateDbFailure, _read_report_change_wm


@pytest.mark.parametrize("stored, expected", [(None, 0), (0, 0), (9, 9)])
def test_report_observation_returns_exact_business_watermark(stored, expected):
    connection = create_autospec(sqlite3.Connection, instance=True)
    cursor = connection.execute.return_value
    cursor.fetchone.return_value = (stored,)

    assert _read_report_change_wm(connection) == expected

    connection.execute.assert_called_once_with("SELECT MAX(change_seq) FROM history_events")
    cursor.close.assert_called_once()


@pytest.mark.parametrize("stored", [-1, True, "9", 9.0])
def test_report_observation_rejects_invalid_watermark(stored):
    connection = create_autospec(sqlite3.Connection, instance=True)
    connection.execute.return_value.fetchone.return_value = (stored,)

    with pytest.raises(ConsistencyError):
        _read_report_change_wm(connection)


def test_report_observation_missing_result_is_an_error():
    connection = create_autospec(sqlite3.Connection, instance=True)
    connection.execute.return_value.fetchone.return_value = None

    with pytest.raises(StateDbFailure):
        _read_report_change_wm(connection)

"""物理活动与输出范围占用分别决定新拍摄能否推进。"""
import sqlite3

import pytest

from camctl.scheduling.resources import capture_scope_blocked


@pytest.mark.parametrize("state,dispatch,mode,directories,expected", [
    (1, 1, 1, (), False),
    (1, 4, 1, (), False),
    (3, 3, 1, (), True),
    (3, 3, 2, ("/same",), True),
    (3, 3, 2, ("/different",), False),
    (1, 1, 2, ("/same",), True),
    (1, 1, 2, ("/different",), False),
    (2, 3, 2, ("/different",), True),
])
def test_scope_guard_distinguishes_unstarted_task_from_held_baseline(mocker, state, dispatch, mode, directories, expected):
    connection = mocker.create_autospec(sqlite3.Connection, instance=True)
    cursor = mocker.create_autospec(sqlite3.Cursor, instance=True)
    cursor.__iter__.return_value = iter([(99, state, dispatch, mode, {"directories": list(directories)})])
    connection.execute.return_value = cursor
    assert capture_scope_blocked(connection, "cam-1", 1, 2, {"directories": ["/same"]}) is expected

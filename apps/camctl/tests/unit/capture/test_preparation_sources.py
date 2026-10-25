"""动作准备失败消费最近一次真实结果，不以旧失败覆盖新成功。"""
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.persistence.repositories.capture_facts import StartFacts
from camctl.persistence.repositories.preparation import verify_failure_source


@pytest.mark.parametrize("reason,latest", [
    ("device_failed", (3, 2, None, 20)),
    ("device_failed", (2, 1, None, None)),
    ("baseline_read_failed", (2, None, 20)),
    ("baseline_read_failed", (1, None, None)),
])
def test_new_success_or_unfinished_result_cannot_use_older_failure(reason, latest):
    connection = create_autospec(sqlite3.Connection, instance=True)
    cursors = [create_autospec(sqlite3.Cursor, instance=True) for _ in range(3)]
    for cursor, returned in zip(cursors, [None, None, latest]):
        cursor.fetchone.return_value = returned
    connection.execute.side_effect = cursors
    facts = StartFacts({"id": 11}, None, 0, True, 1, preparation_resolved=True)
    with pytest.raises(ConsistencyError):
        verify_failure_source(connection, {"id": 7}, facts, reason)


@pytest.mark.parametrize("reason,latest", [
    ("device_failed", (4, 3, '{"code":"setting_failed"}', 20)),
    ("device_failed", (6, 4, '{"code":"control_unconfirmed"}', 20)),
    ("baseline_read_failed", (2, '{"code":"directory_failed"}', 20)),
    ("baseline_read_failed", (3, None, 20)),
])
def test_current_failed_or_unconfirmed_result_preserves_its_classification(reason, latest):
    connection = create_autospec(sqlite3.Connection, instance=True)
    cursors = [create_autospec(sqlite3.Cursor, instance=True) for _ in range(3)]
    for cursor, returned in zip(cursors, [None, None, latest]):
        cursor.fetchone.return_value = returned
    connection.execute.side_effect = cursors
    facts = StartFacts({"id": 11}, None, 0, True, 1, preparation_resolved=True)
    verify_failure_source(connection, {"id": 7}, facts, reason)


@pytest.mark.parametrize("action_type", [1, 2])
def test_running_activity_before_first_start_remains_preparable(mocker, action_type):
    from camctl.persistence.repositories.preparation import preparation_action_eligible
    facts = StartFacts({"id": 11}, None, 0, True, 1, preparation_resolved=True)
    mocker.patch("camctl.persistence.repositories.capture_facts.load_start_facts", return_value=facts)
    connection = create_autospec(sqlite3.Connection, instance=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    cursor.fetchone.return_value = None
    connection.execute.return_value = cursor
    assert preparation_action_eligible(connection,
        {"id": 7, "type": action_type, "status": 2, "execution_started": 1, "cancel_requested": 0}, {"id": 11})


def test_recording_with_reliably_ineffective_start_remains_preparable(mocker):
    from camctl.persistence.repositories.preparation import preparation_action_eligible
    facts = StartFacts({"id": 11}, {"id": 3}, 1, True, 4, preparation_resolved=True)
    mocker.patch("camctl.persistence.repositories.capture_facts.load_start_facts", return_value=facts)
    connection = create_autospec(sqlite3.Connection, instance=True)
    assert preparation_action_eligible(connection,
        {"id": 7, "type": 2, "status": 2, "execution_started": 1, "cancel_requested": 0}, {"id": 11})


@pytest.mark.parametrize("status,execution_started", [(1, 0), (2, 1)])
def test_photo_with_original_start_responsibility_cannot_prepare_again(mocker, status, execution_started):
    from camctl.persistence.repositories.preparation import preparation_action_eligible
    connection = create_autospec(sqlite3.Connection, instance=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True)
    cursor.fetchone.return_value = (1,)
    connection.execute.return_value = cursor
    facts = StartFacts({"id": 11}, {"id": 3}, 1, True, 4, preparation_resolved=True)
    mocker.patch("camctl.persistence.repositories.capture_facts.load_start_facts", return_value=facts)
    assert not preparation_action_eligible(connection,
        {"id": 7, "type": 1, "status": status, "execution_started": execution_started,
         "cancel_requested": 0}, {"id": 11})

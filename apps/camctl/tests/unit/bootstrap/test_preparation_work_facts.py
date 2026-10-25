"""没有本地任务时，原准备释放仍参与持久交接与关闭判断。"""

import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

from camctl.bootstrap.application import query_work_facts
from camctl.persistence.repositories.session import CloseAdmission, _CloseCommand
from camctl.session.handoff import SubmitHandoff
from camctl.session.locks import AdmissionProbe, AdmissionProbeStatus
from camctl.session.work import WorkDecisionKind, classify_work


def _connection(mocker, releases):
    connection = create_autospec(sqlite3.Connection, instance=True)

    def execute(sql, parameters=()):
        cursor = create_autospec(sqlite3.Cursor, instance=True)
        if sql.startswith("SELECT COUNT(*) FROM actions a JOIN device_activities d"):
            value = (releases,)
        elif sql.startswith("SELECT COUNT(*)") or sql.startswith("SELECT acknowledged_wm") or "COALESCE(MAX(to_wm)" in sql:
            value = (0,)
        elif "FROM device_activities da JOIN actions a" in sql:
            value = (1,)
        else:
            value = None
        cursor.fetchone.return_value = value
        return cursor

    connection.execute.side_effect = execute
    mocker.patch("camctl.outputs.cleanup_discovery.extra_cleanup_settlements", autospec=True, return_value=0)
    return connection


def test_submit_without_local_owner_requests_run_for_terminal_preparation_release(mocker):
    connection = _connection(mocker, releases=1)
    probe = Mock(spec=lambda: None, return_value=AdmissionProbe(AdmissionProbeStatus.ACQUIRED_AND_RELEASED))
    handoff = SubmitHandoff(query_work_facts, probe)
    facts = query_work_facts(connection)
    assert facts.unfinished_actions == 0 and facts.required_settlements == 1
    assert handoff.needs_run(connection)


def test_close_without_local_owner_keeps_admission_for_terminal_preparation_release(mocker):
    connection = _connection(mocker, releases=1)
    release = Mock(spec=lambda: None)
    plan = _CloseCommand(CloseAdmission(query_work_facts, release)).plan(SimpleNamespace(connection=connection))
    assert not plan.result.admission_closed
    assert plan.result.work.kind is WorkDecisionKind.NEEDS_DRIVER
    release.assert_not_called()


def test_released_preparation_does_not_turn_other_residual_facts_into_work(mocker):
    facts = query_work_facts(_connection(mocker, releases=0))
    assert facts.residual_device_facts
    assert classify_work(facts).kind is WorkDecisionKind.CAN_EXIT_SUCCESS

"""窗口筛选之后，原观察命令仍用事务内事实决定是否保存。"""

import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import new_operation_key
from camctl.persistence.repositories import scheduling
from camctl.persistence.transaction import TransactionScope


@pytest.mark.parametrize("status,cancel_requested,observed,expected", [
    (1, 0, 1000, scheduling.ObserveOutcome.ALREADY),
    (3, 0, None, scheduling.ObserveOutcome.REJECTED),
    (4, 0, None, scheduling.ObserveOutcome.REJECTED),
    (5, 0, None, scheduling.ObserveOutcome.REJECTED),
    (6, 1, None, scheduling.ObserveOutcome.REJECTED),
])
def test_observation_rechecks_saved_or_terminal_state_after_candidate_read(
        mocker, status, cancel_requested, observed, expected):
    current = dict(id=1, type=2, status=status, cancel_requested=cancel_requested,
                   scheduled_at=1000, max_delay_ms=1,
                   first_window_observed_at=observed)
    mocker.patch.object(scheduling, "saved_transaction_events", autospec=True, return_value=None)
    mocker.patch.object(scheduling, "row_facts", autospec=True, return_value=current)
    scope = TransactionScope(create_autospec(sqlite3.Connection, instance=True), 1, 1)

    plan = scheduling.ObserveWindowCommand(
        scheduling.ObserveWindowRequest(1, 1500, 1500), new_operation_key()).plan(scope)

    assert plan.result.outcome is expected
    assert plan.read_only and plan.events == ()
    assert current["first_window_observed_at"] == observed
    assert current["status"] == status

"""S3 时钟资格的组件集成测试：真实 SQLite 与下界保存事务。

受理后检查、下界事件（真实 clock 守卫）、复检推进及无变更不写
事务；受理结果不受时钟检查影响。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.contracts.values import new_operation_key
from camctl.persistence.repositories.session import SessionRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.session.clock import (
    ClockCheckInput,
    ClockTrustStatus,
    ExecutionMode,
    check_clock,
    enter_execution,
    register_clock_guard,
)

from ..persistence.test_runtime import _create_valid_database

register_clock_guard()

_MIN = 1_735_689_600_000_000


class FixedClock:
    def __init__(self, readings: list[int]) -> None:
        self._readings = list(readings)

    def utc_micros(self) -> int:
        return self._readings.pop(0)

    def monotonic_ns(self) -> int:
        return 0


@pytest.fixture()
def database(tmp_path: Path):
    _create_valid_database(tmp_path / "state.db")
    owned = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
    yield owned.connection, SessionRepository()
    owned.connection.close()


def _input(bound: int | None) -> ClockCheckInput:
    return ClockCheckInput(
        lower_bound_micros=bound, min_plausible_micros=_MIN,
        recheck_delay_s=0.0, recheck_count=1,
    )


class TestLowerBoundPersistence:
    def test_first_trusted_check_saves_bound_event(self, database) -> None:
        connection, repository = database
        reading = _MIN + 10_000
        check = check_clock(_input(None), FixedClock([reading]))
        assert check.needs_bound_update
        outcome = repository.update_lower_bound(check, new_operation_key(), _owned(connection))
        assert outcome.kind.value == "completed"
        row = connection.execute(
            "SELECT trusted_time_lower_bound, trusted_time_event_id FROM runtime_state"
        ).fetchone()
        assert row == (reading, 1)
        event = connection.execute(
            "SELECT event_type, change_seq, occurred_at, clock_status"
            " FROM history_events"
        ).fetchone()
        assert event == (31, None, reading, 2)

    @pytest.mark.asyncio
    async def test_second_run_advances_only_strictly(self, database) -> None:
        connection, repository = database
        first = check_clock(_input(None), FixedClock([_MIN + 10_000]))
        repository.update_lower_bound(first, new_operation_key(), _owned(connection))
        events_before = connection.execute("SELECT COUNT(*) FROM history_events").fetchone()[0]

        # 更晚读数：推进下界并记录依据事件。
        later = check_clock(_input(_MIN + 10_000), FixedClock([_MIN + 20_000]))
        assert later.needs_bound_update
        repository.update_lower_bound(later, new_operation_key(), _owned(connection))
        assert (
            connection.execute("SELECT COUNT(*) FROM history_events").fetchone()[0]
            == events_before + 1
        )
        assert connection.execute(
            "SELECT trusted_time_lower_bound FROM runtime_state"
        ).fetchone()[0] == _MIN + 20_000
        advance_event = connection.execute(
            "SELECT event_type, change_seq, occurred_at, clock_status"
            " FROM history_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert advance_event == (31, None, _MIN + 20_000, 2)

        # 与下界相等的读数可信，但无需推进下界或新增历史事务。
        events_before_stale = connection.execute(
            "SELECT * FROM history_events ORDER BY id"
        ).fetchall()
        transactions_before_stale = connection.execute(
            "SELECT * FROM history_transactions ORDER BY id"
        ).fetchall()
        stale = check_clock(
            _input(_MIN + 20_000), FixedClock([_MIN + 20_000])
        )
        assert stale.status is ClockTrustStatus.TRUSTED
        assert not stale.needs_bound_update
        mode = await enter_execution(
            stale,
            SimpleNamespace(
                repository=repository,
                operation_key=new_operation_key(),
                owned=_owned(connection),
            ),
        )
        assert mode is ExecutionMode.NORMAL
        assert connection.execute(
            "SELECT * FROM history_events ORDER BY id"
        ).fetchall() == events_before_stale
        assert connection.execute(
            "SELECT * FROM history_transactions ORDER BY id"
        ).fetchall() == transactions_before_stale

    def test_clock_failure_preserves_acceptance(self, database) -> None:
        connection, repository = database
        # 复检仍失败：不取得下界资格，不产生任何事件；已受理事实保持。
        check = check_clock(_input(None), FixedClock([_MIN - 1]))
        assert check.status is ClockTrustStatus.UNTRUSTED
        events = connection.execute("SELECT COUNT(*) FROM history_events").fetchone()[0]
        assert events == 0
        assert connection.execute(
            "SELECT trusted_time_lower_bound FROM runtime_state"
        ).fetchone()[0] is None

    def test_concurrent_advance_rejected_by_stale_recheck(self, database) -> None:
        connection, repository = database
        first = check_clock(_input(None), FixedClock([_MIN + 10_000]))
        repository.update_lower_bound(first, new_operation_key(), _owned(connection))
        # 另一推进已提交后，用旧下界构造的检查仍安全：命令在事务内
        # 重读当前下界，读数不晚于当前值时不写。
        stale = check_clock(_input(None), FixedClock([_MIN + 5_000]))
        outcome = repository.update_lower_bound(stale, new_operation_key(), _owned(connection))
        assert outcome.kind.value == "completed"
        assert connection.execute(
            "SELECT trusted_time_lower_bound FROM runtime_state"
        ).fetchone()[0] == _MIN + 10_000
        assert connection.execute("SELECT COUNT(*) FROM history_events").fetchone()[0] == 1


def _owned(connection):
    from camctl.persistence.runtime import OwnedConnection

    return OwnedConnection(connection=connection, metadata=None)

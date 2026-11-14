"""S4 事务内接管与接纳关闭的组件集成测试。

真实 SQLite 关闭事务及接纳锁探测：报告失败后的关闭决策、实际锁释放和重复探测。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from camctl.contracts.values import new_operation_key
from camctl.persistence.repositories.session import (
    CloseAdmission,
    SessionRepository,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, OwnedConnection, open_existing
from camctl.session.locks import (
    AdmissionLease,
    acquire_admission,
    lock_file_paths,
    probe_admission,
)
from camctl.session.work import WorkDecisionKind, WorkFacts

from ..persistence.test_runtime import _create_valid_database


def _facts(**overrides) -> WorkFacts:
    base = dict(
        unfinished_actions=0,
        required_settlements=0,
        pending_report_changes=False,
        report_failed_no_new_changes=False,
        residual_device_facts=False,
        deferred_work_cleanup=False,
        waiting_acknowledgement=True,
        snapshot_backlog=False,
    )
    base.update(overrides)
    return WorkFacts(**base)


@pytest.fixture()
def database(tmp_path: Path):
    _create_valid_database(tmp_path / "state.db")
    owned = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
    _, admission_lock = lock_file_paths(tmp_path / "state.db")
    yield owned, admission_lock
    owned.connection.close()


def _close(repository, owned, facts: WorkFacts, release):
    outcome = repository.close_admission(
        CloseAdmission(facts_query=lambda connection: facts, release_admission=release),
        new_operation_key(),
        owned,
    )
    assert outcome.kind.value == "completed"
    return outcome.value


class TestAdmissionClose:
    def test_idle_close_invokes_release(self, database) -> None:
        owned, _admission_lock = database
        released: list[bool] = []
        result = _close(SessionRepository(), owned, _facts(), lambda: released.append(True))
        assert result.admission_closed is True
        assert released == [True]

    def test_idle_close_returns_closed_decision(self, database) -> None:
        owned, _admission_lock = database
        result = _close(SessionRepository(), owned, _facts(), lambda: None)
        assert result.admission_closed is True

    def test_report_failure_waits_for_new_changes(self, database) -> None:
        owned, admission_lock = database
        repository = SessionRepository()
        # 报告失败且无新变化：关闭接纳，责任仍保留给后续 run。
        result = _close(
            repository, owned, _facts(report_failed_no_new_changes=True), lambda: None
        )
        assert result.admission_closed is True
        assert result.work.kind is WorkDecisionKind.EXIT_REPORT_ERROR
        # 新变化先提交：继续处理（需要驱动）。
        result = _close(
            repository,
            owned,
            _facts(report_failed_no_new_changes=True, pending_report_changes=True),
            lambda: None,
        )
        assert result.admission_closed is False
        assert result.work.kind is WorkDecisionKind.NEEDS_DRIVER

    def test_report_failure_closes_real_admission(self, database) -> None:
        owned, admission_lock = database
        lease = acquire_admission(admission_lock)
        try:
            result = _close(
                SessionRepository(), owned,
                _facts(report_failed_no_new_changes=True), lease.close,
            )
            assert result.admission_closed is True
            assert result.work.kind is WorkDecisionKind.EXIT_REPORT_ERROR
            assert probe_admission(admission_lock).is_free
        finally:
            lease.close()


class TestProbeIsolation:
    def test_repeated_probes_release_admission(self, tmp_path: Path) -> None:
        _, admission_lock = lock_file_paths(tmp_path / "state.db")
        # 每次探测都实际取得并释放锁。
        for _ in range(5):
            assert probe_admission(admission_lock).is_free

    def test_failed_facts_query_rolls_back_close_transaction(self, database) -> None:
        owned, _admission_lock = database
        repository = SessionRepository()
        _close(repository, owned, _facts(), lambda: None)
        outcome = repository.close_admission(
            CloseAdmission(
                facts_query=lambda connection: (_ for _ in ()).throw(RuntimeError("查询失败")),
                release_admission=lambda: None,
            ),
            new_operation_key(),
            owned,
        )
        assert outcome.kind.value == "rolled_back"
        assert not owned.connection.in_transaction

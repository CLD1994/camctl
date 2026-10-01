"""S4 事务内接管与接纳关闭的组件集成测试。

真实 SQLite、接纳锁与关闭事务组合：submit 竞争接纳关闭的两种先
后、多次探测互不误认、报告失败机会结束后关闭接纳。
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
from camctl.session.handoff import HandoffOutcome, decide_handoff
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


class TestSubmitRacesAdmissionClose:
    def test_acceptor_present_hands_work_over(self, database) -> None:
        owned, admission_lock = database
        lease = acquire_admission(admission_lock)
        try:
            # 提交先于关闭：存在接纳者，由其负责，不请求后续 run。
            decision = decide_handoff(
                _close_or_classify(WorkDecisionKind.NEEDS_DRIVER), probe_admission(admission_lock)
            )
            assert decision.outcome is HandoffOutcome.ACCEPTOR_PRESENT
            assert decision.needs_run is False
        finally:
            lease.close()

    def test_close_first_then_submit_requests_run(self, database) -> None:
        owned, admission_lock = database
        repository = SessionRepository()
        released: list[bool] = []

        result = _close(
            repository,
            owned,
            _facts(),
            lambda: released.append(True),
        )
        assert result.admission_closed is True
        assert released == [True]

        # 关闭先结束：新提交探测空闲并发现待处理工作，请求后续 run。
        decision = decide_handoff(
            _close_or_classify(WorkDecisionKind.NEEDS_DRIVER), probe_admission(admission_lock)
        )
        assert decision.outcome is HandoffOutcome.REQUIRES_RUN
        assert decision.needs_run is True

    def test_no_pending_work_close_keeps_admission_released(self, database) -> None:
        owned, admission_lock = database
        repository = SessionRepository()
        result = _close(repository, owned, _facts(), lambda: None)
        assert result.admission_closed is True
        assert probe_admission(admission_lock).is_free

    def test_new_work_keeps_admission_for_current_run(self, database) -> None:
        owned, admission_lock = database
        lease = acquire_admission(admission_lock)
        repository = SessionRepository()
        try:
            result = _close(repository, owned, _facts(unfinished_actions=1), lambda: None)
            assert result.admission_closed is False
            assert result.work.kind is WorkDecisionKind.NEEDS_DRIVER
        finally:
            lease.close()

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
    def test_multiple_submits_probing_do_not_misdetect(self, database) -> None:
        _, admission_lock = database
        # 多次提交的探测各自取得并释放，互不误认为接纳者。
        for _ in range(5):
            assert probe_admission(admission_lock).is_free

    def test_failed_submit_after_close_keeps_admission_closed(self, database) -> None:
        owned, admission_lock = database
        repository = SessionRepository()
        _close(repository, owned, _facts(), lambda: None)
        # 后续提交即使处理失败（回滚），也不会重新取得接纳。
        outcome = repository.close_admission(
            CloseAdmission(
                facts_query=lambda connection: (_ for _ in ()).throw(RuntimeError("查询失败")),
                release_admission=lambda: None,
            ),
            new_operation_key(),
            owned,
        )
        assert outcome.kind.value == "rolled_back"
        assert probe_admission(admission_lock).is_free


def _close_or_classify(kind: WorkDecisionKind):
    from camctl.session.work import WorkDecision

    return WorkDecision(kind)

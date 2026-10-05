"""S6 会话错误顺序与完整收尾的组件集成测试。

真实 SQLite、锁文件与接纳关闭事务：run 逐个驱动已注册流程、报
告失败不阻塞设备工作并触发日志副本、状态库错误停止后续工作；
受限会话执行规定收场与一次报告后按时钟异常退出且不取接纳；
submit 对提交结果未知以同一请求幂等核实，不宣称成功或失败。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from camctl.acceptance.service import CommandMode
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository
from camctl.persistence.repositories.session import SessionRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.session.clock import ClockCheckInput, register_clock_guard
from camctl.session.locks import acquire_admission, acquire_session, lock_file_paths
from camctl.session.service import StateDbFailure, SessionContext, SessionPaths, run_session
from camctl.session.work import WorkDecisionKind, WorkFacts

from ..persistence.test_runtime import _create_valid_database

register_clock_guard()

_MIN = 1_735_689_600_000_000


class FakeClock:
    """墙钟按脚本返回；受限场景读数早于已保存下界。"""

    def __init__(self, readings: list[int]) -> None:
        self._readings = list(readings)

    def utc_micros(self) -> int:
        return self._readings.pop(0)

    def monotonic_ns(self) -> int:
        return 0


@dataclass
class FakeFailureLog:
    requests: list = field(default_factory=list)

    async def on_report_failure(self, request) -> None:
        self.requests.append(request)


@dataclass
class Recorder:
    calls: list = field(default_factory=list)
    admissions: int = 0

    async def ok(self, context) -> None:
        self.calls.append("ok")

    async def report_fails(self, context) -> None:
        self.calls.append("report")
        raise RuntimeError("report file failed")

    async def fails_db(self, context) -> None:
        self.calls.append("db")
        raise StateDbFailure("state db unusable")

    def acquire_admission(self):
        self.admissions += 1
        return _RealAdmission()


class _RealAdmission:
    def __init__(self) -> None:
        self._inner = None

    def close(self) -> None:
        if self._inner is not None:
            self._inner.close()


def _facts(**overrides) -> WorkFacts:
    base = dict(
        unfinished_actions=0, required_settlements=0,
        pending_report_changes=False, report_failed_no_new_changes=False,
        residual_device_facts=False, deferred_work_cleanup=False,
        waiting_acknowledgement=False, snapshot_backlog=False,
    )
    base.update(overrides)
    return WorkFacts(**base)


@pytest.fixture
def environment(tmp_path: Path):
    _create_valid_database(tmp_path / "state.db")
    state_db = (tmp_path / "state.db").resolve()
    session_lock, admission_lock = lock_file_paths(state_db)
    recorder = Recorder()
    holder = {"connection": None, "facts": _facts, "admission_lease": None}

    def open_connection():
        owned = open_existing(state_db, DbOpenMode.EXISTING_RW, DbConfig())
        holder["connection"] = owned
        return owned

    context = SessionContext(
        mode=CommandMode.RUN,
        catalog=None,
        clock=FakeClock([_MIN + 60_000_000]),
        open_connection=open_connection,
        acceptance_repository=AcceptanceRepository(),
        session_repository=SessionRepository(),
        paths=SessionPaths(session_lock=session_lock,
                           admission_lock=admission_lock),
        clock_policy=lambda connection: ClockCheckInput(
            lower_bound_micros=None, min_plausible_micros=_MIN,
            recheck_delay_s=0.0, recheck_count=1),
        acquire_session=lambda: acquire_session(session_lock),
        acquire_admission=lambda: _tracked_admission(holder, admission_lock),
        facts_query=lambda connection: holder["facts"](connection),
    )
    yield context, recorder, holder, tmp_path
    if holder["connection"] is not None:
        holder["connection"].connection.close()


def _tracked_admission(holder, admission_lock):
    lease = acquire_admission(admission_lock)
    holder["admission_lease"] = lease
    return lease


async def _run(context):
    return await run_session(context, None)


class FixedClock:
    """读数固定的墙钟：推进循环按轮读取当前时间。"""

    def __init__(self, now: int) -> None:
        self.now = now

    def utc_micros(self) -> int:
        return self.now

    def monotonic_ns(self) -> int:
        return 0


class TestRunLoop:
    pytestmark = pytest.mark.asyncio

    async def test_run_drives_flows_each_round_until_work_settles(self, environment):
        import asyncio

        context, recorder, holder, _ = environment
        rounds = {"count": 0}
        state = {"facts": _facts(unfinished_actions=1)}

        async def flow(ctx):
            rounds["count"] += 1
            if rounds["count"] >= 3:
                state["facts"] = _facts()

        context.flows = {"work": flow}
        holder["facts"] = lambda connection: state["facts"]
        context.poll_interval_s = 0.01
        outcome = await asyncio.wait_for(_run(context), timeout=10)
        assert outcome.succeeded is True
        # 每轮都驱动流程；责任清空后在关闭事务内释放接纳。
        assert rounds["count"] == 3
        from camctl.session.locks import probe_admission
        assert probe_admission(
            context.paths.admission_lock).status.value == "acquired_and_released"

    async def test_wake_notification_shortens_idle_wait(self, environment):
        import asyncio

        from camctl.scheduling.notifications import WakeReason, WorkNotifier

        context, recorder, holder, _ = environment
        notifier = WorkNotifier()
        context.wake = notifier
        context.poll_interval_s = 30.0
        state = {"facts": _facts(unfinished_actions=1)}
        holder["facts"] = lambda connection: state["facts"]
        calls = {"count": 0}

        async def flow(ctx):
            calls["count"] += 1
            if calls["count"] >= 2:
                state["facts"] = _facts()

        context.flows = {"work": flow}
        task = asyncio.create_task(_run(context))
        await asyncio.sleep(0.05)
        # 长轮询上限内只有通知能唤醒：仍停在第一轮。
        assert calls["count"] == 1
        notifier.mark_changed(WakeReason.NEW_WORK)
        outcome = await asyncio.wait_for(task, timeout=10)
        assert outcome.succeeded is True
        assert calls["count"] == 2

    async def test_pending_deadline_bounds_idle_wait(self, environment):
        import asyncio

        context, recorder, holder, tmp_path = environment
        context.poll_interval_s = 30.0
        clock = FixedClock(_MIN + 60_000_000)
        context.clock = clock
        state = {"facts": _facts(unfinished_actions=1)}
        holder["facts"] = lambda connection: state["facts"]
        calls = {"count": 0}

        async def flow(ctx):
            calls["count"] += 1
            if calls["count"] >= 2:
                state["facts"] = _facts()

        context.flows = {"work": flow}
        # 待执行动作在 1 秒后：等待受该截止约束，而非轮询上限。
        seeding = context.open_connection()
        try:
            connection = seeding.connection
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO history_transactions (id, operation_key,"
                " first_event_id, last_event_id) VALUES (1, ?, 1, 1)", ("a" * 32,))
            connection.execute(
                "INSERT INTO history_events (id, transaction_id, event_type,"
                " event_version, occurred_at, clock_status, change_seq, body_json)"
                " VALUES (1, 1, 2, 1, ?, 2, NULL, ?)",
                (_MIN, '{"reason": 1, "evidence": {}, "rows": []}'))
            connection.execute(
                "INSERT INTO plans (id, request_id, name, created_at, status,"
                " created_event_id, last_event_id, change_count)"
                " VALUES (1, 4242, 'seed', ?, 1, 1, 1, 1)", (_MIN,))
            connection.execute(
                "INSERT INTO actions (id, plan_id, input_index, name, type,"
                " scheduled_at, input_fields_json, execution_spec_json, status,"
                " execution_started, cancel_requested, created_event_id,"
                " last_event_id, change_count)"
                " VALUES (1, 1, 0, 'future', 7, ?, '{}', '{}', 1, 0, 0, 1, 1, 1)",
                (_MIN + 61_000_000,))
            connection.commit()
        finally:
            seeding.connection.close()
        outcome = await asyncio.wait_for(_run(context), timeout=10)
        assert outcome.succeeded is True
        assert calls["count"] >= 2

    async def test_cancel_during_wait_releases_admission(self, environment):
        import asyncio

        from camctl.session.locks import probe_admission

        context, recorder, holder, _ = environment
        context.poll_interval_s = 30.0
        holder["facts"] = lambda connection: _facts(unfinished_actions=1)
        context.flows = {}
        task = asyncio.create_task(_run(context))
        await asyncio.sleep(0.05)
        # 等待期间接纳仍被持有：探测观察到冲突。
        probe = probe_admission(context.paths.admission_lock)
        assert probe.status.value == "conflict"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # 会话收尾释放接纳句柄。
        assert probe_admission(
            context.paths.admission_lock).status.value == "acquired_and_released"


class TestRunCompletion:
    pytestmark = pytest.mark.asyncio
    async def test_run_drives_flows_then_closes_admission(self, environment):
        context, recorder, holder, _ = environment
        context.flows = {"devices": recorder.ok, "scheduling": recorder.ok}
        holder["facts"] = lambda connection: _facts()
        outcome = await _run(context)
        assert outcome.succeeded is True
        assert recorder.calls == ["ok", "ok"]
        # 接纳已释放：非阻塞探测可重新取得。
        from camctl.session.locks import probe_admission
        probe = probe_admission(context.paths.admission_lock)
        assert probe.status.value == "acquired_and_released"

    async def test_report_failure_keeps_device_work_running(self, environment):
        context, recorder, holder, _ = environment
        failure_log = FakeFailureLog()
        context.flows = {"devices": recorder.ok, "report": recorder.report_fails}
        context.failure_log = failure_log
        context.copy_request_factory = (
            lambda error: _CopyProbe(error, context))
        holder["facts"] = lambda connection: _facts(
            report_failed_no_new_changes=True)
        outcome = await _run(context)
        # 设备工作不被报告失败阻塞；退出仍带 report_error。
        assert recorder.calls == ["ok", "report"]
        assert outcome.succeeded is False
        assert outcome.reason == "report_error"
        # 首次报告失败触发一次日志副本交付。
        assert len(failure_log.requests) == 1

    async def test_state_db_failure_stops_remaining_flows(self, environment):
        context, recorder, holder, _ = environment
        context.flows = {"a": recorder.fails_db, "b": recorder.ok}
        outcome = await _run(context)
        assert outcome.succeeded is False
        assert outcome.reason == "state_db_error"
        assert recorder.calls == ["db"]

    async def test_close_failure_reports_state_db_error(self, environment):
        context, recorder, holder, _ = environment
        context.flows = {}

        def broken_facts(connection):
            raise RuntimeError("facts unreadable")

        holder["facts"] = broken_facts
        outcome = await _run(context)
        assert outcome.succeeded is False
        assert outcome.reason == "state_db_error"


class TestRestrictedSession:
    pytestmark = pytest.mark.asyncio
    def _restrict(self, environment) -> None:
        context, _, holder, _ = environment
        # 已保存可信下界，本次墙钟读数早于下界：检查与复检都失败。
        context.clock = FakeClock([_MIN - 5, _MIN - 5, _MIN - 5])
        context.clock_policy = lambda connection: ClockCheckInput(
            lower_bound_micros=_MIN, min_plausible_micros=_MIN,
            recheck_delay_s=0.0, recheck_count=1)

    async def test_restricted_runs_flows_once_report_and_exits_clock_invalid(
            self, environment):
        context, recorder, holder, _ = environment
        self._restrict(environment)
        context.restricted_flows = {"cancel": recorder.ok}
        once = Recorder()
        context.once_report = once.ok
        outcome = await _run(context)
        assert outcome.succeeded is False
        assert outcome.reason == "clock_invalid"
        assert recorder.calls == ["ok"]
        assert once.calls == ["ok"]
        # 受限会话不取得普通接纳资格。
        assert holder["admission_lease"] is None

    async def test_restricted_state_db_error_overrides(self, environment):
        context, recorder, holder, _ = environment
        self._restrict(environment)
        context.restricted_flows = {"cancel": recorder.fails_db}
        once = Recorder()
        context.once_report = once.ok
        outcome = await _run(context)
        assert outcome.succeeded is False
        assert outcome.reason == "state_db_error"
        assert once.calls == []

    async def test_restricted_report_failure_still_clock_invalid(
            self, environment):
        context, recorder, holder, _ = environment
        self._restrict(environment)
        once = Recorder()
        context.once_report = once.report_fails
        outcome = await _run(context)
        assert outcome.reason == "clock_invalid"
        # 一次报告机会失败不重试、不延长受限会话。
        assert once.calls == ["report"]


class TestSubmitVerification:
    pytestmark = pytest.mark.asyncio
    """提交结果未知的核实与恢复装配。"""

    async def test_unknown_submission_keeps_unknown_fact(self, tmp_path):
        """提交结果未知：不宣称成功或失败，保留未知事实供重送核实。"""
        from camctl.acceptance.input import ParsedInput
        _create_valid_database(tmp_path / "state.db")
        state_db = (tmp_path / "state.db").resolve()
        attempts = {"count": 0}
        source = ParsedInput(path=str(tmp_path / "plan.json"),
                             document=_document())

        class UnknownRepository:
            def process_input(self, command, key, owned):
                attempts["count"] += 1
                return DbOutcome(kind=DbOutcomeKind.UNKNOWN,
                                 error="commit unknown")

        context = SessionContext(
            mode=CommandMode.SUBMIT,
            catalog=None,
            clock=FakeClock([_MIN]),
            open_connection=lambda: open_existing(
                state_db, DbOpenMode.EXISTING_RW, DbConfig()),
            acceptance_repository=UnknownRepository(),
            session_repository=SessionRepository(),
            paths=SessionPaths(session_lock=tmp_path / "s.lock",
                               admission_lock=tmp_path / "a.lock"),
            clock_policy=lambda connection: ClockCheckInput(
                lower_bound_micros=None, min_plausible_micros=_MIN,
                recheck_delay_s=0.0, recheck_count=1),
            acquire_session=lambda: None,
            acquire_admission=lambda: None,
            facts_query=lambda connection: _facts(),
        )
        outcome = await run_session(context, source)
        assert outcome.succeeded is False
        assert outcome.reason == "state_db_error"
        assert outcome.needs_run is None
        assert outcome.details.get("unknown_commit") is True
        assert attempts["count"] == 1

    async def test_rolled_back_submission_keeps_plain_state_error(self, tmp_path):
        from camctl.acceptance.input import ParsedInput
        _create_valid_database(tmp_path / "state.db")
        state_db = (tmp_path / "state.db").resolve()
        source = ParsedInput(path=str(tmp_path / "plan.json"),
                             document=_document())

        class RolledBackRepository:
            def process_input(self, command, key, owned):
                return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK,
                                 error="guard rejected")

        context = SessionContext(
            mode=CommandMode.SUBMIT,
            catalog=None,
            clock=FakeClock([_MIN]),
            open_connection=lambda: open_existing(
                state_db, DbOpenMode.EXISTING_RW, DbConfig()),
            acceptance_repository=RolledBackRepository(),
            session_repository=SessionRepository(),
            paths=SessionPaths(session_lock=tmp_path / "s.lock",
                               admission_lock=tmp_path / "a.lock"),
            clock_policy=lambda connection: ClockCheckInput(
                lower_bound_micros=None, min_plausible_micros=_MIN,
                recheck_delay_s=0.0, recheck_count=1),
            acquire_session=lambda: None,
            acquire_admission=lambda: None,
            facts_query=lambda connection: _facts(),
        )
        outcome = await run_session(context, source)
        assert outcome.succeeded is False
        assert outcome.reason == "state_db_error"
        assert "unknown_commit" not in outcome.details


def _document() -> dict:
    return {
        "version": 1,
        "request_id": "42",
        "actions": [],
    }


def _CopyProbe(error, context):  # 简单占位工厂；实现侧消费 CopyRequest。
    return ("copy-request", str(error))

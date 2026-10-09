"""会话锁内消费本次已开始的本地责任，保留退出的主错误。"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from camctl.devices.bindings import DeviceConfigurationError
from camctl.session.clock import ClockBecameUntrusted
from camctl.session.locks import SessionLockConflictError, acquire_session, probe_admission
from camctl.session.service import StateDbFailure, run_session

from .test_session import _facts, environment  # noqa: F401


class LocalWork:
    """本地拥有者端口：任务独立持有连接，实际收场后才关闭连接。"""

    def __init__(self, context, *, failure=None):
        self.context = context
        self.failure = failure
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.owned = None
        self.task = None
        self.saved = 0
        self.closed = False

    def start(self):
        if self.task is None:
            self.owned = self.context.open_connection()
            self.task = asyncio.create_task(self._finish())
            self.started.set()

    async def _finish(self):
        try:
            await self.release.wait()
            self.owned.connection.execute("SELECT 1").fetchone()
            self.saved += 1
            if self.failure is not None:
                raise self.failure
        finally:
            self.owned.connection.close()
            self.closed = True

    def required_settlements(self):
        return int(self.task is not None and not self.task.done())

    async def settle(self):
        if self.task is not None:
            await asyncio.shield(self.task)


async def _turns(count=5):
    for _ in range(count):
        await asyncio.sleep(0)


def _assert_locks_held(context):
    assert not probe_admission(context.paths.admission_lock).is_free
    with pytest.raises(SessionLockConflictError):
        acquire_session(context.paths.session_lock)


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_kind", ["normal", "state", "configuration", "clock"])
async def test_session_waits_actual_local_owner_before_releasing_locks(environment, exit_kind):
    context, _recorder, _holder, _home = environment
    context.facts_query = lambda _connection: _facts()
    local = LocalWork(context)
    context.local_work = local
    device_rounds = []

    async def start(_context):
        local.start()

    async def device(_context):
        device_rounds.append(1)
        if exit_kind == "state":
            raise StateDbFailure("main state failure")
        if exit_kind == "configuration":
            raise DeviceConfigurationError("main configuration failure")
        if exit_kind == "clock":
            raise ClockBecameUntrusted("main clock failure")

    context.flows = {"local": start, "device": device}
    task = asyncio.create_task(run_session(context, None))
    try:
        await local.started.wait()
        await _turns()
        assert device_rounds, "后台文件责任不能阻塞设备流程"
        assert not task.done(), "实际责任未结束时会话不能返回"
        _assert_locks_held(context)
        assert local.owned.connection.execute("SELECT 1").fetchone() == (1,)
        local.release.set()
        outcome = await asyncio.wait_for(task, 1)
        assert local.saved == 1 and local.closed
        assert probe_admission(context.paths.admission_lock).is_free
        if exit_kind == "normal":
            assert outcome.succeeded
        else:
            assert outcome.reason == {"state": "state_db_error", "configuration": "configuration_error",
                                      "clock": "clock_invalid"}[exit_kind]
    finally:
        local.release.set()
        await asyncio.gather(task, local.task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("state_failure", [False, True])
async def test_repeated_cancel_waits_local_owner_and_preserves_cancellation(environment, state_failure):
    context, _recorder, _holder, _home = environment
    local = LocalWork(context, failure=(StateDbFailure("canceled cleanup commit unknown")
                                       if state_failure else None))
    context.local_work = local

    async def start(_context):
        local.start()

    context.flows = {"local": start}
    context.facts_query = lambda _connection: _facts(unfinished_actions=1)
    task = asyncio.create_task(run_session(context, None))
    try:
        await local.started.wait()
        task.cancel()
        await _turns()
        task.cancel()
        await _turns()
        assert not task.done()
        _assert_locks_held(context)
        assert not local.task.cancelled() and not local.closed
        local.release.set()
        with pytest.raises(asyncio.CancelledError) as canceled:
            await task
        assert local.saved == 1 and local.closed
        if state_failure:
            assert "canceled cleanup commit unknown" in str(getattr(canceled.value, "__notes__", ()))
    finally:
        local.release.set()
        await asyncio.gather(task, local.task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("primary", [None, "report_error", "state_db_error", "configuration_error", "clock_invalid"])
async def test_local_state_failure_preserves_exit_error_priority(environment, primary):
    context, _recorder, _holder, _home = environment
    context.facts_query = lambda _connection: _facts()
    local = LocalWork(context, failure=StateDbFailure("cleanup result commit unknown"))
    context.local_work = local
    if primary == "report_error":
        context.facts_query = lambda _connection: _facts(report_failed_no_new_changes=True)

    async def flow(_context):
        local.start()
        if primary == "state_db_error":
            raise StateDbFailure("primary state diagnostic")
        if primary == "configuration_error":
            raise DeviceConfigurationError("primary configuration diagnostic")
        if primary == "clock_invalid":
            raise ClockBecameUntrusted("primary clock diagnostic")

    context.flows = {"local": flow}
    task = asyncio.create_task(run_session(context, None))
    try:
        await local.started.wait()
        local.release.set()
        outcome = await asyncio.wait_for(task, 1)
        assert outcome.reason == ("state_db_error" if primary in (None, "report_error") else primary)
        assert "cleanup result commit unknown" in str(outcome.details)
        if primary in ("state_db_error", "configuration_error"):
            assert "primary" in str(outcome.details)
        if primary == "report_error":
            assert outcome.details["secondary_errors"][0]["reason"] == "report_error"
        assert local.closed
        with pytest.raises(sqlite3.ProgrammingError):
            local.owned.connection.execute("SELECT 1")
    finally:
        local.release.set()
        await asyncio.gather(task, local.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_clock_transition_stops_restricted_work_after_local_state_failure(environment):
    context, _recorder, _holder, _home = environment
    context.facts_query = lambda _connection: _facts()
    local = LocalWork(context, failure=StateDbFailure("cleanup state premise lost"))
    context.local_work = local
    restricted = []

    async def flow(_context):
        local.start()
        raise ClockBecameUntrusted("clock changed")

    async def limited(_context):
        restricted.append("stop or report")

    context.flows = {"local": flow}
    context.restricted_flows = {"winddown": limited}
    context.once_report = limited
    task = asyncio.create_task(run_session(context, None))
    try:
        await local.started.wait()
        _assert_locks_held(context)
        local.release.set()

        outcome = await asyncio.wait_for(task, 1)

        assert outcome.reason == "clock_invalid"
        assert "cleanup state premise lost" in str(outcome.details)
        assert local.saved == 1 and local.closed
        assert restricted == [], "状态库前提失效后不能继续依赖它的必要停止或报告"
    finally:
        local.release.set()
        await asyncio.gather(task, local.task, return_exceptions=True)

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


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_kind', ['state', 'configuration', 'unhandled'])
async def test_fatal_session_stops_new_local_work_before_emergency_await(environment, failure_kind):
    """致命错误一旦成立，其他后台流程不能在应急等待期间继续普通工作。"""
    context, _recorder, _holder, _home = environment
    calls = []

    class Local:
        def required_settlements(self):
            return 1

        def stop_new_work(self):
            calls.append('stop')

        async def settle(self):
            calls.append('settle')

    context.local_work = Local()
    failure = {'state': StateDbFailure('primary state'),
               'configuration': DeviceConfigurationError('primary configuration'),
               'unhandled': LookupError('primary internal')}[failure_kind]

    async def fail(_context):
        if failure_kind == 'unhandled':
            # 逃出中央异常分类的实际内部错误仍走必要收场。
            raise BaseExceptionGroup('unhandled control', [KeyboardInterrupt('primary control')])
        raise failure

    async def emergency():
        assert calls == ['stop']
        calls.append('emergency')
        await asyncio.sleep(0)
        return []

    context.flows = {'failure': fail}
    context.emergency_settlement = emergency
    if failure_kind == 'unhandled':
        with pytest.raises(BaseExceptionGroup):
            await run_session(context, None)
        assert calls[0] == 'stop'
    else:
        outcome = await run_session(context, None)
        assert outcome.reason == ('state_db_error' if failure_kind == 'state' else 'configuration_error')
        assert calls[:2] == ['stop', 'emergency']
    assert calls[-1] == 'settle'


@pytest.mark.asyncio
@pytest.mark.parametrize('exit_kind', ['state', 'normal', 'report', 'clock'])
@pytest.mark.parametrize('settle_failed', [False, True])
async def test_stop_failure_does_not_skip_emergency_or_actual_settle(environment, exit_kind, settle_failed):
    from camctl.bootstrap.background_flow import CombinedLocalWork
    from camctl.session.outcome import SessionOutcome
    context, _recorder, _holder, _home = environment
    calls = []
    stop_error = ValueError('unique original stop error')
    settle_error = LookupError('unique actual settlement error')

    class Owner:
        def required_settlements(self):
            return 0

        def stop_new_work(self):
            calls.append('stop')
            raise stop_error

        async def settle(self):
            calls.append('settle')
            if settle_failed:
                raise settle_error

    context.local_work = CombinedLocalWork((Owner(),))
    context.facts_query = lambda _connection: _facts(report_failed_no_new_changes=exit_kind == 'report')

    async def flow(_context):
        if exit_kind == 'state':
            raise StateDbFailure('primary state failure')
        if exit_kind == 'clock':
            raise ClockBecameUntrusted('primary clock failure')

    async def emergency():
        assert calls == ['stop']
        calls.append('emergency')
        await asyncio.sleep(0)
        return []

    context.flows = {'flow': flow}
    context.emergency_settlement = emergency
    outcome = await run_session(context, None)
    assert outcome.reason == ('clock_invalid' if exit_kind == 'clock' else 'state_db_error')
    assert calls.count('stop') == 1 and calls.count('settle') >= 1
    assert 'unique original stop error' in str(outcome.details)
    if settle_failed:
        assert 'unique actual settlement error' in str(outcome.details)
    if exit_kind == 'state':
        assert calls[:2] == ['stop', 'emergency']
        assert 'primary state failure' in str(outcome.details)
    else:
        assert 'emergency' not in calls
    # 同一个stop错误在final和前置收场中不能被重新登记。
    all_details = [dict(outcome.details), *[entry['details'] for entry in outcome.details.get('secondary_errors', ())]]
    assert sum('unique original stop error' in detail.get('message', '') for detail in all_details) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_startup_outcome_preserves_scan_until_real_parent_cancel(environment, cancel):
    from camctl.session.outcome import SessionOutcome
    context, _recorder, _holder, _home = environment
    calls, started, release = [], asyncio.Event(), asyncio.Event()

    class Owner:
        def stop_new_work(self):
            calls.append('stop')
            raise ValueError('startup stop error')

        def cancel_startup_sweep(self):
            calls.append('sweep')
            raise LookupError('startup sweep error')

        async def settle(self):
            calls.append('settle')
            started.set()
            await release.wait()
            calls.append('settled')

    owner = Owner()
    context.local_work = context.logging_owner = owner
    context.startup_outcome = SessionOutcome(False, reason='state_db_error', details={'message': 'existing startup'})
    task = asyncio.create_task(run_session(context, None))
    await started.wait()
    if cancel:
        task.cancel('first startup parent')
        await _turns()
        task.cancel('repeated startup parent')
        await _turns()
        assert calls == ['settle', 'stop', 'sweep'] and not task.done()
    else:
        assert calls == ['settle']
    release.set()
    if cancel:
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert str(caught.value) == 'first startup parent'
        assert 'startup stop error' in str(caught.value.__notes__)
        assert 'startup sweep error' in str(caught.value.__notes__)
    else:
        outcome = await task
        assert outcome is context.startup_outcome
    assert calls.count('settle') == calls.count('settled') == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_report', [False, True])
async def test_clock_resumed_log_permission_has_one_independent_final_stop(environment, cancel_report):
    from camctl.bootstrap.background_flow import CombinedLocalWork
    context, _recorder, _holder, _home = environment
    calls, report_entered, release_report = [], asyncio.Event(), asyncio.Event()

    class Owner:
        def __init__(self, name):
            self.name = name

        def required_settlements(self):
            return 0

        def stop_new_work(self):
            calls.append(('stop', self.name))

        async def settle(self):
            calls.append(('settle', self.name))

        def resume_restricted_logging(self):
            calls.append(('resume', self.name))

        def cancel_startup_sweep(self):
            calls.append(('sweep', self.name))

    ordinary, logging = Owner('ordinary'), Owner('logging')
    context.local_work = CombinedLocalWork((ordinary, logging))
    context.logging_owner = logging
    context.facts_query = lambda _connection: _facts()

    async def clock_failure(_context):
        raise ClockBecameUntrusted('late clock')

    async def winddown(_context):
        calls.append(('winddown', 'required'))

    async def report(_context):
        calls.append(('report', 'started'))
        report_entered.set()
        await release_report.wait()

    context.flows = {'clock': clock_failure}
    context.restricted_flows = {'winddown': winddown}
    context.once_report = report
    task = asyncio.create_task(run_session(context, None))
    await report_entered.wait()
    assert calls[:5] == [('stop', 'ordinary'), ('stop', 'logging'), ('settle', 'ordinary'), ('winddown', 'required'), ('settle', 'logging')]
    if cancel_report:
        task.cancel('restricted report parent')
        await _turns()
        task.cancel('restricted report repeated')
        await _turns()
        assert not task.done()
    release_report.set()
    if cancel_report:
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert str(caught.value) == 'restricted report parent'
        assert calls.count(('sweep', 'logging')) == 1
    else:
        outcome = await task
        assert outcome.reason == 'clock_invalid'
    assert calls.count(('resume', 'logging')) == 1
    assert calls.count(('stop', 'ordinary')) == 1
    assert calls.count(('stop', 'logging')) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('control_first', [False, True])
async def test_real_typed_stop_group_preserves_first_control_and_actual_settlement(environment, monkeypatch, control_first):
    from camctl.session import locks
    from camctl.session import service
    from .test_lock_error_classification import _FaultingLockBackend
    context, _recorder, _holder, home = environment
    calls, entered, release = [], asyncio.Event(), asyncio.Event()
    control = asyncio.CancelledError('actual synchronous unlock control')
    original_cause = RuntimeError('original typed cause')
    control.__cause__ = original_cause
    control.add_note('original typed note')
    lease = locks.acquire_session(home / 'stop-only.lock', backend=_FaultingLockBackend('unlock', control))
    group = None
    parents = []
    original_record = service._RequiredPhaseWaiter.record_parent_cancel

    def observe_parent(waiter, error):
        if waiter.parent_cancel is None:
            parents.append(error)
        return original_record(waiter, error)

    monkeypatch.setattr(service._RequiredPhaseWaiter, 'record_parent_cancel', observe_parent)

    class Owner:
        def stop_new_work(self):
            nonlocal group
            calls.append('stop')
            try:
                lease.close()
            except asyncio.CancelledError as error:
                assert error is control and locks.lock_error_facts(error) is not None
                group = BaseExceptionGroup('actual original stop group', [error, ValueError('group ordinary leaf')])
                raise group

        def cancel_startup_sweep(self):
            calls.append('sweep')

        async def settle(self):
            calls.append('settle')
            entered.set()
            await release.wait()
            calls.append('settled')

    context.local_work = context.logging_owner = Owner()
    context.facts_query = lambda _connection: _facts(unfinished_actions=1)

    async def flow(_context):
        if control_first:
            raise StateDbFailure('primary state failure')
        entered.set()
        await asyncio.Future()

    context.flows = {'flow': flow}
    task = asyncio.create_task(run_session(context, None))
    await entered.wait()
    if control_first:
        assert calls == ['stop', 'settle']
    task.cancel('original real parent')
    await _turns()
    task.cancel('repeated real parent')
    await _turns()
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert caught.value is (control if control_first else parents[0])
    assert len(parents) == 1
    assert control.__cause__ is original_cause
    assert control.__notes__[0] == 'original typed note'
    assert calls.count('stop') == calls.count('sweep') == calls.count('settle') == calls.count('settled') == 1
    assert group is not None and group.exceptions[0] is control
    assert 'group ordinary leaf' in str(caught.value.__notes__)
    assert 'original typed note' in str(caught.value.__notes__)


@pytest.mark.asyncio
async def test_same_stop_error_reappearing_at_connection_close_is_reported_once(environment):
    from dataclasses import replace
    context, _recorder, _holder, _home = environment
    failure = ValueError('same original stop and close error')
    original_open = context.open_connection

    class Connection:
        def __init__(self, actual):
            self.actual = actual

        def __getattr__(self, name):
            return getattr(self.actual, name)

        def close(self):
            self.actual.close()
            raise failure

    def open_connection():
        current = original_open()
        return replace(current, connection=Connection(current.connection))

    class Owner:
        def required_settlements(self):
            return 0

        def stop_new_work(self):
            raise failure

        async def settle(self):
            pass

    context.local_work = Owner()
    context.open_connection = open_connection
    context.facts_query = lambda _connection: _facts()
    outcome = await run_session(context, None)
    details = [dict(outcome.details), *[entry['details'] for entry in outcome.details.get('secondary_errors', ())]]
    assert outcome.reason == 'state_db_error'
    assert sum('same original stop and close error' in item.get('message', '') for item in details) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('raw_group', [False, True])
async def test_typed_control_in_original_flow_group_precedes_later_parent_cancel(environment, raw_group):
    from camctl.session import locks
    from .test_lock_error_classification import _FaultingLockBackend
    context, _recorder, _holder, home = environment
    control = asyncio.CancelledError('original flow typed control')
    if raw_group:
        control = BaseExceptionGroup('actual synchronous group', [control, ValueError('original group leaf')])
    lease = locks.acquire_session(home / 'flow-control.lock', backend=_FaultingLockBackend('unlock', control))
    settling, release = asyncio.Event(), asyncio.Event()
    calls = []

    class Owner:
        def stop_new_work(self):
            calls.append('stop')

        async def settle(self):
            settling.set()
            await release.wait()

    async def flow(_context):
        try:
            lease.close()
        except BaseException as error:
            if raw_group:
                raise
            raise BaseExceptionGroup('original flow control group', [error, ValueError('original group leaf')])

    context.local_work = Owner()
    context.flows = {'flow': flow}
    task = asyncio.create_task(run_session(context, None))
    await settling.wait()
    task.cancel('later parent')
    await _turns()
    release.set()
    with pytest.raises(type(control)) as caught:
        await task
    assert caught.value is control
    assert calls == ['stop']
    if raw_group:
        assert str(control.exceptions[1]) == 'original group leaf'
    else:
        assert 'original group leaf' in str(control.__notes__)
    assert 'later parent' in str(control.__notes__)

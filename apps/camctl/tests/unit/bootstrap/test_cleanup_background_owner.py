"""清理后台任务、共享数据库端口与原保存集合的完整拥有边界。"""

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, create_autospec

import pytest

from camctl.bootstrap import cleanup_assembly
from camctl.bootstrap.cleanup_assembly import cleanup_flow
from camctl.bootstrap.lifecycle import _local_work
from camctl.contracts.values import ConsistencyError
from camctl.scheduling.notifications import WorkNotifier, WakeReason
from camctl.session.service import StateDbFailure
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbReadError

pytestmark = pytest.mark.asyncio


def _context(wake=None):
    database = create_autospec(BusinessDatabase, instance=True)
    return SimpleNamespace(database=database, wake=wake), database


async def test_one_round_uses_shared_database_and_never_reenters(monkeypatch):
    release = asyncio.Event()
    runtime = object()
    factory = AsyncMock(return_value=runtime)
    factory.pending_collections = ()
    calls = []

    async def advance(current):
        calls.append(current)
        await release.wait()

    monkeypatch.setattr(cleanup_assembly, 'advance_cleanup', advance)
    context, owned = _context()
    flow = cleanup_flow(factory)
    await flow(context)
    for _ in range(3):
        await flow(context)
    assert calls == [runtime] and factory.call_count == 1
    factory.assert_awaited_once_with(context.database)
    owned.close.assert_not_called()
    assert flow.required_settlements() > 0
    release.set()
    await asyncio.sleep(0)
    owned.close.assert_not_called()
    assert flow.required_settlements() > 0  # 已完成但尚未交付。
    await flow(context)
    assert factory.call_count == 1 and flow.required_settlements() == 0
    await flow(context)
    await flow.settle()
    assert calls == [runtime, runtime]


async def test_stop_before_background_body_starts_does_not_open_connection():
    context, _owned = _context()
    factory = AsyncMock(return_value=None)
    factory.pending_collections = ()
    flow = cleanup_flow(factory)
    schedule = asyncio.create_task(flow(context))
    await asyncio.sleep(0)
    assert flow.required_settlements() == 1
    flow.stop_new_work()
    flow.stop_new_work()
    await schedule
    await flow.settle()
    await flow(context)
    _owned.close.assert_not_called()
    factory.assert_not_called()
    assert flow.required_settlements() == 0


@pytest.mark.parametrize('phase', range(6))
async def test_original_pending_is_checked_after_task_and_survives_stopped_owner(monkeypatch, phase):
    pending = tuple({} for _ in range(6))
    original = object()
    pending[phase]['original'] = original
    context, owned = _context()
    flow = cleanup_flow(AsyncMock(return_value=None), pending_collections=pending)
    await flow(context)
    with pytest.raises(StateDbFailure):
        await flow.settle()
    owned.close.assert_not_called()
    assert flow.required_settlements() == 1
    flow.stop_new_work()
    await flow(context)
    assert pending[phase]['original'] is original
    owned.close.assert_not_called()

    async def resume(collections):
        assert collections is pending and collections[phase]['original'] is original
        collections[phase].clear()

    monkeypatch.setattr(cleanup_assembly, 'advance_cleanup', resume)
    replacement = cleanup_flow(AsyncMock(return_value=pending), pending_collections=pending)
    await replacement(context)
    await replacement.settle()
    assert flow.required_settlements() == replacement.required_settlements() == 0


@pytest.mark.parametrize('failure', [ValueError('raw invalid'), ConsistencyError('save unknown'),
    sqlite3.OperationalError('db failed'), asyncio.CancelledError('internal control')])
async def test_original_failure_is_delivered_before_pending_and_keeps_identity(monkeypatch, failure):
    release = asyncio.Event()
    pending = ({},)
    context, owned = _context()

    async def advance(_runtime):
        await release.wait()
        pending[0]['original'] = failure
        raise failure

    monkeypatch.setattr(cleanup_assembly, 'advance_cleanup', advance)
    flow = cleanup_flow(AsyncMock(return_value=object()), pending_collections=pending)
    await flow(context)
    release.set()
    await asyncio.sleep(0)
    assert flow.required_settlements() == 2
    if isinstance(failure, Exception):
        with pytest.raises(ExceptionGroup) as caught:
            await flow.settle()
        assert caught.value.exceptions[0] is failure
        assert isinstance(caught.value.exceptions[1], StateDbFailure)
    else:
        with pytest.raises(asyncio.CancelledError) as caught:
            await flow.settle()
        assert caught.value is failure
        assert 'StateDbFailure' in str(caught.value.__notes__)
    assert pending[0]['original'] is failure
    owned.close.assert_not_called()
    assert flow.required_settlements() == 1


@pytest.mark.parametrize('failure_type', [ConsistencyError, sqlite3.OperationalError, DbReadError])
async def test_state_db_classification_preserves_original_cause(monkeypatch, failure_type):
    failure = failure_type('original state fault')

    async def advance(_runtime):
        raise failure

    monkeypatch.setattr(cleanup_assembly, 'advance_cleanup', advance)
    context, owned = _context()
    flow = cleanup_flow(AsyncMock(return_value=object()), state_db_errors=True)
    with pytest.raises(StateDbFailure) as caught:
        await flow(context)
        await flow.settle()
    assert caught.value.__cause__ is failure
    owned.close.assert_not_called()
    assert flow.required_settlements() == 0


async def test_factory_directory_binding_error_keeps_original_classification():
    from camctl.persistence.runtime import DirectoryBindingError

    context, database = _context()
    original = DirectoryBindingError("原目录绑定不对应")
    factory = AsyncMock(side_effect=original)
    factory.pending_collections = ()
    flow = cleanup_flow(factory, state_db_errors=True)
    with pytest.raises(DirectoryBindingError) as caught:
        await flow(context)
        await flow.settle()
    assert caught.value is original
    factory.assert_awaited_once_with(database)
    database.close.assert_not_awaited()


async def test_repeated_settle_observer_cancel_keeps_actual_task_and_connection(monkeypatch):
    release = asyncio.Event()
    saved = []

    async def advance(_runtime):
        await release.wait()
        saved.append('actual')

    monkeypatch.setattr(cleanup_assembly, 'advance_cleanup', advance)
    context, owned = _context()
    flow = cleanup_flow(AsyncMock(return_value=object()))
    await flow(context)
    original_task = flow._notification_task
    observer = asyncio.create_task(flow.settle())
    await asyncio.sleep(0)
    observer.cancel('first observer')
    await asyncio.sleep(0)
    observer.cancel('second observer')
    await asyncio.sleep(0)
    assert not observer.done() and flow.required_settlements() > 0
    assert flow._notification_task is original_task
    owned.close.assert_not_called()
    release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await observer
    assert str(caught.value) == 'first observer'
    assert saved == ['actual'] and flow.required_settlements() == 0
    assert flow._notification_task is None and not flow._notified
    owned.close.assert_not_called()


@pytest.mark.parametrize('entry', ['call', 'check', 'settle'])
@pytest.mark.parametrize('failed', [False, True])
async def test_each_consumption_releases_notification_task_even_on_original_error(monkeypatch, entry, failed):
    release = asyncio.Event()
    failure = ValueError('original cleanup error')

    async def advance(_runtime):
        await release.wait()
        if failed:
            raise failure

    monkeypatch.setattr(cleanup_assembly, 'advance_cleanup', advance)
    context, _owned = _context(WorkNotifier())
    flow = cleanup_flow(AsyncMock(return_value=object()))
    await flow(context)
    task = flow._notification_task
    assert task is flow._background.unconsumed_task
    flow.check_completed()
    assert flow._notification_task is task and not task.done()
    release.set()
    await asyncio.sleep(0)
    assert task.done() and flow._notification_task is task

    async def consume():
        if entry == 'call':
            await flow(context)
        elif entry == 'check':
            flow.check_completed()
        else:
            await flow.settle()

    if failed:
        with pytest.raises(ValueError) as caught:
            await consume()
        assert caught.value is failure
    else:
        await consume()
    assert flow._background.unconsumed_task is None
    assert flow._notification_task is None and not flow._notified
    assert flow.required_settlements() == 0


@pytest.mark.parametrize('point', ['before_discovery', 'during_discovery', 'before_wait', 'waiting'])
async def test_completion_notification_keeps_unconsumed_result_across_wait_interleavings(monkeypatch, point):
    wake = WorkNotifier()
    context, owned = _context(wake)
    release = asyncio.Event()

    async def advance(_runtime):
        await release.wait()

    monkeypatch.setattr(cleanup_assembly, 'advance_cleanup', advance)
    observed = wake.snapshot()
    flow = cleanup_flow(AsyncMock(return_value=object()))
    await flow(context)
    if point == 'during_discovery':
        observed = wake.snapshot()
    elif point == 'before_wait':
        flow.check_completed()
    waiter = None
    if point == 'waiting':
        waiter = asyncio.create_task(wake.wait_changed(observed))
        await asyncio.sleep(0)
    release.set()
    await asyncio.sleep(0)
    changed = await asyncio.wait_for(waiter or wake.wait_changed(observed), 1)
    assert changed.version > observed.version
    assert WakeReason.ACTIVITY_FINISHED in changed.reasons
    owned.close.assert_not_called()
    assert flow.required_settlements() == 1
    flow.check_completed()
    assert flow.required_settlements() == 0


async def test_external_local_work_registers_complete_cleanup_owner_once(monkeypatch):
    context, owned = _context()
    release = asyncio.Event()

    async def advance(_runtime):
        await release.wait()

    monkeypatch.setattr(cleanup_assembly, 'advance_cleanup', advance)
    flow = cleanup_flow(AsyncMock(return_value=object()))
    outer = _local_work(SimpleNamespace(background_flows=[flow], work_files=None),
                        {'cleanup': flow, 'duplicate': flow})
    await flow(context)
    assert outer.required_settlements() == flow.required_settlements()
    release.set()
    await outer.settle()
    assert outer.required_settlements() == 0
    owned.close.assert_not_called()


@pytest.mark.parametrize('already_done', [False, True])
@pytest.mark.parametrize('failure', [None, ConsistencyError('original delayed save'), asyncio.CancelledError('internal delayed control')])
async def test_scheduling_waiter_cancel_arms_actual_task_even_when_already_done(monkeypatch, already_done, failure):
    wake = WorkNotifier()
    context, owned = _context(wake)
    release = asyncio.Event()

    async def advance(_runtime):
        if not already_done:
            await release.wait()
        if failure is not None:
            raise failure

    monkeypatch.setattr(cleanup_assembly, 'advance_cleanup', advance)
    owner = cleanup_flow(AsyncMock(return_value=object()))
    schedule = asyncio.create_task(owner(context))
    asyncio.get_running_loop().call_soon(schedule.cancel, 'schedule observer')
    with pytest.raises(asyncio.CancelledError) as caught:
        await schedule
    assert str(caught.value) == 'schedule observer'
    assert owner.required_settlements() > 0
    assert owner._notification_task is owner._background.unconsumed_task
    assert owner._notification_task is not None
    release.set()
    for _ in range(3):
        await asyncio.sleep(0)
    assert wake.snapshot().version == 1
    assert owner.required_settlements() == 1
    if failure is None:
        await owner.settle()
    else:
        with pytest.raises(type(failure)) as actual:
            await owner.settle()
        assert actual.value is failure
    owned.close.assert_not_called()
    assert owner.required_settlements() == 0
    assert owner._notification_task is None and not owner._notified


async def test_queued_stop_after_scheduling_cancel_notifies_without_entering_round():
    wake = WorkNotifier()
    context, _owned = _context(wake)
    owner = cleanup_flow(AsyncMock(return_value=None))
    schedule = asyncio.create_task(owner(context))
    await asyncio.sleep(0)
    schedule.cancel('queued schedule observer')
    owner.stop_new_work()
    with pytest.raises(asyncio.CancelledError):
        await schedule
    for _ in range(2):
        await asyncio.sleep(0)
    assert wake.snapshot().version == 1
    _owned.close.assert_not_called()
    assert owner.required_settlements() == 1
    await owner.settle()
    assert owner.required_settlements() == 0


@pytest.mark.parametrize('failed', [False, True])
async def test_consuming_before_callback_invalidates_old_notification_and_next_round(monkeypatch, failed):
    wake = WorkNotifier()
    context, _owned = _context(wake)
    gates = [asyncio.Event(), asyncio.Event()]
    failure = ValueError('first actual failure')
    calls = []

    async def advance(_runtime):
        index = len(calls)
        calls.append(index)
        await gates[index].wait()
        if index == 0 and failed:
            raise failure

    monkeypatch.setattr(cleanup_assembly, 'advance_cleanup', advance)
    owner = cleanup_flow(AsyncMock(return_value=object()))
    await owner(context)
    for _ in range(3):
        await owner(context)
    gates[0].set()
    await asyncio.sleep(0)
    if failed:
        with pytest.raises(ValueError) as caught:
            owner.check_completed()
        assert caught.value is failure
    else:
        owner.check_completed()
    assert owner._notification_task is None and not owner._notified
    assert wake.snapshot().version == 0
    await owner(context)
    assert calls == [0, 1] and wake.snapshot().version == 0
    assert owner._notification_task is owner._background.unconsumed_task
    gates[1].set()
    for _ in range(2):
        await asyncio.sleep(0)
    assert wake.snapshot().version == 1
    await owner.settle()
    assert owner._notification_task is None and not owner._notified


async def test_cleanup_stop_attempts_background_after_round_failure_and_keeps_both(monkeypatch):
    context, _owned = _context()
    owner = cleanup_flow(AsyncMock(return_value=None))
    calls = []
    first, second = ValueError('round stop failure'), LookupError('background stop failure')

    def fail_round():
        calls.append('round')
        raise first

    def fail_background():
        calls.append('background')
        raise second

    monkeypatch.setattr(owner._round, 'stop_new_work', fail_round)
    monkeypatch.setattr(owner._background, 'stop_new_work', fail_background)
    with pytest.raises(ExceptionGroup) as caught:
        owner.stop_new_work()
    assert calls == ['round', 'background']
    assert caught.value.exceptions == (first, second)

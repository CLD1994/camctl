"""停止入口失败后仍完整尝试各拥有者，并等待真实收场。"""

import asyncio
import sys
from types import SimpleNamespace

import pytest

from camctl.bootstrap.background_flow import CombinedLocalWork
from camctl.bootstrap.flows import CaptureFlow
from camctl.session.service import _RequiredPhaseWaiter, _settle_local_work

pytestmark = pytest.mark.asyncio


class Owner:
    def __init__(self, name, calls, *, stop_error=None, settle_error=None, gate=None):
        self.name, self.calls = name, calls
        self.stop_error, self.settle_error, self.gate = stop_error, settle_error, gate

    def stop_new_work(self):
        self.calls.append(('stop', self.name))
        if self.stop_error is not None:
            raise self.stop_error

    def cancel_startup_sweep(self):
        self.calls.append(('sweep', self.name))
        if self.stop_error is not None:
            raise self.stop_error

    def required_settlements(self):
        return 1

    async def settle(self):
        self.calls.append(('settle', self.name))
        if self.gate is not None:
            await self.gate.wait()
        self.calls.append(('settled', self.name))
        if self.settle_error is not None:
            raise self.settle_error


@pytest.mark.parametrize('position', range(3))
async def test_combined_stop_attempts_all_owners_before_delivering_original_failure(position):
    calls = []
    failure = ValueError('original stop failed')
    owners = [Owner(str(i), calls, stop_error=failure if i == position else None) for i in range(3)]
    with pytest.raises(ValueError) as caught:
        CombinedLocalWork(owners).stop_new_work()
    assert caught.value is failure
    assert calls == [('stop', '0'), ('stop', '1'), ('stop', '2')]


@pytest.mark.parametrize('settle_failed', [False, True])
async def test_helper_initial_stop_failure_still_waits_all_actual_settlements(settle_failed):
    calls = []
    failure = ValueError('original stop failed')
    actual_failure = LookupError('original settle failed') if settle_failed else None
    owner = Owner('first', calls, stop_error=failure, settle_error=actual_failure)
    context = SimpleNamespace(local_work=CombinedLocalWork((owner, Owner('last', calls))), logging_owner=None)
    expected = ExceptionGroup if settle_failed else ValueError
    with pytest.raises(expected) as caught:
        await _settle_local_work(context)
    assert calls == [('stop', 'first'), ('stop', 'last'), ('settle', 'first'), ('settled', 'first'), ('settle', 'last'), ('settled', 'last')]
    if settle_failed:
        assert caught.value.exceptions == (failure, actual_failure)
    else:
        assert caught.value is failure


@pytest.mark.parametrize('control', [False, True])
async def test_combined_stop_preserves_original_group_and_control_identity(control):
    calls = []
    first = asyncio.CancelledError('original stop control') if control else ValueError('first stop')
    second = ExceptionGroup('original group', [LookupError('second leaf')])
    combined = CombinedLocalWork((Owner('first', calls, stop_error=first), Owner('last', calls, stop_error=second)))
    if control:
        with pytest.raises(asyncio.CancelledError) as caught:
            combined.stop_new_work()
        assert caught.value is first
        assert 'original group' in str(first.__notes__)
    else:
        with pytest.raises(ExceptionGroup) as caught:
            combined.stop_new_work()
        assert caught.value.exceptions == (first, second)
    assert calls == [('stop', 'first'), ('stop', 'last')]


async def test_capture_children_and_later_outer_owner_all_receive_stop_after_failure():
    calls = []
    failure = ValueError('capture child stop')
    capture = CaptureFlow(None)
    capture._devices = {'first': Owner('first', calls, stop_error=failure), 'last': Owner('last', calls)}
    outer = CombinedLocalWork((capture, Owner('outer', calls)))
    with pytest.raises(ValueError) as caught:
        outer.stop_new_work()
    assert caught.value is failure
    assert calls == [('stop', 'first'), ('stop', 'last'), ('stop', 'outer')]


@pytest.mark.parametrize('stop_failed,sweep_failed', [(True, False), (False, True), (True, True)])
async def test_observer_repeated_cancel_does_not_skip_or_repeat_stop_or_settle(stop_failed, sweep_failed):
    calls, gate = [], asyncio.Event()
    stop = ValueError('ordinary stop') if stop_failed else None
    sweep = LookupError('startup sweep') if sweep_failed else None
    context = SimpleNamespace(local_work=Owner('work', calls, stop_error=stop, gate=gate),
        logging_owner=Owner('log', calls, stop_error=sweep))
    observer = asyncio.create_task(_settle_local_work(context, stop_new=False))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    observer.cancel('first observer')
    await asyncio.sleep(0)
    observer.cancel('second observer')
    await asyncio.sleep(0)
    assert not observer.done()
    assert calls == [('settle', 'work'), ('stop', 'work'), ('sweep', 'log')]
    gate.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await observer
    assert str(caught.value) == 'first observer'
    failures = [e for e in (stop, sweep) if e is not None]
    if len(failures) == 1:
        assert caught.value.__cause__ is failures[0]
    else:
        assert caught.value.__cause__.exceptions == tuple(failures)
    assert calls[-1] == ('settled', 'work')


async def test_parent_cancel_records_original_stop_and_sweep_once_before_actual_settle():
    calls = []
    stop = asyncio.CancelledError('internal stop control')
    sweep = ValueError('sweep failure')
    context = SimpleNamespace(local_work=Owner('work', calls, stop_error=stop), logging_owner=Owner('log', calls, stop_error=sweep))
    waiter = _RequiredPhaseWaiter(context)
    parent = asyncio.CancelledError('real parent')
    waiter.record_parent_cancel(parent)
    waiter.record_parent_cancel(asyncio.CancelledError('repeated parent'))
    assert waiter.parent_cancel is waiter.first_control is parent
    with pytest.raises(asyncio.CancelledError) as caught:
        await _settle_local_work(context, waiter=waiter)
    assert caught.value is stop
    assert calls == [('stop', 'work'), ('sweep', 'log'), ('settle', 'work'), ('settled', 'work')]
    assert waiter.take_stop_errors() == ()
    assert 'sweep failure' in str(stop.__notes__)


async def test_restricted_logging_new_permit_stops_only_resumed_log_once():
    calls = []
    context = SimpleNamespace(local_work=Owner('ordinary', calls), logging_owner=Owner('log', calls))
    waiter = _RequiredPhaseWaiter(context)
    await _settle_local_work(context, waiter=waiter)
    waiter.resumed_restricted_logging()
    waiter.request_stop()
    waiter.request_stop()
    assert calls == [('stop', 'ordinary'), ('settle', 'ordinary'), ('settled', 'ordinary'), ('stop', 'log')]


async def test_normal_settlement_does_not_consume_future_stop_request():
    calls = []
    context = SimpleNamespace(local_work=Owner('ordinary', calls), logging_owner=None)
    waiter = _RequiredPhaseWaiter(context)
    await _settle_local_work(context, stop_new=False, waiter=waiter)
    assert calls == [('settle', 'ordinary'), ('settled', 'ordinary')]
    waiter.request_stop()
    assert calls[-1] == ('stop', 'ordinary')


@pytest.mark.parametrize('link', ['direct', 'group_leaf', 'nested_group'])
@pytest.mark.parametrize('existing_cause', [False, True])
async def test_observer_keeps_notes_without_creating_reverse_cause_cycle(link, existing_cause):
    entered, release, stopped = (asyncio.Event() for _ in range(3))
    failure = ValueError('original stop diagnostic')
    failure.add_note('original stop note')
    previous = LookupError('original observer cause')
    observed = []

    class Work:
        def stop_new_work(self):
            cancellation = sys.exception()
            observed.append(cancellation)
            cancellation.add_note('original observer note')
            if existing_cause:
                cancellation.__cause__ = previous
            if link == 'direct':
                failure.__cause__ = cancellation
            elif link == 'group_leaf':
                failure.__cause__ = BaseExceptionGroup('existing cause group', [cancellation])
            else:
                intermediate = LookupError('existing intermediate')
                intermediate.__cause__ = cancellation
                failure.__cause__ = ExceptionGroup('existing cause group', [intermediate])
            stopped.set()
            raise failure

        async def settle(self):
            entered.set()
            await release.wait()

    context = SimpleNamespace(local_work=Work(), logging_owner=None)
    observer = asyncio.create_task(_settle_local_work(context, stop_new=False))
    await entered.wait()
    observer.cancel('original observer')
    await stopped.wait()
    release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await observer
    assert caught.value is observed[0]
    assert caught.value.__cause__ is (previous if existing_cause else None)
    assert caught.value.__notes__[0] == 'original observer note'
    assert 'original stop diagnostic' in str(caught.value.__notes__)
    assert 'original stop note' in str(caught.value.__notes__)


@pytest.mark.parametrize('stop_new', [False, True])
async def test_helper_without_local_work_completes_normally(stop_new):
    await _settle_local_work(SimpleNamespace(local_work=None, logging_owner=None), stop_new=stop_new)


@pytest.mark.parametrize('grouped', [False, True])
async def test_capture_completed_error_survives_failing_sibling_stop(grouped):
    calls = []
    original = LookupError('original completed device error')
    stop = ValueError('sibling stop diagnostic')
    if grouped:
        stop = ExceptionGroup('actual child group', [stop, LookupError('other original child leaf')])

    class Completed(Owner):
        def check_completed(self):
            raise original

    capture = CaptureFlow(None)
    capture._devices = {'done': Completed('done', calls, stop_error=stop), 'sibling': Owner('sibling', calls)}
    with pytest.raises(LookupError) as caught:
        capture.check_completed()
    assert caught.value is original
    assert calls == [('stop', 'done'), ('stop', 'sibling')]
    assert 'sibling stop diagnostic' in str(original.__notes__)
    if grouped:
        assert 'other original child leaf' in str(original.__notes__)


async def test_capture_actual_error_keeps_classification_and_stops_all_other_children():
    import sqlite3
    from unittest.mock import Mock
    from camctl.session.service import StateDbFailure
    calls = []
    original = sqlite3.OperationalError('original device database')
    stop = ValueError('sibling stop diagnostic')

    def factory(_owned, _device):
        raise original

    connection = SimpleNamespace(close=Mock())
    context = SimpleNamespace(open_connection=lambda: SimpleNamespace(connection=connection))
    capture = CaptureFlow(factory)
    capture._devices = {'current': Owner('current', calls), 'first': Owner('first', calls, stop_error=stop), 'last': Owner('last', calls)}
    with pytest.raises(StateDbFailure) as caught:
        await capture._advance_device(context, 'current', ())
    assert caught.value.__cause__ is original
    assert calls == [('stop', 'first'), ('stop', 'last')]
    assert 'sibling stop diagnostic' in str(original.__notes__)
    connection.close.assert_called_once()

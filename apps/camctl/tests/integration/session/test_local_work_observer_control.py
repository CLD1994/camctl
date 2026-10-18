"""真实锁控制与本地收场观察者取消分别保持首次先后。"""

import asyncio
import sys
from types import SimpleNamespace

import pytest

from camctl.session import locks
from camctl.session.service import _RequiredPhaseWaiter, _settle_local_work

from .test_lock_error_classification import _FaultingLockBackend

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize('source,before,stop_new', [
    ('stop', False, False), ('stop', True, False), ('stop', True, True),
    ('settle', False, False), ('settle', False, True),
    ('settle', True, False), ('settle', True, True),
])
@pytest.mark.parametrize('shape', ['single', 'wrapper', 'typed_group'])
async def test_helper_observer_and_real_typed_control_keep_first_identity(
        tmp_path, source, shape, before, stop_new):
    control = asyncio.CancelledError('actual lock control')
    ordinary = ValueError('original ordinary leaf')
    raw = (BaseExceptionGroup('actual typed group', [control, ordinary])
           if shape == 'typed_group' else control)
    original_cause = LookupError('original cause')
    raw.__cause__ = original_cause
    raw.add_note('original note')
    backend = _FaultingLockBackend('unlock', raw)
    lease = locks.acquire_session(tmp_path / 'observer-control.lock', backend=backend)
    entered, release, swept, finished = (asyncio.Event() for _ in range(4))
    calls, observed, actual_errors = [], [], []
    observer = None

    def fail_from_lock():
        try:
            lease.close()
        except BaseException as error:
            assert error is raw and locks.lock_error_facts(error) is not None
            actual = (BaseExceptionGroup('original wrapper', [error, ordinary])
                      if shape == 'wrapper' else error)
            actual_errors.append(actual)
            raise actual

    class Owner:
        def stop_new_work(self):
            calls.append('stop')
            if source == 'stop':
                fail_from_lock()

        async def settle(self):
            entered.set()
            try:
                if not (source == 'settle' and before):
                    await release.wait()
                if source == 'settle':
                    fail_from_lock()
            finally:
                finished.set()

    class Logging:
        def cancel_startup_sweep(self):
            calls.append('sweep')
            observed.append(sys.exception())
            swept.set()

    context = SimpleNamespace(local_work=Owner(), logging_owner=Logging())

    class Records(_RequiredPhaseWaiter):
        def record_phase_error(self, error):
            super().record_phase_error(error)
            if source == 'settle' and before:
                # 实际来源已登记，collect 尚未交付；取消只中断外部观察。
                observer.cancel('first observer')

    records = Records(context)
    if source == 'stop' and before and not stop_new:
        records.request_stop()
    observer = asyncio.create_task(_settle_local_work(context, stop_new=stop_new, waiter=records))
    try:
        await entered.wait()
        if not (source == 'settle' and before):
            observer.cancel('first observer')
        await swept.wait()
        cancellation = observed[0]
        assert isinstance(cancellation, asyncio.CancelledError)
        assert records.parent_cancel is None
        observer.cancel('repeated observer')
        await asyncio.sleep(0)
        if not finished.is_set():
            assert not observer.done()
        release.set()
        expected = raw if before else cancellation
        with pytest.raises(type(expected)) as caught:
            await observer
        assert caught.value is expected
        assert finished.is_set() and calls == ['stop', 'sweep']
        assert records.first_control is raw
        assert backend.calls['close'] == backend.calls['unlock'] == 1
        assert raw.__cause__ is original_cause
        assert 'original note' in raw.__notes__
        if before:
            assert 'first observer' in str(expected.__notes__)
            if shape == 'wrapper':
                assert 'original ordinary leaf' in str(expected.__notes__)
        else:
            assert cancellation.__cause__ is actual_errors[0]
            assert 'actual lock control' in str(cancellation.__notes__)
        if shape != 'single':
            assert actual_errors[0].exceptions == (control, ordinary)
    finally:
        release.set()
        await asyncio.gather(observer, return_exceptions=True)


@pytest.mark.parametrize('prior', ['delivered', 'parent_first', 'control_first'])
async def test_helper_does_not_reacquire_delivered_control_or_shared_parent(tmp_path, prior):
    control = asyncio.CancelledError('actual earlier lock control')
    lease = locks.acquire_session(tmp_path / 'prior-control.lock',
        backend=_FaultingLockBackend('unlock', control))
    entered, release, observed = asyncio.Event(), asyncio.Event(), []
    calls = []

    class Owner:
        def stop_new_work(self):
            calls.append('stop')
            lease.close()

        async def settle(self):
            entered.set()
            await release.wait()
            calls.append('settled')

    class Logging:
        def cancel_startup_sweep(self):
            calls.append('sweep')

    context = SimpleNamespace(local_work=Owner(), logging_owner=Logging())

    class Records(_RequiredPhaseWaiter):
        def request_stop(self):
            if isinstance(sys.exception(), asyncio.CancelledError):
                observed.append(sys.exception())
            super().request_stop()

    records = Records(context)
    parent = asyncio.CancelledError('actual run parent')
    if prior == 'parent_first':
        records.record_parent_cancel(parent)
    else:
        records.request_stop()
        if prior == 'control_first':
            records.record_parent_cancel(parent)
        else:
            release.set()
            with pytest.raises(asyncio.CancelledError) as delivered:
                await _settle_local_work(context, waiter=records)
            assert delivered.value is control
            assert records.take_stop_errors() == ()
            entered.clear()
            release.clear()

    observer = asyncio.create_task(_settle_local_work(context, stop_new=False, waiter=records))
    await entered.wait()
    observer.cancel('local observer')
    await asyncio.sleep(0)
    observer.cancel('repeated local observer')
    await asyncio.sleep(0)
    assert observed and not observer.done()
    release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await observer
    assert caught.value is (control if prior == 'control_first' else observed[0])
    assert records.first_control is (parent if prior == 'parent_first' else control)
    assert records.parent_cancel is (None if prior == 'delivered' else parent)
    assert calls.count('stop') == calls.count('sweep') == 1
    assert calls[-1] == 'settled'
    if prior == 'parent_first':
        assert caught.value.__cause__ is control
    if prior == 'delivered':
        assert caught.value.__cause__ is None


@pytest.mark.parametrize('shape', ['single', 'wrapper', 'typed_group'])
async def test_observer_first_keeps_real_startup_sweep_control(tmp_path, shape):
    control = asyncio.CancelledError('actual sweep lock control')
    ordinary = ValueError('original sweep leaf')
    raw = BaseExceptionGroup('actual sweep group', [control, ordinary]) if shape == 'typed_group' else control
    lease = locks.acquire_session(tmp_path / 'sweep-control.lock', backend=_FaultingLockBackend('unlock', raw))
    entered, release, swept = (asyncio.Event() for _ in range(3))
    observed, actual_errors, calls = [], [], []

    class Owner:
        def stop_new_work(self):
            calls.append('stop')
            observed.append(sys.exception())

        async def settle(self):
            entered.set()
            await release.wait()
            calls.append('settled')

    class Logging:
        def cancel_startup_sweep(self):
            calls.append('sweep')
            swept.set()
            try:
                lease.close()
            except BaseException as error:
                actual = BaseExceptionGroup('original sweep wrapper', [error, ordinary]) if shape == 'wrapper' else error
                actual_errors.append(actual)
                raise actual

    context = SimpleNamespace(local_work=Owner(), logging_owner=Logging())
    records = _RequiredPhaseWaiter(context)
    observer = asyncio.create_task(_settle_local_work(context, stop_new=False, waiter=records))
    await entered.wait()
    observer.cancel('observer before sweep')
    await swept.wait()
    release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await observer
    assert caught.value is observed[0]
    assert caught.value.__cause__ is actual_errors[0]
    assert records.first_control is raw and records.parent_cancel is None
    assert calls == ['stop', 'sweep', 'settled']


@pytest.mark.parametrize('source', ['stop', 'settle'])
@pytest.mark.parametrize('shape', ['single', 'wrapper', 'typed_group'])
async def test_without_observer_cancel_delivers_original_typed_error_shape(tmp_path, source, shape):
    control = asyncio.CancelledError('actual unobserved lock control')
    ordinary = ValueError('original ordinary leaf')
    raw = BaseExceptionGroup('actual typed group', [control, ordinary]) if shape == 'typed_group' else control
    lease = locks.acquire_session(tmp_path / 'unobserved-control.lock', backend=_FaultingLockBackend('unlock', raw))
    errors, calls = [], []

    def fail():
        try:
            lease.close()
        except BaseException as error:
            actual = BaseExceptionGroup('original wrapper', [error, ordinary]) if shape == 'wrapper' else error
            errors.append(actual)
            raise actual

    class Owner:
        def stop_new_work(self):
            calls.append('stop')
            if source == 'stop':
                fail()

        async def settle(self):
            calls.append('settled')
            if source == 'settle':
                fail()

    context = SimpleNamespace(local_work=Owner(), logging_owner=None)
    records = _RequiredPhaseWaiter(context)
    with pytest.raises(BaseExceptionGroup if shape != 'single' else asyncio.CancelledError) as caught:
        await _settle_local_work(context, waiter=records)
    assert caught.value is errors[0]
    assert records.first_control is raw and records.parent_cancel is None
    assert calls == ['stop', 'settled']

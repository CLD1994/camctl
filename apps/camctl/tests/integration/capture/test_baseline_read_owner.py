"""C1实际目录调用与公开基准仓储共同保存原输入和恢复责任。"""
import asyncio
from dataclasses import replace

import pytest

from camctl.contracts.pages import Page
from camctl.devices.directory import DirectoryCursor, DirectoryRead
from camctl.operations.models import ErrorValue
from camctl.operations.recovery import RecoveryBoundary
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from ..support.baseline_cases import (baseline_case, baseline_case_for,
    BaselineReadBoundary, baseline_read_world, baseline_handler, request_baseline_cancel)
from .baseline_database import database, baseline_read_intent as _intent
from ..scheduling.test_start_action import _SCHEDULED

pytestmark = pytest.mark.asyncio


class BlockingDirectory:
    def __init__(self, error=None):
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []
        self.error = error

    async def read_directory(self, request, *, stop):
        self.calls.append(request)
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.release.wait()
        if self.error is not None:
            return DirectoryRead(None, self.error, None)
        return DirectoryRead(None, None, Page((), DirectoryCursor(
            request.binding, request.directories, 1, None)))


class ObserveRead(CaptureRepository):
    def __init__(self, *, stop_after_return=True):
        self.intents, self.returns, self.unknowns = [], [], []
        self.stop_after_return = stop_after_return

    def save_baseline_read_intent(self, request, key, owned):
        if request.read_no > 1:
            raise BaselineReadBoundary()
        self.intents.append((request, key))
        return super().save_baseline_read_intent(request, key, owned)

    def save_baseline_read_return(self, request, key, owned):
        self.returns.append((request, key))
        saved = super().save_baseline_read_return(request, key, owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        return replace(saved, handoff_error=BaselineReadBoundary()) if self.stop_after_return else saved

    def save_baseline_read_unconfirmed(self, request, key, owned):
        self.unknowns.append((request, key))
        return super().save_baseline_read_unconfirmed(request, key, owned)


async def _stored(owned):
    return owned.connection.execute(
        "SELECT state,result_event_id,returned_at,error_json FROM baseline_read_calls").fetchone()


async def _cold(owned, *, boundary=RecoveryBoundary.HOST_LOCAL_SETTLED, horizon=None, repository=None):
    case = baseline_case_for()
    runtime = await case.reopen(owned, cold=True, recovery_boundary=boundary,
        recovery_max_event_id=None if horizon is None else lambda: horizon)
    if repository is not None:
        case.repositories["CaptureRepository"] = repository
        runtime = await case.view()
    return runtime


@pytest.mark.parametrize("failed", [False, True])
async def test_actual_return_is_saved_before_cancellation_propagates(database, tmp_path, failed):
    error = ErrorValue("directory_failed", "baseline", {"reason": "actual"}) if failed else None
    directory, repository = BlockingDirectory(error), ObserveRead(stop_after_return=False)
    owned, runtime, _ = await baseline_read_world(database, tmp_path, directory, repository=repository)
    task = asyncio.create_task(baseline_handler(runtime))
    try:
        await directory.started.wait()
        assert (await _stored(owned))[0] == 1
        task.cancel()
        await directory.cancelled.wait()
        assert not task.done()
        task.cancel()
        baseline_case_for().clock.wall = _SCHEDULED + 1
        directory.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(directory.calls) == 1 and len(repository.returns) == 1
        request, key = repository.returns[0]
        assert request.returned_at == _SCHEDULED + 1 and request.error == error
        assert (await _stored(owned))[::2] == (2, _SCHEDULED + 1)
        view = CaptureRepository().verify_baseline_read_write(request, key, owned)
        assert view.state.name == "RETURNED" and view.error == error
    finally:
        directory.release.set()
        await asyncio.gather(task, return_exceptions=True)
        owned.connection.close()


async def test_unknown_save_keeps_actual_return_and_does_not_read_again(database, tmp_path):
    class Unknown(ObserveRead):
        def save_baseline_read_return(self, request, key, owned):
            self.returns.append((request, key))
            receipt = CaptureRepository.save_baseline_read_return(self, request, key, owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            return (DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError("lost receipt")) if len(self.returns) == 1
                    else replace(receipt, handoff_error=BaselineReadBoundary()))
    directory, repository = BlockingDirectory(), Unknown()
    directory.release.set()
    owned, runtime, _ = await baseline_read_world(database, tmp_path, directory, repository=repository)
    try:
        with pytest.raises(BaselineReadBoundary):
            await baseline_handler(runtime)
        assert len(directory.calls) == 1
        baseline_case_for().clock.wall += 5_000_000
        assert len(repository.returns) == 2 and repository.returns[0] == repository.returns[1]
        request, key = repository.returns[0]
        assert CaptureRepository().verify_baseline_read_write(request, key, owned).state.name == "RETURNED"
    finally:
        owned.connection.close()


async def test_intent_saved_but_cancelled_before_call_has_no_fabricated_return(database, tmp_path):
    # 在真正的意图提交后、目录端口调用前，通过公开取消命令改变资格。
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    import threading
    gate = threading.Event()
    class Intent(ObserveRead):
        def save_baseline_read_intent(self, request, key, owned):
            saved = super().save_baseline_read_intent(request, key, owned)
            loop.call_soon_threadsafe(entered.set)
            assert gate.wait(10), "测试未交还原意图保存"
            return saved
    directory, repository = BlockingDirectory(), Intent()
    owned, runtime, _ = await baseline_read_world(database, tmp_path, directory, repository=repository)
    cancel_item = await request_baseline_cancel(owned, tmp_path, 1, _SCHEDULED, apply=False)
    task = asyncio.create_task(baseline_handler(runtime))
    try:
        await entered.wait()
        # 意图worker尚在原回调中，取消使用已受理的独立公开仓储连接。
        from camctl.cancellation.models import ApplyCancelTarget, CancelApplyMode
        from camctl.persistence.repositories.cancellation import CancellationRepository
        from camctl.contracts.values import new_operation_key
        canceled = CancellationRepository().apply_cancel_target(ApplyCancelTarget(
            cancel_item, CancelApplyMode.PRE_START, int(_SCHEDULED)), new_operation_key(), owned)
        assert canceled.kind is DbOutcomeKind.COMPLETED, canceled.error
        gate.set()
        try:
            await task
        finally:
            assert directory.calls == []
            state, event, returned_at, error = await _stored(owned)
            assert state == 3 and event is not None and returned_at is None and error is None
    finally:
        gate.set()
        await asyncio.gather(task, return_exceptions=True)
        owned.connection.close()


async def test_return_handoff_waits_for_actual_task_to_finish(database, tmp_path):
    directory, repository = BlockingDirectory(), ObserveRead()
    owned, runtime, _ = await baseline_read_world(database, tmp_path, directory, repository=repository)
    task = asyncio.create_task(baseline_handler(runtime))
    try:
        await directory.started.wait()
        intent, _ = repository.intents[0]
        call_id = owned.connection.execute("SELECT id FROM baseline_read_calls").fetchone()[0]
        assert CaptureRepository().read_baseline_call_result(call_id, owned, execution_finished=False) is None
        assert not task.done() and (await _stored(owned))[0] == 1
        directory.release.set()
        with pytest.raises(BaselineReadBoundary):
            await task
        request, key = repository.returns[0]
        assert CaptureRepository().verify_baseline_read_write(request, key, owned, execution_finished=False) is None
        assert CaptureRepository().verify_baseline_read_write(request, key, owned).state.name == "RETURNED"
    finally:
        directory.release.set()
        await asyncio.gather(task, return_exceptions=True)
        owned.connection.close()


async def test_directory_receives_the_production_stop_port(database, tmp_path):
    class Directory:
        async def read_directory(self, request, *, stop):
            waiter = asyncio.create_task(stop.requested())
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            return DirectoryRead(None, None, Page((), DirectoryCursor(request.binding, request.directories, 1, None)))
    owned, runtime, _ = await baseline_read_world(database, tmp_path, Directory(), repository=ObserveRead())
    try:
        with pytest.raises(BaselineReadBoundary):
            await baseline_handler(runtime)
        assert (await _stored(owned))[0] == 2
    finally:
        owned.connection.close()


async def test_clock_failure_keeps_actual_return_without_repeating_the_call(database, tmp_path):
    calls = []
    class Directory(BlockingDirectory):
        async def read_directory(self, request, *, stop):
            result = await super().read_directory(request, stop=stop)
            def fail_once():
                calls.append(1)
                baseline_case_for().clock.wall_input = None
                baseline_case_for().clock.wall = _SCHEDULED + 5
                raise OSError("clock unavailable")
            baseline_case_for().clock.wall_input = fail_once
            return result
    directory, repository = Directory(), ObserveRead()
    directory.release.set()
    owned, runtime, _ = await baseline_read_world(database, tmp_path, directory, repository=repository)
    try:
        with pytest.raises(OSError):
            await baseline_handler(runtime)
        await baseline_handler(runtime)
        assert calls == [1] and len(directory.calls) == 1 and repository.returns == []
        assert (await _stored(owned))[:2] == (1, None)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("boundary_name,horizon,qualified", [
    ("HOST_LOCAL_SETTLED", "equal", True), ("HOST_POWER_CYCLE", "equal", True),
    ("HOST_LOCAL_SETTLED", "later", True), ("HOST_LOCAL_SETTLED", "before", False),
    ("HOST_LOCAL_SETTLED", "none", False), ("UNCONFIRMED", "equal", False)])
async def test_cold_recovery_uses_original_fixed_host_boundary(database, tmp_path, boundary_name, horizon, qualified):
    directory, repository = BlockingDirectory(), ObserveRead()
    owned, runtime, _ = await baseline_read_world(database, tmp_path, directory, repository=repository)
    # 真实意图保存后在实际目录端口前中断；冷会话不能重做原调用。
    class Interrupt(ObserveRead):
        def save_baseline_read_intent(self, request, key, owned):
            saved = super().save_baseline_read_intent(request, key, owned)
            return replace(saved, handoff_error=BaselineReadBoundary())
    first = Interrupt()
    baseline_case_for().repositories["CaptureRepository"] = first
    runtime = await baseline_case_for().view()
    try:
        with pytest.raises(BaselineReadBoundary):
            await baseline_handler(runtime)
        event = owned.connection.execute("SELECT created_event_id FROM baseline_read_calls").fetchone()[0]
        maximum = {"equal": event, "later": event + 1, "before": event - 1, "none": None}[horizon]
        resumed = await _cold(owned, boundary=RecoveryBoundary[boundary_name], horizon=maximum, repository=repository)
        await baseline_handler(resumed)
        state, _, returned_at, _ = await _stored(owned)
        assert state == (3 if qualified else 1) and returned_at is None
        if qualified:
            assert len(repository.unknowns) == 1
            await baseline_handler(resumed)
            assert len(repository.unknowns) == 1
        assert directory.calls == []
    finally:
        owned.connection.close()


async def test_current_task_prevents_cold_recovery_until_actual_handoff(database, tmp_path):
    directory, repository = BlockingDirectory(), ObserveRead()
    maximum = [None]
    owned, runtime, _ = await baseline_read_world(database, tmp_path, directory, repository=repository,
        boundary=RecoveryBoundary.HOST_POWER_CYCLE, horizon=lambda: maximum[0])
    task = asyncio.create_task(baseline_handler(runtime))
    try:
        await directory.started.wait()
        maximum[0] = owned.connection.execute("SELECT created_event_id FROM baseline_read_calls").fetchone()[0]
        await baseline_handler(await baseline_case_for().view())
        assert not task.done() and (await _stored(owned))[0] == 1
        assert repository.unknowns == []
        directory.release.set()
        with pytest.raises(BaselineReadBoundary):
            await task
        assert (await _stored(owned))[0] == 2
    finally:
        directory.release.set()
        await asyncio.gather(task, return_exceptions=True)
        owned.connection.close()


@pytest.mark.parametrize("kind", ["empty", "nonempty", "error", "unknown"])
async def test_cold_recovery_prefers_committed_return_even_with_no_cold_qualification(database, tmp_path, kind):
    from camctl.capture.baseline_models import BaselineReadReturn, BaselineReadUnconfirmedSave
    from .test_baseline_history import _entries
    intent = await _intent(database, tmp_path)
    capture = CaptureRepository()
    first = await database.write(lambda owned: capture.save_baseline_read_intent(intent, intent.call_key, owned), key=intent.call_key)
    error = ErrorValue("transport_timeout", "directory unavailable") if kind == "error" else None
    returned = BaselineReadReturn(first.value.call_id, _SCHEDULED + 1, error, DirectoryRead(None, error,
        None if error else Page(_entries("a.OSV") if kind == "nonempty" else (), DirectoryCursor(intent.binding, intent.directories, 1, None))))
    request = BaselineReadUnconfirmedSave(first.value.call_id, _SCHEDULED + 2) if kind == "unknown" else returned
    method = capture.save_baseline_read_unconfirmed if kind == "unknown" else capture.save_baseline_read_return
    saved = await database.write(lambda owned: method(request, intent.result_save_key, owned), key=intent.result_save_key)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    from ..support.database import business_database
    async with business_database(tmp_path / "state.db") as fresh:
        actual = await fresh.read(lambda owned: capture.verify_baseline_read_write(request, intent.result_save_key, owned))
        assert actual == saved.value
        assert await fresh.read(lambda owned: capture.read_baseline_call_result(first.value.call_id, owned)) == saved.value


async def test_unconfirmed_save_retry_keeps_original_settlement_time(database, tmp_path):
    class Unknown(ObserveRead):
        def save_baseline_read_unconfirmed(self, request, key, owned):
            self.unknowns.append((request, key))
            result = CaptureRepository.save_baseline_read_unconfirmed(self, request, key, owned)
            assert result.kind is DbOutcomeKind.COMPLETED, result.error
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError("lost receipt")) if len(self.unknowns) == 1 else result
    owned, runtime, _ = await _cold_intent_world(database, tmp_path)
    repository = Unknown()
    try:
        event = owned.connection.execute("SELECT created_event_id FROM baseline_read_calls").fetchone()[0]
        resumed = await _cold(owned, horizon=event, repository=repository)
        baseline_case_for().clock.wall = _SCHEDULED + 5
        await baseline_handler(resumed)
        baseline_case_for().clock.wall = _SCHEDULED + 500
        await baseline_handler(resumed)
        assert repository.unknowns[0] == repository.unknowns[1]
        assert repository.unknowns[0][0].occurred_at == _SCHEDULED + 5
        assert (await _stored(owned))[0] == 3
    finally:
        owned.connection.close()


async def _cold_intent_world(database, tmp_path):
    class Interrupt(ObserveRead):
        def save_baseline_read_intent(self, request, key, owned):
            result = super().save_baseline_read_intent(request, key, owned)
            return replace(result, handoff_error=BaselineReadBoundary())
    world = await baseline_read_world(database, tmp_path, BlockingDirectory(), repository=Interrupt())
    with pytest.raises(BaselineReadBoundary):
        await baseline_handler(world[1])
    return world


async def test_cancelled_actual_directory_call_recovers_unknown_after_session_restart(database, tmp_path):
    class Directory(BlockingDirectory):
        async def read_directory(self, request, *, stop):
            self.calls.append(request)
            self.started.set()
            await asyncio.Event().wait()
    directory, repository = Directory(), ObserveRead()
    owned, runtime, _ = await baseline_read_world(database, tmp_path, directory, repository=repository)
    task = asyncio.create_task(baseline_handler(runtime))
    try:
        await directory.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await _stored(owned))[0] == 1
        assert len(directory.calls) == 1 and repository.unknowns == []
        event = owned.connection.execute("SELECT created_event_id FROM baseline_read_calls").fetchone()[0]
        resumed = await _cold(owned, boundary=RecoveryBoundary.HOST_LOCAL_SETTLED, horizon=event,
            repository=repository)
        await baseline_handler(resumed)
        assert (await _stored(owned))[0] == 3 and len(directory.calls) == 1
        assert len(repository.unknowns) == 1
        request, key = repository.unknowns[0]
        assert key == repository.intents[0][0].result_save_key
        assert CaptureRepository().verify_baseline_read_write(request, key, owned).state.name == "UNCONFIRMED"
    finally:
        owned.connection.close()


@pytest.mark.parametrize("lookup", ["intent", "result"])
async def test_failed_result_lookup_keeps_recovery_responsibility(database, tmp_path, lookup):
    class Failing(ObserveRead):
        def read_baseline_call(self, *args, **kwargs):
            if lookup == "intent":
                raise OSError("intent unavailable")
            return super().read_baseline_call(*args, **kwargs)
        def read_baseline_call_result(self, *args, **kwargs):
            raise OSError("read unavailable")
    owned, _, _ = await _cold_intent_world(database, tmp_path)
    try:
        event = owned.connection.execute("SELECT created_event_id FROM baseline_read_calls").fetchone()[0]
        resumed = await _cold(owned, horizon=event, repository=Failing())
        with pytest.raises(OSError):
            await baseline_handler(resumed)
        assert (await _stored(owned))[:2] == (1, None)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("failure", ["clock", "encoding"])
async def test_unknown_clock_or_encoding_failure_keeps_one_actual_attempt(database, tmp_path, failure):
    calls = []
    clock = baseline_case_for().clock
    class FaultClock(ObserveRead):
        def read_baseline_call_result(self, *args, **kwargs):
            result = super().read_baseline_call_result(*args, **kwargs)
            if result is None and not calls:
                def faulty():
                    calls.append(1)
                    clock.wall_input = None
                    if failure == "clock":
                        raise OSError("clock unavailable")
                    return True
                clock.wall_input = faulty
            return result
    owned, _, _ = await _cold_intent_world(database, tmp_path)
    try:
        event = owned.connection.execute("SELECT created_event_id FROM baseline_read_calls").fetchone()[0]
        resumed = await _cold(owned, horizon=event, repository=FaultClock())
        with pytest.raises((OSError, TypeError, ValueError)):
            await baseline_handler(resumed)
        await baseline_handler(resumed)
        assert calls == [1] and (await _stored(owned))[:2] == (1, None)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("changed", ["foreign_database", "request", "call_key", "result_key", "execution_running"])
async def test_public_write_verification_keeps_database_request_and_original_dual_keys(database, tmp_path, changed):
    """公开存储身份约束；此目标独立于已移除的Python上下文对象身份。"""
    from camctl.contracts.values import ConsistencyError, new_operation_key
    from ..support.database import create_test_database, business_database
    intent = await _intent(database, tmp_path)
    capture = CaptureRepository()
    first = await database.write(lambda owned: capture.save_baseline_read_intent(
        intent, intent.call_key, owned), key=intent.call_key)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    before = await database.read(lambda owned: tuple(owned.connection.iterdump()))
    if changed == "foreign_database":
        path = tmp_path / "other.db"
        create_test_database(path)
        async with business_database(path) as foreign:
            assert foreign.metadata.instance_id != database.metadata.instance_id
            assert await foreign.read(lambda owned: capture.verify_baseline_read_write(
                intent, intent.call_key, owned)) is None
            assert await foreign.read(lambda owned: owned.connection.execute(
                "SELECT COUNT(*) FROM history_transactions").fetchone()) == (0,)
    elif changed == "execution_running":
        assert await database.read(lambda owned: capture.verify_baseline_read_write(
            intent, intent.call_key, owned, execution_finished=False)) is None
        assert await database.read(lambda owned: capture.verify_baseline_read_write(
            intent, intent.call_key, owned)) == first.value
    else:
        request = (replace(intent, occurred_at=intent.occurred_at + 7) if changed == "request"
            else replace(intent, call_key=new_operation_key()) if changed == "call_key"
            else replace(intent, result_save_key=new_operation_key()))
        with pytest.raises(ConsistencyError):
            await database.read(lambda owned: capture.verify_baseline_read_write(
                request, intent.call_key, owned))
    assert await database.read(lambda owned: tuple(owned.connection.iterdump())) == before

"""恢复扫描与原拥有者共同取得完整保存结果。"""

import asyncio
from unittest.mock import create_autospec

import pytest

from camctl.capture.files import FileObservationSave, FilePresenceSave, ObservationDisposition, ObservationOutcome
from camctl.capture.handlers import (
    FileFactStage, PendingCaptureCompletion, PendingCaptureInputReads,
    PendingFileObservation, PendingResultCheckClose, _FileObservationSaves,
    _register_observed, resume_capture_completions, resume_file_observations,
    resume_result_check_closes,
)
from camctl.capture.models import ResultRunClose
from camctl.capture.result_inputs import ObservedFile
from camctl.capture.results import FileKind
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.operations.attempts import RetryWaitGate, RunOutcome, StaleRunFinish
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository, CaptureResult, FinishCanceledCapture, FinishCapture
from camctl.outputs.catalog import OutputCatalogFacts

pytestmark = pytest.mark.asyncio


async def _turns():
    for _ in range(12):
        await asyncio.sleep(0)


@pytest.mark.parametrize("phase", ["write", "apply", "input_reads"])
async def test_capture_owner_and_resume_share_complete_save(phase):
    database = create_autospec(BusinessDatabase, instance=True)
    pending = PendingCaptureCompletion(new_operation_key(), FinishCanceledCapture(21, 1000))
    held = {21: pending}
    entered, release = asyncio.Event(), asyncio.Event()
    read_finish = PendingCaptureInputReads(new_operation_key(), StaleRunFinish(
        ("read/1",), RunOutcome.CANCELED, 1000))

    async def write(callback, **kwargs):
        if phase == "write" or (phase == "input_reads" and kwargs["key"] == read_finish.key):
            entered.set()
            await release.wait()
        return DbOutcome(DbOutcomeKind.COMPLETED, CaptureResult(6, 2, ()))

    async def read(callback, **kwargs):
        if phase == "apply":
            entered.set()
            await release.wait()
        return read_finish if phase == "input_reads" else None

    database.write.side_effect = write
    database.read.side_effect = read
    async def resume():
        await resume_capture_completions(database, pending_capture_completions=held,
            capture=create_autospec(CaptureRepository, instance=True))

    owner = asyncio.create_task(resume())
    await entered.wait()
    recovery = asyncio.create_task(resume())
    await _turns()
    release.set()
    results = await asyncio.gather(owner, recovery, return_exceptions=True)

    assert results == [None, None]
    assert not held
    keys = [call.kwargs["key"] for call in database.write.call_args_list]
    assert keys == ([pending.key, read_finish.key] if phase == "input_reads" else [pending.key])
    assert database.read.await_count == 1


async def test_canceled_capture_waiter_keeps_shared_save_and_other_waiter_alive():
    database = create_autospec(BusinessDatabase, instance=True)
    database.read.return_value = None
    entered, release = asyncio.Event(), asyncio.Event()
    async def write(callback, **kwargs):
        entered.set()
        await release.wait()
        return DbOutcome(DbOutcomeKind.COMPLETED, CaptureResult(6, 2, ()))
    database.write.side_effect = write
    pending = PendingCaptureCompletion(new_operation_key(), FinishCanceledCapture(21, 1000))
    held = {21: pending}
    async def resume():
        await resume_capture_completions(database, pending_capture_completions=held,
            capture=create_autospec(CaptureRepository, instance=True))
    owner = asyncio.create_task(resume())
    await entered.wait()
    recovery = asyncio.create_task(resume())
    await _turns()
    owner.cancel()
    await _turns()
    owner.cancel()
    await _turns()
    assert not owner.done() and not recovery.done() and held[21] is pending
    release.set()
    results = await asyncio.gather(owner, recovery, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError) and results[1] is None
    assert not held
    database.write.assert_awaited_once()


async def test_new_canceled_completion_has_own_shared_save_while_original_waits():
    database = create_autospec(BusinessDatabase, instance=True)
    database.read.side_effect = ({"status": 2, "cancel_requested": 1}, None)
    original = PendingCaptureCompletion(new_operation_key(),
        FinishCapture(21, (), OutputCatalogFacts(21, True), 1000))
    held = {21: original}
    entered, release = asyncio.Event(), asyncio.Event()
    async def write(callback, **kwargs):
        if kwargs["key"] == original.key:
            return DbOutcome(DbOutcomeKind.ROLLED_BACK)
        entered.set()
        await release.wait()
        return DbOutcome(DbOutcomeKind.COMPLETED, CaptureResult(6, 2, ()))
    database.write.side_effect = write
    async def resume():
        await resume_capture_completions(database, pending_capture_completions=held,
            capture=create_autospec(CaptureRepository, instance=True))
    owner = asyncio.create_task(resume())
    await entered.wait()
    canceled = held[21]
    assert isinstance(canceled.request, FinishCanceledCapture)
    assert canceled.key != original.key and canceled.shared is not original.shared
    recovery = asyncio.create_task(resume())
    await _turns()
    release.set()
    assert await asyncio.gather(owner, recovery, return_exceptions=True) == [None, None]
    assert not held
    assert [call.kwargs["key"] for call in database.write.call_args_list] == [original.key, canceled.key]


async def test_result_close_waits_for_one_gate_application():
    database = create_autospec(BusinessDatabase, instance=True)
    database.write.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    entered, release = asyncio.Event(), asyncio.Event()
    async def read(callback, **kwargs):
        entered.set()
        await release.wait()
        return 7
    database.read.side_effect = read
    gate = create_autospec(RetryWaitGate, instance=True)
    pending = PendingResultCheckClose(new_operation_key(), ResultRunClose(21, 1000))
    held = {21: pending}
    async def resume():
        await resume_result_check_closes(database, pending_result_closes=held,
            capture=create_autospec(CaptureRepository, instance=True), retry_gate=gate)
    owner = asyncio.create_task(resume())
    await entered.wait()
    recovery = asyncio.create_task(resume())
    await _turns()
    release.set()
    assert await asyncio.gather(owner, recovery, return_exceptions=True) == [None, None]
    assert not held
    database.write.assert_awaited_once()
    gate.cleared.assert_called_once_with("results/7")


@pytest.mark.parametrize("kind", [DbOutcomeKind.UNKNOWN, DbOutcomeKind.ROLLED_BACK])
async def test_capture_failed_save_recovery_preserves_original_input_and_key(kind):
    database = create_autospec(BusinessDatabase, instance=True)
    database.read.return_value = None
    database.write.side_effect = (DbOutcome(kind, error=RuntimeError("等待核实")),
        DbOutcome(DbOutcomeKind.COMPLETED, CaptureResult(6, 2, ())))
    pending = PendingCaptureCompletion(new_operation_key(), FinishCanceledCapture(21, 1000))
    held = {21: pending}
    repository = create_autospec(CaptureRepository, instance=True)
    with pytest.raises(ConsistencyError):
        await resume_capture_completions(database, pending_capture_completions=held, capture=repository)
    assert held[21] is pending
    await resume_capture_completions(database, pending_capture_completions=held, capture=repository)
    assert not held
    assert [call.kwargs["key"] for call in database.write.call_args_list] == [pending.key, pending.key]
    for call in database.write.call_args_list:
        call.args[0](object())
    assert all(call.args[0] is pending.request for call in repository.finish_canceled_capture.call_args_list)


async def test_capture_input_read_failure_resumes_original_stage_without_rewriting_capture():
    database = create_autospec(BusinessDatabase, instance=True)
    read_finish = PendingCaptureInputReads(new_operation_key(), StaleRunFinish(
        ("read/1",), RunOutcome.CANCELED, 1000))
    database.read.return_value = read_finish
    database.write.side_effect = (DbOutcome(DbOutcomeKind.COMPLETED, CaptureResult(6, 2, ())),
        DbOutcome(DbOutcomeKind.UNKNOWN, error=RuntimeError("读取收场待核实")),
        DbOutcome(DbOutcomeKind.COMPLETED))
    original = PendingCaptureCompletion(new_operation_key(), FinishCanceledCapture(21, 1000))
    held = {21: original}
    repository = create_autospec(CaptureRepository, instance=True)
    with pytest.raises(ConsistencyError):
        await resume_capture_completions(database, pending_capture_completions=held, capture=repository)
    assert held[21].input_reads is read_finish
    await resume_capture_completions(database, pending_capture_completions=held, capture=repository)
    assert not held
    assert [call.kwargs["key"] for call in database.write.call_args_list] == [
        original.key, read_finish.key, read_finish.key]
    database.read.assert_awaited_once()


@pytest.mark.parametrize("kind", ["capture", "result"])
async def test_completed_pending_from_earlier_scan_snapshot_does_not_replay(kind):
    database = create_autospec(BusinessDatabase, instance=True)
    database.read.return_value = None
    first = (PendingCaptureCompletion(new_operation_key(), FinishCanceledCapture(21, 1000))
        if kind == "capture" else PendingResultCheckClose(new_operation_key(), ResultRunClose(21, 1000)))
    second = (PendingCaptureCompletion(new_operation_key(), FinishCanceledCapture(22, 1000))
        if kind == "capture" else PendingResultCheckClose(new_operation_key(), ResultRunClose(22, 1000)))
    held = {21: first, 22: second}
    entered, release = asyncio.Event(), asyncio.Event()
    async def write(callback, **kwargs):
        if kwargs["key"] == first.key:
            entered.set()
            await release.wait()
        return DbOutcome(DbOutcomeKind.COMPLETED, CaptureResult(6, 2, ()))
    database.write.side_effect = write
    async def resume(action_id=None):
        repository = create_autospec(CaptureRepository, instance=True)
        if kind == "capture":
            await resume_capture_completions(database, pending_capture_completions=held,
                action_id=action_id, capture=repository)
        else:
            await resume_result_check_closes(database, pending_result_closes=held,
                action_id=action_id, capture=repository)
    earlier_scan = asyncio.create_task(resume())
    await entered.wait()
    await resume(22)
    assert 22 not in held and not earlier_scan.done()
    release.set()
    await earlier_scan
    assert not held
    assert [call.kwargs["key"] for call in database.write.call_args_list] == [first.key, second.key]


@pytest.mark.parametrize("fact", [False, True])
async def test_file_save_shares_original_observation_or_fact(fact):
    database = create_autospec(BusinessDatabase, instance=True)
    entered, release = asyncio.Event(), asyncio.Event()
    response = ObservationOutcome(ObservationDisposition.SAVED, 3)
    async def write(callback, **kwargs):
        entered.set()
        await release.wait()
        return DbOutcome(DbOutcomeKind.COMPLETED, response)
    database.write.side_effect = write
    observation = PendingFileObservation(FileObservationSave(21, "a", {"path": "a.jpg"}, 1000),
        new_operation_key(), (), {})
    held = {(21, "a"): observation}
    runtime = _FileObservationSaves(database, held, {})
    async def save():
        if fact:
            await runtime._save_file_fact((21, "a"), FileFactStage.PRESENCE, FilePresenceSave(3, 2, 1000))
        else:
            await runtime._save_file_observations(21)
    owner = asyncio.create_task(save())
    await entered.wait()
    recovery = asyncio.create_task(save())
    await _turns()
    release.set()
    assert await asyncio.gather(owner, recovery, return_exceptions=True) == [None, None]
    database.write.assert_awaited_once()
    saved = held[(21, "a")].facts[FileFactStage.PRESENCE] if fact else held[(21, "a")]
    assert saved.response is response


@pytest.mark.parametrize("cancel_owner", [False, True])
async def test_file_registration_resume_shares_entire_batch_and_child_facts(cancel_owner):
    database = create_autospec(BusinessDatabase, instance=True)
    entered, release = asyncio.Event(), asyncio.Event()
    writes = []
    async def write(callback, **kwargs):
        writes.append(kwargs["description"])
        if kwargs["description"] == "保存原文件发现":
            entered.set()
            await release.wait()
        return DbOutcome(DbOutcomeKind.COMPLETED, ObservationOutcome(ObservationDisposition.SAVED, 3))
    database.write.side_effect = write
    database.read.return_value = {"presence_state": 2, "completion_state": 1, "size_bytes": 41}
    runtime = _FileObservationSaves(database, {}, {})
    entry = ObservedFile("a", {"path": "a.jpg"}, {"complete": True}, True, 41, FileKind.PHOTO)
    owner = asyncio.create_task(_register_observed(runtime, 21, (entry,), occurred_at=1000))
    await entered.wait()
    recovery = asyncio.create_task(resume_file_observations(database,
        pending_file_observations=runtime.pending_file_observations, pending_call_results={}))
    await _turns()
    if cancel_owner:
        owner.cancel()
        await _turns()
        assert not owner.done() and runtime.pending_file_observations
    release.set()
    results = await asyncio.gather(owner, recovery, return_exceptions=True)
    if cancel_owner:
        assert isinstance(results[0], asyncio.CancelledError) and results[1] is None
    else:
        assert not any(isinstance(result, BaseException) for result in results)
    assert not runtime.pending_file_observations
    assert writes == ["保存原文件发现", "保存原文件事实", "保存原文件事实"]


async def test_file_registration_recovery_preserves_each_original_discovery_time():
    database = create_autospec(BusinessDatabase, instance=True)
    database.read.return_value = {"presence_state": 2, "completion_state": 1}
    entries = tuple(ObservedFile(identity, {"path": identity}, {"listed": identity}, False, None)
        for identity in ("a", "b"))
    runtime = _FileObservationSaves(database, {}, {})
    times = iter((1000, 1001))
    runtime.wall_us = lambda: next(times)
    commands = []
    async def write(callback, **kwargs):
        return DbOutcome(DbOutcomeKind.UNKNOWN, error=RuntimeError("发现待核实"))
    database.write.side_effect = write
    with pytest.raises(ConsistencyError):
        await _register_observed(runtime, 21, entries)
    original = dict(runtime.pending_file_observations)
    assert [pending.command.occurred_at for pending in original.values()] == [1000, 1001]
    repository = create_autospec(CaptureRepository, instance=True)
    def observe(command, key, owned):
        commands.append(command)
        file_id = 3 if command.file_identity == "a" else 4
        return DbOutcome(DbOutcomeKind.COMPLETED, ObservationOutcome(ObservationDisposition.SAVED, file_id))
    repository.save_file_observation.side_effect = observe
    repository.save_file_ownership.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        ObservationOutcome(ObservationDisposition.SAVED, 3))
    runtime.capture = repository
    async def save(callback, **kwargs):
        return callback(object())
    database.write.side_effect = save

    await runtime.resume_file_observations(21)

    assert not runtime.pending_file_observations
    assert commands == [pending.command for pending in original.values()]
    assert [call.args[0].occurred_at for call in repository.save_file_ownership.call_args_list] == [1000, 1001]

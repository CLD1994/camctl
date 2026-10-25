"""完成保存先交付实际回执，再传播取消或提交后通知错误。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.capture.handlers import PendingCaptureCompletion, resume_capture_completions
from camctl.capture.preparation import PendingPreparation, RuntimePreparationCache, resume_preparation_saves
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.operations.models import AttemptTicket
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbOutcome, DbOutcomeKind, _attach_db_outcome
from camctl.persistence.repositories.capture import CaptureRepository, CaptureResult, FinishCanceledCapture
from camctl.persistence.repositories.operations import OperationRepository

pytestmark = pytest.mark.asyncio


def _pending():
    pending = PendingCaptureCompletion(new_operation_key(), FinishCanceledCapture(21, 1000))
    return pending, {21: pending}


async def test_completed_capture_notification_error_applies_pending_before_propagation():
    pending, held = _pending()
    database = create_autospec(BusinessDatabase, instance=True)
    database.read.return_value = None
    failure = RuntimeError("提交后的通知错误")
    _attach_db_outcome(failure, DbOutcome(DbOutcomeKind.COMPLETED, CaptureResult(6, 2, ())))
    database.write.side_effect = failure

    with pytest.raises(RuntimeError) as raised:
        await resume_capture_completions(database, pending_capture_completions=held,
            capture=create_autospec(CaptureRepository, instance=True))

    assert raised.value is failure and not held
    assert database.write.call_args.kwargs["key"] == pending.key


async def test_canceled_capture_waits_for_actual_save_and_pending_application():
    pending, held = _pending()
    database = create_autospec(BusinessDatabase, instance=True)
    database.read.return_value = None
    entered, release = asyncio.Event(), asyncio.Event()

    async def save(callback, **kwargs):
        entered.set()
        await release.wait()
        return DbOutcome(DbOutcomeKind.COMPLETED, CaptureResult(6, 2, ()))

    database.write.side_effect = save
    task = asyncio.create_task(resume_capture_completions(database,
        pending_capture_completions=held, capture=create_autospec(CaptureRepository, instance=True)))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and held[21] is pending
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not held


async def test_real_unknown_save_error_has_priority_over_original_cancel():
    _, held = _pending()
    database = create_autospec(BusinessDatabase, instance=True)
    entered, release = asyncio.Event(), asyncio.Event()

    async def save(callback, **kwargs):
        entered.set()
        await release.wait()
        return DbOutcome(DbOutcomeKind.UNKNOWN, error=RuntimeError("结果未知"))

    database.write.side_effect = save
    task = asyncio.create_task(resume_capture_completions(database,
        pending_capture_completions=held, capture=create_autospec(CaptureRepository, instance=True)))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(ConsistencyError) as raised:
        await task
    assert isinstance(raised.value.__cause__, asyncio.CancelledError)
    assert 21 in held


async def test_completed_preparation_notification_error_marks_original_finish_saved():
    database = create_autospec(BusinessDatabase, instance=True)
    ticket = AttemptTicket(1, "control", "7", "prepare/7/" + "a" * 32, 11)
    owner = PendingPreparation(21, 7, "a" * 32, None, None, None,
        ticket=ticket, finish=object())
    cache = RuntimePreparationCache(owners={21: owner})
    failure = RuntimeError("结果已经提交，通知失败")
    _attach_db_outcome(failure, DbOutcome(DbOutcomeKind.COMPLETED, object()))
    database.write.side_effect = failure

    with pytest.raises(RuntimeError) as raised:
        await resume_preparation_saves(database, cache,
            operations=create_autospec(OperationRepository, instance=True))

    assert raised.value is failure and owner.saved

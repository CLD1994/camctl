"""日志回执保存一次接纳及写入事实。"""

import asyncio
import inspect

import pytest

from camctl.logging_runtime.admission import AdmissionDecision, AdmissionKind
from camctl.logging_runtime.models import LogLevel
from camctl.logging_runtime.receipt import LogReceipt
from camctl.logging_runtime.service import AdmissionResult


def result(*, delivered=None):
    return AdmissionResult(AdmissionDecision(AdmissionKind.ACCEPTED, LogLevel.ERROR),
                           1, delivered=delivered)


def test_receipt_is_not_a_coroutine_and_keeps_first_facts():
    receipt = LogReceipt()
    assert not inspect.iscoroutine(receipt)
    admission, written = result(), result(delivered=True)
    receipt.complete_admission(admission)
    receipt.complete_admission(result())
    receipt.complete_write(written)
    receipt.complete_write(result(delivered=False))
    assert receipt.admission_result is admission
    assert receipt.write_result is written


@pytest.mark.asyncio
async def test_canceling_one_waiter_preserves_shared_write_fact():
    receipt = LogReceipt()

    async def wait():
        return await receipt

    canceled, remaining = asyncio.create_task(wait()), asyncio.create_task(wait())
    await asyncio.sleep(0)
    canceled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await canceled
    admission, written = result(), result(delivered=True)
    receipt.complete_admission(admission)
    assert await receipt.wait_admission() is admission
    assert not remaining.done()
    receipt.complete_write(written)
    assert await remaining is written
    assert await receipt is written

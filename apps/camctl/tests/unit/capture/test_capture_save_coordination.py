"""正常保存与恢复共同推进原申请，提交和结果消费均只发生一次。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.capture import media_flow, result_scans, segment_completion, segment_inspections
from camctl.capture.media import SaveDisposition
from camctl.capture.media_flow import CaptureProcessingSaves
from camctl.capture.result_pages import ResultPageRef
from camctl.capture.result_scans import PendingResultScan
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.segment_inspections import InspectionResultSave
from .test_media_save_identity import _request as media_request
from .test_result_page_saves import _request as page_request

pytestmark = pytest.mark.asyncio


def _database():
    return create_autospec(BusinessDatabase, instance=True, spec_set=True)


async def _join(task):
    # 让新等待者进入其必需阶段；不依赖实际线程或经过的时长。
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    return task


async def test_segment_recovery_joins_original_submission_and_consumption():
    database = _database()
    state = SimpleNamespace(database=database, pending_segment_writes={})
    repository = create_autospec(CaptureRepository, instance=True, spec_set=True)
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(callback, **kwargs):
        entered.set()
        await release.wait()
        return DbOutcome(DbOutcomeKind.COMPLETED, "saved")

    database.write.side_effect = write
    original = asyncio.create_task(segment_completion._save(
        state, 7, repository, "finish_segment_recording", object()))
    await entered.wait()
    recovery = await _join(asyncio.create_task(segment_completion.resume_segment_writes(
        database, state.pending_segment_writes, 7)))
    release.set()
    results = await asyncio.gather(original, recovery, return_exceptions=True)

    assert results == ["saved", None]
    assert database.write.await_count == 1
    assert not state.pending_segment_writes


async def test_result_page_application_is_shared_through_qualification_and_cursor_update():
    from decimal import Decimal

    request = page_request()
    pending = PendingResultScan(request.ticket, None, Decimal(10))
    pending.saves.begin(request)
    database = _database()
    # 使用原页解码后的游标对象，回执必须与原输入完整相符。
    from camctl.capture.result_inputs import page_from_outcome
    response = ResultPageRef(request.ticket, 1, 10,
        page_from_outcome(request.ticket, request.outcome.outcome).next_cursor)
    database.write.return_value = DbOutcome(DbOutcomeKind.COMPLETED, response)
    database.read.return_value = None
    runtime = SimpleNamespace(database=database,
        capture=create_autospec(CaptureRepository, instance=True, spec_set=True))
    entered, release = asyncio.Event(), asyncio.Event()
    qualified_pages = []

    async def qualify(saved):
        qualified_pages.append(saved)
        entered.set()
        await release.wait()
        return True

    original = asyncio.create_task(result_scans._accept_saved_page(pending, runtime, qualify))
    await entered.wait()
    recovery = await _join(asyncio.create_task(result_scans._accept_saved_page(pending, runtime, qualify)))
    release.set()
    results = await asyncio.gather(original, recovery, return_exceptions=True)

    assert results == [False, False]
    assert len(qualified_pages) == 1
    assert pending.page_no == 2 and pending.last_ref is response
    assert pending.saves.pending is None
    assert database.write.await_count == 1


async def test_result_page_recovery_notification_error_still_applies_waiting_consumer():
    from decimal import Decimal
    from camctl.capture.result_inputs import page_from_outcome
    from camctl.persistence.models import _attach_db_outcome

    request = page_request()
    pending = PendingResultScan(request.ticket, None, Decimal(10))
    pending.saves.begin(request)
    response = ResultPageRef(request.ticket, 1, 10,
        page_from_outcome(request.ticket, request.outcome.outcome).next_cursor)
    failure = RuntimeError("已提交后的通知失败")
    _attach_db_outcome(failure, DbOutcome(DbOutcomeKind.COMPLETED, response))
    database = _database()
    database.read.return_value = None
    runtime = SimpleNamespace(database=database,
        capture=create_autospec(CaptureRepository, instance=True, spec_set=True))
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(callback, **kwargs):
        entered.set()
        await release.wait()
        raise failure

    database.write.side_effect = write
    recovery = asyncio.create_task(result_scans.resume_result_page_saves(database,
        pending_scans={7: pending}, repository=runtime.capture))
    await entered.wait()
    normal = await _join(asyncio.create_task(result_scans._accept_saved_page(pending, runtime, None)))
    release.set()
    results = await asyncio.gather(recovery, normal, return_exceptions=True)

    assert results == [failure, failure]
    assert pending.page_no == 2 and pending.last_ref is response
    assert pending.saves.pending is None
    assert database.write.await_count == 1


async def test_media_recovery_from_another_adapter_shares_original_phase(monkeypatch):
    database = _database()
    database.read.return_value = (1,)
    repository = create_autospec(CaptureRepository, instance=True, spec_set=True)
    monkeypatch.setattr(media_flow, "CaptureRepository", lambda: repository)
    pending = {}
    normal = CaptureProcessingSaves(database, pending)
    recovery = CaptureProcessingSaves(database, pending)
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(callback, **kwargs):
        entered.set()
        await release.wait()
        return DbOutcome(DbOutcomeKind.COMPLETED)

    database.write.side_effect = write
    command = media_request(1)
    first = asyncio.create_task(normal.save_check_result(command))
    await entered.wait()
    second = await _join(asyncio.create_task(recovery.save_check_result(command)))
    release.set()
    receipts = await asyncio.gather(first, second, return_exceptions=True)

    assert all(getattr(receipt, "disposition", None) is SaveDisposition.SAVED for receipt in receipts)
    assert database.write.await_count == 1
    assert not pending


@pytest.mark.parametrize("delivery", ["receipt", "cancellation", "notification", "cancelled_notification"])
async def test_media_recovery_output_receipt_updates_joining_adapter_file_scope(monkeypatch, delivery):
    from camctl.capture.processing import (
        ProcessingDisposition, ProcessingError, RepairOutcome, RepairOutputFile, RepairResultSave, RepairStart,
    )

    database = _database()
    database.read.return_value = (1,)
    repository = create_autospec(CaptureRepository, instance=True, spec_set=True)
    monkeypatch.setattr(media_flow, "CaptureRepository", lambda: repository)
    pending = {}
    normal = CaptureProcessingSaves(database, pending, file_ids=(11,))
    recovery = CaptureProcessingSaves(database, pending)
    command = RepairStart(1, "mp4", 100)
    database.write.return_value = DbOutcome(DbOutcomeKind.UNKNOWN)
    assert (await normal.start_repair_output(command)).disposition is SaveDisposition.UNKNOWN
    entered, release = asyncio.Event(), asyncio.Event()
    output = RepairOutputFile(ProcessingDisposition.SAVED, 22, "repaired.mp4")
    failure = RuntimeError("修复输出已提交后的通知失败")
    from camctl.persistence.models import _attach_db_outcome
    _attach_db_outcome(failure, DbOutcome(DbOutcomeKind.COMPLETED, output))

    async def write(callback, **kwargs):
        entered.set()
        await release.wait()
        if "notification" in delivery:
            raise failure
        return DbOutcome(DbOutcomeKind.COMPLETED, output)

    database.write.side_effect = write
    first = asyncio.create_task(recovery.start_repair_output(command))
    await entered.wait()
    second = await _join(asyncio.create_task(normal.start_repair_output(command)))
    if delivery.startswith("cancel"):
        for _ in range(2):
            second.cancel("原媒体调用取消")
            await asyncio.sleep(0)
            assert not second.done()
    release.set()
    receipts = await asyncio.gather(first, second, return_exceptions=True)

    if "notification" in delivery:
        assert receipts == [failure, failure]
    elif delivery == "cancellation":
        assert receipts[0].value is output
        assert isinstance(receipts[1], asyncio.CancelledError)
    else:
        assert all(receipt.value is output for receipt in receipts)
    assert normal.file_ids == (11, 22)
    assert not pending and database.write.await_count == 2
    database.write.side_effect = None
    await normal.save_repair_result(RepairResultSave(1, RepairOutcome.FAILED, 100,
        error=ProcessingError("repair_failed", "repair", {})))
    assert pending[(media_flow.MediaSaveStage.REPAIR, 1)].file_ids == (11, 22)


async def test_inspection_recovery_joins_result_save_and_waiter_cancel_keeps_receipt():
    database = _database()
    flow = SimpleNamespace(database=database, pending_segment_inspection_results={})
    request = InspectionResultSave(3, 4, {"reason": "unknown"}, 5)
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(callback, **kwargs):
        entered.set()
        await release.wait()
        return DbOutcome(DbOutcomeKind.COMPLETED)

    database.write.side_effect = write
    first = asyncio.create_task(segment_inspections._save_result(flow, request))
    await entered.wait()
    second = await _join(asyncio.create_task(segment_inspections._save_result(flow, request)))
    first.cancel("inspection waiter")
    await asyncio.sleep(0)
    assert not first.done()
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert isinstance(results[0], asyncio.CancelledError) and results[1] is True
    assert database.write.await_count == 1
    assert not flow.pending_segment_inspection_results

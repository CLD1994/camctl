"""正式分段推进保留原失败、源收场和实际范围，不改变失败归属。"""

import asyncio
from pathlib import Path
import threading
from unittest.mock import create_autospec

import pytest

from camctl.devices.read_session import ReadEnd, ReadSession
from camctl.host_files.models import BoundDirectories, FilePurpose
from camctl.host_files.segments import SegmentFailure, SegmentResult
from camctl.host_files.tasks import FileTaskExecutor
from camctl.operations.error_diagnostics import exception_diagnostic
from camctl.outputs import copy
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository


@pytest.fixture
def world(monkeypatch):
    database = create_autospec(BusinessDatabase, instance=True, spec_set=True)
    repository = create_autospec(OutputsRepository, instance=True, spec_set=True)
    target = copy.CopyTargetRef(3, next(iter(FilePurpose)), 'copy.bin')
    facts = copy.CopyStateFacts(2, 10, 0, 1, False, target, ())
    database.read.return_value = facts
    database.write.return_value = DbOutcome(DbOutcomeKind.COMPLETED,
        copy.SegmentSaveOutcome(copy.SegmentSaveDisposition.SAVED, 10))
    executor = create_autospec(FileTaskExecutor, instance=True, spec_set=True)
    async def owned(task):
        return await task.body(None)
    executor.run_owned_async_file_task.side_effect = owned
    stop = create_autospec(threading.Event, instance=True, spec_set=True)
    stop.is_set.return_value = False
    context = copy.SegmentContext(repository, database, BoundDirectories(Path('/staging')),
        123, 10, create_autospec(ReadSession, instance=True, spec_set=True), stop=stop, executor=executor)
    async def transfer(function, *args):
        assert function is copy._transfer_segment
        return transfer.result
    monkeypatch.setattr(asyncio, 'to_thread', transfer)
    return context, transfer


def _failure():
    try:
        raise OSError(5, 'original source failure', 'device-file')
    except OSError as error:
        return error


@pytest.mark.asyncio
@pytest.mark.parametrize('classification,stage,source_failed', [
    (SegmentFailure.READ_FAILED, 'segment_transfer_failed', True),
    (SegmentFailure.NO_DATA, 'segment_transfer_failed', True),
    (SegmentFailure.SOURCE_EOF_EARLY, 'segment_transfer_failed', True),
    (SegmentFailure.WRITE_FAILED, 'segment_transfer_failed', False),
    (SegmentFailure.SYNC_FAILED, 'segment_sync_failed', False),
    (SegmentFailure.STOPPED, 'segment_transfer_failed', False),
    (None, 'segment_sync_failed', False),
])
@pytest.mark.parametrize('source_stopped', [False, True])
async def test_failed_segment_preserves_original_result_and_chain(world, classification, stage, source_failed, source_stopped):
    context, transfer = world
    original = _failure() if classification in (
        SegmentFailure.READ_FAILED, SegmentFailure.WRITE_FAILED, SegmentFailure.SYNC_FAILED) else None
    close_error = OSError(16, 'original close failure')
    source_end = ReadEnd(True, 7, 'read_failed') if source_stopped else None
    result = SegmentResult(7, False, classification, original, False, source_end, close_error)
    transfer.result = result
    with pytest.raises(copy.CopySegmentError) as caught:
        await copy.copy_next_segment(2, context)
    error = caught.value
    assert error.stage == stage and error.source_failed is source_failed
    assert error.result is result and error.__cause__ is original
    diagnostic = exception_diagnostic(error)['diagnostic_details']['segment_result']
    assert diagnostic['error'] == classification
    assert diagnostic['processed_end'] == 7 and diagnostic['synced'] is False
    assert diagnostic['write_extent_known'] is False
    assert diagnostic['failure'] == exception_diagnostic(original)
    assert diagnostic['source_close_error']['message'] == str(close_error)
    assert diagnostic['source_end'] == (None if source_end is None else
        {'stopped': True, 'bytes_read': 7, 'error': 'read_failed'})
    context.database.write.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome_kind,stage', [
    (DbOutcomeKind.UNKNOWN, 'segment_save_unknown'),
    (DbOutcomeKind.ROLLED_BACK, 'segment_save_failed'),
    (DbOutcomeKind.NOT_EXECUTED, 'segment_save_failed'),
])
async def test_save_failure_preserves_successful_transfer_and_original_database_error(world, outcome_kind, stage):
    context, transfer = world
    transfer.result = SegmentResult(10, True, None, None, True, ReadEnd(True, 10, None), None)
    original = OSError('actual database save failure')
    context.database.write.return_value = DbOutcome(outcome_kind, error=original)
    with pytest.raises(copy.CopySegmentError) as caught:
        await copy.copy_next_segment(2, context)
    assert caught.value.stage == stage and not caught.value.source_failed
    assert caught.value.result is transfer.result
    assert caught.value.__cause__ is original


@pytest.mark.asyncio
async def test_success_preserves_transfer_and_only_then_saves_progress(world):
    context, transfer = world
    transfer.result = SegmentResult(10, True, None, None, True, ReadEnd(True, 10, None), None)
    step = await copy.copy_next_segment(2, context)
    assert step.transfer is transfer.result and step.saved.committed_bytes == 10
    context.database.write.assert_awaited_once()


def test_direct_segment_error_construction_keeps_optional_result():
    error = copy.CopySegmentError('stage', 'original detail')
    assert isinstance(error, RuntimeError) and error.result is None
    assert exception_diagnostic(error)['diagnostic_details']['segment_result'] is None

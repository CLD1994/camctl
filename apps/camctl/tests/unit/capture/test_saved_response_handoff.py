"""原基准、主机文件和核实收尾保存先交付已提交结果。"""

from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.capture.baseline_models import BaselineReadCallView, BaselineReadState
from camctl.capture.baseline_reads import PendingBaselineRead, resume_baseline_read_save
from camctl.capture.handlers import PendingResultCheckClose, resume_result_check_closes
from camctl.capture.host_photo import _prepare_files, _save_complete
from camctl.capture.models import ResultRunClose
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.values import new_operation_key
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbOutcome, DbOutcomeKind, _attach_db_outcome
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.host_capture import HostPhotoComplete, HostCaptureRepository
from .test_baseline_read_models import _intent

pytestmark = pytest.mark.asyncio


def _failed_notification(value):
    database = create_autospec(BusinessDatabase, instance=True)
    failure = RuntimeError("已提交后的通知失败")
    _attach_db_outcome(failure, DbOutcome(DbOutcomeKind.COMPLETED, value))
    database.write.side_effect = failure
    return database, failure


async def test_baseline_call_applies_already_validated_intent_before_notification_error():
    intent = _intent()
    response = BaselineReadCallView(7, 11, BaselineReadState.INTENDED, intent,
        None, None, None, None, None, HistoryBoundary(1, 11))
    database, failure = _failed_notification(response)
    owner = PendingBaselineRead(intent)
    owner.saves.begin(intent)

    with pytest.raises(RuntimeError) as raised:
        await resume_baseline_read_save(owner, capture=create_autospec(CaptureRepository, instance=True),
            database=database)

    assert raised.value is failure
    assert owner.intent_view is response and owner.saves.pending is None


@pytest.mark.parametrize("kind", ["prepare", "complete"])
async def test_host_file_save_releases_reliable_pending_before_notification_error(kind):
    database, failure = _failed_notification(object())
    runtime = SimpleNamespace(database=database,
        host_capture=create_autospec(HostCaptureRepository, instance=True), roots=object(),
        wall_us=lambda: 1000, pending_host_photo_prepares={}, pending_host_photo_completions={})

    with pytest.raises(RuntimeError) as raised:
        if kind == "prepare":
            await _prepare_files(runtime, 21)
        else:
            await _save_complete(runtime, 21, HostPhotoComplete(21, 29, 100, "a" * 64,
                30, 20, "b" * 64, 1000))

    assert raised.value is failure
    assert not runtime.pending_host_photo_prepares and not runtime.pending_host_photo_completions


async def test_result_close_releases_reliable_pending_before_notification_error():
    database, failure = _failed_notification(None)
    pending = {21: PendingResultCheckClose(new_operation_key(), ResultRunClose(21, 1000))}

    with pytest.raises(RuntimeError) as raised:
        await resume_result_check_closes(database, pending_result_closes=pending,
            capture=create_autospec(CaptureRepository, instance=True))

    assert raised.value is failure and not pending

"""文件业务自定义阶段异常保留数据库原异常及控制对象。"""

import asyncio

import pytest

from camctl.outputs import copy, handoff, work_files
from camctl.persistence.models import DbOutcome, DbOutcomeKind, db_outcome_for


@pytest.mark.parametrize("boundary", [copy, handoff, work_files])
@pytest.mark.parametrize("controlled", [False, True])
def test_file_stage_wrapper_keeps_original_database_error(boundary, controlled):
    original = asyncio.CancelledError("original control") if controlled else OSError(5, "original DB error")
    outcome = DbOutcome(DbOutcomeKind.UNKNOWN, error=original)
    expected = (copy.CopyCompletionError if boundary is copy else
                handoff.DeliveryHandoffError if boundary is handoff else work_files.WorkFileCleanupError)
    with pytest.raises(asyncio.CancelledError if controlled else expected) as caught:
        if boundary is work_files:
            boundary._require_completed(outcome, "save_failed")
        else:
            boundary._require_completed(outcome, "save_failed", "save_unknown")
    if controlled:
        assert caught.value is original
    else:
        assert caught.value.__cause__ is original
        assert caught.value.stage == ("save_failed" if boundary is work_files else "save_unknown")
    assert db_outcome_for(caught.value) is outcome

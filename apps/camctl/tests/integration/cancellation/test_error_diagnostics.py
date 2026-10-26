"""取消实际拒绝与收场失败由原保存请求保留诊断身份。"""

from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.cancellation import service
from camctl.cancellation.models import RecordCancelResult
from camctl.logging_runtime.diagnostics import error_diagnostics_context, flush_diagnostics
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.cancellation import CancellationRepository


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["task_cancel_unsupported", "target_cleanup_failed"])
async def test_cancel_failure_saved_twice_reuses_original_diagnostic(code):
    database = create_autospec(BusinessDatabase, instance=True)
    database.metadata = SimpleNamespace(instance_id="a" * 32)
    database.write.return_value = DbOutcome(DbOutcomeKind.COMPLETED)
    runtime = service.CancellationRuntime(database, create_autospec(CancellationRepository, instance=True),
        settlement=None, occurred_at=lambda: 1)
    request = RecordCancelResult(7, 1, code=code, details={"action_instance_id": "9"})
    diagnostics = []
    with error_diagnostics_context(None, diagnostics.append):
        await service._save_target(runtime, 9, request, action_type=1, origin_action_id=5)
        await flush_diagnostics()
        await service._save_target(runtime, 9, request, action_type=1, origin_action_id=5)
        await flush_diagnostics()

    assert len(diagnostics) == 1
    for fact in (code, '"item_id":7', '"target_action_id":9', '"origin_action_id":5', "a" * 32):
        assert fact in diagnostics[0]

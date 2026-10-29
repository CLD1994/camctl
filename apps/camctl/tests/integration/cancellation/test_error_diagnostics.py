"""公开取消消费者重复推进时，原拒绝与收场失败只交付一次诊断。"""

import pytest

from camctl.cancellation.ports import SettlementOutcome
from camctl.cancellation.service import ApplyCancel, CancellationRuntime, apply_cancel
from camctl.logging_runtime.diagnostics import error_diagnostics_context, flush_diagnostics
from camctl.persistence.repositories.cancellation import CancellationRepository

from .test_effects import SettlementDouble, pipeline


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["task_cancel_unsupported", "target_cleanup_failed"])
async def test_cancel_failure_saved_twice_reuses_original_diagnostic(
        pipeline, cancellation_database, code):
    owned = pipeline
    connection = owned.connection
    if code == "task_cancel_unsupported":
        connection.execute("UPDATE device_activities SET stop_supported=0,safe_repeat_stop=0 WHERE action_id=11")
    else:
        connection.execute("UPDATE actions SET status=6,cancel_requested=1 WHERE id=11")
        connection.execute("UPDATE cancel_items SET status=2,cancellation_effect=2 WHERE id=91")
    connection.commit()
    runtime = CancellationRuntime(
        database=cancellation_database, repository=CancellationRepository(),
        settlement=SettlementDouble({11: SettlementOutcome(complete=True, failed=True)}),
        occurred_at=lambda: 1,
    )
    diagnostics = []
    with error_diagnostics_context(None, diagnostics.append):
        first = await apply_cancel(ApplyCancel(50, (91,)), runtime)
        await flush_diagnostics()
        again = await apply_cancel(ApplyCancel(50, (91,)), runtime)
        await flush_diagnostics()

    assert first.items[0].status == again.items[0].status == 4
    for field in ("item_id", "target_action_id", "status", "outcome", "error_code", "error_details_json"):
        assert getattr(first.items[0], field) == getattr(again.items[0], field)
    assert len(diagnostics) == 1
    for fact in (code, '"item_id":91', '"target_action_id":11',
                 '"origin_action_id":50', cancellation_database.metadata.instance_id):
        assert fact in diagnostics[0]

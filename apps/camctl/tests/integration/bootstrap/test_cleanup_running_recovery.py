"""无法合法恢复的旧文件调用有限退出，保留 needs_run 与原尝试。"""

import asyncio

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.application import query_work_facts
from camctl.bootstrap.cleanup_assembly import cleanup_flow, session_cleanup_assembly
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.devices.drivers.registry import DriverRegistry

from ..outputs.test_cleanup_delete_save_recovery import _prepared
from ..outputs.test_cleanup_residual_recovery import _orphan_intent, _orphan_intent_for_prepared
from ..support.outputs import output_test_session
from .test_output_binding_changes import _NOW, Catalog, environment

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("operation", ["delete", "query"])
async def test_run_reports_state_db_error_for_unqualified_original_cleanup(
    environment, operation
):
    with output_test_session():
        if operation == "query":
            from camctl.contracts.values import new_operation_key
            from camctl.outputs.batches import BatchOutcome, BatchRequest
            from camctl.persistence.models import DbOutcomeKind
            from camctl.persistence.repositories.file_batches import FileBatchRepository

            prepared = await _prepared(environment, before_query=True)
            owned = prepared[0]
            prior_action, device = owned.connection.execute(
                "SELECT action_id,device_id FROM device_file_batches WHERE released_event_id IS NULL"
            ).fetchone()
            released = FileBatchRepository().release(BatchRequest(prior_action, device, _NOW),
                new_operation_key(), owned)
            assert released.kind is DbOutcomeKind.COMPLETED and released.value is BatchOutcome.RELEASED, released
            owned, _, driver, item_id, ticket, _ = await _orphan_intent_for_prepared(environment, prepared, operation)
        else:
            owned, _, driver, item_id, ticket, _ = await _orphan_intent(environment, operation)
    cfg = environment[0]
    before_attempts = tuple(
        owned.connection.execute("SELECT * FROM operation_attempts")
    )
    before_members = tuple(owned.connection.execute("SELECT * FROM cleanup_items"))
    diagnostics = []
    factory = session_cleanup_assembly(
        devices={},
        drivers=DriverRegistry(),
        max_delete_attempts=1,
        max_query_attempts=1,
        occurred_at=lambda: _NOW,
        on_recovery_diagnostic=diagnostics.append,
    )
    flow = cleanup_flow(factory, state_db_errors=True)
    deps = build_runtime(CommandMode.RUN, cfg, catalog=Catalog())
    try:
        outcome = await asyncio.wait_for(
            execute_command(deps, None, flows={"cleanup": flow}), timeout=10
        )
        assert outcome.reason == "state_db_error" and not outcome.succeeded
        assert len(diagnostics) == 1
        assert query_work_facts(owned.connection).required_settlements > 0
        assert (
            tuple(owned.connection.execute("SELECT * FROM operation_attempts"))
            == before_attempts
        )
        assert (
            tuple(owned.connection.execute("SELECT * FROM cleanup_items"))
            == before_members
        )
        assert not driver.deletes and not driver.queries
    finally:
        close_runtime(deps)
        owned.connection.close()

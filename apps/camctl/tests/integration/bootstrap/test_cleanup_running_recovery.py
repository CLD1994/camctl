"""无法合法恢复的旧文件调用有限退出，保留 needs_run 与原尝试。"""

import asyncio

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.application import query_work_facts
from camctl.bootstrap.cleanup_assembly import cleanup_flow, session_cleanup_assembly
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.devices.drivers.registry import DriverRegistry

from ..outputs.test_cleanup_residual_recovery import _orphan_intent
from .test_output_binding_changes import _NOW, Catalog, environment

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("operation", ["delete", "query"])
async def test_run_reports_state_db_error_for_unqualified_original_cleanup(
    environment, operation
):
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

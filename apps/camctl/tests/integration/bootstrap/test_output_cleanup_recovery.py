"""清理取消先核对原实际调用和删除事实，再结束成员。"""

from decimal import Decimal
from pathlib import Path

import pytest

from camctl.bootstrap.cleanup_assembly import cleanup_flow, session_cleanup_assembly
from camctl.contracts.values import new_operation_key
from camctl.operations.attempts import AttemptConfig, AttemptIntent, AttemptTarget, OperationKind
from camctl.outputs.cleanup_flow import CancelCleanupItem
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import OutputsRepository

from .test_output_binding_cancellation import _apply_cleanup_cancel
from .test_output_binding_changes import _NOW, _changed, _pending_cleanup, _registry, environment

pytestmark = pytest.mark.asyncio


async def _unended_query(environment):
    _cfg, owned, _context, _driver = environment
    output_id = await _pending_cleanup(environment)
    item_id, action_id = owned.connection.execute("SELECT id,action_id FROM cleanup_items").fetchone()
    result = OperationRepository().begin_attempt(AttemptIntent(
        "query", action_id, OperationKind.CHECK_FILE_EXISTS, AttemptTarget(cleanup_item_id=item_id), None,
        AttemptConfig(3, Decimal("10"), Decimal("0")), _NOW), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.ticket is not None
    _apply_cleanup_cancel(owned)
    return item_id, output_id


async def test_cancel_cleanup_item_rejects_unended_original_query(environment):
    _item_cfg, owned, _context, _driver = environment
    item_id, output_id = await _unended_query(environment)
    before = tuple(owned.connection.iterdump())

    result = OutputsRepository().cancel_cleanup_item(CancelCleanupItem(
        item_id, _NOW, "delete_unconfirmed", {"output_id": str(output_id)}), new_operation_key(), owned)

    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("change", ["missing", "mismatch", "matched"])
async def test_cancel_cleanup_without_original_query_result_keeps_all_responsibility(environment, change):
    cfg, owned, context, driver = environment
    await _unended_query(environment)
    before = tuple(owned.connection.iterdump())
    calls = (tuple(driver.deletes), tuple(driver.queries))
    current = _changed(cfg, change)
    factory = session_cleanup_assembly(
        devices=current.devices, drivers=_registry(driver), max_delete_attempts=3, max_query_attempts=3,
        staging=Path(cfg.paths.staging), occurred_at=lambda: _NOW, monotonic_ns=lambda: 0)

    await cleanup_flow(factory)(context)

    assert tuple(owned.connection.iterdump()) == before
    assert (tuple(driver.deletes), tuple(driver.queries)) == calls


@pytest.mark.parametrize("confirmed_present,incorrect_code", [
    (False, "file_delete_failed"), (True, "delete_unconfirmed"),
])
async def test_cancel_cleanup_item_rejects_error_that_disagrees_with_original_delete_facts(
        environment, confirmed_present, incorrect_code):
    _cfg, owned, _context, _driver = environment
    output_id = await _pending_cleanup(environment, confirmed_present=confirmed_present)
    _apply_cleanup_cancel(owned)
    item_id = owned.connection.execute("SELECT id FROM cleanup_items").fetchone()[0]
    before = tuple(owned.connection.iterdump())

    result = OutputsRepository().cancel_cleanup_item(CancelCleanupItem(
        item_id, _NOW, incorrect_code, {"output_id": str(output_id)}), new_operation_key(), owned)

    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert tuple(owned.connection.iterdump()) == before

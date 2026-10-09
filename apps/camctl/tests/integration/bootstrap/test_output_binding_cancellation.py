"""实际删除结束后，清理取消的逐项结果与原操作键核实。"""

from dataclasses import replace
from pathlib import Path

import pytest

from camctl.bootstrap.cleanup_assembly import cleanup_flow, session_cleanup_assembly
from camctl.cancellation.models import (
    ApplyCancelTarget, CancelApplyMode, CancellationEffect, FinishCancelAction,
    FixCancelTargets, FixedCancelSet, FixedTarget, SelectionBasis, StartCancelAction,
)
from camctl.cancellation.service import ApplyCancel, CancellationRuntime, apply_cancel
from camctl.cancellation.settlement import TargetSettlement
from camctl.contracts.enums import enum_for
from camctl.contracts.values import new_operation_key
from camctl.contracts.workflow_errors import item_error_id
from camctl.devices.bindings import DeviceBinding, check_binding
from camctl.outputs.cleanup_flow import CancelCleanupItem
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.cancellation import CancellationRepository
from camctl.persistence.repositories.outputs import FailCleanupBinding, OutputsRepository

from .test_output_binding_changes import (
    _NOW, _accept, _changed, _pending_cleanup, _registry, environment,
)

pytestmark = pytest.mark.asyncio


def _apply_cleanup_cancel(owned):
    """受理真实取消请求，固定现有清理目标并可靠施加取消。"""
    target = owned.connection.execute("SELECT id FROM actions WHERE type=5").fetchone()[0]
    _accept(owned, "3", [{"name": "取消清理", "type": "cancel_task",
                          "scheduled_at": "2026-01-15 09:00:00",
                          "params": {"target": {"action_instance_id": str(target)}}}])
    origin = owned.connection.execute("SELECT id FROM actions WHERE type=6").fetchone()[0]
    repository = CancellationRepository()
    started = repository.start_cancel_action(StartCancelAction(origin, _NOW), new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    fixed = repository.fix_cancel_targets(FixCancelTargets(origin, FixedCancelSet((
        FixedTarget(target, SelectionBasis.DIRECT, CancellationEffect.NOT_APPLIED),
    )), _NOW), new_operation_key(), owned)
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    applied = repository.apply_cancel_target(ApplyCancelTarget(
        fixed.value.item_ids[0], CancelApplyMode.WITH_STOP, _NOW), new_operation_key(), owned)
    assert applied.kind is DbOutcomeKind.COMPLETED, applied.error
    return target, origin, fixed.value.item_ids[0]


@pytest.mark.parametrize("code", ["delete_unconfirmed", "file_delete_failed"])
async def test_canceled_cleanup_error_fails_actual_cancel_origin(environment, code):
    _cfg, owned, _context, _driver = environment
    output_id = await _pending_cleanup(environment, confirmed_present=code == "file_delete_failed")
    target, origin, cancel_item_id = _apply_cleanup_cancel(owned)
    cleanup_item_id = owned.connection.execute("SELECT id FROM cleanup_items").fetchone()[0]
    saved = OutputsRepository().cancel_cleanup_item(CancelCleanupItem(
        cleanup_item_id, _NOW, code, {"output_id": str(output_id)}), new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    settlement = TargetSettlement(owned, OutputsRepository(), CancellationRepository(),
                                  lambda _: "unknown", lambda: _NOW)

    progress = await apply_cancel(ApplyCancel(origin, (cancel_item_id,)), CancellationRuntime(
        owned, CancellationRepository(), settlement, lambda: _NOW))

    assert progress.items[0].status == int(enum_for("cancel_items.status").FAILED)
    assert progress.items[0].error_code == item_error_id("cancel_items", "target_cleanup_failed")
    assert owned.connection.execute("SELECT status,cancel_requested FROM actions WHERE id=?", (target,)).fetchone() == (6, 1)
    assert owned.connection.execute("SELECT status,error_code FROM cleanup_items").fetchone() == (
        6, item_error_id("cleanup_items", code))
    finished = CancellationRepository().finish_cancel_action(
        FinishCancelAction(origin, _NOW), new_operation_key(), owned)
    assert finished.kind is DbOutcomeKind.COMPLETED, finished.error
    assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (origin,)).fetchone() == (4,)


@pytest.mark.parametrize("change", ["missing", "mismatch"])
async def test_canceled_cleanup_binding_failure_ends_original_open_runs(environment, change):
    cfg, owned, context, driver = environment
    output_id = await _pending_cleanup(environment)
    target, _origin, _cancel_item = _apply_cleanup_cancel(owned)
    attempts = tuple(owned.connection.execute(
        "SELECT * FROM operation_attempts WHERE run_id IN"
        " (SELECT id FROM operation_runs WHERE cleanup_item_id IS NOT NULL) ORDER BY id"))
    source_before = owned.connection.execute(
        "SELECT * FROM device_files WHERE id=(SELECT device_file_id FROM outputs WHERE id=?)", (output_id,)).fetchone()
    current = _changed(cfg, change)
    factory = session_cleanup_assembly(
        devices=current.devices, drivers=_registry(driver), max_delete_attempts=3,
        max_query_attempts=3, staging=Path(cfg.paths.staging), occurred_at=lambda: _NOW,
        monotonic_ns=lambda: 0)

    await cleanup_flow(factory)(context)

    assert len(driver.deletes) == len(driver.queries) == 1
    assert tuple(owned.connection.execute(
        "SELECT * FROM operation_attempts WHERE run_id IN"
        " (SELECT id FROM operation_runs WHERE cleanup_item_id IS NOT NULL) ORDER BY id")) == attempts
    assert owned.connection.execute("SELECT status,restriction_state,error_code FROM cleanup_items").fetchone() == (
        6, int(enum_for("cleanup_items.restriction_state").IRREVERSIBLE),
        item_error_id("cleanup_items", "delete_unconfirmed"))
    assert owned.connection.execute(
        "SELECT status,attempts_used,retry_wait_required FROM operation_runs"
        " WHERE cleanup_item_id IS NOT NULL ORDER BY id").fetchall() == [(4, 1, 0), (4, 1, 0)]
    assert owned.connection.execute(
        "SELECT * FROM device_files WHERE id=(SELECT device_file_id FROM outputs WHERE id=?)", (output_id,)).fetchone() == source_before
    settled = await TargetSettlement(owned, OutputsRepository(), CancellationRepository(),
                                    lambda _: "unknown", lambda: _NOW).settle(target)
    assert settled.complete and settled.failed


@pytest.mark.parametrize("change", ["code", "clear_error", "details", "item_id", "occurred_at"])
async def test_cleanup_cancel_original_key_rejects_changed_adopted_input(environment, change):
    _cfg, owned, _context, _driver = environment
    output_id = await _pending_cleanup(environment)
    _apply_cleanup_cancel(owned)
    item_id = owned.connection.execute("SELECT id FROM cleanup_items").fetchone()[0]
    command = CancelCleanupItem(item_id, _NOW, "delete_unconfirmed", {"output_id": str(output_id)})
    repository, key = OutputsRepository(), new_operation_key()
    saved = repository.cancel_cleanup_item(command, key, owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    before = tuple(owned.connection.iterdump())
    changed = {
        "code": replace(command, code="file_delete_failed"),
        "clear_error": replace(command, code=None, details=None),
        "details": replace(command, details={"output_id": str(output_id + 1)}),
        "item_id": replace(command, item_id=item_id + 1),
        "occurred_at": replace(command, occurred_at=_NOW + 1),
    }[change]

    result = repository.cancel_cleanup_item(changed, key, owned)

    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert tuple(owned.connection.iterdump()) == before
    assert repository.cancel_cleanup_item(command, key, owned).kind is DbOutcomeKind.COMPLETED
    assert tuple(owned.connection.iterdump()) == before


async def test_cleanup_cancel_rejects_detail_for_other_original_output(environment):
    _cfg, owned, _context, _driver = environment
    output_id = await _pending_cleanup(environment)
    _apply_cleanup_cancel(owned)
    item_id = owned.connection.execute("SELECT id FROM cleanup_items").fetchone()[0]
    before = tuple(owned.connection.iterdump())

    result = OutputsRepository().cancel_cleanup_item(CancelCleanupItem(
        item_id, _NOW, "delete_unconfirmed", {"output_id": str(output_id + 1)}), new_operation_key(), owned)

    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert tuple(owned.connection.iterdump()) == before


async def test_single_item_cancel_cannot_reuse_original_composite_binding_key(environment):
    cfg, owned, _context, _driver = environment
    output_id = await _pending_cleanup(environment)
    _apply_cleanup_cancel(owned)
    item_id, source_id = owned.connection.execute(
        "SELECT c.id,o.device_file_id FROM cleanup_items c JOIN outputs o ON o.id=c.output_id").fetchone()
    keys = tuple(row[0] for row in owned.connection.execute(
        "SELECT responsibility_key FROM operation_runs WHERE cleanup_item_id=? ORDER BY id", (item_id,)))
    repository, key = OutputsRepository(), new_operation_key()
    compound = FailCleanupBinding(item_id, source_id, check_binding(
        DeviceBinding("cam-a", "camctl-adb"), _changed(cfg, "missing")), keys, _NOW, canceled=True)
    saved = repository.fail_cleanup_binding(compound, key, owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    before = tuple(owned.connection.iterdump())

    result = repository.cancel_cleanup_item(CancelCleanupItem(
        item_id, _NOW, "delete_unconfirmed", {"output_id": str(output_id)}), key, owned)

    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert tuple(owned.connection.iterdump()) == before
    assert repository.fail_cleanup_binding(compound, key, owned).kind is DbOutcomeKind.COMPLETED
    assert tuple(owned.connection.iterdump()) == before

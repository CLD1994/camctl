"""交付撤回的完整原键、固定等待明细及提交未知核实。"""

from dataclasses import replace
from pathlib import Path
import json

import pytest

from camctl.bootstrap.flows import _resolve_and_fix
from camctl.cancellation.models import ApplyCancelTarget, CancelApplyMode, StartCancelAction
from camctl.contracts.values import new_operation_key
from camctl.contracts.workflow_errors import item_error_id
from camctl.outputs.handoff import AdvanceWithdrawal, WithdrawalChoice
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.cancellation import CancellationRepository
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import TransactionError, saved_transaction_events

from .test_binding_transactions import _CommitFailure
from .test_cancel_ready_runtime import _published
from .test_output_binding_changes import _NOW, _accept, environment  # noqa: F401
from ..operations.test_result_reuse import _FaultConnection

pytestmark = pytest.mark.asyncio


def _apply_origin(owned, origin):
    repository = CancellationRepository()
    spec = owned.connection.execute("SELECT input_fields_json FROM actions WHERE id=?", (origin,)).fetchone()[0]
    started = repository.start_cancel_action(StartCancelAction(origin, _NOW), new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    items = _resolve_and_fix(owned, repository, origin, spec, lambda: _NOW)
    assert len(items) == 1
    key = new_operation_key()
    applied = repository.apply_cancel_target(
        ApplyCancelTarget(items[0], CancelApplyMode.TERMINAL, _NOW), key, owned)
    assert applied.kind is DbOutcomeKind.COMPLETED, applied.error
    assert owned.connection.execute("SELECT status,cancel_requested FROM actions WHERE id=(SELECT target_action_id FROM cancel_items WHERE id=?)", (items[0],)).fetchone() == (3,0)
    return key


async def _pending(environment, *, second_origin=False):
    target, _contents, runtime = await _published(environment)
    owned = environment[1]
    if second_origin:
        _accept(owned, "4", [{"name": "第二个撤回请求", "type": "cancel_task",
            "scheduled_at": "2026-01-15 09:00:00",
            "params": {"target": {"action_instance_id": str(target)}}}])
    for (origin,) in owned.connection.execute("SELECT id FROM actions WHERE type=6 ORDER BY id").fetchall():
        _apply_origin(owned, origin)
    delivery_ids = tuple(row[0] for row in owned.connection.execute("SELECT id FROM deliveries ORDER BY id"))
    repository = OutputsRepository()
    for delivery_id in delivery_ids:
        result = repository.advance_withdrawal(
            AdvanceWithdrawal(delivery_id, WithdrawalChoice.REQUESTED, _NOW), new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
    return target, delivery_ids, runtime


def _error(delivery_id, marker="first"):
    return {"code": "withdrawal_unconfirmed", "stage": "publication",
            "details": {"delivery_id": str(delivery_id), "error": marker}}


@pytest.mark.parametrize("changed", ["delivery", "choice", "error", "time"])
async def test_withdrawal_original_key_rejects_changed_full_input(environment, changed):
    _target, ids, _runtime = await _pending(environment)
    owned, repository = environment[1], OutputsRepository()
    command = AdvanceWithdrawal(ids[0], WithdrawalChoice.UNKNOWN, _NOW, _error(ids[0]))
    key = new_operation_key()
    saved = repository.advance_withdrawal(command, key, owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    changed_command = replace(command, **({"delivery_id": ids[1]} if changed == "delivery" else
        {"choice": WithdrawalChoice.FAILED} if changed == "choice" else
        {"error": _error(ids[0], "changed")} if changed == "error" else {"occurred_at": _NOW+1}))
    baseline = tuple(owned.connection.iterdump())
    rejected = repository.advance_withdrawal(changed_command, key, owned)
    assert rejected.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(rejected.error, TransactionError)
    assert tuple(owned.connection.iterdump()) == baseline


async def test_withdrawal_result_cannot_reuse_requested_only_key(environment):
    target, _contents, _runtime = await _published(environment)
    owned, repository = environment[1], OutputsRepository()
    _apply_origin(owned, owned.connection.execute("SELECT id FROM actions WHERE type=6").fetchone()[0])
    delivery_id = owned.connection.execute("SELECT id FROM deliveries ORDER BY id LIMIT 1").fetchone()[0]
    key = new_operation_key()
    result = repository.advance_withdrawal(AdvanceWithdrawal(delivery_id, WithdrawalChoice.REQUESTED, _NOW), key, owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    baseline = tuple(owned.connection.iterdump())
    rejected = repository.advance_withdrawal(AdvanceWithdrawal(delivery_id, WithdrawalChoice.WITHDRAWN, _NOW), key, owned)
    assert rejected.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(rejected.error, TransactionError)
    assert tuple(owned.connection.iterdump()) == baseline
    assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (target,)).fetchone() == (3,)


@pytest.mark.parametrize("choice", [WithdrawalChoice.WITHDRAWN, WithdrawalChoice.NOT_RETRACTABLE,
                                    WithdrawalChoice.FAILED, WithdrawalChoice.UNKNOWN])
async def test_one_withdrawal_result_finishes_all_existing_request_waiters(environment, choice):
    target, ids, _runtime = await _pending(environment, second_origin=True)
    owned, repository = environment[1], OutputsRepository()
    before_target = owned.connection.execute("SELECT status,cancel_requested,error_code,error_details_json,execution_spec_json FROM actions WHERE id=?", (target,)).fetchone()
    command = AdvanceWithdrawal(ids[0], choice, _NOW,
        _error(ids[0]) if choice in (WithdrawalChoice.FAILED, WithdrawalChoice.UNKNOWN) else None)
    key = new_operation_key()
    saved = repository.advance_withdrawal(command, key, owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    expected = (2, None) if choice is WithdrawalChoice.WITHDRAWN else (
        (3, None) if choice is WithdrawalChoice.NOT_RETRACTABLE else
        (4, item_error_id("cancel_delivery_items", "delivery_withdrawal_failed" if choice is WithdrawalChoice.FAILED else "delivery_position_unconfirmed")))
    items = owned.connection.execute("SELECT status,error_code FROM cancel_delivery_items WHERE delivery_id=? ORDER BY id", (ids[0],)).fetchall()
    assert items == [expected, expected]
    if choice in (WithdrawalChoice.FAILED, WithdrawalChoice.UNKNOWN):
        details = owned.connection.execute("SELECT error_details_json FROM cancel_delivery_items WHERE delivery_id=? ORDER BY id", (ids[0],)).fetchall()
        assert [json.loads(row[0]) for row in details] == [{"delivery_id": str(ids[0])}] * 2
        assert json.loads(owned.connection.execute("SELECT withdrawal_error_json FROM deliveries WHERE id=?", (ids[0],)).fetchone()[0]) == command.error
    events = saved_transaction_events(owned.connection, key)
    assert [(e["type"], e["reason"]) for e in events] == [(23, 7)]
    assert [(r["table"], r["id"]) for r in events[0]["body"]["rows"]] == [
        ("deliveries", ids[0]), *[("cancel_delivery_items", row[0]) for row in owned.connection.execute(
            "SELECT id FROM cancel_delivery_items WHERE delivery_id=? ORDER BY id", (ids[0],))]]
    assert owned.connection.execute("SELECT status,cancel_requested,error_code,error_details_json,execution_spec_json FROM actions WHERE id=?", (target,)).fetchone() == before_target
    baseline = tuple(owned.connection.iterdump())
    assert repository.advance_withdrawal(command, key, owned).kind is DbOutcomeKind.COMPLETED
    assert tuple(owned.connection.iterdump()) == baseline


@pytest.mark.parametrize("phase", ["projection", "commit_before", "commit_after"])
async def test_withdrawal_whole_waiter_group_recovers_original_key(environment, phase):
    _target, ids, _runtime = await _pending(environment, second_origin=True)
    cfg, owned, _context, _driver = environment
    repository, key = OutputsRepository(), new_operation_key()
    command = AdvanceWithdrawal(ids[0], WithdrawalChoice.WITHDRAWN, _NOW)
    baseline = tuple(owned.connection.iterdump())
    faulty = (_FaultConnection(owned.connection, "UPDATE cancel_delivery_items") if phase == "projection" else
              _CommitFailure(owned.connection, phase == "commit_after"))
    saved = repository.advance_withdrawal(command, key, replace(owned, connection=faulty))
    assert saved.kind is (DbOutcomeKind.ROLLED_BACK if phase == "projection" else DbOutcomeKind.UNKNOWN)
    if phase == "projection":
        assert tuple(owned.connection.iterdump()) == baseline
    owned.connection.close()
    reopened = open_existing(Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
    try:
        if phase == "commit_before":
            assert tuple(reopened.connection.iterdump()) == baseline
        confirmed = repository.advance_withdrawal(command, key, reopened)
        assert confirmed.kind is DbOutcomeKind.COMPLETED, confirmed.error
        assert reopened.connection.execute("SELECT status,error_code FROM cancel_delivery_items WHERE delivery_id=? ORDER BY id", (ids[0],)).fetchall() == [(2,None),(2,None)]
        events = saved_transaction_events(reopened.connection, key)
        assert len(events) == 1 and len(events[0]["body"]["rows"]) == 3
        after = tuple(reopened.connection.iterdump())
        assert repository.advance_withdrawal(command, key, reopened).kind is DbOutcomeKind.COMPLETED
        assert tuple(reopened.connection.iterdump()) == after
    finally:
        reopened.connection.close()


@pytest.mark.parametrize("original_choice", [None, WithdrawalChoice.FAILED, WithdrawalChoice.UNKNOWN])
async def test_later_cancel_request_joins_original_pending_or_failed_delivery_responsibility(environment, monkeypatch, original_choice):
    from camctl.bootstrap.flows import cancel_flow
    from camctl.host_files import handoff

    target, ids, runtime = await _pending(environment)
    cfg, owned, context, _driver = environment
    if original_choice is not None:
        repository = OutputsRepository()
        for delivery_id in ids:
            result = repository.advance_withdrawal(
                AdvanceWithdrawal(delivery_id, original_choice, _NOW, _error(delivery_id)), new_operation_key(), owned)
            assert result.kind is DbOutcomeKind.COMPLETED, result.error
    _accept(owned, "4", [{"name": "后到撤回请求", "type": "cancel_task",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"target": {"action_instance_id": str(target)}}}])
    second = owned.connection.execute("SELECT id FROM actions WHERE type=6 ORDER BY id DESC LIMIT 1").fetchone()[0]
    _apply_origin(owned, second)
    assert owned.connection.execute("SELECT COUNT(*) FROM cancel_delivery_items WHERE cancel_item_id IN (SELECT id FROM cancel_items WHERE action_id=?)", (second,)).fetchone() == (2,)
    removed, original_remove = [], handoff._remove
    def remove(path):
        removed.append(path.name)
        original_remove(path)
    monkeypatch.setattr(handoff, "_remove", remove)
    await cancel_flow(ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing), work_files=runtime)(context)
    statuses = owned.connection.execute("SELECT cdi.status FROM cancel_delivery_items cdi JOIN cancel_items ci ON ci.id=cdi.cancel_item_id WHERE ci.action_id=? ORDER BY cdi.id", (second,)).fetchall()
    assert statuses == ([(2,),(2,)] if original_choice is None else [(4,),(4,)])
    assert len(removed) == (2 if original_choice is None else 0)
    assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (second,)).fetchone() == ((3,) if original_choice is None else (4,))
    assert owned.connection.execute("SELECT status,cancel_requested FROM actions WHERE id=?", (target,)).fetchone() == (3,0)


async def test_original_withdrawal_key_excludes_member_created_after_original_boundary_even_if_parent_is_older(environment):
    target, ids, _runtime = await _pending(environment)
    owned, repository = environment[1], OutputsRepository()
    _accept(owned, "4", [{"name": "先接受后生效的撤回", "type": "cancel_task",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"target": {"action_instance_id": str(target)}}}])
    second = owned.connection.execute("SELECT id FROM actions WHERE type=6 ORDER BY id DESC LIMIT 1").fetchone()[0]
    command, key = AdvanceWithdrawal(ids[0], WithdrawalChoice.FAILED, _NOW, _error(ids[0])), new_operation_key()
    saved = repository.advance_withdrawal(command, key, owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    original_boundary = saved_transaction_events(owned.connection, key)[0]["transaction"]
    late_key = _apply_origin(owned, second)
    late = owned.connection.execute("SELECT c.id,c.status FROM cancel_delivery_items c JOIN cancel_items ci ON ci.id=c.cancel_item_id WHERE ci.action_id=? ORDER BY c.id", (second,)).fetchall()
    assert len(late) == 2 and all(row[1] == 1 for row in late)
    assert owned.connection.execute("SELECT created_event_id FROM actions WHERE id=?", (second,)).fetchone()[0] < original_boundary.last_event_id
    late_events = saved_transaction_events(owned.connection, late_key)
    assert late_events[0]["transaction"].first_event_id > original_boundary.last_event_id
    assert [(row["id"],row["before"]["exists"],row["after"]["exists"]) for event in late_events
            for row in event["body"]["rows"] if row["table"] == "cancel_delivery_items"] == [(row[0],False,True) for row in late]
    baseline = tuple(owned.connection.iterdump())
    repeated = repository.advance_withdrawal(command, key, owned)
    assert repeated.kind is DbOutcomeKind.COMPLETED, repeated.error
    assert tuple(owned.connection.iterdump()) == baseline
    assert len(saved_transaction_events(owned.connection, key)[0]["body"]["rows"]) == 2

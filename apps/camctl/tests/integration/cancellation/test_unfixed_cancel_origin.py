"""C2 经公开取消入口结束尚未固定目标的 C1，并保留原目标。"""

from dataclasses import replace
from types import SimpleNamespace

import pytest
import pytest_asyncio

from camctl.acceptance.service import AcceptanceContext, CommandMode
from camctl.cancellation.models import (
    ApplyCancelTarget, CancelApplyMode, FinishCancelAction, FixCancelTargets,
    FixedCancelSet, FixedTarget, SelectionBasis, CancellationEffect, StartCancelAction,
)
from camctl.cancellation.service import (
    ApplyCancel, CancellationRuntime, OriginSettle, apply_cancel, settle_origin_cancel,
)
from camctl.cancellation.settlement import TargetSettlement
from camctl.contracts.values import new_operation_key, to_utc_micros
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository
from camctl.persistence.repositories.cancellation import CancellationRepository
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.repositories.scheduling import SchedulingRepository, StartActionRequest
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import row_facts

from ..acceptance.test_acceptance import Catalog, _plan_body
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.repositories.cancellation import register_cancellation_guards

register_capture_guards()
register_outputs_guards()
register_cancellation_guards()
from ..support.acceptance import accept_document
from ..support.database import business_database, create_test_database

pytestmark = pytest.mark.asyncio
_NOW = int(to_utc_micros("2026-01-15 09:00:00"))


@pytest_asyncio.fixture
async def environment(tmp_path):
    path = tmp_path / "state.db"
    create_test_database(path)
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    clock = SimpleNamespace(utc_micros=lambda: _NOW)
    class PreviewCatalog(Catalog):
        def parameter_definition(self, *args):
            return replace(super().parameter_definition(*args), preview_supported=True)
    context = AcceptanceContext(CommandMode.RUN, PreviewCatalog(), AcceptanceRepository(), clock)
    photo = _plan_body()["actions"][0]
    preview = {"name": "预览", "type": "obtain_action_outputs",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"source": {"action_name": "shoot"}, "filter": "preview", "purpose": "auto_preview"}}
    cancel = {"name": "C1", "type": "cancel_task", "params": {"target": {"action_instance_id": "1"}}}
    try:
        await accept_document(_plan_body(actions=[photo, preview, cancel]), context, path)
        started = SchedulingRepository().start_action(
            StartActionRequest(1, _NOW, _NOW), new_operation_key(), owned)
        assert started.kind is DbOutcomeKind.COMPLETED, started.error
        repository = CancellationRepository()
        started = repository.start_cancel_action(StartCancelAction(3, _NOW), new_operation_key(), owned)
        assert started.kind is DbOutcomeKind.COMPLETED, started.error
        async with business_database(path) as database:
            runtime = CancellationRuntime(database, repository,
                TargetSettlement(database, OutputsRepository(), repository,
                    lambda _: "unknown", lambda: _NOW), lambda: _NOW)
            yield owned, path, context, repository, runtime
    finally:
        owned.connection.close()


async def _mark_c1(environment, *, apply=True):
    owned, path, context, repository, runtime = environment
    result = await accept_document(_plan_body(request_id="2", actions=[{
        "name": "C2", "type": "cancel_task", "params": {"target": {"action_instance_id": "3"}},
    }]), context, path)
    assert result.plan_id == 2
    started = repository.start_cancel_action(StartCancelAction(4, _NOW), new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    fixed = repository.fix_cancel_targets(FixCancelTargets(4, FixedCancelSet((
        FixedTarget(3, SelectionBasis.DIRECT, CancellationEffect.NOT_APPLIED),)), _NOW),
        new_operation_key(), owned)
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    if apply:
        applied = repository.apply_cancel_target(
            ApplyCancelTarget(fixed.value.item_ids[0], CancelApplyMode.WITH_STOP, _NOW),
            new_operation_key(), owned)
        assert applied.kind is DbOutcomeKind.COMPLETED, applied.error
    return fixed.value.item_ids


async def test_real_c2_cancels_running_unfixed_c1_without_touching_its_targets(environment):
    owned, _, _, _, runtime = environment
    ids = await _mark_c1(environment, apply=False)
    progress = await apply_cancel(ApplyCancel(4, ids), runtime)
    assert [item.status for item in progress.items] == [3]
    assert owned.connection.execute("SELECT status,target_selection_state FROM actions WHERE id=3").fetchone() == (6, 1)
    assert owned.connection.execute("SELECT id FROM cancel_items WHERE action_id=3").fetchall() == []
    assert owned.connection.execute("SELECT id FROM operation_runs").fetchall() == []
    assert owned.connection.execute("SELECT cancel_requested FROM actions WHERE id IN (1,2)").fetchall() == [(0,), (0,)]
    before = tuple(owned.connection.iterdump())
    again = await settle_origin_cancel(OriginSettle(3), runtime)
    assert again.kind is DbOutcomeKind.COMPLETED
    assert again.value.succeeded == again.value.failed == 0
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize('pending_sibling', [False, True], ids=['last-action', 'pending-sibling'])
async def test_canceled_origin_finishes_parent_only_when_last_action(tmp_path, pending_sibling):
    path = tmp_path / 'state.db'
    create_test_database(path)
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    clock = SimpleNamespace(utc_micros=lambda: _NOW)
    context = AcceptanceContext(CommandMode.RUN, Catalog(), AcceptanceRepository(), clock)
    repository = CancellationRepository()
    try:
        assert (await accept_document(_plan_body(request_id='1'), context, path)).plan_id == 1
        actions = [{
            'name': 'C1', 'type': 'cancel_task',
            'params': {'target': {'action_instance_id': '1'}},
        }]
        if pending_sibling:
            actions.append({'name': '未来报告', 'type': 'report_status',
                'scheduled_at': '2026-01-15 10:00:00', 'params': {'scope': 'full'}})
        assert (await accept_document(_plan_body(request_id='2', actions=actions), context, path)).plan_id == 2
        origin, = owned.connection.execute("SELECT id FROM actions WHERE plan_id=2 AND type=6").fetchone()
        started = repository.start_cancel_action(StartCancelAction(origin, _NOW), new_operation_key(), owned)
        assert started.kind is DbOutcomeKind.COMPLETED, started.error
        plan_before = owned.connection.execute('SELECT status,last_event_id FROM plans WHERE id=2').fetchone()

        c2_body = _plan_body(request_id='3', actions=[{
            'name': 'C2', 'type': 'cancel_task',
            'params': {'target': {'action_instance_id': str(origin)}},
        }])
        assert (await accept_document(c2_body, context, path)).plan_id == 3
        c2, = owned.connection.execute('SELECT id FROM actions WHERE plan_id=3').fetchone()
        assert repository.start_cancel_action(StartCancelAction(c2, _NOW), new_operation_key(), owned).kind is DbOutcomeKind.COMPLETED
        fixed = repository.fix_cancel_targets(FixCancelTargets(c2, FixedCancelSet((
            FixedTarget(origin, SelectionBasis.DIRECT, CancellationEffect.NOT_APPLIED),)), _NOW),
            new_operation_key(), owned)
        assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
        applied = repository.apply_cancel_target(
            ApplyCancelTarget(fixed.value.item_ids[0], CancelApplyMode.WITH_STOP, _NOW),
            new_operation_key(), owned)
        assert applied.kind is DbOutcomeKind.COMPLETED, applied.error
        assert owned.connection.execute(
            'SELECT status,cancel_requested,target_selection_state FROM actions WHERE id=?',
            (origin,)).fetchone() == (2, 1, 1)
        assert owned.connection.execute('SELECT COUNT(*) FROM cancel_items WHERE action_id=?',
                                        (origin,)).fetchone() == (0,)
        key = new_operation_key()
        request = FinishCancelAction(origin, _NOW)
        result = repository.finish_origin_canceled(request, key, owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value.action_status == 6
        after = owned.connection.execute('SELECT status,last_event_id FROM plans WHERE id=2').fetchone()
        assert after[0] == (plan_before[0] if pending_sibling else 3)
        if pending_sibling:
            assert after == plan_before
            assert owned.connection.execute(
                "SELECT status,execution_started FROM actions WHERE plan_id=2 AND name='未来报告'"
            ).fetchone() == (1, 0)
        else:
            events = owned.connection.execute(
                'SELECT event_type,transaction_id FROM history_events WHERE id IN ('
                'SELECT last_event_id FROM actions WHERE id=? UNION ALL SELECT last_event_id FROM plans WHERE id=2)',
                (origin,)).fetchall()
            assert {event[0] for event in events} == {8, 9}
            assert len({event[1] for event in events}) == 1
        # 其他计划仍有待执行动作，不能阻止当前计划的末项结束。
        assert owned.connection.execute('SELECT status FROM actions WHERE plan_id=1').fetchone() == (1,)
        before_replay = tuple(owned.connection.iterdump())
        assert repository.finish_origin_canceled(request, key, owned).kind is DbOutcomeKind.COMPLETED
        assert tuple(owned.connection.iterdump()) == before_replay
    finally:
        owned.connection.close()


@pytest.mark.parametrize("fault", ["pending_members", "fixed_empty", "failed_empty", "read_failure"])
async def test_finish_itself_rechecks_complete_pending_empty_members(environment, monkeypatch, fault):
    import sqlite3
    owned, _, _, repository, _ = environment
    await _mark_c1(environment)
    if fault == "pending_members":
        owned.connection.execute("INSERT INTO cancel_items(id,action_id,target_action_id,selection_basis,status,cancellation_effect) VALUES (99,3,1,1,1,1)")
    elif fault == "fixed_empty":
        owned.connection.execute("UPDATE actions SET target_selection_state=2 WHERE id=3")
    elif fault == "failed_empty":
        from camctl.persistence.repositories import cancellation as module
        original = module.row_facts
        def contradictory(connection, table, identity):
            row = original(connection, table, identity)
            return {**row, "target_selection_state": 3} if table == "actions" and identity == 3 else row
        monkeypatch.setattr(module, "row_facts", contradictory)
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    if fault == "read_failure":
        owned.connection.set_authorizer(lambda operation, table, *_: sqlite3.SQLITE_DENY
            if operation == sqlite3.SQLITE_READ and table == "cancel_items" else sqlite3.SQLITE_OK)
    try:
        result = repository.finish_origin_canceled(FinishCancelAction(3, _NOW), new_operation_key(), owned)
    finally:
        owned.connection.set_authorizer(None)
    assert result.kind is not DbOutcomeKind.COMPLETED
    assert tuple(owned.connection.iterdump()) == before
    assert row_facts(owned.connection, "actions", 3)["status"] == 2
    assert row_facts(owned.connection, "actions", 1)["cancel_requested"] == 0


@pytest.mark.parametrize("committed", [False, True])
async def test_empty_finish_reuses_original_key_and_time_after_unknown(environment, monkeypatch, committed):
    from camctl.persistence.repositories import cancellation as module
    from camctl.persistence.transaction import WriteReceipt
    owned, _, _, repository, _ = environment
    await _mark_c1(environment)
    command = FinishCancelAction(3, _NOW)
    key = new_operation_key()
    original = module.commit_operation
    def lost_receipt(planner, operation_key, connection):
        if committed:
            saved = original(planner, operation_key, connection)
            assert saved.kind == "completed", saved.error
            return replace(saved, kind="unknown", error=OSError("receipt"))
        return WriteReceipt(kind="unknown", error=OSError("receipt"))
    with monkeypatch.context() as patch:
        patch.setattr(module, "commit_operation", lost_receipt)
        assert repository.finish_origin_canceled(command, key, owned).kind is DbOutcomeKind.UNKNOWN
    result = repository.finish_origin_canceled(command, key, owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.succeeded == result.value.failed == 0
    before = tuple(owned.connection.iterdump())
    assert repository.finish_origin_canceled(command, key, owned).kind is DbOutcomeKind.COMPLETED
    assert repository.finish_origin_canceled(replace(command, occurred_at=command.occurred_at + 1), key, owned).kind is not DbOutcomeKind.COMPLETED
    assert tuple(owned.connection.iterdump()) == before
    assert row_facts(owned.connection, "actions", 1)["cancel_requested"] == 0

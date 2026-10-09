"""取消取回的原成员、交付、读取与源保护共同收场及完整原键。"""

from dataclasses import replace
from pathlib import Path

import pytest

from camctl.bootstrap.flows import _resolve_and_fix
from camctl.bootstrap.obtain_assembly import obtain_flow, session_obtain_assembly
from camctl.cancellation.models import (
    ApplyCancelTarget, CancelApplyMode, StartCancelAction,
)
from camctl.contracts.values import new_operation_key
from camctl.outputs.handoff import (
    DeliveryContext, DeliveryDirectories, DeliveryPhase,
    PublicationIntentRequest, publish_delivery,
)
from camctl.outputs.work_files import RetentionRelease
from camctl.outputs.slots import SlotRequest
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories import outputs
from camctl.persistence.repositories.cancellation import CancellationRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import TransactionError, saved_transaction_events

from .test_binding_transactions import _CommitFailure
from .test_cancel_work_file_first_result import _active_obtain
from .test_output_binding_changes import (
    _NOW, _accept, _registry, _save_photos, environment,  # noqa: F401
)
from ..operations.test_result_reuse import _FaultConnection

pytestmark = pytest.mark.asyncio


def _request(owned, target):
    # 延后导入使缺少正式接口的反例也先证明真实前置事实成立。
    from camctl.persistence.repositories.outputs import SettleCanceledObtain
    ids = tuple(row[0] for row in owned.connection.execute(
        "SELECT i.id FROM obtain_items i JOIN obtain_source_selections s ON s.id=i.selection_id"
        " JOIN action_dependencies d ON d.id=s.dependency_id WHERE d.action_id=? ORDER BY i.id", (target,)))
    return SettleCanceledObtain(target, _NOW, ids)


def _apply(owned, target):
    _accept(owned, "3", [{"name": "取消取回", "type": "cancel_task",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"target": {"action_instance_id": str(target)}}}])
    origin, spec = owned.connection.execute(
        "SELECT id,input_fields_json FROM actions WHERE type=6").fetchone()
    repository = CancellationRepository()
    begun = repository.start_cancel_action(StartCancelAction(origin, _NOW), new_operation_key(), owned)
    assert begun.kind is DbOutcomeKind.COMPLETED, begun.error
    items = _resolve_and_fix(owned, repository, origin, spec, lambda: _NOW)
    assert len(items) == 1
    applied = repository.apply_cancel_target(
        ApplyCancelTarget(items[0], CancelApplyMode.WITH_STOP, _NOW), new_operation_key(), owned)
    assert applied.kind is DbOutcomeKind.COMPLETED, applied.error
    assert owned.connection.execute("SELECT status,cancel_requested FROM actions WHERE id=?", (target,)).fetchone() == (2, 1)


async def _partial(environment):
    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    target = await _active_obtain(environment, "2")
    _apply(owned, target)
    return target


def _attempts(owned):
    return tuple(owned.connection.execute("SELECT * FROM operation_attempts ORDER BY id"))


def _assert_closed(owned, target, original_attempts):
    assert owned.connection.execute("SELECT status,cancel_requested FROM actions WHERE id=?", (target,)).fetchone() == (6, 1)
    assert owned.connection.execute("SELECT status FROM deliveries WHERE action_id=? ORDER BY id", (target,)).fetchall() == [(7,), (7,)]
    assert owned.connection.execute("SELECT status,retry_wait_required,attempts_used FROM operation_runs WHERE copy_id IS NOT NULL ORDER BY id").fetchall() == [(5, 0, 1), (5, 0, 1)]
    assert owned.connection.execute("SELECT source_dependency FROM obtain_items ORDER BY id").fetchall() == [(0,), (0,)]
    assert owned.connection.execute("SELECT slot_device_id,committed_bytes FROM file_copies ORDER BY id").fetchall() == [(None, 4), (None, 4)]
    assert _attempts(owned) == original_attempts


@pytest.mark.parametrize("phase", ["projection", "commit_before", "commit_after"])
async def test_cancel_obtain_whole_group_recovers_after_database_failure(environment, phase):
    cfg, owned, _context, _driver = environment
    target = await _partial(environment)
    request, key = _request(owned, target), new_operation_key()
    attempts, baseline = _attempts(owned), tuple(owned.connection.iterdump())
    faulty = (_FaultConnection(owned.connection, "UPDATE operation_runs") if phase == "projection"
              else _CommitFailure(owned.connection, phase == "commit_after"))
    result = outputs.OutputsRepository().settle_canceled_obtain(request, key, replace(owned, connection=faulty))
    assert result.kind is (DbOutcomeKind.ROLLED_BACK if phase == "projection" else DbOutcomeKind.UNKNOWN)
    if phase == "projection":
        assert tuple(owned.connection.iterdump()) == baseline
    owned.connection.close()
    reopened = open_existing(Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
    try:
        if phase == "commit_before":
            assert tuple(reopened.connection.iterdump()) == baseline
        saved = outputs.OutputsRepository().settle_canceled_obtain(request, key, reopened)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        assert saved.value.complete
        assert len(saved.value.file_ids) == 2
        _assert_closed(reopened, target, attempts)
        events = saved_transaction_events(reopened.connection, key)
        assert [(event["type"], event["reason"]) for event in events] == [
            (23, 6), (10, 3), (21, 3), (22, 6),
            (23, 6), (10, 3), (21, 3), (22, 6), (8, 4), (9, 2)]
        before = tuple(reopened.connection.iterdump())
        assert outputs.OutputsRepository().settle_canceled_obtain(request, key, reopened).kind is DbOutcomeKind.COMPLETED
        assert tuple(reopened.connection.iterdump()) == before
    finally:
        reopened.connection.close()


@pytest.mark.parametrize("changed", ["target", "time", "members"])
async def test_cancel_obtain_original_key_rejects_changed_request(environment, changed):
    target = await _partial(environment)
    owned = environment[1]
    request, key = _request(owned, target), new_operation_key()
    saved = outputs.OutputsRepository().settle_canceled_obtain(request, key, owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    different = replace(request, **({"action_id": 1} if changed == "target" else
        {"occurred_at": _NOW + 1} if changed == "time" else {"item_ids": request.item_ids[:1]}))
    before = tuple(owned.connection.iterdump())
    rejected = outputs.OutputsRepository().settle_canceled_obtain(different, key, owned)
    assert rejected.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(rejected.error, TransactionError)
    assert tuple(owned.connection.iterdump()) == before


async def test_cancel_obtain_original_key_keeps_group_after_file_lifecycle_changes(environment):
    target = await _partial(environment)
    owned = environment[1]
    request, key = _request(owned, target), new_operation_key()
    first = outputs.OutputsRepository().settle_canceled_obtain(request, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    for identity in first.value.file_ids:
        released = outputs.OutputsRepository().save_retention_release(RetentionRelease(identity, _NOW + 1), new_operation_key(), owned)
        assert released.kind is DbOutcomeKind.COMPLETED, released.error
    before = tuple(owned.connection.iterdump())
    repeated = outputs.OutputsRepository().settle_canceled_obtain(request, key, owned)
    assert repeated.kind is DbOutcomeKind.COMPLETED, repeated.error
    assert repeated.value.file_ids == first.value.file_ids
    assert tuple(owned.connection.iterdump()) == before


async def test_cancel_obtain_original_key_rejects_other_partial_transaction(environment):
    target = await _partial(environment)
    owned = environment[1]
    request, key = _request(owned, target), new_operation_key()
    file_id = owned.connection.execute("SELECT id FROM intermediate_files ORDER BY id LIMIT 1").fetchone()[0]
    checked = outputs.OutputsRepository().save_cleanup_checked(
        outputs.CleanupChecked(file_id, _NOW), key, owned)
    assert checked.kind is DbOutcomeKind.COMPLETED, checked.error
    before = tuple(owned.connection.iterdump())
    rejected = outputs.OutputsRepository().settle_canceled_obtain(request, key, owned)
    assert rejected.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(rejected.error, TransactionError)
    assert tuple(owned.connection.iterdump()) == before


async def test_cancel_obtain_unbuilt_items_end_without_new_delivery_or_attempt(environment):
    from camctl.outputs import obtain_flow as obtain
    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    _accept(owned, "2", [{"name": "取回待建档", "type": "obtain_action_outputs",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"source": {"plan_instance_id": "1", "group": "files"}}}])
    target = owned.connection.execute("SELECT MAX(id) FROM actions WHERE type=4").fetchone()[0]
    runtime = session_obtain_assembly(devices=cfg.devices, drivers=_registry(driver), staging=Path(cfg.paths.staging),
        ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing), occurred_at=lambda: _NOW)(owned)
    obtain._start_due(runtime, _NOW)
    assert obtain._resolve_sources(runtime, obtain._action_facts(owned.connection, target), _NOW)
    obtain._fix_pending_selections(runtime, obtain._action_facts(owned.connection, target), _NOW)
    assert owned.connection.execute("SELECT status,delivery_id FROM obtain_items ORDER BY id").fetchall() == [(2, None), (2, None)]
    _apply(owned, target)
    before_attempts = _attempts(owned)
    saved = outputs.OutputsRepository().settle_canceled_obtain(_request(owned, target), new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    assert saved.value.complete and saved.value.file_ids == ()
    assert owned.connection.execute("SELECT status,delivery_id FROM obtain_items ORDER BY id").fetchall() == [(5, None), (5, None)]
    assert owned.connection.execute("SELECT COUNT(*) FROM deliveries").fetchone() == (0,)
    assert _attempts(owned) == before_attempts
    assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (target,)).fetchone() == (6,)


async def test_cancel_obtain_keeps_original_unfinished_read_and_all_source_protection(environment):
    from camctl.outputs import obtain_flow as obtain
    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    target = await _active_obtain(environment, "2")
    moment = [0]
    runtime = session_obtain_assembly(devices=cfg.devices, drivers=_registry(driver), staging=Path(cfg.paths.staging),
        ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing), occurred_at=lambda: _NOW,
        monotonic_ns=lambda: moment[0])(owned)
    copy_id = owned.connection.execute("SELECT id FROM file_copies ORDER BY id LIMIT 1").fetchone()[0]
    facts = obtain._read_facts(owned.connection, copy_id)
    source_device = owned.connection.execute(
        "SELECT a.device_id FROM device_files f JOIN actions a ON a.id=f.observer_action_id WHERE f.id=?",
        (facts["source_device_file_id"],)).fetchone()[0]
    assembly = runtime.devices[source_device]
    assert obtain._read_wait_remaining(runtime, copy_id, assembly.retry_interval_s, assembly.max_read_attempts) is not None
    moment[0] = 1_000_000_000_000
    assert obtain._read_wait_remaining(runtime, copy_id, assembly.retry_interval_s, assembly.max_read_attempts) is None
    slot = outputs.OutputsRepository().grant_read_slot(SlotRequest(copy_id, _NOW), new_operation_key(), owned)
    assert slot.kind is DbOutcomeKind.COMPLETED, slot.error
    ticket = obtain._begin_read_attempt(runtime, assembly, facts, _NOW)
    assert ticket is not None
    assert owned.connection.execute("SELECT COUNT(*) FROM operation_attempts WHERE status=1").fetchone() == (1,)
    _apply(owned, target)
    request = _request(owned, target)
    before = tuple(owned.connection.iterdump())

    result = outputs.OutputsRepository().settle_canceled_obtain(request, new_operation_key(), owned)

    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert not result.value.complete and result.value.file_ids == ()
    assert tuple(owned.connection.iterdump()) == before
    assert owned.connection.execute("SELECT source_dependency FROM obtain_items ORDER BY id").fetchall() == [(1,), (1,)]


@pytest.mark.parametrize("publication", ["prepared", "moving", "published"])
async def test_cancel_obtain_preserves_actual_publication_boundary(environment, publication):
    from camctl.outputs.obtain_flow import _advance_reads
    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    target = await _active_obtain(environment, "2")
    moment = [0]
    factory = session_obtain_assembly(devices=cfg.devices, drivers=_registry(driver), staging=Path(cfg.paths.staging),
        ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing), segment_size=4,
        occurred_at=lambda: _NOW, monotonic_ns=lambda: moment[0])
    runtime = factory(owned)
    # 先沿原重试等待登记本会话的单调钟起点；随后只推进实际读取，
    # 不调用会先发布其他 PREPARED 成员的动作汇总入口。
    await _advance_reads(runtime, _NOW)
    moment[0] = 1_000_000_000_000
    await _advance_reads(runtime, _NOW)
    assert owned.connection.execute("SELECT status FROM deliveries ORDER BY id").fetchall() == [(3,), (3,)]
    assert len(driver.reads) == 2
    assert owned.connection.execute("SELECT COUNT(*) FROM operation_attempts WHERE status=1").fetchone() == (0,)
    ids = tuple(row[0] for row in owned.connection.execute("SELECT id FROM deliveries ORDER BY id"))
    if publication != "prepared":
        for identity in ids:
            if publication == "published":
                saved = await publish_delivery(identity, DeliveryContext(
                    outputs.OutputsRepository(), owned,
                    DeliveryDirectories(Path(cfg.paths.staging), Path(cfg.paths.ready), Path(cfg.paths.processing)), _NOW))
                assert saved.phase is DeliveryPhase.PUBLISHED, saved
            else:
                intent = outputs.OutputsRepository().save_publication_intent(PublicationIntentRequest(identity, _NOW), new_operation_key(), owned)
                assert intent.kind is DbOutcomeKind.COMPLETED, intent.error
    _apply(owned, target)
    before = tuple(owned.connection.iterdump())
    result = outputs.OutputsRepository().settle_canceled_obtain(_request(owned, target), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    if publication == "moving":
        assert not result.value.complete
        assert tuple(owned.connection.iterdump()) == before
    else:
        assert result.value.complete
        assert owned.connection.execute("SELECT status FROM deliveries ORDER BY id").fetchall() == ([(7,), (7,)] if publication == "prepared" else [(5,), (5,)])
        assert len(result.value.file_ids) == (2 if publication == "prepared" else 0)

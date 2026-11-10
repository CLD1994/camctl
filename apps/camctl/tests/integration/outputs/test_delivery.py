"""普通交付发布的真实组合：意图、移动、同步与完成事实及恢复。

真实 SQLite、真实交接目录与受 F5 交接契约约束的移动共同验证
[普通交付的保存顺序与中断恢复](../../../../architecture/file-handoff.md#普通交付的保存顺序与中断恢复)：
先保存发布意图，事务外原子移动并同步目录，再按实际证据保存完
成事实；主程序提前领取或删除不撤销已确认事实，三处均无且无完
成事实时结束为终局未知失败，不自动重投。
"""

import asyncio
import json
import os
import stat
import sys
from contextlib import closing
from dataclasses import replace

import pytest
import pytest_asyncio

from ..support.outputs import output_operation, prepare_cancel_target
from ..support.output_planning import EventSequence, capture_operation
from ..support.output_storage_cases import observed_operation
from ..support.public_delivery_cases import prepare_public_delivery
from ..bootstrap.test_output_binding_changes import Catalog

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.history.validators import EventValidationError
from camctl.host_files.io import DirectorySyncStage
from camctl.outputs.handoff import (
    DeliveryContext, DeliveryHandoffError,
    DeliveryPhase, IntentDisposition, PublicationIntentRequest, PublicationSaveRequest,
    UnconfirmedFailureSave, publish_delivery,
)
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.repositories.transactions import commit_operation

from .test_copy_segments import _CONTENT
from .test_qualification import _NOW


@pytest_asyncio.fixture
async def prepared_env(tmp_path):
    """已准备完成（PREPARED）的 10 字节设备源交付副本。

    完整拷贝并校验通过后交付进入 PREPARED、源依赖已解除；staging
    工作副本仍在原位，ready 与 processing 为空目录。
    """
    world = await prepare_public_delivery(tmp_path, content=_CONTENT, now=_NOW)
    try:
        yield world
    finally:
        world[0].connection.close()


def _publish(owned, directories, delivery_id, *, occurred_at=_NOW + 10,
             conditions=True):
    return asyncio.run(output_operation(
        owned, publish_delivery, delivery_id, DeliveryContext,
        repository=OutputsRepository(),
        directories=directories,
        occurred_at=occurred_at,
        publication_conditions_met=conditions,
    ))


def _row(owned, sql: str, *params):
    with closing(owned.connection.execute(sql, params)) as cursor:
        return cursor.fetchone()


def _events(owned, event_type: int, reason: int) -> int:
    return _row(
        owned,
        "SELECT COUNT(*) FROM history_events WHERE event_type=?"
        " AND json_extract(body_json, '$.reason')=?",
        event_type, reason)[0]


def _file_name(owned, delivery_id: int) -> str:
    return _row(owned, "SELECT file_name FROM deliveries WHERE id=?",
                delivery_id)[0]


def _staging_copy(roots, owned, target_file_id: int):
    return roots.staging / _row(
        owned, "SELECT relative_path FROM intermediate_files WHERE id=?",
        target_file_id)[0]


def _save_intent(owned, delivery_id: int) -> None:
    outcome = OutputsRepository().save_publication_intent(
        PublicationIntentRequest(
            delivery_id=delivery_id, occurred_at=_NOW + 8),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    assert outcome.value.disposition is IntentDisposition.SAVED


# ---- 正常发布 ----


def test_publish_saves_intent_moves_and_completes(prepared_env):
    owned, roots, directories, qualification = prepared_env
    delivery_id = qualification.delivery_id
    result = _publish(owned, directories, delivery_id)
    assert result.phase is DeliveryPhase.PUBLISHED
    intent_id, published_id, status = _row(
        owned,
        "SELECT publication_intent_event_id, published_event_id, status"
        " FROM deliveries WHERE id=?", delivery_id)
    assert status == 5
    assert _row(
        owned,
        "SELECT json_extract(body_json, '$.reason') FROM history_events"
        " WHERE id=?", intent_id)[0] == 3
    assert _row(
        owned,
        "SELECT json_extract(body_json, '$.reason') FROM history_events"
        " WHERE id=?", published_id)[0] == 4
    file_name = _file_name(owned, delivery_id)
    assert (directories.ready / file_name).read_bytes() == _CONTENT
    assert not _staging_copy(roots, owned, qualification.target_file_id).exists()
    assert not (directories.processing / file_name).exists()
    assert _events(owned, 23, 3) == 1
    assert _events(owned, 23, 4) == 1


def test_hold_when_conditions_unmet(prepared_env):
    """取回整体发布条件未满足：保持原状，不移动不保存意图。"""
    owned, roots, directories, qualification = prepared_env
    result = _publish(owned, directories, qualification.delivery_id,
                      conditions=False)
    assert result.phase is DeliveryPhase.HELD
    assert _row(owned, "SELECT status FROM deliveries WHERE id=?",
                qualification.delivery_id)[0] == 3
    assert _staging_copy(roots, owned, qualification.target_file_id).exists()
    assert _events(owned, 23, 3) == 0


def test_canceled_owner_skips_intent(prepared_env):
    owned, roots, directories, qualification = prepared_env
    prepare_cancel_target(owned, catalog=Catalog(), now=_NOW,
                          action_id=qualification.action_id)
    with pytest.raises(DeliveryHandoffError) as caught:
        _publish(owned, directories, qualification.delivery_id)
    assert caught.value.stage == "owner_canceled"
    assert _row(owned, "SELECT status FROM deliveries WHERE id=?",
                qualification.delivery_id)[0] == 3
    assert _events(owned, 23, 3) == 0
    assert _staging_copy(roots, owned, qualification.target_file_id).exists()


# ---- 发布各边界的失败与恢复 ----


def test_intent_save_unknown_keeps_staging(prepared_env, monkeypatch):
    """意图事务提交未知：不开始移动，恢复后重试成功。"""
    owned, roots, directories, qualification = prepared_env

    def _unknown(self, request, key, owned_connection):
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=RuntimeError("提交未知"))

    monkeypatch.setattr(OutputsRepository, "save_publication_intent", _unknown)
    with pytest.raises(DeliveryHandoffError) as caught:
        _publish(owned, directories, qualification.delivery_id)
    assert caught.value.stage == "intent_save_unknown"
    monkeypatch.undo()
    assert _staging_copy(roots, owned, qualification.target_file_id).exists()
    assert _row(owned, "SELECT status FROM deliveries WHERE id=?",
                qualification.delivery_id)[0] == 3
    assert _publish(
        owned, directories, qualification.delivery_id).phase \
        is DeliveryPhase.PUBLISHED


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 的文件交接不执行目录同步")
def test_sync_failure_keeps_moved_fact_and_recovers(prepared_env, monkeypatch):
    """移动成功但目录同步失败：不保存完成事实；恢复观察 ready 后补存。"""
    owned, roots, directories, qualification = prepared_env
    delivery_id = qualification.delivery_id
    file_name = _file_name(owned, delivery_id)

    original_fsync = os.fsync

    def _broken(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("sync refused")
        return original_fsync(fd)

    monkeypatch.setattr(os, "fsync", _broken)
    with pytest.raises(DeliveryHandoffError) as caught:
        _publish(owned, directories, delivery_id)
    assert caught.value.stage == "publication_sync_failed"
    monkeypatch.undo()
    # 已移动事实保留：文件在 ready，意图已保存，完成事实未保存。
    assert (directories.ready / file_name).read_bytes() == _CONTENT
    assert _row(owned, "SELECT status FROM deliveries WHERE id=?",
                delivery_id)[0] == 4
    assert _events(owned, 23, 4) == 0
    result = _publish(owned, directories, delivery_id)
    assert result.phase is DeliveryPhase.PUBLISHED
    assert _row(owned, "SELECT status FROM deliveries WHERE id=?",
                delivery_id)[0] == 5
    assert _events(owned, 23, 4) == 1


def test_host_claims_to_processing_before_save(prepared_env, monkeypatch):
    """主程序在完成记录前领取到 processing 并删除：按观察保存事实。"""
    owned, roots, directories, qualification = prepared_env
    delivery_id = qualification.delivery_id
    file_name = _file_name(owned, delivery_id)
    claimed = {"done": False}

    def _claim_and_unknown(self, request, key, owned_connection):
        if not claimed["done"]:
            claimed["done"] = True
            os.replace(
                directories.ready / file_name,
                directories.processing / file_name)
            return DbOutcome(
                kind=DbOutcomeKind.UNKNOWN, error=RuntimeError("提交未知"))
        return DbOutcome(kind=DbOutcomeKind.COMPLETED, value=None)

    monkeypatch.setattr(
        OutputsRepository, "save_publication", _claim_and_unknown)
    with pytest.raises(DeliveryHandoffError) as caught:
        _publish(owned, directories, delivery_id)
    assert caught.value.stage == "publication_save_unknown"
    monkeypatch.undo()
    assert (directories.processing / file_name).read_bytes() == _CONTENT
    # 恢复观察 processing 中的副本，保存本地交付事实，不重新投放。
    result = _publish(owned, directories, delivery_id)
    assert result.phase is DeliveryPhase.PUBLISHED
    assert _row(owned, "SELECT status FROM deliveries WHERE id=?",
                delivery_id)[0] == 5
    assert not _staging_copy(roots, owned, qualification.target_file_id).exists()


def test_move_not_moved_reobserves_and_confirms(prepared_env, monkeypatch):
    """目标在竞争期间出现时重新观察同一完整副本。"""
    owned, roots, directories, qualification = prepared_env
    delivery_id = qualification.delivery_id
    file_name = _file_name(owned, delivery_id)
    from camctl.host_files.handoff import PublishResult, PublishStage
    from camctl.outputs import handoff as handoff_rules

    async def _conflict(ref, roots_arg, directories_arg, target):
        os.link(_staging_copy(roots, owned, qualification.target_file_id),
                directories.ready / file_name)
        return PublishResult(
            stage=PublishStage.NOT_MOVED,
            directory=DirectorySyncStage.NOT_ATTEMPTED,
            source_removed=False, error="target_exists")

    monkeypatch.setattr(handoff_rules, "publish_file", _conflict)
    result = _publish(owned, directories, delivery_id)
    monkeypatch.undo()
    assert result.phase is DeliveryPhase.PUBLISHED
    assert (directories.ready / file_name).read_bytes() == _CONTENT
    assert _staging_copy(roots, owned, qualification.target_file_id).exists()
    assert _row(owned, "SELECT status FROM deliveries WHERE id=?",
                delivery_id)[0] == 5


def test_existing_ready_file_is_not_overwritten(prepared_env):
    """ready 已有同名文件：不覆盖、不移动，按观察确认同一完整副本。"""
    owned, roots, directories, qualification = prepared_env
    delivery_id = qualification.delivery_id
    file_name = _file_name(owned, delivery_id)
    (directories.ready / file_name).write_bytes(_CONTENT)
    result = _publish(owned, directories, delivery_id)
    assert result.phase is DeliveryPhase.PUBLISHED
    assert _row(owned, "SELECT status FROM deliveries WHERE id=?",
                delivery_id)[0] == 5
    # staging 副本不投放，交由后续清理责任处理。
    assert _staging_copy(roots, owned, qualification.target_file_id).exists()


def test_publishing_recovery_continues_identity(prepared_env):
    """意图已保存、副本仍在 staging：继续原 delivery，不分配新身份。"""
    owned, roots, directories, qualification = prepared_env
    delivery_id = qualification.delivery_id
    _save_intent(owned, delivery_id)
    deliveries_before = _row(owned, "SELECT COUNT(*) FROM deliveries")[0]
    copies_before = _row(owned, "SELECT COUNT(*) FROM file_copies")[0]
    file_name = _file_name(owned, delivery_id)
    result = _publish(owned, directories, delivery_id)
    assert result.phase is DeliveryPhase.PUBLISHED
    assert _row(owned, "SELECT COUNT(*) FROM deliveries")[0] == deliveries_before
    assert _row(owned, "SELECT COUNT(*) FROM file_copies")[0] == copies_before
    assert _file_name(owned, delivery_id) == file_name
    assert (directories.ready / file_name).read_bytes() == _CONTENT


def test_observed_delivery_after_cancel_still_saves(prepared_env):
    """副本已进 ready 后发起责任取消：确认已发生事实不受取消影响。"""
    owned, roots, directories, qualification = prepared_env
    delivery_id = qualification.delivery_id
    _save_intent(owned, delivery_id)
    file_name = _file_name(owned, delivery_id)
    os.replace(
        _staging_copy(roots, owned, qualification.target_file_id),
        directories.ready / file_name)
    prepare_cancel_target(owned, catalog=Catalog(), now=_NOW,
                          action_id=qualification.action_id)
    result = _publish(owned, directories, delivery_id)
    assert result.phase is DeliveryPhase.PUBLISHED
    assert _row(owned, "SELECT status FROM deliveries WHERE id=?",
                delivery_id)[0] == 5


def test_publication_from_prepared_backfills_intent(prepared_env):
    """副本已在交接位置而意图未保存：同一事务补存意图与完成事实。"""
    owned, roots, directories, qualification = prepared_env
    delivery_id = qualification.delivery_id
    file_name = _file_name(owned, delivery_id)
    os.replace(
        _staging_copy(roots, owned, qualification.target_file_id),
        directories.processing / file_name)
    result = _publish(owned, directories, delivery_id)
    assert result.phase is DeliveryPhase.PUBLISHED
    assert _row(owned, "SELECT status FROM deliveries WHERE id=?",
                delivery_id)[0] == 5
    assert _events(owned, 23, 3) == 1
    assert _events(owned, 23, 4) == 1


# ---- 终局未知失败与终态保持 ----


def test_missing_all_copies_saves_final_unknown_failure(prepared_env):
    """三处均无且无完成事实：终局失败，不自动重投，终态不复活。"""
    owned, roots, directories, qualification = prepared_env
    delivery_id = qualification.delivery_id
    _save_intent(owned, delivery_id)
    _staging_copy(roots, owned, qualification.target_file_id).unlink()
    result = _publish(owned, directories, delivery_id)
    assert result.phase is DeliveryPhase.FAILED_FINAL
    assert result.decision.republishes == 0
    status, error_json = _row(
        owned, "SELECT status, error_json FROM deliveries WHERE id=?",
        delivery_id)
    assert status == 6
    error = json.loads(error_json)
    assert error["code"] == "delivery_handoff_unconfirmed"
    assert error["stage"] == "publication"
    assert error["details"] == {"delivery_id": str(delivery_id)}
    assert _events(owned, 23, 5) == 1
    again = _publish(owned, directories, delivery_id)
    assert again.phase is DeliveryPhase.NOT_ACTIVE
    assert _events(owned, 23, 5) == 1


def test_ready_observation_failure_is_not_missing(prepared_env, monkeypatch):
    """交接位置观察失败不套用三处均无分支。"""
    owned, roots, directories, qualification = prepared_env
    delivery_id = qualification.delivery_id
    _save_intent(owned, delivery_id)
    _staging_copy(roots, owned, qualification.target_file_id).unlink()

    original_stat = os.stat
    target = directories.ready / _file_name(owned, delivery_id)

    def _broken(path, *args, **kwargs):
        if os.fspath(path) == os.fspath(target):
            raise OSError("busy")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", _broken)
    with pytest.raises(DeliveryHandoffError) as caught:
        _publish(owned, directories, delivery_id)
    assert caught.value.stage == "handoff_undecidable"
    assert _row(owned, "SELECT status FROM deliveries WHERE id=?",
                delivery_id)[0] == 4
    assert _events(owned, 23, 5) == 0


def test_saved_publication_survives_file_removal(prepared_env):
    """已保存的完成事实不因交接文件消失而撤销。"""
    owned, roots, directories, qualification = prepared_env
    delivery_id = qualification.delivery_id
    assert _publish(owned, directories, delivery_id).phase \
        is DeliveryPhase.PUBLISHED
    (directories.ready / _file_name(owned, delivery_id)).unlink()
    again = _publish(owned, directories, delivery_id)
    assert again.phase is DeliveryPhase.PUBLISHED
    assert _events(owned, 23, 4) == 1
    assert _events(owned, 23, 5) == 0


def test_intent_rejected_when_delivery_not_prepared(prepared_env):
    owned, roots, directories, qualification = prepared_env
    owned.connection.execute(
        "UPDATE deliveries SET status=2 WHERE id=?",
        (qualification.delivery_id,))
    owned.connection.commit()
    outcome = OutputsRepository().save_publication_intent(
        PublicationIntentRequest(
            delivery_id=qualification.delivery_id, occurred_at=_NOW + 8),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert "准备完成" in str(outcome.error)


def test_unconfirmed_rejects_published_delivery(prepared_env):
    owned, roots, directories, qualification = prepared_env
    delivery_id = qualification.delivery_id
    assert _publish(owned, directories, delivery_id).phase \
        is DeliveryPhase.PUBLISHED
    from camctl.outputs.handoff import DeliveryFailure
    outcome = OutputsRepository().save_unconfirmed_failure(
        UnconfirmedFailureSave(
            delivery_id=delivery_id,
            failure=DeliveryFailure(
                code="delivery_handoff_unconfirmed", stage="publication",
                details={"delivery_id": str(delivery_id)}),
            occurred_at=_NOW + 12),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert "完成事实" in str(outcome.error)


# ---- 原键恢复 ----


def test_intent_key_recovers_first_response(prepared_env):
    owned, roots, directories, qualification = prepared_env
    repository = OutputsRepository()
    key = new_operation_key()
    request = PublicationIntentRequest(
        delivery_id=qualification.delivery_id, occurred_at=_NOW + 8)
    first = repository.save_publication_intent(request, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    before = tuple(owned.connection.iterdump())
    again = repository.save_publication_intent(request, key, owned)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert tuple(owned.connection.iterdump()) == before
    changed = repository.save_publication_intent(
        replace(request, occurred_at=_NOW + 9), key, owned)
    assert changed.kind is DbOutcomeKind.ROLLED_BACK


def test_publication_key_recovers_first_response(prepared_env):
    owned, roots, directories, qualification = prepared_env
    repository = OutputsRepository()
    _save_intent(owned, qualification.delivery_id)
    key = new_operation_key()
    request = PublicationSaveRequest(
        delivery_id=qualification.delivery_id, occurred_at=_NOW + 9)
    first = repository.save_publication(request, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    before = tuple(owned.connection.iterdump())
    again = repository.save_publication(request, key, owned)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert tuple(owned.connection.iterdump()) == before
    changed = repository.save_publication(
        replace(request, occurred_at=_NOW + 10), key, owned)
    assert changed.kind is DbOutcomeKind.ROLLED_BACK


def test_publication_backfill_key_recovers_first_response(prepared_env):
    """从 PREPARED 补意图并保存完成的原键恢复。"""
    owned, roots, directories, qualification = prepared_env
    delivery_id = qualification.delivery_id
    os.replace(
        _staging_copy(roots, owned, qualification.target_file_id),
        directories.processing / _file_name(owned, delivery_id))
    repository = OutputsRepository()
    key = new_operation_key()
    request = PublicationSaveRequest(
        delivery_id=delivery_id, occurred_at=_NOW + 9)
    first = repository.save_publication(request, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    before = tuple(owned.connection.iterdump())
    again = repository.save_publication(request, key, owned)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert tuple(owned.connection.iterdump()) == before
    changed = repository.save_publication(
        replace(request, occurred_at=_NOW + 10), key, owned)
    assert changed.kind is DbOutcomeKind.ROLLED_BACK


def test_unconfirmed_key_recovers_first_response(prepared_env):
    owned, roots, directories, qualification = prepared_env
    delivery_id = qualification.delivery_id
    _save_intent(owned, delivery_id)
    _staging_copy(roots, owned, qualification.target_file_id).unlink()
    repository = OutputsRepository()
    from camctl.outputs.handoff import DeliveryFailure
    key = new_operation_key()
    request = UnconfirmedFailureSave(
        delivery_id=delivery_id,
        failure=DeliveryFailure(
            code="delivery_handoff_unconfirmed", stage="publication",
            details={"delivery_id": str(delivery_id)}),
        occurred_at=_NOW + 11)
    first = repository.save_unconfirmed_failure(request, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    before = tuple(owned.connection.iterdump())
    again = repository.save_unconfirmed_failure(request, key, owned)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert tuple(owned.connection.iterdump()) == before
    changed = repository.save_unconfirmed_failure(
        replace(request, occurred_at=_NOW + 12), key, owned)
    assert changed.kind is DbOutcomeKind.ROLLED_BACK


# ---- 正式守卫 ----


def _proposal(owned, method, request):
    key = new_operation_key()
    return capture_operation(
        owned, lambda actual: getattr(OutputsRepository(), method)(request, key, actual))


def _validate(owned, plan, events=None, state_rows=None):
    """由真实事务内核绑定读取事实，在独立副本验证事件组。"""
    proposal = plan if state_rows is None else replace(plan, state_rows=state_rows)
    sequence = EventSequence(plan.events if events is None else events, proposal)
    receipt = observed_operation(
        owned, lambda actual: commit_operation(sequence, new_operation_key(), actual))
    if receipt.kind != "completed":
        raise receipt.error


def test_guard_accepts_real_intent_and_publish_events(prepared_env):
    owned, roots, directories, qualification = prepared_env
    intent = _proposal(owned, "save_publication_intent",
        PublicationIntentRequest(
            delivery_id=qualification.delivery_id, occurred_at=_NOW + 8))
    _validate(owned, intent)
    _save_intent(owned, qualification.delivery_id)
    publication = _proposal(owned, "save_publication",
        PublicationSaveRequest(
            delivery_id=qualification.delivery_id, occurred_at=_NOW + 9))
    _validate(owned, publication)


def test_guard_accepts_backfill_publication_events(prepared_env):
    """从 PREPARED 补意图并保存完成的真实组合通过正式守卫。"""
    owned, roots, directories, qualification = prepared_env
    plan = _proposal(owned, "save_publication",
        PublicationSaveRequest(
            delivery_id=qualification.delivery_id, occurred_at=_NOW + 9))
    reasons = [event.reason for event in plan.events]
    # 意图、完成与目标文件交接（HANDED_OFF）在同一事务共同保存。
    assert reasons == [3, 4, 2]
    _validate(owned, plan)


def test_guard_rejects_intent_referencing_other_event(prepared_env):
    owned, roots, directories, qualification = prepared_env
    plan = _proposal(owned, "save_publication_intent",
        PublicationIntentRequest(
            delivery_id=qualification.delivery_id, occurred_at=_NOW + 8))
    event = plan.events[0]
    row = event.rows[0]
    variant = replace(
        row, after=replace(row.after, values={
            **row.after.values,
            "publication_intent_event_id": event.event_id + 5}))
    with pytest.raises(EventValidationError, match="发布意图必须"):
        _validate(owned, plan, events=(replace(event, rows=(variant,)),))


def test_guard_rejects_publish_referencing_other_event(prepared_env):
    owned, roots, directories, qualification = prepared_env
    _save_intent(owned, qualification.delivery_id)
    plan = _proposal(owned, "save_publication",
        PublicationSaveRequest(
            delivery_id=qualification.delivery_id, occurred_at=_NOW + 9))
    event = plan.events[0]
    row = event.rows[0]
    variant = replace(
        row, after=replace(row.after, values={
            **row.after.values,
            "published_event_id": event.event_id + 5}))
    with pytest.raises(EventValidationError, match="交接完成必须"):
        _validate(owned, plan, events=(replace(event, rows=(variant,)),))


def test_guard_rejects_fail_with_unregistered_code(prepared_env):
    owned, roots, directories, qualification = prepared_env
    delivery_id = qualification.delivery_id
    from camctl.outputs.handoff import DeliveryFailure
    plan = _proposal(owned, "save_unconfirmed_failure",
        UnconfirmedFailureSave(
            delivery_id=delivery_id,
            failure=DeliveryFailure(
                code="delivery_handoff_unconfirmed", stage="publication",
                details={"delivery_id": str(delivery_id)}),
            occurred_at=_NOW + 11))
    event = plan.events[0]
    row = event.rows[0]
    variant = replace(
        row, after=replace(row.after, values={
            **row.after.values,
            "error_json": {
                "code": "not_a_registered_code", "stage": "publication",
                "details": {"delivery_id": str(delivery_id)},
            }}))
    with pytest.raises(EventValidationError, match="公共错误登记"):
        _validate(owned, plan, events=(replace(event, rows=(variant,)),))


def test_guard_rejects_publish_without_verified_copy(prepared_env):
    """副本校验未完成时保存发布事实被正式守卫拒绝。"""
    owned, roots, directories, qualification = prepared_env
    _save_intent(owned, qualification.delivery_id)
    plan = _proposal(owned, "save_publication",
        PublicationSaveRequest(
            delivery_id=qualification.delivery_id, occurred_at=_NOW + 9))
    tampered = {
        table: {row_id: dict(rows) for row_id, rows in rows.items()}
        for table, rows in plan.state_rows.items()
    }
    tampered["file_copies"][qualification.copy_id]["verification_state"] = 1
    with pytest.raises(EventValidationError, match="副本未完成字节校验"):
        _validate(owned, plan, state_rows=tampered)

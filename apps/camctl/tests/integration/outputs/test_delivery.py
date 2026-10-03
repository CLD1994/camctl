"""普通交付发布的真实组合：意图、移动、同步与完成事实及恢复。

真实 SQLite、真实交接目录与受 F5 交接契约约束的移动共同验证
[普通交付的保存顺序与中断恢复](../../../../architecture/file-handoff.md#普通交付的保存顺序与中断恢复)：
先保存发布意图，事务外原子移动并同步目录，再按实际证据保存完
成事实；主程序提前领取或删除不撤销已确认事实，三处均无且无完
成事实时结束为终局未知失败，不自动重投。
"""

import asyncio
import hashlib
import json
import os
import shutil
from contextlib import closing
from dataclasses import replace

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.history.validators import EventContext, EventValidationError, validate_event
from camctl.host_files.io import DirectorySyncStage
from camctl.host_files.models import BoundDirectories
from camctl.outputs.copy import CompletionContext, CompletionPhase, complete_copy
from camctl.outputs.handoff import (
    DeliveryContext, DeliveryDirectories, DeliveryHandoffError,
    DeliveryPhase, PublicationIntentRequest, PublicationSaveRequest,
    UnconfirmedFailureSave, publish_delivery,
)
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories import outputs
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.transaction import TransactionScope

from .test_copy_resume import _command, _qualified
from .test_copy_segments import _CONTENT, _drive
from .test_local_read import local_read  # noqa: F401
from .test_qualification import _NOW
from .test_read_associations import read_targets  # noqa: F401

register_capture_guards()

_CONTENT_DIGEST = hashlib.sha256(_CONTENT).hexdigest()


@pytest.fixture
def prepared_env(read_targets, tmp_path):
    """已准备完成（PREPARED）的 10 字节设备源交付副本。

    完整拷贝并校验通过后交付进入 PREPARED、源依赖已解除；staging
    工作副本仍在原位，ready 与 processing 为空目录。
    """
    owned = read_targets
    owned.connection.execute("UPDATE actions SET status=3 WHERE id=11")
    owned.connection.execute(
        "UPDATE device_files SET size_bytes=10, checksum_support=2, sha256=?"
        " WHERE id=501", (_CONTENT_DIGEST,))
    owned.connection.commit()
    qualification = _qualified(owned, _command())
    staging = tmp_path / "staging"
    for name in ("deliveries", "recording-inputs", "derived"):
        (staging / name).mkdir(parents=True)
    ready = tmp_path / "ready"
    ready.mkdir()
    processing = tmp_path / "processing"
    processing.mkdir()
    roots = BoundDirectories(staging=staging)
    asyncio.run(_drive(owned, roots, qualification.copy_id, segment_size=10))
    step = asyncio.run(complete_copy(qualification.copy_id, CompletionContext(
        repository=OutputsRepository(), owned=owned, roots=roots,
        occurred_at=_NOW + 5)))
    assert step.phase is CompletionPhase.PREPARED
    directories = DeliveryDirectories(
        staging=staging, ready=ready, processing=processing)
    return owned, roots, directories, qualification


def _publish(owned, directories, delivery_id, *, occurred_at=_NOW + 10,
             conditions=True):
    return asyncio.run(publish_delivery(
        delivery_id,
        DeliveryContext(
            repository=OutputsRepository(), owned=owned,
            directories=directories, occurred_at=occurred_at,
            publication_conditions_met=conditions),
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
    owned.connection.execute(
        "UPDATE actions SET cancel_requested=1 WHERE id=31")
    owned.connection.commit()
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


def test_sync_failure_keeps_moved_fact_and_recovers(prepared_env, monkeypatch):
    """移动成功但目录同步失败：不保存完成事实；恢复观察 ready 后补存。"""
    owned, roots, directories, qualification = prepared_env
    delivery_id = qualification.delivery_id
    file_name = _file_name(owned, delivery_id)

    def _broken(path):
        raise OSError("sync refused")

    from camctl.host_files import handoff as handoff_module
    if not handoff_module._DIRECTORY_SYNC_SUPPORTED:
        monkeypatch.setattr(
            handoff_module, "_DIRECTORY_SYNC_SUPPORTED", True)
    monkeypatch.setattr(handoff_module, "_sync_directory", _broken)
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
    """移动被拒绝后重新观察：副本已被第三方放到 ready 时确认交付。"""
    owned, roots, directories, qualification = prepared_env
    delivery_id = qualification.delivery_id
    file_name = _file_name(owned, delivery_id)
    from camctl.host_files.handoff import PublishResult, PublishStage
    from camctl.outputs import handoff as handoff_rules

    async def _conflict(ref, roots_arg, directories_arg, target):
        shutil.move(
            str(_staging_copy(roots, owned, qualification.target_file_id)),
            str(directories.ready / file_name))
        return PublishResult(
            stage=PublishStage.NOT_MOVED,
            directory=DirectorySyncStage.NOT_ATTEMPTED,
            source_removed=False, error="target_exists")

    monkeypatch.setattr(handoff_rules, "publish_file", _conflict)
    result = _publish(owned, directories, delivery_id)
    monkeypatch.undo()
    assert result.phase is DeliveryPhase.PUBLISHED
    assert (directories.ready / file_name).read_bytes() == _CONTENT
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
    owned.connection.execute(
        "UPDATE actions SET cancel_requested=1 WHERE id=31")
    owned.connection.commit()
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

    def _broken(path):
        from camctl.outputs.handoff import LocationObservation
        return LocationObservation(observed=False, error=OSError("busy"))

    from camctl.outputs import handoff as handoff_rules
    monkeypatch.setattr(handoff_rules, "_digest_location", _broken)
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


def _proposal(owned, command):
    owned.connection.execute("BEGIN")
    try:
        plan = command.plan(TransactionScope(owned.connection, 1, 1))
    finally:
        owned.connection.rollback()
    assert plan.events, "提案必须产生事件"
    return plan


def _validate(plan, events=None, state_rows=None):
    """模仿内核按顺序分配报告变化序号并逐事件校验。"""
    from camctl.history.changes import event_report_targets

    working = {
        table: {row_id: dict(rows) for row_id, rows in table_rows.items()}
        for table, table_rows in plan.state_rows.items()
    }
    chosen = events if events is not None else plan.events
    seq = 0
    for event in chosen:
        if event_report_targets(event, working):
            seq += 1
            event = replace(event, change_seq=seq)
        context = EventContext(
            TransactionRange(
                chosen[0].transaction_id, chosen[0].event_id, chosen[-1].event_id),
            dict(plan.owners),
            state_rows if state_rows is not None else plan.state_rows,
            read_coverage=plan.read_coverage)
        validate_event(event, context)
        for row in event.rows:
            if row.after.exists:
                working.setdefault(row.table, {}).setdefault(
                    row.row_id, {}).update(row.after.values)


def test_guard_accepts_real_intent_and_publish_events(prepared_env):
    owned, roots, directories, qualification = prepared_env
    intent = _proposal(owned, outputs._PublicationIntentCommand(
        PublicationIntentRequest(
            delivery_id=qualification.delivery_id, occurred_at=_NOW + 8),
        new_operation_key()))
    _validate(intent)
    _save_intent(owned, qualification.delivery_id)
    publication = _proposal(owned, outputs._PublicationSaveCommand(
        PublicationSaveRequest(
            delivery_id=qualification.delivery_id, occurred_at=_NOW + 9),
        new_operation_key()))
    _validate(publication)


def test_guard_accepts_backfill_publication_events(prepared_env):
    """从 PREPARED 补意图并保存完成的真实组合通过正式守卫。"""
    owned, roots, directories, qualification = prepared_env
    plan = _proposal(owned, outputs._PublicationSaveCommand(
        PublicationSaveRequest(
            delivery_id=qualification.delivery_id, occurred_at=_NOW + 9),
        new_operation_key()))
    reasons = [event.reason for event in plan.events]
    assert reasons == [3, 4]
    _validate(plan)


def test_guard_rejects_intent_referencing_other_event(prepared_env):
    owned, roots, directories, qualification = prepared_env
    plan = _proposal(owned, outputs._PublicationIntentCommand(
        PublicationIntentRequest(
            delivery_id=qualification.delivery_id, occurred_at=_NOW + 8),
        new_operation_key()))
    event = plan.events[0]
    row = event.rows[0]
    variant = replace(
        row, after=replace(row.after, values={
            **row.after.values,
            "publication_intent_event_id": event.event_id + 5}))
    with pytest.raises(EventValidationError):
        _validate(plan, events=(replace(event, rows=(variant,)),))


def test_guard_rejects_publish_referencing_other_event(prepared_env):
    owned, roots, directories, qualification = prepared_env
    _save_intent(owned, qualification.delivery_id)
    plan = _proposal(owned, outputs._PublicationSaveCommand(
        PublicationSaveRequest(
            delivery_id=qualification.delivery_id, occurred_at=_NOW + 9),
        new_operation_key()))
    event = plan.events[0]
    row = event.rows[0]
    variant = replace(
        row, after=replace(row.after, values={
            **row.after.values,
            "published_event_id": event.event_id + 5}))
    with pytest.raises(EventValidationError):
        _validate(plan, events=(replace(event, rows=(variant,)),))


def test_guard_rejects_fail_with_unregistered_code(prepared_env):
    owned, roots, directories, qualification = prepared_env
    delivery_id = qualification.delivery_id
    from camctl.outputs.handoff import DeliveryFailure
    plan = _proposal(owned, outputs._UnconfirmedFailureCommand(
        UnconfirmedFailureSave(
            delivery_id=delivery_id,
            failure=DeliveryFailure(
                code="delivery_handoff_unconfirmed", stage="publication",
                details={"delivery_id": str(delivery_id)}),
            occurred_at=_NOW + 11),
        new_operation_key()))
    event = plan.events[0]
    row = event.rows[0]
    variant = replace(
        row, after=replace(row.after, values={
            **row.after.values,
            "error_json": {
                "code": "not_a_registered_code", "stage": "publication",
                "details": {"delivery_id": str(delivery_id)},
            }}))
    with pytest.raises(EventValidationError):
        _validate(plan, events=(replace(event, rows=(variant,)),))


def test_guard_rejects_publish_without_verified_copy(prepared_env):
    """副本校验未完成时保存发布事实被正式守卫拒绝。"""
    owned, roots, directories, qualification = prepared_env
    _save_intent(owned, qualification.delivery_id)
    plan = _proposal(owned, outputs._PublicationSaveCommand(
        PublicationSaveRequest(
            delivery_id=qualification.delivery_id, occurred_at=_NOW + 9),
        new_operation_key()))
    tampered = {
        table: {row_id: dict(rows) for row_id, rows in rows.items()}
        for table, rows in plan.state_rows.items()
    }
    tampered["file_copies"][qualification.copy_id]["verification_state"] = 1
    with pytest.raises(EventValidationError):
        _validate(plan, state_rows=tampered)

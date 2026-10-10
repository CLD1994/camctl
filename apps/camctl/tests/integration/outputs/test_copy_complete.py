"""完整性收尾的真实组合：校验、有限重拷与准备完成解除源依赖。

真实 SQLite、真实文件与受源摘要契约约束的替身共同验证
[读取正确性与文件校验](../../../../architecture/file-copy.md#读取正确性与文件校验)、
[摘要不一致后的有限重拷](../../../../architecture/file-copy.md#摘要不一致后的有限重拷)
及[准备完成与断电恢复](../../../../architecture/output-cleanup.md#准备完成与断电恢复)：
主机摘要必须计算，摘要不一致先提交轮次消耗和进度归零再截断
重建，准备完成与解除源依赖共同保存。
"""

import asyncio
import hashlib
from contextlib import closing
from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.contracts.values import new_operation_key
from camctl.history.validators import EventContext, EventValidationError, validate_event
from camctl.operations.attempts import (
    AttemptConfig, AttemptIntent, AttemptTarget, BeginDisposition, OperationKind,
)
from camctl.outputs.copy import (
    CompletionContext, CompletionPhase, CopyCompletionError, CopyContext,
    PreparedRequest, RecopyDisposition, RecopyRegistration, SourceDigest,
    VerificationSave, complete_copy, prepare_copy,
)
from camctl.outputs.slots import SlotRequest
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories import outputs
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.transaction import TransactionScope

from .test_copy_resume import _command, _make_roots, _qualified, _seed_failed_attempts
from .test_copy_segments import _CONTENT, _drive, _target_path
from .test_local_read import local_read  # noqa: F401
from .test_qualification import _NOW
from .test_read_associations import read_targets  # noqa: F401

register_capture_guards()

_CONTENT_DIGEST = hashlib.sha256(_CONTENT).hexdigest()
_WRONG_DIGEST = "b" * 64


class _FixedDigest:
    """受源摘要端口契约约束的替身：返回固定获取结果。"""

    def __init__(self, digest: str | None = None, error: object | None = None) -> None:
        self._result = SourceDigest(digest=digest, error=error)
        self.calls = 0

    async def read_digest(self) -> SourceDigest:
        self.calls += 1
        return self._result


def _complete(owned, roots, copy_id, *, digest=None, max_recopies=1):
    context = CompletionContext(
        repository=OutputsRepository(), owned=owned, roots=roots,
        occurred_at=_NOW + 5, digest=digest, max_recopies=max_recopies,
    )
    return asyncio.run(complete_copy(copy_id, context))


def _row(owned, sql: str, *params):
    with closing(owned.connection.execute(sql, params)) as cursor:
        return cursor.fetchone()


def _events(owned, event_type: int, reason: int) -> int:
    return _row(
        owned,
        "SELECT COUNT(*) FROM history_events WHERE event_type=?"
        " AND json_extract(body_json, '$.reason')=?",
        event_type, reason)[0]


def _item_id(owned, delivery_id: int) -> int:
    return _row(owned, "SELECT id FROM obtain_items WHERE delivery_id=?",
                delivery_id)[0]


def _owner_action_id(owned, copy_id: int) -> int:
    return _row(
        owned,
        "SELECT d.action_id FROM deliveries d JOIN file_copies c"
        " ON c.delivery_id = d.id WHERE c.id=?", copy_id)[0]


@pytest.fixture
def complete_env(read_targets, tmp_path):
    """已全部可靠保存的 10 字节设备源交付拷贝。

    建档时设备源已支持并携带正确摘要，拷贝记录复用该源摘要；
    各用例按分区需要改写源能力或已保存源摘要。
    """
    owned = read_targets
    owned.connection.execute("UPDATE actions SET status=3 WHERE id=11")
    owned.connection.execute("UPDATE device_files SET size_bytes=10, checksum_support=2"
                             " WHERE id=501")
    owned.connection.execute(
        "UPDATE device_files SET sha256=? WHERE id=501", (_CONTENT_DIGEST,))
    owned.connection.commit()
    qualification = _qualified(owned, _command())
    roots = _make_roots(tmp_path)
    asyncio.run(_drive(owned, roots, qualification.copy_id, segment_size=10))
    return owned, roots, qualification


@pytest.fixture
def internal_complete_env(read_targets, tmp_path):
    """已全部可靠保存的录像内部输入拷贝，同样复用正确源摘要。"""
    owned = read_targets
    owned.connection.execute("DELETE FROM obtain_items")
    owned.connection.execute("DELETE FROM outputs")
    owned.connection.execute("UPDATE device_files SET size_bytes=10, checksum_support=2"
                             " WHERE id=501")
    owned.connection.execute(
        "UPDATE device_files SET sha256=? WHERE id=501", (_CONTENT_DIGEST,))
    owned.connection.commit()
    qualification = _qualified(owned, _command(internal=True))
    roots = _make_roots(tmp_path)
    asyncio.run(_drive(owned, roots, qualification.copy_id, segment_size=10))
    return owned, roots, qualification


# ---- 校验通过与准备完成 ----


def test_matched_copy_prepares_and_releases_source(complete_env):
    owned, roots, qualification = complete_env
    step = _complete(owned, roots, qualification.copy_id)
    assert step.phase is CompletionPhase.PREPARED
    assert step.prepared.source_dependency_released is True
    assert step.prepared.sha256 == _CONTENT_DIGEST
    assert _row(owned, "SELECT verification_state, source_sha256, target_sha256"
        " FROM file_copies WHERE id=?", qualification.copy_id) == (
        3, _CONTENT_DIGEST, _CONTENT_DIGEST)
    assert _row(owned, "SELECT status FROM deliveries WHERE id=?",
        qualification.delivery_id)[0] == 3
    assert _row(owned, "SELECT source_dependency FROM obtain_items WHERE id=?",
        _item_id(owned, qualification.delivery_id))[0] == 0
    assert _row(owned, "SELECT size_bytes, sha256 FROM intermediate_files WHERE id=?",
        qualification.target_file_id) == (10, _CONTENT_DIGEST)
    assert _target_path(roots, owned, qualification.target_file_id).read_bytes() == _CONTENT
    assert _events(owned, 22, 3) == 1
    assert _events(owned, 23, 2) == 1
    assert _events(owned, 21, 3) == 1
    assert _events(owned, 26, 2) == 1


def test_prepared_commit_releases_source(complete_env, monkeypatch):
    """完整性及同步成功但准备事务尚未确认：源依赖保持，确认后解除。"""
    owned, roots, qualification = complete_env

    def _unknown(self, request, key, owned_connection):
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=RuntimeError("提交未知"))

    monkeypatch.setattr(OutputsRepository, "save_prepared", _unknown)
    with pytest.raises(CopyCompletionError) as caught:
        _complete(owned, roots, qualification.copy_id)
    assert caught.value.stage == "prepared_save_unknown"
    monkeypatch.undo()
    # 提交结果未知不冒充完成：源依赖与交付状态保持未解除事实。
    assert _row(owned, "SELECT source_dependency FROM obtain_items WHERE id=?",
        _item_id(owned, qualification.delivery_id))[0] == 1
    assert _row(owned, "SELECT status FROM deliveries WHERE id=?",
        qualification.delivery_id)[0] == 1
    step = _complete(owned, roots, qualification.copy_id)
    assert step.phase is CompletionPhase.PREPARED
    assert step.prepared.source_dependency_released is True
    assert _row(owned, "SELECT source_dependency FROM obtain_items WHERE id=?",
        _item_id(owned, qualification.delivery_id))[0] == 0


def test_unsupported_source_completes_without_checksum(complete_env):
    """明确不支持源端摘要：按可靠读取降级完成，主机摘要仍保存。"""
    owned, roots, qualification = complete_env
    owned.connection.execute("UPDATE device_files SET checksum_support=3, sha256=NULL"
                             " WHERE id=501")
    owned.connection.execute(
        "UPDATE file_copies SET source_sha256=NULL WHERE id=?", (qualification.copy_id,))
    owned.connection.commit()
    step = _complete(owned, roots, qualification.copy_id)
    assert step.phase is CompletionPhase.PREPARED
    assert _row(owned, "SELECT verification_state, source_sha256, target_sha256"
        " FROM file_copies WHERE id=?", qualification.copy_id) == (5, None, _CONTENT_DIGEST)
    assert _row(owned, "SELECT source_dependency FROM obtain_items WHERE id=?",
        _item_id(owned, qualification.delivery_id))[0] == 0


def test_undetermined_capability_blocks_completion(complete_env):
    """能力未知不折叠为不支持：收尾停止，不保存校验事实。"""
    owned, roots, qualification = complete_env
    owned.connection.execute("UPDATE device_files SET checksum_support=1, sha256=NULL"
                             " WHERE id=501")
    owned.connection.execute(
        "UPDATE file_copies SET source_sha256=NULL WHERE id=?", (qualification.copy_id,))
    owned.connection.commit()
    with pytest.raises(CopyCompletionError) as caught:
        _complete(owned, roots, qualification.copy_id,
                  digest=_FixedDigest(_CONTENT_DIGEST))
    assert caught.value.stage == "checksum_support_undetermined"
    assert _row(owned, "SELECT verification_state FROM file_copies WHERE id=?",
        qualification.copy_id)[0] == 1
    assert _events(owned, 22, 3) == 0


def test_source_digest_failure_records_failed_verification(complete_env):
    """支持源端校验但获取失败：保存失败诊断，不降级、不准备完成。"""
    owned, roots, qualification = complete_env
    owned.connection.execute(
        "UPDATE file_copies SET source_sha256=NULL WHERE id=?", (qualification.copy_id,))
    owned.connection.commit()
    with pytest.raises(CopyCompletionError) as caught:
        _complete(owned, roots, qualification.copy_id,
                  digest=_FixedDigest(error="device refused"))
    assert caught.value.stage == "verification_failed"
    state, source, target, error = _row(
        owned,
        "SELECT verification_state, source_sha256, target_sha256,"
        " verification_error_json FROM file_copies WHERE id=?", qualification.copy_id)
    assert state == 6 and source is None and target == _CONTENT_DIGEST
    assert error is not None
    assert _row(owned, "SELECT source_dependency FROM obtain_items WHERE id=?",
        _item_id(owned, qualification.delivery_id))[0] == 1
    assert _events(owned, 23, 2) == 0


def test_fetched_source_digest_is_saved_and_reused(complete_env):
    """建档未带源摘要时经端口取得并保存；重复收尾沿用已保存值。"""
    owned, roots, qualification = complete_env
    owned.connection.execute(
        "UPDATE file_copies SET source_sha256=NULL WHERE id=?", (qualification.copy_id,))
    owned.connection.commit()
    digest = _FixedDigest(_CONTENT_DIGEST)
    step = _complete(owned, roots, qualification.copy_id, digest=digest)
    assert step.phase is CompletionPhase.PREPARED
    assert digest.calls == 1
    assert _row(owned, "SELECT verification_state, source_sha256 FROM file_copies"
        " WHERE id=?", qualification.copy_id) == (3, _CONTENT_DIGEST)
    # 再次收尾（无端口）沿用数据库已保存的源摘要，不重新获取。
    again = _complete(owned, roots, qualification.copy_id)
    assert again.phase is CompletionPhase.PREPARED


def test_host_digest_failure_keeps_state(complete_env, monkeypatch):
    """主机摘要计算失败：不保存任何校验事实，恢复后重算。"""
    from camctl.host_files.io import HashResult
    from camctl.outputs import copy as copy_module

    owned, roots, qualification = complete_env

    def _fail(ref, roots_):
        return HashResult(digest=None, size_bytes=None, error="read failed")

    monkeypatch.setattr(copy_module, "hash_target", _fail)
    with pytest.raises(CopyCompletionError) as caught:
        _complete(owned, roots, qualification.copy_id)
    assert caught.value.stage == "host_digest_failed"
    monkeypatch.undo()
    assert _row(owned, "SELECT verification_state FROM file_copies WHERE id=?",
        qualification.copy_id)[0] == 1
    assert _events(owned, 22, 3) == 0
    assert _complete(owned, roots, qualification.copy_id).phase is CompletionPhase.PREPARED


def test_incomplete_copy_cannot_complete(complete_env):
    owned, roots, qualification = complete_env
    owned.connection.execute(
        "UPDATE file_copies SET committed_bytes=5 WHERE id=?", (qualification.copy_id,))
    owned.connection.commit()
    with pytest.raises(CopyCompletionError) as caught:
        _complete(owned, roots, qualification.copy_id,
                  digest=_FixedDigest(_CONTENT_DIGEST))
    assert caught.value.stage == "copy_incomplete"


# ---- 摘要不一致与有限重拷 ----


def _seed_wrong_source_digest(owned, copy_id: int) -> None:
    owned.connection.execute(
        "UPDATE file_copies SET source_sha256=? WHERE id=?", (_WRONG_DIGEST, copy_id))
    owned.connection.commit()


def test_mismatch_registers_recopy_round(complete_env):
    """摘要不一致：同一事务保存不一致事实并登记新一轮从零拷贝。"""
    owned, roots, qualification = complete_env
    _seed_wrong_source_digest(owned, qualification.copy_id)
    step = _complete(owned, roots, qualification.copy_id)
    assert step.phase is CompletionPhase.RECOPY_REGISTERED
    assert step.recopy.round == 2 and step.recopy.recopies_used == 1
    assert _row(owned, "SELECT round, recopies_used, max_recopies_used,"
        " committed_bytes, reset_state, verification_state, source_sha256, target_sha256"
        " FROM file_copies WHERE id=?", qualification.copy_id) == (
        2, 1, 1, 0, 2, 1, _WRONG_DIGEST, None)
    assert _events(owned, 22, 3) == 1
    assert _events(owned, 22, 4) == 1


def test_recopy_round_restarts_target_and_completes(complete_env):
    """重拷登记后截断重建，新一轮完整拷贝后校验通过并准备完成。"""
    owned, roots, qualification = complete_env
    _seed_wrong_source_digest(owned, qualification.copy_id)
    first = _complete(owned, roots, qualification.copy_id)
    assert first.phase is CompletionPhase.RECOPY_REGISTERED
    # 截断/重建并保存重置完成，再按更正后的源摘要从头拷贝一轮。
    asyncio.run(prepare_copy(qualification.copy_id, CopyContext(
        repository=OutputsRepository(), owned=owned, roots=roots, occurred_at=_NOW + 6)))
    owned.connection.execute(
        "UPDATE file_copies SET source_sha256=? WHERE id=?",
        (_CONTENT_DIGEST, qualification.copy_id))
    owned.connection.commit()
    asyncio.run(_drive(owned, roots, qualification.copy_id, segment_size=4,
                       occurred_at=_NOW + 7))
    step = _complete(owned, roots, qualification.copy_id)
    assert step.phase is CompletionPhase.PREPARED
    assert step.prepared.source_dependency_released is True
    assert _row(owned, "SELECT round, verification_state FROM file_copies WHERE id=?",
        qualification.copy_id) == (2, 3)


def test_exhausted_recopies_fail_without_new_round(complete_env):
    """重拷额度耗尽：保存判定上限，不再登记轮次，副本不准备完成。"""
    owned, roots, qualification = complete_env
    _seed_wrong_source_digest(owned, qualification.copy_id)
    owned.connection.execute(
        "UPDATE file_copies SET round=2, recopies_used=1 WHERE id=?",
        (qualification.copy_id,))
    owned.connection.commit()
    with pytest.raises(CopyCompletionError) as caught:
        _complete(owned, roots, qualification.copy_id, max_recopies=1)
    assert caught.value.stage == "recopy_budget_exhausted"
    assert _row(owned, "SELECT round, recopies_used, max_recopies_used, reset_state,"
        " verification_state FROM file_copies WHERE id=?", qualification.copy_id) == (
        2, 1, 1, 1, 4)
    assert _events(owned, 22, 4) == 0
    assert _events(owned, 22, 7) == 1
    assert _row(owned, "SELECT source_dependency FROM obtain_items WHERE id=?",
        _item_id(owned, qualification.delivery_id))[0] == 1


def test_read_budget_independent_from_recopy(complete_env):
    """原额度 5 下三次失败仍可登记重拷；本次读取额度 3 拒绝新尝试。"""
    owned, roots, qualification = complete_env
    _seed_wrong_source_digest(owned, qualification.copy_id)
    run_id = _row(owned, "SELECT id FROM operation_runs WHERE responsibility_key=?",
        f"read/{qualification.copy_id}")[0]
    _seed_failed_attempts(owned, run_id, 3)
    with closing(owned.connection.execute(
        "SELECT * FROM operation_attempts WHERE run_id=? ORDER BY attempt_no",
        (run_id,))) as cursor:
        attempts_before = cursor.fetchall()
    step = _complete(owned, roots, qualification.copy_id)
    assert step.phase is CompletionPhase.RECOPY_REGISTERED
    assert _row(owned, "SELECT round,recopies_used,max_recopies_used,committed_bytes"
        " FROM file_copies WHERE id=?", qualification.copy_id) == (2, 1, 1, 0)
    assert _row(owned, "SELECT attempts_used,max_attempts_used,retry_wait_required"
        " FROM operation_runs WHERE id=?", run_id) == (3, 5, 1)
    # 重拷只消耗重拷额度；新增读取按本次上限 3 与原已用 3 次比较。
    OutputsRepository().grant_read_slot(
        SlotRequest(qualification.copy_id, _NOW + 8), new_operation_key(), owned)
    intent = AttemptIntent(
        operation="read", action_id=_row(
            owned, "SELECT action_id FROM operation_runs WHERE id=?", run_id)[0],
        kind=OperationKind.READ_FILE, target=AttemptTarget(copy_id=qualification.copy_id),
        query_purpose=None, config=AttemptConfig(3, Decimal("10"), Decimal("0")),
        occurred_at=_NOW + 9, copy_round=2,
    )
    denied = OperationRepository().begin_attempt(intent, new_operation_key(), owned)
    assert denied.kind is DbOutcomeKind.COMPLETED, denied.error
    assert denied.value.disposition is BeginDisposition.REJECTED
    assert denied.value.reason == "budget_exhausted"
    assert _row(owned, "SELECT attempts_used,max_attempts_used,status,retry_wait_required"
        " FROM operation_runs WHERE id=?", run_id) == (3, 3, 4, 0)
    assert _row(owned, "SELECT round,recopies_used FROM file_copies WHERE id=?",
        qualification.copy_id) == (2, 1)
    with closing(owned.connection.execute(
        "SELECT * FROM operation_attempts WHERE run_id=? ORDER BY attempt_no",
        (run_id,))) as cursor:
        assert cursor.fetchall() == attempts_before


def test_canceled_owner_neither_verifies_nor_recopies(complete_env):
    owned, roots, qualification = complete_env
    _seed_wrong_source_digest(owned, qualification.copy_id)
    owned.connection.execute(
        "UPDATE actions SET status=6, cancel_requested=1 WHERE id=?",
        (_owner_action_id(owned, qualification.copy_id),))
    owned.connection.commit()
    with pytest.raises(CopyCompletionError) as caught:
        _complete(owned, roots, qualification.copy_id)
    assert caught.value.stage == "owner_canceled"
    assert _row(owned, "SELECT verification_state, round FROM file_copies WHERE id=?",
        qualification.copy_id) == (1, 1)
    assert _events(owned, 22, 3) == 0


# ---- 内部输入与主机源 ----


def test_internal_input_completes_without_release(internal_complete_env):
    """内部输入副本同样校验并保存目标事实，但不解除取回源依赖。"""
    owned, roots, qualification = internal_complete_env
    step = _complete(owned, roots, qualification.copy_id)
    assert step.phase is CompletionPhase.PREPARED
    assert step.prepared.source_dependency_released is False
    assert _row(owned, "SELECT verification_state FROM file_copies WHERE id=?",
        qualification.copy_id)[0] == 3
    assert _row(owned, "SELECT size_bytes, sha256 FROM intermediate_files WHERE id=?",
        qualification.target_file_id) == (10, _CONTENT_DIGEST)
    assert _events(owned, 23, 2) == 0
    assert _events(owned, 21, 3) == 0


def test_local_source_completes_matched(local_read, tmp_path):
    """主机派生成品源按实际内容校验并准备完成。"""
    owned, command = local_read
    roots = _make_roots(tmp_path)
    source_path = roots.staging / "derived" / "801.mp4"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(_CONTENT)
    owned.connection.execute(
        "UPDATE intermediate_files SET size_bytes=10, sha256=? WHERE id=801",
        (_CONTENT_DIGEST,))
    owned.connection.commit()
    qualification = _qualified(owned, command)
    asyncio.run(_drive(owned, roots, qualification.copy_id, segment_size=10))
    step = _complete(owned, roots, qualification.copy_id)
    assert step.phase is CompletionPhase.PREPARED
    assert step.prepared.source_dependency_released is True
    assert step.prepared.sha256 == _CONTENT_DIGEST


# ---- 保存事务与原键 ----


def test_save_verification_key_recovers_first_response(complete_env):
    owned, roots, qualification = complete_env
    repository = OutputsRepository()
    key = new_operation_key()
    command = VerificationSave(
        copy_id=qualification.copy_id, state=3, source_sha256=_CONTENT_DIGEST,
        target_sha256=_CONTENT_DIGEST, error_json=None, occurred_at=_NOW + 5)
    first = repository.save_verification(command, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    before = tuple(owned.connection.iterdump())
    again = repository.save_verification(command, key, owned)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert tuple(owned.connection.iterdump()) == before
    changed = repository.save_verification(replace(
        command, target_sha256=_WRONG_DIGEST), key, owned)
    assert changed.kind is DbOutcomeKind.ROLLED_BACK


def test_register_recopy_key_recovers_first_response(complete_env):
    owned, roots, qualification = complete_env
    _seed_wrong_source_digest(owned, qualification.copy_id)
    repository = OutputsRepository()
    key = new_operation_key()
    command = RecopyRegistration(
        copy_id=qualification.copy_id, source_sha256=_WRONG_DIGEST,
        target_sha256=_CONTENT_DIGEST, max_recopies=1, occurred_at=_NOW + 5)
    first = repository.register_recopy(command, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    assert first.value.disposition is RecopyDisposition.REGISTERED
    before = tuple(owned.connection.iterdump())
    again = repository.register_recopy(command, key, owned)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert tuple(owned.connection.iterdump()) == before
    changed = repository.register_recopy(replace(command, max_recopies=2), key, owned)
    assert changed.kind is DbOutcomeKind.ROLLED_BACK


def test_save_prepared_key_recovers_first_response(complete_env):
    owned, roots, qualification = complete_env
    repository = OutputsRepository()
    repository.save_verification(
        VerificationSave(
            copy_id=qualification.copy_id, state=3, source_sha256=_CONTENT_DIGEST,
            target_sha256=_CONTENT_DIGEST, error_json=None, occurred_at=_NOW + 5),
        new_operation_key(), owned)
    key = new_operation_key()
    request = PreparedRequest(
        copy_id=qualification.copy_id, target_sha256=_CONTENT_DIGEST,
        size_bytes=10, occurred_at=_NOW + 6)
    first = repository.save_prepared(request, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    assert first.value.prepared.source_dependency_released is True
    before = tuple(owned.connection.iterdump())
    again = repository.save_prepared(request, key, owned)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert tuple(owned.connection.iterdump()) == before
    changed = repository.save_prepared(replace(request, size_bytes=9), key, owned)
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


def test_guard_accepts_real_verification_and_recopy_events(complete_env):
    owned, roots, qualification = complete_env
    verify = _proposal(owned, outputs._IntegritySaveCommand(VerificationSave(
        copy_id=qualification.copy_id, state=3, source_sha256=_CONTENT_DIGEST,
        target_sha256=_CONTENT_DIGEST, error_json=None, occurred_at=_NOW + 5,
    ), new_operation_key()))
    _validate(verify)
    _seed_wrong_source_digest(owned, qualification.copy_id)
    recopy = _proposal(owned, outputs._RecopyCommand(RecopyRegistration(
        copy_id=qualification.copy_id, source_sha256=_WRONG_DIGEST,
        target_sha256=_CONTENT_DIGEST, max_recopies=1, occurred_at=_NOW + 5,
    ), new_operation_key()))
    _validate(recopy)


def test_guard_rejects_matched_with_unequal_digests(complete_env):
    owned, roots, qualification = complete_env
    plan = _proposal(owned, outputs._IntegritySaveCommand(VerificationSave(
        copy_id=qualification.copy_id, state=3, source_sha256=_CONTENT_DIGEST,
        target_sha256=_CONTENT_DIGEST, error_json=None, occurred_at=_NOW + 5,
    ), new_operation_key()))
    event = plan.events[0]
    row = event.rows[0]
    variant = replace(
        row, after=replace(row.after, values={
            **row.after.values, "target_sha256": _WRONG_DIGEST}))
    with pytest.raises(EventValidationError):
        _validate(plan, events=(replace(event, rows=(variant,)),))


def test_guard_rejects_recopy_without_full_progress(complete_env):
    owned, roots, qualification = complete_env
    _seed_wrong_source_digest(owned, qualification.copy_id)
    owned.connection.execute(
        "UPDATE file_copies SET verification_state=4, target_sha256=? WHERE id=?",
        (_CONTENT_DIGEST, qualification.copy_id))
    owned.connection.commit()
    plan = _proposal(owned, outputs._RecopyCommand(RecopyRegistration(
        copy_id=qualification.copy_id, source_sha256=_WRONG_DIGEST,
        target_sha256=_CONTENT_DIGEST, max_recopies=1, occurred_at=_NOW + 5,
    ), new_operation_key()))
    event = next(e for e in plan.events if e.event_type == 22 and e.reason == 4)
    row = event.rows[0]
    variant = replace(
        row, after=replace(row.after, values={
            **row.after.values, "committed_bytes": 4}))
    with pytest.raises(EventValidationError):
        _validate(plan, events=(replace(event, rows=(variant,)),))


def test_guard_rejects_release_without_prepared_copy(complete_env):
    """副本未完成校验前解除源保护被正式守卫拒绝。"""
    owned, roots, qualification = complete_env
    repository = OutputsRepository()
    repository.save_verification(
        VerificationSave(
            copy_id=qualification.copy_id, state=3, source_sha256=_CONTENT_DIGEST,
            target_sha256=_CONTENT_DIGEST, error_json=None, occurred_at=_NOW + 5),
        new_operation_key(), owned)
    plan = _proposal(owned, outputs._PreparedSaveCommand(PreparedRequest(
        copy_id=qualification.copy_id, target_sha256=_CONTENT_DIGEST,
        size_bytes=10, occurred_at=_NOW + 6,
    ), new_operation_key()))
    release = next(e for e in plan.events if e.event_type == 21)
    tampered = {
        table: {row_id: dict(rows) for row_id, rows in rows.items()}
        for table, rows in plan.state_rows.items()
    }
    tampered["file_copies"][qualification.copy_id]["verification_state"] = 1
    with pytest.raises(EventValidationError):
        _validate(plan, events=(release,), state_rows=tampered)

"""中间文件有限清理的真实组合：生命周期、预算、游标与失败收场。

真实 SQLite、真实 staging 文件与清理事务共同验证
[取回中间文件的保留与清理](../../../../architecture/obtaining-outputs.md#取回中间文件的保留与清理)
及[中间文件清理的运行预算](../../../../architecture/file-handoff.md#中间文件清理的运行预算)：
先可靠保存取消或失败并确认操作停止，再保存意图、删除并保存结果；
历史扫描按固定上界、剩余额度与可靠游标单轮推进，删除失败保留责任
留给后续正常运行，不重开原动作，也不单独要求会话继续。
"""

import asyncio
import hashlib
import json
from contextlib import closing
from dataclasses import replace

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.history.validators import (
    EventContext, EventValidationError, validate_event,
)
from camctl.host_files.models import BoundDirectories
from camctl.outputs.copy import CompletionContext, CompletionPhase, complete_copy
from camctl.outputs.handoff import (
    DeliveryContext, DeliveryDirectories, DeliveryPhase, publish_delivery,
)
from camctl.outputs.work_files import (
    CleanupChecked, CleanupIntent, CleanupResultSave, RetentionRelease,
    WorkFileAction, WorkFileContext, WorkFileFailure, WorkFileLimits,
    WorkFileOutcome, WorkFileSingleOutcome, clean_one_work_file,
    clean_work_files,
)
from camctl.persistence.models import DbOutcomeKind
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
def work_env(read_targets, tmp_path):
    """已准备完成（PREPARED）的交付副本；staging 工作副本仍在原位。"""
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
    roots = BoundDirectories(staging=staging)
    asyncio.run(_drive(owned, roots, qualification.copy_id, segment_size=10))
    step = asyncio.run(complete_copy(qualification.copy_id, CompletionContext(
        repository=OutputsRepository(), owned=owned, roots=roots,
        occurred_at=_NOW + 5)))
    assert step.phase is CompletionPhase.PREPARED
    return owned, roots, qualification


def _context(owned, roots, *, batch=32, limit=128, occurred_at=_NOW + 20):
    return WorkFileContext(
        repository=OutputsRepository(), owned=owned, staging=roots.staging,
        occurred_at=occurred_at,
        limits=WorkFileLimits(batch_size=batch, limit_per_run=limit))


def _row(owned, sql: str, *params):
    with closing(owned.connection.execute(sql, params)) as cursor:
        return cursor.fetchone()


def _events(owned, event_type: int, reason: int) -> int:
    return _row(
        owned,
        "SELECT COUNT(*) FROM history_events WHERE event_type=?"
        " AND json_extract(body_json, '$.reason')=?",
        event_type, reason)[0]


def _last_event(owned) -> int:
    return _row(owned, "SELECT MAX(id) FROM history_events")[0]


def _file_state(owned, file_id: int):
    return _row(
        owned,
        "SELECT retention_state, cleanup_state, last_error_json"
        " FROM intermediate_files WHERE id=?", file_id)


def _work_path(roots, owned, file_id: int):
    return roots.staging / _row(
        owned, "SELECT relative_path FROM intermediate_files WHERE id=?",
        file_id)[0]


def _cancel_delivery(owned, delivery_id: int) -> None:
    """测试准备：交付取消终态（模拟取消收场事实已可靠保存）。"""
    owned.connection.execute(
        "UPDATE deliveries SET status=7 WHERE id=?", (delivery_id,))
    owned.connection.commit()


def _seed_action_candidate(owned, roots, *, file_id: int) -> None:
    """登记一条动作归属的额外候选（归属录像动作 11，已终态）。

    测试准备直接构造行，不经被测入口；与交付副本的归属互不影响。
    """
    path = f"recording-inputs/{file_id}.bin"
    owned.connection.execute(
        "INSERT INTO intermediate_files (id, owner_action_id,"
        " owner_delivery_id, purpose, relative_path, retention_state,"
        " cleanup_state, created_event_id, last_event_id, change_count)"
        " VALUES (?, 11, NULL, 2, ?, 2, 2, ?, ?, 1)",
        (file_id, path, _last_event(owned), _last_event(owned)))
    owned.connection.commit()
    target = roots.staging / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_CONTENT)


def _release(owned, file_id: int):
    return OutputsRepository().save_retention_release(
        RetentionRelease(file_id=file_id, occurred_at=_NOW + 12),
        new_operation_key(), owned)


def _release_ok(owned, file_id: int) -> None:
    outcome = _release(owned, file_id)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error


# ---- 核心用例：清理失败不重开动作 ----


def test_work_file_cleanup_does_not_restart_action(work_env, monkeypatch):
    """取消后完整副本删除失败：动作与交付终态保持，责任保留不阻塞收尾。"""
    owned, roots, qualification = work_env
    delivery_id = qualification.delivery_id
    file_id = qualification.target_file_id
    _cancel_delivery(owned, delivery_id)
    action_status = _row(owned, "SELECT status FROM actions WHERE id=31")[0]
    assert _work_path(roots, owned, file_id).exists()

    def _refused(path):
        raise OSError("删除被拒绝")

    monkeypatch.setattr(
        "camctl.outputs.work_files._remove_work_file", _refused)
    result = asyncio.run(clean_one_work_file(file_id, _context(owned, roots)))
    assert result.outcome is WorkFileSingleOutcome.FAILED
    retention, cleanup, error_json = _file_state(owned, file_id)
    assert (retention, cleanup) == (2, 5)
    error = json.loads(error_json)
    assert error["code"] == "work_file_delete_failed"
    assert error["details"] == {"delivery_id": str(delivery_id)}
    # 动作与交付终态不被清理失败改写，也不产生新的动作收场事件。
    assert _row(
        owned, "SELECT status FROM actions WHERE id=31")[0] == action_status
    assert _row(
        owned, "SELECT status FROM deliveries WHERE id=?",
        delivery_id)[0] == 7
    assert _events(owned, 8, 4) == 0
    # 文件保留，责任留给后续正常运行有限重试。
    assert _work_path(roots, owned, file_id).exists()

    monkeypatch.undo()
    again = asyncio.run(clean_one_work_file(
        file_id, _context(owned, roots)))
    assert again.outcome is WorkFileSingleOutcome.DELETED
    assert not _work_path(roots, owned, file_id).exists()
    assert _file_state(owned, file_id)[:2] == (2, 4)


# ---- 生命周期判定 ----


def test_release_requires_owner_terminal(work_env):
    """交付仍有效待发布：释放被拒绝，副本保留继续原发布流程。"""
    owned, roots, qualification = work_env
    outcome = _release(owned, qualification.target_file_id)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, ConsistencyError)
    assert _file_state(owned, qualification.target_file_id)[:2] == (1, 1)
    assert _work_path(
        roots, owned, qualification.target_file_id).exists()


def test_release_requires_stopped_operations(work_env):
    """存在未结束读取尝试：释放被拒绝，实际操作未结束不删。"""
    owned, roots, qualification = work_env
    _cancel_delivery(owned, qualification.delivery_id)
    run_id = _row(owned, "SELECT id FROM operation_runs WHERE copy_id=?",
                  qualification.copy_id)[0]
    marker = _last_event(owned)
    owned.connection.execute(
        "INSERT INTO operation_attempts (id, run_id, attempt_no, status,"
        " intent_event_id, max_attempts_used, effect_state)"
        " VALUES (9001, ?, 1, 1, ?, 3, 1)",
        (run_id, marker))
    owned.connection.commit()
    outcome = _release(owned, qualification.target_file_id)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, ConsistencyError)
    assert _file_state(owned, qualification.target_file_id)[:2] == (1, 1)


def test_promoted_file_is_not_cleaned(work_env):
    """已成正式产物（PROMOTED）的中间文件不进入自动清理。"""
    owned, roots, qualification = work_env
    file_id = qualification.target_file_id
    owned.connection.execute(
        "UPDATE intermediate_files SET retention_state=3, cleanup_state=1"
        " WHERE id=?", (file_id,))
    owned.connection.commit()
    result = asyncio.run(clean_one_work_file(
        file_id, _context(owned, roots)))
    assert result.decision.action is WorkFileAction.NOT_MANAGED
    assert result.outcome is WorkFileSingleOutcome.NOT_MANAGED
    assert _work_path(roots, owned, file_id).exists()
    assert _file_state(owned, file_id)[:2] == (3, 1)
    scan = asyncio.run(clean_work_files(_context(owned, roots)))
    assert scan.checked == 0
    assert _work_path(roots, owned, file_id).exists()


def test_released_candidate_with_active_owner_is_kept(work_env):
    """候选已释放但归属又出现活跃事实（数据被外力改变）：保留不删。"""
    owned, roots, qualification = work_env
    file_id = qualification.target_file_id
    owned.connection.execute(
        "UPDATE intermediate_files SET retention_state=2, cleanup_state=2"
        " WHERE id=?", (file_id,))
    owned.connection.commit()
    result = asyncio.run(clean_one_work_file(
        file_id, _context(owned, roots)))
    assert result.decision.action is WorkFileAction.KEEP_REQUIRED
    assert result.outcome is WorkFileSingleOutcome.KEPT
    assert _work_path(roots, owned, file_id).exists()
    assert _file_state(owned, file_id)[:2] == (2, 2)


def test_missing_file_after_interrupt_records_completion(work_env):
    """文件已删而结果未保存就中断：重新核实不存在后补记完成。"""
    owned, roots, qualification = work_env
    file_id = qualification.target_file_id
    _cancel_delivery(owned, qualification.delivery_id)
    _work_path(roots, owned, file_id).unlink()
    result = asyncio.run(clean_one_work_file(
        file_id, _context(owned, roots)))
    assert result.outcome is WorkFileSingleOutcome.DELETED
    assert _file_state(owned, file_id)[:2] == (2, 4)


def test_observation_failure_is_not_missing(work_env, monkeypatch):
    """工作位置观察失败：保存失败诊断，不冒充清理完成。"""
    owned, roots, qualification = work_env
    _cancel_delivery(owned, qualification.delivery_id)

    def _broken(path):
        return OSError("目录不可访问")

    monkeypatch.setattr(
        "camctl.outputs.work_files._observe_work_file", _broken)
    result = asyncio.run(clean_one_work_file(
        qualification.target_file_id, _context(owned, roots)))
    assert result.outcome is WorkFileSingleOutcome.FAILED
    retention, cleanup, error_json = _file_state(
        owned, qualification.target_file_id)
    assert (retention, cleanup) == (2, 5)
    assert json.loads(error_json)["code"] == "work_file_delete_failed"


# ---- 历史扫描预算与游标 ----


def test_history_scan_respects_run_limit_and_resumes(work_env):
    """额度先尽即止；已检查记录推进游标，下次运行从其后继续。"""
    owned, roots, qualification = work_env
    file_id = qualification.target_file_id
    _cancel_delivery(owned, qualification.delivery_id)
    _release_ok(owned, file_id)
    _seed_action_candidate(owned, roots, file_id=900)
    first = asyncio.run(clean_work_files(
        _context(owned, roots, batch=32, limit=1)))
    assert (first.checked, first.cleaned) == (1, 1)
    assert _row(
        owned, "SELECT cleanup_cursor_file_id FROM runtime_state")[0] \
        == file_id
    assert _file_state(owned, 900)[:2] == (2, 2)
    second = asyncio.run(clean_work_files(
        _context(owned, roots, batch=32, limit=1)))
    assert (second.checked, second.cleaned) == (1, 1)
    assert _row(
        owned, "SELECT cleanup_cursor_file_id FROM runtime_state")[0] == 900
    assert _file_state(owned, 900)[:2] == (2, 4)
    third = asyncio.run(clean_work_files(
        _context(owned, roots, batch=32, limit=1)))
    assert (third.checked, third.cleaned) == (0, 0)


def test_history_scan_wraps_to_start_once(work_env):
    """游标已到记录末尾后绕回候选范围起点，本轮最多检查一轮。"""
    owned, roots, qualification = work_env
    file_id = qualification.target_file_id
    _cancel_delivery(owned, qualification.delivery_id)
    _release_ok(owned, file_id)
    _seed_action_candidate(owned, roots, file_id=900)
    # 第一轮只检查交付副本（较小 id），游标落在它之前还有候选 900。
    first = asyncio.run(clean_work_files(
        _context(owned, roots, batch=1, limit=1)))
    assert first.checked == 1
    assert _row(
        owned, "SELECT cleanup_cursor_file_id FROM runtime_state")[0] \
        == file_id
    second = asyncio.run(clean_work_files(
        _context(owned, roots, batch=32, limit=1)))
    assert (second.checked, second.cleaned) == (1, 1)
    # 候选全部完成，新一轮无记录可查。
    third = asyncio.run(clean_work_files(
        _context(owned, roots, batch=32, limit=2)))
    assert third.checked == 0
    assert _file_state(owned, 900)[:2] == (2, 4)


def test_kept_candidate_consumes_budget_and_advances(work_env):
    """不能安全删除的候选同样占用额度并推进游标，不阻挡后续记录。"""
    owned, roots, qualification = work_env
    file_id = qualification.target_file_id
    # 交付保持 PREPARED（归属活跃）：该候选保留。
    owned.connection.execute(
        "UPDATE intermediate_files SET retention_state=2, cleanup_state=2"
        " WHERE id=?", (file_id,))
    owned.connection.commit()
    _seed_action_candidate(owned, roots, file_id=900)
    result = asyncio.run(clean_work_files(
        _context(owned, roots, batch=32, limit=1)))
    assert (result.checked, result.cleaned, result.kept) == (1, 0, 1)
    assert _work_path(roots, owned, file_id).exists()
    assert _row(
        owned, "SELECT cleanup_cursor_file_id FROM runtime_state")[0] \
        == file_id
    following = asyncio.run(clean_work_files(
        _context(owned, roots, batch=32, limit=1)))
    assert (following.checked, following.cleaned) == (1, 1)
    assert _file_state(owned, 900)[:2] == (2, 4)


def test_batch_configuration_cannot_exceed_remaining(work_env):
    """批量配置大于剩余额度时按剩余额度读取。"""
    owned, roots, qualification = work_env
    _cancel_delivery(owned, qualification.delivery_id)
    _release_ok(owned, qualification.target_file_id)
    _seed_action_candidate(owned, roots, file_id=900)
    result = asyncio.run(clean_work_files(
        _context(owned, roots, batch=32, limit=1)))
    assert result.checked == 1


def test_first_cleanup_does_not_consume_history_budget(work_env):
    """本次运行中新形成的清理责任由首次入口清理，不占历史额度。"""
    owned, roots, qualification = work_env
    _cancel_delivery(owned, qualification.delivery_id)
    _seed_action_candidate(owned, roots, file_id=900)
    single = asyncio.run(clean_one_work_file(
        qualification.target_file_id, _context(owned, roots)))
    assert single.outcome is WorkFileSingleOutcome.DELETED
    # 首次清理不推进历史游标。
    assert _row(
        owned, "SELECT cleanup_cursor_file_id FROM runtime_state")[0] is None
    scan = asyncio.run(clean_work_files(
        _context(owned, roots, limit=1)))
    # 已完成清理的本文件不再是候选，额度用于其余候选。
    assert (scan.checked, scan.cleaned) == (1, 1)
    assert _file_state(owned, 900)[:2] == (2, 4)


def test_same_run_reuses_first_attempt_for_same_file(work_env):
    """首次入口已处理的文件被历史扫描再次发现时复用结果不重试。"""
    owned, roots, qualification = work_env
    file_id = qualification.target_file_id
    # 交付保持活跃：首次清理判定保留，文件不动。
    owned.connection.execute(
        "UPDATE intermediate_files SET retention_state=2, cleanup_state=2"
        " WHERE id=?", (file_id,))
    owned.connection.commit()
    context = _context(owned, roots)
    first = asyncio.run(clean_one_work_file(file_id, context))
    assert first.outcome is WorkFileSingleOutcome.KEPT
    events_before = _events(owned, 26, 3)
    scan = asyncio.run(clean_work_files(context))
    assert scan.checked == 1
    assert scan.kept == 1
    # 没有发起第二次清理尝试，也没有保存意图。
    assert _events(owned, 26, 3) == events_before
    assert _work_path(roots, owned, file_id).exists()
    assert _row(
        owned, "SELECT cleanup_cursor_file_id FROM runtime_state")[0] == file_id


def test_action_owned_delete_failure_keeps_responsibility(
        work_env, monkeypatch):
    """动作归属候选删除失败：保留未决责任，继续处理其余记录。"""
    owned, roots, qualification = work_env
    _cancel_delivery(owned, qualification.delivery_id)
    _release_ok(owned, qualification.target_file_id)
    _seed_action_candidate(owned, roots, file_id=900)
    from camctl.outputs import work_files as work_files_module
    original_remove = work_files_module._remove_work_file

    def _refused(path):
        if path.name == "900.bin":
            raise OSError("动作副本删除被拒绝")
        original_remove(path)

    monkeypatch.setattr(
        "camctl.outputs.work_files._remove_work_file", _refused)
    scan = asyncio.run(clean_work_files(_context(owned, roots)))
    monkeypatch.undo()
    assert (scan.checked, scan.cleaned, scan.failed) == (2, 1, 1)
    # 动作归属候选保留已保存意图的未决责任，错误对象尚无登记详情。
    assert _file_state(owned, 900)[1] == 3
    assert (roots.staging / "recording-inputs" / "900.bin").exists()
    # 交付副本正常清理，游标推进到本次最后检查的记录。
    assert _file_state(owned, qualification.target_file_id)[:2] == (2, 4)
    assert _row(
        owned, "SELECT cleanup_cursor_file_id FROM runtime_state")[0] == 900

# ---- 发布后的生命周期 ----


def _handoff_dirs(tmp_path, roots):
    ready = tmp_path / "ready"
    ready.mkdir(exist_ok=True)
    processing = tmp_path / "processing"
    processing.mkdir(exist_ok=True)
    return DeliveryDirectories(
        staging=roots.staging, ready=ready, processing=processing)


def test_publication_hands_off_intermediate_file(work_env, tmp_path):
    """发布确认后中间文件转 HANDED_OFF，交接所有权不归自动清理。"""
    owned, roots, qualification = work_env
    result = asyncio.run(publish_delivery(
        qualification.delivery_id,
        DeliveryContext(
            repository=OutputsRepository(), owned=owned,
            directories=_handoff_dirs(tmp_path, roots),
            occurred_at=_NOW + 10)))
    assert result.phase is DeliveryPhase.PUBLISHED
    assert _file_state(owned, qualification.target_file_id)[0] == 4
    assert _events(owned, 26, 2) >= 1
    # 已交接文件不进入历史清理候选。
    scan = asyncio.run(clean_work_files(_context(owned, roots)))
    assert scan.checked == 0


def test_published_leftover_copy_is_released_and_cleaned(
        work_env, tmp_path):
    """发布经同名文件确认而 staging 副本留存：转 HANDED_OFF 后释放清理。"""
    owned, roots, qualification = work_env
    directories = _handoff_dirs(tmp_path, roots)
    file_name = _row(owned, "SELECT file_name FROM deliveries WHERE id=?",
                     qualification.delivery_id)[0]
    (directories.ready / file_name).write_bytes(_CONTENT)
    result = asyncio.run(publish_delivery(
        qualification.delivery_id,
        DeliveryContext(
            repository=OutputsRepository(), owned=owned,
            directories=directories, occurred_at=_NOW + 10)))
    assert result.phase is DeliveryPhase.PUBLISHED
    leftover = _work_path(roots, owned, qualification.target_file_id)
    assert leftover.exists()
    assert _file_state(owned, qualification.target_file_id)[0] == 4
    _release_ok(owned, qualification.target_file_id)
    assert _file_state(owned, qualification.target_file_id)[:2] == (2, 2)
    single = asyncio.run(clean_one_work_file(
        qualification.target_file_id, _context(owned, roots)))
    assert single.outcome is WorkFileSingleOutcome.DELETED
    assert not leftover.exists()


# ---- 原键恢复 ----


def test_release_intent_result_checked_recover_first_response(work_env):
    """四个清理事务各自按原键恢复首次响应，输入不符拒绝。"""
    owned, roots, qualification = work_env
    repository = OutputsRepository()
    file_id = qualification.target_file_id
    _cancel_delivery(owned, qualification.delivery_id)

    release = RetentionRelease(file_id=file_id, occurred_at=_NOW + 12)
    key = new_operation_key()
    first = repository.save_retention_release(release, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    before = tuple(owned.connection.iterdump())
    again = repository.save_retention_release(release, key, owned)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert tuple(owned.connection.iterdump()) == before
    changed = repository.save_retention_release(
        replace(release, occurred_at=_NOW + 13), key, owned)
    assert changed.kind is DbOutcomeKind.ROLLED_BACK

    intent = CleanupIntent(file_id=file_id, occurred_at=_NOW + 14)
    key = new_operation_key()
    first = repository.save_cleanup_intent(intent, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    before = tuple(owned.connection.iterdump())
    again = repository.save_cleanup_intent(intent, key, owned)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert tuple(owned.connection.iterdump()) == before

    result_save = CleanupResultSave(
        file_id=file_id, outcome=WorkFileOutcome.COMPLETED,
        error=None, occurred_at=_NOW + 15, advance_cursor=True)
    key = new_operation_key()
    first = repository.save_cleanup_result(result_save, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    before = tuple(owned.connection.iterdump())
    again = repository.save_cleanup_result(result_save, key, owned)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert tuple(owned.connection.iterdump()) == before
    changed = repository.save_cleanup_result(
        replace(result_save, occurred_at=_NOW + 17), key, owned)
    assert changed.kind is DbOutcomeKind.ROLLED_BACK

    _seed_action_candidate(owned, roots, file_id=900)
    checked = CleanupChecked(file_id=900, occurred_at=_NOW + 16)
    key = new_operation_key()
    first = repository.save_cleanup_checked(checked, key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    before = tuple(owned.connection.iterdump())
    again = repository.save_cleanup_checked(checked, key, owned)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert tuple(owned.connection.iterdump()) == before
    assert _row(
        owned, "SELECT cleanup_cursor_file_id FROM runtime_state")[0] == 900


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
                chosen[0].transaction_id, chosen[0].event_id,
                chosen[-1].event_id),
            dict(plan.owners),
            state_rows if state_rows is not None else plan.state_rows,
            read_coverage=plan.read_coverage)
        validate_event(event, context)
        for row in event.rows:
            if row.after.exists:
                working.setdefault(row.table, {}).setdefault(
                    row.row_id, {}).update(row.after.values)


def _force_candidate(owned, file_id: int) -> None:
    """把目标置为 RELEASABLE 待清理（绕过释放事务，直接构造状态）。"""
    owned.connection.execute(
        "UPDATE intermediate_files SET retention_state=2, cleanup_state=2"
        " WHERE id=?", (file_id,))
    owned.connection.commit()


def test_guard_accepts_real_cleanup_events(work_env):
    owned, roots, qualification = work_env
    file_id = qualification.target_file_id
    _cancel_delivery(owned, qualification.delivery_id)
    _validate(_proposal(
        owned, outputs._RetentionReleaseCommand(
            RetentionRelease(file_id=file_id, occurred_at=_NOW + 12),
            new_operation_key())))
    _release_ok(owned, file_id)
    _validate(_proposal(
        owned, outputs._CleanupIntentCommand(
            CleanupIntent(file_id=file_id, occurred_at=_NOW + 14),
            new_operation_key())))
    OutputsRepository().save_cleanup_intent(
        CleanupIntent(file_id=file_id, occurred_at=_NOW + 14),
        new_operation_key(), owned)
    _validate(_proposal(
        owned, outputs._CleanupResultCommand(
            CleanupResultSave(
                file_id=file_id, outcome=WorkFileOutcome.COMPLETED,
                error=None, occurred_at=_NOW + 15, advance_cursor=True),
            new_operation_key())))
    failure = WorkFileFailure(
        code="work_file_delete_failed",
        details={"delivery_id": str(qualification.delivery_id)})
    _validate(_proposal(
        owned, outputs._CleanupResultCommand(
            CleanupResultSave(
                file_id=file_id, outcome=WorkFileOutcome.FAILED,
                error=failure, occurred_at=_NOW + 15, advance_cursor=True),
            new_operation_key())))


def _result_command(owned, file_id, outcome, error):
    return outputs._CleanupResultCommand(
        CleanupResultSave(
            file_id=file_id, outcome=outcome, error=error,
            occurred_at=_NOW + 15, advance_cursor=True),
        new_operation_key())


def test_guard_rejects_result_without_required_error(work_env):
    """删除失败结果必须携带按公共登记构造的错误对象。"""
    owned, roots, qualification = work_env
    file_id = qualification.target_file_id
    _force_candidate(owned, file_id)
    plan = _proposal(
        owned, _result_command(
            owned, file_id, WorkFileOutcome.COMPLETED, None))
    event = next(
        event for event in plan.events
        if event.event_type == 26 and event.reason == 4)
    row = next(
        row for row in event.rows if row.table == "intermediate_files")
    variant = replace(
        row, after=replace(row.after, values={
            **row.after.values, "cleanup_state": 5}))
    with pytest.raises(EventValidationError):
        _validate(plan, events=(replace(event, rows=(variant,)),))


def test_guard_rejects_completed_with_error(work_env):
    """完成结果不得携带清理错误。"""
    owned, roots, qualification = work_env
    file_id = qualification.target_file_id
    _force_candidate(owned, file_id)
    failure = WorkFileFailure(
        code="work_file_delete_failed",
        details={"delivery_id": str(qualification.delivery_id)})
    plan = _proposal(
        owned, _result_command(
            owned, file_id, WorkFileOutcome.FAILED, failure))
    event = next(
        event for event in plan.events
        if event.event_type == 26 and event.reason == 4)
    row = next(
        row for row in event.rows if row.table == "intermediate_files")
    variant = replace(
        row, after=replace(row.after, values={
            **row.after.values, "cleanup_state": 4}))
    with pytest.raises(EventValidationError):
        _validate(plan, events=(replace(event, rows=(variant,)),))


def test_guard_rejects_intent_with_retention_change(work_env):
    """意图事件只推进清理状态，不得改动保留状态。"""
    owned, roots, qualification = work_env
    file_id = qualification.target_file_id
    _force_candidate(owned, file_id)
    plan = _proposal(
        owned, outputs._CleanupIntentCommand(
            CleanupIntent(file_id=file_id, occurred_at=_NOW + 14),
            new_operation_key()))
    event = plan.events[0]
    row = event.rows[0]
    variant = replace(
        row, after=replace(row.after, values={
            **row.after.values, "retention_state": 1}))
    with pytest.raises(EventValidationError):
        _validate(plan, events=(replace(event, rows=(variant,)),))

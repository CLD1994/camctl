"""共用拷贝的续传准备：真实 SQLite 与真实文件验证中断恢复。"""

import asyncio
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.history.validators import EventContext, EventValidationError, validate_event
from camctl.host_files.models import BoundDirectories, FilePurpose
from camctl.operations.attempts import (
    AttemptConfig, AttemptFinish, AttemptIntent, AttemptTarget, BeginDisposition,
    FinishDisposition, OperationKind, RunStatus,
)
from camctl.operations.models import (
    AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue, Settlement,
    SettlementBasis,
)
from camctl.operations.validation import validate_outcome
from camctl.outputs.copy import (
    AttemptPlan, CopyContext, CopyPreparationError, ResumeOutcome, TargetResetOutcome,
    TargetResetRequest, prepare_copy,
)
from camctl.outputs.qualification import QualificationOutcome
from camctl.outputs.slots import SlotOutcome, SlotRequest
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories import outputs
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.transaction import TransactionScope

from .test_local_read import local_read  # noqa: F401  主机源建档夹具
from .test_qualification import _NOW
from .test_read_associations import read_targets, _command


def _qualified(owned, command):
    first = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    assert first.value.outcome is QualificationOutcome.GRANTED
    return first.value


@pytest.fixture(params=["delivery", "internal"])
def copy_env(request, read_targets, tmp_path):
    """已建档拷贝与真实 staging 目录；交付与录像内部输入共用同一入口。"""
    owned = read_targets
    command = _command(internal=request.param == "internal")
    if request.param == "internal":
        owned.connection.execute("DELETE FROM obtain_items")
        owned.connection.execute("DELETE FROM outputs")
    else:
        owned.connection.execute("UPDATE actions SET status=3 WHERE id=11")
    owned.connection.commit()
    qualification = _qualified(owned, command)
    return owned, _make_roots(tmp_path), qualification


def _make_roots(tmp_path) -> BoundDirectories:
    staging = tmp_path / "staging"
    for name in ("deliveries", "recording-inputs", "derived"):
        (staging / name).mkdir(parents=True)
    return BoundDirectories(staging=staging)


def _run_action(owned, run_id: int) -> int:
    with closing(owned.connection.execute(
        "SELECT action_id FROM operation_runs WHERE id=?", (run_id,),
    )) as cursor:
        return cursor.fetchone()[0]


def _relative(owned, file_id: int) -> str:
    with closing(owned.connection.execute(
        "SELECT relative_path FROM intermediate_files WHERE id=?", (file_id,),
    )) as cursor:
        return cursor.fetchone()[0]


def _target_path(roots: BoundDirectories, owned, file_id: int):
    return roots.staging / _relative(owned, file_id)


def _seed_progress(owned, copy_id: int, committed: int) -> None:
    # SEGMENT 事件生产者属于 X5；准备与恢复用例直接保存可靠进度事实。
    owned.connection.execute(
        "UPDATE file_copies SET committed_bytes=? WHERE id=?", (committed, copy_id))
    owned.connection.commit()


def _seed_reset_pending(owned, copy_id: int) -> None:
    # RECOPY 事务属于 X6；重置完成路径直接登记重置意图事实。
    owned.connection.execute(
        "UPDATE file_copies SET reset_state=2, committed_bytes=0 WHERE id=?", (copy_id,))
    owned.connection.commit()


def _seed_failed_attempts(owned, run_id: int, count: int) -> None:
    """原额度为 5 时保存真实失败及等待，供后续运行按新额度判定。"""
    with closing(owned.connection.execute(
        "SELECT r.action_id,r.copy_id,c.round,r.attempts_used FROM operation_runs r"
        " JOIN file_copies c ON c.id=r.copy_id WHERE r.id=?", (run_id,))) as cursor:
        action_id, copy_id, copy_round, attempts_used = cursor.fetchone()
    assert attempts_used == 0
    with closing(owned.connection.execute(
        "SELECT COUNT(*) FROM operation_attempts WHERE run_id=?", (run_id,))) as cursor:
        assert cursor.fetchone() == (0,)
    slot = OutputsRepository().grant_read_slot(
        SlotRequest(copy_id, _NOW + 2), new_operation_key(), owned)
    assert slot.kind is DbOutcomeKind.COMPLETED, slot.error
    assert slot.value.outcome in (SlotOutcome.GRANTED, SlotOutcome.HELD)
    repository = OperationRepository()
    evidence = EvidenceRegistry((
        EvidenceContract("read_returned", 1, "read", frozenset()),))
    for no in range(1, count + 1):
        granted = repository.begin_attempt(AttemptIntent(
            operation="read", action_id=action_id, kind=OperationKind.READ_FILE,
            target=AttemptTarget(copy_id=copy_id), query_purpose=None,
            config=AttemptConfig(5, Decimal("10"), Decimal("0")),
            occurred_at=_NOW + 2, copy_round=copy_round,
        ), new_operation_key(), owned)
        assert granted.kind is DbOutcomeKind.COMPLETED, granted.error
        assert granted.value.disposition is BeginDisposition.GRANTED
        ticket = granted.value.ticket
        assert ticket.run_id == run_id
        with closing(owned.connection.execute(
            "SELECT attempt_no FROM operation_attempts WHERE run_id=? AND status=1",
            (run_id,))) as cursor:
            assert cursor.fetchall() == [(no,)]
        failed = CallOutcome(
            status=AttemptStatus.FAILED, error=ErrorValue("device_error", "read"),
            effect=EffectState.UNKNOWN,
            settlement=Settlement(
                SettlementBasis.OBSERVED, EvidenceValue("read_returned", 1, {})),
        )
        saved = repository.finish_attempt(AttemptFinish(
            ticket, validate_outcome(ticket, failed, evidence), _NOW + 2,
            retry_wait=True,
        ), new_operation_key(), owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        assert saved.value.disposition is FinishDisposition.SAVED
        assert saved.value.attempt_status is AttemptStatus.FAILED
        assert saved.value.run_status is RunStatus.ACTIVE
    with closing(owned.connection.execute(
        "SELECT attempts_used,max_attempts_used,retry_wait_required FROM operation_runs"
        " WHERE id=?", (run_id,))) as cursor:
        assert cursor.fetchone() == (count, 5, 1)


def _context(owned, roots, occurred_at=_NOW + 1):
    return CopyContext(
        repository=OutputsRepository(), owned=owned, roots=roots, occurred_at=occurred_at,
    )


def _reset_events(owned):
    with closing(owned.connection.execute(
        "SELECT COUNT(*) FROM history_events WHERE event_type=22"
        " AND json_extract(body_json, '$.reason')=5",
    )) as cursor:
        return cursor.fetchone()[0]


# ---- 续传分区 ----


def test_prepare_creates_missing_target_from_zero(copy_env):
    owned, roots, first = copy_env
    step = asyncio.run(prepare_copy(first.copy_id, _context(owned, roots)))
    assert step.decision.outcome is ResumeOutcome.CREATE
    assert step.decision.offset == 0
    assert step.attempt.plan is AttemptPlan.NEW_REQUIRED
    assert _target_path(roots, owned, first.target_file_id).stat().st_size == 0


def test_prepare_truncates_unconfirmed_tail_then_continues(copy_env):
    """多中断恢复：截去未确认尾部后从可靠进度继续，字节不被重写。"""
    owned, roots, first = copy_env
    path = _target_path(roots, owned, first.target_file_id)
    path.write_bytes(b"abcde")
    _seed_progress(owned, first.copy_id, 3)
    step = asyncio.run(prepare_copy(first.copy_id, _context(owned, roots)))
    assert step.decision.outcome is ResumeOutcome.TRUNCATE
    assert step.decision.truncate_to == 3
    assert step.decision.offset == 3
    assert path.read_bytes() == b"abc"
    again = asyncio.run(prepare_copy(first.copy_id, _context(owned, roots, _NOW + 2)))
    assert again.decision.outcome is ResumeOutcome.CONTINUE
    assert again.decision.offset == 3
    assert path.read_bytes() == b"abc"


def test_prepare_continues_in_place_without_rewriting(copy_env):
    owned, roots, first = copy_env
    path = _target_path(roots, owned, first.target_file_id)
    path.write_bytes(b"abc")
    _seed_progress(owned, first.copy_id, 3)
    before = path.read_bytes()
    step = asyncio.run(prepare_copy(first.copy_id, _context(owned, roots)))
    assert step.decision.outcome is ResumeOutcome.CONTINUE
    assert path.read_bytes() == before


def test_sync_failure_blocks_resume_without_progress_change(copy_env, monkeypatch):
    """截断后同步失败：保留阶段与诊断，不确认尾部、不改可靠进度。"""
    from camctl.host_files import io as host_io

    owned, roots, first = copy_env
    path = _target_path(roots, owned, first.target_file_id)
    path.write_bytes(b"abcde")
    _seed_progress(owned, first.copy_id, 3)

    def _refuse(fd: int) -> None:
        raise OSError("sync refused")

    monkeypatch.setattr(host_io, "_fsync", _refuse)
    with pytest.raises(CopyPreparationError) as caught:
        asyncio.run(prepare_copy(first.copy_id, _context(owned, roots)))
    assert caught.value.stage == "target_sync_failed"
    monkeypatch.undo()
    committed = owned.connection.execute(
        "SELECT committed_bytes FROM file_copies WHERE id=?", (first.copy_id,)).fetchone()[0]
    assert committed == 3
    # 截断的物理效果允许存在；文件现状由下一次准备重新观察并分类。
    assert path.stat().st_size <= 5


def test_complete_target_enters_verification_without_writes(copy_env):
    owned, roots, first = copy_env
    size = owned.connection.execute(
        "SELECT source_size FROM file_copies WHERE id=?", (first.copy_id,)).fetchone()[0]
    path = _target_path(roots, owned, first.target_file_id)
    path.write_bytes(b"x" * size)
    _seed_progress(owned, first.copy_id, size)
    events_before = _reset_events(owned)
    step = asyncio.run(prepare_copy(first.copy_id, _context(owned, roots)))
    assert step.decision.outcome is ResumeOutcome.VERIFY
    assert step.decision.offset is None
    assert path.stat().st_size == size
    assert _reset_events(owned) == events_before


def test_missing_target_with_confirmed_progress_keeps_facts(copy_env):
    owned, roots, first = copy_env
    _seed_progress(owned, first.copy_id, 3)
    before = tuple(owned.connection.iterdump())
    with pytest.raises(ConsistencyError):
        asyncio.run(prepare_copy(first.copy_id, _context(owned, roots)))
    assert tuple(owned.connection.iterdump()) == before


def test_directory_at_target_path_is_not_a_missing_file(copy_env):
    """路径被目录占用不是缺失，也不能当作进度为零继续。"""
    owned, roots, first = copy_env
    _target_path(roots, owned, first.target_file_id).mkdir()
    with pytest.raises(ConsistencyError):
        asyncio.run(prepare_copy(first.copy_id, _context(owned, roots)))


def test_changed_device_source_identity_rejects_preparation(copy_env):
    """源文件固定长度与建档事实不一致：不猜测原因，按一致性错误拒绝。"""
    owned, roots, first = copy_env
    owned.connection.execute(
        "UPDATE device_files SET size_bytes=8192 WHERE id=501")
    owned.connection.commit()
    with pytest.raises(ConsistencyError):
        asyncio.run(prepare_copy(first.copy_id, _context(owned, roots)))


def test_changed_host_source_identity_rejects_preparation(local_read, tmp_path):
    owned, command = local_read
    qualification = _qualified(owned, command)
    roots = _make_roots(tmp_path)
    owned.connection.execute(
        "UPDATE intermediate_files SET size_bytes=8192 WHERE id=801")
    owned.connection.commit()
    with pytest.raises(ConsistencyError):
        asyncio.run(prepare_copy(qualification.copy_id, _context(owned, roots)))


def test_same_entry_prepares_internal_and_delivery_copies(copy_env):
    """取回交付与录像内部输入通过同一准备入口，目标按各自用途定位。"""
    owned, roots, first = copy_env
    step = asyncio.run(prepare_copy(first.copy_id, _context(owned, roots)))
    purpose = owned.connection.execute(
        "SELECT purpose FROM intermediate_files WHERE id=?", (first.target_file_id,)).fetchone()[0]
    expected = (FilePurpose.DELIVERY_COPY if purpose == 1 else FilePurpose.RECORDING_INPUT)
    assert _relative(owned, first.target_file_id).split("/")[0] == (
        "deliveries" if expected is FilePurpose.DELIVERY_COPY else "recording-inputs")
    assert step.decision.outcome is ResumeOutcome.CREATE


def test_host_source_copy_preparation_uses_local_facts(local_read, tmp_path):
    """主机源拷贝不占相机机会，同一入口按本地事实准备。"""
    owned, command = local_read
    qualification = _qualified(owned, command)
    roots = _make_roots(tmp_path)
    step = asyncio.run(prepare_copy(qualification.copy_id, _context(owned, roots)))
    assert step.decision.outcome is ResumeOutcome.CREATE
    assert step.decision.offset == 0
    assert _target_path(roots, owned, qualification.target_file_id).stat().st_size == 0


# ---- 读取尝试计划 ----


def test_in_flight_attempt_is_resumed_not_duplicated(copy_env):
    """未保存读取失败的重启沿原尝试，不重复登记意图或次数。"""
    owned, roots, first = copy_env
    OutputsRepository().grant_read_slot(
        SlotRequest(first.copy_id, _NOW + 1), new_operation_key(), owned)
    intent = AttemptIntent(
        operation="read", action_id=_run_action(owned, first.run_id),
        kind=OperationKind.READ_FILE,
        target=AttemptTarget(copy_id=first.copy_id), query_purpose=None,
        config=AttemptConfig(3, Decimal("10"), Decimal("0")), occurred_at=_NOW + 2,
        copy_round=1,
    )
    granted = OperationRepository().begin_attempt(intent, new_operation_key(), owned)
    assert granted.kind is DbOutcomeKind.COMPLETED, granted.error
    before = tuple(owned.connection.iterdump())
    step = asyncio.run(prepare_copy(first.copy_id, _context(owned, roots, _NOW + 3)))
    assert step.attempt.plan is AttemptPlan.RESUME_EXISTING
    assert step.attempt.attempt_id == granted.value.ticket.attempt_id
    assert tuple(owned.connection.iterdump()) == before


def test_failed_attempt_requires_new_legal_attempt(copy_env):
    """原额度 5 下三次失败后，本次额度降为 3，不复活或新增尝试。"""
    owned, roots, first = copy_env
    _seed_failed_attempts(owned, first.run_id, 3)
    with closing(owned.connection.execute(
        "SELECT * FROM operation_attempts WHERE run_id=? ORDER BY attempt_no",
        (first.run_id,))) as cursor:
        attempts_before = cursor.fetchall()
    step = asyncio.run(prepare_copy(first.copy_id, _context(owned, roots, _NOW + 3)))
    assert step.attempt.plan is AttemptPlan.NEW_REQUIRED
    intent = AttemptIntent(
        operation="read", action_id=_run_action(owned, first.run_id),
        kind=OperationKind.READ_FILE,
        target=AttemptTarget(copy_id=first.copy_id), query_purpose=None,
        config=AttemptConfig(3, Decimal("10"), Decimal("0")), occurred_at=_NOW + 4,
        copy_round=1,
    )
    denied = OperationRepository().begin_attempt(intent, new_operation_key(), owned)
    assert denied.kind is DbOutcomeKind.COMPLETED, denied.error
    assert denied.value.disposition is BeginDisposition.REJECTED
    assert denied.value.reason == "budget_exhausted"
    with closing(owned.connection.execute(
        "SELECT attempts_used,max_attempts_used,status,retry_wait_required"
        " FROM operation_runs WHERE id=?", (first.run_id,))) as cursor:
        assert cursor.fetchone() == (3, 3, 4, 0)
    with closing(owned.connection.execute(
        "SELECT * FROM operation_attempts WHERE run_id=? ORDER BY attempt_no",
        (first.run_id,))) as cursor:
        assert cursor.fetchall() == attempts_before


def test_attempts_of_earlier_round_do_not_resume(copy_env):
    owned, roots, first = copy_env
    _seed_failed_attempts(owned, first.run_id, 2)
    owned.connection.execute(
        "UPDATE file_copies SET round=3, recopies_used=2 WHERE id=?", (first.copy_id,))
    owned.connection.commit()
    step = asyncio.run(prepare_copy(first.copy_id, _context(owned, roots, _NOW + 3)))
    assert step.attempt.plan is AttemptPlan.NEW_REQUIRED


# ---- 目标重置 ----


def test_reset_pending_truncates_and_saves_reset_event(copy_env):
    """重置意图登记后：截断旧轮数据、同步并保存重置完成事实。"""
    owned, roots, first = copy_env
    path = _target_path(roots, owned, first.target_file_id)
    path.write_bytes(b"old-round")
    _seed_reset_pending(owned, first.copy_id)
    step = asyncio.run(prepare_copy(first.copy_id, _context(owned, roots)))
    assert step.decision.outcome is ResumeOutcome.RESET_TARGET
    assert step.decision.offset == 0
    assert step.reset is TargetResetOutcome.COMPLETED
    assert path.stat().st_size == 0
    assert owned.connection.execute(
        "SELECT reset_state FROM file_copies WHERE id=?", (first.copy_id,)).fetchone()[0] == 1
    assert _reset_events(owned) == 1


def test_reset_recovers_after_truncate_before_save(copy_env):
    """截断已落盘、重置事实未保存的恢复：幂等重做并保存完成。"""
    owned, roots, first = copy_env
    path = _target_path(roots, owned, first.target_file_id)
    path.write_bytes(b"")
    _seed_reset_pending(owned, first.copy_id)
    step = asyncio.run(prepare_copy(first.copy_id, _context(owned, roots)))
    assert step.reset is TargetResetOutcome.COMPLETED
    assert owned.connection.execute(
        "SELECT reset_state FROM file_copies WHERE id=?", (first.copy_id,)).fetchone()[0] == 1
    assert _reset_events(owned) == 1


def test_reset_already_ready_is_read_only(copy_env):
    owned, roots, first = copy_env
    before = tuple(owned.connection.iterdump())
    result = OutputsRepository().reset_copy_target(
        TargetResetRequest(first.copy_id, _NOW + 1), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is TargetResetOutcome.ALREADY_READY
    assert tuple(owned.connection.iterdump()) == before


def test_reset_key_returns_first_response_and_rejects_changed_input(copy_env):
    owned, roots, first = copy_env
    _seed_reset_pending(owned, first.copy_id)
    repository = OutputsRepository()
    key = new_operation_key()
    first_call = repository.reset_copy_target(
        TargetResetRequest(first.copy_id, _NOW + 1), key, owned)
    assert first_call.value.outcome is TargetResetOutcome.COMPLETED
    before = tuple(owned.connection.iterdump())
    again = repository.reset_copy_target(
        TargetResetRequest(first.copy_id, _NOW + 1), key, owned)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert again.value.outcome is TargetResetOutcome.COMPLETED
    assert tuple(owned.connection.iterdump()) == before
    changed = repository.reset_copy_target(
        TargetResetRequest(first.copy_id, _NOW + 9), key, owned)
    assert changed.kind is DbOutcomeKind.ROLLED_BACK


# ---- 正式守卫 ----


def _reset_proposal(owned, copy_id: int, occurred_at: int = _NOW + 1):
    owned.connection.execute("BEGIN")
    try:
        plan = outputs._CopyResetCommand(
            TargetResetRequest(copy_id, occurred_at), new_operation_key(),
        ).plan(TransactionScope(owned.connection, 1, 1))
    finally:
        owned.connection.rollback()
    assert plan.events, "重置提案必须产生事件"
    return plan


def test_reset_guard_accepts_real_event(copy_env):
    owned, roots, first = copy_env
    _seed_reset_pending(owned, first.copy_id)
    plan = _reset_proposal(owned, first.copy_id)
    context = EventContext(
        TransactionRange(2, 2, 2), dict(plan.owners), deepcopy(plan.state_rows),
        read_coverage=plan.read_coverage)
    assert validate_event(plan.events[0], context).branch_name == "RESET"


def test_reset_guard_rejects_nonzero_progress_or_missing_facts(copy_env):
    owned, roots, first = copy_env
    _seed_reset_pending(owned, first.copy_id)
    plan = _reset_proposal(owned, first.copy_id)
    nonzero = deepcopy(plan.state_rows)
    nonzero["file_copies"][first.copy_id]["committed_bytes"] = 2
    context = EventContext(
        TransactionRange(2, 2, 2), dict(plan.owners), nonzero,
        read_coverage=plan.read_coverage)
    with pytest.raises(EventValidationError):
        validate_event(plan.events[0], context)
    empty = {table: dict(rows) for table, rows in deepcopy(plan.state_rows).items()}
    del empty["file_copies"][first.copy_id]
    context = EventContext(
        TransactionRange(2, 2, 2), dict(plan.owners), empty,
        read_coverage=plan.read_coverage)
    with pytest.raises(EventValidationError):
        validate_event(plan.events[0], context)


def test_reset_guard_rejects_ready_start(copy_env):
    """reset_state 已为 READY 的行不能保存重置完成事件。"""
    owned, roots, first = copy_env
    _seed_reset_pending(owned, first.copy_id)
    plan = _reset_proposal(owned, first.copy_id)
    event = plan.events[0]
    row = event.rows[0]
    already_ready = replace(
        row, before=replace(row.before, values={"reset_state": 1}))
    context = EventContext(
        TransactionRange(2, 2, 2), dict(plan.owners), deepcopy(plan.state_rows),
        read_coverage=plan.read_coverage)
    with pytest.raises(EventValidationError):
        validate_event(replace(event, rows=(already_ready,)), context)

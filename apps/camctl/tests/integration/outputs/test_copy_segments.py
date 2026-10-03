"""分段拷贝的真实组合：段传输、可靠进度保存、取消与恢复配置。

真实 SQLite、真实文件与受 SourceStream 契约约束的设备流替身共同
验证[分段规则](../../../architecture/file-copy.md#进度保存的分段大小)：
一文件一次一段、同步成功且资格仍成立才保存进度、提交确认后推进
下一段、取消不为待清理副本新增进度提交。
"""

import asyncio
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.contracts.values import new_operation_key
from camctl.devices.read_session import ReadSession, SourceFile
from camctl.history.validators import EventContext, EventValidationError, validate_event
from camctl.host_files.models import BoundDirectories
from camctl.operations.attempts import (
    AttemptConfig, AttemptIntent, AttemptTarget, AttemptTicket, BeginDisposition,
    OperationKind, ReadResumeDecision, ReadResumeDisposition, ReadResumeRequest,
)
from camctl.outputs.copy import (
    CopyContext, CopySegmentError, ReliableSegment, SegmentContext, SegmentOutcome,
    SegmentSaveDisposition, copy_next_segment, prepare_copy,
)
from camctl.outputs.slots import SlotRequest
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories import operations, outputs
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.transaction import TransactionScope

from .test_copy_resume import (
    _command, _make_roots, _qualified, _run_action, local_read,  # noqa: F401
)
from .test_qualification import _NOW
from .test_read_associations import read_targets  # noqa: F401

_CONTENT = b"0123456789"


class _ByteStream:
    """受 SourceStream 契约约束的设备流替身：按序返回固定内容。"""

    def __init__(self, content: bytes) -> None:
        self._content = content
        self._position = 0
        self.closed = False

    def read(self, limit: int) -> bytes:
        data = self._content[self._position:self._position + limit]
        self._position += len(data)
        return data

    def cancel(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def _ticket(copy_id: int) -> AttemptTicket:
    return AttemptTicket(
        attempt_id=1, operation="read", target_id=str(copy_id),
        responsibility_key=f"read/{copy_id}", run_id=0,
    )


@pytest.fixture(params=["delivery", "internal"])
def segment_env(request, read_targets, tmp_path):
    """已建档的 10 字节设备源拷贝；交付与录像内部输入共用同一分段入口。"""
    owned = read_targets
    command = _command(internal=request.param == "internal")
    if request.param == "internal":
        owned.connection.execute("DELETE FROM obtain_items")
        owned.connection.execute("DELETE FROM outputs")
    else:
        owned.connection.execute("UPDATE actions SET status=3 WHERE id=11")
    owned.connection.execute("UPDATE device_files SET size_bytes=10 WHERE id=501")
    owned.connection.commit()
    qualification = _qualified(owned, command)
    return owned, _make_roots(tmp_path), qualification


def _committed(owned, copy_id: int) -> int:
    with closing(owned.connection.execute(
        "SELECT committed_bytes FROM file_copies WHERE id=?", (copy_id,),
    )) as cursor:
        return cursor.fetchone()[0]


def _segment_events(owned) -> int:
    with closing(owned.connection.execute(
        "SELECT COUNT(*) FROM history_events WHERE event_type=22"
        " AND json_extract(body_json, '$.reason')=2",
    )) as cursor:
        return cursor.fetchone()[0]


def _target_path(roots: BoundDirectories, owned, file_id: int):
    with closing(owned.connection.execute(
        "SELECT relative_path FROM intermediate_files WHERE id=?", (file_id,),
    )) as cursor:
        return roots.staging / cursor.fetchone()[0]


async def _drive(
    owned, roots, copy_id: int, *, segment_size: int, max_segments: int | None = None,
    occurred_at: int = _NOW + 1, offset: int = 0, prepare: bool = True,
):
    """先按恢复规则准备目标，再用同一读取会话驱动分段。"""
    if prepare:
        await prepare_copy(copy_id, CopyContext(
            repository=OutputsRepository(), owned=owned, roots=roots,
            occurred_at=occurred_at,
        ))
    session = ReadSession(
        SourceFile("501", {}, len(_CONTENT)), offset, _ByteStream(_CONTENT[offset:]),
        Decimal("10"),
    )
    steps = []
    context = SegmentContext(
        repository=OutputsRepository(), owned=owned, roots=roots,
        occurred_at=occurred_at, segment_size=segment_size, session=session,
    )
    try:
        for index in range(max_segments if max_segments is not None else 99):
            step = await copy_next_segment(
                copy_id, replace(context, occurred_at=occurred_at + index))
            steps.append(step)
            if step.plan.outcome is SegmentOutcome.ALL_COMMITTED:
                break
        return steps
    finally:
        session.request_stop()
        await session.wait_stopped()


def test_segments_commit_reliable_progress_until_complete(segment_env):
    """10 字节按 4 字节分段：[0,4)[4,8)[8,10)，段提交后推进下一段。"""
    owned, roots, first = segment_env
    steps = asyncio.run(_drive(owned, roots, first.copy_id, segment_size=4))
    assert [(step.plan.start, step.plan.end, step.plan.final) for step in steps] == [
        (0, 4, False), (4, 8, False), (8, 10, True), (None, None, False),
    ]
    assert all(step.saved.disposition is SegmentSaveDisposition.SAVED for step in steps[:3])
    assert steps[-1].plan.outcome is SegmentOutcome.ALL_COMMITTED
    assert steps[-1].saved is None
    assert _committed(owned, first.copy_id) == 10
    assert _segment_events(owned) == 3
    assert _target_path(roots, owned, first.target_file_id).read_bytes() == _CONTENT


def test_all_committed_does_not_add_empty_segment(segment_env):
    """进度已到源长度：再次推进不新增空进度段，也不产生新事件。"""
    owned, roots, first = segment_env
    asyncio.run(_drive(owned, roots, first.copy_id, segment_size=10))
    events_before = _segment_events(owned)
    step = asyncio.run(_drive(owned, roots, first.copy_id, segment_size=4)).pop()
    assert step.plan.outcome is SegmentOutcome.ALL_COMMITTED
    assert step.saved is None and step.transfer is None
    assert _segment_events(owned) == events_before


def test_changed_segment_size_continues_from_committed_position(segment_env):
    """改用更小段大小后从已确认字节位置继续，不对齐新段大小倍数。"""
    owned, roots, first = segment_env
    steps = asyncio.run(_drive(owned, roots, first.copy_id, segment_size=8, max_segments=1))
    assert (steps[0].plan.start, steps[0].plan.end) == (0, 8)
    assert _committed(owned, first.copy_id) == 8
    steps = asyncio.run(_drive(
        owned, roots, first.copy_id, segment_size=6, max_segments=1, offset=8))
    assert (steps[0].plan.start, steps[0].plan.end) == (8, 10)
    assert steps[0].saved.committed_bytes == 10


def test_segment_transfer_keeps_existing_attempts(segment_env):
    """段保存不新建读取尝试、不改次数：在途尝试身份保持不变。"""
    owned, roots, first = segment_env
    OutputsRepository().grant_read_slot(
        SlotRequest(first.copy_id, _NOW + 1), new_operation_key(), owned)
    intent = AttemptIntent(
        operation="read", action_id=_run_action(owned, first.run_id),
        kind=OperationKind.READ_FILE, target=AttemptTarget(copy_id=first.copy_id),
        query_purpose=None, config=AttemptConfig(3, Decimal("10"), Decimal("0")),
        occurred_at=_NOW + 2, copy_round=1,
    )
    granted = OperationRepository().begin_attempt(intent, new_operation_key(), owned)
    assert granted.kind is DbOutcomeKind.COMPLETED, granted.error
    assert granted.value.disposition is BeginDisposition.GRANTED
    before = owned.connection.execute(
        "SELECT COUNT(*), MAX(attempt_no) FROM operation_attempts WHERE run_id=?",
        (first.run_id,)).fetchone()
    asyncio.run(_drive(owned, roots, first.copy_id, segment_size=4))
    after = owned.connection.execute(
        "SELECT COUNT(*), MAX(attempt_no) FROM operation_attempts WHERE run_id=?",
        (first.run_id,)).fetchone()
    assert after == before


def test_transfer_success_but_canceled_owner_skips_progress_commit(segment_env):
    """段传输成功返回但普通取回已取消：不为待清理副本新增进度提交。"""
    owned, roots, first = segment_env
    action_id = _run_action(owned, first.run_id)
    owned.connection.execute(
        "UPDATE actions SET status=6, cancel_requested=1 WHERE id=?", (action_id,))
    owned.connection.commit()
    steps = asyncio.run(_drive(owned, roots, first.copy_id, segment_size=4, max_segments=1))
    step = steps[0]
    assert step.saved.disposition is SegmentSaveDisposition.SKIPPED
    assert step.saved.reason == "cancel_requested"
    assert _committed(owned, first.copy_id) == 0
    assert _segment_events(owned) == 0
    # 物理写入及同步已发生；未确认尾部由下一次准备重新观察并截断。
    assert _target_path(roots, owned, first.target_file_id).stat().st_size == 4


def test_finished_owner_without_cancel_also_skips(segment_env):
    """动作已终态但未取消：同样不推进可靠进度，原因分区不同。"""
    owned, roots, first = segment_env
    action_id = _run_action(owned, first.run_id)
    owned.connection.execute(
        "UPDATE actions SET status=4, cancel_requested=0, error_code=10,"
        " error_details_json='{}' WHERE id=?", (action_id,))
    owned.connection.commit()
    steps = asyncio.run(_drive(owned, roots, first.copy_id, segment_size=4, max_segments=1))
    assert steps[0].saved.disposition is SegmentSaveDisposition.SKIPPED
    assert steps[0].saved.reason == "owner_not_running"
    assert _committed(owned, first.copy_id) == 0


def test_sync_failure_keeps_progress_and_reports_stage(segment_env, monkeypatch):
    """段尾同步失败：保留阶段与诊断，可靠进度不变，文件尾部未确认。"""
    from camctl.host_files import io as host_io

    owned, roots, first = segment_env
    asyncio.run(prepare_copy(first.copy_id, CopyContext(
        repository=OutputsRepository(), owned=owned, roots=roots, occurred_at=_NOW + 1)))

    def _refuse(fd: int) -> None:
        raise OSError("sync refused")

    monkeypatch.setattr(host_io, "_fsync", _refuse)
    with pytest.raises(CopySegmentError) as caught:
        asyncio.run(_drive(
            owned, roots, first.copy_id, segment_size=4, max_segments=1, prepare=False))
    assert caught.value.stage == "segment_sync_failed"
    monkeypatch.undo()
    assert _committed(owned, first.copy_id) == 0
    assert _segment_events(owned) == 0


def test_stop_notice_halt_between_chunks(segment_env):
    """停止通知在小块之间生效：不再新增读写与同步，进度保持。"""
    import threading as _threading

    owned, roots, first = segment_env

    async def _run():
        await prepare_copy(first.copy_id, CopyContext(
            repository=OutputsRepository(), owned=owned, roots=roots,
            occurred_at=_NOW + 1,
        ))
        session = ReadSession(
            SourceFile("501", {}, len(_CONTENT)), 0, _ByteStream(_CONTENT),
            Decimal("10"))
        stop = _threading.Event()
        stop.set()
        context = SegmentContext(
            repository=OutputsRepository(), owned=owned, roots=roots,
            occurred_at=_NOW + 1, segment_size=4, session=session, stop=stop,
        )
        try:
            return await copy_next_segment(first.copy_id, context)
        finally:
            session.request_stop()
            await session.wait_stopped()

    with pytest.raises(CopySegmentError) as caught:
        asyncio.run(_run())
    assert caught.value.stage == "segment_transfer_failed"
    assert _committed(owned, first.copy_id) == 0
    assert _segment_events(owned) == 0


def test_unknown_commit_does_not_advance(segment_env, monkeypatch):
    """段保存提交结果未知：编排停止推进，不安排下一段。"""
    owned, roots, first = segment_env

    def _unknown(self, command, key, owned_connection):
        return DbOutcome(kind=DbOutcomeKind.UNKNOWN, error=RuntimeError("提交未知"))

    monkeypatch.setattr(OutputsRepository, "save_segment", _unknown)
    with pytest.raises(CopySegmentError) as caught:
        asyncio.run(_drive(owned, roots, first.copy_id, segment_size=4, max_segments=3))
    assert caught.value.stage == "segment_save_unknown"
    monkeypatch.undo()
    # 未知不回滚已提交事实：可靠进度与事件由恢复按数据库实际提交确认。
    committed = _committed(owned, first.copy_id)
    assert committed in (0, 4)


def test_source_early_eof_reports_read_failure(segment_env):
    """源提前结束不构成合法最后短段：按读取失败保留进度。"""
    owned, roots, first = segment_env
    async def _run():
        await prepare_copy(first.copy_id, CopyContext(
            repository=OutputsRepository(), owned=owned, roots=roots,
            occurred_at=_NOW + 1,
        ))
        # 只提供 2 字节的流：请求更多时不再有数据，经无数据超时结束尝试。
        session = ReadSession(
            SourceFile("501", {}, 10), 0, _ByteStream(b"01"), Decimal("0.01"))
        context = SegmentContext(
            repository=OutputsRepository(), owned=owned, roots=roots,
            occurred_at=_NOW + 1, segment_size=4, session=session,
        )
        try:
            return await copy_next_segment(first.copy_id, context)
        finally:
            session.request_stop()
            await session.wait_stopped()
    with pytest.raises(CopySegmentError) as caught:
        asyncio.run(_run())
    assert caught.value.stage == "segment_transfer_failed"
    assert _committed(owned, first.copy_id) == 0
    assert _segment_events(owned) == 0


# ---- 保存事务与原键 ----


def _segment_command(copy_id: int, before: int, end: int, occurred_at=_NOW + 1):
    return ReliableSegment(
        copy_id=copy_id, copy_round=1, committed_before=before,
        segment_end=end, synced=True, occurred_at=occurred_at,
    )


def test_save_segment_key_recovers_first_response(segment_env):
    owned, roots, first = segment_env
    repository = OutputsRepository()
    key = new_operation_key()
    outcome = repository.save_segment(
        _segment_command(first.copy_id, 0, 4), key, owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    assert outcome.value.disposition is SegmentSaveDisposition.SAVED
    assert outcome.value.committed_bytes == 4
    before = tuple(owned.connection.iterdump())
    again = repository.save_segment(
        _segment_command(first.copy_id, 0, 4), key, owned)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert again.value.committed_bytes == 4
    assert tuple(owned.connection.iterdump()) == before
    changed = repository.save_segment(
        _segment_command(first.copy_id, 0, 8), key, owned)
    assert changed.kind is DbOutcomeKind.ROLLED_BACK


def test_save_segment_rejects_stale_or_foreign_progress(segment_env):
    """旧进度、轮次或范围与当前拷贝不符：拒绝保存并保持数据库事实。"""
    owned, roots, first = segment_env
    repository = OutputsRepository()
    # 旧进度与当前 committed_bytes 不一致（他方已推进）。
    stale = repository.save_segment(
        _segment_command(first.copy_id, 4, 8), new_operation_key(), owned)
    assert stale.kind is DbOutcomeKind.ROLLED_BACK
    # 范围越过固定源长度。
    overflow = repository.save_segment(
        _segment_command(first.copy_id, 0, 11), new_operation_key(), owned)
    assert overflow.kind is DbOutcomeKind.ROLLED_BACK
    # 轮次与当前拷贝轮次不一致。
    round_mismatch = repository.save_segment(
        replace(_segment_command(first.copy_id, 0, 4), copy_round=2),
        new_operation_key(), owned)
    assert round_mismatch.kind is DbOutcomeKind.ROLLED_BACK
    assert _committed(owned, first.copy_id) == 0
    assert _segment_events(owned) == 0


# ---- 正式守卫 ----


def _segment_proposal(owned, copy_id: int, before: int, end: int):
    owned.connection.execute("BEGIN")
    try:
        plan = outputs._SegmentSaveCommand(
            _segment_command(copy_id, before, end), new_operation_key(),
        ).plan(TransactionScope(owned.connection, 1, 1))
    finally:
        owned.connection.rollback()
    assert plan.events, "段保存提案必须产生事件"
    return plan


def test_segment_guard_accepts_real_event(segment_env):
    owned, roots, first = segment_env
    plan = _segment_proposal(owned, first.copy_id, 0, 4)
    context = EventContext(
        TransactionRange(2, 2, 2), dict(plan.owners), plan.state_rows,
        read_coverage=plan.read_coverage)
    assert validate_event(plan.events[0], context).branch_name == "SEGMENT"


def test_segment_guard_rejects_empty_or_overflowing_advance(segment_env):
    owned, roots, first = segment_env
    plan = _segment_proposal(owned, first.copy_id, 0, 4)
    event = plan.events[0]
    for after_value in (0, 11):
        row = event.rows[0]
        variant = replace(
            row, after=replace(row.after, values={
                "committed_bytes": after_value}))
        context = EventContext(
            TransactionRange(2, 2, 2), dict(plan.owners), plan.state_rows,
            read_coverage=plan.read_coverage)
        with pytest.raises(EventValidationError):
            validate_event(replace(event, rows=(variant,)), context)


# ---- 主机源 ----


def test_local_source_segments_copy_bytes(local_read, tmp_path):
    """主机修复产物源经同一分段入口拷贝，字节与源逐段一致。"""
    owned, command = local_read
    qualification = _qualified(owned, command)
    roots = _make_roots(tmp_path)
    source_path = roots.staging / "derived" / "801.mp4"
    content = bytes(range(256)) * 16  # 4096 字节
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(content)
    from camctl.outputs.copy import LocalCopySource

    async def _run():
        await prepare_copy(qualification.copy_id, CopyContext(
            repository=OutputsRepository(), owned=owned, roots=roots,
            occurred_at=_NOW + 1,
        ))
        source = LocalCopySource(source_path, 0)
        context = SegmentContext(
            repository=OutputsRepository(), owned=owned, roots=roots,
            occurred_at=_NOW + 1, segment_size=1500, session=source,
        )
        try:
            steps = []
            for index in range(10):
                step = await copy_next_segment(
                    qualification.copy_id, replace(context, occurred_at=_NOW + 1 + index))
                steps.append(step)
                if step.plan.outcome is SegmentOutcome.ALL_COMMITTED:
                    break
            return steps
        finally:
            source.close()

    steps = asyncio.run(_run())
    assert [(step.plan.start, step.plan.end) for step in steps if step.transfer] == [
        (0, 1500), (1500, 3000), (3000, 4096),
    ]
    target = _target_path(roots, owned, qualification.target_file_id)
    assert target.read_bytes() == content
    assert _committed(owned, qualification.copy_id) == 4096


# ---- 恢复在途读取尝试的配置 ----


def _in_flight_attempt(owned, first):
    slot = OutputsRepository().grant_read_slot(
        SlotRequest(first.copy_id, _NOW + 1), new_operation_key(), owned)
    assert slot.kind is DbOutcomeKind.COMPLETED and slot.value.outcome.name == "GRANTED", slot.value
    intent = AttemptIntent(
        operation="read", action_id=_run_action(owned, first.run_id),
        kind=OperationKind.READ_FILE, target=AttemptTarget(copy_id=first.copy_id),
        query_purpose=None, config=AttemptConfig(3, Decimal("10"), Decimal("0")),
        occurred_at=_NOW + 2, copy_round=1,
    )
    granted = OperationRepository().begin_attempt(intent, new_operation_key(), owned)
    assert granted.kind is DbOutcomeKind.COMPLETED, granted.error
    assert granted.value.disposition is BeginDisposition.GRANTED, granted.value.reason
    run_id = granted.value.ticket.run_id
    attempt_row = owned.connection.execute(
        "SELECT id FROM operation_attempts WHERE run_id=? AND attempt_no=1",
        (run_id,)).fetchone()[0]
    return granted.value.ticket, run_id, attempt_row


def test_resume_read_saves_current_run_config(segment_env):
    """重启后沿原在途尝试继续：本次运行的配置写入原尝试行。"""
    owned, roots, first = segment_env
    ticket, run_id, attempt_row = _in_flight_attempt(owned, first)
    request = ReadResumeRequest(
        ticket=ticket,
        config=AttemptConfig(5, Decimal("30"), Decimal("2")),
        occurred_at=_NOW + 5,
    )
    outcome = OperationRepository().resume_read(request, new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    assert outcome.value.disposition is ReadResumeDisposition.APPLIED
    assert owned.connection.execute(
        "SELECT max_attempts_used, timeout_s_json, retry_interval_s_json, status"
        " FROM operation_attempts WHERE id=?", (attempt_row,),
    ).fetchone() == (5, "30", "2", 1)
    with closing(owned.connection.execute(
        "SELECT COUNT(*) FROM history_events WHERE event_type=12"
        " AND json_extract(body_json, '$.reason')=4",
    )) as cursor:
        assert cursor.fetchone()[0] == 1


def test_resume_read_same_config_is_read_only(segment_env):
    owned, roots, first = segment_env
    ticket, run_id, _attempt_row = _in_flight_attempt(owned, first)
    request = ReadResumeRequest(
        ticket=ticket,
        config=AttemptConfig(3, Decimal("10"), Decimal("0")),
        occurred_at=_NOW + 5,
    )
    before = tuple(owned.connection.iterdump())
    outcome = OperationRepository().resume_read(request, new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    assert outcome.value.disposition is ReadResumeDisposition.UNCHANGED
    assert tuple(owned.connection.iterdump()) == before


def test_resume_read_finished_attempt_reports_not_running(segment_env):
    owned, roots, first = segment_env
    ticket, run_id, attempt_row = _in_flight_attempt(owned, first)
    owned.connection.execute(
        'UPDATE operation_attempts SET status=3, result_event_id=1,'
        " error_json='{\"reason\": \"seed\"}' WHERE id=?", (attempt_row,))
    owned.connection.commit()
    request = ReadResumeRequest(
        ticket=ticket,
        config=AttemptConfig(5, Decimal("30"), Decimal("2")),
        occurred_at=_NOW + 5,
    )
    outcome = OperationRepository().resume_read(request, new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    assert outcome.value.disposition is ReadResumeDisposition.NOT_RUNNING
    assert owned.connection.execute(
        "SELECT max_attempts_used FROM operation_attempts WHERE id=?",
        (attempt_row,)).fetchone()[0] == 3


def test_resume_read_key_recovers_first_response(segment_env):
    owned, roots, first = segment_env
    ticket, run_id, _attempt_row = _in_flight_attempt(owned, first)
    request = ReadResumeRequest(
        ticket=ticket,
        config=AttemptConfig(5, Decimal("30"), Decimal("2")),
        occurred_at=_NOW + 5,
    )
    repository = OperationRepository()
    key = new_operation_key()
    first_outcome = repository.resume_read(request, key, owned)
    assert first_outcome.value.disposition is ReadResumeDisposition.APPLIED
    before = tuple(owned.connection.iterdump())
    again = repository.resume_read(request, key, owned)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert again.value.disposition is ReadResumeDisposition.APPLIED
    assert tuple(owned.connection.iterdump()) == before


def test_resume_read_guard_rejects_non_read_flow(segment_env):
    """恢复配置事件只属于读取尝试；流程类型不符被正式守卫拒绝。"""
    owned, roots, first = segment_env
    ticket, run_id, attempt_row = _in_flight_attempt(owned, first)
    request = ReadResumeRequest(
        ticket=ticket,
        config=AttemptConfig(5, Decimal("30"), Decimal("2")),
        occurred_at=_NOW + 5,
    )
    owned.connection.execute("BEGIN")
    try:
        plan = operations._ReadResumeCommand(
            request, new_operation_key(),
        ).plan(TransactionScope(owned.connection, 1, 1))
    finally:
        owned.connection.rollback()
    context = EventContext(
        TransactionRange(2, 2, 2), dict(plan.owners), plan.state_rows,
        read_coverage=plan.read_coverage)
    assert validate_event(plan.events[0], context).branch_name == "RESUME_READ"
    foreign = deepcopy(plan.state_rows)
    foreign["operation_runs"][run_id]["kind"] = 1
    with pytest.raises(EventValidationError):
        validate_event(
            plan.events[0],
            EventContext(TransactionRange(2, 2, 2), dict(plan.owners), foreign,
                         read_coverage=plan.read_coverage))

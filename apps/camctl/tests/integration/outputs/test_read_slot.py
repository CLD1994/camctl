"""相机读取机会的授予、等待、释放与恢复；正式守卫核对当前事实。"""

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.history.validators import EventContext, EventValidationError, validate_event
from camctl.operations.attempts import (
    AttemptConfig, AttemptIntent, AttemptTarget, BeginDisposition, OperationKind,
)
from camctl.outputs.qualification import FileCandidate, OperationConfig, QualificationOutcome
from camctl.outputs.slots import SlotOutcome, SlotRequest
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories import outputs
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.transaction import TransactionScope

from .test_local_read import local_read  # noqa: F401  主机源建档夹具
from .test_qualification import _NOW
from .test_read_associations import read_targets, _command


def _prepare(owned, command):
    first = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    assert first.value.outcome is QualificationOutcome.GRANTED
    return first.value


def _slot(copy_id: int, occurred_at: int = _NOW + 1) -> SlotRequest:
    return SlotRequest(copy_id=copy_id, occurred_at=occurred_at)


def _dump(owned):
    return tuple(owned.connection.iterdump())


def _slot_of(owned, copy_id: int):
    return owned.connection.execute(
        "SELECT slot_device_id FROM file_copies WHERE id=?", (copy_id,)).fetchone()[0]


def _slot_history(owned):
    return owned.connection.execute(
        "SELECT transaction_id, event_type, json_extract(body_json, '$.reason')"
        " FROM history_events WHERE event_type=22"
        " AND json_extract(body_json, '$.reason')=6").fetchall()


@pytest.fixture(params=["device", "internal"])
def prepared(request, read_targets):
    """设备来源的已建档拷贝；主机源单独在专用用例覆盖。"""
    owned = read_targets
    command = _command(internal=request.param == "internal")
    if request.param == "internal":
        owned.connection.execute("DELETE FROM obtain_items")
        owned.connection.execute("DELETE FROM outputs")
    else:
        owned.connection.execute("UPDATE actions SET status=3 WHERE id=11")
    owned.connection.commit()
    return owned, command, _prepare(owned, command)


def _later_read_command():
    return FileCandidate(
        action_id=32, item_id=102, processing_id=None, output_id=702,
        source_device_file_id=502, target_extension="part",
        delivery_extension="mp4", delivery_display_name="录像二",
        config=OperationConfig(3, Decimal("10"), Decimal("0")), occurred_at=_NOW,
    )


@pytest.fixture
def two_copies(read_targets):
    owned = read_targets
    owned.connection.execute("UPDATE actions SET status=3 WHERE id=12")
    owned.connection.commit()
    early = _prepare(owned, _command())
    late = _prepare(owned, _later_read_command())
    return owned, early, late


# ---- 授予 ----


def test_grant_saves_slot_and_history(prepared):
    owned, command, first = prepared
    result = OutputsRepository().grant_read_slot(_slot(first.copy_id), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is SlotOutcome.GRANTED, result.value
    assert _slot_of(owned, first.copy_id) == "cam-1"
    assert len(_slot_history(owned)) == 1
    body = owned.connection.execute(
        "SELECT body_json FROM history_events WHERE event_type=22"
        " AND json_extract(body_json, '$.reason')=6").fetchone()[0]
    assert '"slot_device_id"' in body


def test_grant_reuses_held_slot_without_new_events(prepared):
    owned, command, first = prepared
    OutputsRepository().grant_read_slot(_slot(first.copy_id), new_operation_key(), owned)
    before = _dump(owned)
    result = OutputsRepository().grant_read_slot(_slot(first.copy_id), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is SlotOutcome.HELD
    assert _dump(owned) == before


def test_grant_waits_while_another_copy_holds(two_copies):
    owned, early, late = two_copies
    OutputsRepository().grant_read_slot(_slot(early.copy_id), new_operation_key(), owned)
    before = _dump(owned)
    result = OutputsRepository().grant_read_slot(_slot(late.copy_id), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is SlotOutcome.WAIT
    assert result.value.reason == "device_busy"
    assert _dump(owned) == before


def test_grant_uses_registered_candidate_order(two_copies):
    """同设备合格候选按统一文件顺序竞争；用途不增加优先级。"""
    owned, early, late = two_copies
    late_first = OutputsRepository().grant_read_slot(
        _slot(late.copy_id), new_operation_key(), owned)
    assert late_first.value.outcome is SlotOutcome.WAIT, late_first.value
    assert late_first.value.reason == "predecessor"
    granted = OutputsRepository().grant_read_slot(
        _slot(early.copy_id), new_operation_key(), owned)
    assert granted.value.outcome is SlotOutcome.GRANTED, granted.value


def test_grant_order_follows_action_time_not_request_time(two_copies):
    """较晚申请者的发起动作时间更早时取得机会。"""
    owned, early, late = two_copies
    owned.connection.execute(
        "UPDATE actions SET scheduled_at=? WHERE id=32", (_NOW - 10_000_000,))
    owned.connection.commit()
    result = OutputsRepository().grant_read_slot(_slot(late.copy_id), new_operation_key(), owned)
    assert result.value.outcome is SlotOutcome.GRANTED, result.value
    # 较早候选后到时由当前持有者优先，不因排序打断已开始的拷贝责任。
    later = OutputsRepository().grant_read_slot(_slot(early.copy_id), new_operation_key(), owned)
    assert later.value.outcome is SlotOutcome.WAIT
    assert later.value.reason == "device_busy"


def test_grant_rejects_foreign_slot(prepared):
    owned, command, first = prepared
    owned.connection.execute(
        "UPDATE file_copies SET slot_device_id='another-camera' WHERE id=?", (first.copy_id,))
    owned.connection.commit()
    before = _dump(owned)
    result = OutputsRepository().grant_read_slot(_slot(first.copy_id), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result
    assert isinstance(result.error, ConsistencyError), result.error
    assert _dump(owned) == before


def test_host_source_never_occupies_a_slot(local_read):
    """主机源拷贝建档后也不申请相机机会；入口按输入非法拒绝。"""
    owned, command = local_read
    first = _prepare(owned, command)
    assert command.source_device_file_id is None
    before = _dump(owned)
    result = OutputsRepository().grant_read_slot(_slot(first.copy_id), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result
    assert isinstance(result.error, ConsistencyError), result.error
    assert _dump(owned) == before


def test_grant_waits_for_retry_interval(prepared):
    owned, command, first = prepared
    owned.connection.execute(
        "UPDATE operation_runs SET retry_wait_required=1 WHERE copy_id=?", (first.copy_id,))
    owned.connection.commit()
    before = _dump(owned)
    result = OutputsRepository().grant_read_slot(_slot(first.copy_id), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is SlotOutcome.WAIT
    assert result.value.reason == "retry_wait"
    assert _dump(owned) == before


def test_finished_responsibility_needs_no_slot(prepared):
    owned, command, first = prepared
    owned.connection.execute(
        "UPDATE operation_runs SET status=4, error_json='{}' WHERE copy_id=?", (first.copy_id,))
    owned.connection.commit()
    before = _dump(owned)
    result = OutputsRepository().grant_read_slot(_slot(first.copy_id), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is SlotOutcome.FINISHED
    assert _dump(owned) == before


def test_canceled_owner_needs_no_new_slot(prepared):
    owned, command, first = prepared
    owned.connection.execute(
        "UPDATE actions SET cancel_requested=1 WHERE id=?", (command.action_id,))
    owned.connection.commit()
    result = OutputsRepository().grant_read_slot(_slot(first.copy_id), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is SlotOutcome.FINISHED


def test_grant_key_returns_first_response_and_rejects_changed_input(prepared):
    owned, command, first = prepared
    repository = OutputsRepository()
    key = new_operation_key()
    original = repository.grant_read_slot(_slot(first.copy_id), key, owned)
    assert original.value.outcome is SlotOutcome.GRANTED
    OutputsRepository().release_read_slot(_slot(first.copy_id, _NOW + 9), new_operation_key(), owned)
    before = _dump(owned)
    repeated = repository.grant_read_slot(_slot(first.copy_id), key, owned)
    assert repeated.kind is DbOutcomeKind.COMPLETED, repeated.error
    assert repeated.value == original.value
    assert _dump(owned) == before
    changed_time = repository.grant_read_slot(
        SlotRequest(first.copy_id, _NOW + 5), key, owned)
    assert changed_time.kind is DbOutcomeKind.ROLLED_BACK, changed_time
    assert isinstance(changed_time.error, Exception), changed_time.error


def test_slot_key_cannot_be_used_as_file_grant_key(prepared):
    owned, command, first = prepared
    key = new_operation_key()
    OutputsRepository().grant_read_slot(_slot(first.copy_id), key, owned)
    result = OutputsRepository().grant_file(command, key, owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result


# ---- 释放 ----


def test_release_after_bytes_complete(prepared):
    owned, command, first = prepared
    OutputsRepository().grant_read_slot(_slot(first.copy_id), new_operation_key(), owned)
    size = owned.connection.execute(
        "SELECT source_size FROM file_copies WHERE id=?", (first.copy_id,)).fetchone()[0]
    owned.connection.execute(
        "UPDATE file_copies SET committed_bytes=? WHERE id=?", (size, first.copy_id))
    owned.connection.commit()
    result = OutputsRepository().release_read_slot(
        _slot(first.copy_id, _NOW + 5), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is SlotOutcome.RELEASED, result.value
    assert _slot_of(owned, first.copy_id) is None
    assert len(_slot_history(owned)) == 2


def test_release_when_run_finished(prepared):
    owned, command, first = prepared
    OutputsRepository().grant_read_slot(_slot(first.copy_id), new_operation_key(), owned)
    owned.connection.execute(
        "UPDATE operation_runs SET status=5 WHERE copy_id=?", (first.copy_id,))
    owned.connection.commit()
    result = OutputsRepository().release_read_slot(
        _slot(first.copy_id, _NOW + 5), new_operation_key(), owned)
    assert result.value.outcome is SlotOutcome.RELEASED, result.value
    assert _slot_of(owned, first.copy_id) is None


def test_release_is_idempotent_when_slot_empty(prepared):
    owned, command, first = prepared
    before = _dump(owned)
    result = OutputsRepository().release_read_slot(
        _slot(first.copy_id, _NOW + 5), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is SlotOutcome.ALREADY_RELEASED
    assert _dump(owned) == before


def test_release_waits_for_active_attempt(prepared):
    """实际读取未结束时迟到通知不能清空机会。"""
    owned, command, first = prepared
    OutputsRepository().grant_read_slot(_slot(first.copy_id), new_operation_key(), owned)
    run_id = owned.connection.execute(
        "SELECT id FROM operation_runs WHERE copy_id=?", (first.copy_id,)).fetchone()[0]
    owned.connection.execute(
        "INSERT INTO operation_attempts (id, run_id, attempt_no, status, effect_state,"
        " intent_event_id, result_event_id, result_json, error_json, copy_round,"
        " max_attempts_used, timeout_s_json, retry_interval_s_json)"
        " VALUES (900, ?, 1, 1, 1, 1, NULL, NULL, NULL, 1, 3, 10, 0)", (run_id,))
    owned.connection.commit()
    before = _dump(owned)
    result = OutputsRepository().release_read_slot(
        _slot(first.copy_id, _NOW + 5), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is SlotOutcome.WAIT
    assert result.value.reason == "read_active"
    assert _slot_of(owned, first.copy_id) == "cam-1"
    assert _dump(owned) == before


def test_release_waits_for_retry_wait(prepared):
    owned, command, first = prepared
    OutputsRepository().grant_read_slot(_slot(first.copy_id), new_operation_key(), owned)
    owned.connection.execute(
        "UPDATE operation_runs SET retry_wait_required=1 WHERE copy_id=?", (first.copy_id,))
    owned.connection.commit()
    result = OutputsRepository().release_read_slot(
        _slot(first.copy_id, _NOW + 5), new_operation_key(), owned)
    assert result.value.outcome is SlotOutcome.WAIT, result.value
    assert result.value.reason == "retry_wait"


def test_release_waits_while_bytes_incomplete(prepared):
    owned, command, first = prepared
    OutputsRepository().grant_read_slot(_slot(first.copy_id), new_operation_key(), owned)
    result = OutputsRepository().release_read_slot(
        _slot(first.copy_id, _NOW + 5), new_operation_key(), owned)
    assert result.value.outcome is SlotOutcome.WAIT, result.value
    assert result.value.reason == "still_reading"


def test_release_key_returns_first_response(prepared):
    owned, command, first = prepared
    OutputsRepository().grant_read_slot(_slot(first.copy_id), new_operation_key(), owned)
    owned.connection.execute(
        "UPDATE operation_runs SET status=5 WHERE copy_id=?", (first.copy_id,))
    owned.connection.commit()
    repository = OutputsRepository()
    key = new_operation_key()
    original = repository.release_read_slot(_slot(first.copy_id, _NOW + 5), key, owned)
    assert original.value.outcome is SlotOutcome.RELEASED
    OutputsRepository().grant_read_slot(
        _slot(first.copy_id, _NOW + 6), new_operation_key(), owned)
    before = _dump(owned)
    repeated = repository.release_read_slot(_slot(first.copy_id, _NOW + 5), key, owned)
    assert repeated.kind is DbOutcomeKind.COMPLETED, repeated.error
    assert repeated.value == original.value
    assert _dump(owned) == before


def test_missing_copy_is_a_consistency_error(read_targets):
    owned = read_targets
    result = OutputsRepository().grant_read_slot(_slot(404), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result
    assert isinstance(result.error, ConsistencyError), result.error
    released = OutputsRepository().release_read_slot(_slot(404), new_operation_key(), owned)
    assert released.kind is DbOutcomeKind.ROLLED_BACK, released


@pytest.fixture
def mixed_purposes(read_targets):
    """同一设备上同时具备条件的内部检查输入与普通取回副本。"""
    owned = read_targets
    owned.connection.execute("UPDATE actions SET status=3 WHERE id=12")
    owned.connection.commit()
    external = _prepare(owned, _later_read_command())
    owned.connection.execute("DELETE FROM obtain_items")
    owned.connection.execute("DELETE FROM outputs")
    owned.connection.commit()
    internal = _prepare(owned, _command(internal=True))
    return owned, internal, external


def test_slot_order_has_no_purpose_priority(mixed_purposes):
    """同时间的内部输入与普通取回只按动作身份排序，用途不参与。"""
    owned, internal, external = mixed_purposes
    internal_first = OutputsRepository().grant_read_slot(
        _slot(internal.copy_id), new_operation_key(), owned)
    assert internal_first.value.outcome is SlotOutcome.GRANTED, internal_first.value
    external_later = OutputsRepository().grant_read_slot(
        _slot(external.copy_id), new_operation_key(), owned)
    assert external_later.value.outcome is SlotOutcome.WAIT
    assert external_later.value.reason == "device_busy"


# ---- 真实链路 ----


def test_granted_slot_enables_real_read_intent(prepared):
    """真实授予后读取意图可用；释放后不再登记新尝试。"""
    owned, command, first = prepared
    repository = OutputsRepository()
    repository.grant_read_slot(_slot(first.copy_id), new_operation_key(), owned)
    intent = AttemptIntent(
        operation="read", action_id=command.action_id, kind=OperationKind.READ_FILE,
        target=AttemptTarget(copy_id=first.copy_id), query_purpose=None,
        config=AttemptConfig(3, Decimal("10"), Decimal("0")) if command.config else AttemptConfig(1),
        occurred_at=_NOW + 2, copy_round=1,
    )
    granted = OperationRepository().begin_attempt(intent, new_operation_key(), owned)
    assert granted.kind is DbOutcomeKind.COMPLETED, granted.error
    assert granted.value.disposition is BeginDisposition.GRANTED
    # 第一次读取实际结束并推进可靠进度后，机会才能释放。
    owned.connection.execute(
        "UPDATE operation_attempts SET status=2, result_event_id=1,"
        " result_json='{\"format_version\":1}' WHERE run_id=?",
        (granted.value.ticket.run_id,))
    size = owned.connection.execute(
        "SELECT source_size FROM file_copies WHERE id=?", (first.copy_id,)).fetchone()[0]
    owned.connection.execute(
        "UPDATE file_copies SET committed_bytes=? WHERE id=?", (size, first.copy_id))
    owned.connection.commit()
    released = repository.release_read_slot(
        _slot(first.copy_id, _NOW + 3), new_operation_key(), owned)
    assert released.value.outcome is SlotOutcome.RELEASED, released.value
    # 等待重试期间机会已不在：新意图仍不派发读取。
    owned.connection.execute(
        "UPDATE operation_runs SET retry_wait_required=1 WHERE copy_id=?", (first.copy_id,))
    owned.connection.commit()
    denied = OperationRepository().begin_attempt(
        replace(intent, occurred_at=_NOW + 4), new_operation_key(), owned)
    assert denied.kind is DbOutcomeKind.COMPLETED, denied.error
    assert denied.value.disposition is BeginDisposition.REJECTED
    assert denied.value.reason == "read_slot_not_held"


def test_recopy_round_reacquires_after_release(two_copies):
    """释放后重拷轮次重新参与候选竞争，沿用原拷贝身份。"""
    owned, early, late = two_copies
    repository = OutputsRepository()
    repository.grant_read_slot(_slot(early.copy_id), new_operation_key(), owned)
    size = owned.connection.execute(
        "SELECT source_size FROM file_copies WHERE id=?", (early.copy_id,)).fetchone()[0]
    owned.connection.execute(
        "UPDATE file_copies SET committed_bytes=?, round=2, recopies_used=1"
        " WHERE id=?", (size, early.copy_id))
    owned.connection.commit()
    released = repository.release_read_slot(
        _slot(early.copy_id, _NOW + 5), new_operation_key(), owned)
    assert released.value.outcome is SlotOutcome.RELEASED, released.value
    reacquired = repository.grant_read_slot(
        _slot(early.copy_id, _NOW + 6), new_operation_key(), owned)
    assert reacquired.value.outcome is SlotOutcome.GRANTED, reacquired.value
    assert _slot_of(owned, early.copy_id) == "cam-1"


def test_file_grant_key_survives_real_slot_changes(read_targets):
    """建档原键在真实机会取得、释放及重新取得后仍返回首次响应。"""
    owned = read_targets
    owned.connection.execute("UPDATE actions SET status=3 WHERE id=11")
    owned.connection.commit()
    command = _command()
    key = new_operation_key()
    first = OutputsRepository().grant_file(command, key, owned)
    assert first.value.outcome is QualificationOutcome.GRANTED, first.error
    repository = OutputsRepository()
    repository.grant_read_slot(_slot(first.value.copy_id), new_operation_key(), owned)
    size = owned.connection.execute(
        "SELECT source_size FROM file_copies WHERE id=?",
        (first.value.copy_id,)).fetchone()[0]
    owned.connection.execute(
        "UPDATE file_copies SET committed_bytes=? WHERE id=?",
        (size, first.value.copy_id))
    owned.connection.commit()
    repository.release_read_slot(
        _slot(first.value.copy_id, _NOW + 2), new_operation_key(), owned)
    repository.grant_read_slot(
        _slot(first.value.copy_id, _NOW + 3), new_operation_key(), owned)
    before = _dump(owned)
    repeated = OutputsRepository().grant_file(command, key, owned)
    assert repeated.kind is DbOutcomeKind.COMPLETED, repeated.error
    assert repeated.value == first.value
    assert _dump(owned) == before


# ---- 正式守卫 ----


def _proposal(owned, first: int, direction: str, occurred_at: int = _NOW + 1):
    owned.connection.execute("BEGIN")
    try:
        plan = outputs._SlotChangeCommand(
            SlotRequest(first, occurred_at), direction, new_operation_key(),
        ).plan(TransactionScope(owned.connection, 1, 1))
    finally:
        owned.connection.rollback()
    return plan


def _guard_case(owned, first: int):
    grant = _proposal(owned, first, "grant")
    assert grant.events, "授予提案必须产生事件"
    OutputsRepository().grant_read_slot(
        SlotRequest(first, _NOW + 1), new_operation_key(), owned)
    size = owned.connection.execute(
        "SELECT source_size FROM file_copies WHERE id=?", (first,)).fetchone()[0]
    owned.connection.execute(
        "UPDATE file_copies SET committed_bytes=? WHERE id=?", (size, first))
    owned.connection.commit()
    release = _proposal(owned, first, "release", _NOW + 2)
    return grant, release


def test_guard_accepts_real_grant_and_release_events(prepared):
    owned, command, first = prepared
    grant, release = _guard_case(owned, first.copy_id)
    for plan in (grant, release):
        context = EventContext(
            TransactionRange(2, 2, 2), dict(plan.owners), deepcopy(plan.state_rows),
            read_coverage=plan.read_coverage)
        assert validate_event(plan.events[0], context).branch_name == "SLOT"


def test_guard_rejects_grant_that_skips_or_changes_binding(prepared):
    owned, command, first = prepared
    grant, _ = _guard_case(owned, first.copy_id)
    context = EventContext(
        TransactionRange(2, 2, 2), dict(grant.owners), deepcopy(grant.state_rows),
        read_coverage=grant.read_coverage)
    event = grant.events[0]
    row = event.rows[0]
    occupied = replace(row, before=replace(row.before, values={"slot_device_id": "cam-1"}))
    with pytest.raises(EventValidationError):
        validate_event(replace(event, rows=(occupied,)), context)
    wrong_device = replace(
        row, after=replace(row.after, values={"slot_device_id": "another-camera"}))
    with pytest.raises(EventValidationError):
        validate_event(replace(event, rows=(wrong_device,)), context)


def test_guard_rejects_grant_for_host_source_or_finished_run(prepared):
    owned, command, first = prepared
    grant, _ = _guard_case(owned, first.copy_id)
    rows = deepcopy(grant.state_rows)
    copy_id = first.copy_id
    rows["file_copies"][copy_id]["source_device_file_id"] = None
    context = EventContext(
        TransactionRange(2, 2, 2), dict(grant.owners), rows, read_coverage=grant.read_coverage)
    with pytest.raises(EventValidationError):
        validate_event(grant.events[0], context)

    run_id = next(iter(grant.state_rows["operation_runs"]))
    terminal = deepcopy(grant.state_rows)
    terminal["operation_runs"][run_id]["status"] = 4
    context = EventContext(
        TransactionRange(2, 2, 2), dict(grant.owners), terminal, read_coverage=grant.read_coverage)
    with pytest.raises(EventValidationError):
        validate_event(grant.events[0], context)

    waiting = deepcopy(grant.state_rows)
    waiting["operation_runs"][run_id]["retry_wait_required"] = 1
    context = EventContext(
        TransactionRange(2, 2, 2), dict(grant.owners), waiting, read_coverage=grant.read_coverage)
    with pytest.raises(EventValidationError):
        validate_event(grant.events[0], context)


def test_guard_rejects_release_with_active_attempt_or_incomplete_bytes(prepared):
    owned, command, first = prepared
    OutputsRepository().grant_read_slot(SlotRequest(first.copy_id, _NOW + 1),
                                        new_operation_key(), owned)
    size = owned.connection.execute(
        "SELECT source_size FROM file_copies WHERE id=?", (first.copy_id,)).fetchone()[0]
    owned.connection.execute(
        "UPDATE file_copies SET committed_bytes=? WHERE id=?", (size, first.copy_id))
    owned.connection.commit()
    release = _proposal(owned, first.copy_id, "release", _NOW + 2)
    run_id = next(iter(release.state_rows["operation_runs"]))
    active = deepcopy(release.state_rows)
    active["operation_attempts"][901] = {"id": 901, "run_id": run_id, "status": 1}
    context = EventContext(
        TransactionRange(2, 2, 2), dict(release.owners), active,
        read_coverage=release.read_coverage)
    with pytest.raises(EventValidationError):
        validate_event(release.events[0], context)

    reading = deepcopy(release.state_rows)
    reading["file_copies"][first.copy_id]["committed_bytes"] = 0
    context = EventContext(
        TransactionRange(2, 2, 2), dict(release.owners), reading,
        read_coverage=release.read_coverage)
    with pytest.raises(EventValidationError):
        validate_event(release.events[0], context)

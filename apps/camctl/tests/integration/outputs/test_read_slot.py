"""相机读取机会的授予、等待、释放与恢复；正式守卫核对当前事实。"""

from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.history.validators import EventValidationError
from ..support.output_planning import EventSequence, capture_operation, proposal_state
from ..support.output_storage_cases import observed_operation
from ..support.read_batch_cases import acquire_read_batch
from camctl.operations.attempts import (
    AttemptConfig, AttemptIntent, AttemptTarget, BeginDisposition, OperationKind,
)
from camctl.outputs.qualification import FileCandidate, OperationConfig, QualificationOutcome
from camctl.outputs.batches import BatchOutcome, BatchRequest
from camctl.outputs.slots import SlotOutcome, SlotRequest
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories import outputs
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.file_batches import FileBatchRepository, register_file_batch_guards
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.repositories.transactions import commit_operation

from .test_local_read import local_read  # noqa: F401  主机源建档夹具
from .test_qualification import _NOW, _seed_selection_and_item
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


@pytest.fixture
def prepared_with_batch(prepared):
    owned, command, first = prepared
    if command.item_id is not None:
        acquire_read_batch(owned, command.action_id, "cam-1", _NOW)
    return owned, command, first


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


@pytest.fixture
def same_batch_copies(read_targets):
    """同一取回动作固定选择两份已完成的同机源文件。"""
    owned = read_targets
    owned.connection.execute("UPDATE actions SET status=3 WHERE id IN (11,12)")
    _seed_selection_and_item(owned.connection, dependency_id=33, selection_id=33,
                             item_id=103, owner_action_id=31, source_action_id=12,
                             output_id=702)
    owned.connection.commit()
    early = _prepare(owned, _command())
    late = _prepare(owned, replace(_later_read_command(), action_id=31, item_id=103))
    acquire_read_batch(owned, 31, "cam-1", _NOW)
    return owned, early, late


def _acquire_early_batch(owned):
    # 这些槽位用例使用已完成来源的文件，不测试等待来源结束。
    owned.connection.execute("UPDATE actions SET status=3 WHERE id=11")
    owned.connection.commit()
    acquire_read_batch(owned, 31, "cam-1", _NOW)


# ---- 授予 ----


def test_grant_saves_slot_and_history(prepared_with_batch):
    owned, command, first = prepared_with_batch
    result = OutputsRepository().grant_read_slot(_slot(first.copy_id), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is SlotOutcome.GRANTED, result.value
    assert _slot_of(owned, first.copy_id) == "cam-1"
    assert len(_slot_history(owned)) == 1
    body = owned.connection.execute(
        "SELECT body_json FROM history_events WHERE event_type=22"
        " AND json_extract(body_json, '$.reason')=6").fetchone()[0]
    assert '"slot_device_id"' in body


def test_grant_reuses_held_slot_without_new_events(prepared_with_batch):
    owned, command, first = prepared_with_batch
    OutputsRepository().grant_read_slot(_slot(first.copy_id), new_operation_key(), owned)
    before = _dump(owned)
    result = OutputsRepository().grant_read_slot(_slot(first.copy_id), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is SlotOutcome.HELD
    assert _dump(owned) == before


def test_grant_waits_while_another_copy_holds(same_batch_copies):
    owned, early, late = same_batch_copies
    initial = OutputsRepository().grant_read_slot(_slot(early.copy_id), new_operation_key(), owned)
    assert initial.kind is DbOutcomeKind.COMPLETED and initial.value.outcome is SlotOutcome.GRANTED, initial
    before = _dump(owned)
    result = OutputsRepository().grant_read_slot(_slot(late.copy_id), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is SlotOutcome.WAIT
    assert result.value.reason == "device_busy"
    assert _dump(owned) == before


def test_grant_uses_registered_candidate_order(same_batch_copies):
    """同设备合格候选按统一文件顺序竞争；用途不增加优先级。"""
    owned, early, late = same_batch_copies
    late_first = OutputsRepository().grant_read_slot(
        _slot(late.copy_id), new_operation_key(), owned)
    assert late_first.value.outcome is SlotOutcome.WAIT, late_first.value
    assert late_first.value.reason == "predecessor"
    granted = OutputsRepository().grant_read_slot(
        _slot(early.copy_id), new_operation_key(), owned)
    assert granted.value.outcome is SlotOutcome.GRANTED, granted.value


def test_grant_order_follows_action_time_not_request_time(two_copies):
    """较早计划的取回批次先取得设备，后续申请不抢占。"""
    owned, early, late = two_copies
    owned.connection.execute(
        "UPDATE actions SET scheduled_at=? WHERE id=32", (_NOW - 10_000_000,))
    owned.connection.execute("UPDATE actions SET status=3 WHERE id=11")
    owned.connection.commit()
    register_file_batch_guards()
    repository = FileBatchRepository()
    first = repository.acquire(BatchRequest(31, "cam-1", _NOW), new_operation_key(), owned)
    assert first.kind is DbOutcomeKind.COMPLETED and first.value is BatchOutcome.WAIT, first
    selected = repository.acquire(BatchRequest(32, "cam-1", _NOW), new_operation_key(), owned)
    assert selected.kind is DbOutcomeKind.COMPLETED and selected.value is BatchOutcome.GRANTED, selected
    later = repository.acquire(BatchRequest(31, "cam-1", _NOW + 1), new_operation_key(), owned)
    assert later.kind is DbOutcomeKind.COMPLETED and later.value is BatchOutcome.WAIT, later


def test_grant_rejects_foreign_slot(prepared_with_batch):
    owned, command, first = prepared_with_batch
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


def test_grant_waits_for_retry_interval(prepared_with_batch):
    owned, command, first = prepared_with_batch
    owned.connection.execute(
        "UPDATE operation_runs SET retry_wait_required=1 WHERE copy_id=?", (first.copy_id,))
    owned.connection.commit()
    before = _dump(owned)
    result = OutputsRepository().grant_read_slot(_slot(first.copy_id), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is SlotOutcome.WAIT
    assert result.value.reason == "retry_wait"
    assert _dump(owned) == before


def test_finished_responsibility_needs_no_slot(prepared_with_batch):
    owned, command, first = prepared_with_batch
    owned.connection.execute(
        "UPDATE operation_runs SET status=4, error_json='{}' WHERE copy_id=?", (first.copy_id,))
    owned.connection.commit()
    before = _dump(owned)
    result = OutputsRepository().grant_read_slot(_slot(first.copy_id), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is SlotOutcome.FINISHED
    assert _dump(owned) == before


def test_canceled_owner_needs_no_new_slot(prepared_with_batch):
    owned, command, first = prepared_with_batch
    owned.connection.execute(
        "UPDATE actions SET cancel_requested=1 WHERE id=?", (command.action_id,))
    owned.connection.commit()
    result = OutputsRepository().grant_read_slot(_slot(first.copy_id), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is SlotOutcome.FINISHED


def test_grant_key_returns_first_response_and_rejects_changed_input(prepared_with_batch):
    owned, command, first = prepared_with_batch
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


def test_slot_key_cannot_be_used_as_file_grant_key(prepared_with_batch):
    owned, command, first = prepared_with_batch
    key = new_operation_key()
    OutputsRepository().grant_read_slot(_slot(first.copy_id), key, owned)
    result = OutputsRepository().grant_file(command, key, owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result


# ---- 释放 ----


def test_release_after_bytes_complete(prepared_with_batch):
    owned, command, first = prepared_with_batch
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


def test_release_when_run_finished(prepared_with_batch):
    owned, command, first = prepared_with_batch
    OutputsRepository().grant_read_slot(_slot(first.copy_id), new_operation_key(), owned)
    owned.connection.execute(
        "UPDATE operation_runs SET status=5 WHERE copy_id=?", (first.copy_id,))
    owned.connection.commit()
    result = OutputsRepository().release_read_slot(
        _slot(first.copy_id, _NOW + 5), new_operation_key(), owned)
    assert result.value.outcome is SlotOutcome.RELEASED, result.value
    assert _slot_of(owned, first.copy_id) is None


def test_release_is_idempotent_when_slot_empty(prepared_with_batch):
    owned, command, first = prepared_with_batch
    before = _dump(owned)
    result = OutputsRepository().release_read_slot(
        _slot(first.copy_id, _NOW + 5), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is SlotOutcome.ALREADY_RELEASED
    assert _dump(owned) == before


def test_release_waits_for_active_attempt(prepared_with_batch):
    """实际读取未结束时迟到通知不能清空机会。"""
    owned, command, first = prepared_with_batch
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


def test_release_waits_for_retry_wait(prepared_with_batch):
    owned, command, first = prepared_with_batch
    OutputsRepository().grant_read_slot(_slot(first.copy_id), new_operation_key(), owned)
    owned.connection.execute(
        "UPDATE operation_runs SET retry_wait_required=1 WHERE copy_id=?", (first.copy_id,))
    owned.connection.commit()
    result = OutputsRepository().release_read_slot(
        _slot(first.copy_id, _NOW + 5), new_operation_key(), owned)
    assert result.value.outcome is SlotOutcome.WAIT, result.value
    assert result.value.reason == "retry_wait"


def test_release_waits_while_bytes_incomplete(prepared_with_batch):
    owned, command, first = prepared_with_batch
    OutputsRepository().grant_read_slot(_slot(first.copy_id), new_operation_key(), owned)
    result = OutputsRepository().release_read_slot(
        _slot(first.copy_id, _NOW + 5), new_operation_key(), owned)
    assert result.value.outcome is SlotOutcome.WAIT, result.value
    assert result.value.reason == "still_reading"


def test_release_key_returns_first_response(prepared_with_batch):
    owned, command, first = prepared_with_batch
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
    """内部读取和取回使用同机的不同来源，互不依赖来源终态。"""
    owned = read_targets
    owned.connection.execute("UPDATE actions SET status=3 WHERE id=12")
    owned.connection.commit()
    external = _prepare(owned, _later_read_command())
    internal = _prepare(owned, _command(internal=True))
    return owned, internal, external


@pytest.mark.parametrize("order", ["tie_internal_first", "internal_earlier", "obtain_earlier"])
def test_slot_order_has_no_purpose_priority(mixed_purposes, order):
    """空闲相机按动作时间及计划内位置协调内部读取与取回批次。"""
    owned, internal, external = mixed_purposes
    if order != "tie_internal_first":
        earlier = 11 if order == "internal_earlier" else 32
        owned.connection.execute("UPDATE actions SET scheduled_at=? WHERE id=?", (_NOW - 1_000_000, earlier))
        owned.connection.commit()
    assert _slot_of(owned, internal.copy_id) is None and _slot_of(owned, external.copy_id) is None
    register_file_batch_guards()
    batches = FileBatchRepository()
    requested = batches.acquire(BatchRequest(32, "cam-1", _NOW), new_operation_key(), owned)
    assert requested.kind is DbOutcomeKind.COMPLETED, requested.error
    if order == "obtain_earlier":
        assert requested.value is BatchOutcome.GRANTED, requested
        internal_result = OutputsRepository().grant_read_slot(
            _slot(internal.copy_id), new_operation_key(), owned)
        assert internal_result.value.outcome is SlotOutcome.WAIT, internal_result
        assert internal_result.value.reason == "file_batch_waiting"
        external_result = OutputsRepository().grant_read_slot(
            _slot(external.copy_id), new_operation_key(), owned)
        assert external_result.value.outcome is SlotOutcome.GRANTED, external_result
    else:
        assert requested.value is BatchOutcome.WAIT, requested
        internal_result = OutputsRepository().grant_read_slot(
            _slot(internal.copy_id), new_operation_key(), owned)
        assert internal_result.value.outcome is SlotOutcome.GRANTED, internal_result
        again = batches.acquire(BatchRequest(32, "cam-1", _NOW + 1), new_operation_key(), owned)
        assert again.kind is DbOutcomeKind.COMPLETED and again.value is BatchOutcome.WAIT, again


# ---- 真实链路 ----


def test_granted_slot_enables_real_read_intent(prepared_with_batch):
    """真实授予后读取意图可用；释放后不再登记新尝试。"""
    owned, command, first = prepared_with_batch
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
    _acquire_early_batch(owned)
    initial = repository.grant_read_slot(_slot(early.copy_id), new_operation_key(), owned)
    assert initial.value.outcome is SlotOutcome.GRANTED, initial
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
    acquire_read_batch(owned, command.action_id, "cam-1", _NOW)
    repository = OutputsRepository()
    slot = repository.grant_read_slot(_slot(first.value.copy_id), new_operation_key(), owned)
    assert slot.value.outcome is SlotOutcome.GRANTED, slot
    size = owned.connection.execute(
        "SELECT source_size FROM file_copies WHERE id=?",
        (first.value.copy_id,)).fetchone()[0]
    owned.connection.execute(
        "UPDATE file_copies SET committed_bytes=? WHERE id=?",
        (size, first.value.copy_id))
    owned.connection.commit()
    released = repository.release_read_slot(
        _slot(first.value.copy_id, _NOW + 2), new_operation_key(), owned)
    assert released.value.outcome is SlotOutcome.RELEASED, released
    reacquired = repository.grant_read_slot(
        _slot(first.value.copy_id, _NOW + 3), new_operation_key(), owned)
    assert reacquired.value.outcome is SlotOutcome.GRANTED, reacquired
    before = _dump(owned)
    repeated = OutputsRepository().grant_file(command, key, owned)
    assert repeated.kind is DbOutcomeKind.COMPLETED, repeated.error
    assert repeated.value == first.value
    assert _dump(owned) == before


# ---- 正式守卫 ----


def _proposal(owned, first: int, direction: str, occurred_at: int = _NOW + 1):
    method = (OutputsRepository().grant_read_slot if direction == "grant"
              else OutputsRepository().release_read_slot)
    return capture_operation(owned, lambda target: method(
        SlotRequest(first, occurred_at), new_operation_key(), target))


def _replay_slot_event(owned, plan, *, event=None, mutate=None):
    """在独立数据库副本重放正式事件，由事务绑定真实事实端口。"""
    def replay(target):
        if mutate is not None:
            target.connection.execute("PRAGMA ignore_check_constraints=ON")
            mutate(target.connection)
            target.connection.commit()
        before = _dump(target)
        current = proposal_state(target, owners=plan.owners)
        receipt = commit_operation(
            EventSequence((event or plan.events[0],), current), new_operation_key(), target)
        if receipt.kind == "rolled_back":
            assert _dump(target) == before
            assert not target.connection.in_transaction
        return receipt

    return observed_operation(owned, replay)


def _reject_slot_event(owned, plan, message, *, event=None, mutate=None):
    receipt = _replay_slot_event(owned, plan, event=event, mutate=mutate)
    assert receipt.kind == "rolled_back", receipt
    assert isinstance(receipt.error, EventValidationError), receipt.error
    assert message in str(receipt.error), receipt.error


def _release_proposal(owned, copy_id):
    granted = OutputsRepository().grant_read_slot(_slot(copy_id), new_operation_key(), owned)
    assert granted.kind is DbOutcomeKind.COMPLETED and granted.value.outcome is SlotOutcome.GRANTED, granted
    size = owned.connection.execute(
        "SELECT source_size FROM file_copies WHERE id=?", (copy_id,)).fetchone()[0]
    owned.connection.execute(
        "UPDATE file_copies SET committed_bytes=? WHERE id=?", (size, copy_id))
    owned.connection.commit()
    return _proposal(owned, copy_id, "release", _NOW + 2)


def test_guard_accepts_real_grant_and_release_events(prepared_with_batch):
    owned, command, first = prepared_with_batch
    grant = _proposal(owned, first.copy_id, "grant")
    receipt = _replay_slot_event(owned, grant)
    assert receipt.kind == "completed", receipt.error
    release = _release_proposal(owned, first.copy_id)
    receipt = _replay_slot_event(owned, release)
    assert receipt.kind == "completed", receipt.error


def test_guard_rejects_grant_that_skips_or_changes_binding(prepared_with_batch):
    owned, command, first = prepared_with_batch
    grant = _proposal(owned, first.copy_id, "grant")
    event, = grant.events
    row, = event.rows
    occupied = replace(row, before=replace(row.before, values={"slot_device_id": "another-camera"}))
    _reject_slot_event(owned, grant, "授予必须从空闲机会开始",
        event=replace(event, rows=(occupied,)), mutate=lambda db: db.execute(
            "UPDATE file_copies SET slot_device_id='another-camera' WHERE id=?", (first.copy_id,)))
    wrong = replace(row, after=replace(row.after, values={"slot_device_id": "another-camera"}))
    _reject_slot_event(owned, grant, "授予的设备必须等于原来源设备绑定",
                       event=replace(event, rows=(wrong,)))


def test_guard_rejects_grant_for_host_source_or_finished_run(prepared_with_batch):
    owned, command, first = prepared_with_batch
    grant = _proposal(owned, first.copy_id, "grant")
    _reject_slot_event(owned, grant, "主机源拷贝不能占用相机读取机会",
        mutate=lambda db: db.execute(
            "UPDATE file_copies SET source_device_file_id=NULL WHERE id=?", (first.copy_id,)))
    _reject_slot_event(owned, grant, "读取责任已结束，不能再授予机会",
        mutate=lambda db: db.execute(
            "UPDATE operation_runs SET status=4, error_json='{}' WHERE id=?", (first.run_id,)))
    _reject_slot_event(owned, grant, "重试等待期间不授予新的读取机会",
        mutate=lambda db: db.execute(
            "UPDATE operation_runs SET retry_wait_required=1 WHERE id=?", (first.run_id,)))


def test_guard_rejects_release_with_active_attempt_or_incomplete_bytes(prepared_with_batch):
    owned, command, first = prepared_with_batch
    release = _release_proposal(owned, first.copy_id)
    _reject_slot_event(owned, release, "实际读取未结束，不能释放机会",
        mutate=lambda db: db.execute(
            "INSERT INTO operation_attempts (id, run_id, attempt_no, status, effect_state,"
            " intent_event_id, result_event_id, result_json, error_json, copy_round,"
            " max_attempts_used, timeout_s_json, retry_interval_s_json)"
            " VALUES (901, ?, 1, 1, 1, 1, NULL, NULL, NULL, 1, 3, 10, 0)", (first.run_id,)))
    _reject_slot_event(owned, release, "源内容尚未读完，机会保留",
        mutate=lambda db: db.execute(
            "UPDATE file_copies SET committed_bytes=0 WHERE id=?", (first.copy_id,)))

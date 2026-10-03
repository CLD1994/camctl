"""内部输入的当前需求分区：仓储与创建守卫消费同一判定。

混合状态按 2026-10-03 的用户决策：检查需求仍在而修复已终局结束
按一致性错误拒绝；检查已失败或未确认时不保留待执行修复的输入
需求，只读不授予。
"""

from copy import deepcopy
from dataclasses import replace

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.history.validators import EventContext, EventValidationError, validate_event
from camctl.outputs.qualification import QualificationOutcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories import outputs
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.transaction import TransactionScope

from .test_qualification import _NOW
from .test_read_associations import read_targets, _command

# 检查决定: 1=UNDETERMINED 2=NOT_NEEDED 3=REQUIRED
# 检查进度: 1=NOT_PERFORMED 2=RUNNING 3=COMPLETED 4=FAILED 5=UNCONFIRMED
# 修复进度: 1=UNDETERMINED 2=NOT_NEEDED 3=PENDING 4=RUNNING 5=SUCCEEDED 6=FAILED 7=CANCELED
# 丢弃进度: 1=NOT_NEEDED 2=PENDING


@pytest.fixture
def internal(read_targets):
    owned = read_targets
    owned.connection.execute("DELETE FROM obtain_items")
    owned.connection.execute("DELETE FROM outputs")
    owned.connection.commit()
    return owned, _command(internal=True)


def _set_processing(owned, decision, check, repair, *, discard=1):
    owned.connection.execute(
        "UPDATE recording_processing SET check_decision=?, check_state=?, repair_state=?,"
        " repair_basis_json=?, repair_error_json=?, discard_state=? WHERE id=5",
        (decision, check, repair,
         None if repair == 1 else "{}",
         '{"reason":"seed"}' if repair == 6 else None,
         discard),
    )
    owned.connection.commit()


def _apply(owned, command):
    return OutputsRepository().grant_file(command, new_operation_key(), owned)


def test_undecided_processing_waits_without_grant(internal):
    owned, command = internal
    _set_processing(owned, decision=1, check=1, repair=1)
    before = tuple(owned.connection.iterdump())
    result = _apply(owned, command)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is QualificationOutcome.REJECTED
    assert result.value.reason == "input_undecided"
    assert tuple(owned.connection.iterdump()) == before


def test_not_needed_processing_waits_without_grant(internal):
    owned, command = internal
    _set_processing(owned, decision=2, check=1, repair=2)
    result = _apply(owned, command)
    assert result.value.outcome is QualificationOutcome.REJECTED
    assert result.value.reason == "input_not_needed"


def test_pending_repair_constitutes_independent_need(internal):
    """检查不需要而修复等待执行：修复构成独立输入需求，正常建档。"""
    owned, command = internal
    _set_processing(owned, decision=2, check=1, repair=3)
    result = _apply(owned, command)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.outcome is QualificationOutcome.GRANTED


@pytest.mark.parametrize("repair", [6, 7])
def test_check_need_with_terminal_repair_is_unexplainable(internal, repair):
    """组合 A：检查需求仍在而修复已终局结束，按一致性错误整组拒绝。"""
    owned, command = internal
    _set_processing(owned, decision=3, check=1, repair=repair)
    before = tuple(owned.connection.iterdump())
    result = _apply(owned, command)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result.error
    assert isinstance(result.error, ConsistencyError), result.error
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("check", [4, 5])
def test_failed_check_does_not_retain_pending_repair(internal, check):
    """组合 B：检查已失败或未确认，不保留待执行修复的输入需求。"""
    owned, command = internal
    _set_processing(owned, decision=3, check=check, repair=3)
    before = tuple(owned.connection.iterdump())
    result = _apply(owned, command)
    assert result.value.outcome is QualificationOutcome.REJECTED
    assert result.value.reason == "input_need_ended"
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("decision,check,repair", [(3, 4, 1), (2, 1, 6), (2, 1, 7)])
def test_ended_processing_does_not_create_new_need(internal, decision, check, repair):
    """检查或修复已终局结束且无存活需求：只读不授予。"""
    owned, command = internal
    _set_processing(owned, decision=decision, check=check, repair=repair)
    result = _apply(owned, command)
    assert result.value.outcome is QualificationOutcome.REJECTED
    assert result.value.reason == "input_need_ended"


def test_discard_in_progress_blocks_new_input(internal):
    """已进入丢弃处理后不再建立新的普通输入，保留既有处理事实。"""
    owned, command = internal
    _set_processing(owned, decision=3, check=1, repair=1, discard=2)
    result = _apply(owned, command)
    assert result.value.outcome is QualificationOutcome.REJECTED
    assert result.value.reason == "discard_active"


def test_contradiction_precedes_time_wait(internal):
    """组合 A 的一致性错误先于计划时间等待出口。"""
    owned, command = internal
    _set_processing(owned, decision=3, check=1, repair=6)
    early = replace(command, occurred_at=_NOW - 10_000_000)
    result = OutputsRepository().grant_file(early, new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK, result.error
    assert isinstance(result.error, ConsistencyError), result.error


def test_existing_preparation_reuse_ignores_later_need_change(internal):
    """已建档的完整责任不因后来需求状态变化重新判定。"""
    owned, command = internal
    first = _apply(owned, command)
    assert first.value.outcome is QualificationOutcome.GRANTED
    _set_processing(owned, decision=2, check=1, repair=2)
    again = _apply(owned, command)
    assert again.kind is DbOutcomeKind.COMPLETED, again.error
    assert again.value.outcome is QualificationOutcome.GRANTED
    assert again.value.reason == "already_granted"
    assert again.value.copy_id == first.value.copy_id


# ---- 正式创建守卫消费同一判定 ----


def _creation_event(owned, command):
    owned.connection.execute("BEGIN")
    try:
        plan = outputs._GrantFileCommand(command, new_operation_key()).plan(
            TransactionScope(owned.connection, 1, 1))
    finally:
        owned.connection.rollback()
    event = next(
        event for event in plan.events
        if any(row.table == "file_copies" for row in event.rows)
    )
    return plan, event


def _validate(plan, rows, event):
    context = EventContext(
        TransactionRange(2, 2, 2), dict(plan.owners), rows,
        read_coverage=plan.read_coverage)
    validate_event(event, context)


def test_creation_guard_rejects_contradiction_or_missing_need(internal):
    owned, command = internal
    plan, event = _creation_event(owned, command)
    contradiction = deepcopy(plan.state_rows)
    processing = contradiction["recording_processing"][5]
    processing.update(repair_state=6, repair_basis_json={}, repair_error_json={"reason": "seed"})
    with pytest.raises(EventValidationError):
        _validate(plan, contradiction, event)
    without_need = deepcopy(plan.state_rows)
    processing = without_need["recording_processing"][5]
    processing.update(check_decision=2, repair_state=2, repair_basis_json={})
    with pytest.raises(EventValidationError):
        _validate(plan, without_need, event)
    ending = deepcopy(plan.state_rows)
    processing = ending["recording_processing"][5]
    processing.update(check_state=4, repair_state=3, repair_basis_json={})
    with pytest.raises(EventValidationError):
        _validate(plan, ending, event)
    # 判定所需的检查与修复事实缺失同样拒绝，不能猜测需求。
    missing = deepcopy(plan.state_rows)
    del missing["recording_processing"][5]
    with pytest.raises(EventValidationError):
        _validate(plan, missing, event)

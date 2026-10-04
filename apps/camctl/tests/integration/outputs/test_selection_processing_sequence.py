"""录像处理事件与来源选择固定的真实先后顺序。

来源取消后的收场进度事件经正式 processing 守卫验证：先于选择固
定生效才满足"适用产物处理完成"；排在选择之后的完成事实不能补足
当前资格，整组回滚。
"""

from dataclasses import replace
import sqlite3

import pytest

from camctl.contracts.values import new_operation_key
from camctl.history import validators
from camctl.history.validators import EventValidationError
from camctl.outputs.sources import SelectionMode
from camctl.persistence.repositories import outputs
from camctl.persistence.repositories.operations import register_operation_guards
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.transaction import commit_operation, event_envelope, update_change

from .test_output_family import family_database  # noqa: F401  产物目录夹具
from .test_selection_event_sequence import _Sequence
from .test_selection_guard import _NOW, _proposal, selection_database
from ..operations.test_result_reuse import _FaultConnection

register_operation_guards()
register_outputs_guards()

#: RECORDING_PROCESSED.DISCARD 的事件与分支编号（与采集仓储登记一致）。
_PROCESSED_EVENT = 19
_DISCARD_REASON = 3


def _canceled_source_with_completed_discard(owned) -> None:
    """来源已取消且收场已完成，让真实固定命令可以预跑。"""
    owned.connection.execute(
        "INSERT INTO recording_processing (id, action_id, source_device_file_id,"
        " check_state, check_decision, check_basis_json, media_json, repair_state,"
        " repair_basis_json, repair_output_file_id, repair_error_json, discard_state,"
        " discard_error_json)"
        " VALUES (51, 11, 501, 1, 1, NULL, '{}', 1, NULL, NULL, NULL, 4, NULL)")
    owned.connection.execute("UPDATE actions SET status=6, cancel_requested=1 WHERE id=11")
    owned.connection.commit()


def _selection_against_pending_discard(owned, mode):
    """按收场完成预跑真实固定命令，再回置事务前的待执行收场事实。"""
    _canceled_source_with_completed_discard(owned)
    event, context = _proposal(owned, mode)
    owned.connection.execute("UPDATE recording_processing SET discard_state=2 WHERE id=51")
    owned.connection.commit()
    context.state_rows["recording_processing"][51]["discard_state"] = 2
    context.owners["recording_processing", 51] = ("action", 11)
    return event, context


def _discard_progress(after: int):
    row = update_change(
        "recording_processing", 51, {"discard_state": 2}, {"discard_state": after})
    return event_envelope(2, 2, _PROCESSED_EVENT, _DISCARD_REASON, (row,), _NOW)


@pytest.mark.parametrize("mode", list(SelectionMode))
@pytest.mark.parametrize("completion_first", [True, False])
def test_selection_uses_only_preceding_processing_completion(
    selection_database, monkeypatch, mode, completion_first,
):
    monkeypatch.setattr(validators, "NAMED_GUARDS", dict(validators.NAMED_GUARDS))
    owned = selection_database
    selection, context = _selection_against_pending_discard(owned, mode)
    events = ((_discard_progress(4), selection) if completion_first
              else (selection, _discard_progress(4)))
    selection_seen = []
    original_selection = validators.NAMED_GUARDS["source_selection"]
    processing_seen = []
    original_processing = validators.NAMED_GUARDS["processing"]

    def inspect_selection(event, current):
        selection_seen.append(
            current.state_rows["recording_processing"][51]["discard_state"])
        original_selection(event, current)

    def inspect_processing(event, current):
        processing_seen.append(
            current.state_rows["recording_processing"][51]["discard_state"])
        original_processing(event, current)

    monkeypatch.setitem(validators.NAMED_GUARDS, "source_selection", inspect_selection)
    monkeypatch.setitem(validators.NAMED_GUARDS, "processing", inspect_processing)
    before = tuple(owned.connection.iterdump())
    receipt = commit_operation(_Sequence(events, context), new_operation_key(), owned)
    # 选择先被拒绝时收场事件不再验证；否则收场事件总由正式
    # processing 守卫按事件前的待执行事实验证。
    assert processing_seen == ([2] if completion_first else []), receipt.error
    assert selection_seen == [4 if completion_first else 2], receipt.error
    if completion_first:
        assert receipt.kind == "completed", receipt.error
        assert owned.connection.execute(
            "SELECT status FROM obtain_source_selections WHERE id=61").fetchone() == (2,)
        assert owned.connection.execute(
            "SELECT discard_state FROM recording_processing WHERE id=51").fetchone() == (4,)
        expected_items = 4 if mode is SelectionMode.EXPLICIT_IDS else 2
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM obtain_items WHERE selection_id=61").fetchone() == (expected_items,)
    else:
        assert receipt.kind == "rolled_back", receipt.error
        assert isinstance(receipt.error, EventValidationError), receipt.error
        assert "处理未完成" in str(receipt.error)
        assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("mode", list(SelectionMode))
def test_prior_unfinished_discard_progress_still_blocks_selection(
    selection_database, monkeypatch, mode,
):
    monkeypatch.setattr(validators, "NAMED_GUARDS", dict(validators.NAMED_GUARDS))
    owned = selection_database
    selection, context = _selection_against_pending_discard(owned, mode)
    events = (_discard_progress(3), selection)
    seen = []
    original = validators.NAMED_GUARDS["source_selection"]

    def inspect(event, current):
        seen.append(current.state_rows["recording_processing"][51]["discard_state"])
        original(event, current)

    monkeypatch.setitem(validators.NAMED_GUARDS, "source_selection", inspect)
    before = tuple(owned.connection.iterdump())
    receipt = commit_operation(_Sequence(events, context), new_operation_key(), owned)
    assert seen == [3], receipt.error
    assert receipt.kind == "rolled_back", receipt.error
    assert isinstance(receipt.error, EventValidationError), receipt.error
    assert "处理未完成" in str(receipt.error)
    assert tuple(owned.connection.iterdump()) == before


def test_write_failure_rolls_back_processing_and_selection(selection_database, monkeypatch):
    monkeypatch.setattr(validators, "NAMED_GUARDS", dict(validators.NAMED_GUARDS))
    owned = selection_database
    selection, context = _selection_against_pending_discard(owned, SelectionMode.DEFAULT)
    events = (_discard_progress(4), selection)
    before = tuple(owned.connection.iterdump())
    target = replace(
        owned, connection=_FaultConnection(owned.connection, "INSERT INTO obtain_items"))
    receipt = commit_operation(_Sequence(events, context), new_operation_key(), target)
    assert receipt.kind == "rolled_back", receipt.error
    assert isinstance(receipt.error, sqlite3.OperationalError), receipt.error
    assert tuple(owned.connection.iterdump()) == before

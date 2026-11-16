"""数据库结果模型保留原实际异常和诊断。"""

import asyncio
import json

from camctl.persistence.models import DbOutcome, DbOutcomeKind, db_failure, db_outcome_for


def test_failure_keeps_all_database_exception_objects():
    first = OSError(5, "提交失败")
    secondary = RuntimeError("回滚失败")
    outcome = DbOutcome(DbOutcomeKind.UNKNOWN, error=first, secondary_errors=(secondary, first))
    failure = db_failure(ValueError, "原结果无法保存", outcome)
    assert type(failure) is ValueError
    assert failure.__cause__.exceptions == (first, secondary)
    assert db_outcome_for(failure) is outcome


def test_failure_preserves_control_identity_and_secondary_diagnostics():
    control = asyncio.CancelledError("原控制对象")
    secondary = OSError(5, "连接关闭失败", "/state")
    outcome = DbOutcome(DbOutcomeKind.COMPLETED, control=control, secondary_errors=(secondary,))
    assert db_failure(RuntimeError, "结果已提交但控制交付失败", outcome) is control
    assert db_outcome_for(control) is outcome
    notes = getattr(control, "__notes__", ())
    diagnostic = next(json.loads(note[note.index("{"):]) for note in notes if "{" in note)
    assert diagnostic["errno"] == 5
    assert diagnostic["filename"] == "/state"


def test_exception_conversion_transfers_actual_result_or_leaves_it_absent():
    from camctl.contracts.history_values import HistoryBoundary
    from camctl.persistence.models import transfer_db_outcome

    observer = OSError("observer failed")
    secondary = RuntimeError("secondary")
    outcomes = (
        DbOutcome(DbOutcomeKind.COMPLETED, value="saved", boundary=HistoryBoundary(1, 1),
                  observer_error=observer, secondary_errors=(secondary,)),
        DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError("commit unknown"), stage="commit"),
        DbOutcome(DbOutcomeKind.ROLLED_BACK, error=OSError("rolled back")),
        DbOutcome(DbOutcomeKind.NOT_EXECUTED, error=TimeoutError("enqueue timed out")),
    )
    for outcome in outcomes:
        original = db_failure(RuntimeError, "database failure", outcome)
        converted = RuntimeError("converted")
        assert transfer_db_outcome(original, converted) is converted
        assert db_outcome_for(converted) is outcome

    empty = RuntimeError("no outcome")
    converted = RuntimeError("converted")
    assert transfer_db_outcome(empty, converted) is converted
    assert db_outcome_for(converted) is None

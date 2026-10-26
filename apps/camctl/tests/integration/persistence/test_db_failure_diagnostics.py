"""包资源与数据库结果模型组合后保留原实际异常。"""

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
    diagnostic = next(json.loads(note[ note.index("{"):]) for note in notes if "{" in note)
    assert diagnostic["errno"] == 5
    assert diagnostic["filename"] == "/state"

"""从原生结果的冻结成员独立恢复专属分类。"""

from types import SimpleNamespace
from dataclasses import dataclass
from camctl.capture.media import RecordingFailure
from camctl.capture.segment_models import CurrentSegmentResult, SegmentAssessmentStatus, DefaultScopeState
from camctl.contracts.values import ConsistencyError
from camctl.capture.segment_storage import SegmentResultRef
from camctl.contracts.enums import enum_for
from camctl.persistence.transaction import row_facts


def native_classification(db, setrow):
    """两遍读取同一冻结成员，空尾分类由可靠顺序和最后非零原片证明。"""
    return _load_native_classification(db, setrow)


def _load_native_classification(db, setrow):
    from .segments import read_segment_members
    from .segment_result_sets import _one
    from camctl.capture.native_segments import classify_native_members

    ref = SegmentResultRef(setrow["action_id"], setrow["id"], setrow["version"])

    def members():
        cursor = None
        while True:
            page = read_segment_members(
                ref, cursor, 128, SimpleNamespace(connection=db)
            )
            yield from (stored.member for stored in page.items)
            if page.next_cursor is None:
                return
            cursor = page.next_cursor

    latest = _one(
        db,
        "SELECT id FROM operation_attempts WHERE run_id=? ORDER BY attempt_no DESC LIMIT 1",
        (setrow["run_id"],),
    )
    attempt = None if latest is None else row_facts(db, "operation_attempts", latest[0])
    return classify_native_members(
        members,
        complete=setrow["source_kind"]
        == int(enum_for("segment_sets.source_kind").COMPLETE_SCAN),
        activity_id=setrow["activity_id"],
        explicit_error=attempt is not None
        and attempt["status"] == int(enum_for("operation_attempts.status").FAILED)
        and attempt["error_json"] is not None,
    )


@dataclass(frozen=True)
class SavedNativeClassification:
    """消费原有限结论所需的分类；不附带重新分类的中间计数。"""
    current_result: CurrentSegmentResult
    failure: RecordingFailure | None
    gap_count: int = 0

    @property
    def status(self):
        return (SegmentAssessmentStatus.SUCCEEDED
                if self.current_result is CurrentSegmentResult.SUFFICIENT
                else SegmentAssessmentStatus.FAILED
                if self.current_result is CurrentSegmentResult.UNMET
                else SegmentAssessmentStatus.UNCONFIRMED)

    @property
    def default_scope(self):
        return SimpleNamespace(state=DefaultScopeState.UNKNOWN, member_count=None)


def read_saved_native_classification(db, setrow):
    """消费已保存原生结论，不重新遍历成员执行空尾分类。"""
    from .segment_check_conclusions import read_fixed_result_check_conclusion
    return native_classification_from_saved_conclusion(read_fixed_result_check_conclusion(db, setrow))


def native_classification_from_saved_conclusion(conclusion):
    """把已取得的原生结论转换为本次业务所需分类。"""
    reasons = enum_for("segment_check_conclusions.reason")
    if conclusion["reason"] not in (int(reasons.NATIVE_RESULT_DECIDED), int(reasons.NATIVE_FINITE_CANCEL_END)):
        raise ConsistencyError("固定结论不是原生结果分类")
    result = enum_for("segment_check_conclusions.current_result")(conclusion["current_result"])
    raw = conclusion["current_failure_json"]
    return SavedNativeClassification(CurrentSegmentResult[result.name], None if raw is None else RecordingFailure(**raw))

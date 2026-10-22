"""原生有限拍摄的空尾分类，不采用普通录像的时长下界和选段公式。"""

from dataclasses import dataclass
from types import SimpleNamespace
from camctl.capture.media import RecordingFailure
from camctl.capture.segment_models import (
    CurrentSegmentResult,
    SegmentMemberRole,
    OwnershipState,
    FileCompletionState,
    SegmentAssessmentStatus,
    DefaultScopeState,
)


@dataclass(frozen=True)
class NativeClassification:
    current_result: CurrentSegmentResult
    original_count: int
    empty_tail_count: int | None
    failure: RecordingFailure | None
    gap_count: int = 0

    @property
    def status(self):
        return (
            SegmentAssessmentStatus.SUCCEEDED
            if self.current_result is CurrentSegmentResult.SUFFICIENT
            else SegmentAssessmentStatus.FAILED
            if self.current_result is CurrentSegmentResult.UNMET
            else SegmentAssessmentStatus.UNCONFIRMED
        )

    @property
    def default_scope(self):
        return SimpleNamespace(state=DefaultScopeState.UNKNOWN, member_count=None)


def classify_native_members(members, *, complete, activity_id, explicit_error=False):
    last = None
    previous = None
    known = True
    count = 0
    observed = 0
    for member in members():
        observed += 1
        if member.key is None:
            known = False
        if member.role is not SegmentMemberRole.ORIGINAL:
            continue
        if previous is not None and member.key == previous:
            known = False
        previous = member.key
        if (
            member.ownership is not OwnershipState.OWNED
            or member.completion is not FileCompletionState.COMPLETE
            or member.size_bytes is None
        ):
            known = False
        elif member.size_bytes > 0:
            count += 1
            last = member.key
    empty = 0
    invalid = False
    for member in members():
        if member.role is SegmentMemberRole.ORIGINAL and member.size_bytes == 0:
            if known and last is not None and member.key > last:
                empty += 1
            elif known:
                invalid = True
    failure = None
    if explicit_error:
        failure = RecordingFailure(
            "capture_failed",
            {"activity_id": str(activity_id), "reason": "device_failed"},
        )
    elif complete and known and (invalid or not count):
        failure = RecordingFailure(
            "capture_failed",
            {
                "activity_id": str(activity_id),
                "reason": "no_outputs" if observed == 0 else "invalid_outputs",
            },
        )
    result = (
        CurrentSegmentResult.UNMET
        if failure is not None
        else CurrentSegmentResult.SUFFICIENT
        if complete and known and count > 0
        else CurrentSegmentResult.UNCONFIRMED
    )
    return NativeClassification(
        result, count, empty if complete and known else None, failure
    )

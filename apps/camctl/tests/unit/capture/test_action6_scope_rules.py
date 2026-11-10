"""固定成员逐项复核 Action6 目标与默认范围。"""

from decimal import Decimal

import pytest

from camctl.capture.action6_scope_rules import Action6ScopeAssessment, require_action6_coverage_header
from camctl.capture.segment_models import (
    CoverageMetric, CoverageStatus, DefaultScopeState, FileCompletionState,
    OwnershipState, SegmentDecision, SegmentDecisionScope, SegmentDecisionStatus,
    SegmentMember, SegmentMemberRole, SegmentSetRef,
)
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.contracts.video_coverage import CoverageTarget
from camctl.devices.segments import SegmentKey


_REF = SegmentSetRef(1, 2, 3, 1)
_SOURCE = int(enum_for('segment_sets.source_kind').COMPLETE_SCAN)
_RESULT = int(enum_for('segment_sets.publication_kind').RESULT)
_CANCELED = int(enum_for('segment_sets.publication_kind').CANCELED)


def member(index):
    key = None if index is None else SegmentKey(1, index)
    return SegmentMember(f'/DCIM/{index}.mp4', key, SegmentMemberRole.ORIGINAL,
                         OwnershipState.OWNED, FileCompletionState.COMPLETE, 10)


def decision(first, last, count, *, canceled=False, checked=False):
    scope = SegmentDecisionScope(DefaultScopeState.NONEMPTY, _REF, first.cursor, last.cursor, count)
    return SegmentDecision(_REF, SegmentDecisionStatus.CANCELED if canceled else SegmentDecisionStatus.SUCCEEDED,
        CoverageMetric.DURATION_S, Decimal('10'), Decimal('12') if checked else None,
        CoverageStatus.REACHED if checked else CoverageStatus.NOT_PERFORMED,
        1 if checked else 0, scope)


def test_complete_unchecked_scope_uses_all_ordered_eligible_originals():
    first, second = member(1), member(2)
    selected = decision(first, second, 2)
    require_action6_coverage_header(selected, CoverageTarget(CoverageMetric.DURATION_S, Decimal('10')))
    assessment = Action6ScopeAssessment(selected)
    for value in (first, second):
        assessment.observe(value)
    assessment.require_decision(action_type='camera_record', source_kind=_SOURCE, publication_kind=_RESULT)
    with pytest.raises(ConsistencyError, match='冻结参数'):
        require_action6_coverage_header(selected, CoverageTarget(CoverageMetric.DURATION_S, Decimal('11')))


def test_checked_scope_contains_only_the_saved_checked_prefix():
    first, second = member(1), member(2)
    assessment = Action6ScopeAssessment(decision(first, first, 1, checked=True))
    assessment.observe(first)
    assessment.observe(second)
    assessment.require_decision(action_type='camera_record', source_kind=_SOURCE, publication_kind=_RESULT)


def test_canceled_recording_stops_at_gap_but_native_cancel_keeps_all_qualified():
    first, third = member(1), member(3)
    ordinary = Action6ScopeAssessment(decision(first, first, 1, canceled=True))
    native = Action6ScopeAssessment(decision(first, third, 2, canceled=True))
    for assessment in (ordinary, native):
        assessment.observe(first)
        assessment.observe(third)
    ordinary.require_decision(action_type='camera_record', source_kind=_SOURCE, publication_kind=_CANCELED)
    native.require_decision(action_type='camera_timelapse', source_kind=_SOURCE, publication_kind=_CANCELED)
    with pytest.raises(ConsistencyError, match='默认范围'):
        native.require_decision(action_type='camera_record', source_kind=_SOURCE, publication_kind=_CANCELED)


def test_native_cancel_cannot_hide_qualified_member_without_order_key():
    first = member(1)
    assessment = Action6ScopeAssessment(decision(first, first, 1, canceled=True))
    assessment.observe(first)
    assessment.observe(member(None))
    with pytest.raises(ConsistencyError, match='缺少可排序游标'):
        assessment.require_decision(action_type='camera_timelapse', source_kind=_SOURCE, publication_kind=_CANCELED)

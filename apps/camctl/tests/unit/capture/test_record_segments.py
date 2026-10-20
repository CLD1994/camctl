"""OSMO 分段排序、有限判定状态与默认范围。"""

import pytest


def _api():
    from camctl.capture.segment_models import (
        CompletionBasis,
        CompletionFacts,
        DefaultScopeState,
        EvidenceState,
        FileCompletionState,
        OwnershipState,
        OverrecordStatus,
        SegmentAssessmentReason,
        SegmentAssessmentStatus,
        SegmentCursor,
        SegmentGapKind,
        SegmentMember,
        SegmentMemberRole,
        SegmentSetRef,
    )
    from camctl.devices.segments import SegmentKey
    from camctl.capture.segments import (
        SegmentAccumulator,
        advance_segment_assessment,
        default_segment_count,
        finish_segment_assessment,
        iter_segment_gaps,
    )
    return locals()


def _set_ref(api):
    return api["SegmentSetRef"](action_id=7, scan_id=3, final_page_event_id=28, version=1)


def _member(api, index, *, directory=1, name=None, role="original", size=10,
            ownership="owned", completion="complete", pairing_identity=None):
    key = api["SegmentKey"](directory, index)
    identity = name or f"/DCIM/CAM{directory:03d}/CAM_20261021090000_{index:04d}_D.OSV"
    if role == "preview":
        identity = identity.replace(".OSV", ".LRF")
    return api["SegmentMember"](
        identity=identity,
        key=key,
        role=api["SegmentMemberRole"](role),
        ownership=api["OwnershipState"](ownership),
        completion=api["FileCompletionState"](completion),
        size_bytes=size,
        pairing_identity=pairing_identity,
    )


def _facts(api, *, target_ms=12_000, start="confirmed", start_key=(1, 1),
           stop="confirmed", file_wait="confirmed", scan="confirmed",
           control="unknown", overrun="unknown", finite=False, error=False):
    key = None if start_key is None else api["SegmentKey"](*start_key)
    return api["CompletionFacts"](
        stop=api["EvidenceState"](stop),
        file_wait=api["EvidenceState"](file_wait),
        scan=api["EvidenceState"](scan),
        start=api["EvidenceState"](start),
        start_key=key,
        control_completion=api["EvidenceState"](control),
        control_overrun=api["EvidenceState"](overrun),
        finite_check_complete=finite,
        explicit_error=error,
    )


def _assess(api, members, *, target_ms=12_000, facts=None):
    state = api["SegmentAccumulator"](set_ref=_set_ref(api), target_duration_ms=target_ms)
    for member in members:
        state = api["advance_segment_assessment"](state, member)
    return state, api["finish_segment_assessment"](
        state, facts or _facts(api, target_ms=target_ms))


@pytest.mark.parametrize(("target_ms", "expected"), [
    (9_000, 2),
    (10_000, 3),
    (12_000, 3),
    (9_223_372_036_854_775_807, 2_049_638_230_412_173),
])
def test_default_segment_count_uses_exact_milliseconds(target_ms, expected):
    api = _api()
    assert api["default_segment_count"](target_ms) == expected


def test_complete_lower_bound_excludes_last_nonzero_even_when_empty_files_follow():
    api = _api()
    originals = [_member(api, index) for index in (1, 2, 3, 4)]
    empty_tail = _member(api, 5, size=0)
    empty_preview = _member(api, 5, role="preview", size=0)
    state, result = _assess(api, [*originals, empty_tail, empty_preview])

    assert result.complete_segment_count == 3
    assert result.lower_bound_ms == 13_500
    assert result.empty_tail_count == 1
    assert result.set_ref == state.set_ref


def test_reliable_control_completion_succeeds_below_segment_lower_bound():
    api = _api()
    _, result = _assess(
        api, [_member(api, index) for index in (1, 2, 3)],
        facts=_facts(api, control="confirmed"),
    )

    assert result.status is api["SegmentAssessmentStatus"].SUCCEEDED
    assert result.reason is api["SegmentAssessmentReason"].NONE
    assert result.basis is api["CompletionBasis"].CONTROL
    assert result.complete_segment_count == 2
    assert result.lower_bound_ms == 9_000


def test_finite_check_without_control_or_sufficient_lower_bound_fails_as_unconfirmed():
    api = _api()
    _, result = _assess(
        api, [_member(api, index) for index in (1, 2, 3)],
        facts=_facts(api, finite=True),
    )

    assert result.status is api["SegmentAssessmentStatus"].FAILED
    assert result.reason is api["SegmentAssessmentReason"].LOWER_BOUND_INSUFFICIENT
    assert result.basis is api["CompletionBasis"].NONE
    assert result.lower_bound_ms == 9_000
    assert result.default_scope.state is api["DefaultScopeState"].NONEMPTY
    assert result.default_scope.member_count == 3


def test_missing_segment_interval_fails_and_defaults_only_to_the_contiguous_prefix():
    api = _api()
    _, result = _assess(
        api, [_member(api, index) for index in (1, 2, 4, 5)],
    )

    assert result.status is api["SegmentAssessmentStatus"].FAILED
    assert result.reason is api["SegmentAssessmentReason"].GAP
    assert result.gap_count == 1
    assert result.first_gap.start_key == api["SegmentKey"](1, 3)
    assert result.first_gap.end_key == api["SegmentKey"](1, 3)
    assert result.first_gap.kind is api["SegmentGapKind"].MISSING_POSITION
    assert result.default_scope.state is api["DefaultScopeState"].NONEMPTY
    assert result.default_scope.member_count == 2
    assert result.default_scope.last_cursor == api["SegmentCursor"](
        api["SegmentKey"](1, 2), _member(api, 2).identity)


def test_non_tail_zero_osv_is_a_gap_and_does_not_count_as_a_complete_segment():
    api = _api()
    _, result = _assess(api, [
        _member(api, 1),
        _member(api, 2, size=0),
        _member(api, 3),
    ])

    assert result.reason is api["SegmentAssessmentReason"].GAP
    assert result.complete_segment_count == 1
    assert result.lower_bound_ms == 4_500
    assert result.empty_tail_count == 0
    assert result.first_gap.kind is api["SegmentGapKind"].ZERO_BYTE_NON_TAIL
    assert result.first_gap.start_key == result.first_gap.end_key == api["SegmentKey"](1, 2)
    assert result.default_scope.member_count == 1


def test_confirmed_missing_start_produces_empty_scope_without_discarding_later_files():
    api = _api()
    _, result = _assess(
        api, [_member(api, 2), _member(api, 3)],
        facts=_facts(api, start_key=(1, 1)),
    )

    assert result.status is api["SegmentAssessmentStatus"].FAILED
    assert result.reason is api["SegmentAssessmentReason"].GAP
    assert result.default_scope.state is api["DefaultScopeState"].EMPTY
    assert result.default_scope.member_count == 0
    assert result.first_gap.start_key == result.first_gap.end_key == api["SegmentKey"](1, 1)


def test_unknown_start_or_order_keeps_default_scope_unknown():
    api = _api()
    _, unknown_start = _assess(
        api, [_member(api, 1), _member(api, 2)],
        facts=_facts(api, start="unknown", start_key=None, control="confirmed"),
    )
    _, unordered = _assess(
        api, [_member(api, 2), _member(api, 1)],
    )

    assert unknown_start.default_scope.state is api["DefaultScopeState"].UNKNOWN
    assert unordered.default_scope.state is api["DefaultScopeState"].UNKNOWN
    assert unordered.status is api["SegmentAssessmentStatus"].UNCONFIRMED
    assert unordered.complete_segment_count is None
    assert unordered.lower_bound_ms is None


def test_conflicting_originals_with_the_same_key_remain_unknown():
    api = _api()
    first = _member(api, 1)
    conflicting = _member(
        api, 1, name="/DCIM/CAM001/CAM_20261021090001_0001_D.OSV")
    _, result = _assess(api, [first, conflicting])

    assert result.status is api["SegmentAssessmentStatus"].UNCONFIRMED
    assert result.reason is api["SegmentAssessmentReason"].ORDER_UNKNOWN
    assert result.default_scope.state is api["DefaultScopeState"].UNKNOWN
    assert result.complete_segment_count is None


def test_zero_or_missing_preview_does_not_change_original_count_or_range():
    api = _api()
    _, result = _assess(api, [
        _member(api, 1),
        _member(api, 1, role="preview", size=0),
        _member(api, 2),
    ])

    assert result.complete_segment_count == 1
    assert result.lower_bound_ms == 4_500
    assert result.default_scope.state is api["DefaultScopeState"].NONEMPTY
    assert result.default_scope.member_count == 2


@pytest.mark.parametrize("members", [
    lambda api: [_member(api, 1, role="preview")],
    lambda api: [_member(api, 1, size=0)],
])
def test_only_preview_or_empty_osv_fails_the_nonzero_original_requirement(members):
    api = _api()
    _, result = _assess(api, members(api))

    assert result.status is api["SegmentAssessmentStatus"].FAILED
    assert result.reason is api["SegmentAssessmentReason"].NO_NONZERO_ORIGINAL
    assert result.default_scope.state is api["DefaultScopeState"].EMPTY


def test_empty_tail_classification_remains_unknown_until_the_final_scan():
    api = _api()
    _, result = _assess(
        api, [_member(api, 1), _member(api, 2, size=0)],
        facts=_facts(api, scan="unknown"),
    )

    assert result.status is api["SegmentAssessmentStatus"].UNCONFIRMED
    assert result.reason is api["SegmentAssessmentReason"].SCAN_UNCONFIRMED
    assert result.empty_tail_count is None
    assert result.default_scope.state is api["DefaultScopeState"].UNKNOWN


def test_confirmed_overrecord_selects_first_n_by_stable_cursor_and_keeps_bounded_state():
    api = _api()
    members = [_member(api, index) for index in range(1, 7)]
    state, result = _assess(
        api, members,
        facts=_facts(api, overrun="confirmed"),
    )

    assert result.status is api["SegmentAssessmentStatus"].SUCCEEDED
    assert result.basis is api["CompletionBasis"].SEGMENT_LOWER_BOUND
    assert result.complete_segment_count == 5
    assert result.lower_bound_ms == 22_500
    assert result.overrecord is api["OverrecordStatus"].CONFIRMED
    assert result.default_scope.state is api["DefaultScopeState"].NONEMPTY
    assert result.default_scope.member_count == 3
    assert result.default_scope.first_cursor == api["SegmentCursor"](
        api["SegmentKey"](1, 1), members[0].identity)
    assert result.default_scope.last_cursor == api["SegmentCursor"](
        api["SegmentKey"](1, 3), members[2].identity)
    assert not hasattr(state, "members")
    assert result.last_nonzero_cursor == state.last_nonzero_cursor
    assert result.default_scope.last_cursor == api["SegmentCursor"](
        api["SegmentKey"](1, 3), members[2].identity)


def test_long_missing_run_is_one_interval_instead_of_one_entry_per_position():
    api = _api()
    _, result = _assess(api, [
        _member(api, 1),
        _member(api, 999, directory=900),
    ])

    assert result.gap_count == 1
    assert result.first_gap.start_key == api["SegmentKey"](1, 2)
    assert result.first_gap.end_key == api["SegmentKey"](900, 998)
    assert result.default_scope.member_count == 1


def test_second_pass_classifies_each_empty_original_and_missing_interval_exactly():
    api = _api()
    members = [
        _member(api, 1),
        _member(api, 2, size=0),
        _member(api, 4, size=0),
        _member(api, 6),
    ]
    state, result = _assess(api, members)

    gaps = list(api["iter_segment_gaps"](
        members,
        last_nonzero_cursor=state.last_nonzero_cursor,
        start_key=api["SegmentKey"](1, 1),
    ))
    assert [(gap.kind, gap.start_key, gap.end_key, gap.empty_member_identity)
            for gap in gaps] == [
        (api["SegmentGapKind"].ZERO_BYTE_NON_TAIL,
         api["SegmentKey"](1, 2), api["SegmentKey"](1, 2), members[1].identity),
        (api["SegmentGapKind"].MISSING_POSITION,
         api["SegmentKey"](1, 3), api["SegmentKey"](1, 3), None),
        (api["SegmentGapKind"].ZERO_BYTE_NON_TAIL,
         api["SegmentKey"](1, 4), api["SegmentKey"](1, 4), members[2].identity),
        (api["SegmentGapKind"].MISSING_POSITION,
         api["SegmentKey"](1, 5), api["SegmentKey"](1, 5), None),
    ]
    assert len(gaps) == result.gap_count == 4
    assert gaps[0] == result.first_gap


def test_second_pass_keeps_all_zero_files_as_tail_and_finds_trusted_start_gap():
    api = _api()
    all_empty = [_member(api, 1, size=0), _member(api, 2, size=0)]
    state, result = _assess(api, all_empty)
    empty_gaps = list(api["iter_segment_gaps"](
        all_empty, last_nonzero_cursor=state.last_nonzero_cursor,
        start_key=api["SegmentKey"](1, 1)))
    assert empty_gaps == []
    assert result.empty_tail_count == 2
    assert result.reason is api["SegmentAssessmentReason"].NO_NONZERO_ORIGINAL

    with_start_gap = [_member(api, 3), _member(api, 4)]
    state, result = _assess(
        api, with_start_gap, facts=_facts(api, start_key=(1, 1)))
    gaps = list(api["iter_segment_gaps"](
        with_start_gap, last_nonzero_cursor=state.last_nonzero_cursor,
        start_key=api["SegmentKey"](1, 1)))
    assert len(gaps) == result.gap_count == 1
    assert gaps[0] == result.first_gap
    assert gaps[0].start_key == api["SegmentKey"](1, 1)
    assert gaps[0].end_key == api["SegmentKey"](1, 2)
    assert result.default_scope.state is api["DefaultScopeState"].EMPTY

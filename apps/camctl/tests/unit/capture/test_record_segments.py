"""OSMO 分段排序、有限判定状态与默认范围。"""

import pytest


def _api():
    import camctl.capture.segment_models as segment_models
    from camctl.capture.segment_models import (
        CompletionBasis,
        CompletionFacts,
        DefaultScopeBasis,
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
            ownership="owned", completion="complete", pairing_identity=None,
            pairing_state=None):
    key = api["SegmentKey"](directory, index)
    identity = name or f"/DCIM/CAM{directory:03d}/CAM_20261021090000_{index:04d}_D.OSV"
    if role == "preview":
        identity = identity.replace(".OSV", ".LRF")
    values = dict(
        identity=identity,
        key=key,
        role=api["SegmentMemberRole"](role),
        ownership=api["OwnershipState"](ownership),
        completion=api["FileCompletionState"](completion),
        size_bytes=size,
        pairing_identity=pairing_identity,
    )
    pairing_enum = getattr(api["segment_models"], "PreviewPairingState", None)
    if role == "preview" and pairing_enum is not None:
        values["pairing_state"] = pairing_enum(pairing_state or "unknown")
    elif pairing_state is not None:
        assert pairing_enum is not None, "分段模型尚未表达预览配对核实状态"
        values["pairing_state"] = pairing_enum(pairing_state)
    return api["SegmentMember"](**values)


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
    facts = facts or _facts(api, target_ms=target_ms)
    confirmed_start_key = (
        facts.start_key if facts.start is api["EvidenceState"].CONFIRMED else None)
    state = api["SegmentAccumulator"](
        set_ref=_set_ref(api), target_duration_ms=target_ms,
        confirmed_start_key=confirmed_start_key)
    for member in members:
        state = api["advance_segment_assessment"](state, member)
    return state, api["finish_segment_assessment"](
        state, facts)


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
    assert result.last_nonzero_cursor == originals[-1].cursor
    assert result.last_owned_cursor == empty_tail.cursor
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


def test_unknown_original_after_closed_gap_keeps_reliable_prefix():
    api = _api()
    members = [
        _member(api, 1),
        _member(api, 2),
        _member(api, 4),
        _member(api, 5, completion="unknown"),
    ]

    state, result = _assess(api, members)

    assert result.status is api["SegmentAssessmentStatus"].UNCONFIRMED
    assert result.reason is api["SegmentAssessmentReason"].MEMBER_UNKNOWN
    assert result.gap_count == 1
    assert result.first_gap.start_key == api["SegmentKey"](1, 3)
    assert result.default_scope.state is api["DefaultScopeState"].NONEMPTY
    assert result.default_scope.basis is api["DefaultScopeBasis"].CONTIGUOUS_PREFIX
    assert result.default_scope.member_count == 2
    assert result.default_scope.first_cursor.member_identity == members[0].identity
    assert result.default_scope.last_cursor.member_identity == members[1].identity
    assert state.last_nonzero_cursor == members[2].cursor


def test_unknown_original_after_confirmed_start_gap_keeps_empty_scope():
    api = _api()
    _, result = _assess(
        api,
        [_member(api, 2), _member(api, 4, size=None)],
        facts=_facts(api, start_key=(1, 1)),
    )

    assert result.status is api["SegmentAssessmentStatus"].UNCONFIRMED
    assert result.default_scope.state is api["DefaultScopeState"].EMPTY
    assert result.default_scope.basis is api["DefaultScopeBasis"].EMPTY_PREFIX
    assert result.default_scope.member_count == 0
    assert result.first_gap.start_key == api["SegmentKey"](1, 1)
    assert result.first_gap.end_key == api["SegmentKey"](1, 1)


def test_unknown_original_inside_candidate_prefix_keeps_scope_unknown():
    api = _api()
    _, result = _assess(api, [
        _member(api, 1),
        _member(api, 2, completion="unknown"),
        _member(api, 4),
    ])

    assert result.status is api["SegmentAssessmentStatus"].UNCONFIRMED
    assert result.default_scope.state is api["DefaultScopeState"].UNKNOWN
    assert result.default_scope.member_count is None


def test_unknown_ownership_anchor_does_not_hide_missing_positions_inside_owned_span():
    api = _api()
    members = [
        _member(api, 1),
        _member(api, 2, ownership="unknown"),
        _member(api, 6),
    ]

    state, result = _assess(api, members)

    assert result.status is api["SegmentAssessmentStatus"].UNCONFIRMED
    assert result.reason is api["SegmentAssessmentReason"].MEMBER_UNKNOWN
    assert result.gap_count == 1
    assert (result.first_gap.start_key, result.first_gap.end_key) == (
        api["SegmentKey"](1, 3), api["SegmentKey"](1, 5))
    assert result.first_gap.left_member_identity is None
    assert result.first_gap.left_neighbor_state.value == "unknown"
    assert result.first_gap.right_member_identity == members[2].identity
    assert result.first_gap.right_neighbor_state.value == "known"
    assert state.last_owned_cursor == members[2].cursor
    assert result.last_owned_cursor == members[2].cursor
    assert result.default_scope.state is api["DefaultScopeState"].UNKNOWN

    gaps = list(api["iter_segment_gaps"](
        members,
        last_nonzero_cursor=state.last_nonzero_cursor,
        last_owned_cursor=state.last_owned_cursor,
        start_key=api["SegmentKey"](1, 1),
    ))

    assert gaps == [result.first_gap]


def test_unknown_ownership_anchors_split_multiple_missing_intervals_between_owned_members():
    api = _api()
    members = [
        _member(api, 1),
        _member(api, 2, ownership="unknown"),
        _member(api, 4, ownership="unknown"),
        _member(api, 8),
    ]

    state, result = _assess(api, members)
    gaps = list(api["iter_segment_gaps"](
        members,
        last_nonzero_cursor=state.last_nonzero_cursor,
        last_owned_cursor=state.last_owned_cursor,
        start_key=api["SegmentKey"](1, 1),
    ))

    assert result.gap_count == len(gaps) == 2
    assert [(gap.start_key, gap.end_key) for gap in gaps] == [
        (api["SegmentKey"](1, 3), api["SegmentKey"](1, 3)),
        (api["SegmentKey"](1, 5), api["SegmentKey"](1, 7)),
    ]
    assert gaps[0].left_neighbor_state.value == "unknown"
    assert gaps[0].right_neighbor_state.value == "unknown"
    assert gaps[1].left_neighbor_state.value == "unknown"
    assert gaps[1].right_neighbor_state.value == "known"
    assert gaps[0] == result.first_gap


def test_unknown_ownership_after_last_owned_does_not_extend_confirmed_coverage():
    api = _api()
    members = [_member(api, 1), _member(api, 6, ownership="unknown")]

    state, result = _assess(api, members)
    gaps = list(api["iter_segment_gaps"](
        members,
        last_nonzero_cursor=state.last_nonzero_cursor,
        last_owned_cursor=state.last_owned_cursor,
        start_key=api["SegmentKey"](1, 1),
    ))

    assert state.last_owned_cursor == members[0].cursor
    assert result.gap_count == 0
    assert result.first_gap is None
    assert gaps == []


def test_unknown_prefix_without_trusted_start_does_not_create_initial_gaps():
    api = _api()
    members = [
        _member(api, 4, ownership="unknown"),
        _member(api, 8, ownership="unknown"),
        _member(api, 10),
    ]
    facts = _facts(api, start="unknown", start_key=None)

    state, result = _assess(api, members, facts=facts)
    gaps = list(api["iter_segment_gaps"](
        members,
        last_nonzero_cursor=state.last_nonzero_cursor,
        last_owned_cursor=state.last_owned_cursor,
        start_key=None,
    ))

    assert result.gap_count == 0
    assert result.first_gap is None
    assert gaps == []
    assert result.default_scope.state is api["DefaultScopeState"].UNKNOWN


def test_unknown_zero_byte_osv_is_neither_owned_zero_gap_nor_tail_file():
    api = _api()
    members = [
        _member(api, 1),
        _member(api, 2, ownership="unknown", size=0),
        _member(api, 3),
    ]

    state, result = _assess(api, members)
    gaps = list(api["iter_segment_gaps"](
        members,
        last_nonzero_cursor=state.last_nonzero_cursor,
        last_owned_cursor=state.last_owned_cursor,
        start_key=api["SegmentKey"](1, 1),
    ))

    assert result.status is api["SegmentAssessmentStatus"].UNCONFIRMED
    assert result.reason is api["SegmentAssessmentReason"].MEMBER_UNKNOWN
    assert result.gap_count == 0
    assert result.empty_tail_count is None
    assert state.trailing_zero_count == 0
    assert state.last_owned_cursor == members[2].cursor
    assert gaps == []


def test_confirmed_start_bounds_unknown_anchor_intervals_before_first_owned_member():
    api = _api()
    members = [
        _member(api, 4, ownership="unknown"),
        _member(api, 8, ownership="unknown"),
        _member(api, 10),
    ]
    facts = _facts(api, start_key=(1, 1))

    state, result = _assess(api, members, facts=facts)
    gaps = list(api["iter_segment_gaps"](
        members,
        last_nonzero_cursor=state.last_nonzero_cursor,
        last_owned_cursor=state.last_owned_cursor,
        start_key=facts.start_key,
    ))

    assert result.status is api["SegmentAssessmentStatus"].UNCONFIRMED
    assert result.reason is api["SegmentAssessmentReason"].MEMBER_UNKNOWN
    assert result.gap_count == len(gaps) == 3
    assert [(gap.start_key, gap.end_key) for gap in gaps] == [
        (api["SegmentKey"](1, 1), api["SegmentKey"](1, 3)),
        (api["SegmentKey"](1, 5), api["SegmentKey"](1, 7)),
        (api["SegmentKey"](1, 9), api["SegmentKey"](1, 9)),
    ]
    assert gaps[0].left_neighbor_state.value == "absent"
    assert gaps[0].right_neighbor_state.value == "unknown"
    assert gaps[0] == result.first_gap
    assert result.default_scope.state is api["DefaultScopeState"].EMPTY


def test_confirmed_start_key_is_frozen_with_accumulator_and_checked_at_finish():
    api = _api()
    state = api["SegmentAccumulator"](
        set_ref=_set_ref(api), target_duration_ms=12_000,
        confirmed_start_key=api["SegmentKey"](1, 1))

    with pytest.raises(ValueError, match="起点"):
        api["finish_segment_assessment"](
            state, _facts(api, start="unknown", start_key=None))


def test_unknown_position_before_confirmed_start_does_not_duplicate_start_gap():
    api = _api()
    members = [
        _member(api, 1, ownership="unknown"),
        _member(api, 4),
    ]
    facts = _facts(api, start_key=(1, 2))

    state, result = _assess(api, members, facts=facts)
    gaps = list(api["iter_segment_gaps"](
        members,
        last_nonzero_cursor=state.last_nonzero_cursor,
        last_owned_cursor=state.last_owned_cursor,
        start_key=facts.start_key,
    ))

    assert result.gap_count == len(gaps) == 1
    assert result.first_gap == gaps[0]
    assert (gaps[0].start_key, gaps[0].end_key) == (
        api["SegmentKey"](1, 2), api["SegmentKey"](1, 3))


def test_second_pass_rejects_a_different_last_owned_cursor():
    api = _api()
    members = [_member(api, 1), _member(api, 2)]

    with pytest.raises(ValueError, match="最后归属游标"):
        list(api["iter_segment_gaps"](
            members,
            last_nonzero_cursor=members[1].cursor,
            last_owned_cursor=members[0].cursor,
            start_key=api["SegmentKey"](1, 1),
        ))


def test_gap_neighbor_state_requires_a_real_identity_only_when_known():
    api = _api()
    neighbor = api["segment_models"].SegmentGapNeighborState
    values = dict(
        start_key=api["SegmentKey"](1, 3),
        end_key=api["SegmentKey"](1, 5),
        kind=api["SegmentGapKind"].MISSING_POSITION,
        left_member_identity=None,
        right_member_identity=None,
        left_neighbor_state=neighbor.KNOWN,
        right_neighbor_state=neighbor.UNKNOWN,
    )

    with pytest.raises(ValueError, match="邻接"):
        api["segment_models"].SegmentGap(**values)

    values["left_neighbor_state"] = neighbor.UNKNOWN
    values["left_member_identity"] = "/possibly-unowned/OSV"
    with pytest.raises(ValueError, match="邻接"):
        api["segment_models"].SegmentGap(**values)


def test_confirmed_overrecord_intersects_reliable_prefix_with_first_n_after_unknown():
    api = _api()
    members = [
        _member(api, 1),
        _member(api, 2),
        _member(api, 4),
        _member(api, 5, size=None),
    ]
    _, result = _assess(
        api, members, target_ms=4_500,
        facts=_facts(api, target_ms=4_500, overrun="confirmed"),
    )

    assert result.status is api["SegmentAssessmentStatus"].UNCONFIRMED
    assert result.default_scope.state is api["DefaultScopeState"].NONEMPTY
    assert result.default_scope.basis is api["DefaultScopeBasis"].PREFIX_FIRST_N
    assert result.default_scope.member_count == 1
    assert result.default_scope.first_cursor == members[0].cursor
    assert result.default_scope.last_cursor == members[0].cursor


def test_second_pass_keeps_known_gap_before_owned_unknown_suffix():
    api = _api()
    members = [
        _member(api, 1),
        _member(api, 2),
        _member(api, 4),
        _member(api, 5, completion="unknown"),
    ]
    state, result = _assess(api, members)

    gaps = list(api["iter_segment_gaps"](
        members,
        last_nonzero_cursor=state.last_nonzero_cursor,
        last_owned_cursor=state.last_owned_cursor,
        start_key=api["SegmentKey"](1, 1),
    ))

    assert [(gap.kind, gap.start_key, gap.end_key) for gap in gaps] == [
        (api["SegmentGapKind"].MISSING_POSITION,
         api["SegmentKey"](1, 3), api["SegmentKey"](1, 3)),
    ]
    assert len(gaps) == result.gap_count == 1
    assert gaps[0] == result.first_gap


def test_pending_zero_before_unknown_member_is_emitted_when_later_nonzero_arrives():
    api = _api()
    members = [
        _member(api, 1),
        _member(api, 2, size=0),
        _member(api, 4, size=None),
        _member(api, 5),
    ]
    state, result = _assess(api, members)

    gaps = list(api["iter_segment_gaps"](
        members,
        last_nonzero_cursor=state.last_nonzero_cursor,
        last_owned_cursor=state.last_owned_cursor,
        start_key=api["SegmentKey"](1, 1),
    ))

    assert [(gap.kind, gap.start_key, gap.end_key, gap.empty_member_identity)
            for gap in gaps] == [
        (api["SegmentGapKind"].ZERO_BYTE_NON_TAIL,
         api["SegmentKey"](1, 2), api["SegmentKey"](1, 2), members[1].identity),
        (api["SegmentGapKind"].MISSING_POSITION,
         api["SegmentKey"](1, 3), api["SegmentKey"](1, 3), None),
    ]
    assert len(gaps) == result.gap_count == 2
    assert gaps[0] == result.first_gap


def test_pending_zero_resolves_prefix_before_later_unknown_member():
    api = _api()
    members = [
        _member(api, 1),
        _member(api, 2, size=0),
        _member(api, 3, size=None),
        _member(api, 4),
    ]
    state, result = _assess(api, members)

    gaps = list(api["iter_segment_gaps"](
        members,
        last_nonzero_cursor=state.last_nonzero_cursor,
        last_owned_cursor=state.last_owned_cursor,
        start_key=api["SegmentKey"](1, 1),
    ))

    assert result.status is api["SegmentAssessmentStatus"].UNCONFIRMED
    assert result.reason is api["SegmentAssessmentReason"].MEMBER_UNKNOWN
    assert result.gap_count == 1
    assert result.first_gap.kind is api["SegmentGapKind"].ZERO_BYTE_NON_TAIL
    assert result.first_gap.empty_member_identity == members[1].identity
    assert result.default_scope.state is api["DefaultScopeState"].NONEMPTY
    assert result.default_scope.basis is api["DefaultScopeBasis"].CONTIGUOUS_PREFIX
    assert result.default_scope.member_count == 1
    assert result.default_scope.first_cursor == members[0].cursor
    assert result.default_scope.last_cursor == members[0].cursor
    assert gaps == [result.first_gap]


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


@pytest.mark.parametrize("preview_facts", [
    {"ownership": "unknown"},
    {"completion": "unknown"},
    {"size": None},
])
def test_unknown_optional_preview_blocks_success_without_changing_original_scope(preview_facts):
    api = _api()
    originals = [_member(api, 1), _member(api, 2)]
    preview = _member(
        api, 1, role="preview", pairing_state="paired",
        pairing_identity=originals[0].identity, **preview_facts)

    _, result = _assess(
        api, [originals[0], preview, originals[1]],
        facts=_facts(api, control="confirmed"),
    )

    assert result.status is api["SegmentAssessmentStatus"].UNCONFIRMED
    assert result.reason.value == "preview_unknown"
    assert getattr(result, "optional_preview_unknown", False) is True
    assert result.complete_segment_count == 1
    assert result.lower_bound_ms == 4_500
    assert result.gap_count == 0
    assert result.default_scope.state is api["DefaultScopeState"].NONEMPTY
    assert result.default_scope.member_count == 2
    assert result.default_scope.first_cursor == originals[0].cursor
    assert result.default_scope.last_cursor == originals[1].cursor


def test_unknown_preview_pairing_blocks_success_until_pairing_is_classified():
    api = _api()
    assert api["segment_models"].__dict__.get("PreviewPairingState") is not None, (
        "分段模型尚未表达预览配对核实状态")
    originals = [_member(api, 1), _member(api, 2)]
    preview = _member(api, 1, role="preview", pairing_state="unknown")

    _, result = _assess(
        api, [originals[0], preview, originals[1]],
        facts=_facts(api, control="confirmed"),
    )

    assert result.status is api["SegmentAssessmentStatus"].UNCONFIRMED
    assert result.reason.value == "preview_unknown"
    assert result.optional_preview_unknown is True
    assert result.complete_segment_count == 1
    assert result.lower_bound_ms == 4_500
    assert result.gap_count == 0
    assert result.default_scope.member_count == 2


def test_confirmed_preview_pairing_does_not_change_original_facts_or_success():
    api = _api()
    originals = [_member(api, 1), _member(api, 2)]
    preview = _member(
        api, 1, role="preview", pairing_state="paired",
        pairing_identity=originals[0].identity,
    )

    _, result = _assess(
        api, [originals[0], preview, originals[1]],
        facts=_facts(api, control="confirmed"),
    )

    assert result.status is api["SegmentAssessmentStatus"].SUCCEEDED
    assert result.reason is api["SegmentAssessmentReason"].NONE
    assert result.optional_preview_unknown is False
    assert result.complete_segment_count == 1
    assert result.lower_bound_ms == 4_500
    assert result.gap_count == 0
    assert result.default_scope.member_count == 2


@pytest.mark.parametrize("preview_facts", [
    {"size": 0, "pairing_state": "unknown"},
    {"completion": "incomplete", "pairing_state": "unknown"},
    {"ownership": "not_owned", "completion": "unknown", "size": None,
     "pairing_state": "unknown"},
    {"pairing_state": "unpaired"},
])
def test_reliably_ineligible_preview_does_not_block_control_success(preview_facts):
    api = _api()
    originals = [_member(api, 1), _member(api, 2)]
    preview = _member(api, 1, role="preview", **preview_facts)

    _, result = _assess(
        api, [originals[0], preview, originals[1]],
        facts=_facts(api, control="confirmed"),
    )

    assert result.status is api["SegmentAssessmentStatus"].SUCCEEDED
    assert result.reason is api["SegmentAssessmentReason"].NONE
    assert getattr(result, "optional_preview_unknown", False) is False
    assert result.complete_segment_count == 1
    assert result.lower_bound_ms == 4_500
    assert result.gap_count == 0
    assert result.default_scope.member_count == 2


def test_confirmed_unrelated_other_file_does_not_block_control_success():
    api = _api()
    originals = [_member(api, 1), _member(api, 2)]
    other = _member(api, 1, role="other", ownership="unknown",
                    completion="unknown", size=None)

    _, result = _assess(
        api, [originals[0], other, originals[1]],
        facts=_facts(api, control="confirmed"),
    )

    assert result.status is api["SegmentAssessmentStatus"].SUCCEEDED
    assert getattr(result, "optional_preview_unknown", False) is False
    assert result.complete_segment_count == 1
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
        last_owned_cursor=state.last_owned_cursor,
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


def test_long_zero_run_with_missing_position_is_replayed_without_summary_member_list():
    api = _api()
    members = [_member(api, 1)]
    members.extend(_member(api, index, size=0) for index in range(2, 102))
    members.extend(_member(api, index, size=0) for index in range(103, 203))
    members.append(_member(api, 203))

    state, result = _assess(api, members)
    gaps = list(api["iter_segment_gaps"](
        members,
        last_nonzero_cursor=state.last_nonzero_cursor,
        last_owned_cursor=state.last_owned_cursor,
        start_key=api["SegmentKey"](1, 1),
    ))

    assert not hasattr(state, "members")
    assert state.trailing_zero_count == 0
    assert result.gap_count == len(gaps) == 201
    assert [(gap.kind, gap.start_key, gap.end_key) for gap in gaps[:2]] == [
        (api["SegmentGapKind"].ZERO_BYTE_NON_TAIL,
         api["SegmentKey"](1, 2), api["SegmentKey"](1, 2)),
        (api["SegmentGapKind"].ZERO_BYTE_NON_TAIL,
         api["SegmentKey"](1, 3), api["SegmentKey"](1, 3)),
    ]
    missing = next(gap for gap in gaps if gap.kind is api["SegmentGapKind"].MISSING_POSITION)
    assert (missing.start_key, missing.end_key) == (
        api["SegmentKey"](1, 102), api["SegmentKey"](1, 102))
    assert gaps[99].empty_member_identity == members[100].identity
    assert gaps[100] == missing
    assert result.first_gap == gaps[0]


def test_second_pass_keeps_all_zero_files_as_tail_and_finds_trusted_start_gap():
    api = _api()
    all_empty = [_member(api, 1, size=0), _member(api, 2, size=0)]
    state, result = _assess(api, all_empty)
    empty_gaps = list(api["iter_segment_gaps"](
        all_empty, last_nonzero_cursor=state.last_nonzero_cursor,
        last_owned_cursor=state.last_owned_cursor,
        start_key=api["SegmentKey"](1, 1)))
    assert empty_gaps == []
    assert result.empty_tail_count == 2
    assert result.reason is api["SegmentAssessmentReason"].NO_NONZERO_ORIGINAL

    with_start_gap = [_member(api, 3), _member(api, 4)]
    state, result = _assess(
        api, with_start_gap, facts=_facts(api, start_key=(1, 1)))
    gaps = list(api["iter_segment_gaps"](
        with_start_gap, last_nonzero_cursor=state.last_nonzero_cursor,
        last_owned_cursor=state.last_owned_cursor,
        start_key=api["SegmentKey"](1, 1)))
    assert len(gaps) == result.gap_count == 1
    assert gaps[0] == result.first_gap
    assert gaps[0].start_key == api["SegmentKey"](1, 1)
    assert gaps[0].end_key == api["SegmentKey"](1, 2)
    assert result.default_scope.state is api["DefaultScopeState"].EMPTY

"""OSMO 普通录像分段的纯排序、下界与默认范围规则。"""

from dataclasses import replace
from collections.abc import Iterable, Iterator

from camctl.contracts.values import MAX_OBJECT_ID
from camctl.capture.segment_models import (
    CompletionBasis,
    CompletionFacts,
    DefaultScopeBasis,
    DefaultScopeState,
    DefaultSegmentScope,
    EvidenceState,
    FileCompletionState,
    OverrecordStatus,
    OwnershipState,
    PreviewPairingState,
    SegmentAccumulator,
    SegmentAssessment,
    SegmentAssessmentReason,
    SegmentAssessmentStatus,
    SegmentCursor,
    SegmentGap,
    SegmentGapKind,
    SegmentGapNeighborState,
    SegmentMember,
    SegmentMemberRole,
)
from camctl.devices.segments import SegmentKey


_SEGMENT_DURATION_MS = 4_500


def default_segment_count(target_duration_ms: int) -> int:
    """按精确毫秒返回异常多录的保守默认段数 N。"""
    if (type(target_duration_ms) is not int
            or not 1 <= target_duration_ms <= MAX_OBJECT_ID):
        raise ValueError("目标时长必须是正安全整数毫秒")
    return (target_duration_ms - 1) // _SEGMENT_DURATION_MS + 1


def _append_gap_count(
    state: SegmentAccumulator,
    gap: SegmentGap | None,
    count: int,
) -> SegmentAccumulator:
    if count == 0:
        return state
    first = state.first_gap
    if gap is not None and (first is None or gap.start_key < first.start_key):
        first = gap
    return replace(state, gap_count=state.gap_count + count, first_gap=first)


def _member_neighbor(member: SegmentMember) -> tuple[SegmentGapNeighborState, str | None]:
    if member.ownership is OwnershipState.OWNED:
        return SegmentGapNeighborState.KNOWN, member.identity
    assert member.ownership is OwnershipState.UNKNOWN
    return SegmentGapNeighborState.UNKNOWN, None


def _missing_interval(
    previous: SegmentMember,
    current: SegmentMember,
    *,
    start_key: SegmentKey | None = None,
) -> SegmentGap | None:
    """返回相邻位置锚点之间的压缩缺失区间，不枚举缺失位置。"""
    assert previous.key is not None and current.key is not None
    left_state, left_identity = _member_neighbor(previous)
    if start_key is not None and previous.key < start_key:
        first_missing = start_key
        left_state = SegmentGapNeighborState.ABSENT
        left_identity = None
    else:
        try:
            first_missing = previous.key.successor()
        except ValueError:
            # 最大可表示键之后不存在合法的设备位置。
            return None
    if first_missing >= current.key:
        return None
    last_missing = current.key.predecessor()
    if last_missing is None or last_missing < first_missing:
        return None
    return SegmentGap(
        start_key=first_missing,
        end_key=last_missing,
        kind=SegmentGapKind.MISSING_POSITION,
        left_member_identity=left_identity,
        right_member_identity=_member_neighbor(current)[1],
        left_neighbor_state=left_state,
        right_neighbor_state=_member_neighbor(current)[0],
    )


def _zero_gap(member: SegmentMember) -> SegmentGap:
    assert member.key is not None
    return SegmentGap(
        start_key=member.key,
        end_key=member.key,
        kind=SegmentGapKind.ZERO_BYTE_NON_TAIL,
        left_member_identity=None,
        right_member_identity=None,
        left_neighbor_state=SegmentGapNeighborState.UNKNOWN,
        right_neighbor_state=SegmentGapNeighborState.UNKNOWN,
        empty_member_identity=member.identity,
    )


def _stage_gap(state: SegmentAccumulator, gap: SegmentGap | None) -> SegmentAccumulator:
    if gap is None:
        return state
    first = state.pending_first_gap
    if first is None or gap.start_key < first.start_key:
        first = gap
    return replace(
        state,
        pending_gap_count=state.pending_gap_count + 1,
        pending_first_gap=first,
    )


def _commit_pending_gaps(state: SegmentAccumulator) -> SegmentAccumulator:
    if state.pending_gap_count == 0:
        return state
    state = _append_gap_count(state, state.pending_first_gap, state.pending_gap_count)
    return replace(state, pending_gap_count=0, pending_first_gap=None)


def _close_prefix_at_gap(
    state: SegmentAccumulator,
    gap: SegmentGap | None,
    cursor: SegmentCursor | None,
) -> SegmentAccumulator:
    if gap is None:
        return state
    unknown_cursor = state.prefix_unknown_cursor
    prefix_unknown = state.prefix_unknown
    if (prefix_unknown and unknown_cursor is not None
            and unknown_cursor.key >= gap.start_key):
        prefix_unknown = False
        unknown_cursor = None
    return replace(
        state,
        prefix_open=False,
        prefix_unknown=prefix_unknown,
        prefix_unknown_cursor=unknown_cursor,
        prefix_limit_cursor=cursor or state.prefix_limit_cursor,
    )


def _preview_needs_verification(member: SegmentMember) -> bool:
    """仅将可能成为有效预览但事实未定的 LRF 作为待核实输入。"""
    if member.ownership is OwnershipState.NOT_OWNED:
        return False
    if member.completion is FileCompletionState.INCOMPLETE:
        return False
    if member.pairing_state is PreviewPairingState.UNPAIRED:
        return False
    if (member.completion is FileCompletionState.COMPLETE
            and member.size_bytes == 0):
        return False
    return (
        member.ownership is OwnershipState.UNKNOWN
        or member.completion is FileCompletionState.UNKNOWN
        or member.size_bytes is None
        or member.pairing_state is PreviewPairingState.UNKNOWN
    )


def advance_segment_assessment(
    state: SegmentAccumulator,
    member: SegmentMember,
) -> SegmentAccumulator:
    """按一个有序结果成员推进 O(1) 累积状态。

    输入必须按 `SegmentKey` 排序。预览、其他文件和明确不归属本次的成员
    不改变原片事实。有可靠键的归属未知原片只占据可能位置，不贡献合格段；
    只有可靠起点或已确认归属原片提供左界，后续已确认归属原片提供右界时，
    才提交其间的缺口摘要。
    """
    if not isinstance(state, SegmentAccumulator):
        raise TypeError("分段推进状态必须是 SegmentAccumulator")
    if not isinstance(member, SegmentMember):
        raise TypeError("分段成员必须是 SegmentMember")

    if member.role is SegmentMemberRole.PREVIEW:
        if not _preview_needs_verification(member):
            return state
        return replace(state, optional_preview_unknown=True)
    if member.role is SegmentMemberRole.OTHER:
        return state
    if member.ownership is OwnershipState.NOT_OWNED:
        return state
    if member.key is None:
        prefix_unknown = state.prefix_unknown or state.prefix_open
        prefix_unknown_cursor = state.prefix_unknown_cursor
        return replace(
            state,
            unknown_original=True,
            order_unknown=True,
            prefix_open=False,
            prefix_unknown=prefix_unknown,
            prefix_unknown_cursor=prefix_unknown_cursor,
        )

    previous = state.last_position_anchor
    if previous is not None:
        assert previous.key is not None
        if member.key < previous.key:
            return replace(
                state,
                order_unknown=True,
                prefix_open=False,
                prefix_limit_cursor=member.cursor,
            )
        if member.key == previous.key:
            if member == previous:
                return state
            return replace(
                state,
                conflicting_key=True,
                order_unknown=True,
                unknown_original=True,
                prefix_open=False,
                prefix_limit_cursor=member.cursor,
            )

    cursor = member.cursor
    assert cursor is not None
    is_unknown_owner = member.ownership is OwnershipState.UNKNOWN
    boundary_known = (state.confirmed_start_key is not None
                      or state.first_original is not None)
    previous_in_scope = (previous is not None
                         and (state.confirmed_start_key is None
                              or previous.key >= state.confirmed_start_key))
    missing = (
        _missing_interval(previous, member, start_key=state.confirmed_start_key)
        if previous_in_scope and boundary_known and not state.order_unknown
        else None
    )
    if missing is not None:
        state = _stage_gap(state, missing)

    if is_unknown_owner:
        prefix_applies = (state.confirmed_start_key is None
                          or member.key >= state.confirmed_start_key)
        prefix_unknown = state.prefix_unknown
        prefix_unknown_cursor = state.prefix_unknown_cursor
        prefix_open = state.prefix_open
        if prefix_open and prefix_applies:
            prefix_unknown = True
            prefix_unknown_cursor = prefix_unknown_cursor or cursor
            prefix_open = False
        return replace(
            state,
            unknown_original=True,
            last_position_anchor=member,
            first_position_anchor=(state.first_position_anchor or
                                   (member if state.confirmed_start_key is None
                                    or member.key >= state.confirmed_start_key else None)),
            prefix_open=prefix_open,
            prefix_unknown=prefix_unknown,
            prefix_unknown_cursor=prefix_unknown_cursor,
            prefix_limit_cursor=cursor if prefix_applies else state.prefix_limit_cursor,
        )

    # 已归属的任何原片（即使写入或大小未知）都可闭合可能位置锚点之间的
    # 缺口，并提供第二遍的覆盖右界。
    closing_gap = state.pending_first_gap
    closing_cursor = cursor
    state = _commit_pending_gaps(state)
    if (member.completion is FileCompletionState.COMPLETE
            and member.size_bytes is not None and member.size_bytes > 0
            and state.trailing_zero_count):
        first_zero = state.trailing_zero_first
        assert first_zero is not None and first_zero.key is not None
        zero_gap = _zero_gap(first_zero)
        if closing_gap is None or zero_gap.start_key < closing_gap.start_key:
            closing_gap = zero_gap
            closing_cursor = first_zero.cursor
        state = _append_gap_count(state, zero_gap, state.trailing_zero_count)
        state = replace(state, trailing_zero_count=0, trailing_zero_first=None)
    state = _close_prefix_at_gap(state, closing_gap, closing_cursor)
    if member.completion is not FileCompletionState.COMPLETE or member.size_bytes is None:
        prefix_unknown = state.prefix_unknown
        prefix_unknown_cursor = state.prefix_unknown_cursor
        prefix_open = state.prefix_open
        if prefix_open:
            prefix_unknown = True
            prefix_unknown_cursor = prefix_unknown_cursor or cursor
            prefix_open = False
        return replace(
            state,
            last_original=member,
            first_original=state.first_original or member,
            last_position_anchor=member,
            first_position_anchor=(state.first_position_anchor or
                                   (member if state.confirmed_start_key is None
                                    or member.key >= state.confirmed_start_key else None)),
            last_owned_cursor=cursor,
            unknown_original=True,
            prefix_open=prefix_open,
            prefix_unknown=prefix_unknown,
            prefix_unknown_cursor=prefix_unknown_cursor,
            prefix_limit_cursor=cursor,
        )

    if member.size_bytes == 0:
        return replace(
            state,
            last_original=member,
            first_original=state.first_original or member,
            last_position_anchor=member,
            first_position_anchor=(state.first_position_anchor or
                                   (member if state.confirmed_start_key is None
                                    or member.key >= state.confirmed_start_key else None)),
            last_owned_cursor=cursor,
            trailing_zero_count=state.trailing_zero_count + 1,
            trailing_zero_first=state.trailing_zero_first or member,
        )

    # 只有位于首个缺口之前的连续、完整、非零原片才属于候选前缀。
    next_count = state.nonzero_original_count + 1
    first_n_cursor = state.first_n_cursor
    if next_count == default_segment_count(state.target_duration_ms):
        first_n_cursor = cursor
    state = replace(
        state,
        nonzero_original_count=next_count,
        first_n_cursor=first_n_cursor,
    )
    if state.prefix_open:
        if state.prefix_first_cursor is None:
            state = replace(
                state,
                prefix_first_cursor=cursor,
                prefix_last_cursor=cursor,
                prefix_count=state.prefix_count + 1,
            )
        elif state.prefix_last_cursor is not None:
            state = replace(
                state,
                prefix_last_cursor=cursor,
                prefix_count=state.prefix_count + 1,
            )

    return replace(
        state,
        last_original=member,
        first_original=state.first_original or member,
        last_position_anchor=member,
        first_position_anchor=(state.first_position_anchor or
                               (member if state.confirmed_start_key is None
                                or member.key >= state.confirmed_start_key else None)),
        last_owned_cursor=cursor,
        last_nonzero_cursor=cursor,
    )


def _start_gap(
    state: SegmentAccumulator,
    start_key: SegmentKey,
) -> SegmentGap | None:
    first = state.first_position_anchor
    if (state.last_owned_cursor is None or first is None
            or first.key is None or first.key <= start_key):
        return None
    end = first.key.predecessor()
    if end is None:
        return None
    return SegmentGap(
        start_key=start_key,
        end_key=end,
        kind=SegmentGapKind.MISSING_POSITION,
        left_member_identity=None,
        right_member_identity=_member_neighbor(first)[1],
        left_neighbor_state=SegmentGapNeighborState.ABSENT,
        right_neighbor_state=_member_neighbor(first)[0],
    )


def _collection_complete(facts: CompletionFacts) -> bool:
    return (facts.stop is EvidenceState.CONFIRMED
            and facts.file_wait is EvidenceState.CONFIRMED
            and facts.scan is EvidenceState.CONFIRMED)


def _scope(
    state: SegmentAccumulator,
    facts: CompletionFacts,
    *,
    overrecord: OverrecordStatus,
    first_gap: SegmentGap | None,
    gap_count: int,
) -> DefaultSegmentScope:
    ref = state.set_ref
    if (not _collection_complete(facts)
            or facts.start is not EvidenceState.CONFIRMED
            or state.order_unknown):
        return DefaultSegmentScope(
            state=DefaultScopeState.UNKNOWN,
            basis=DefaultScopeBasis.UNKNOWN,
            set_ref=ref,
            first_cursor=None,
            last_cursor=None,
            member_count=None,
        )

    # 第一份本次原片早于可靠启动键，说明顺序/归属前提自相矛盾。
    if (state.first_original is not None and state.first_original.key is not None
            and state.first_original.key < facts.start_key):
        return DefaultSegmentScope(
            state=DefaultScopeState.UNKNOWN,
            basis=DefaultScopeBasis.UNKNOWN,
            set_ref=ref,
            first_cursor=None,
            last_cursor=None,
            member_count=None,
        )

    start_gap = _start_gap(state, facts.start_key)
    if start_gap is not None:
        return DefaultSegmentScope(
            state=DefaultScopeState.EMPTY,
            basis=DefaultScopeBasis.EMPTY_PREFIX,
            set_ref=ref,
            first_cursor=None,
            last_cursor=None,
            member_count=0,
        )
    if state.prefix_unknown:
        return DefaultSegmentScope(
            state=DefaultScopeState.UNKNOWN,
            basis=DefaultScopeBasis.UNKNOWN,
            set_ref=ref,
            first_cursor=None,
            last_cursor=None,
            member_count=None,
        )
    has_gap = gap_count > 0 or first_gap is not None or start_gap is not None
    if has_gap:
        count = 0 if start_gap is not None else state.prefix_count
        basis = (DefaultScopeBasis.EMPTY_PREFIX if count == 0 else
                 DefaultScopeBasis.PREFIX_FIRST_N if overrecord is OverrecordStatus.CONFIRMED else
                 DefaultScopeBasis.CONTIGUOUS_PREFIX)
    elif overrecord is OverrecordStatus.CONFIRMED:
        count = state.nonzero_original_count
        basis = DefaultScopeBasis.FIRST_N
    else:
        count = state.nonzero_original_count
        basis = DefaultScopeBasis.ALL_QUALIFYING

    if overrecord is OverrecordStatus.CONFIRMED:
        count = min(count, default_segment_count(state.target_duration_ms))
    if count == 0:
        return DefaultSegmentScope(
            state=DefaultScopeState.EMPTY,
            basis=(basis if basis is not DefaultScopeBasis.UNKNOWN
                   else DefaultScopeBasis.EMPTY_PREFIX),
            set_ref=ref,
            first_cursor=None,
            last_cursor=None,
            member_count=0,
        )

    first_cursor = state.prefix_first_cursor if has_gap else state.first_original.cursor
    last_cursor = state.prefix_last_cursor if has_gap else state.last_nonzero_cursor
    assert first_cursor is not None and last_cursor is not None
    if overrecord is OverrecordStatus.CONFIRMED:
        qualifying_count = state.prefix_count if has_gap else state.nonzero_original_count
        if qualifying_count >= default_segment_count(state.target_duration_ms):
            assert state.first_n_cursor is not None
            last_cursor = state.first_n_cursor
        else:
            last_cursor = state.prefix_last_cursor if has_gap else state.last_nonzero_cursor

    return DefaultSegmentScope(
        state=DefaultScopeState.NONEMPTY,
        basis=basis,
        set_ref=ref,
        first_cursor=first_cursor,
        last_cursor=last_cursor,
        member_count=count,
    )


def iter_segment_gaps(
    members: Iterable[SegmentMember],
    *,
    last_nonzero_cursor: SegmentCursor | None,
    last_owned_cursor: SegmentCursor | None,
    start_key: SegmentKey | None,
) -> Iterator[SegmentGap]:
    """对冻结的有序成员做第二遍精确分类，逐项产生缺口事实。

    调用方必须提供同一冻结集合中的成员，并按设备顺序读取。函数只保留
    相邻位置锚点；有键的未知归属原片只占据可能位置，最后确认归属的原片
    提供覆盖截止。每份非尾部零字节 OSV 都带自己的真实身份，缺失位置压缩
    成一个包含起止键的区间。
    """
    if last_nonzero_cursor is not None and not isinstance(last_nonzero_cursor, SegmentCursor):
        raise TypeError("最后非零游标必须是 SegmentCursor 或未知")
    if last_owned_cursor is not None and not isinstance(last_owned_cursor, SegmentCursor):
        raise TypeError("最后归属游标必须是 SegmentCursor 或未知")
    if start_key is not None:
        if not isinstance(start_key, SegmentKey):
            raise TypeError("可信起点必须是 SegmentKey 或未知")

    previous_anchor = None
    boundary_established = start_key is not None
    last_ordered_key = None
    observed_last_nonzero = None
    observed_last_owned = None
    for member in members:
        if not isinstance(member, SegmentMember):
            raise TypeError("冻结集合成员必须是 SegmentMember")
        if member.role is not SegmentMemberRole.ORIGINAL:
            continue
        if member.ownership is OwnershipState.NOT_OWNED:
            continue
        if member.key is None:
            raise ValueError("缺口二次分类要求有关联原片具有可靠顺序键")
        if last_ordered_key is not None and member.key <= last_ordered_key:
            raise ValueError("冻结原片集合含重复或倒退的排序键")
        last_ordered_key = member.key

        cursor = member.cursor
        assert cursor is not None
        owned = member.ownership is OwnershipState.OWNED
        if owned:
            observed_last_owned = cursor
            if (member.completion is FileCompletionState.COMPLETE
                    and member.size_bytes is not None and member.size_bytes > 0):
                observed_last_nonzero = cursor

        if last_owned_cursor is None or member.key > last_owned_cursor.key:
            continue
        if (member.key == last_owned_cursor.key
                and cursor != last_owned_cursor):
            raise ValueError("最后归属游标与冻结成员身份不符")
        if owned and start_key is not None and member.key < start_key:
            raise ValueError("原片顺序早于可信录制起点")
        if (member.ownership is OwnershipState.UNKNOWN and start_key is not None
                and member.key < start_key):
            # 起点之前的归属未知文件不能充当本次录像范围的成员锚点。
            continue

        if previous_anchor is None:
            if start_key is not None and member.key > start_key:
                end = member.key.predecessor()
                if end is not None:
                    right_state, right_identity = _member_neighbor(member)
                    yield SegmentGap(
                        start_key=start_key,
                        end_key=end,
                        kind=SegmentGapKind.MISSING_POSITION,
                        left_member_identity=None,
                        right_member_identity=right_identity,
                        left_neighbor_state=SegmentGapNeighborState.ABSENT,
                        right_neighbor_state=right_state,
                    )
            previous_anchor = member
            boundary_established = (start_key is not None or owned)
        else:
            if boundary_established:
                missing = _missing_interval(
                    previous_anchor, member, start_key=start_key)
                if missing is not None:
                    yield missing
            previous_anchor = member
            if owned:
                boundary_established = True

        if (owned and member.completion is FileCompletionState.COMPLETE
                and member.size_bytes == 0 and last_nonzero_cursor is not None
                and member.key < last_nonzero_cursor.key):
            yield _zero_gap(member)

    if observed_last_nonzero != last_nonzero_cursor:
        raise ValueError("最后非零游标未与冻结集合中的末份非零原片吻合")
    if observed_last_owned != last_owned_cursor:
        raise ValueError("最后归属游标未与冻结集合中的末份归属原片吻合")


def finish_segment_assessment(
    state: SegmentAccumulator,
    facts: CompletionFacts,
) -> SegmentAssessment:
    """冻结有限摘要、结果依据、缺口结论及稳定默认范围。"""
    if not isinstance(state, SegmentAccumulator):
        raise TypeError("分段收束状态必须是 SegmentAccumulator")
    if not isinstance(facts, CompletionFacts):
        raise TypeError("完成事实必须是 CompletionFacts")
    expected_start_key = (facts.start_key
                          if facts.start is EvidenceState.CONFIRMED else None)
    if state.confirmed_start_key != expected_start_key:
        raise ValueError("累积器已确认起点必须与完成事实中的起点一致")

    start_gap = (_start_gap(state, facts.start_key)
                 if facts.start is EvidenceState.CONFIRMED else None)
    first_gap = start_gap or state.first_gap
    gap_count = state.gap_count + int(start_gap is not None)
    start_mismatch = (facts.start is EvidenceState.CONFIRMED
                      and state.first_original is not None
                      and state.first_original.key is not None
                      and state.first_original.key < facts.start_key)
    structurally_known = (not state.order_unknown and not state.unknown_original
                          and not start_mismatch)
    complete_count = (
        max(state.nonzero_original_count - 1, 0) if structurally_known else None
    )
    lower_bound = (complete_count * _SEGMENT_DURATION_MS
                   if complete_count is not None else None)
    final_collection = _collection_complete(facts)
    empty_tail_count = (
        state.trailing_zero_count
        if final_collection and structurally_known else None
    )

    lower_bound_proves_overrecord = (
        final_collection
        and facts.start is EvidenceState.CONFIRMED
        and structurally_known
        and gap_count == 0
        and lower_bound is not None
        and lower_bound > state.target_duration_ms
    )
    if (facts.control_overrun is EvidenceState.CONFIRMED
            or lower_bound_proves_overrecord):
        overrecord = OverrecordStatus.CONFIRMED
    elif facts.control_overrun is EvidenceState.REJECTED:
        overrecord = OverrecordStatus.NOT_CONFIRMED
    else:
        overrecord = OverrecordStatus.UNKNOWN

    scope = _scope(
        state,
        facts,
        overrecord=overrecord,
        first_gap=first_gap,
        gap_count=gap_count,
    )

    status = SegmentAssessmentStatus.UNCONFIRMED
    reason = SegmentAssessmentReason.COMPLETION_UNCONFIRMED
    basis = CompletionBasis.NONE
    if facts.explicit_error:
        status = SegmentAssessmentStatus.FAILED
        reason = SegmentAssessmentReason.EXPLICIT_ERROR
    elif facts.stop is not EvidenceState.CONFIRMED:
        reason = SegmentAssessmentReason.STOP_UNCONFIRMED
    elif facts.file_wait is not EvidenceState.CONFIRMED:
        reason = SegmentAssessmentReason.FILE_WAIT_UNCONFIRMED
    elif facts.scan is not EvidenceState.CONFIRMED:
        reason = SegmentAssessmentReason.SCAN_UNCONFIRMED
    elif state.order_unknown or start_mismatch:
        reason = SegmentAssessmentReason.ORDER_UNKNOWN
    elif state.unknown_original:
        reason = SegmentAssessmentReason.MEMBER_UNKNOWN
    elif state.nonzero_original_count == 0:
        status = SegmentAssessmentStatus.FAILED
        reason = SegmentAssessmentReason.NO_NONZERO_ORIGINAL
    elif gap_count:
        status = SegmentAssessmentStatus.FAILED
        reason = SegmentAssessmentReason.GAP
    elif state.optional_preview_unknown:
        reason = SegmentAssessmentReason.PREVIEW_UNKNOWN
    elif facts.control_completion is EvidenceState.CONFIRMED:
        status = SegmentAssessmentStatus.SUCCEEDED
        reason = SegmentAssessmentReason.NONE
        basis = CompletionBasis.CONTROL
    elif (facts.start is EvidenceState.CONFIRMED
            and lower_bound is not None
            and lower_bound >= state.target_duration_ms):
        status = SegmentAssessmentStatus.SUCCEEDED
        reason = SegmentAssessmentReason.NONE
        basis = CompletionBasis.SEGMENT_LOWER_BOUND
    elif facts.finite_check_complete:
        status = SegmentAssessmentStatus.FAILED
        reason = SegmentAssessmentReason.LOWER_BOUND_INSUFFICIENT
    else:
        reason = SegmentAssessmentReason.COMPLETION_UNCONFIRMED

    return SegmentAssessment(
        status=status,
        reason=reason,
        basis=basis,
        complete_segment_count=complete_count,
        lower_bound_ms=lower_bound,
        last_nonzero_cursor=state.last_nonzero_cursor,
        last_owned_cursor=state.last_owned_cursor,
        confirmed_start_key=state.confirmed_start_key,
        overrecord=overrecord,
        default_scope=scope,
        set_ref=state.set_ref,
        target_duration_ms=state.target_duration_ms,
        gap_count=gap_count,
        first_gap=first_gap,
        empty_tail_count=empty_tail_count,
        optional_preview_unknown=state.optional_preview_unknown,
    )

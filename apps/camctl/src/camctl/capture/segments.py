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
    SegmentAccumulator,
    SegmentAssessment,
    SegmentAssessmentReason,
    SegmentAssessmentStatus,
    SegmentCursor,
    SegmentGap,
    SegmentGapKind,
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


def _append_gap(state: SegmentAccumulator, gap: SegmentGap) -> SegmentAccumulator:
    first = state.first_gap
    if first is None or gap.start_key < first.start_key:
        first = gap
    return replace(state, gap_count=state.gap_count + 1, first_gap=first)


def _missing_interval(previous: SegmentMember, current: SegmentMember) -> SegmentGap | None:
    """返回两份相邻观察之间的压缩缺失区间，不枚举缺失位置。"""
    assert previous.key is not None and current.key is not None
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
        left_member_identity=previous.identity,
        right_member_identity=current.identity,
    )


def _zero_gap(member: SegmentMember) -> SegmentGap:
    assert member.key is not None
    return SegmentGap(
        start_key=member.key,
        end_key=member.key,
        kind=SegmentGapKind.ZERO_BYTE_NON_TAIL,
        left_member_identity=None,
        right_member_identity=None,
        empty_member_identity=member.identity,
    )


def advance_segment_assessment(
    state: SegmentAccumulator,
    member: SegmentMember,
) -> SegmentAccumulator:
    """按一个有序结果成员推进 O(1) 累积状态。

    输入必须按 `SegmentKey` 排序。预览、其他文件和明确不归属本次的成员
    不改变原片事实。归属未知或顺序冲突会保守地阻止完成与默认范围结论。
    """
    if not isinstance(state, SegmentAccumulator):
        raise TypeError("分段推进状态必须是 SegmentAccumulator")
    if not isinstance(member, SegmentMember):
        raise TypeError("分段成员必须是 SegmentMember")

    if member.role is not SegmentMemberRole.ORIGINAL:
        return state
    if member.ownership is OwnershipState.NOT_OWNED:
        return state
    if member.ownership is OwnershipState.UNKNOWN:
        cursor = member.cursor
        return replace(
            state,
            unknown_original=True,
            order_unknown=True,
            prefix_open=False,
            prefix_limit_cursor=cursor or state.prefix_limit_cursor,
        )
    if member.key is None:
        return replace(
            state,
            unknown_original=True,
            order_unknown=True,
            prefix_open=False,
        )

    previous = state.last_original
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

    if previous is not None:
        missing = _missing_interval(previous, member)
        if missing is not None:
            state = _append_gap(state, missing)
            state = replace(
                state,
                prefix_open=False,
                prefix_limit_cursor=member.cursor,
            )

    if member.completion is not FileCompletionState.COMPLETE or member.size_bytes is None:
        # 仍保存可靠顺序锚点；该原片的资格和尾段分类保持未知。
        return replace(
            state,
            last_original=member,
            first_original=state.first_original or member,
            unknown_original=True,
            prefix_open=False,
            prefix_limit_cursor=member.cursor,
            trailing_zero_count=0,
            trailing_zero_first=None,
        )

    if member.size_bytes == 0:
        return replace(
            state,
            last_original=member,
            first_original=state.first_original or member,
            trailing_zero_count=state.trailing_zero_count + 1,
            trailing_zero_first=state.trailing_zero_first or member,
        )

    # 后续非零原片使之前尚待分类的零 OSV 成为非尾部缺口。
    if state.trailing_zero_count:
        first_zero = state.trailing_zero_first
        assert first_zero is not None and first_zero.key is not None
        zero_gap = _zero_gap(first_zero)
        state = _append_gap(state, zero_gap)
        # 多个待分类空文件仍以固定状态保留计数；完整逐文件诊断由持久化层
        # 从已保存的成员页补齐，不在累积器中保存无界的身份列表。
        if state.trailing_zero_count > 1:
            state = replace(
                state,
                gap_count=state.gap_count + state.trailing_zero_count - 1,
            )
        if state.prefix_open:
            state = replace(
                state,
                prefix_open=False,
                prefix_limit_cursor=first_zero.cursor,
            )
        state = replace(
            state,
            trailing_zero_count=0,
            trailing_zero_first=None,
        )

    cursor = member.cursor
    assert cursor is not None
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
        last_nonzero_cursor=cursor,
    )


def _start_gap(
    state: SegmentAccumulator,
    start_key,
) -> SegmentGap | None:
    first = state.first_original
    if first is None or first.key is None or first.key <= start_key:
        return None
    end = first.key.predecessor()
    if end is None:
        return None
    return SegmentGap(
        start_key=start_key,
        end_key=end,
        kind=SegmentGapKind.MISSING_POSITION,
        left_member_identity=None,
        right_member_identity=first.identity,
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
            or state.order_unknown
            or state.unknown_original):
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
    start_key: SegmentKey | None,
) -> Iterator[SegmentGap]:
    """对冻结的有序成员做第二遍精确分类，逐项产生缺口事实。

    调用方必须提供同一冻结集合中的成员，并按设备顺序读取。函数只保留
    相邻原片和一个前瞻成员；每份非尾部零字节 OSV 都带自己的真实身份，
    缺失位置则压缩成一个包含起止键的区间。
    """
    if last_nonzero_cursor is not None and not isinstance(last_nonzero_cursor, SegmentCursor):
        raise TypeError("最后非零游标必须是 SegmentCursor 或未知")
    if start_key is not None:
        if not isinstance(start_key, SegmentKey):
            raise TypeError("可信起点必须是 SegmentKey 或未知")

    def originals() -> Iterator[SegmentMember]:
        for member in members:
            if not isinstance(member, SegmentMember):
                raise TypeError("冻结集合成员必须是 SegmentMember")
            if member.role is not SegmentMemberRole.ORIGINAL:
                continue
            if member.ownership is OwnershipState.NOT_OWNED:
                continue
            if member.ownership is not OwnershipState.OWNED:
                raise ValueError("缺口二次分类要求原片归属已经确认")
            if member.key is None:
                raise ValueError("缺口二次分类要求每份原片具有可靠顺序键")
            if (member.completion is not FileCompletionState.COMPLETE
                    or member.size_bytes is None):
                raise ValueError("缺口二次分类要求原片写入状态及大小已经确认")
            yield member

    source = iter(originals())
    previous = None
    current = next(source, None)
    if (current is not None and start_key is not None
            and current.key < start_key):
        raise ValueError("原片顺序早于可信录制起点")
    if current is not None and start_key is not None and current.key > start_key:
        end = current.key.predecessor()
        if end is not None:
            yield SegmentGap(
                start_key=start_key,
                end_key=end,
                kind=SegmentGapKind.MISSING_POSITION,
                left_member_identity=None,
                right_member_identity=current.identity,
            )

    observed_last_nonzero = None
    while current is not None:
        following = next(source, None)
        if previous is not None:
            if current.key <= previous.key:
                raise ValueError("冻结原片集合含重复或倒退的排序键")
            missing = _missing_interval(previous, current)
            if missing is not None:
                yield missing

        cursor = current.cursor
        if current.size_bytes > 0:
            observed_last_nonzero = cursor
        elif (last_nonzero_cursor is not None
                and current.key < last_nonzero_cursor.key):
            yield _zero_gap(current)

        previous, current = current, following

    if observed_last_nonzero != last_nonzero_cursor:
        raise ValueError("最后非零游标未与冻结集合中的末份非零原片吻合")


def finish_segment_assessment(
    state: SegmentAccumulator,
    facts: CompletionFacts,
) -> SegmentAssessment:
    """冻结有限摘要、结果依据、缺口结论及稳定默认范围。"""
    if not isinstance(state, SegmentAccumulator):
        raise TypeError("分段收束状态必须是 SegmentAccumulator")
    if not isinstance(facts, CompletionFacts):
        raise TypeError("完成事实必须是 CompletionFacts")

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
        overrecord=overrecord,
        default_scope=scope,
        set_ref=state.set_ref,
        target_duration_ms=state.target_duration_ms,
        gap_count=gap_count,
        first_gap=first_gap,
        empty_tail_count=empty_tail_count,
    )

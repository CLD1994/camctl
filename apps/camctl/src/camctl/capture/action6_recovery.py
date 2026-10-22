"""按冻结视频顺序检查实际内容，固定达到目标的完整文件前缀。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable

from camctl.capture.segment_models import (
    FileCompletionState, OwnershipState, SegmentCursor, SegmentKey,
    SegmentMemberRole,
)
from camctl.capture.video_coverage import (
    CoverageAccumulator, CoverageDecision, CoverageFinishFacts, CoverageTarget,
    VideoMetric, advance_coverage, finish_coverage,
)


@dataclass(frozen=True)
class CheckedPrefix:
    coverage: CoverageDecision
    first_cursor: SegmentCursor | None
    last_cursor: SegmentCursor | None


async def inspect_continuous_prefix(
    members: Iterable,
    target: CoverageTarget,
    *,
    start: SegmentKey | None,
    collection_complete: bool,
    inspect: Callable[[object], Awaitable[VideoMetric | None]],
) -> CheckedPrefix | None:
    """逐个消费已冻结成员；None 表示当前检查仍在推进。

    inspect 返回可靠指标或带原因的未知指标，尚在读取时返回 None。
    已保存指标由检查仓储恢复，因而重入不会重新拷贝已完成文件。
    照片和预览不提供顺序、起点或覆盖量。
    """
    state = CoverageAccumulator(target)
    first = last = None
    reason = None
    if start is None:
        reason = "start_unknown"
    else:
        expected = start
        for stored in members:
            member = stored.member
            if member.role is not SegmentMemberRole.ORIGINAL:
                continue
            if member.key != expected:
                reason = "order_unknown" if member.key is None else "segment_gap"
                break
            if (member.ownership is not OwnershipState.OWNED
                    or member.completion is not FileCompletionState.COMPLETE
                    or member.size_bytes is None):
                reason = "file_unconfirmed"
                break
            if member.size_bytes == 0:
                reason = "empty_original"
                break
            metric = await inspect(stored)
            if metric is None:
                return None
            state = advance_coverage(state, metric)
            if state.interruption_reason is not None:
                reason = state.interruption_reason
                break
            if first is None:
                first = member.cursor
            last = member.cursor
            if state.reached:
                break
            expected = member.key.successor()
    decision = finish_coverage(state, CoverageFinishFacts(
        collection_complete=collection_complete and reason is None,
        interruption_reason=reason,
    ))
    return CheckedPrefix(decision, first, last)

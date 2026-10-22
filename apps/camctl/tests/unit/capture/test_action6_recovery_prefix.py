"""实际内容前缀规则；检查端口只提供真实合同中的逐段指标。"""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from camctl.capture.action6_recovery import inspect_continuous_prefix
from camctl.capture.segment_models import (
    FileCompletionState, OwnershipState, SegmentKey, SegmentMember,
    SegmentMemberRole,
)
from camctl.capture.video_coverage import CoverageMetric, CoverageTarget, VideoMetric


def members(count):
    return tuple(SimpleNamespace(member_id=index, member=SegmentMember(
        f"video-{index}", SegmentKey(1, index), SegmentMemberRole.ORIGINAL,
        OwnershipState.OWNED, FileCompletionState.COMPLETE, 10,
    )) for index in range(1, count + 1))


@pytest.mark.asyncio
@pytest.mark.parametrize("metric,target,values,confirmed,count,status", [
    (CoverageMetric.DURATION_S, Decimal(60), (18, 27, 22, 40), Decimal(67), 3, "reached"),
    (CoverageMetric.DURATION_S, Decimal(60), (18, 27, None, 40), Decimal(45), 2, "unconfirmed"),
    (CoverageMetric.DURATION_S, Decimal(60), (None, 27, 22), Decimal(0), 0, "unconfirmed"),
    (CoverageMetric.DURATION_S, Decimal(60), (18, 27), Decimal(45), 2, "confirmed_short"),
    (CoverageMetric.FRAMES, 151, (60, 60, 45, 20), 165, 3, "reached"),
])
async def test_checks_only_necessary_whole_files(metric, target, values, confirmed, count, status):
    calls = []

    async def inspect(stored):
        calls.append(stored.member_id)
        value = values[stored.member_id - 1]
        if value is None:
            return VideoMetric(metric, None, "metric_unknown")
        return VideoMetric(metric, Decimal(value) if metric is CoverageMetric.DURATION_S else value)

    result = await inspect_continuous_prefix(members(len(values)), CoverageTarget(metric, target),
        start=SegmentKey(1, 1), collection_complete=True, inspect=inspect)
    assert result.coverage.status.value == status
    assert result.coverage.confirmed == confirmed
    assert result.coverage.confirmed_segments == count
    assert calls == list(range(1, min(count + (status == "unconfirmed"), len(values)) + 1))
    assert (result.last_cursor.key.index if result.last_cursor else 0) == count


@pytest.mark.asyncio
async def test_unknown_start_does_not_inspect_or_skip_forward():
    async def inspect(stored):
        pytest.fail("未知起点不能检查后续视频来猜测连续覆盖")

    result = await inspect_continuous_prefix(members(2),
        CoverageTarget(CoverageMetric.FRAMES, 151), start=None,
        collection_complete=True, inspect=inspect)
    assert result.coverage.status.value == "unconfirmed"
    assert result.coverage.confirmed_segments == 0
    assert result.coverage.reason == "start_unknown"


@pytest.mark.asyncio
async def test_pending_copy_keeps_source_action_open():
    async def inspect(stored):
        return None

    result = await inspect_continuous_prefix(members(1),
        CoverageTarget(CoverageMetric.FRAMES, 151), start=SegmentKey(1, 1),
        collection_complete=True, inspect=inspect)
    assert result is None


@pytest.mark.asyncio
async def test_foreign_original_with_unknown_key_does_not_interrupt_prefix():
    foreign = SimpleNamespace(member_id=0, member=SegmentMember(
        "foreign-video", None, SegmentMemberRole.ORIGINAL,
        OwnershipState.NOT_OWNED, FileCompletionState.UNKNOWN, None,
    ))
    source = members(1)[0]
    calls = []

    async def inspect(stored):
        calls.append(stored.member_id)
        return VideoMetric(CoverageMetric.FRAMES, 151)

    result = await inspect_continuous_prefix((foreign, source),
        CoverageTarget(CoverageMetric.FRAMES, 151), start=SegmentKey(1, 1),
        collection_complete=True, inspect=inspect)

    assert result.coverage.status.value == "reached"
    assert result.coverage.confirmed_segments == 1
    assert calls == [source.member_id]


@pytest.mark.asyncio
async def test_incomplete_collection_is_not_confirmed_short():
    async def inspect(stored):
        return VideoMetric(CoverageMetric.FRAMES, 60)

    result = await inspect_continuous_prefix(members(1),
        CoverageTarget(CoverageMetric.FRAMES, 151), start=SegmentKey(1, 1),
        collection_complete=False, inspect=inspect)
    assert result.coverage.status.value == "unconfirmed"
    assert result.coverage.confirmed == 60

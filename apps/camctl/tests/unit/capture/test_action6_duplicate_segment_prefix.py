"""重复键前缀过滤照片与未归属原片。"""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from camctl.capture.action6_recovery import inspect_continuous_prefix
from camctl.capture.segment_models import (
    FileCompletionState,
    OwnershipState,
    SegmentKey,
    SegmentMember,
    SegmentMemberRole,
)
from camctl.contracts.video_coverage import CoverageMetric, CoverageTarget, VideoMetric


def _stored(member_id, identity, key, role, ownership):
    return SimpleNamespace(
        member_id=member_id,
        member=SegmentMember(
            identity,
            key,
            role,
            ownership,
            FileCompletionState.COMPLETE,
            10,
        ),
    )


@pytest.mark.asyncio
async def test_photo_and_not_owned_duplicate_keys_do_not_break_action6_prefix():
    start = SegmentKey(1, 1)
    members = (
        _stored(
            1, "original-1.mp4", start,
            SegmentMemberRole.ORIGINAL, OwnershipState.OWNED,
        ),
        _stored(
            2, "foreign-1.mp4", start,
            SegmentMemberRole.ORIGINAL, OwnershipState.NOT_OWNED,
        ),
        _stored(
            3, "photo-1.jpg", start,
            SegmentMemberRole.OTHER, OwnershipState.OWNED,
        ),
        _stored(
            4, "original-2.mp4", SegmentKey(1, 2),
            SegmentMemberRole.ORIGINAL, OwnershipState.OWNED,
        ),
    )
    inspected = []

    async def inspect(stored):
        inspected.append(stored.member_id)
        return VideoMetric(CoverageMetric.FRAMES, 60 if stored.member_id == 1 else 91)

    result = await inspect_continuous_prefix(
        members,
        CoverageTarget(CoverageMetric.FRAMES, 151),
        start=start,
        collection_complete=True,
        inspect=inspect,
    )

    assert result.coverage.status.value == "reached"
    assert result.coverage.confirmed == 151
    assert result.coverage.confirmed_segments == 2
    assert inspected == [1, 4]

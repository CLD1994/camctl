"""Action6 重复顺序键只中断连续覆盖前缀，不抹去前缀。"""

from decimal import Decimal

import pytest

from camctl.capture.segment_models import SegmentKey
from camctl.persistence.repositories.segments import SegmentRepository

from .test_action6_segmented_recovery import (
    _CONTENT,
    _entry,
    _overrun_recording,
    _publish_ordinary_result,
)

pytestmark = pytest.mark.asyncio


def _video(index: int, *, timestamp: str = "20261022100000"):
    return _entry(
        f"DJI_001/DJI_{timestamp}_{index:04d}_D.MP4",
        size=len(_CONTENT),
    )


def _photo(index: int):
    return _entry(
        f"DJI_001/IMG_{index:04d}.JPG",
        size=len(_CONTENT),
    )


def _sorted(entries):
    return sorted(entries, key=lambda entry: entry["identity"])


async def test_late_duplicate_preserves_reached_prefix_without_probing_after_target(
    tmp_path,
):
    entries = _sorted([
        _video(7), _video(8), _video(9),
        _video(10), _video(10, timestamp="20261022100100"),
    ])
    owned, runtime, action_id, handler, tools = await _overrun_recording(
        tmp_path, (Decimal("18"), Decimal("27"), Decimal("22"), Decimal("40"))
    )
    try:
        await _publish_ordinary_result(runtime, owned, action_id, handler, entries)

        repository = SegmentRepository()
        ref = repository.read_active_set(action_id, owned)
        assert ref is not None
        assert repository.read_start(ref, owned).key == SegmentKey(1, 7)
        decision = repository.read_decision(ref, owned)
        assert decision.status.value == "succeeded"
        assert decision.coverage_status.value == "reached"
        assert decision.confirmed == Decimal("67")
        assert decision.checked_count == 3
        assert decision.default_scope.state.value == "nonempty"
        assert decision.default_scope.member_count == 3
        assert len(tools.calls) == 3
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM outputs WHERE source_action_id=?", (action_id,)
        ).fetchone() == (5,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM segment_scope_members WHERE set_id=?",
            (repository.read_set(ref, owned)["id"],),
        ).fetchone() == (3,)
    finally:
        owned.connection.close()


async def test_duplicate_at_video_start_keeps_unknown_scope_and_does_not_probe(tmp_path):
    entries = _sorted([
        _video(7), _video(7, timestamp="20261022100100"), _video(8),
    ])
    owned, runtime, action_id, handler, tools = await _overrun_recording(
        tmp_path, (Decimal("18"), Decimal("27"), Decimal("22"))
    )
    try:
        await _publish_ordinary_result(runtime, owned, action_id, handler, entries)

        repository = SegmentRepository()
        ref = repository.read_active_set(action_id, owned)
        assert ref is not None
        assert repository.read_start(ref, owned).key is None
        decision = repository.read_decision(ref, owned)
        assert decision.status.value == "succeeded"
        assert decision.coverage_status.value == "unconfirmed"
        assert decision.checked_count == 0
        assert decision.default_scope.state.value == "unknown"
        assert decision.default_scope.member_count is None
        assert tools.calls == []
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM outputs WHERE source_action_id=?", (action_id,)
        ).fetchone() == (3,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM segment_scope_members WHERE set_id=?",
            (repository.read_set(ref, owned)["id"],),
        ).fetchone() == (0,)
    finally:
        owned.connection.close()


async def test_duplicate_split_across_member_pages_keeps_only_prior_video_prefix(
    tmp_path, monkeypatch,
):
    # Use real two-member repository pages. The two photos fill page 1; key 1
    # and the first key-2 member fill page 2; the duplicate starts page 3.
    from camctl.persistence.repositories import segments as segment_repository

    read_members = segment_repository.read_segment_members

    def small_member_pages(ref, cursor, batch, owned):
        return read_members(ref, cursor, min(batch, 2), owned)

    monkeypatch.setattr(segment_repository, "read_segment_members", small_member_pages)
    entries = [
        _photo(1), _photo(2), _video(1), _video(2),
        _video(2, timestamp="20261022100100"), _video(3),
    ]
    owned, runtime, action_id, handler, tools = await _overrun_recording(
        tmp_path, (Decimal("0.1"), Decimal("18"), Decimal("27"))
    )
    try:
        await _publish_ordinary_result(
            runtime, owned, action_id, handler, _sorted(entries)
        )

        repository = SegmentRepository()
        ref = repository.read_active_set(action_id, owned)
        assert ref is not None
        pages = []
        cursor = None
        while True:
            page = repository.read_members(ref, cursor, 2, owned)
            pages.append(page)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        assert pages[1].items[-1].member.key == SegmentKey(1, 2)
        assert pages[2].items[0].member.key == SegmentKey(1, 2)
        assert repository.read_start(ref, owned).key == SegmentKey(1, 1)
        decision = repository.read_decision(ref, owned)
        assert decision.status.value == "succeeded"
        assert decision.coverage_status.value == "unconfirmed"
        assert decision.confirmed == Decimal("0.1")
        assert decision.checked_count == 1
        assert decision.default_scope.state.value == "nonempty"
        assert decision.default_scope.member_count == 1
        assert len(tools.calls) == 1
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM outputs WHERE source_action_id=?", (action_id,)
        ).fetchone() == (6,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM segment_scope_members WHERE set_id=?",
            (repository.read_set(ref, owned)["id"],),
        ).fetchone() == (1,)
    finally:
        owned.connection.close()

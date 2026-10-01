"""C6 文件归属、集合核实及占用释放的单元测试。

文件归属、单文件写完与集合齐备分别判断，任何一项不由另一项推
导；一份文件写完不完成集合；读取错误不解释为缺失；集合已定仍
缺必需类别是明确不满足；ENDED+HELD 不表示仍拍摄，归属未定时
同范围新拍摄不得放行。
"""

from __future__ import annotations

import pytest

from camctl.capture.results import (
    ActivityFacts,
    ActivityState,
    CaptureFile,
    CaptureFileSet,
    FileKind,
    OccupancyState,
    ProductRequirements,
    ReleaseDecision,
    assess_capture_files,
    decide_release,
)

pytestmark = pytest.mark.asyncio

_VIDEO = ProductRequirements(required_kinds=frozenset({FileKind.VIDEO}))
_PHOTO_VIDEO = ProductRequirements(
    required_kinds=frozenset({FileKind.PHOTO, FileKind.VIDEO})
)


def _file(
    file_id: str,
    kind: FileKind = FileKind.VIDEO,
    *,
    complete: bool = True,
    owned: bool = True,
    read_error: bool = False,
) -> CaptureFile:
    return CaptureFile(
        file_id=file_id,
        kind=kind,
        complete=complete,
        ownership_confirmed=owned,
        read_error=read_error,
    )


def _set(files, *, finalized: bool = True) -> CaptureFileSet:
    return CaptureFileSet(files=tuple(files), set_finalized=finalized)


class TestAssess:
    async def test_written_file_does_not_complete_set(self) -> None:
        """一份文件已写完但集合未齐：整体不完整。"""
        assessment = assess_capture_files(
            _set([_file("v.mp4")]), _PHOTO_VIDEO
        )
        assert assessment.is_complete is False
        assert assessment.missing_kinds == (FileKind.PHOTO,)

    async def test_complete_set_passes(self) -> None:
        assessment = assess_capture_files(
            _set([_file("a.jpg", FileKind.PHOTO), _file("v.mp4")]), _PHOTO_VIDEO
        )
        assert assessment.is_complete is True
        assert assessment.missing_kinds == ()

    async def test_legal_empty_set(self) -> None:
        empty = ProductRequirements(required_kinds=frozenset())
        assessment = assess_capture_files(_set([]), empty)
        assert assessment.is_complete is True

    async def test_missing_required_kind_while_pending(self) -> None:
        """集合尚未确定：缺类别按暂未齐处理，不判明确不满足。"""
        assessment = assess_capture_files(
            _set([_file("a.jpg", FileKind.PHOTO)], finalized=False), _PHOTO_VIDEO
        )
        assert assessment.is_complete is False
        assert assessment.explicitly_unmet is False

    async def test_missing_required_kind_after_finalized(self) -> None:
        """集合已确定仍缺必需类别：明确不满足，不当作暂时未知。"""
        assessment = assess_capture_files(
            _set([_file("a.jpg", FileKind.PHOTO)], finalized=True), _PHOTO_VIDEO
        )
        assert assessment.explicitly_unmet is True

    async def test_read_error_is_not_missing(self) -> None:
        assessment = assess_capture_files(
            _set([_file("v.mp4", read_error=True)]), _VIDEO
        )
        assert assessment.is_complete is False
        assert assessment.explicitly_unmet is False
        assert assessment.missing_kinds == ()
        assert assessment.read_errors == ("v.mp4",)

    async def test_unowned_file_blocks_registration(self) -> None:
        """归属未确认的文件不能进入集合判定。"""
        assessment = assess_capture_files(
            _set([_file("v.mp4", owned=False)]), _VIDEO
        )
        assert assessment.is_complete is False
        assert assessment.ownership_pending == ("v.mp4",)

    async def test_incomplete_file_is_waited_not_unmet(self) -> None:
        assessment = assess_capture_files(
            _set([_file("v.mp4", complete=False)]), _VIDEO
        )
        assert assessment.is_complete is False
        assert assessment.incomplete_files == ("v.mp4",)
        assert assessment.explicitly_unmet is False


class TestRelease:
    def facts(**overrides) -> ActivityFacts:
        values = dict(
            activity_state=ActivityState.ENDED,
            occupancy_state=OccupancyState.HELD,
            completion_evidence=True,
            unresolved_calls=False,
            file_ownership_resolved=True,
        )
        values.update(overrides)
        return ActivityFacts(**values)

    async def test_ended_held_does_not_imply_recording(self) -> None:
        """ENDED+HELD 不表示仍拍摄；没有完成依据时不释放也不判运行。"""
        decision = decide_release(
            TestRelease.facts(completion_evidence=False)
        )
        assert decision is ReleaseDecision.KEEP_HELD_UNKNOWN

    async def test_release_requires_evidence_calls_and_ownership(self) -> None:
        assert decide_release(TestRelease.facts()) is ReleaseDecision.RELEASE
        assert (
            decide_release(TestRelease.facts(unresolved_calls=True))
            is ReleaseDecision.KEEP_HELD_CALLS
        )
        assert (
            decide_release(TestRelease.facts(file_ownership_resolved=False))
            is ReleaseDecision.KEEP_HELD_OWNERSHIP
        )

    async def test_unresolved_scope_blocks_new_capture(self) -> None:
        """文件归属未定：同范围新拍摄不得放行。"""
        decision = decide_release(
            TestRelease.facts(
                activity_state=ActivityState.ACTIVE,
                completion_evidence=False,
                file_ownership_resolved=False,
            )
        )
        assert decision is ReleaseDecision.KEEP_HELD_OWNERSHIP

    async def test_active_activity_keeps_occupancy(self) -> None:
        decision = decide_release(
            TestRelease.facts(activity_state=ActivityState.ACTIVE)
        )
        assert decision is ReleaseDecision.KEEP_HELD_ACTIVE

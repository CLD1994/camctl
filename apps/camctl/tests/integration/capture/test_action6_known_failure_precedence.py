"""明确的视频成员错误优先于控制侧完成，但保留先前可取的检查前缀。"""

from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.capture.handlers import capture_handler
from camctl.capture.segment_completion import _control_facts
from camctl.capture.segment_models import EvidenceState
from camctl.contracts.enums import enum_for
from camctl.host_files.media import MediaProbe
from camctl.host_files.tasks import FileTaskId, FileTaskResult
from camctl.persistence.repositories.segment_inspections import (
    read_inspection_for_member,
)
from camctl.persistence.repositories.segments import SegmentRepository
from camctl.persistence.transaction import row_facts

from .test_action6_segmented_normal import _entry
from .test_action6_segmented_recovery import (
    _CONTENT,
    _overrun_recording,
    _publish_ordinary_result,
)

pytestmark = pytest.mark.asyncio


def _video(index: int, *, size: int = len(_CONTENT)):
    return _entry(
        f"DJI_001/DJI_20261022100000_{index:04d}_D.MP4",
        size=size,
    )


def _photo(index: int):
    return _entry(
        f"DJI_001/DJI_20261022100000_{index:04d}_D.JPG",
        size=len(_CONTENT),
    )


def _coverage_facts(owned, action_id: int):
    repository = SegmentRepository()
    ref = repository.read_active_set(action_id, owned)
    assert ref is not None
    return ref, repository.read_set(ref, owned), repository.read_decision(ref, owned)


def _assert_reliable_control_complete(runtime, owned, action_id: int):
    (activity_id,) = owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id=?", (action_id,)
    ).fetchone()
    activity = row_facts(owned.connection, "device_activities", activity_id)
    action = row_facts(owned.connection, "actions", action_id)
    status, _overrun = _control_facts(runtime, action, activity)
    assert status is EvidenceState.CONFIRMED


def _scope_rows(owned, set_id: int):
    return owned.connection.execute(
        "SELECT m.identity,s.output_id,o.availability,o.device_file_id,m.file_id"
        " FROM segment_scope_members s"
        " JOIN segment_members m ON m.id=s.member_id"
        " LEFT JOIN outputs o ON o.id=s.output_id"
        " WHERE s.set_id=? ORDER BY s.ordinal",
        (set_id,),
    ).fetchall()


def _assert_available_prefix(owned, set_id: int, identities):
    rows = _scope_rows(owned, set_id)
    assert tuple(row[0] for row in rows) == identities
    available = int(enum_for("outputs.availability").AVAILABLE)
    assert all(
        output_id is not None
        and availability == available
        and output_file_id == member_file_id
        for _, output_id, availability, output_file_id, member_file_id in rows
    )


@pytest.mark.parametrize(
    (
        "entries",
        "probe_values",
        "status",
        "coverage_status",
        "confirmed",
        "checked_count",
        "scope_identities",
    ),
    [
        pytest.param(
            (_video(7), _photo(8), _video(8)),
            (Decimal("18"), Decimal("42")),
            "succeeded",
            "reached",
            Decimal("60"),
            2,
            ("/DCIM/DJI_001/DJI_20261022100000_0007_D.MP4",
             "/DCIM/DJI_001/DJI_20261022100000_0008_D.MP4"),
            id="photo-does-not-break-contiguous-video",
        ),
        pytest.param(
            (_video(7), _photo(8), _video(9)),
            (Decimal("18"), Decimal("27")),
            "failed",
            "unconfirmed",
            Decimal("18"),
            1,
            ("/DCIM/DJI_001/DJI_20261022100000_0007_D.MP4",),
            id="video-gap-after-photo-fails-with-readable-prefix",
        ),
    ],
)
async def test_video_sequence_and_photo_members_do_not_override_explicit_coverage(
    tmp_path,
    entries,
    probe_values,
    status,
    coverage_status,
    confirmed,
    checked_count,
    scope_identities,
):
    owned, runtime, action_id, handler, tools = await _overrun_recording(
        tmp_path, probe_values
    )
    try:
        await _publish_ordinary_result(
            runtime, owned, action_id, handler,
            sorted(entries, key=lambda entry: entry["identity"]),
        )

        ref, setrow, decision = _coverage_facts(owned, action_id)
        _assert_reliable_control_complete(runtime, owned, action_id)
        assert decision.status.value == status
        assert decision.coverage_status.value == coverage_status
        assert decision.confirmed == confirmed
        assert decision.checked_count == checked_count
        assert decision.default_scope.member_count == checked_count
        assert runtime.action(action_id)["status"] == (
            3 if status == "succeeded" else 4
        )
        assert len(tools.calls) == checked_count
        _assert_available_prefix(owned, setrow["id"], scope_identities)

        if coverage_status == "unconfirmed":
            assert decision.reason == "invalid_outputs"
            assert decision.failure_code == "capture_failed"
    finally:
        owned.connection.close()


async def test_zero_byte_required_video_fails_without_publishing_that_output(
    tmp_path,
):
    owned, runtime, action_id, handler, tools = await _overrun_recording(
        tmp_path, (Decimal("18"), Decimal("42"))
    )
    try:
        empty = _video(8, size=0)
        await _publish_ordinary_result(
            runtime, owned, action_id, handler, (_video(7), empty)
        )

        ref, setrow, decision = _coverage_facts(owned, action_id)
        _assert_reliable_control_complete(runtime, owned, action_id)
        assert decision.status.value == "failed"
        assert decision.coverage_status.value == "unconfirmed"
        assert decision.confirmed == Decimal("18")
        assert decision.checked_count == 1
        assert decision.default_scope.member_count == 1
        assert runtime.action(action_id)["status"] == 4
        assert len(tools.calls) == 1

        prefix_identity = "/DCIM/DJI_001/DJI_20261022100000_0007_D.MP4"
        _assert_available_prefix(owned, setrow["id"], (prefix_identity,))
        (empty_file_id,) = owned.connection.execute(
            "SELECT file_id FROM segment_members WHERE set_id=? AND identity=?",
            (setrow["id"], empty["identity"]),
        ).fetchone()
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM outputs WHERE source_action_id=? AND device_file_id=?",
            (action_id, empty_file_id),
        ).fetchone() == (0,)
        assert read_inspection_for_member(
            action_id,
            owned.connection.execute(
                "SELECT id FROM segment_members WHERE set_id=? AND identity=?",
                (setrow["id"], empty["identity"]),
            ).fetchone()[0],
            owned,
        ).inspection is None
    finally:
        owned.connection.close()


class _NoVideoStreamTools:
    def __init__(self, owned):
        self.owned = owned
        self.calls = []

    async def probe_segment(self, input_file, *, count_frames: bool):
        assert count_frames is False
        copy = self.owned.connection.execute(
            "SELECT committed_bytes,source_size,target_sha256 FROM file_copies"
            " WHERE target_file_id=?",
            (input_file.file_id,),
        ).fetchone()
        assert copy is not None
        assert copy[0] == copy[1] == len(_CONTENT)
        assert copy[2] is not None
        assert (input_file.root / input_file.relative_path).read_bytes() == _CONTENT
        self.calls.append(input_file.file_id)
        return FileTaskResult(
            FileTaskId("action6-explicit-no-video"),
            ran=True,
            value=MediaProbe(
                duration_s=None,
                error="no_video_stream: 容器没有视频流",
                streams=(),
            ),
        )

    async def probe(self, input_file):
        raise AssertionError("Action6 分段检查必须调用 probe_segment")

    async def repair(self, input_file, output_file, *, trim_s):
        raise AssertionError("覆盖检查不能启动媒体修复")


async def test_parsed_no_video_stream_fails_even_when_control_is_complete(
    tmp_path,
):
    owned, runtime, action_id, handler, _ = await _overrun_recording(
        tmp_path, (Decimal("18"), Decimal("42"))
    )
    try:
        tools = _NoVideoStreamTools(owned)
        runtime.media = replace(runtime.media, tools=tools)
        await _publish_ordinary_result(
            runtime, owned, action_id, handler, (_video(7), _video(8))
        )

        ref, setrow, decision = _coverage_facts(owned, action_id)
        _assert_reliable_control_complete(runtime, owned, action_id)
        assert decision.status.value == "failed"
        assert decision.failure_code == "capture_failed"
        assert runtime.action(action_id)["status"] == 4
        assert decision.default_scope.state.value == "empty"
        assert decision.default_scope.member_count == 0
        assert tools.calls and len(tools.calls) == 1

        (member_id,) = owned.connection.execute(
            "SELECT id FROM segment_members WHERE set_id=? AND identity=?",
            (setrow["id"], "/DCIM/DJI_001/DJI_20261022100000_0007_D.MP4"),
        ).fetchone()
        facts = read_inspection_for_member(action_id, member_id, owned)
        assert facts.inspection is not None
        assert facts.inspection.result_json["metric"]["reason"] == "no_video_stream"
        assert facts.inspection.result_json["probe"]["error"] == (
            "no_video_stream: 容器没有视频流"
        )
        assert facts.inspection.result_json["tool"] == {"ran": True, "error": None}
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM segment_scope_members WHERE set_id=?",
            (setrow["id"],),
        ).fetchone() == (0,)
    finally:
        owned.connection.close()

"""明确的视频成员错误优先于控制侧完成，但保留先前可取的检查前缀。"""

from decimal import Decimal
import json

import pytest

from camctl.contracts.enums import enum_for
from camctl.persistence.repositories.segment_inspections import (
    read_inspection_for_member,
)
from camctl.persistence.repositories.segments import SegmentRepository
from camctl.persistence.transaction import row_facts

from .test_action6_segmented_normal import _entry
from .test_input_copy import _CONTENT
from ..support.capture_guard_cases import known_failure_case

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


def _assert_reliable_control_complete(world):
    owned, action_id = world.owned, world.action_id
    activity = row_facts(owned.connection, "device_activities", 1)
    action = row_facts(owned.connection, "actions", action_id)
    assert action["execution_spec_json"]["target_duration_ms"] == 60000
    assert action["execution_spec_json"].get("startup_margin_ms", 0) == 0
    assert activity["control_elapsed_ns"] == 75_000_000_000
    rows = owned.connection.execute(
        "SELECT r.kind,t.status,t.effect_state,t.result_json FROM operation_runs r"
        " JOIN operation_attempts t ON t.run_id=r.id WHERE r.action_id=? AND r.kind IN (1,2) ORDER BY r.kind",
        (action_id,)).fetchall()
    assert len(rows) == 3
    first_stop = rows[1]
    assert first_stop[0] == 2
    assert first_stop[1] == int(enum_for("operation_attempts.status").FAILED)
    assert first_stop[2] == int(enum_for("operation_attempts.effect_state").UNKNOWN)
    for (_kind, status, effect, result), observation in zip((rows[0], rows[2]), ("start_confirmed", "stop_confirmed")):
        assert status == int(enum_for("operation_attempts.status").SUCCEEDED)
        assert effect == int(enum_for("operation_attempts.effect_state").CONFIRMED)
        actual = json.loads(result)
        assert actual["settlement"]["basis"] == "observed"
        assert any(item["type"] == observation and item["data"]["activity_id"] == str(activity["id"])
            for item in actual["observations"])
    world.control.control.assert_awaited_once()
    assert world.stopper.stop.await_count == 2


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
    tmp_path, capture_resources, monkeypatch,
    entries,
    probe_values,
    status,
    coverage_status,
    confirmed,
    checked_count,
    scope_identities,
):
    world = await known_failure_case(tmp_path, capture_resources, monkeypatch, probe_values,
        sorted(entries, key=lambda entry: entry["identity"]))
    owned, action_id, tools = world.owned, world.action_id, world.tools
    try:
        ref, setrow, decision = _coverage_facts(owned, action_id)
        _assert_reliable_control_complete(world)
        assert decision.status.value == status
        assert decision.coverage_status.value == coverage_status
        assert decision.confirmed == confirmed
        assert decision.checked_count == checked_count
        assert decision.default_scope.member_count == checked_count
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone()[0] == (
            3 if status == "succeeded" else 4
        )
        assert len(tools) == checked_count
        _assert_available_prefix(owned, setrow["id"], scope_identities)

        if coverage_status == "unconfirmed":
            assert decision.reason == "invalid_outputs"
            assert decision.failure_code == "capture_failed"
    finally:
        owned.connection.close()


async def test_zero_byte_required_video_fails_without_publishing_that_output(
    tmp_path, capture_resources, monkeypatch,
):
    empty = _video(8, size=0)
    world = await known_failure_case(tmp_path, capture_resources, monkeypatch,
        (Decimal("18"), Decimal("42")), (_video(7), empty))
    owned, action_id, tools = world.owned, world.action_id, world.tools
    try:
        ref, setrow, decision = _coverage_facts(owned, action_id)
        _assert_reliable_control_complete(world)
        assert decision.status.value == "failed"
        assert decision.coverage_status.value == "unconfirmed"
        assert decision.confirmed == Decimal("18")
        assert decision.checked_count == 1
        assert decision.default_scope.member_count == 1
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone()[0] == 4
        assert len(tools) == 1

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


async def test_parsed_no_video_stream_fails_even_when_control_is_complete(
    tmp_path, capture_resources, monkeypatch,
):
    world = await known_failure_case(tmp_path, capture_resources, monkeypatch,
        (Decimal("18"), Decimal("42")), (_video(7), _video(8)), no_video=True)
    owned, action_id, tools = world.owned, world.action_id, world.tools
    try:
        ref, setrow, decision = _coverage_facts(owned, action_id)
        _assert_reliable_control_complete(world)
        assert decision.status.value == "failed"
        assert decision.failure_code == "capture_failed"
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone()[0] == 4
        assert decision.default_scope.state.value == "empty"
        assert decision.default_scope.member_count == 0
        assert tools and len(tools) == 1

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

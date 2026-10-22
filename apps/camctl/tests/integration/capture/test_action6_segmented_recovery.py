"""Action6 异常收场沿真实处理器、检查副本与持久化决策推进。"""

from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from camctl.capture.baseline_models import BaselineFixSave
from camctl.capture.handlers import _stop_call, capture_handler
from camctl.capture.media import MediaPolicy
from camctl.capture.media_flow import DriverReadSessions, MediaFlow
from camctl.capture.models import HostTimerStopSave
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.contracts.values import new_operation_key
from camctl.devices.ports import DeviceCallResult, StopDriver
from camctl.host_files.models import BoundDirectories
from camctl.host_files.media import MediaProbe, MediaStream
from camctl.host_files.tasks import FileTaskId, FileTaskResult
from camctl.outputs.copy import SourceDigest
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.segments import SegmentRepository
from camctl.operations.attempts import AttemptConfig
from camctl.persistence.repositories.segment_selections import (
    FixSegmentSelection,
    fix_segment_selection,
)
from camctl.reporting.generation import generate_report_file
from ..reporting.test_generation import _freeze, _spec

from .result_consumer_fixtures import consumer_world, returned
from .test_action6_segmented_normal import (
    Action6NativeCatalog,
    Action6RecordCatalog,
    _entry,
)
from .test_capture_contract import _NOW
from .test_input_copy import _CONTENT
from .test_segment_inspections import _ReadDriver
from .test_record_file_completion_wait import (
    _completed_result,
    _result_port,
    _stop_context,
)
from ..outputs.test_segment_default_scope import _obtain

pytestmark = pytest.mark.asyncio


class _Digest:
    def __init__(self) -> None:
        self.digest = hashlib.sha256(_CONTENT).hexdigest()

    async def read_digest(self):
        return SourceDigest(digest=self.digest, error=None)


class _ProbeTools:
    def __init__(self, owned, values) -> None:
        self.owned = owned
        self.values = tuple(values)
        self.calls: list[tuple[int, bool]] = []

    async def probe_segment(self, input, *, count_frames: bool):
        rows = self.owned.connection.execute(
            "SELECT committed_bytes,source_size,target_sha256 FROM file_copies"
            " WHERE target_file_id=?",
            (input.file_id,),
        ).fetchall()
        assert len(rows) == 1
        committed, source_size, target_digest = rows[0]
        assert committed == source_size == len(_CONTENT)
        assert target_digest is not None
        assert (input.root / input.relative_path).read_bytes() == _CONTENT

        index = len(self.calls)
        self.calls.append((input.file_id, count_frames))
        value = self.values[index]
        if isinstance(value, BaseException):
            raise value
        stream = MediaStream(
            index=0,
            codec_type="video",
            codec_name="h264",
            width=1920,
            height=1080,
            duration_s=(None if count_frames else Decimal(value)),
            attached_pic=False,
            frame_count=(value if count_frames else None),
            fps=Decimal("30"),
        )
        return FileTaskResult(
            FileTaskId(f"action6-segment-{index + 1}"),
            ran=True,
            value=MediaProbe(duration_s=None, error=None, streams=(stream,)),
        )

    async def probe(self, input):
        raise AssertionError("Action6 分段检查必须使用 probe_segment")

    async def repair(self, input, output, *, trim_s):
        raise AssertionError("Action6 覆盖检查不能启动修复")


def _attach_media(runtime, owned, tmp_path: Path, values) -> _ProbeTools:
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    tools = _ProbeTools(owned, values)
    runtime.media = MediaFlow(
        owned=owned,
        roots=BoundDirectories(staging=staging),
        sessions=DriverReadSessions(owned, _ReadDriver(_CONTENT), ticket=None),
        tools=tools,
        policy=MediaPolicy(repair_margin_s=Decimal("2")),
        occurred_at=runtime.wall_us,
        digest_supported=True,
        digest=_Digest(),
    )
    return tools


async def _overrun_recording(
    tmp_path, values, *, catalog=None, fix_baseline=True,
):
    catalog = Action6RecordCatalog() if catalog is None else catalog
    catalog.duration_s = Decimal("60")
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, "record", catalog=catalog, start=False
    )
    (activity_id,) = owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id=?", (action_id,)
    ).fetchone()
    if fix_baseline:
        fixed = runtime.capture.fix_baseline(
            BaselineFixSave(activity_id, 0, 0, _NOW), new_operation_key(), owned
        )
        assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    await capture_handler(handler)(action_id, runtime)

    original_finish = runtime.finish

    def finish_with_elapsed(ticket, actual, **kwargs):
        if ticket.operation == "stop":
            kwargs["host_timer_stop"] = HostTimerStopSave(75_000_000_000)
        return original_finish(ticket, actual, **kwargs)

    runtime.finish = finish_with_elapsed
    runtime.wall_us = lambda: _NOW + 75_000_000
    runtime.monotonic_ns = lambda: 80_000_000_000
    stopper = create_autospec(StopDriver, instance=True)
    stopper.stop.return_value = DeviceCallResult.from_outcome(
        returned("stop", "stop_confirmed", activity_id)
    )
    runtime.stopper = stopper
    stopped = await _stop_call(runtime, runtime.action(action_id), "stop_recording")
    assert stopped.phase == "confirmed"
    runtime.wall_us = lambda: _NOW + 80_000_000
    runtime.monotonic_ns = lambda: 85_000_000_000
    tools = _attach_media(runtime, owned, tmp_path, values)
    return owned, runtime, action_id, handler, tools


def _entries(count: int, *, photo: bool = False):
    entries = [
        _entry(
            f"DJI_001/DJI_20261022100000_{index:04d}_D.MP4",
            size=len(_CONTENT),
        )
        for index in range(7, 7 + count)
    ]
    if photo:
        entries.append(
            _entry(
                "DJI_001/DJI_20261022100000_0007_D.JPG",
                size=len(_CONTENT),
            )
        )
    return sorted(entries, key=lambda entry: entry["identity"])


async def _publish_ordinary_result(runtime, owned, action_id, handler, entries):
    context = _stop_context(owned, action_id)

    async def read(request, batch):
        return _completed_result(
            request.ticket, context, entries=entries, finalized=True
        )

    driver = _result_port(runtime, read)
    await capture_handler(handler)(action_id, runtime)
    driver.list_results.assert_awaited_once()


async def _assert_saved_decision(owned, action_id, *, coverage, confirmed, count, reason=None):
    repository = SegmentRepository()
    ref = repository.read_active_set(action_id, owned)
    assert ref is not None
    decision = repository.read_decision(ref, owned)
    assert decision.status.value == "succeeded"
    assert decision.coverage_status.value == coverage
    assert decision.confirmed == confirmed
    assert decision.checked_count == count
    assert decision.default_scope.state.value == "nonempty"
    assert decision.default_scope.member_count == count
    assert decision.reason == reason


def _reported_coverage(tmp_path: Path, owned, action_id: int):
    report = _freeze(owned)
    generated = generate_report_file(
        _spec(tmp_path, report, "staging/action6-coverage.json")
    )
    document = json.loads(generated.path.read_bytes())
    from camctl.contracts.schemas import validate_document

    validate_document("protocol/status-report.schema.json", document)
    action = next(
        action
        for plan in document["plans"]
        for action in plan["actions"]
        if action["action_instance_id"] == str(action_id)
    )
    return action["result"]["coverage"]


class _Action6TaskScopeCatalog(Action6RecordCatalog):
    def parameter_definition(self, *args):
        definition = super().parameter_definition(*args)
        if args[1] != "camera_record":
            return definition
        factory = definition.task_factory
        return replace(
            definition,
            task_factory=lambda params: replace(
                factory(params), ownership_mode=1,
                output_scope={"directories": ["/DCIM"]},
            ),
        )


async def test_action6_overrun_measures_only_the_prefix_needed_to_reach_target(tmp_path):
    owned, runtime, action_id, handler, tools = await _overrun_recording(
        tmp_path, (Decimal("18"), Decimal("27"), Decimal("22"), Decimal("40"))
    )
    try:
        await _publish_ordinary_result(
            runtime, owned, action_id, handler, _entries(4)
        )

        await _assert_saved_decision(
            owned,
            action_id,
            coverage="reached",
            confirmed=Decimal("67"),
            count=3,
        )
        report = _reported_coverage(tmp_path, owned, action_id)
        assert report["status"] == "reached"
        assert report["metric"] == "duration_s" and report["target"] == Decimal("60")
        assert report["confirmed"] == Decimal("67")
        assert len(report["checked_output_ids"]) == 3
        assert report["default_scope"]["state"] == "nonempty"
        assert report["default_scope"]["member_count"] == 3
        assert len(tools.calls) == 3
        assert all(not count_frames for _, count_frames in tools.calls)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM segment_inspections"
        ).fetchone() == (3,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM segment_scope_members"
        ).fetchone() == (3,)
        assert runtime.action(action_id)["status"] == 3
    finally:
        owned.connection.close()


async def test_action6_unknown_third_probe_keeps_reliable_control_success_and_two_file_prefix(
    tmp_path,
):
    owned, runtime, action_id, handler, tools = await _overrun_recording(
        tmp_path,
        (Decimal("18"), Decimal("27"), RuntimeError("ffprobe unavailable"), Decimal("40")),
    )
    try:
        await _publish_ordinary_result(
            runtime, owned, action_id, handler, _entries(4)
        )

        await _assert_saved_decision(
            owned,
            action_id,
            coverage="unconfirmed",
            confirmed=Decimal("45"),
            count=2,
            reason="probe_failed",
        )
        report = _reported_coverage(tmp_path, owned, action_id)
        assert report["status"] == "unconfirmed"
        assert report["confirmed"] == Decimal("45")
        assert report["reason"] == {"code": "probe_failed"}
        assert len(report["checked_output_ids"]) == 2
        assert report["default_scope"]["state"] == "nonempty"
        assert report["default_scope"]["member_count"] == 2
        assert len(tools.calls) == 3
        assert all(not count_frames for _, count_frames in tools.calls)
        assert owned.connection.execute(
            "SELECT state, json_extract(result_json,'$.failure.code')"
            " FROM segment_inspections ORDER BY member_id"
        ).fetchall() == [(2, None), (2, None), (3, "probe_failed")]
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM segment_scope_members"
        ).fetchone() == (2,)
        assert runtime.action(action_id)["status"] == 3
    finally:
        owned.connection.close()


async def test_action6_complete_short_sequence_reports_confirmed_short(tmp_path):
    owned, runtime, action_id, handler, tools = await _overrun_recording(
        tmp_path, (Decimal("18"), Decimal("20"), Decimal("12"), Decimal("40"))
    )
    try:
        await _publish_ordinary_result(
            runtime, owned, action_id, handler, _entries(3)
        )
        ref = SegmentRepository().read_active_set(action_id, owned)
        assert ref is not None
        decision = SegmentRepository().read_decision(ref, owned)
        assert decision.status.value == "failed"
        assert decision.coverage_status.value == "confirmed_short"
        assert decision.confirmed == Decimal("50")
        assert decision.checked_count == 3
        assert decision.default_scope.state.value == "nonempty"
        assert decision.default_scope.member_count == 3
        report = _reported_coverage(tmp_path, owned, action_id)
        assert report["status"] == "confirmed_short"
        assert report["metric"] == "duration_s" and report["target"] == Decimal("60")
        assert report["confirmed"] == Decimal("50")
        assert len(report["checked_output_ids"]) == 3
        assert report["default_scope"]["member_count"] == 3
        assert len(tools.calls) == 3
        assert runtime.action(action_id)["status"] == 4
    finally:
        owned.connection.close()


async def test_action6_unknown_start_saves_unknown_scope_header(tmp_path):
    owned, runtime, action_id, handler, tools = await _overrun_recording(
        tmp_path,
        (Decimal("18"), Decimal("27"), Decimal("22"), Decimal("40")),
        catalog=_Action6TaskScopeCatalog(),
        fix_baseline=False,
    )
    try:
        await _publish_ordinary_result(
            runtime, owned, action_id, handler, _entries(4)
        )
        ref = SegmentRepository().read_active_set(action_id, owned)
        assert ref is not None
        decision = SegmentRepository().read_decision(ref, owned)
        assert decision.status.value == "succeeded"
        assert decision.coverage_status.value == "unconfirmed"
        assert decision.confirmed == 0 and decision.checked_count == 0
        assert decision.reason == "start_unknown"
        assert decision.default_scope.state.value == "unknown"
        assert decision.default_scope.first_cursor is None
        assert decision.default_scope.last_cursor is None
        assert decision.default_scope.member_count is None
        report = _reported_coverage(tmp_path, owned, action_id)
        assert report["status"] == "unconfirmed"
        assert report["confirmed"] == 0
        assert report["reason"] == {"code": "start_unknown"}
        assert report["checked_output_ids"] == []
        assert report["default_scope"] == {
            "state": "unknown", "member_count": None, "output_ids": []
        }
        assert tools.calls == []
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM segment_scope_members"
        ).fetchone() == (0,)
    finally:
        owned.connection.close()


async def test_action6_first_probe_unknown_keeps_known_start_as_empty_scope(tmp_path):
    owned, runtime, action_id, handler, tools = await _overrun_recording(
        tmp_path,
        (RuntimeError("ffprobe unavailable"), Decimal("27"), Decimal("22")),
    )
    try:
        await _publish_ordinary_result(
            runtime, owned, action_id, handler, _entries(3)
        )
        ref = SegmentRepository().read_active_set(action_id, owned)
        assert ref is not None
        decision = SegmentRepository().read_decision(ref, owned)
        assert decision.status.value == "succeeded"
        assert decision.coverage_status.value == "unconfirmed"
        assert decision.confirmed == 0 and decision.checked_count == 0
        assert decision.reason == "probe_failed"
        assert decision.default_scope.state.value == "empty"
        assert decision.default_scope.first_cursor is None
        assert decision.default_scope.last_cursor is None
        assert decision.default_scope.member_count == 0
        report = _reported_coverage(tmp_path, owned, action_id)
        assert report["status"] == "unconfirmed"
        assert report["confirmed"] == 0
        assert report["reason"] == {"code": "probe_failed"}
        assert report["checked_output_ids"] == []
        assert report["default_scope"] == {
            "state": "empty", "member_count": 0, "output_ids": []
        }
        assert len(tools.calls) == 1
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM segment_scope_members"
        ).fetchone() == (0,)
    finally:
        owned.connection.close()


class _Action6Native151Catalog(Action6NativeCatalog):
    def parameter_definition(self, *args):
        definition = super().parameter_definition(*args)
        if args[1] != "camera_timelapse":
            return definition
        factory = definition.task_factory
        return replace(
            definition,
            defaults={"duration_s": Decimal("300"), "interval_s": Decimal("2")},
            task_factory=lambda params: replace(
                factory(params), target_duration_s=Decimal("300")
            ),
        )


async def test_action6_native_recovery_counts_explicit_frames_to_151(tmp_path, monkeypatch):
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path,
        "timelapse",
        catalog=_Action6Native151Catalog(0),
        start=False,
    )
    from camctl.capture.timelapse import CaptureWaitConfig

    try:
        (activity_id,) = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (action_id,)
        ).fetchone()
        fixed = runtime.capture.fix_baseline(
            BaselineFixSave(activity_id, 0, 0, _NOW), new_operation_key(), owned
        )
        assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
        from camctl.capture.handlers import _control_call

        await _control_call(
            runtime,
            runtime.action(action_id),
            "start_timelapse",
            "timelapse_sent",
            activity_facts={"sent_at": _NOW},
        )
        tools = _attach_media(runtime, owned, tmp_path, (60, 60, 45, 20))
        wall, mono = [_NOW], [5_000_000_000]
        runtime.wall_us = lambda: wall[0]
        runtime.monotonic_ns = lambda: mono[0]
        runtime.wait_config = lambda action: CaptureWaitConfig(300_000, 200, 0, 0)
        await capture_handler(handler)(action_id, runtime)

        async def read(request, batch):
            from .test_result_page_history import _actual

            return DeviceCallResult.from_outcome(
                _actual(request.ticket, entries=_entries(4, photo=True), finalized=True)
            )

        driver = _result_port(runtime, read)
        wall[0] = _NOW + 300_200_000
        mono[0] += 300_200_000_000

        # The ordinary native branch intentionally trusts a confirmed natural
        # completion without media inspection. Exercise the recovery branch
        # with the very same production handler/listing and saved frozen set.
        from camctl.capture import action6_completion
        from camctl.capture.action6_recovery import advance_action6_recovery

        async def inspect_saved_native_result(runtime, requested_action_id, *, canceled=False):
            action = runtime.action(requested_action_id)
            ref = await action6_completion.prepare_action6_segment_set(
                runtime, action, None, canceled=canceled
            )
            assert ref is not None
            await advance_action6_recovery(runtime, action)

        monkeypatch.setattr(
            action6_completion,
            "advance_action6_native_result",
            inspect_saved_native_result,
        )
        await capture_handler(handler)(action_id, runtime)

        assert driver.list_results.await_count == 1
        assert owned.connection.execute(
            "SELECT target_duration_ms FROM segment_sets"
        ).fetchone() == (300_000,)
        await _assert_saved_decision(
            owned, action_id, coverage="reached", confirmed=165, count=3
        )
        assert len(tools.calls) == 3
        assert all(count_frames for _, count_frames in tools.calls)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM segment_scope_members"
        ).fetchone() == (3,)
        assert runtime.action(action_id)["status"] == 3
        driver.list_results.assert_awaited_once()
    finally:
        owned.connection.close()


async def _native_action6_recovery_with_unknown_video_scope(
    tmp_path, monkeypatch, *, start_unknown: bool, probe_values
):
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path,
        "timelapse",
        catalog=_Action6Native151Catalog(0),
        start=False,
    )
    (activity_id,) = owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id=?", (action_id,)
    ).fetchone()
    fixed = runtime.capture.fix_baseline(
        BaselineFixSave(activity_id, 0, 0, _NOW), new_operation_key(), owned
    )
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    from camctl.capture.handlers import _control_call

    await _control_call(
        runtime,
        runtime.action(action_id),
        "start_timelapse",
        "timelapse_sent",
        activity_facts={"sent_at": _NOW},
    )
    tools = _attach_media(runtime, owned, tmp_path, probe_values)
    wall, mono = [_NOW], [5_000_000_000]
    runtime.wall_us = lambda: wall[0]
    runtime.monotonic_ns = lambda: mono[0]
    runtime.wait_config = lambda action: CaptureWaitConfig(300_000, 200, 0, 0)
    runtime.check_config = AttemptConfig(1, Decimal("1"), Decimal(0))
    await capture_handler(handler)(action_id, runtime)

    async def read(request, batch):
        from .test_result_page_history import _actual

        entries = _entries(3, photo=True)
        if start_unknown:
            entries.append(_entry("DJI_001/unkeyed_D.MP4"))
        entries.sort(key=lambda entry: entry["identity"])
        return DeviceCallResult.from_outcome(
            _actual(request.ticket, entries=entries, finalized=True)
        )

    driver = _result_port(runtime, read)
    wall[0] = _NOW + 300_200_000
    mono[0] += 300_200_000_000
    from camctl.capture import action6_completion
    from camctl.capture.action6_recovery import advance_action6_recovery

    async def inspect_saved_native_result(
        runtime, requested_action_id, *, canceled=False
    ):
        action = runtime.action(requested_action_id)
        ref = await action6_completion.prepare_action6_segment_set(
            runtime, action, None, canceled=canceled
        )
        assert ref is not None
        await advance_action6_recovery(runtime, action)

    monkeypatch.setattr(
        action6_completion,
        "advance_action6_native_result",
        inspect_saved_native_result,
    )
    await capture_handler(handler)(action_id, runtime)
    assert driver.list_results.await_count == 1
    assert runtime.action(action_id)["status"] == 3
    return owned, runtime, action_id, tools


async def _select_native_outputs(
    owned, runtime, source_action_id, photo_id, mode, request_id
):
    _, selection_id = _obtain(
        owned, runtime, source_action_id, mode, request_id
    )
    saved = fix_segment_selection(
        FixSegmentSelection(selection_id, runtime.wall_us()),
        new_operation_key(),
        owned,
    )
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    rows = owned.connection.execute(
        "SELECT i.output_id,o.kind,json_extract(d.identity_key,'$[2]')"
        " FROM obtain_items i"
        " JOIN outputs o ON o.id=i.output_id"
        " JOIN device_files d ON d.id=o.device_file_id"
        " WHERE i.selection_id=? ORDER BY i.id",
        (selection_id,),
    ).fetchall()
    assert len(rows) == 1
    output_id, kind, identity = rows[0]
    assert output_id == photo_id
    assert kind == 1
    assert identity.endswith(".JPG")
    return output_id


async def _select_explicit_native_video(
    owned, runtime, source_action_id, video_id, request_id
):
    _, selection_id = _obtain(
        owned, runtime, source_action_id, [video_id], request_id
    )
    saved = fix_segment_selection(
        FixSegmentSelection(selection_id, runtime.wall_us()),
        new_operation_key(),
        owned,
    )
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    rows = owned.connection.execute(
        "SELECT i.output_id,o.kind FROM obtain_items i"
        " JOIN outputs o ON o.id=i.output_id WHERE i.selection_id=? ORDER BY i.id",
        (selection_id,),
    ).fetchall()
    assert rows == [(video_id, 1)]


@pytest.mark.parametrize(
    ("start_unknown", "probe_values", "scope_state", "member_count", "reason"),
    [
        (False, (RuntimeError("ffprobe unavailable"),), "empty", 0, "probe_failed"),
        (True, (), "unknown", None, "start_unknown"),
    ],
    ids=("first-probe-unknown-empty", "unknown-start"),
)
async def test_action6_native_empty_or_unknown_video_scope_keeps_photo_selectable(
    tmp_path,
    monkeypatch,
    start_unknown,
    probe_values,
    scope_state,
    member_count,
    reason,
):
    owned, runtime, action_id, tools = (
        await _native_action6_recovery_with_unknown_video_scope(
            tmp_path,
            monkeypatch,
            start_unknown=start_unknown,
            probe_values=probe_values,
        )
    )
    try:
        outputs = owned.connection.execute(
            "SELECT o.id,o.kind,json_extract(d.identity_key,'$[2]')"
            " FROM outputs o JOIN device_files d ON d.id=o.device_file_id"
            " WHERE o.source_action_id=? ORDER BY o.id",
            (action_id,),
        ).fetchall()
        photo_ids = [
            output_id
            for output_id, kind, identity in outputs
            if kind == 1 and identity.endswith(".JPG")
        ]
        video_ids = [
            output_id
            for output_id, kind, identity in outputs
            if kind == 1 and identity.endswith(".MP4")
        ]
        assert len(photo_ids) == 1
        assert len(video_ids) == (4 if start_unknown else 3)

        ref = SegmentRepository().read_active_set(action_id, owned)
        assert ref is not None
        decision = SegmentRepository().read_decision(ref, owned)
        assert decision.status.value == "succeeded"
        assert decision.coverage_status.value == "unconfirmed"
        assert decision.checked_count == 0
        assert decision.default_scope.state.value == scope_state
        assert decision.default_scope.member_count == member_count
        assert decision.reason == reason

        coverage = _reported_coverage(tmp_path, owned, action_id)
        assert coverage["metric"] == "frames" and coverage["target"] == 151
        assert coverage["status"] == "unconfirmed"
        assert coverage["confirmed"] == 0
        assert coverage["reason"] == {"code": reason}
        assert coverage["checked_output_ids"] == []
        assert coverage["default_scope"] == {
            "state": scope_state,
            "member_count": member_count,
            "output_ids": [],
        }
        coverage_ids = (
            coverage["checked_output_ids"]
            + coverage["default_scope"]["output_ids"]
        )
        assert set(coverage_ids).issubset(set(map(str, video_ids)))
        assert str(photo_ids[0]) not in coverage_ids

        for mode, request_id in (("default", 101), ("preview", 102)):
            await _select_native_outputs(
                owned, runtime, action_id, photo_ids[0], mode, request_id
            )
        explicit_video = next(
            output_id
            for output_id, kind, identity in outputs
            if kind == 1 and identity.endswith("_0009_D.MP4")
        )
        await _select_explicit_native_video(
            owned, runtime, action_id, explicit_video, 103
        )
        assert len(tools.calls) == (0 if start_unknown else 1)
        assert all(count_frames for _, count_frames in tools.calls)
    finally:
        owned.connection.close()

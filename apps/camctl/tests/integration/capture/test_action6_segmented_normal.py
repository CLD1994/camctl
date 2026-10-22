"""Action6 分段正常完成只发布原始视频范围和全部独立原片。"""

from dataclasses import replace
from decimal import Decimal
from unittest.mock import create_autospec

import pytest

from camctl.capture.baseline_models import BaselineFixSave
from camctl.capture.handlers import _stop_call, capture_handler
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.contracts.values import new_operation_key
from camctl.devices.ports import DeviceCallResult, StopDriver
from camctl.devices.tasks import CaptureResultPolicy, VideoResultMode
from camctl.operations.attempts import AttemptConfig
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.segments import SegmentRepository

from .result_consumer_fixtures import consumer_world, returned
from .test_capture_contract import _NOW
from .test_native_segment_completion import NativeSegmentCatalog
from .test_record_file_completion_wait import (
    _completed_result,
    _result_port,
    _stop_context,
)
from .test_record_segment_scope_recovery import SegmentWaitCatalog
from .test_result_page_history import _actual

pytestmark = pytest.mark.asyncio


def _rules(*, photo=False):
    values = [
        {
            "kind": "video",
            "format_id": "mp4",
            "min_count": 1,
            "exact_count": None,
            "min_size_bytes": 1,
            "require_pairing": False,
        }
    ]
    if photo:
        values.append(
            {
                "kind": "photo",
                "format_id": "jpeg",
                "min_count": 1,
                "exact_count": None,
                "min_size_bytes": 1,
                "require_pairing": False,
            }
        )
    return tuple(values)


class Action6RecordCatalog(SegmentWaitCatalog):
    def parameter_definition(self, *args):
        definition = super().parameter_definition(*args)
        if args[1] != "camera_record":
            return definition
        factory = definition.task_factory
        return replace(
            definition,
            task_factory=lambda params: replace(
                factory(params),
                video_result_mode=VideoResultMode.SEGMENTED,
                result_policy=CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1,
                product_rules=_rules(),
            ),
        )


class Action6NativeCatalog(NativeSegmentCatalog):
    def parameter_definition(self, *args):
        definition = super().parameter_definition(*args)
        if args[1] != "camera_timelapse":
            return definition
        schema = dict(definition.schema)
        schema["properties"] = {
            **schema.get("properties", {}),
            "duration_s": {"type": "number", "exclusiveMinimum": 0},
            "interval_s": {"type": "number", "exclusiveMinimum": 0},
        }
        factory = definition.task_factory
        return replace(
            definition,
            schema=schema,
            defaults={"duration_s": Decimal("1"), "interval_s": Decimal("0.5")},
            task_factory=lambda params: replace(
                factory(params),
                video_result_mode=VideoResultMode.SEGMENTED,
                result_policy=CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1,
                product_rules=_rules(photo=True),
            ),
        )


def _entry(relative, *, size=32):
    identity = "/DCIM/" + relative
    suffix = relative.rpartition(".")[2].lower()
    paired_identity = (
        identity[: -len(".LRF")] + ".MP4"
        if suffix == "lrf"
        else None
    )
    return {
        "identity": identity,
        "locator": {"path": identity},
        "complete": True,
        "size_bytes": size,
        "kind": "image" if suffix in ("jpg", "jpeg", "dng") else "video",
        "format_id": "jpeg" if suffix in ("jpg", "jpeg") else suffix,
        "paired_identity": paired_identity,
    }


async def _stopped_action6_record(tmp_path):
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, "record", catalog=Action6RecordCatalog(), start=False
    )
    (activity,) = owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id=?", (action_id,)
    ).fetchone()
    fixed = runtime.capture.fix_baseline(
        BaselineFixSave(activity, 0, 0, _NOW), new_operation_key(), owned
    )
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    await capture_handler(handler)(action_id, runtime)
    runtime.wall_us = lambda: _NOW + 12_000_000
    runtime.monotonic_ns = lambda: 17_000_000_000
    stopper = create_autospec(StopDriver, instance=True)
    stopper.stop.return_value = DeviceCallResult.from_outcome(
        returned("stop", "stop_confirmed", activity)
    )
    runtime.stopper = stopper
    stopped = await _stop_call(runtime, runtime.action(action_id), "stop_recording")
    assert stopped.phase == "confirmed"
    runtime.wall_us = lambda: _NOW + 17_000_000
    runtime.monotonic_ns = lambda: 22_000_000_000
    return owned, runtime, action_id, handler, stopper


async def test_action6_ordinary_normal_publishes_all_mp4_lrf_and_photo_without_media(
    tmp_path,
):
    owned, runtime, action_id, handler, stopper = await _stopped_action6_record(tmp_path)
    try:
        context = _stop_context(owned, action_id)
        entries = [
            _entry(f"DJI_001/DJI_20261022100000_{index:04d}_D.MP4")
            for index in (7, 8, 9)
        ]
        entries.extend(
            _entry(f"DJI_001/DJI_20261022100000_{index:04d}_D.LRF")
            for index in (7, 8)
        )
        entries.append(_entry("DJI_001/DJI_20261022100000_0007_D.JPG"))
        entries.sort(key=lambda entry: entry["identity"])

        async def read(request, batch):
            assert request.params["completion_context"] == context
            return _completed_result(
                request.ticket, context, entries=entries, finalized=True
            )

        driver = _result_port(runtime, read)
        await capture_handler(handler)(action_id, runtime)

        assert runtime.action(action_id)["status"] == 3
        assert owned.connection.execute(
            "SELECT kind,COUNT(*) FROM outputs GROUP BY kind ORDER BY kind"
        ).fetchall() == [(1, 4), (3, 2)]
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM segment_scope_members WHERE output_id IS NOT NULL"
        ).fetchone() == (3,)
        assert owned.connection.execute("SELECT COUNT(*) FROM file_copies").fetchone() == (0,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM intermediate_files"
        ).fetchone() == (0,)

        repository = SegmentRepository()
        ref = repository.read_active_set(action_id, owned)
        decision = repository.read_decision(ref, owned)
        assert decision.status.value == "succeeded"
        assert decision.coverage_status.value == "not_performed"
        assert decision.confirmed is None and decision.checked_count == 0
        assert decision.metric.value == "duration_s" and decision.target == Decimal("12")
        assert decision.default_scope.state.value == "nonempty"
        assert decision.default_scope.member_count == 3
        assert owned.connection.execute(
            "SELECT json_extract(assessment_json,'$.decision_type') FROM segment_sets"
            " WHERE published_event_id IS NOT NULL"
        ).fetchone() == ("video_coverage_v1",)
        stopper.stop.assert_awaited_once()
        driver.list_results.assert_awaited_once()
    finally:
        owned.connection.close()


async def test_action6_native_normal_publishes_all_video_and_required_photo_separately(
    tmp_path,
):
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, "timelapse", catalog=Action6NativeCatalog(0), start=False
    )
    wall, mono = [_NOW], [5_000_000_000]
    runtime.wall_us = lambda: wall[0]
    runtime.monotonic_ns = lambda: mono[0]
    runtime.wait_config = lambda action: CaptureWaitConfig(1000, 200, 0, 0)
    runtime.check_config = AttemptConfig(1, Decimal("1"), Decimal(0))
    try:
        await capture_handler(handler)(action_id, runtime)
        entries = [
            _entry(f"DJI_001/DJI_20261022100000_{index:04d}_D.MP4")
            for index in (7, 8, 9)
        ]
        entries.extend(
            _entry(f"DJI_001/DJI_20261022100000_{index:04d}_D.LRF")
            for index in (7, 8)
        )
        entries.append(_entry("DJI_001/DJI_20261022100000_0007_D.JPG"))
        entries.sort(key=lambda entry: entry["identity"])

        async def read(request, batch):
            assert request.params["completion_context"]["kind"] == "timelapse_natural"
            return DeviceCallResult.from_outcome(
                _actual(request.ticket, entries=entries, finalized=True)
            )

        driver = _result_port(runtime, read)
        wall[0] = _NOW + 1_200_000
        mono[0] += 1_200_000_000
        await capture_handler(handler)(action_id, runtime)

        assert runtime.action(action_id)["status"] == 3
        assert owned.connection.execute(
            "SELECT completion_basis,occupancy_state,result_set_state FROM device_activities"
            " WHERE action_id=?",
            (action_id,),
        ).fetchone() == (3, 2, 3)
        assert owned.connection.execute(
            "SELECT kind,COUNT(*) FROM outputs GROUP BY kind ORDER BY kind"
        ).fetchall() == [(1, 4), (3, 2)]
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM segment_scope_members WHERE output_id IS NOT NULL"
        ).fetchone() == (3,)
        assert owned.connection.execute("SELECT COUNT(*) FROM file_copies").fetchone() == (0,)
        repository = SegmentRepository()
        ref = repository.read_active_set(action_id, owned)
        decision = repository.read_decision(ref, owned)
        assert decision.status.value == "succeeded"
        assert decision.coverage_status.value == "not_performed"
        assert decision.confirmed is None and decision.checked_count == 0
        assert decision.metric.value == "frames" and decision.target == 3
        assert decision.default_scope.member_count == 3
        driver.list_results.assert_awaited_once()
    finally:
        owned.connection.close()

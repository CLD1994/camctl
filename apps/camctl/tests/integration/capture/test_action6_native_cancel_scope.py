"""Action6 native cancel keeps every qualified video in the default scope."""

from unittest.mock import create_autospec

import pytest

from camctl.capture.baseline_models import BaselineFixSave
from camctl.capture.handlers import capture_handler, _control_call
from camctl.contracts.values import new_operation_key
from camctl.devices.ports import DeviceCallResult, StopDriver
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.segment_selections import (
    FixSegmentSelection,
    fix_segment_selection,
)
from camctl.persistence.repositories.segments import SegmentRepository

from .result_consumer_fixtures import consumer_world, returned
from .test_action6_segmented_normal import Action6NativeCatalog, _entry
from .test_cancel_timelapse_exhaustion_files import _apply_public_cancel
from .test_capture_contract import _NOW
from .test_record_file_completion_wait import (
    _completed_result,
    _result_port,
)
from ..outputs.test_segment_default_scope import _obtain

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "video_identities",
    [
        (
            "DJI_001/DJI_20261022100000_0007_D.MP4",
            "DJI_001/DJI_20261022100000_0008_D.MP4",
        ),
        (
            "DJI_001/DJI_20261022100000_0007_D.MP4",
            "DJI_001/DJI_20261022100000_0009_D.MP4",
        ),
        (
            "DJI_001/DJI_20261022100000_0007_D.MP4",
            "DJI_001/dji_20261022100000_0007_D.MP4",
        ),
    ],
    ids=("continuous", "known-gap", "duplicate-key"),
)
async def test_action6_native_cancel_defaults_to_all_qualified_videos_and_photo(
    tmp_path, video_identities
):
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, "timelapse", catalog=Action6NativeCatalog(0), start=False
    )
    try:
        (activity_id,) = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (action_id,)
        ).fetchone()
        fixed = runtime.capture.fix_baseline(
            BaselineFixSave(activity_id, 0, 0, _NOW), new_operation_key(), owned
        )
        assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error

        await _control_call(
            runtime,
            runtime.action(action_id),
            "start_timelapse",
            "timelapse_sent",
            activity_facts={"sent_at": _NOW},
        )
        wall = [_NOW + 100_000]
        runtime.wall_us = lambda: wall[0]
        runtime.monotonic_ns = lambda: 6_000_000_000
        _apply_public_cancel(owned, action_id, wall[0])

        stopper = create_autospec(StopDriver, instance=True)
        stopper.stop.return_value = DeviceCallResult.from_outcome(
            returned("stop", "stop_confirmed", activity_id)
        )
        runtime.stopper = stopper

        entries = [
            _entry(identity) for identity in video_identities
        ]
        entries.append(_entry("DJI_001/DJI_20261022100000_0007_D.JPG"))
        entries.sort(key=lambda entry: entry["identity"])

        async def read(request, batch):
            context = request.params["completion_context"]
            completion = {
                "method": "stop_return_and_wait",
                "version": 1,
                "activity_id": context["activity_id"],
                "stop_result_event_id": context["stop_result_event_id"],
                "required_wait_ms": context["file_completion_wait_ms"],
                "observed_wait_ns": context["file_completion_wait_ms"] * 1_000_000,
                "completed": True,
            }
            return _completed_result(
                request.ticket,
                context,
                entries=entries,
                finalized=True,
                completion=completion,
            )

        driver = _result_port(runtime, read)
        await capture_handler(handler)(action_id, runtime)

        assert runtime.action(action_id)["status"] == 6
        ref = SegmentRepository().read_active_set(action_id, owned)
        decision = SegmentRepository().read_decision(ref, owned)
        assert decision.status.value == "canceled"
        assert decision.default_scope.state.value == "nonempty"
        assert decision.default_scope.member_count == len(video_identities)

        outputs = owned.connection.execute(
            "SELECT o.id,o.kind,json_extract(d.identity_key,'$[2]')"
            " FROM outputs o JOIN device_files d ON d.id=o.device_file_id"
            " WHERE o.source_action_id=? ORDER BY o.id",
            (action_id,),
        ).fetchall()
        video_ids = {
            output_id
            for output_id, kind, identity in outputs
            if kind == 1 and identity.endswith(".MP4")
        }
        photo_ids = {
            output_id
            for output_id, kind, identity in outputs
            if kind == 1 and identity.endswith(".JPG")
        }
        assert len(video_ids) == len(video_identities)
        assert len(photo_ids) == 1

        (set_id,) = owned.connection.execute(
            "SELECT id FROM segment_sets WHERE action_id=? AND version=?",
            (action_id, ref.version),
        ).fetchone()
        scope_ids = {
            row[0]
            for row in owned.connection.execute(
                "SELECT output_id FROM segment_scope_members WHERE set_id=?",
                (set_id,),
            )
        }
        assert scope_ids == video_ids

        _, selection_id = _obtain(owned, runtime, action_id, "default", 99)
        selected = fix_segment_selection(
            FixSegmentSelection(selection_id, runtime.wall_us()),
            new_operation_key(),
            owned,
        )
        assert selected.kind is DbOutcomeKind.COMPLETED, selected.error
        selected_ids = {
            row[0]
            for row in owned.connection.execute(
                "SELECT output_id FROM obtain_items WHERE selection_id=?",
                (selection_id,),
            )
        }
        assert selected_ids == video_ids | photo_ids
        driver.list_results.assert_awaited_once()
        stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()

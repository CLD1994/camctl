"""Action6 分段普通录像沿真实当前会话计时进入异常内容检查。"""

import json
from decimal import Decimal
from unittest.mock import create_autospec

import pytest

from camctl.capture.baseline_models import BaselineFixSave
from camctl.capture.handlers import capture_handler
from camctl.contracts.values import new_operation_key
from camctl.devices.ports import DeviceCallResult, StopDriver
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.segments import SegmentRepository

from .result_consumer_fixtures import consumer_world, returned
from .test_action6_segmented_normal import Action6RecordCatalog
from .test_action6_segmented_recovery import _attach_media, _entries
from .test_capture_contract import _NOW, _PAGE_EVIDENCE, ResultsDouble
from ..support.capture import capture_runtime
from .test_record_file_completion_wait import (
    _completed_result,
    _result_port,
    _stop_context,
)

pytestmark = pytest.mark.asyncio


async def test_action6_recording_handler_persists_current_session_elapsed_and_inspects(
    tmp_path,
):
    clock = {"wall_us": _NOW, "monotonic_ns": 5_000_000_000}
    catalog = Action6RecordCatalog()
    catalog.duration_s = Decimal("60")
    stopper = create_autospec(StopDriver, instance=True)
    stopper.stop.side_effect = lambda request: DeviceCallResult.from_outcome(
        returned("stop", "stop_confirmed", request.ticket.target_id))
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, "record", catalog=catalog, start=False,
        runtime_options={"wall_us": lambda: clock["wall_us"],
            "monotonic_ns": lambda: clock["monotonic_ns"], "stopper": stopper,
            "devices": {"cam-1": {"driver": "camctl-adb",
                "recording": {"repair_margin_s": Decimal("2")}}}},
    )

    (activity_id,) = owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id=?", (action_id,)
    ).fetchone()
    fixed = runtime.capture.fix_baseline(
        BaselineFixSave(activity_id, 0, 0, _NOW), new_operation_key(), owned
    )
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error

    async def read_results(request, batch):
        return _completed_result(
            request.ticket,
            _stop_context(owned, action_id),
            entries=_entries(4),
            finalized=True,
        )

    result_driver = _result_port(runtime, read_results)
    tools = (await _attach_media(
        runtime,
        owned,
        tmp_path,
        (Decimal("18"), Decimal("27"), Decimal("22"), Decimal("40")),
    ))

    try:
        await capture_handler(handler)(action_id, runtime)
        stopper.stop.assert_not_awaited()
        spec, = owned.connection.execute(
            "SELECT execution_spec_json FROM actions WHERE id=?", (action_id,)).fetchone()
        spec = json.loads(spec)
        start_anchor_ns = clock["monotonic_ns"]
        stop_target_ns = start_anchor_ns + (
            spec["target_duration_ms"] + spec.get("startup_margin_ms", 0)
        ) * 1_000_000
        clock["monotonic_ns"] = stop_target_ns - 1
        await capture_handler(handler)(action_id, runtime)
        stopper.stop.assert_not_awaited()
        clock["monotonic_ns"] = start_anchor_ns + 75_000_000_000
        clock["wall_us"] = _NOW + 75_000_000

        await capture_handler(handler)(action_id, runtime)

        (control_elapsed_ns,) = owned.connection.execute(
            "SELECT control_elapsed_ns FROM device_activities WHERE action_id=?",
            (action_id,),
        ).fetchone()
        assert control_elapsed_ns == 75_000_000_000
        stopper.stop.assert_awaited_once()
        result_driver.list_results.assert_awaited_once()
        assert len(tools.calls) == 3
        assert all(not count_frames for _, count_frames in tools.calls)

        repository = SegmentRepository()
        ref = repository.read_active_set(action_id, owned)
        decision = repository.read_decision(ref, owned)
        assert decision.status.value == "succeeded"
        assert decision.coverage_status.value == "reached"
        assert decision.metric.value == "duration_s"
        assert decision.target == Decimal("60")
        assert decision.confirmed == Decimal("67")
        assert decision.checked_count == 3
    finally:
        owned.connection.close()


async def test_action6_recording_stop_after_restart_saves_unknown_without_reusing_clock(
    tmp_path, capture_resources,
):
    clock = {"wall_us": _NOW, "monotonic_ns": 5_000_000_000}
    catalog = Action6RecordCatalog()
    catalog.duration_s = Decimal("60")
    controls = []
    stopper = create_autospec(StopDriver, instance=True)
    stopper.stop.side_effect = lambda request: DeviceCallResult.from_outcome(
        returned("stop", "stop_confirmed", request.ticket.target_id))

    async def assemble(owned, *, driver):
        controls.append(driver)
        return await capture_runtime(owned, driver=driver, evidence=_PAGE_EVIDENCE,
            results=ResultsDouble({}), stopper=stopper,
            wall_us=lambda: clock["wall_us"], monotonic_ns=lambda: clock["monotonic_ns"])

    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, "record", catalog=catalog, start=False, runtime_factory=assemble)

    (activity_id,) = owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id=?", (action_id,)
    ).fetchone()
    fixed = runtime.capture.fix_baseline(
        BaselineFixSave(activity_id, 0, 0, _NOW), new_operation_key(), owned
    )
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error

    try:
        await capture_handler(handler)(action_id, runtime)
        stopper.stop.assert_not_awaited()
        clock["monotonic_ns"] = 1_000_000_000
        clock["wall_us"] = _NOW + 75_000_000
        database = await capture_resources.restart_database(owned)
        runtime = await capture_resources.runtime(database, driver=controls[0],
            evidence=_PAGE_EVIDENCE, results=ResultsDouble({}), stopper=stopper,
            wall_us=lambda: clock["wall_us"], monotonic_ns=lambda: clock["monotonic_ns"])
        await capture_handler(handler)(action_id, runtime)
        stopper.stop.assert_awaited_once()
        assert owned.connection.execute(
            "SELECT control_elapsed_ns FROM device_activities WHERE action_id=?",
            (action_id,),
        ).fetchone() == (None,)

        stop_result_event_id = _stop_context(owned, action_id)[
            "stop_result_event_id"
        ]
        stop_observation = next(
            json.loads(body_json)["evidence"]["observation"]
            for (body_json,) in owned.connection.execute(
                "SELECT body_json FROM history_events WHERE id>? ORDER BY id",
                (stop_result_event_id,),
            )
            if json.loads(body_json).get("evidence", {})
            .get("observation", {})
            .get("stop_result_event_id")
            == stop_result_event_id
        )
        assert "control_elapsed_ns" in stop_observation
        assert stop_observation["control_elapsed_ns"] is None
    finally:
        owned.connection.close()

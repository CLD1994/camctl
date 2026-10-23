"""Action6 分段普通录像沿真实当前会话计时进入异常内容检查。"""

import json
from decimal import Decimal
from unittest.mock import create_autospec

import pytest

from camctl.capture.baseline_models import BaselineFixSave
from camctl.capture.handlers import _recording_port, _stop_call, capture_handler
from camctl.contracts.values import new_operation_key
from camctl.devices.ports import DeviceCallResult, StopDriver
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.segments import SegmentRepository

from .result_consumer_fixtures import consumer_world, returned
from .test_action6_segmented_normal import Action6RecordCatalog
from .test_action6_segmented_recovery import _attach_media, _entries
from .test_capture_contract import _NOW
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
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, "record", catalog=catalog, start=False
    )
    runtime.wall_us = lambda: clock["wall_us"]
    runtime.monotonic_ns = lambda: clock["monotonic_ns"]
    runtime.repair_margin_s = Decimal("2")

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
    tools = _attach_media(
        runtime,
        owned,
        tmp_path,
        (Decimal("18"), Decimal("27"), Decimal("22"), Decimal("40")),
    )
    stopper = create_autospec(StopDriver, instance=True)
    stopper.stop.return_value = DeviceCallResult.from_outcome(
        returned("stop", "stop_confirmed", activity_id)
    )
    runtime.stopper = stopper

    try:
        await capture_handler(handler)(action_id, runtime)
        state = _recording_port(runtime).recording_state(action_id)
        assert state.anchor_from_current_session
        assert state.stop_target_ns is not None
        stop_target_ns = state.stop_target_ns

        # 模拟同一 run 的单调时钟到达真实停止调用之后；启动及内容起点
        # 都来自生产 handler / RecordingStatePort，不从 finish 注入时长。
        start_anchor_ns = clock["monotonic_ns"]
        target_ms = runtime.action(action_id)["execution_spec_json"][
            "target_duration_ms"
        ]
        startup_margin_ms = runtime.action(action_id)["execution_spec_json"].get(
            "startup_margin_ms", 0
        )
        assert stop_target_ns == start_anchor_ns + (
            target_ms + startup_margin_ms
        ) * 1_000_000
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
    tmp_path,
):
    clock = {"wall_us": _NOW, "monotonic_ns": 5_000_000_000}
    catalog = Action6RecordCatalog()
    catalog.duration_s = Decimal("60")
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, "record", catalog=catalog, start=False
    )
    runtime.wall_us = lambda: clock["wall_us"]
    runtime.monotonic_ns = lambda: clock["monotonic_ns"]

    (activity_id,) = owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id=?", (action_id,)
    ).fetchone()
    fixed = runtime.capture.fix_baseline(
        BaselineFixSave(activity_id, 0, 0, _NOW), new_operation_key(), owned
    )
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error

    stopper = create_autospec(StopDriver, instance=True)
    stopper.stop.return_value = DeviceCallResult.from_outcome(
        returned("stop", "stop_confirmed", activity_id)
    )
    runtime.stopper = stopper

    try:
        await capture_handler(handler)(action_id, runtime)
        assert _recording_port(runtime).recording_state(
            action_id
        ).anchor_from_current_session

        # 重启后旧 monotonic 不能和新 session 拼接成原始控制时长。
        runtime.recording_state = None
        clock["monotonic_ns"] = 1_000_000_000
        clock["wall_us"] = _NOW + 75_000_000
        assert not _recording_port(runtime).recording_state(
            action_id
        ).anchor_from_current_session

        stopped = await _stop_call(runtime, runtime.action(action_id))

        assert stopped.phase == "confirmed"
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

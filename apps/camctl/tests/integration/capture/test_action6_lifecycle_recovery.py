"""Action6 普通录像与原生延时的异常流程通过分段发布器保存结果。"""

from decimal import Decimal
from unittest.mock import create_autospec

import pytest

from ..support.capture import capture_database
from ..support.result_cases import CaseClock

from camctl.capture.baseline_models import BaselineFixSave
from camctl.capture.handlers import capture_handler
from camctl.contracts.values import new_operation_key
from camctl.devices.bindings import check_binding
from camctl.devices.ports import DeviceCallResult, StopDriver
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import ErrorValue
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.segments import SegmentRepository

from .test_record_file_completion_wait import _result_port, _result_setup
from .result_consumer_fixtures import consumer_world
from .test_action6_segmented_recovery import (
    _Action6Native151Catalog,
    _ProbeTools,
    _media_options,
    _entries,
    _overrun_recording,
    _publish_ordinary_result,
)
from .test_action6_segmented_normal import Action6NativeCatalog, Action6RecordCatalog
from .test_call_result_save import _outcome
from .test_cancel_timelapse_exhaustion_files import _apply_public_cancel
from .test_capture_contract import _NOW
from .result_consumer_fixtures import returned

pytestmark = pytest.mark.asyncio


async def test_action6_ordinary_cancel_publishes_canceled_segment_result_without_outputs(
    tmp_path, monkeypatch,
):
    owned, runtime, action_id, handler, _, result_port, evidence = await _overrun_recording(
        tmp_path, monkeypatch, (Decimal("18"), Decimal("27"), Decimal("22"))
    )
    try:
        _apply_public_cancel(owned, action_id, _NOW + 90_000_000)

        await capture_handler(handler)(action_id, runtime)

        assert (await runtime.action(action_id))["status"] == 6
        assert owned.connection.execute(
            "SELECT publication_kind,state,published_event_id IS NOT NULL"
            " FROM segment_sets WHERE action_id=?",
            (action_id,),
        ).fetchone() == (2, 3, 1)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM outputs WHERE source_action_id=?", (action_id,)
        ).fetchone() == (0,)
    finally:
        owned.connection.close()


async def test_action6_baseline_failure_publishes_no_results_source(tmp_path):
    from camctl.devices.directory import DirectoryRead
    from .test_baseline_prepare import Directory

    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, "record", catalog=Action6RecordCatalog(), start=False
    )
    try:
        runtime.baseline_directory = Directory([
            lambda request: DirectoryRead(
                None,
                ErrorValue("adb_directory_failed", "device_file_access", {
                    "reason": "injected_baseline_failure",
                }),
                None,
            )
        ])
        await capture_handler(handler)(action_id, runtime)

        assert (await runtime.action(action_id))["status"] == 4
        assert owned.connection.execute(
            "SELECT source_kind,member_count,published_event_id IS NOT NULL"
            " FROM segment_sets WHERE action_id=?",
            (action_id,),
        ).fetchone() == (3, 0, 1)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM outputs WHERE source_action_id=?", (action_id,)
        ).fetchone() == (0,)
        assert owned.connection.execute(
            "SELECT reason FROM segment_check_conclusions"
        ).fetchone() == (9,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("consumer", ["record", "timelapse"])
async def test_action6_start_no_effect_failure_publishes_no_results_source(
    tmp_path, consumer,
):
    from .test_recording_start_runtime import StartDriver, _result

    catalog = (
        Action6RecordCatalog()
        if consumer == "record"
        else Action6NativeCatalog(0)
    )
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, consumer, catalog=catalog, start=False
    )
    driver = StartDriver(_result(no_effect=True, error=ErrorValue(
        "device_start_failed", "execution", {"operation_run_id": "1"},
    )))
    runtime.driver = driver
    try:
        await capture_handler(handler)(action_id, runtime)

        assert (await runtime.action(action_id))["status"] == 4
        assert owned.connection.execute(
            "SELECT source_kind,member_count,published_event_id IS NOT NULL"
            " FROM segment_sets WHERE action_id=?",
            (action_id,),
        ).fetchone() == (3, 0, 1)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM outputs WHERE source_action_id=?", (action_id,)
        ).fetchone() == (0,)
        assert owned.connection.execute(
            "SELECT reason FROM segment_check_conclusions"
        ).fetchone() == (9,)
        assert len(driver.calls) == 1
    finally:
        owned.connection.close()


async def test_action6_stop_budget_exhaustion_publishes_unconfirmed_without_releasing_activity(
    tmp_path,
):
    from .test_action6_segmented_recovery import Action6RecordCatalog

    clock = CaseClock(_NOW, 5_000_000_000)
    stopper = create_autospec(StopDriver, instance=True)
    stopper.stop.return_value = DeviceCallResult.from_outcome(_outcome("stop"))
    task_catalog = Action6RecordCatalog()
    task_catalog.duration_s = Decimal("60")
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, "record", catalog=task_catalog, start=False, clock=clock,
        runtime_options={"stopper": stopper},
    )
    try:
        (activity_id,) = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (action_id,)
        ).fetchone()
        fixed = runtime.capture.fix_baseline(
            BaselineFixSave(activity_id, 0, 0, _NOW), new_operation_key(), owned
        )
        assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
        await capture_handler(handler)(action_id, runtime)
        action = (await runtime.action(action_id))
        assert action["execution_started"] == 1
        assert action["execution_spec_json"]["result_policy"] == (
            "action6_video_segments_v1"
        )

        runtime.stop_config = AttemptConfig(1, Decimal("1"), Decimal(0))
        clock.wall = _NOW + 75_000_000
        clock.monotonic = 80_000_000_000
        await capture_handler(handler)(action_id, runtime)
        await capture_handler(handler)(action_id, runtime)

        assert (await runtime.action(action_id))["status"] == 4
        assert owned.connection.execute(
            "SELECT activity_state,occupancy_state FROM device_activities WHERE id=?",
            (activity_id,),
        ).fetchone() == (2, 1)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM segment_sets WHERE action_id=?", (action_id,)
        ).fetchone() == (1,)
        run_status, = owned.connection.execute(
            "SELECT status FROM operation_runs WHERE responsibility_key=?",
            (f"stop/{action_id}",),
        ).fetchone()
        assert run_status == 6
        ref = SegmentRepository().read_active_set(action_id, owned)
        assert ref is not None
        assert owned.connection.execute(
            "SELECT reason FROM segment_check_conclusions WHERE set_id=?",
            (ref.set_id,),
        ).fetchone() == (8,)
    finally:
        owned.connection.close()


async def test_action6_binding_failure_uses_external_segment_terminal(tmp_path, monkeypatch):
    from types import SimpleNamespace

    owned, runtime, action_id, _handler, _tools, result_port, evidence = await _overrun_recording(
        tmp_path, monkeypatch, (Decimal("18"), Decimal("27"), Decimal("22"))
    )
    try:
        runtime.binding_check = lambda binding: check_binding(
            binding, SimpleNamespace(devices={})
        )
        await capture_handler("camera_record")(action_id, runtime)

        assert (await runtime.action(action_id))["status"] == 4
        assert owned.connection.execute(
            "SELECT published_event_id IS NOT NULL FROM segment_sets"
        ).fetchone() == (1,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM outputs WHERE source_action_id=?", (action_id,)
        ).fetchone() == (0,)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM segment_check_conclusions"
        ).fetchone() == (1,)
    finally:
        owned.connection.close()


async def test_action6_native_required_photo_failure_reaches_recovery_through_real_handler(
    tmp_path, monkeypatch,
):
    result_port, evidence = _result_setup()
    clock = CaseClock(_NOW, 5_000_000_000)
    tools = _ProbeTools((60, 60, 31, 20), monkeypatch)
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, "timelapse", catalog=_Action6Native151Catalog(0), start=False, clock=clock,
        runtime_options=_media_options(tools, results=result_port, evidence=evidence,
            wall_us=clock.wall_us, monotonic_ns=clock.monotonic_ns)
    )
    try:
        (activity_id,) = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (action_id,)
        ).fetchone()
        fixed = runtime.capture.fix_baseline(
            BaselineFixSave(activity_id, 0, 0, _NOW), new_operation_key(), owned
        )
        assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error

        from camctl.capture.timelapse import CaptureWaitConfig
        from .test_result_page_history import _actual

        await capture_handler(handler)(action_id, runtime)
        runtime.wait_config = lambda action: CaptureWaitConfig(300_000, 200, 0, 0)
        await capture_handler(handler)(action_id, runtime)

        async def read(request, batch):
            return DeviceCallResult.from_outcome(
                _actual(request.ticket, entries=_entries(4), finalized=True)
            )

        driver = _result_port(result_port, evidence, read)
        clock.wall = _NOW + 300_200_000
        clock.monotonic += 300_200_000_000
        await capture_handler(handler)(action_id, runtime)

        assert driver.list_results.await_count == 1
        assert len(tools.calls) == 3
        assert all(count_frames for _, count_frames in tools.calls)
        ref = SegmentRepository().read_active_set(action_id, owned)
        assert ref is not None
        decision = SegmentRepository().read_decision(ref, owned)
        assert decision.status.value == "failed"
        assert decision.coverage_status.value == "reached"
        assert decision.confirmed == 151
        assert decision.checked_count == 3
        assert (await runtime.action(action_id))["status"] == 4
        event_count = owned.connection.execute(
            "SELECT COUNT(*) FROM history_events"
        ).fetchone()
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM history_events"
        ).fetchone() == event_count
        assert len(tools.calls) == 3
        driver.list_results.assert_awaited_once()
    finally:
        owned.connection.close()


async def test_action6_pending_inspection_waits_for_full_session_then_cancels_without_resume(
    tmp_path, monkeypatch, capture_resources,
):
    from camctl.contracts.enums import enum_for
    from camctl.persistence.repositories.segment_inspections import read_inspection_for_member
    from .test_segment_inspections import _CONTENT, _ReadDriver

    owned, runtime, action_id, handler, tools, result_port, evidence = await _overrun_recording(
        tmp_path, monkeypatch, (Decimal("18"), Decimal("27"), Decimal("22")),
        read_driver=_ReadDriver(_CONTENT, fail_after=4), segment_size=4,
    )
    try:
        await _publish_ordinary_result(runtime, owned, action_id, handler, _entries(3), result_port, evidence)

        facts = read_inspection_for_member(action_id, 1, owned)
        assert facts.inspection is not None
        assert facts.inspection.state == int(
            enum_for("segment_inspections.state").PENDING
        )
        assert facts.copy is not None
        assert 0 < facts.copy.committed_bytes < facts.copy.source_size
        assert tools.calls == []
        output_count_before_cancel = owned.connection.execute(
            "SELECT COUNT(*) FROM outputs WHERE source_action_id=?", (action_id,)
        ).fetchone()
        assert output_count_before_cancel == (0,)

        # 受限媒体会话可以继续保存已有状态，但不重开输入或启动媒体工具。
        # READ 已失败结束，部分拷贝和结果均已可靠保存；受限会话
        # 从该持久事实恢复，复用原装配输入，只关闭媒体能力。
        runtime = await capture_resources.runtime(owned, driver=tools.assembly_driver,
            **{**tools.assembly_options, "media_enabled": False})
        event_count = owned.connection.execute(
            "SELECT COUNT(*) FROM history_events"
        ).fetchone()
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM history_events"
        ).fetchone() == event_count
        assert tools.calls == []
        assert read_inspection_for_member(action_id, 1, owned).inspection.state == int(
            enum_for("segment_inspections.state").PENDING
        )

        _apply_public_cancel(owned, action_id, _NOW + 90_000_000)
        await capture_handler(handler)(action_id, runtime)
        facts = read_inspection_for_member(action_id, 1, owned)
        assert facts.inspection.state == int(
            enum_for("segment_inspections.state").UNCONFIRMED
        )
        assert (await runtime.action(action_id))["status"] == 6
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM outputs WHERE source_action_id=?", (action_id,)
        ).fetchone() == (0,)
        assert owned.connection.execute(
            "SELECT publication_kind,member_count,published_event_id IS NOT NULL"
            " FROM segment_sets WHERE action_id=? ORDER BY id DESC LIMIT 1",
            (action_id,),
        ).fetchone() == (2, 3, 1)
        event_count = owned.connection.execute(
            "SELECT COUNT(*) FROM history_events"
        ).fetchone()
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM history_events"
        ).fetchone() == event_count
        assert tools.calls == []
    finally:
        owned.connection.close()


async def test_action6_native_cancel_retains_qualified_videos_and_required_photo(
    tmp_path,
):
    result_port, evidence = _result_setup()
    clock = CaseClock(_NOW, 5_000_000_000)
    stopper = create_autospec(StopDriver, instance=True)
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, "timelapse", catalog=Action6NativeCatalog(0), start=False, clock=clock,
        runtime_options={"stopper": stopper, "results": result_port, "evidence": evidence},
    )
    try:
        (activity_id,) = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (action_id,)
        ).fetchone()
        fixed = runtime.capture.fix_baseline(
            BaselineFixSave(activity_id, 0, 0, _NOW), new_operation_key(), owned
        )
        assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
        from .test_record_file_completion_wait import _completed_result

        await capture_handler(handler)(action_id, runtime)
        clock.wall = _NOW + 100_000
        clock.monotonic = 6_000_000_000
        _apply_public_cancel(owned, action_id, clock.wall)
        stopper.stop.return_value = DeviceCallResult.from_outcome(
            returned("stop", "stop_confirmed", activity_id)
        )

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
                request.ticket, context,
                entries=_entries(3, photo=True), finalized=True,
                completion=completion,
            )

        driver = _result_port(result_port, evidence, read)
        await capture_handler(handler)(action_id, runtime)

        assert (await runtime.action(action_id))["status"] == 6
        ref = SegmentRepository().read_active_set(action_id, owned)
        assert ref is not None
        decision = SegmentRepository().read_decision(ref, owned)
        assert decision.status.value == "canceled"
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM outputs WHERE source_action_id=?", (action_id,)
        ).fetchone() == (4,), (
            owned.connection.execute(
                "SELECT id,identity_key,completion_state,size_bytes FROM device_files"
            ).fetchall(),
            owned.connection.execute(
                "SELECT file_id,role,ownership,completion FROM segment_members"
            ).fetchall(),
        )
        identities = [
            row[0]
            for row in owned.connection.execute(
                "SELECT json_extract(identity_key,'$[2]') FROM device_files"
                " WHERE source_action_id=? ORDER BY id",
                (action_id,),
            )
        ]
        assert sum(identity.endswith(".MP4") for identity in identities) == 3
        assert sum(identity.endswith(".JPG") for identity in identities) == 1
        driver.list_results.assert_awaited_once()
        stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()

"""真实 OSMO 部署绑定穿过单文件媒体链及 native 公共发布。"""

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.capture.handlers import capture_handler
from camctl.capture.media_flow import DriverReadSessions
from camctl.capture.segment_storage import SegmentResultRef
from camctl.contracts.values import ConsistencyError
from camctl.host_files.tasks import FileTaskId, FileTaskResult
from camctl.operations.recovery import RecoveryBoundary
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.segments import SegmentRepository

from ..capture.test_recording_media_link import _RepairTools, _media
from ..capture.test_media_flow import ReadDriverDouble, _Digest
from ..capture.test_osv_probe_history import dual_probe
from ..scheduling.test_start_action import owned
from .test_osmo_shared_recovery import (
    _SCHEDULED,
    _capture,
    _result_attempt,
    _reopen,
    _save_actual_page,
    _stop_call,
    _world,
)

pytestmark = pytest.mark.asyncio


async def test_single_file_result_reader_uses_frozen_mode_after_deployment_change(
    owned, tmp_path, monkeypatch
):
    world = await _world(owned, tmp_path, monkeypatch, single_original=True)
    try:
        osv_path = world.channel.result_path
        lrf_path = osv_path.rsplit(".", 1)[0] + ".LRF"
        world.channel.result_paths = [osv_path, lrf_path]
        runtime = _capture(world)
        await capture_handler(world.kind)(1, runtime)

        world.wall[0] += 22_000_000
        world.mono[0] += 22_000_000_000
        stopped = await _stop_call(runtime, runtime.action(1), "stop_recording")
        assert stopped.phase == "confirmed", stopped

        horizon = _reopen(world)
        # 本轮重启前尚无结果列举调用；开放的是新派发，而非重放旧调用。
        world.channel.forbidden = False
        current_devices = load_config({"devices": {"cam-1": {
            "kind": "camera", "driver": world.binding.driver_id,
            "video": {"result_mode": "segmented"}, "adb": {"serial": "serial-1"},
            "recording": {"start_retry_interval_s": "0"},
            "result_check": {"max_attempts": 1, "retry_interval_s": "0"},
            "copy": {"retry_interval_s": "0"},
        }}}, ConfigDefaults()).devices
        resumed = _capture(world, devices=current_devices,
            recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=lambda: horizon)

        requests = []
        results = []
        list_results = world.entry.driver.list_results

        async def observe_request(request, batch):
            requests.append(request)
            result = await list_results(request, batch)
            results.append(result)
            return result

        world.entry.driver.list_results = observe_request

        async def elapsed(seconds):
            world.mono[0] += int(seconds * 1_000_000_000)

        monkeypatch.setattr("asyncio.sleep", elapsed)
        await capture_handler(world.kind)(1, resumed)

        assert len(requests) == 1
        assert requests[0].params["video_result_mode"] == "single_file"
        listed = next(value.data for value in results[0].outcome.observations
            if value.type == "result_files_listed")
        entries = {entry["identity"]: entry for entry in listed["entries"]}
        assert entries[osv_path]["paired_identity"] is None
        assert entries[lrf_path]["paired_identity"] == osv_path
        frozen = json.loads(world.owned.connection.execute(
            "SELECT execution_spec_json FROM actions WHERE id=1").fetchone()[0])
        assert frozen["video_result_mode"] == "single_file"
    finally:
        world.owned.connection.close()


async def test_osmo_single_file_resume_dispatches_automatic_media_repair(
    owned, tmp_path, monkeypatch
):
    world = await _world(owned, tmp_path, monkeypatch, single_original=True)
    try:
        action = world.owned.connection.execute(
            "SELECT execution_spec_json FROM actions WHERE id=1"
        ).fetchone()[0]
        frozen = json.loads(action)
        assert frozen["video_result_mode"] == "single_file"
        assert frozen.get("result_policy") is None

        runtime, ticket = await _result_attempt(world, elapsed_us=22_000_000)
        await _save_actual_page(world, runtime, ticket, monkeypatch, "terminal")

        horizon = _reopen(world)
        resumed = _capture(
            world,
            recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=lambda: horizon,
        )
        tools = _RepairTools("200")

        async def probe_osv_or_mp4(input_file):
            tools.calls.append("probe")
            probe = dual_probe()
            if input_file.relative_path.lower().endswith(".mp4"):
                probe = replace(
                    probe,
                    duration_s=Decimal("10"),
                    streams=tuple(
                        replace(stream, duration_s=Decimal("10"))
                        for stream in probe.streams
                        if stream.attached_pic is not True
                    ),
                )
            return FileTaskResult(
                task_id=FileTaskId("probe"), ran=True, value=probe
            )

        async def repair_osv(input_file, output_file, *, trim_s, **kwargs):
            tools.calls.append("repair")
            return FileTaskResult(
                task_id=FileTaskId("repair"), ran=True,
                value=SimpleNamespace(
                    complete=True, size_bytes=10, digest="a" * 64
                ),
            )

        tools.probe = probe_osv_or_mp4
        tools.repair = repair_osv
        resumed.media = _media(
            world.owned, tools, Path(world.cfg.paths.staging)
        )
        # The formal OSMO fixture reports a 50-byte source. Match the local
        # read stream and digest to that observed source size so the real copy
        # path can complete before probe and repair are invoked.
        source_bytes = b"0" * 50
        resumed.media.sessions = DriverReadSessions(
            world.owned, ReadDriverDouble(source_bytes), ticket=None
        )
        resumed.media.digest = _Digest(source_bytes)
        # This workflow's scheduled time is fixed independently of the older
        # media-link fixture clock; use the world's current wall clock so the
        # internal read qualification is due.
        resumed.media.occurred_at = lambda: world.wall[0]

        await capture_handler(world.kind)(1, resumed)
        # OSMO repair has a persisted prepare step before the native container
        # is processed; the next handler turn resumes that saved responsibility.
        await capture_handler(world.kind)(1, resumed)

        assert tools.calls == ["probe", "repair", "probe"]
        assert world.owned.connection.execute(
            "SELECT status FROM actions WHERE id=1"
        ).fetchone() == (3,)
        processing = world.owned.connection.execute(
            "SELECT source_device_file_id,repair_state,repair_output_file_id "
            "FROM recording_processing WHERE action_id=1"
        ).fetchone()
        assert processing[0] is not None
        assert processing[1] == 5
        assert processing[2] is not None
        assert world.owned.connection.execute(
            "SELECT kind FROM outputs WHERE source_action_id=1 ORDER BY kind"
        ).fetchall() == [(1,), (2,)]
        assert world.owned.connection.execute(
            "SELECT COUNT(*) FROM segment_sets WHERE action_id=1"
        ).fetchone() == (0,)
    finally:
        world.owned.connection.close()


async def test_osmo_native_segment_resume_publishes_saved_conclusion_without_device_calls(
    owned, tmp_path, monkeypatch
):
    world = await _world(owned, tmp_path, monkeypatch, finite=True)
    try:
        frozen = json.loads(
            world.owned.connection.execute(
                "SELECT execution_spec_json FROM actions WHERE id=1"
            ).fetchone()[0]
        )
        assert frozen["video_result_mode"] == "segmented"
        assert frozen["result_policy"] == "osmo_empty_tail_classification_v1"

        runtime = _capture(world)
        await capture_handler(world.kind)(1, runtime)

        # OSMO 原生有限任务在目标、结果余量和额外等待到期后自然完成。
        wait_ms = frozen["target_duration_ms"] + 30_000 + 1_000
        world.wall[0] = _SCHEDULED + wait_ms * 1_000 + 1
        world.mono[0] = wait_ms * 1_000_000 + 1

        finish = runtime.capture.finish_segment_recording
        runtime.capture.finish_segment_recording = lambda *args, **kwargs: DbOutcome(
            DbOutcomeKind.ROLLED_BACK, error=OSError("公共结果提交前中断")
        )
        with pytest.raises(ConsistencyError):
            await capture_handler(world.kind)(1, runtime)
        runtime.capture.finish_segment_recording = finish

        prepared = SegmentRepository().read_prepared_result_ref(1, world.owned)
        assert isinstance(prepared, SegmentResultRef)
        assert world.owned.connection.execute(
            "SELECT COUNT(*) FROM segment_check_conclusions WHERE set_id=?",
            (prepared.set_id,),
        ).fetchone() == (1,)
        assert world.owned.connection.execute(
            "SELECT published_event_id FROM segment_sets WHERE id=?",
            (prepared.set_id,),
        ).fetchone() == (None,)
        calls_before_recovery = tuple(world.channel.calls)
        result_tickets_before_recovery = tuple(world.channel.result_tickets)

        horizon = _reopen(world)
        resumed = _capture(
            world,
            recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=lambda: horizon,
        )
        await capture_handler(world.kind)(1, resumed)

        assert tuple(world.channel.calls) == calls_before_recovery
        assert tuple(world.channel.result_tickets) == result_tickets_before_recovery
        assert world.owned.connection.execute(
            "SELECT status FROM actions WHERE id=1"
        ).fetchone() == (3,)
        assert world.owned.connection.execute(
            "SELECT published_event_id IS NOT NULL FROM segment_sets WHERE id=?",
            (prepared.set_id,),
        ).fetchone() == (1,)
        assert world.owned.connection.execute(
            "SELECT COUNT(*) FROM segment_check_conclusions WHERE set_id=?",
            (prepared.set_id,),
        ).fetchone() == (1,)
        assert world.owned.connection.execute(
            "SELECT COUNT(*) FROM outputs WHERE source_action_id=1"
        ).fetchone() == (1,)
        assert world.owned.connection.execute(
            "SELECT COUNT(*) FROM file_copies"
        ).fetchone() == (0,)
    finally:
        world.owned.connection.close()

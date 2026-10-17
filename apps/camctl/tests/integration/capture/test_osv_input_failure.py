"""任何已保存原片阶段都不能用控制完成覆盖明确的原输入媒体失败。"""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.capture.handlers import _register_observed, capture_handler
from camctl.capture.media import MediaPolicy
from camctl.capture.media_flow import MediaFlow, DriverReadSessions, run_recording_media, load_processing_status
from camctl.capture.processing import SourceFileSave
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import new_operation_key
from camctl.host_files.media import MediaProbe, MediaArtifact
from camctl.host_files.models import BoundDirectories, FileObservation, FileObservationKind
from camctl.host_files.tasks import FileTaskResult, FileTaskId
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from .test_media_flow import ReadDriverDouble, _Digest
from .test_recording_media_link import _environment, _runtime, _CONTENT, _NOW
from .test_capture_contract import _entry
from .test_osv_probe_history import dual_probe


class Tools:
    def __init__(self, probe):
        self.actual = probe
        self.repairs = 0
        self.probes = 0

    async def probe(self, input):
        self.probes += 1
        return FileTaskResult(FileTaskId("source-probe"), ran=True, value=self.actual)

    async def repair(self, input, output, **kwargs):
        self.repairs += 1
        return FileTaskResult(FileTaskId("repair"), ran=True,
            value=MediaArtifact("tool_failed: exit=1", FileObservation(
                FileObservationKind.MISSING, Path("staging/derived/output.mp4"))))


@pytest.mark.asyncio
@pytest.mark.parametrize("check_required", [True, False])
@pytest.mark.parametrize("fault", ["short_video", "short_audio", "short_audio_unknown", "missing_video", "no_video_empty", "short_container", "short_container_unknown", "unknown", "tool_failed", "valid"])
async def test_capture_result_respects_saved_source_probe_without_fabricating_check(tmp_path, check_required, fault):
    owned, runtime, flow, source_id, source, tools, entry = prepared_input(tmp_path, check_required, fault)
    try:
        await run_recording_media(flow, 1, 41, source_id, target_extension="osv")
        status = load_processing_status(owned, 41)
        assert status.source_probe == source
        media = parse_exact_json(owned.connection.execute("SELECT media_json FROM recording_processing WHERE id=41").fetchone()[0])
        if check_required and source.error is None and source.duration_s is not None:
            assert media["duration"] == {"status":"available","seconds":source.duration_s}
            assert not {"streams", "source_probe", "output_probe"} & media.keys()
            if fault in ("short_container", "short_container_unknown"):
                assert any(issue["code"] == "container_too_short" and
                    issue["details"]["actual_duration_s"] == Decimal("55") for issue in media["issues"])
        if not check_required:
            assert status.check_state == 1
            assert parse_exact_json(owned.connection.execute("SELECT media_json FROM recording_processing WHERE id=41").fetchone()[0]) == {}
        elif fault in ("short_video", "short_audio"):
            assert status.check_duration_s == Decimal("75") and status.check_issues
        await capture_handler("camera_record")(1, runtime)
        actual = owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone()[0]
        known_failure = fault in ("short_video", "short_audio", "short_audio_unknown", "missing_video", "no_video_empty", "short_container", "short_container_unknown")
        expected = 4 if known_failure else 3
        assert actual == expected
        if known_failure or fault in ("unknown", "tool_failed"):
            assert tools.repairs == 0
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs WHERE source_action_id=1 AND kind=1").fetchone()[0] == 1
    finally:
        owned.connection.close()


def prepared_input(tmp_path, check_required, fault):
    basis = '{"reason":2,"target_duration_ms":60000}' if check_required else (
    '{"reason":3,"target_duration_ms":60000,"control_elapsed_ns":75000000000}')
    owned, staging = _environment(tmp_path, check_decision=3 if check_required else 2, check_basis=basis)
    entry = replace(_entry("clip", size=len(_CONTENT)), original_name="clip.OSV",
        media_type=None, format_id="osv")
    runtime = _runtime(owned, {1: (entry,)})
    registered = _register_observed(runtime, 1, (entry,))
    source_id = registered[0][1]
    saved = CaptureRepository().save_source_file(SourceFileSave(41, source_id, _NOW), new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    if not check_required:
        owned.connection.execute("UPDATE recording_processing SET repair_state=3,repair_basis_json=? WHERE id=41",
            ('{"reason":2,"target_duration_ms":60000,"threshold_s":70000,"control_elapsed_ns":75000000000}',))
        owned.connection.commit()
    source = dual_probe()
    streams = list(source.streams)
    if fault == "short_video": streams[1] = replace(streams[1], duration_s=Decimal("55"))
    if fault in ("short_audio", "short_audio_unknown"):
        streams[2] = replace(streams[2], duration_s=Decimal("55"))
    if fault == "short_audio_unknown": streams[0] = replace(streams[0], attached_pic=None)
    if fault == "missing_video": streams.pop(1)
    if fault == "unknown": streams[1] = replace(streams[1], duration_s=None)
    source = replace(source, streams=tuple(streams))
    if fault in ("short_container", "short_container_unknown"):
        source = replace(source, duration_s=Decimal("55"))
        if fault == "short_container_unknown":
            source = replace(source, streams=tuple(replace(s, duration_s=None) if s.index == 2 else s
                for s in source.streams))
    if fault == "tool_failed": source = MediaProbe(None, "tool_failed: exit=1")
    if fault == "no_video_empty": source = MediaProbe(None, "no_video_stream: 容器没有视频流")
    tools = Tools(source)
    flow = MediaFlow(owned=owned, roots=BoundDirectories(staging=staging),
        sessions=DriverReadSessions(owned, ReadDriverDouble(_CONTENT), ticket=None), tools=tools,
        policy=MediaPolicy(Decimal("10")), occurred_at=lambda: _NOW + 100,
        digest_supported=True, digest=_Digest(_CONTENT))
    return owned, runtime, flow, source_id, source, tools, entry


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["short_video", "short_audio", "short_audio_unknown", "missing_video", "no_video_empty", "short_container", "short_container_unknown", "unknown", "tool_failed", "valid"])
async def test_running_repair_restart_keeps_original_probe_and_capture_failure_boundary(tmp_path, monkeypatch, fault):
    from camctl.persistence.runtime import open_existing, DbOpenMode, DbConfig
    owned, runtime, flow, source_id, source, tools, entry = prepared_input(tmp_path, False, fault)
    original = CaptureRepository.start_repair_output
    class ProcessStopped(Exception):
        pass
    def interrupted(repository, command, key, current):
        saved = original(repository, command, key, current)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        raise ProcessStopped()
    monkeypatch.setattr(CaptureRepository, "start_repair_output", interrupted)
    with pytest.raises(ProcessStopped):
        await run_recording_media(flow, 1, 41, source_id, target_extension="osv")
    monkeypatch.setattr(CaptureRepository, "start_repair_output", original)
    assert load_processing_status(owned, 41).repair_state == 4
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    owned.connection.close()
    reopened = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        flow.owned = reopened
        flow.sessions = DriverReadSessions(reopened, ReadDriverDouble(_CONTENT), ticket=None)
        probes = tools.probes
        await run_recording_media(flow, 1, 41, source_id, target_extension="osv")
        assert tools.probes == probes
        assert load_processing_status(reopened, 41).source_probe == source
        assert load_processing_status(reopened, 41).check_state == 1
        runtime = _runtime(reopened, {1: (entry,)})
        await capture_handler("camera_record")(1, runtime)
        known_failure = fault in ("short_video", "short_audio", "short_audio_unknown", "missing_video", "no_video_empty", "short_container", "short_container_unknown")
        assert reopened.connection.execute("SELECT status FROM actions WHERE id=1").fetchone()[0] == (4 if known_failure else 3)
        assert tools.repairs == (1 if fault == "valid" else 0)
    finally:
        reopened.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["short_video", "no_video_empty", "short_container_unknown"])
async def test_success_guard_independently_rejects_saved_input_failure_and_checks_plan_scope(tmp_path, fault):
    from camctl.history.reads import PlanReadScope
    from camctl.persistence.event_reads import SqlEventReads
    from camctl.contracts.values import ConsistencyError
    from camctl.outputs.catalog import FileReference, OutputCatalogFacts, OutputDraft, OutputKind
    from camctl.persistence.repositories.capture import FinishCapture
    owned, runtime, flow, source_id, source, tools, entry = prepared_input(tmp_path, False, fault)
    try:
        await run_recording_media(flow, 1, 41, source_id, target_extension="osv")
        # 完成申请本身仍请求成功，守卫须读取原输入而独立拒绝该提案。
        outcome = CaptureRepository().finish_capture(FinishCapture(1,
            (OutputDraft(OutputKind.ORIGINAL, FileReference(device_file_id=source_id), True),),
            OutputCatalogFacts(1, True), _NOW + 200), new_operation_key(), owned)
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone()[0] == 2
        owned.connection.execute("BEGIN IMMEDIATE")
        assert SqlEventReads(owned.connection, PlanReadScope(1, 128)).recording_input_issues(1)
        with pytest.raises(ConsistencyError):
            SqlEventReads(owned.connection, PlanReadScope(2, 128)).recording_input_issues(1)
        owned.connection.rollback()
    finally:
        owned.connection.close()

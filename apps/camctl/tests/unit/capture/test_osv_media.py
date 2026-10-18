"""OSV 异常处理必须根据原逐流事实保留双路内容。"""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.capture.media import (CheckContext, CheckExecutionPhase, RepairContext,
    RepairExecutionPhase, execute_check, execute_repair)
from camctl.capture.processing import CheckPhase, ProcessingDisposition, RepairOutputFile
from camctl.host_files.media import MediaProbe, MediaStream
from camctl.host_files.models import FilePurpose, FileRef
from camctl.host_files.tasks import FileTaskId, FileTaskResult
from .test_media_execution import _Saves, _status, _artifact, _saved, _POLICY, _NOW


def dual_probe(duration="75", *, output=False):
    return MediaProbe(Decimal(duration), None, (
        MediaStream(0, "video", "hevc", 3840, 3840, Decimal(duration), False),
        MediaStream(2, "video", "hevc", 3840, 3840, Decimal(duration), False),
        MediaStream(3, "audio", "aac", None, None, Decimal(duration), False),
        *(() if output else (MediaStream(1, "video", "mjpeg", 320, 180, None, True),)),
    ), "mov,mp4,m4a,3gp,3g2,mj2")


class Tools:
    def __init__(self, source, output=None, artifact=None):
        self.source, self.output = source, output
        self.artifact = artifact or _artifact(tool_error=None)
        self.probes, self.repairs = [], []

    async def probe(self, input):
        self.probes.append(input)
        value = self.output if input.purpose is FilePurpose.REPAIR_OUTPUT else self.source
        return FileTaskResult(FileTaskId("probe"), ran=True, value=value)

    async def repair(self, input, output, *, trim_s, stream_indices=None, output_format=None):
        self.repairs.append((trim_s, stream_indices, output_format))
        return FileTaskResult(FileTaskId("repair"), ran=True, value=self.artifact)


def input_ref():
    return FileRef(11, FilePurpose.RECORDING_INPUT, "recording-inputs/11.osv", Path("staging"))


def saves():
    return _Saves({"start_repair_output": _saved(RepairOutputFile(
        ProcessingDisposition.SAVED, 21, "derived/21.mp4"))})


@pytest.mark.asyncio
async def test_check_preserves_container_duration_and_reports_short_video():
    source = dual_probe()
    source = replace(source, streams=tuple(replace(s, duration_s=Decimal("55"))
        if s.index == 2 else s for s in source.streams))
    saved = saves()
    step = await execute_check(CheckContext(_status(), input_ref(), _POLICY,
        Tools(source), saved, _NOW))
    assert step.phase is CheckExecutionPhase.CHECK_COMPLETED
    assert step.media.duration_s == Decimal("75")
    assert step.media.issues[0].details == {
        "stream_index": 2, "kind": "video", "actual_duration_s": Decimal("55"),
        "target_duration_ms": 60000}
    assert step.repair_decision.decision.name == "NOT_NEEDED"
    assert step.repair_decision.basis.reason.name == "NO_USABLE_INPUT"
    assert saved.calls[1][1].source_probe == source


@pytest.mark.asyncio
async def test_osv_check_does_not_replace_missing_stream_duration_with_container_duration():
    source = dual_probe()
    source = replace(source, streams=tuple(replace(s, duration_s=None)
        if s.index == 2 else s for s in source.streams))
    step = await execute_check(CheckContext(_status(), input_ref(), _POLICY,
        Tools(source), saves(), _NOW))
    assert step.media.phase is CheckPhase.UNCONFIRMED


@pytest.mark.asyncio
async def test_osv_repair_freezes_input_selects_both_videos_and_audio_excluding_cover():
    source = dual_probe()
    tools, saved = Tools(source, dual_probe("60", output=True)), saves()
    step = await execute_repair(RepairContext(_status(check_decision=2, repair_state=3), input_ref(),
        "osv", tools, saved, _NOW))
    assert step.phase is RepairExecutionPhase.PREPARED
    assert tools.repairs == [] and len(tools.probes) == 1
    started = saved.calls[0][1]
    status = _status(repair_state=4, repair_output_file_id=21,
        repair_output_path="derived/21.mp4", source_probe=started.source_probe)
    step = await execute_repair(RepairContext(status, input_ref(), "osv", tools, saved, _NOW + 1))
    assert step.phase is RepairExecutionPhase.SUCCEEDED
    assert tools.repairs == [(Decimal("60"), (0, 2, 3), "mp4")]
    assert saved.calls[0][1].extension == "mp4"
    assert saved.calls[0][1].source_probe == source
    assert saved.calls[-1][1].output_probe == dual_probe("60", output=True)
    assert len(tools.probes) == 2


@pytest.mark.parametrize("fault", ["missing_video", "codec", "dimensions", "short_video",
    "short_audio", "missing_audio", "extra_video", "cover", "container", "probe_failed", "tool_failed"])
@pytest.mark.asyncio
async def test_osv_repair_rejects_unqualified_output_before_success(fault):
    output = dual_probe("60", output=True)
    streams = list(output.streams)
    if fault == "missing_video": streams.pop(1)
    if fault == "missing_audio": streams.pop(2)
    if fault == "codec": streams[1] = replace(streams[1], codec_name="h264")
    if fault == "dimensions": streams[1] = replace(streams[1], width=1920)
    if fault == "short_video": streams[1] = replace(streams[1], duration_s=Decimal("59.999"))
    if fault == "short_audio": streams[2] = replace(streams[2], duration_s=Decimal("59.999"))
    if fault == "extra_video": streams.append(replace(streams[1], index=4))
    if fault == "cover": streams.append(dual_probe().streams[3])
    output = replace(output, streams=tuple(streams))
    if fault == "container": output = replace(output, format_name="matroska,webm")
    if fault == "probe_failed": output = MediaProbe(None, "tool_failed: exit=1")
    artifact = _artifact(tool_error="tool_failed: exit=1" if fault == "tool_failed" else None)
    tools, saved = Tools(dual_probe(), output, artifact), saves()
    step = await execute_repair(RepairContext(_status(repair_state=4, repair_output_file_id=21,
        repair_output_path="derived/21.mp4", source_probe=dual_probe()), input_ref(),
        "osv", tools, saved, _NOW))
    assert step.phase is RepairExecutionPhase.FAILED_SAVED
    assert "complete_repair_output" not in saved.names()


@pytest.mark.asyncio
async def test_osv_running_repair_reuses_saved_input_and_does_not_reprobe_source():
    tools, saved = Tools(MediaProbe(None, "tool_failed"), dual_probe("60", output=True)), saves()
    status = _status(repair_state=4, repair_output_file_id=21,
        repair_output_path="derived/21.mp4", source_probe=dual_probe())
    step = await execute_repair(RepairContext(status, input_ref(), "osv", tools, saved, _NOW))
    assert step.phase is RepairExecutionPhase.SUCCEEDED
    assert [p.purpose for p in tools.probes] == [FilePurpose.REPAIR_OUTPUT]


@pytest.mark.parametrize("missing", ["source_probe", "video_duration", "audio_duration", "video_codec", "dimensions", "container", "video_count"])
@pytest.mark.asyncio
async def test_osv_unverifiable_original_is_not_replaced_or_sent_to_ffmpeg(missing):
    source = dual_probe()
    if missing == "source_probe": source = None
    if missing in ("video_duration", "audio_duration", "video_codec", "dimensions"):
        position = 2 if missing == "audio_duration" else 1
        fields = {"duration_s": None} if missing.endswith("duration") else (
            {"codec_name": None} if missing == "video_codec" else {"width": None})
        streams = list(source.streams)
        streams[position] = replace(streams[position], **fields)
        source = replace(source, streams=tuple(streams))
    if missing == "container": source = replace(source, format_name=None)
    if missing == "video_count": source = replace(source, streams=source.streams[1:])
    tools, saved = Tools(dual_probe(), dual_probe("60", output=True)), saves()
    status = _status(repair_state=4, repair_output_file_id=21,
        repair_output_path="derived/21.mp4", source_probe=source)
    step = await execute_repair(RepairContext(status, input_ref(), "osv", tools, saved, _NOW))
    assert step.phase is RepairExecutionPhase.FAILED_SAVED
    assert tools.probes == [] and tools.repairs == []
    assert saved.names() == ["save_repair_result"]


@pytest.mark.asyncio
async def test_osv_probe_tool_failure_remains_failed_check():
    probe = MediaProbe(None, "tool_failed: exit=1")
    step = await execute_check(CheckContext(_status(), input_ref(), _POLICY,
        Tools(probe), saves(), _NOW))
    assert step.media.phase is CheckPhase.FAILED


@pytest.mark.asyncio
async def test_osv_pending_repair_does_not_reprobe_missing_historical_check_facts():
    tools, saved = Tools(dual_probe(), dual_probe("60", output=True)), saves()
    status = _status(check_state=3, check_duration_s=Decimal("75"), repair_state=3)
    step = await execute_repair(RepairContext(status, input_ref(), "osv", tools, saved, _NOW))
    assert step.phase is RepairExecutionPhase.PREPARED
    assert saved.calls[0][1].source_probe is None
    status = replace(status, repair_state=4, repair_output_file_id=21,
        repair_output_path="derived/21.mp4")
    step = await execute_repair(RepairContext(status, input_ref(), "osv", tools, saved, _NOW + 1))
    assert step.phase is RepairExecutionPhase.FAILED_SAVED
    assert tools.probes == [] and tools.repairs == []


@pytest.mark.parametrize(("position", "index", "kind"), [(0, 0, "video"), (2, 3, "audio")])
@pytest.mark.asyncio
async def test_short_required_stream_keeps_container_fact_and_rejects_recording_success(position, index, kind):
    from camctl.capture.media import (RecordingOutcomeFacts, RecordingResultKind, decide_recording_result)
    source = dual_probe()
    streams = list(source.streams)
    streams[position] = replace(streams[position], duration_s=Decimal("59.999"))
    source = replace(source, streams=tuple(streams))
    saved, tools = saves(), Tools(source)
    step = await execute_check(CheckContext(_status(), input_ref(), _POLICY, tools, saved, _NOW))
    assert step.media.duration_s == Decimal("75")
    assert step.media.issues[0].details == {"stream_index": index, "kind": kind,
        "actual_duration_s": Decimal("59.999"), "target_duration_ms": 60000}
    assert step.repair_decision.decision.name == "NOT_NEEDED"
    assert step.repair_decision.basis.reason.name == "NO_USABLE_INPUT"
    result = decide_recording_result(RecordingOutcomeFacts(1, False, 3, 3,
        step.media.duration_s, bool(step.media.issues), False, 2, 60000))
    assert result.kind is RecordingResultKind.FAILED
    assert tools.repairs == []


@pytest.mark.asyncio
async def test_finished_check_recovery_keeps_saved_issues_and_refuses_repair():
    from camctl.capture.processing import ProcessingError
    issue = ProcessingError("stream_too_short", "probe", {
        "stream_index": 2, "kind": "video", "actual_duration_s": Decimal("55"),
        "target_duration_ms": 60000})
    status = _status(check_state=3, check_duration_s=Decimal("75"), check_issues=(issue,))
    tools, saved = Tools(None), saves()
    step = await execute_check(CheckContext(status, input_ref(), _POLICY, tools, saved, _NOW))
    assert step.media.duration_s == Decimal("75") and step.media.issues == (issue,)
    assert step.repair_decision.basis.reason.name == "NO_USABLE_INPUT"
    assert tools.probes == [] and tools.repairs == []


@pytest.mark.parametrize("check_state", [1, 2, 3, 4, 5])
def test_known_input_issue_prevents_control_success_in_every_check_phase(check_state):
    from camctl.capture.media import RecordingOutcomeFacts, RecordingResultKind, decide_recording_result
    result = decide_recording_result(RecordingOutcomeFacts(1, True, 2, check_state,
        Decimal("75") if check_state == 3 else None, True, False, 6, 60000))
    assert result.kind is RecordingResultKind.FAILED


@pytest.mark.parametrize("fault", ["short_video", "short_audio", "missing_video"])
@pytest.mark.asyncio
async def test_direct_timing_repair_preserves_known_input_failure_without_dispatch(fault):
    source = dual_probe()
    streams = list(source.streams)
    if fault == "missing_video": streams.pop(1)
    else:
        position = 2 if fault == "short_audio" else 1
        streams[position] = replace(streams[position], duration_s=Decimal("55"))
    source = replace(source, streams=tuple(streams))
    tools, saved = Tools(source, dual_probe("60", output=True)), saves()
    step = await execute_repair(RepairContext(_status(check_decision=2, repair_state=3),
        input_ref(), "osv", tools, saved, _NOW))
    assert step.phase is RepairExecutionPhase.PREPARED
    assert tools.repairs == []
    status = _status(repair_state=4, repair_output_file_id=21,
        repair_output_path="derived/21.mp4", source_probe=saved.calls[0][1].source_probe)
    step = await execute_repair(RepairContext(status, input_ref(), "osv", tools, saved, _NOW + 1))
    assert step.phase is RepairExecutionPhase.FAILED_SAVED
    assert saved.calls[0][1].source_probe == source
    assert tools.repairs == []


@pytest.mark.parametrize(("profile", "expected"), [
    (MediaProbe(None, "no_video_stream: 容器没有视频流"), ["required_video_stream_missing"]),
    (MediaProbe(Decimal("75"), None), []),
    (MediaProbe(Decimal("75"), None, (MediaStream(0, "audio", "aac", None, None, Decimal("75"), False),)), ["required_video_stream_missing"]),
    (MediaProbe(Decimal("75"), None, (MediaStream(0, "video", "mjpeg", 320, 180, None, True),)), ["required_video_stream_missing"]),
    (MediaProbe(Decimal("75"), None, (MediaStream(0, "video", "hevc", 3840, 3840, Decimal("75"), False),)), ["required_video_stream_missing"]),
    (MediaProbe(Decimal("75"), None, (MediaStream(0, "video", "hevc", 3840, 3840, Decimal("75"), None),)), []),
    (MediaProbe(Decimal("75"), None, (MediaStream(0, "video", "hevc", 3840, 3840, Decimal("75"), None),
        MediaStream(1, "audio", "aac", None, None, Decimal("55"), False))), ["stream_too_short"]),
    (MediaProbe(None, "tool_failed: exit=1"), []),
])
def test_original_failure_classifier_distinguishes_explicit_absence_from_unknown(profile, expected):
    from camctl.capture.media_facts import osv_input_issues
    assert [issue.code for issue in osv_input_issues(profile, 60000)] == expected


@pytest.mark.parametrize("control_complete", [False, True])
@pytest.mark.parametrize("phase", [1, 3, 5])
@pytest.mark.parametrize("unknown_stream", [False, True])
def test_reliable_short_container_is_independent_of_stream_duration_and_control(phase, control_complete, unknown_stream):
    from camctl.capture.media_facts import osv_input_issues
    from camctl.capture.media import RecordingOutcomeFacts, RecordingResultKind, decide_recording_result
    source = replace(dual_probe(), duration_s=Decimal("55"))
    if unknown_stream:
        source = replace(source, streams=tuple(replace(s, duration_s=None) if s.index == 2 else s
            for s in source.streams))
    issues = osv_input_issues(source, 60000)
    assert any(issue.code == "container_too_short" for issue in issues)
    result = decide_recording_result(RecordingOutcomeFacts(1, control_complete, 2, phase,
        source.duration_s if phase == 3 else None, bool(issues), False, 6, 60000))
    assert result.kind is RecordingResultKind.FAILED


@pytest.mark.parametrize("short_part", ["source_container", "output_container", "source_stream"])
def test_repair_qualification_checks_each_duration_fact_without_compensation(short_part):
    from camctl.capture.media_facts import osv_output_error
    source, output = dual_probe(), dual_probe("60", output=True)
    if short_part == "source_container": source = replace(source, duration_s=Decimal("55"))
    if short_part == "output_container": output = replace(output, duration_s=Decimal("55"))
    if short_part == "source_stream": source = replace(source, streams=(replace(source.streams[0], duration_s=Decimal("55")), *source.streams[1:]))
    assert osv_output_error(source, output, Decimal("60")) is not None


def test_recording_success_guard_requires_independent_scoped_reader():
    from types import SimpleNamespace
    from camctl.history.events import RowChange, RowImage
    from camctl.history.validators import EventValidationError
    from camctl.persistence.repositories.capture import _action_finish_guard
    event = SimpleNamespace(rows=(RowChange("actions", 1, RowImage(True, {"status": 2}),
        RowImage(True, {"status": 3})),))
    context = SimpleNamespace(current_reads=None,
        state_rows={"actions": {1: {"id": 1, "type": 2, "cancel_requested": 0}}})
    with pytest.raises(EventValidationError):
        _action_finish_guard(event, context)


@pytest.mark.asyncio
async def test_pending_repair_preserves_existing_probe_without_new_tool_call():
    source = dual_probe()
    tools, saved = Tools(MediaProbe(None, "tool_failed: later input")), saves()
    status = _status(check_decision=2, repair_state=3, source_probe=source)
    step = await execute_repair(RepairContext(status, input_ref(), "osv", tools, saved, _NOW))
    assert step.phase is RepairExecutionPhase.PREPARED
    assert saved.calls[0][1].source_probe is source
    assert tools.probes == [] and tools.repairs == []

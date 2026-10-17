"""F6 受管媒体工具执行的单元测试。

工具退出与成品校验分别确认：工具失败但文件存在不是完成；时长保
持全精度不舍入；非法结构与读取错误单独分类；媒体调用经 O3 受管
执行。工具执行与文件检查经窄注入点替身。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, is_dataclass
from decimal import Decimal
from pathlib import Path
from typing import get_type_hints

import pytest

import camctl.host_files.media as media
from camctl.host_files.io import DirectorySyncStage, HashResult, SyncResult
from camctl.host_files.media import (
    MediaArtifact,
    MediaProbe,
    ProbeRequest,
    RepairRequest,
)
from media_helpers import probe_media, repair_media
from camctl.host_files.models import (
    BoundDirectories, FileObservation, FileObservationKind, FilePurpose, FileRef,
)
from camctl.operations.process import LocalExit, RawToolOutcome, SignalFailure, SignalStage, ToolStartError

pytestmark = pytest.mark.asyncio

_ROOT = Path("D:\\state")


class NeverStop:
    async def requested(self) -> None:
        import asyncio

        await asyncio.Future()


class FakeMedia:
    """窄注入点替身：脚本化工具结果与文件事实。"""

    def __init__(self) -> None:
        self.outcome = RawToolOutcome(
            exit=LocalExit(exit_code=0), output=b"{}", error=None, used_grace_s=None
        )
        self.fail_spawn: Exception | None = None
        self.argvs: list[tuple[str, ...]] = []
        self.output_exists = False
        self.output_size = 0
        self.observe_error: OSError | None = None
        self.is_file = True
        self.digest = HashResult(digest="a" * 64, size_bytes=1, error=None)
        self.sync = SyncResult(
            file_synced=True, directory=DirectorySyncStage.UNSUPPORTED, error=None
        )

    async def execute(self, spec, *, stop, spawner=None):
        self.argvs.append(spec.argv)
        if self.fail_spawn is not None:
            raise self.fail_spawn
        return self.outcome

    def observe(self, ref: FileRef, roots: BoundDirectories) -> FileObservation:
        path = roots.staging / ref.relative_path
        if self.observe_error is not None:
            return FileObservation(FileObservationKind.ERROR, path, error=self.observe_error)
        if not self.output_exists:
            return FileObservation(FileObservationKind.MISSING, path)
        return FileObservation(
            FileObservationKind.VALID_OBJECT if self.is_file else FileObservationKind.TYPE_MISMATCH,
            path, is_file=self.is_file, size_bytes=self.output_size if self.is_file else None,
        )

    def hash_target(self, ref, roots):
        return self.digest

    def sync_target(self, ref, roots):
        return self.sync


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeMedia:
    fm = FakeMedia()
    monkeypatch.setattr(media, "_execute_tool", fm.execute)
    monkeypatch.setattr(media, "_file_digest", fm.hash_target)
    monkeypatch.setattr(media, "_file_sync", fm.sync_target)
    monkeypatch.setattr(media, "observe_file", fm.observe)
    return fm


def _input_ref() -> FileRef:
    return FileRef(
        file_id=11,
        purpose=FilePurpose.RECORDING_INPUT,
        relative_path="recording-inputs/11.mp4",
        root=_ROOT / "staging",
    )


def _output_ref() -> FileRef:
    return FileRef(
        file_id=12,
        purpose=FilePurpose.REPAIR_OUTPUT,
        relative_path="derived/12.mp4",
        root=_ROOT / "staging",
    )


def _roots() -> BoundDirectories:
    return BoundDirectories(staging=_ROOT / "staging")


def _repair_request(
    *,
    ffmpeg: str = "ffmpeg",
    output_args: tuple[str, ...] = (),
    stream_indices=None,
    output_format=None,
) -> RepairRequest:
    return RepairRequest(
        ffmpeg=ffmpeg,
        output_args=output_args,
        stream_indices=stream_indices,
        output_format=output_format,
    )


def _stream_fact_values(probe: MediaProbe) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(getattr(stream, name, None) for name in (
            "index", "codec_type", "codec_name", "width", "height",
            "duration_s", "attached_pic",
        ))
        for stream in getattr(probe, "streams", ())
    )


async def test_failed_media_output_is_not_complete(fake: FakeMedia) -> None:
    """工具失败但输出文件存在：不是完成，部分成品不作正式产物。"""
    fake.outcome = RawToolOutcome(
        exit=LocalExit(exit_code=1), output=b"", error=None, used_grace_s=None
    )
    fake.output_exists = True
    fake.output_size = 1024
    artifact = await repair_media(
        _input_ref(), _output_ref(), _roots(), RepairRequest()
    )
    assert isinstance(artifact, MediaArtifact)
    assert artifact.exists is True
    assert artifact.complete is False
    assert artifact.error is not None
    assert artifact.error.startswith("tool_failed")


async def test_successful_repair_collects_full_evidence(fake: FakeMedia) -> None:
    fake.output_exists = True
    fake.output_size = 2048
    fake.digest = HashResult(digest="a" * 64, size_bytes=2048, error=None)
    artifact = await repair_media(
        _input_ref(), _output_ref(), _roots(),
        RepairRequest(output_args=("-c", "copy")),
    )
    assert artifact.complete is True
    assert artifact.exists is True
    assert artifact.size_bytes == 2048
    assert artifact.digest == "a" * 64
    assert artifact.file_synced is True
    assert artifact.error is None
    argv = fake.argvs[0]
    assert argv[0] == "ffmpeg"
    assert "-nostdin" in argv and "-y" in argv
    assert argv.index("-c") < argv.index(str(_ROOT / "staging" / "derived" / "12.mp4"))


async def test_zero_exit_but_missing_output(fake: FakeMedia) -> None:
    fake.output_exists = False
    artifact = await repair_media(
        _input_ref(), _output_ref(), _roots(), RepairRequest()
    )
    assert artifact.complete is False
    assert artifact.exists is False
    assert artifact.error is not None
    assert artifact.error.startswith("output_missing")


async def test_zero_exit_but_empty_output(fake: FakeMedia) -> None:
    fake.output_exists = True
    fake.output_size = 0
    artifact = await repair_media(
        _input_ref(), _output_ref(), _roots(), RepairRequest()
    )
    assert artifact.complete is False
    assert artifact.error is not None
    assert artifact.error.startswith("output_empty")


async def test_hash_failure_keeps_exit_and_sync_evidence(fake: FakeMedia) -> None:
    fake.output_exists = True
    fake.output_size = 10
    fake.digest = HashResult(digest=None, size_bytes=None, error="read_failed: x")
    artifact = await repair_media(
        _input_ref(), _output_ref(), _roots(), RepairRequest()
    )
    assert artifact.complete is False
    assert artifact.error is not None
    assert artifact.error.startswith("hash_failed")
    assert artifact.file_synced is True


async def test_probe_duration_keeps_full_precision(fake: FakeMedia) -> None:
    fake.outcome = RawToolOutcome(
        exit=LocalExit(exit_code=0),
        output=(b'{"streams": [{"codec_type": "video"}],'
                b' "format": {"duration": "3.016999"}}'),
        error=None,
        used_grace_s=None,
    )
    probe = await probe_media(_input_ref(), _roots(), ProbeRequest())
    assert isinstance(probe, MediaProbe)
    assert probe.error is None
    assert probe.duration_s == Decimal("3.016999")
    # 全精度保留：不舍入到目标毫秒。
    assert probe.duration_s != Decimal("3.017")


async def test_media_probe_keeps_its_two_positional_arguments() -> None:
    probe = MediaProbe(Decimal("1.25"), "legacy error")

    assert probe.duration_s == Decimal("1.25")
    assert probe.error == "legacy error"
    assert getattr(probe, "streams", ()) == ()
    assert getattr(probe, "format_name", None) is None


async def test_repair_request_keeps_legacy_fields_and_adds_optional_selection() -> None:
    request = RepairRequest("ffmpeg-custom", ("-c", "copy"))

    assert request.ffmpeg == "ffmpeg-custom"
    assert request.output_args == ("-c", "copy")
    assert tuple(field.name for field in fields(RepairRequest)) == (
        "ffmpeg", "output_args", "stream_indices", "output_format",
    )
    hints = get_type_hints(RepairRequest)
    assert hints["stream_indices"] == tuple[int, ...] | None
    assert hints["output_format"] == str | None
    assert getattr(request, "stream_indices", object()) is None
    assert getattr(request, "output_format", object()) is None


async def test_media_stream_is_an_exported_frozen_fact_type() -> None:
    stream_type = getattr(media, "MediaStream", None)

    assert stream_type is not None
    assert is_dataclass(stream_type)
    assert "MediaStream" in media.__all__
    assert tuple(field.name for field in fields(stream_type)) == (
        "index", "codec_type", "codec_name", "width", "height",
        "duration_s", "attached_pic",
    )
    hints = get_type_hints(stream_type)
    assert hints == {
        "index": int | None,
        "codec_type": str,
        "codec_name": str | None,
        "width": int | None,
        "height": int | None,
        "duration_s": Decimal | None,
        "attached_pic": bool | None,
    }
    stream = stream_type(0, "video", "h264", 32, 24, Decimal("1"), False)
    with pytest.raises(FrozenInstanceError):
        stream.index = 1


async def test_probe_parses_two_videos_audio_and_attached_cover(fake: FakeMedia) -> None:
    fake.outcome = RawToolOutcome(
        exit=LocalExit(exit_code=0),
        output=(
            b'{"format":{"duration":"6.2500000000000001",'
            b'"format_name":"mov,mp4,m4a,3gp,3g2,mj2"},"streams":['
            b'{"index":0,"codec_type":"video","codec_name":"h264",'
            b'"width":3840,"height":1920,"duration":"6.25",'
            b'"disposition":{"attached_pic":0}},'
            b'{"index":1,"codec_type":"video","codec_name":"hevc",'
            b'"width":1920,"height":960,"duration":"6.25000000000000001",'
            b'"disposition":{"attached_pic":0}},'
            b'{"index":2,"codec_type":"audio","codec_name":"aac",'
            b'"duration":"6.25","disposition":{"attached_pic":0}},'
            b'{"index":3,"codec_type":"video","codec_name":"mjpeg",'
            b'"width":320,"height":180,"duration":"0.04",'
            b'"disposition":{"attached_pic":1}}]}'
        ),
        error=None,
        used_grace_s=None,
    )

    probe = await probe_media(_input_ref(), _roots(), ProbeRequest())

    assert probe.error is None
    assert probe.duration_s == Decimal("6.2500000000000001")
    assert getattr(probe, "format_name", None) == "mov,mp4,m4a,3gp,3g2,mj2"
    assert _stream_fact_values(probe) == (
        (0, "video", "h264", 3840, 1920, Decimal("6.25"), False),
        (1, "video", "hevc", 1920, 960, Decimal("6.25000000000000001"), False),
        (2, "audio", "aac", None, None, Decimal("6.25"), False),
        (3, "video", "mjpeg", 320, 180, Decimal("0.04"), True),
    )


async def test_probe_keeps_new_facts_unknown_for_legacy_stream_json(
    fake: FakeMedia,
) -> None:
    fake.outcome = RawToolOutcome(
        exit=LocalExit(exit_code=0),
        output=(b'{"streams":[{"codec_type":"video"}],'
                b'"format":{"duration":"6"}}'),
        error=None,
        used_grace_s=None,
    )

    probe = await probe_media(_input_ref(), _roots(), ProbeRequest())

    assert probe.error is None
    assert probe.duration_s == Decimal("6")
    assert getattr(probe, "format_name", None) is None
    assert _stream_fact_values(probe) == (
        (None, "video", None, None, None, None, None),
    )


async def test_probe_keeps_missing_attached_pic_unknown(fake: FakeMedia) -> None:
    fake.outcome = RawToolOutcome(
        exit=LocalExit(exit_code=0),
        output=(b'{"streams":[{"index":4,"codec_type":"video",'
                b'"disposition":{}}],"format":{"duration":"1"}}'),
        error=None,
        used_grace_s=None,
    )

    probe = await probe_media(_input_ref(), _roots(), ProbeRequest())

    assert probe.error is None
    assert _stream_fact_values(probe) == (
        (4, "video", None, None, None, None, None),
    )


@pytest.mark.parametrize(
    "streams",
    [
        '[{"index":true,"codec_type":"video"}]',
        '[{"index":null,"codec_type":"video"}]',
        '[{"index":-1,"codec_type":"video"}]',
        '[{"index":0.0,"codec_type":"video"}]',
        '[{"codec_type":"video","width":true}]',
        '[{"codec_type":"video","width":null}]',
        '[{"codec_type":"video","height":0}]',
        '[{"codec_type":"video","height":-2}]',
        '[{"codec_type":"video","codec_name":false}]',
        '[{"codec_type":"video","duration":"NaN"}]',
        '[{"codec_type":"video","duration":"-0.1"}]',
        '[{"codec_type":"video","duration":true}]',
        '[{"codec_type":"video","disposition":null}]',
        '[{"codec_type":"video","disposition":{"attached_pic":2}}]',
        '[{"codec_type":"video","disposition":{"attached_pic":true}}]',
        '[{"codec_type":"video","disposition":{"attached_pic":null}}]',
        '[{"index":0,"codec_type":"video"},'
        '{"index":0,"codec_type":"audio"}]',
        '[null]',
    ],
)
async def test_probe_rejects_present_illegal_stream_facts(
    fake: FakeMedia, streams: str
) -> None:
    fake.outcome = RawToolOutcome(
        exit=LocalExit(exit_code=0),
        output=(f'{{"streams":{streams},"format":{{"duration":"1"}}}}'
                ).encode(),
        error=None,
        used_grace_s=None,
    )

    probe = await probe_media(_input_ref(), _roots(), ProbeRequest())

    assert probe.duration_s is None
    assert probe.streams == ()
    assert probe.error is not None
    assert probe.error.startswith("invalid_structure:")


async def test_probe_rejects_non_string_present_format_name(fake: FakeMedia) -> None:
    fake.outcome = RawToolOutcome(
        exit=LocalExit(exit_code=0),
        output=(b'{"streams":[{"codec_type":"video"}],'
                b'"format":{"duration":"1","format_name":false}}'),
        error=None,
        used_grace_s=None,
    )

    probe = await probe_media(_input_ref(), _roots(), ProbeRequest())

    assert probe.duration_s is None
    assert probe.error is not None
    assert probe.error.startswith("invalid_structure:")


async def test_probe_accepts_video_only_container_without_audio(
        fake: FakeMedia) -> None:
    """容器时长作为视频时长事实：没有音频流不影响时长取得。"""
    fake.outcome = RawToolOutcome(
        exit=LocalExit(exit_code=0),
        output=(b'{"streams": [{"codec_type": "video"}],'
                b' "format": {"duration": "6"}}'),
        error=None,
        used_grace_s=None,
    )
    probe = await probe_media(_input_ref(), _roots(), ProbeRequest())
    assert probe.error is None
    assert probe.duration_s == Decimal("6")


@pytest.mark.parametrize(
    "streams",
    [
        '[{"codec_type": "audio"}]',
        '[{"codec_type": "audio"}, {"codec_type": "data"}]',
        '[]',
        '[{"codec_type": "subtitle"}]',
    ],
)
async def test_probe_without_video_stream_is_not_video_duration(
        fake: FakeMedia, streams: str) -> None:
    """无视频流的容器不提供视频时长事实，明确分类不补造。"""
    fake.outcome = RawToolOutcome(
        exit=LocalExit(exit_code=0),
        output=(f'{{"streams": {streams}, "format": {{"duration": "3.5"}}}}'
                ).encode(),
        error=None,
        used_grace_s=None,
    )
    probe = await probe_media(_input_ref(), _roots(), ProbeRequest())
    assert probe.duration_s is None
    assert probe.error is not None
    assert probe.error.startswith("no_video_stream")


@pytest.mark.parametrize(
    "payload",
    [
        b'{"format": {"duration": "3.5"}}',
        b'{"streams": "video", "format": {"duration": "3.5"}}',
        b'{"streams": {}, "format": {"duration": "3.5"}}',
    ],
)
async def test_probe_missing_stream_section_is_invalid_structure(
        fake: FakeMedia, payload: bytes) -> None:
    """工具输出缺少流信息段：结构不符，不解释为有视频。"""
    fake.outcome = RawToolOutcome(
        exit=LocalExit(exit_code=0), output=payload, error=None,
        used_grace_s=None,
    )
    probe = await probe_media(_input_ref(), _roots(), ProbeRequest())
    assert probe.duration_s is None
    assert probe.error is not None
    assert probe.error.startswith("invalid_structure")


async def test_probe_invalid_structure(fake: FakeMedia) -> None:
    fake.outcome = RawToolOutcome(
        exit=LocalExit(exit_code=0), output=b"not json", error=None, used_grace_s=None
    )
    probe = await probe_media(_input_ref(), _roots(), ProbeRequest())
    assert probe.duration_s is None
    assert probe.error is not None
    assert probe.error.startswith("invalid_structure")


async def test_probe_missing_duration(fake: FakeMedia) -> None:
    fake.outcome = RawToolOutcome(
        exit=LocalExit(exit_code=0),
        output=b'{"format": {}}',
        error=None,
        used_grace_s=None,
    )
    probe = await probe_media(_input_ref(), _roots(), ProbeRequest())
    assert probe.duration_s is None
    assert probe.error is not None
    assert probe.error.startswith("missing_duration")


async def test_probe_tool_failed(fake: FakeMedia) -> None:
    fake.outcome = RawToolOutcome(
        exit=LocalExit(signal=1), output=b"", error=None, used_grace_s=None
    )
    probe = await probe_media(_input_ref(), _roots(), ProbeRequest())
    assert probe.duration_s is None
    assert probe.error is not None
    assert probe.error.startswith("tool_failed")


async def test_tool_unavailable(fake: FakeMedia) -> None:
    fake.fail_spawn = ToolStartError("no ffprobe")
    probe = await probe_media(_input_ref(), _roots(), ProbeRequest())
    assert probe.error is not None
    assert probe.error.startswith("tool_unavailable")
    fake.output_exists = True
    artifact = await repair_media(
        _input_ref(), _output_ref(), _roots(), RepairRequest()
    )
    assert artifact.complete is False
    assert artifact.error is not None
    assert artifact.error.startswith("tool_unavailable")


@pytest.mark.parametrize("entry", ["probe", "repair"])
async def test_signal_failure_keeps_actual_exit_and_reason(fake, entry):
    fake.outcome = RawToolOutcome(
        LocalExit(exit_code=7), b"", "cancelled", Decimal("1"),
        (SignalFailure(SignalStage.TERMINATE, "PermissionError: denied"),),
        output_failure="OSError: pipe read failed",
    )
    if entry == "probe":
        result = await probe_media(_input_ref(), _roots(), ProbeRequest())
    else:
        result = await repair_media(_input_ref(), _output_ref(), _roots(), RepairRequest())
    assert "exit=7" in result.error
    assert "cancelled" in result.error
    assert "terminate" in result.error and "denied" in result.error
    assert "pipe read failed" in result.error
    assert "tool_unavailable" not in result.error


@pytest.mark.parametrize("entry", ["probe", "repair"])
async def test_stop_before_tool_start_preserves_no_dispatch(fake, entry):
    import asyncio
    from camctl.host_files.tasks import FileTaskExecutor, FileTaskId
    from camctl.session.supervision import Supervisor

    class Owner:
        async def take_over(self, task):
            raise AssertionError("正常领取不应接手")

    executor, identity = FileTaskExecutor(Supervisor()), FileTaskId("before-tool")
    kwargs = dict(executor=executor, task_id=identity, owner=Owner())
    operation = (media.probe_media(_input_ref(), _roots(), ProbeRequest(), **kwargs)
                 if entry == "probe" else media.repair_media(
                     _input_ref(), _output_ref(), _roots(), RepairRequest(), **kwargs))
    waiting = asyncio.create_task(operation)
    await asyncio.sleep(0)
    executor.request_stop(identity)
    result = await waiting
    assert result.error is None and "tool_cancelled" in result.value.error
    assert fake.argvs == []
    assert executor.unfinished_files() == ()


async def test_repair_rejects_same_file_before_starting_tool(fake):
    from camctl.host_files.tasks import FileTaskError

    with pytest.raises(FileTaskError):
        await repair_media(_input_ref(), _input_ref(), _roots(), RepairRequest())
    assert fake.argvs == []


async def test_probe_argv_uses_managed_prefix(fake: FakeMedia) -> None:
    await probe_media(
        _input_ref(), _roots(), ProbeRequest(ffprobe="C:\\tools\\ffprobe.exe")
    )
    argv = fake.argvs[0]
    assert argv[0] == "C:\\tools\\ffprobe.exe"
    assert "-of" in argv and "json" in argv
    assert argv[argv.index("-show_entries") + 1] == (
        "format=duration,format_name:stream=index,codec_type,codec_name,width,height,duration:"
        "stream_disposition=attached_pic"
    )
    assert argv[-1] == str(_ROOT / "staging" / "recording-inputs" / "11.mp4")


async def test_repair_maps_requested_streams_and_selects_output_format(
    fake: FakeMedia,
) -> None:
    fake.output_exists = True
    fake.output_size = 1

    await repair_media(
        _input_ref(), _output_ref(), _roots(),
        _repair_request(
            output_args=("-c", "copy"),
            stream_indices=(2, 0),
            output_format="matroska",
        ),
    )

    argv = fake.argvs[0]
    input_index = argv.index(str(_ROOT / "staging" / "recording-inputs" / "11.mp4"))
    assert argv[input_index + 1:input_index + 5] == (
        "-map", "0:2", "-map", "0:0",
    )
    assert argv[input_index + 5:input_index + 7] == ("-c", "copy")
    assert argv[-3:] == ("-f", "matroska", str(_ROOT / "staging" / "derived" / "12.mp4"))


async def test_repair_without_explicit_mapping_keeps_legacy_arguments(
    fake: FakeMedia,
) -> None:
    fake.output_exists = True
    fake.output_size = 1

    await repair_media(
        _input_ref(), _output_ref(), _roots(),
        RepairRequest(output_args=("-c", "copy")),
    )

    argv = fake.argvs[0]
    assert "-map" not in argv
    assert "-f" not in argv
    assert argv[-3:] == (
        "-c", "copy", str(_ROOT / "staging" / "derived" / "12.mp4")
    )


@pytest.mark.parametrize(
    "invalid_request",
    [
        _repair_request(stream_indices=()),
        _repair_request(stream_indices=(0, 0)),
        _repair_request(stream_indices=(-1,)),
        _repair_request(stream_indices=(True,)),
        _repair_request(stream_indices=(1.5,)),
        _repair_request(stream_indices=[]),
        _repair_request(output_format=""),
        _repair_request(output_format="  mp4  "),
        _repair_request(output_format="-f"),
        _repair_request(output_format="mp4;exit"),
        _repair_request(output_format="../mp4"),
        _repair_request(output_format=False),
        _repair_request(output_format=0),
    ],
)
async def test_repair_rejects_invalid_stream_selection_before_tool_call(
    fake: FakeMedia, invalid_request: RepairRequest
) -> None:
    with pytest.raises(ValueError):
        await repair_media(_input_ref(), _output_ref(), _roots(), invalid_request)

    assert fake.argvs == []


@pytest.mark.parametrize("entry", ["probe", "repair"])
@pytest.mark.parametrize("reason", [None, "cancelled", "timeout"])
@pytest.mark.parametrize(
    "exit_value", [LocalExit(exit_code=0), LocalExit(exit_code=1), LocalExit(signal=15), None]
)
async def test_tool_completion_requires_exit_and_no_call_error(
    fake: FakeMedia, entry: str, reason: str | None, exit_value: LocalExit | None
) -> None:
    fake.outcome = RawToolOutcome(
        exit=exit_value,
        output=(b'{"streams": [{"codec_type": "video"}],'
                b' "format":{"duration":"1.25"}}'),
        error=reason,
        used_grace_s=Decimal("5") if reason else None,
    )
    fake.output_exists = True
    fake.output_size = 1
    if entry == "probe":
        result = await probe_media(_input_ref(), _roots(), ProbeRequest())
        succeeded = result.duration_s is not None
    else:
        result = await repair_media(_input_ref(), _output_ref(), _roots(), RepairRequest())
        succeeded = result.complete
        assert result.exists is True

    if reason is None and exit_value == LocalExit(exit_code=0):
        assert succeeded is True
        assert result.error is None
    else:
        assert succeeded is False
        assert result.error is not None
        assert result.error.startswith("tool_failed:")
        if reason:
            assert reason in result.error
        if exit_value is None:
            assert "no-exit" in result.error
        elif exit_value.signal is not None:
            assert f"signal={exit_value.signal}" in result.error
        else:
            assert f"exit={exit_value.exit_code}" in result.error


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ('"3.01699900000000000000000000001"', "3.01699900000000000000000000001"),
        ("3.01699900000000000000000000001", "3.01699900000000000000000000001"),
        ("9007199254740993", "9007199254740993"),
        ("1.00000000000000000000000000001e-3", "0.00100000000000000000000000000001"),
        ("0", "0"),
        ('"0.000000"', "0"),
    ],
)
async def test_probe_numeric_representations_are_exact(
    fake: FakeMedia, literal: str, expected: str
) -> None:
    fake.outcome = RawToolOutcome(
        exit=LocalExit(exit_code=0),
        output=('{"streams": [{"codec_type": "video"}],'
                '"format":{"duration":' + literal + '}}').encode(),
        error=None,
        used_grace_s=None,
    )
    result = await probe_media(_input_ref(), _roots(), ProbeRequest())
    assert result.error is None
    assert result.duration_s == Decimal(expected)


@pytest.mark.parametrize(
    "literal",
    [
        "-0.001", '"-1e-100"', '"NaN"', '"sNaN"', '"Infinity"', '"-Infinity"',
        "NaN", "Infinity", "-Infinity", "true", "false", "null", "[]", "{}",
        '"N/A"', '""',
    ],
)
async def test_probe_invalid_duration_is_unknown(fake: FakeMedia, literal: str) -> None:
    fake.outcome = RawToolOutcome(
        exit=LocalExit(exit_code=0),
        output=('{"format":{"duration":' + literal + '}}').encode(),
        error=None,
        used_grace_s=None,
    )
    result = await probe_media(_input_ref(), _roots(), ProbeRequest())
    assert result.duration_s is None
    assert result.error is not None
    assert result.error.startswith("invalid_structure:")


@pytest.mark.parametrize(
    "payload",
    [
        b'{"format":{"duration":1,"duration":2}}',
        b'{"format":{"duration":1},"format":{"duration":2}}',
        b'{"format":{"duration":"\xff"}}',
        b'{"format":{"duration":1},"other":"\\ud800"}',
        b'[]', b'{"format":null}', b'{"format":[]}',
    ],
)
async def test_probe_malformed_output_is_not_partially_used(
    fake: FakeMedia, payload: bytes
) -> None:
    fake.outcome = RawToolOutcome(
        exit=LocalExit(exit_code=0), output=payload, error=None, used_grace_s=None
    )
    result = await probe_media(_input_ref(), _roots(), ProbeRequest())
    assert result.duration_s is None
    assert result.error is not None
    assert result.error.startswith("invalid_structure:")


@pytest.mark.parametrize("hash_fails", [False, True])
@pytest.mark.parametrize(
    "synced",
    [
        SyncResult(False, DirectorySyncStage.NOT_ATTEMPTED, "fsync_failed: disk"),
        SyncResult(True, DirectorySyncStage.FAILED, "directory_sync_failed: disk"),
        SyncResult(True, DirectorySyncStage.SYNCED, "directory_close_failed: disk"),
    ],
)
async def test_repair_keeps_sync_error_and_independent_hash(
    fake: FakeMedia, synced: SyncResult, hash_fails: bool
) -> None:
    fake.output_exists = True
    fake.output_size = 1
    fake.sync = synced
    if hash_fails:
        fake.digest = HashResult(None, None, "read_failed: disk")
    result = await repair_media(_input_ref(), _output_ref(), _roots(), RepairRequest())
    assert result.complete is (not hash_fails)
    assert result.size_bytes == 1
    assert result.file_synced is synced.file_synced
    assert result.directory is synced.directory
    assert result.error is not None and synced.error in result.error
    if hash_fails:
        assert "hash_failed" in result.error


@pytest.mark.parametrize("tool_fails", [False, True])
async def test_output_inspection_error_preserves_unknown_and_tool_error(
    fake: FakeMedia, tool_fails: bool
) -> None:
    fake.observe_error = PermissionError("cannot inspect output")
    if tool_fails:
        fake.outcome = RawToolOutcome(LocalExit(exit_code=1), b"", None, None)
    result = await repair_media(_input_ref(), _output_ref(), _roots(), RepairRequest())
    assert result.exists is None
    assert result.complete is False
    assert result.error is not None and "inspect_failed" in result.error
    if tool_fails:
        assert "tool_failed" in result.error


async def test_non_file_output_does_not_qualify(fake: FakeMedia) -> None:
    fake.output_exists = True
    fake.output_size = 1
    fake.is_file = False
    result = await repair_media(_input_ref(), _output_ref(), _roots(), RepairRequest())
    assert result.exists is True
    assert result.complete is False
    assert result.error is not None and "output_type_mismatch" in result.error


async def test_hash_length_conflict_is_not_complete(fake: FakeMedia) -> None:
    fake.output_exists = True
    fake.output_size = 2
    result = await repair_media(_input_ref(), _output_ref(), _roots(), RepairRequest())
    assert result.size_bytes == 2
    assert result.complete is False
    assert result.error is not None and "size_mismatch" in result.error


async def test_hash_close_failure_preserves_computed_digest(fake: FakeMedia) -> None:
    fake.output_exists = True
    fake.output_size = 1
    fake.digest = HashResult("a" * 64, 1, "close_failed: disk")
    result = await repair_media(_input_ref(), _output_ref(), _roots(), RepairRequest())
    assert result.complete is False
    assert result.digest == "a" * 64
    assert result.file_synced is True
    assert result.error is not None and "close_failed" in result.error


async def test_hash_close_and_size_conflict_are_both_visible(fake: FakeMedia) -> None:
    fake.output_exists = True
    fake.output_size = 2
    fake.digest = HashResult("a" * 64, 1, "close_failed: disk")
    result = await repair_media(_input_ref(), _output_ref(), _roots(), RepairRequest())
    assert result.complete is False
    assert "close_failed" in result.error
    assert "size_mismatch" in result.error

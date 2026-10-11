"""软件相机的本机文件工具保持目标响应与原始媒体字节。"""

import asyncio
from dataclasses import replace
from decimal import Decimal
import hashlib
import json

import pytest

from camera_demo_fixtures import CameraWorld, WorldTransport
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryRequest
from camctl.devices.drivers.adb_cameras.contracts import CameraCall
from camctl.devices.drivers.adb_cameras.filesystem import AdbFilesystem, ShellFileTools
from camctl.devices.drivers.adb_cameras.transport import AdbTransport, shell_argv
from camctl.devices.file_identity import FileIdentity
from camctl.devices.ports import ControlRequest
from camctl.devices.read_session import SourceFile
from camctl.operations.models import AttemptTicket
from camctl.operations.process import LocalExit, RawToolOutcome, SignalFailure, SignalStage, ToolSpec


pytestmark = pytest.mark.asyncio
SERIAL = "camera-serial"
BINDING = DeviceBinding("camera", "action6")
REMOTE = "/mnt/media_rw/emulated/DCIM/clip 'quoted' name.mp4"
HEX = b"0123456789abcdef" * 4
BINARY_RESPONSE = HEX + b" *-\n"
POSIX_RESPONSE = HEX + b"  -\n"
TOOLS = ShellFileTools()


class NeverStop:
    async def requested(self):
        await asyncio.Future()


@pytest.fixture
def world(tmp_path):
    clock = tmp_path / "clock"
    clock.write_text("0")
    gates = tmp_path / "gates"
    gates.mkdir()
    return CameraWorld({"remote_root": str(tmp_path / "remote"),
        "clock_path": str(clock), "gate_dir": str(gates),
        "devices": {SERIAL: {"device_id": "camera", "driver_id": "action6"}}})


def _spec(script):
    return ToolSpec(shell_argv(SERIAL, script), Decimal("10"), Decimal("1"))


def _returned(output=BINARY_RESPONSE, **changes):
    return replace(RawToolOutcome(LocalExit(exit_code=0), output, None, None, stderr=b""), **changes)


def _supply_tool_result(monkeypatch, raw):
    # 实际启动、读取、退出和失败均由这个稳定传输接口交付。
    async def run(self, spec, stop):
        return raw
    monkeypatch.setattr(AdbTransport, "run", run)


@pytest.mark.parametrize("stderr", [b"", b"tool diagnostic\n", None], ids=["empty", "diagnostic", "unknown"])
@pytest.mark.parametrize("remote", [REMOTE, "/mnt/media_rw/emulated/DCIM/plain.mp4"])
async def test_run_normalizes_only_digest_marker_and_preserves_tool_facts(world, monkeypatch, stderr, remote):
    raw = _returned(stderr=stderr, used_grace_s=Decimal("1"),
                    signal_failures=(SignalFailure(SignalStage.TERMINATE, "signal diagnostic"),))
    _supply_tool_result(monkeypatch, raw)
    result = await WorldTransport(world).run(_spec(TOOLS.digest_script(remote)), NeverStop())
    assert result == replace(raw, output=POSIX_RESPONSE)


@pytest.mark.parametrize("raw", [
    _returned(POSIX_RESPONSE),
    _returned(HEX.upper() + b" *-\n"),
    _returned(b"g" + HEX[1:] + b" *-\n"),
    _returned(HEX[:-1] + b" *-\n"),
    _returned(HEX + b"0 *-\n"),
    _returned(HEX + b" *-\r\n"),
    _returned(HEX + b" *-"),
    _returned(HEX + b" *file.mp4\n"),
    _returned(BINARY_RESPONSE + b"extra"),
    _returned(BINARY_RESPONSE + BINARY_RESPONSE),
    _returned(b""),
    _returned(None),
    _returned(exit=LocalExit(exit_code=1)),
    _returned(exit=LocalExit(signal=9)),
    _returned(exit=None),
    _returned(error="timeout", used_grace_s=Decimal("1")),
    _returned(error="cancelled", used_grace_s=Decimal("1")),
    _returned(error="output_failed", output_failure="read failed"),
    _returned(output_failure="read failed"),
], ids=["posix", "uppercase", "nonhex", "short", "long", "crlf", "no-lf", "filename",
        "suffix", "multiple-lines", "empty", "none", "exit-error", "signal",
        "no-exit", "timeout", "cancelled", "output-error", "output-failure"])
async def test_run_keeps_noncanonical_or_unsuccessful_digest_results(world, monkeypatch, raw):
    _supply_tool_result(monkeypatch, raw)
    result = await WorldTransport(world).run(_spec(TOOLS.digest_script(REMOTE)), NeverStop())
    assert result == raw


@pytest.mark.parametrize("script", [
    "printf sha256sum",
    TOOLS.metadata_script(REMOTE),
    TOOLS.delete_script(REMOTE),
    TOOLS.read_script(REMOTE, 0),
    TOOLS.directory_script("/mnt/media_rw/emulated/DCIM", None, 1),
    TOOLS.digest_script(REMOTE) + "; true",
    TOOLS.digest_script(REMOTE).replace("sha256sum", "sha256sum --binary"),
    "LC_ALL=C sha256sum /mnt/media_rw/emulated/DCIM/plain.mp4",
    "LC_ALL=C sha256sum < 'unterminated",
    "   ",
], ids=["command-name-only", "metadata", "delete", "read", "directory", "extra-command",
        "extra-option", "not-stdin", "unparseable", "no-words"])
async def test_run_keeps_matching_output_from_other_scripts(world, monkeypatch, script):
    raw = _returned()
    _supply_tool_result(monkeypatch, raw)
    result = await WorldTransport(world).run(_spec(script), NeverStop())
    assert result == raw


async def test_run_registered_software_command_takes_priority_over_file_script(world):
    script = TOOLS.digest_script(REMOTE)
    request = ControlRequest("start_record", BINDING, {}, AttemptTicket(1, "control", "1", "capture/1", 1))
    world.requests[(SERIAL, script)] = (request, CameraCall.SETTING, 1)
    result = await WorldTransport(world).run(_spec(script), NeverStop())
    assert result == _returned(b"SOFTWARE-CAMERA/1 setting")


async def test_software_result_pages_keep_posix_identity_locator_and_cursor(world):
    root = "/mnt/media_rw/emulated/DCIM"
    paths = [root + "/a 'quoted'.mp4", root + "/z.mp4"]
    for remote in paths:
        source = world.source_path(remote)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"media")
    transport = WorldTransport(world)
    cursor = None
    for remote in paths:
        request = ControlRequest("list_results", BINDING,
            {"output_scope": {"directories": [root]}, "cursor": cursor},
            AttemptTicket(1, "result", "1", "capture/1", 1))
        world.requests[(SERIAL, "software-results")] = (request, CameraCall.RESULT, 1)
        raw = await transport.run(_spec("software-results"), NeverStop())
        page = json.loads(raw.output.removeprefix(b"SOFTWARE-RESULT/1\n"))
        assert [(entry["identity"], entry["locator"]) for entry in page["entries"]] == [(remote, {"path": remote})]
        cursor = page["next_cursor"]
        if remote == paths[0]:
            assert cursor["after_path"] == remote
    assert cursor is None


@pytest.fixture
def real_file(world):
    content = (b"line\r\n\x00" + "相机媒体".encode("utf-8") + b"\x1a\xff\n") * 5000
    path = world.source_path(REMOTE)
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    filesystem = AdbFilesystem(BINDING, SERIAL, WorldTransport(world), tools=TOOLS)
    return filesystem, FileIdentity(BINDING, REMOTE), path, content


async def test_real_file_digest_matches_original_binary_bytes(real_file):
    filesystem, identity, path, content = real_file
    result = await filesystem.source_digest(identity, timeout_s=Decimal("10"), stop=NeverStop())
    assert result.error is None, result
    assert result.value == hashlib.sha256(content).hexdigest()
    assert path.read_bytes() == content


async def test_real_file_metadata_counts_original_binary_bytes(real_file):
    filesystem, identity, path, content = real_file
    result = await filesystem.metadata(identity, timeout_s=Decimal("10"), stop=NeverStop())
    assert result.error is None, result
    assert result.value == len(content)
    assert path.read_bytes() == content


@pytest.mark.parametrize("offset", [0, 65535])
async def test_real_file_stream_keeps_original_binary_bytes(real_file, offset):
    filesystem, identity, path, content = real_file
    source = SourceFile(json.dumps([BINDING.device_id, BINDING.driver_id, identity.path]),
                        {"path": identity.path}, len(content))
    session = await filesystem.open_read(source, offset, AttemptTicket(1, "read", "9", "copy/9", 1),
                                         idle_timeout_s=Decimal("10"))
    received = bytearray()
    try:
        while True:
            chunk = await asyncio.to_thread(session.read_chunk, 32768)
            assert chunk.error is None
            received.extend(chunk.data or b"")
            if chunk.eof:
                break
        end = await session.wait_stopped()
        assert end.stopped and end.error is None
        assert bytes(received) == content[offset:]
        assert end.bytes_read == len(content) - offset
        assert path.read_bytes() == content
    finally:
        session.request_stop()
        await session.wait_stopped()


async def test_real_directory_pages_keep_target_paths_and_forward_cursor(real_file, world):
    filesystem, identity, _, _ = real_file
    other = "/mnt/media_rw/emulated/DCIM/z.mp4"
    world.source_path(other).write_bytes(b"keep")
    request = DirectoryRequest(BINDING, ("/mnt/media_rw/emulated/DCIM",), None, 1, Decimal("10"))
    first = await filesystem.read_directory(request, stop=NeverStop())
    assert first.error is None, first
    assert [entry.path for entry in first.page.items] == [identity.path]
    assert first.page.next_cursor.after_path == identity.path
    second = await filesystem.read_directory(replace(request, cursor=first.page.next_cursor), stop=NeverStop())
    assert second.error is None, second
    assert [entry.path for entry in second.page.items] == [other]
    assert second.page.next_cursor is None


async def test_real_delete_removes_only_the_requested_source(real_file, world):
    filesystem, identity, path, _ = real_file
    other = world.source_path("/mnt/media_rw/emulated/DCIM/keep.mp4")
    other.write_bytes(b"keep")
    result = await filesystem.delete_file(identity, timeout_s=Decimal("10"), stop=NeverStop())
    assert result.error is None, result
    assert result.value is True
    assert not path.exists()
    assert other.read_bytes() == b"keep"

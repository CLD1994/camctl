"""共用单文件工厂与真实前台 shell／受管传输组合，不连接相机。"""
from decimal import Decimal
import json
import os
import shlex
import sys

import pytest

from camctl.devices.adb_transport import invoke
from camctl.devices.bindings import DeviceBinding
from camctl.devices.drivers.adb_cameras.contracts import CameraCall
from camctl.devices.drivers.adb_cameras.file_commands import FILE_COMMAND_EVIDENCE, file_command_factory
from camctl.devices.drivers.adb_cameras.filesystem import ShellFileTools
from camctl.devices.evidence import EvidenceRegistry
from camctl.devices.ports import ControlRequest, DeviceCallResult
from camctl.operations.models import AttemptTicket, AttemptStatus, EffectState, SettlementBasis
from .test_adb_camera_transport import LocalAdbChannel

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.name == "nt", reason="前台 shell 的 POSIX 软件验证")]
BINDING = DeviceBinding("cam-1", "dji-osmo360-ii")


async def _call(path, kind=CameraCall.FILE_QUERY, *, tools=None, timeout=Decimal("3")):
    operation = "delete" if kind is CameraCall.DELETE else "query"
    request = ControlRequest(operation, BINDING, {
        "cleanup_item_id": "7", "identity_key": json.dumps([BINDING.device_id, BINDING.driver_id, str(path)]),
        "locator": {"path": str(path)}}, AttemptTicket(1, operation, "7", "cleanup/7", 2), timeout)
    factory = file_command_factory(kind, tools=tools or ShellFileTools(), evidence=EvidenceRegistry(FILE_COMMAND_EVIDENCE))
    channel = LocalAdbChannel()
    outcome = await invoke(factory(request, "serial-1", None, None), request.ticket, channel)
    assert len(channel.calls) == 1
    return DeviceCallResult.from_outcome(outcome)


@pytest.mark.parametrize("name", ["empty.OSV", "a 'quote'\nzero.LRF", "b\\literal * ?.OSV"])
async def test_zero_byte_original_is_present_and_exact_absence_is_reliable(tmp_path, name):
    target = tmp_path / name
    target.write_bytes(b"")
    present = await _call(target)
    assert present.error is None
    assert present.observations[0].data == {"cleanup_item_id": "7", "present": True}
    target.unlink()
    (tmp_path / (name + ".other")).write_bytes(b"other")
    absent = await _call(target)
    assert absent.error is None
    assert absent.observations[0].data == {"cleanup_item_id": "7", "present": False}


@pytest.mark.parametrize("name", ["source.OSV", "source.LRF"])
async def test_delete_only_removes_exact_original_and_observes_once(tmp_path, name):
    target = tmp_path / name
    target.write_bytes(b"source")
    sibling = tmp_path / ("source.LRF" if name.endswith("OSV") else "source.OSV")
    sibling.write_bytes(b"sibling")
    result = await _call(target, CameraCall.DELETE)
    assert result.error is None
    assert not target.exists() and sibling.read_bytes() == b"sibling"
    assert result.observations[0].type == "file_delete_presence"
    assert result.observations[0].data["present"] is False


@pytest.mark.parametrize("kind", [CameraCall.FILE_QUERY, CameraCall.DELETE])
async def test_same_name_directory_is_unknown_and_never_recursively_deleted(tmp_path, kind):
    target = tmp_path / "file.OSV"
    target.mkdir()
    content = target / "kept"
    content.write_bytes(b"kept")
    result = await _call(target, kind)
    assert result.error is not None and result.observations == ()
    assert content.read_bytes() == b"kept"
    if kind is CameraCall.DELETE:
        assert result.outcome.error.details["rm_exit_code"] != 0
        assert result.outcome.error.details["reason"] == "type"


@pytest.mark.parametrize("parent_type", ["missing", "file"])
async def test_unavailable_original_directory_cannot_prove_absence(tmp_path, parent_type):
    parent = tmp_path / "directory"
    if parent_type == "file":
        parent.write_bytes(b"not directory")
    result = await _call(parent / "target.OSV", CameraCall.DELETE)
    assert result.error is not None and result.observations == ()
    assert result.outcome.error.details["rm_exit_code"] != 0
    assert result.outcome.error.details["scan_exit_code"] != 0


@pytest.mark.parametrize("listed", [True, False])
@pytest.mark.parametrize("scan_code", [1, 127])
async def test_find_failure_retains_present_only_when_original_name_was_observed(tmp_path, monkeypatch, listed, scan_code):
    target = tmp_path / "target.OSV"
    target.write_bytes(b"")
    tools = tmp_path / "tools"
    tools.mkdir()
    find = tools / "find"
    find.write_text(f"#!{sys.executable}\nimport sys\n"
                    + (f"sys.stdout.buffer.write({str(target).encode()!r} + b'\\0')\n" if listed else "")
                    + f"sys.exit({scan_code})\n")
    find.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    result = await _call(target)
    assert result.error is not None
    assert bool(result.observations) is listed
    if listed:
        assert result.observations[0].data["present"] is True
    assert result.outcome.error.details["scan_exit_code"] == scan_code


class _AfterFrameTools(ShellFileTools):
    def __init__(self, tail):
        self.tail = tail

    def file_presence_script(self, path, *, delete=False):
        return super().file_presence_script(path, delete=delete) + "\n" + self.tail


async def test_observation_before_timeout_survives_actual_local_termination(tmp_path):
    target = tmp_path / "target.OSV"
    target.write_bytes(b"")
    tail = shlex.join((sys.executable, "-c", "import time; time.sleep(5)"))
    result = await _call(target, tools=_AfterFrameTools(tail), timeout=Decimal("0.1"))
    assert result.error["code"] == "transport_timeout"
    assert result.observations[0].data["present"] is True
    assert result.outcome.effect is EffectState.CONFIRMED
    assert result.outcome.status is AttemptStatus.FAILED
    assert result.outcome.settlement.basis is SettlementBasis.ASSUMED


@pytest.mark.parametrize("channel", ["stdout", "stderr"])
async def test_real_output_limit_discards_any_retained_observation_after_local_settlement(tmp_path, channel):
    target = tmp_path / "target.OSV"
    target.write_bytes(b"")
    tail = shlex.join((sys.executable, "-c", f"import sys; sys.{channel}.buffer.write(b'x' * (2 << 20))"))
    result = await _call(target, tools=_AfterFrameTools(tail))
    assert result.error["code"] == "transport_output_failed"
    assert result.observations == () and result.outcome.call_info is not None
    assert "output_limit_exceeded" in result.outcome.error.details["output_failure"]


async def test_original_parent_symlink_must_be_traversed_before_absence_can_be_claimed(tmp_path):
    directory = tmp_path / "actual"
    directory.mkdir()
    target = directory / "target.OSV"
    target.write_bytes(b"")
    parent = tmp_path / "original"
    parent.symlink_to(directory, target_is_directory=True)
    result = await _call(parent / target.name)
    assert result.error is None
    assert result.observations[0].data["present"] is True


@pytest.mark.parametrize("representation", ["double-separator", "different-root", "dot-component", "missing-root", "duplicate-root"])
async def test_uninterpretable_find_namespace_never_proves_absence(tmp_path, monkeypatch, representation):
    root = tmp_path / "original"
    root.mkdir()
    tools = tmp_path / "tools"
    tools.mkdir()
    records = [str(root) + "/"]
    if representation == "double-separator":
        records.append(str(root) + "//target.OSV")
    elif representation == "different-root":
        records.append("/different/target.OSV")
    elif representation == "dot-component":
        records.append(str(root) + "/./target.OSV")
    elif representation == "missing-root":
        records = []
    else:
        records.append(str(root))
    find = tools / "find"
    payload = b"".join(record.encode() + b"\0" for record in records)
    find.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.buffer.write({payload!r})\n")
    find.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    result = await _call(root / "target.OSV")
    assert result.error is not None and result.observations == ()
    assert result.outcome.error.details["reason"] == "invalid"


async def test_filesystem_root_find_prefix_is_single_slash(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    find = tools / "find"
    find.write_text(f"#!{sys.executable}\nimport sys\n"
                    "assert sys.argv[1:] == ['/', '-print0'], sys.argv\n"
                    "sys.stdout.buffer.write(b'/\\0')\n")
    find.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    result = await _call("/camctl-R5-software-query-target.OSV")
    assert result.error is None
    assert result.observations[0].data["present"] is False


async def test_failed_delete_uses_one_rm_and_one_scan_without_retry(tmp_path, monkeypatch):
    import shutil

    root = tmp_path / "original"
    root.mkdir()
    target = root / "target.OSV"
    target.write_bytes(b"kept")
    tools = tmp_path / "tools"
    tools.mkdir()
    log = tmp_path / "tool-calls"
    real_find = shutil.which("find")
    assert real_find is not None
    for name, ending in (("rm", "sys.exit(1)"),
                         ("find", f"os.execv({real_find!r}, [{real_find!r}, *sys.argv[1:]])")):
        command = tools / name
        command.write_text(f"#!{sys.executable}\nimport os,sys\n"
                           f"with open({str(log)!r}, 'a') as stream: stream.write({name!r} + '\\n')\n"
                           + ending + "\n")
        command.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    result = await _call(target, CameraCall.DELETE)
    assert result.error is not None
    assert result.observations[0].data["present"] is True
    assert result.outcome.error.details["rm_exit_code"] == 1
    assert target.read_bytes() == b"kept"
    assert log.read_text().splitlines() == ["rm", "find"]


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root 可绕过目录权限，另由受控扫描失败覆盖")
@pytest.mark.parametrize("kind", [CameraCall.FILE_QUERY, CameraCall.DELETE])
async def test_directory_permission_failure_is_unknown_and_preserves_the_file(tmp_path, kind):
    directory = tmp_path / "locked"
    directory.mkdir()
    target = directory / "target.OSV"
    target.write_bytes(b"kept")
    directory.chmod(0)
    try:
        result = await _call(target, kind)
        assert result.error is not None and result.observations == ()
        assert result.outcome.error.details["scan_exit_code"] != 0
    finally:
        directory.chmod(0o700)
    assert target.read_bytes() == b"kept"

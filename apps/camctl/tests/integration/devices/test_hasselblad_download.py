"""相机指定下载与主机偏移读取使用同一份固定源内容。"""

import asyncio
import hashlib
import json
from decimal import Decimal
from pathlib import Path
import time
import sys
import os

import pytest

from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
from camctl.devices.drivers.hasselblad.driver import HasselbladDriver
from camctl.devices.read_session import SourceFile
from camctl.host_files.models import BoundDirectories
from camctl.operations.models import AttemptTicket
from camctl.operations.process import LocalExit, RawToolOutcome


RAW = b"II*\x00fixed camera original content"
PREVIEW = b"\xff\xd8fixed capture preview\xff\xd9"
NAME = "capture_123.3FR"
CAMERA_PATH = f"/mnt/media/DCIM/{NAME}"
TICKET = AttemptTicket(1, "read", "22", "read/22", 31)


def source(variant="original", *, content=None):
    content = (RAW if variant == "original" else PREVIEW) if content is None else content
    digest = hashlib.sha256(content).hexdigest()
    opaque = json.dumps(
        ["hb-download-v2", CAMERA_PATH, NAME, variant, digest], separators=(",", ":")
    )
    return SourceFile(json.dumps(["camera-1", DRIVER_ID, opaque], separators=(",", ":")),
                      {"camera_path": CAMERA_PATH, "camera_name": NAME,
                       "variant": variant, "sha256": digest}, len(content))


class DownloadTransport:
    def __init__(self, *, payload=None, failure=None, serial="SERIAL"):
        self.calls = []
        self.payload, self.failure, self.serial = payload, failure, serial

    async def run(self, spec, stop):
        self.calls.append(spec)
        command = spec.argv[3]
        if command == "list":
            output = b"vid=0x2756 pid=0x000a  bus=  1 addr= 23\n"
        elif command == "info":
            output = f"serial : {self.serial}\n".encode()
        else:
            assert spec.argv[3:5] in (("download", "original"), ("download", "thumbnail"))
            assert spec.argv[5] == NAME
            assert spec.argv[6] == "--output"
            work = Path(spec.argv[7])
            payload = self.payload if self.payload is not None else (RAW if spec.argv[4] == "original" else PREVIEW)
            (work / "tool-selected-name.bin").write_bytes(payload)
            if self.failure:
                return RawToolOutcome(LocalExit(exit_code=17), b"arbitrary output", self.failure, None)
            output = b"unstructured download stdout; filename is not parsed"
        return RawToolOutcome(LocalExit(exit_code=0), output, None, None)


def harness(tmp_path, **kwargs):
    staging = tmp_path / "staging"
    staging.mkdir()
    transport = DownloadTransport(**kwargs)
    driver = HasselbladDriver({"camera-1": {"kind": "camera", "driver": DRIVER_ID,
        "hasselblad": {"executable": "/tool/hb_cli", "serial": "SERIAL", "product_id": 10}}},
        BoundDirectories(staging), monotonic_ns=time.monotonic_ns, transport=transport)
    return driver, transport, staging


@pytest.mark.asyncio
@pytest.mark.parametrize("variant,payload", [("original", RAW), ("thumbnail", PREVIEW)])
@pytest.mark.parametrize("offset", [0, 7, "end"])
async def test_fixed_download_reads_requested_offset_and_removes_owned_copy(tmp_path, variant, payload, offset):
    driver, transport, staging = harness(tmp_path)
    assert callable(getattr(driver, "open_read", None)), "驱动应提供受管相机读取"
    offset = len(payload) if offset == "end" else offset
    session = await driver.open_read(source(variant), offset, TICKET, idle_timeout_s=Decimal("10"))
    received = bytearray()
    while True:
        chunk = await asyncio.to_thread(session.read_chunk, 5)
        if chunk.data:
            received.extend(chunk.data)
        if chunk.eof:
            break
    end = await session.wait_stopped()
    assert bytes(received) == payload[offset:]
    assert end.stopped and end.error is None
    assert not (staging / "read-downloads" / "31-1").exists()
    assert [spec.argv[3] for spec in transport.calls] == ["list", "info", "download"]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [RAW[:-1], b"x" * len(RAW)])
async def test_partial_or_replaced_content_is_rejected_before_session(tmp_path, payload):
    driver, transport, staging = harness(tmp_path, payload=payload)
    assert callable(getattr(driver, "open_read", None)), "驱动应提供受管相机读取"
    with pytest.raises(ValueError) as failure:
        await driver.open_read(source(), 0, TICKET, idle_timeout_s=Decimal("10"))
    assert getattr(failure.value, "steps", None), "内容核验失败也应保留实际下载收场事实"
    assert failure.value.steps[-1]["local_exit_code"] == 0
    assert not (staging / "read-downloads" / "31-1").exists()
    assert len(transport.calls) == 3


@pytest.mark.asyncio
async def test_source_identity_and_locator_must_match_before_dispatch(tmp_path):
    driver, transport, _staging = harness(tmp_path)
    assert callable(getattr(driver, "open_read", None)), "驱动应提供受管相机读取"
    original = source()
    changed = SourceFile(original.file_id, dict(original.locator, variant="thumbnail"), original.size_bytes)
    with pytest.raises(ValueError):
        await driver.open_read(changed, 0, TICKET, idle_timeout_s=Decimal("10"))
    assert transport.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["missing", "mismatch", "serial"])
async def test_original_binding_and_serial_fail_before_download(tmp_path, binding):
    from camctl.devices.bindings import DeviceConfigurationError
    driver, transport, staging = harness(tmp_path, serial="OTHER" if binding == "serial" else "SERIAL")
    if binding == "missing":
        driver.devices.clear()
    if binding == "mismatch":
        driver.devices["camera-1"]["driver"] = "other-driver"
    with pytest.raises((DeviceConfigurationError, ValueError)):
        await driver.open_read(source(), 0, TICKET, idle_timeout_s=Decimal("10"))
    assert not (staging / "read-downloads" / "31-1").exists()
    assert [s.argv[3] for s in transport.calls] == (["list", "info"] if binding == "serial" else [])


@pytest.mark.asyncio
async def test_nonzero_download_cleans_partial_and_retains_actual_exit(tmp_path):
    driver, transport, staging = harness(tmp_path, payload=RAW[:3], failure="output_failed")
    with pytest.raises(ValueError) as failure:
        await driver.open_read(source(), 0, TICKET, idle_timeout_s=Decimal("10"))
    assert failure.value.steps[-1]["local_exit_code"] == 17
    assert failure.value.steps[-1]["transport_error"] == "output_failed"
    assert not (staging / "read-downloads" / "31-1").exists()


@pytest.mark.asyncio
async def test_download_error_and_cleanup_error_are_both_retained(tmp_path, monkeypatch):
    from camctl.host_files.downloads import DownloadWorkspace, recover_download
    driver, _transport, staging = harness(tmp_path, payload=RAW[:3], failure="output_failed")
    original = DownloadWorkspace.remove
    def fail(_self):
        raise OSError("unlink unavailable")
    monkeypatch.setattr(DownloadWorkspace, "remove", fail)
    with pytest.raises(ValueError) as failure:
        await driver.open_read(source(), 0, TICKET, idle_timeout_s=Decimal("10"))
    reason = str(failure.value)
    assert "output_failed" in reason and "unlink unavailable" in reason and "17" in reason
    assert (staging / "read-downloads" / "31-1").exists()
    monkeypatch.setattr(DownloadWorkspace, "remove", original)
    await recover_download(staging, TICKET)


@pytest.mark.asyncio
async def test_repeated_read_gets_an_independent_download_and_preserves_source(tmp_path):
    driver, transport, staging = harness(tmp_path)
    for _ in range(2):
        session = await driver.open_read(source(), 0, TICKET, idle_timeout_s=Decimal("10"))
        chunk = await asyncio.to_thread(session.read_chunk, 100)
        assert chunk.data == RAW and chunk.eof
        assert (await session.wait_stopped()).stopped
    assert [s.argv[3] for s in transport.calls].count("download") == 2
    assert not (staging / "read-downloads" / "31-1").exists()


@pytest.mark.asyncio
async def test_stopping_open_session_closes_file_and_removes_download(tmp_path):
    driver, _transport, staging = harness(tmp_path)
    session = await driver.open_read(source(), 0, TICKET, idle_timeout_s=Decimal("10"))
    session.request_stop()
    end = await session.wait_stopped()
    assert end.stopped and end.error == "stopped"
    assert not (staging / "read-downloads" / "31-1").exists()


@pytest.mark.asyncio
async def test_cleanup_error_is_not_a_successful_read_end_and_can_be_recovered(tmp_path, monkeypatch):
    from camctl.host_files.downloads import DownloadWorkspace, recover_download
    driver, _transport, staging = harness(tmp_path)
    session = await driver.open_read(source(), 0, TICKET, idle_timeout_s=Decimal("10"))
    original = DownloadWorkspace.remove
    def fail(_self):
        raise OSError("unlink failed")
    monkeypatch.setattr(DownloadWorkspace, "remove", fail)
    with pytest.raises(Exception, match="unlink failed"):
        await asyncio.to_thread(session.read_chunk, 100)
    with pytest.raises(Exception, match="unlink failed"):
        await session.wait_stopped()
    assert (staging / "read-downloads" / "31-1").exists()
    monkeypatch.setattr(DownloadWorkspace, "remove", original)
    assert await recover_download(staging, TICKET)
    assert not (staging / "read-downloads" / "31-1").exists()


def managed_harness(tmp_path, *, sleep=False, exit_code=0):
    driver, _transport, staging = harness(tmp_path)
    script = tmp_path / "hb_cli"
    marker = tmp_path / "download-pid"
    script.write_text(f"#!{sys.executable}\n" +
        "import os,sys,time,pathlib\n"
        "command=sys.argv[3]\n"
        "if command=='list': print('vid=0x2756 pid=0x000a  bus=  1 addr= 23')\n"
        "elif command=='info': print('serial : SERIAL')\n"
        "elif command=='download':\n"
        f" pathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\n"
        " work=pathlib.Path(sys.argv[7])\n"
        f" (work/'unknown-tool-name').write_bytes({RAW if not sleep else RAW[:3]!r})\n"
        f" time.sleep({60 if sleep else 0})\n"
        f" sys.exit({exit_code})\n", encoding="utf-8")
    script.chmod(0o700)
    driver.devices["camera-1"]["hasselblad"]["executable"] = str(script)
    driver = HasselbladDriver(driver.devices, driver.roots, monotonic_ns=time.monotonic_ns,
                              terminate_grace_s=Decimal("0.05"))
    return driver, staging, marker


@pytest.mark.skipif(os.name == "nt", reason="目标部署使用 POSIX 可执行脚本")
@pytest.mark.asyncio
async def test_real_managed_download_returns_fixed_bytes(tmp_path):
    driver, staging, _marker = managed_harness(tmp_path)
    session = await driver.open_read(source(), 3, TICKET, idle_timeout_s=Decimal("10"))
    assert (await asyncio.to_thread(session.read_chunk, 100)).data == RAW[3:]
    assert (await session.wait_stopped()).stopped
    assert not (staging / "read-downloads" / "31-1").exists()


@pytest.mark.skipif(os.name == "nt", reason="目标部署使用 POSIX 可执行脚本")
@pytest.mark.asyncio
async def test_capture_artifacts_and_both_download_variants_have_identical_bytes(tmp_path):
    from .test_hasselblad_managed_capture import _harness
    from camctl.capture.files import file_identity_key
    from camctl.devices.drivers.hasselblad.files import CameraFileIdentity, DownloadVariant
    driver, request, roots, _executable, _log, workspace = _harness(tmp_path)
    capture = await driver.control(request)
    photo = capture.observations[0].data
    for role, variant in (("original", DownloadVariant.ORIGINAL), ("preview", DownloadVariant.THUMBNAIL)):
        artifact = photo[role]
        identity = CameraFileIdentity(
            request.binding,
            "/mnt/media/DCIM/fixture_capture.3FR",
            "fixture_capture.3FR",
            variant,
            artifact["sha256"],
        )
        fixed = SourceFile(file_identity_key(request.binding.device_id, DRIVER_ID, identity.file_identity),
                           identity.locator(), artifact["size_bytes"])
        session = await driver.open_read(fixed, 0, TICKET, idle_timeout_s=Decimal("10"))
        downloaded = await asyncio.to_thread(session.read_chunk, artifact["size_bytes"])
        assert downloaded.eof
        assert downloaded.data == (workspace / artifact["original_name"]).read_bytes()
        assert (await session.wait_stopped()).stopped
    assert not (roots.staging / "read-downloads" / "31-1").exists()


@pytest.mark.skipif(os.name == "nt", reason="目标部署使用 POSIX 可执行脚本")
@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_real_timeout_or_cancel_waits_for_process_exit_before_removing_partial(tmp_path, monkeypatch, cancel):
    from camctl.devices.drivers.hasselblad import driver as module
    driver, staging, marker = managed_harness(tmp_path, sleep=True)
    monkeypatch.setattr(module, "READ_PREPARATION_TIMEOUT_S", Decimal("0.4"))
    task = asyncio.create_task(driver.open_read(source(), 0, TICKET, idle_timeout_s=Decimal("10")))
    if cancel:
        async with asyncio.timeout(2):
            while not marker.exists():
                await asyncio.sleep(0.005)
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else (TimeoutError, ValueError)):
        await task
    pid = int(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not (staging / "read-downloads" / "31-1").exists()

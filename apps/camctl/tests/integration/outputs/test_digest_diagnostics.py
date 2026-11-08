"""源摘要失败沿原驱动结果保留完整工具事实，校验状态保持原语义。"""

import json
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from camctl.bootstrap.capture_assembly import DriverDigestReader
from camctl.devices.bindings import DeviceBinding
from camctl.devices.drivers.adb_cameras.commands import CameraModel
from camctl.devices.drivers.adb_cameras.driver import AdbCameraDriver
from camctl.devices.evidence import EvidenceContract
from camctl.devices.read_session import SourceFile
from camctl.logging_runtime.diagnostics import diagnostic_scope, error_diagnostics_context, flush_diagnostics
from camctl.operations.process import LocalExit, RawToolOutcome, SignalFailure, SignalStage, ToolStartError
from camctl.outputs import copy
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.repositories.file_batches import register_file_batch_guards
from camctl.outputs.obtain_flow import advance_obtain
from camctl.contracts.values import ConsistencyError
from camctl.contracts.enums import enum_for
from ..support.capture import capture_database
from ..support.outputs import obtain_factory, output_drivers
from .test_source_digest_failure_settlement import _ReadDriver, _CONTENT
from .test_read_associations import read_targets
from .test_qualification import _NOW

from unit.devices.test_adb_camera_driver import EVIDENCE, Transport, _driver
from support.logging import recording_log_channel


register_file_batch_guards()


def _digest_driver(raw):
    class DigestTransport(Transport):
        async def run(self, spec, stop):
            self.calls.append(spec)
            if isinstance(raw, BaseException):
                raise raw
            return raw
    transport = DigestTransport()
    original = _driver(CameraModel.ACTION6, transport)
    evidence = EVIDENCE.with_contract(EvidenceContract("file_digest", 1, "digest",
        frozenset({"file_id", "sha256"}), identity_field="file_id"))
    driver = AdbCameraDriver(replace(original.contract, evidence=evidence,
        digest_timeout_s=lambda size: Decimal("240")), original.devices,
        transport, terminate_grace_s=Decimal("1"), monotonic_ns=lambda: 0)
    return driver, evidence, transport


def digest_reader(raw):
    driver, evidence, _ = _digest_driver(raw)
    binding = DeviceBinding("cam-1", CameraModel.ACTION6)
    source = SourceFile('["cam-1","dji-action6","/DCIM/source.mp4"]',
        {"path": "/DCIM/source.mp4"}, 100)
    return DriverDigestReader(driver, binding, evidence, source, "9")


def raw_failure():
    return RawToolOutcome(LocalExit(signal=9), b"private-media-stdout", "timeout", Decimal("1.25"),
        signal_failures=(SignalFailure(SignalStage.KILL, "kill permission denied"),),
        output_failure="stderr read failed", stderr=b"checksum failed\n\xff\x80")


@pytest.mark.asyncio
async def test_digest_adapter_keeps_raw_exit_stderr_and_signal_facts():
    raw = raw_failure()
    records = []
    channel = recording_log_channel(records)
    with error_diagnostics_context(channel, lambda _: None):
        async with diagnostic_scope():
            result = await digest_reader(raw).read_digest()
    assert records == []
    assert result.digest is None
    assert result.error["code"] == "adb_digest_failed"
    assert result.error["details"]["stderr_hex"] == raw.stderr.hex()
    assert result.raw_outcome is raw
    assert result.raw_outcome.used_grace_s == Decimal("1.25")
    assert result.raw_outcome.signal_failures == raw.signal_failures


@pytest.mark.asyncio
async def test_digest_adapter_retains_original_start_exception():
    error = ToolStartError(13, "adb unavailable", "/usr/bin/adb")
    result = await digest_reader(error).read_digest()
    assert result.digest is None and result.raw_outcome is None
    assert result.error["code"] == "adb_digest_failed"
    assert result.diagnostic_error is error


@pytest.mark.asyncio
@pytest.mark.parametrize("save_failed", [False, True])
async def test_verification_failure_logs_original_digest_facts_once_before_save(read_targets, tmp_path, save_failed, monkeypatch):
    owned = read_targets
    raw = raw_failure()
    digest_driver, evidence, transport = _digest_driver(raw)
    evidence = evidence.with_contract(EvidenceContract("read_returned", 1, "read", frozenset()))
    read_driver = _ReadDriver()
    driver = SimpleNamespace(open_read=read_driver.open_read, digest=digest_driver.digest)
    owned.connection.execute("UPDATE actions SET status=3 WHERE id IN (11,12,32)")
    owned.connection.execute("UPDATE actions SET driver_id=? WHERE id=11", (str(CameraModel.ACTION6),))
    owned.connection.execute("UPDATE device_files SET identity_key=?,locator_json=?,size_bytes=?,checksum_support=2,sha256=NULL WHERE id=501",
        ('["cam-1","dji-action6","/DCIM/source.mp4"]', '{"path":"/DCIM/source.mp4"}', len(_CONTENT)))
    owned.connection.commit()
    staging = tmp_path / "staging"
    for name in ("deliveries", "recording-inputs", "derived"):
        (staging / name).mkdir(parents=True)
    factory = obtain_factory(devices={"cam-1": {"driver": str(CameraModel.ACTION6), "copy": {
        "max_read_attempts":3, "retry_interval_s":Decimal(0)}}},
        drivers=output_drivers(driver, evidence, driver_id=str(CameraModel.ACTION6), read=True, digest=True),
        staging=staging, ready=tmp_path / "ready", processing=tmp_path / "processing", segment_size=len(_CONTENT),
        occurred_at=lambda: _NOW + 23_000_000, monotonic_ns=lambda: 0)
    runtime = factory(await capture_database(owned))
    saved = []
    save_verification = OutputsRepository.save_verification
    def verification(self, request, key, worker):
        saved.append((request, key))
        if save_failed:
            raise OSError("verification save failed")
        return save_verification(self, request, key, worker)
    monkeypatch.setattr(OutputsRepository, "save_verification", verification)
    records = []
    channel = recording_log_channel(records)
    with error_diagnostics_context(channel, lambda _: None):
        async with diagnostic_scope():
            if save_failed:
                with pytest.raises(ConsistencyError, match="verification save failed"):
                    await advance_obtain(runtime)
            else:
                await advance_obtain(runtime)
            assert not records
        await flush_diagnostics()
        await flush_diagnostics()
    assert len(saved) == len(transport.calls) == 1
    assert len(read_driver.offsets) == 1
    request, key = saved[0]
    assert request.state == int(enum_for("file_copies.verification_state").FAILED)
    if not save_failed:
        assert owned.connection.execute("SELECT verification_state FROM file_copies").fetchone() == (int(enum_for("file_copies.verification_state").FAILED),)
    assert len(records) == 1
    record = json.loads(records[0].message)
    tool = record["details"]["raw_outcome"]
    assert tool["exit"] == {"exit_code": None, "signal": 9}
    assert tool["used_grace_s"] == "1.25"
    assert tool["signal_failures"] == [{"stage": "kill", "message": "kill permission denied"}]
    assert tool["stderr"] == {"hex": raw.stderr.hex(), "text": raw.stderr.decode("utf-8", "backslashreplace")}
    assert "private-media-stdout" not in json.dumps(record)

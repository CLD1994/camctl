"""源摘要失败沿原驱动结果保留完整工具事实，校验状态保持原语义。"""

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.bootstrap.capture_assembly import DriverDigestReader
from camctl.devices.bindings import DeviceBinding
from camctl.devices.drivers.adb_cameras.commands import CameraModel
from camctl.devices.drivers.adb_cameras.driver import AdbCameraDriver
from camctl.devices.evidence import EvidenceContract
from camctl.devices.read_session import SourceFile
from camctl.host_files.models import BoundDirectories, FilePurpose
from camctl.logging_runtime.diagnostics import diagnostic_scope, error_diagnostics_context, flush_diagnostics
from camctl.operations.attempts import AttemptFinish, RunFinish, RunOutcome
from camctl.operations.models import AttemptStatus, AttemptTicket, CallOutcome, EffectState, ErrorValue, EvidenceValue, Settlement, SettlementBasis
from camctl.operations.process import LocalExit, RawToolOutcome, SignalFailure, SignalStage, ToolStartError
from camctl.operations.validation import validate_outcome
from camctl.outputs import copy
from camctl.outputs.obtain_flow import _LOCAL_READ_EVIDENCE
from camctl.outputs.read_attempts import hold_read_result
from camctl.persistence.models import DbOutcome, DbOutcomeKind

from ..devices.test_adb_camera_driver import EVIDENCE, Transport, _driver
from .test_read_diagnostics import Channel


def digest_reader(raw):
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
    channel = Channel()
    with error_diagnostics_context(channel, lambda _: None):
        async with diagnostic_scope():
            result = await digest_reader(raw).read_digest()
    assert channel.records == []
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
async def test_verification_failure_logs_original_digest_facts_once_before_save(monkeypatch, save_failed):
    raw = raw_failure()
    saved = []
    class Repository:
        def save_verification(self, request, key, owned):
            saved.append(request)
            return DbOutcome(DbOutcomeKind.COMPLETED,
                copy.VerificationResult(copy.VerificationDisposition.SAVED, request.state))
    async def write(callback, **kwargs):
        if save_failed:
            raise OSError("verification save failed")
        return callback(None)
    async def inline(function, *args):
        assert function is copy.hash_target
        return SimpleNamespace(error=None, digest="b" * 64)
    monkeypatch.setattr(copy.asyncio, "to_thread", inline)
    context = copy.CompletionContext(Repository(), SimpleNamespace(write=write),
        BoundDirectories(staging=Path("/staging")), 123, digest=digest_reader(raw))
    facts = SimpleNamespace(target=SimpleNamespace(file_id=3, purpose=FilePurpose.DELIVERY_COPY,
        relative_path="deliveries/3.part"), source_sha256=None, source_support=copy.SourceChecksumSupport.SUPPORTED,
        source_size=100, committed_bytes=100, recopies_used=0)
    channel = Channel()
    with error_diagnostics_context(channel, lambda _: None):
        try:
            async with diagnostic_scope():
                with pytest.raises(copy.CopyCompletionError) as caught:
                    await copy._verify_and_finish(2, context, facts)
                assert caught.value.stage == "verification_failed"
                ticket = AttemptTicket(1, "read", "2", "read/2", 5)
                outcome = CallOutcome(status=AttemptStatus.SUCCEEDED, effect=EffectState.UNKNOWN,
                    settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("read_returned", 1, {})))
                hold_read_result(SimpleNamespace(pending_read_results={}, pending_read_ends={}),
                    AttemptFinish(ticket, validate_outcome(ticket, outcome, _LOCAL_READ_EVIDENCE), 123,
                        run_finish=RunFinish(RunOutcome.FAILED, ErrorValue("source_file_unconfirmed", "source_read"))),
                    diagnostic_error=caught.value, diagnostic_stage="verification_failed")
                assert not channel.records
        except OSError as error:
            assert save_failed and str(error) == "verification save failed"
        await flush_diagnostics()
    if not save_failed:
        assert saved[0].state == int(copy._VERIFICATION.FAILED)
    assert len(channel.records) == 1
    record = channel.records[0]
    tool = record["details"]["raw_outcome"]
    assert tool["exit"] == {"exit_code": None, "signal": 9}
    assert tool["used_grace_s"] == "1.25"
    assert tool["signal_failures"] == [{"stage": "kill", "message": "kill permission denied"}]
    assert tool["stderr"] == {"hex": raw.stderr.hex(), "text": raw.stderr.decode("utf-8", "backslashreplace")}
    assert b"private-media-stdout".decode() not in json.dumps(record)

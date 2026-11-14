"""默认 run 等待本地维护后沿原 READ 责任重新下载并发布照片。"""

import asyncio
import errno
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import time

import pytest

from support.repository_ports import repository_port

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.capture.recovery import RecoveryBoundary
from camctl.contracts.values import new_operation_key
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
from camctl.devices.drivers.hasselblad.driver import EVIDENCE, HasselbladDriver
from camctl.devices.read_session import DeferredSourceFile
from camctl.host_files.models import BoundDirectories
from camctl.operations.attempts import AttemptConfig, AttemptFinish, AttemptIntent, AttemptTarget, OperationKind
from camctl.operations.models import AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue, Settlement, SettlementBasis
from camctl.operations.error_diagnostics import exception_diagnostic
from camctl.operations.validation import validate_outcome
from camctl.outputs.copy import ReliableSegment
from camctl.outputs.photo_read import PhotoReadStart
from camctl.outputs.slots import SlotRequest
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import OutputsRepository

from ..capture.test_host_photo_flow import _config, _NOW
from ..devices import test_hasselblad_download
from ..devices.test_hasselblad_download import DownloadTransport
from .test_download_cleanup import acquire_batch, capture_selection, photo_read_request  # noqa: F401
from .test_output_binding_changes import _CONTENT

pytestmark = pytest.mark.asyncio


def _saved(result):
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    return result.value


async def _pending_last_read(tmp_path, photo_read_request, monkeypatch):
    owned, original = photo_read_request
    config = AttemptConfig(2, Decimal("10"), Decimal("0"))
    command = replace(original, config=replace(original.config, max_attempts=2))
    outputs, operations = OutputsRepository(), OperationRepository()
    prepared = _saved(outputs.grant_file(command, new_operation_key(), owned))
    acquire_batch(owned, command)
    _saved(outputs.grant_read_slot(SlotRequest(prepared.copy_id, _NOW, capture_selection(_NOW)),
                                   new_operation_key(), owned))

    def begin(when):
        return _saved(operations.begin_attempt(AttemptIntent(
            "read", command.action_id, OperationKind.READ_FILE, AttemptTarget(copy_id=prepared.copy_id), None,
            config, when, copy_round=1, capture_selection=capture_selection(when)), new_operation_key(), owned)).ticket

    first = begin(_NOW + 1)
    cfg = _config(tmp_path, storage="device")
    devices = {"cam-1": dict(cfg.devices["cam-1"], copy={"max_read_attempts": 2, "retry_interval_s": 0})}
    cfg = replace(cfg, devices=devices, cleanup=replace(cfg.cleanup, work_file_limit_per_run=1))
    monkeypatch.setattr(test_hasselblad_download, "NAME", "B9996039.3FR")
    transport = DownloadTransport(payload=_CONTENT, serial="SERIAL-1")
    driver = HasselbladDriver(devices, BoundDirectories(Path(cfg.paths.staging)),
                              monotonic_ns=time.monotonic_ns, transport=transport)
    identity, locator = owned.connection.execute(
        "SELECT identity_key,locator_json FROM device_files WHERE id=?", (command.source_device_file_id,)).fetchone()
    session = await driver.open_read(DeferredSourceFile(identity, json.loads(locator)), 0, first,
                                     idle_timeout_s=Decimal("10"))
    try:
        _saved(outputs.save_photo_read_start(PhotoReadStart(prepared.copy_id, first, session.source_size, _NOW + 2),
                                             new_operation_key(), owned))
        chunk = await asyncio.to_thread(session.read_chunk, 4)
        assert chunk.data == _CONTENT[:4] and len(chunk.data) == 4
        relative, = owned.connection.execute("SELECT relative_path FROM intermediate_files WHERE id=?",
                                             (prepared.target_file_id,)).fetchone()
        target = Path(cfg.paths.staging) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as stream:
            stream.write(chunk.data)
            stream.flush()
            os.fsync(stream.fileno())
        _saved(outputs.save_segment(ReliableSegment(prepared.copy_id, 1, 0, 4, True, _NOW + 3),
                                    new_operation_key(), owned))
        downloaded = (Path(cfg.paths.staging) / "read-downloads" /
                      f"{first.run_id}-{first.attempt_id}" / "tool-selected-name.bin")
        source_stat = downloaded.stat()
        real_read = os.read
        def failed_read(fd, length):
            actual = os.fstat(fd)
            if (actual.st_dev, actual.st_ino) == (source_stat.st_dev, source_stat.st_ino):
                raise OSError(errno.EIO, "injected source read failure", str(downloaded))
            return real_read(fd, length)
        with monkeypatch.context() as patch:
            patch.setattr(os, "read", failed_read)
            with pytest.raises(OSError, match="injected source read failure") as failure:
                await asyncio.to_thread(session.read_chunk, 4)
        end = await session.wait_stopped()
    finally:
        session.request_stop()
        await session.wait_stopped()
    assert end.stopped and end.bytes_read == 4 and end.error == "failed"
    failed = CallOutcome(status=AttemptStatus.FAILED, effect=EffectState.UNKNOWN,
        error=ErrorValue("device_error", "read", {"reason": str(failure.value),
            "exception": exception_diagnostic(failure.value),
            "read_end": {"stopped": end.stopped, "bytes_read": end.bytes_read, "error": end.error}}),
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("read_returned", 1, {})))
    _saved(operations.finish_attempt(AttemptFinish(first, validate_outcome(first, failed, EVIDENCE),
                                                   _NOW + 4, retry_wait=True), new_operation_key(), owned))
    ticket = begin(_NOW + 5)
    assert ticket.attempt_id == 2
    assert target.read_bytes() == _CONTENT[:4]
    assert owned.connection.execute("SELECT committed_bytes FROM file_copies WHERE id=?", (prepared.copy_id,)).fetchone() == (4,)
    staging = Path(cfg.paths.staging)
    earlier = staging / "read-downloads" / f"{first.run_id}-{first.attempt_id}"
    work = staging / "read-downloads" / f"{ticket.run_id}-{ticket.attempt_id}"
    return cfg, owned, prepared, ticket, target, earlier, work


def _assert_waiting(owned, prepared, ticket, target):
    assert owned.connection.execute(
        "SELECT copy_id,responsibility_key,attempts_used,max_attempts_used FROM operation_runs WHERE id=?",
        (ticket.run_id,)).fetchone() == (prepared.copy_id, ticket.responsibility_key, 2, 2)
    attempts = owned.connection.execute(
        "SELECT attempt_no,status,result_json,error_json FROM operation_attempts WHERE run_id=? ORDER BY attempt_no",
        (ticket.run_id,)).fetchall()
    assert len(attempts) == 2 and attempts[0][:2] == (1, 3) and attempts[1] == (2, 1, None, None)
    assert json.loads(attempts[0][2])["settlement"]["evidence"]["type"] == "read_returned"
    failure = json.loads(attempts[0][3])
    assert (failure["code"], failure["stage"]) == ("device_error", "read")
    assert failure["details"]["exception"]["errno"] == errno.EIO
    assert failure["details"]["read_end"] == {"stopped": True, "bytes_read": 4, "error": "failed"}
    assert owned.connection.execute(
        "SELECT slot_device_id,committed_bytes FROM file_copies WHERE id=?", (prepared.copy_id,)).fetchone() == ("cam-1", 4)
    assert target.read_bytes() == _CONTENT[:4]


async def _default_run(cfg):
    deps = build_runtime(CommandMode.RUN, cfg, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED)
    try:
        # 完整会话包含报告生成及收场；此上限仅防止测试无限等待。
        return await asyncio.wait_for(execute_command(deps, None, poll_interval_s=0.001), timeout=30)
    finally:
        close_runtime(deps)


@pytest.mark.parametrize("waiting_for", ["outside_budget", "prepare_denied"])
async def test_default_run_exits_on_local_precondition_then_resumes_original_last_ticket(
        tmp_path, photo_read_request, monkeypatch, waiting_for):
    from camctl.devices.drivers.builtin import register_builtin_drivers
    from camctl.devices.drivers import runtime

    cfg, owned, prepared, ticket, target, earlier, work = await _pending_last_read(
        tmp_path, photo_read_request, monkeypatch)
    earlier.mkdir(parents=True)
    (earlier / "old").write_bytes(b"original responsibility")
    if waiting_for == "outside_budget":
        work.mkdir()
        (work / "old").write_bytes(b"original responsibility")
    transport = DownloadTransport(payload=_CONTENT, serial="SERIAL-1")
    register_builtin_drivers(cfg)
    monkeypatch.setattr(runtime.current_registry().entry(DRIVER_ID).driver, "transport", transport)
    real_mkdir = os.mkdir
    with monkeypatch.context() as patch:
        if waiting_for == "prepare_denied":
            def denied(path, *args, **kwargs):
                if Path(path) == work:
                    raise PermissionError(f"workspace preparation denied: {work}")
                return real_mkdir(path, *args, **kwargs)
            patch.setattr(os, "mkdir", denied)
        first = await _default_run(cfg)

    assert not first.succeeded and first.reason == "state_db_error", first
    assert str(work) in str(first.details)
    assert ("denied" if waiting_for == "prepare_denied" else "FileExistsError") in str(first.details)
    assert not earlier.exists()
    if waiting_for == "outside_budget":
        assert (work / "old").read_bytes() == b"original responsibility"
    else:
        assert not work.exists()
    assert not transport.calls
    _assert_waiting(owned, prepared, ticket, target)

    observations = []

    class ObservePhotoRead:
        delegate = OutputsRepository()

        def save_photo_read_start(self, request, key, worker):
            result = self.delegate.save_photo_read_start(request, key, worker)
            if result.kind is DbOutcomeKind.COMPLETED:
                saved = worker.connection.execute(
                    "SELECT source_size,committed_bytes FROM file_copies WHERE id=?",
                    (prepared.copy_id,)).fetchone()
                observations.append((request.ticket, request.size_bytes, saved))
            return result

    # 每次默认会话有独立的退出保护；轮数上限防止维护未推进时忙循环。
    with repository_port(OutputsRepository, ObservePhotoRead(), ("save_photo_read_start",)):
        for _ in range(10):
            second = await _default_run(cfg)
            if second.succeeded:
                break
            assert second.reason == "state_db_error", second
            assert str(work) in str(second.details) and "FileExistsError" in str(second.details), second
            assert (work / "old").read_bytes() == b"original responsibility"
            assert not transport.calls
            _assert_waiting(owned, prepared, ticket, target)
        else:
            pytest.fail("恢复观察内未能处理原工作目录")

    assert observations == [(ticket, len(_CONTENT), (len(_CONTENT), 0))]

    assert not work.exists()
    assert [spec.argv[3] for spec in transport.calls] == ["list", "info", "download"]
    assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (2,)
    attempts = owned.connection.execute("SELECT attempt_no,status,result_json,max_attempts_used,timeout_s_json,retry_interval_s_json FROM operation_attempts WHERE run_id=? ORDER BY attempt_no", (ticket.run_id,)).fetchall()
    assert len(attempts) == 2 and [row[0] for row in attempts] == [1, 2]
    assert all((row[3], Decimal(row[4]), Decimal(row[5])) == (2, Decimal("10"), Decimal("0")) for row in attempts)
    assert attempts[1][1] == 2 and json.loads(attempts[1][2])["settlement"]["evidence"]["type"] == "read_returned"
    assert owned.connection.execute("SELECT slot_device_id,committed_bytes,target_sha256 FROM file_copies WHERE id=?", (prepared.copy_id,)).fetchone() == (None, len(_CONTENT), hashlib.sha256(_CONTENT).hexdigest())
    assert owned.connection.execute("SELECT status FROM deliveries WHERE id=?", (prepared.delivery_id,)).fetchone() == (5,)
    assert [path.read_bytes() for path in Path(cfg.paths.ready).glob("*.3fr")] == [_CONTENT]

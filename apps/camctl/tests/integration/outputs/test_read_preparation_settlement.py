"""真实下载准备经过两个读取入口，只有已确认收场才结束原 READ。"""

import asyncio
import json
import os
import threading
from decimal import Decimal
from types import SimpleNamespace

import pytest

from camctl.capture.input_copy import InputContext, obtain_recording_input
from camctl.capture.media_flow import DriverReadSessions, RecordingInputCopies, _finish_internal_read
from camctl.capture.recovery import RecoveryBoundary
from camctl.devices.drivers.hasselblad.driver import EVIDENCE
from camctl.host_files.downloads import recover_download
from camctl.host_files.models import BoundDirectories
from camctl.operations.attempts import RetryWaitGate, AttemptConfig, AttemptIntent, AttemptTarget, OperationKind
from camctl.operations.process import RawToolOutcome, ToolStartError
from camctl.outputs.obtain_flow import _copy_delivery, _finish_read_attempt
from camctl.outputs.read_attempts import acquire_read_attempt, running_read_ticket
from camctl.persistence.repositories.operations import OperationRepository

from .test_grant_reuse import granted, read_request, read_targets, local_read, _later_attempt
from ..devices.test_hasselblad_download import harness, source, RAW


@pytest.mark.asyncio
@pytest.mark.parametrize("read_request", ["device", "internal"], indirect=True)
@pytest.mark.parametrize("failure", ["not_started", "exited", "exited_cleanup", "no_exit", "transport_error", "bare_cancel", "verify_close", "dispose_close"])
async def test_preparation_settlement_survives_real_consumer_and_result_save(granted, tmp_path, read_request, failure, monkeypatch):
    owned, command, _, prepared = granted
    _later_attempt(owned, command, prepared)
    ticket, intent_id = running_read_ticket(owned, prepared.copy_id)
    driver, transport, staging = harness(tmp_path)
    for name in ("deliveries", "recording-inputs"):
        (staging / name).mkdir()
    fixed = source()
    owned.connection.execute("UPDATE device_files SET identity_key=?,locator_json=?,size_bytes=? WHERE id=501",
        (fixed.file_id, json.dumps(dict(fixed.locator)), len(RAW)))
    owned.connection.execute("UPDATE file_copies SET source_size=? WHERE id=?", (len(RAW), prepared.copy_id))
    owned.connection.commit()
    original = transport.run
    async def run(spec, stop):
        if spec.argv[3] != "download":
            return await original(spec, stop)
        if failure == "not_started":
            transport.calls.append(spec)
            raise ToolStartError("exec not found")
        if failure in ("exited", "exited_cleanup"):
            transport.failure = "failed"
            return await original(spec, stop)
        await original(spec, stop)
        if failure == "no_exit":
            return RawToolOutcome(None, b"", None, None)
        if failure == "bare_cancel":
            raise asyncio.CancelledError("transport cancelled without exit")
        raise OSError("transport lost exit")
    transport.run = run
    if failure in ("verify_close", "dispose_close"):
        transport.run = original
    if failure == "verify_close":
        transport.payload = b"x" * len(RAW)
    runtime = SimpleNamespace(owned=owned, directories=BoundDirectories(staging), segment_size=5,
        file_executor=None, pending_read_results={}, pending_read_ends={}, pending_read_business={},
        continuing_read_tickets={}, operations=OperationRepository(), occurred_at=lambda: command.occurred_at + 20,
        retry_gate=RetryWaitGate(), monotonic_ns=lambda: 0, max_read_attempts=5, evidence=EVIDENCE,
        recovery_boundary=RecoveryBoundary.UNCONFIRMED, recovery_max_event_id=intent_id,
        last_recovery_diagnostic=None, on_recovery_diagnostic=None)
    sessions = DriverReadSessions(owned, driver, ticket, idle_timeout_s=Decimal("10"))
    internal = command.processing_id is not None
    async def execute():
        if internal:
            copies = RecordingInputCopies(owned, runtime.directories, runtime.occurred_at)
            step = await obtain_recording_input(InputContext(command.action_id, command.processing_id,
                501, "part", command.config, 5, staging, copies, sessions, runtime.occurred_at()))
            _finish_internal_read(runtime, ticket, step)
        else:
            facts = {"id": prepared.copy_id, "source_device_file_id": 501, "delivery_status": 1}
            step = await _copy_delivery(runtime, sessions, facts, None)
            _finish_read_attempt(runtime, SimpleNamespace(evidence=EVIDENCE, max_read_attempts=5), facts, ticket, step)
    unsettled = failure in ("no_exit", "transport_error", "bare_cancel", "verify_close", "dispose_close")
    work = staging / "read-downloads" / f"{ticket.run_id}-{ticket.attempt_id}"
    from camctl.host_files import downloads
    entered, release = threading.Event(), threading.Event()
    real_close, real_read = os.close, os.read
    close_calls = []
    def fail_close(fd):
        owner = downloads._ACTIVE.get(work)
        if owner is not None and owner.fd == fd:
            close_calls.append(fd)
            raise OSError("file close not confirmed")
        return real_close(fd)
    def blocked_read(fd, limit):
        if not entered.is_set():
            entered.set()
            assert release.wait(3)
        return real_read(fd, limit)
    if failure == "exited_cleanup":
        real_remove = downloads.shutil.rmtree
        def fail_remove(path):
            if path == work:
                raise PermissionError("download cleanup denied")
            return real_remove(path)
        monkeypatch.setattr(downloads.shutil, "rmtree", fail_remove)
    if failure in ("verify_close", "dispose_close"):
        monkeypatch.setattr(os, "close", fail_close)
    if failure == "dispose_close":
        monkeypatch.setattr(os, "read", blocked_read)
    async def attempt():
        if failure != "dispose_close":
            return await execute()
        task = asyncio.create_task(execute())
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        task.cancel("cancel_verify")
        release.set()
        return await task
    try:
        if unsettled:
            with pytest.raises(BaseException) as observed:
                await attempt()
            if failure in ("verify_close", "dispose_close"):
                assert "file close not confirmed" in str(observed.value)
                assert ("SHA-256" if failure == "verify_close" else "cancel_verify") in str(observed.value)
            status, result = owned.connection.execute("SELECT status,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
                (ticket.run_id, ticket.attempt_id)).fetchone()
            if failure in ("verify_close", "dispose_close"):
                assert len(close_calls) == 1, "关闭失败不能盲目重试 fd"
                assert os.fstat(close_calls[0]).st_size == len(RAW)
            assert status == 1 and result is None, "没有退出事实时原 READ 不能保存 read_returned"
            assert running_read_ticket(owned, prepared.copy_id)[0] == ticket
            intent = AttemptIntent("read", command.action_id, OperationKind.READ_FILE,
                AttemptTarget(copy_id=prepared.copy_id), None, AttemptConfig(5, Decimal("10"), Decimal("0")), runtime.occurred_at(), copy_round=1)
            assert acquire_read_attempt(runtime, intent) == (None, "recovery_blocked")
            assert work.exists()
            assert not await recover_download(staging, ticket), "实际下载拥有者仍须保护目录"
            assert len(transport.calls) == 3
            assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone()[0] == 1
            assert owned.connection.execute("SELECT slot_device_id FROM file_copies WHERE id=?", (prepared.copy_id,)).fetchone()[0] is not None
        else:
            await execute()
            status, result = owned.connection.execute("SELECT status,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
                (ticket.run_id, ticket.attempt_id)).fetchone()
            assert status == 3
            assert json.loads(result)["settlement"]["evidence"]["type"] == "read_returned"
            stored_error = json.loads(owned.connection.execute(
                "SELECT error_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
                (ticket.run_id, ticket.attempt_id)).fetchone()[0])
            assert stored_error["code"] == "device_error"
            assert ("exec not found" if failure == "not_started" else "17") in stored_error["details"]["reason"]
            if failure == "exited_cleanup":
                assert "download cleanup denied" in stored_error["details"]["reason"]
                assert work.exists()
            else:
                assert not work.exists()
            assert RAW.decode("latin1") not in stored_error["details"]["reason"]
            intent = AttemptIntent("read", command.action_id, OperationKind.READ_FILE,
                AttemptTarget(copy_id=prepared.copy_id), None, AttemptConfig(5, Decimal("10"), Decimal("0")), runtime.occurred_at(), copy_round=1)
            next_ticket, reason = acquire_read_attempt(runtime, intent)
            assert reason is None and next_ticket.attempt_id == ticket.attempt_id + 1
            from camctl.devices.read_session import ReadPreparationError, ReadPreparationSettlement
            with pytest.raises(ReadPreparationError) as next_failure:
                await sessions.for_attempt(next_ticket, idle_timeout_s=Decimal("10")).open_session(501, 0)
            assert next_failure.value.settlement is ReadPreparationSettlement.SETTLED
            assert len(transport.calls) == 6
    finally:
        # 测试 transport 已实际返回；测试自行承担注入的不确定调用收场。
        if close_calls:
            real_close(close_calls[0])
        with downloads._ACTIVE_LOCK:
            downloads._ACTIVE.pop(work, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("read_request", ["device", "internal"], indirect=True)
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("waiting_for", ["existing_directory", "prepare_denied"])
async def test_maintenance_waiting_keeps_original_read_even_when_user_cancelled(granted, tmp_path, cancelled, waiting_for, monkeypatch):
    from camctl.devices.read_session import ReadPreparationError, ReadPreparationSettlement
    from camctl.capture.input_copy import InputPhase
    owned, command, _, prepared = granted
    _later_attempt(owned, command, prepared)
    ticket, _ = running_read_ticket(owned, prepared.copy_id)
    driver, transport, staging = harness(tmp_path)
    for name in ("deliveries", "recording-inputs"):
        (staging / name).mkdir()
    work = staging / "read-downloads" / f"{ticket.run_id}-{ticket.attempt_id}"
    work.mkdir(parents=True)
    (work / "old").write_bytes(b"old responsibility")
    if waiting_for == "prepare_denied":
        from camctl.host_files.downloads import DownloadWorkspace
        def denied(_workspace):
            raise PermissionError("download workspace preparation denied")
        monkeypatch.setattr(DownloadWorkspace, "_prepare", denied)
    fixed = source()
    owned.connection.execute("UPDATE device_files SET identity_key=?,locator_json=?,size_bytes=? WHERE id=501",
        (fixed.file_id, json.dumps(dict(fixed.locator)), len(RAW)))
    owned.connection.execute("UPDATE file_copies SET source_size=? WHERE id=?", (len(RAW), prepared.copy_id))
    owned.connection.commit()
    runtime = SimpleNamespace(owned=owned, directories=BoundDirectories(staging), segment_size=5,
        file_executor=None, pending_read_results={}, pending_read_ends={}, pending_read_business={},
        continuing_read_tickets={}, operations=OperationRepository(), occurred_at=lambda: command.occurred_at + 20,
        retry_gate=RetryWaitGate(), monotonic_ns=lambda: 0, max_read_attempts=1, evidence=EVIDENCE)
    sessions = DriverReadSessions(owned, driver, ticket, idle_timeout_s=Decimal("10"))
    if command.processing_id is not None:
        copies = RecordingInputCopies(owned, runtime.directories, runtime.occurred_at)
        step = await obtain_recording_input(InputContext(command.action_id, command.processing_id,
            501, "part", command.config, 5, staging, copies, sessions, runtime.occurred_at()))
    else:
        facts = {"id": prepared.copy_id, "source_device_file_id": 501, "delivery_status": 1}
        step = await _copy_delivery(runtime, sessions, facts, None)
    assert step.phase is InputPhase.WAITING and step.preparation_settlement is ReadPreparationSettlement.WAITING
    if cancelled:
        owned.connection.execute("UPDATE actions SET cancel_requested=1 WHERE id=?", (command.action_id,))
        owned.connection.commit()
    with pytest.raises(ReadPreparationError) as delivered:
        if command.processing_id is not None:
            _finish_internal_read(runtime, ticket, step)
        else:
            _finish_read_attempt(runtime, SimpleNamespace(evidence=EVIDENCE, max_read_attempts=1), facts, ticket, step)
    assert delivered.value is step.error, "向会话交付原本地前提错误，保留完整诊断"
    assert ("preparation denied" if waiting_for == "prepare_denied" else str(work)) in str(delivered.value)
    assert step.phase is InputPhase.WAITING and step.preparation_settlement is ReadPreparationSettlement.WAITING
    assert running_read_ticket(owned, prepared.copy_id)[0] == ticket
    assert not transport.calls
    assert (work / "old").read_bytes() == b"old responsibility"
    assert not runtime.pending_read_results


def test_ordinary_business_waiting_is_not_a_preparation_error():
    from camctl.capture.input_copy import InputPhase, InputStep
    from camctl.outputs.read_attempts import require_preparation_settled

    assert require_preparation_settled(InputStep(InputPhase.WAITING)) is True

"""原 READ 责任限定下载遗留回收；不依赖当前相机配置。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.capture.recovery import RecoveryBoundary, recovery_registry
from camctl.contracts.values import new_operation_key
from camctl.devices.bindings import DeviceBinding
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
from camctl.devices.drivers.hasselblad.driver import HasselbladDriver
from camctl.host_files.models import BoundDirectories
from camctl.outputs.read_attempts import recover_unavailable_read, running_read_ticket
from camctl.persistence.repositories.operations import OperationRepository
from camctl.operations.attempts import AttemptFinish
from camctl.operations.models import CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis, ErrorValue
from camctl.operations.validation import validate_outcome
from camctl.devices.drivers.hasselblad.driver import EVIDENCE
from camctl.persistence.models import DbOutcomeKind

from ..outputs.test_grant_reuse import granted, read_request, read_targets, local_read, _later_attempt


@pytest.fixture
def original_read(granted, tmp_path):
    owned, command, _key, prepared = granted
    _later_attempt(owned, command, prepared)
    ticket, intent = running_read_ticket(owned, prepared.copy_id)
    owned.connection.execute("UPDATE actions SET driver_id=? WHERE id=11", (DRIVER_ID,))
    owned.connection.commit()
    staging = tmp_path / "staging"
    work = staging / "read-downloads" / f"{ticket.run_id}-{ticket.attempt_id}"
    work.mkdir(parents=True)
    (work / "partial").write_bytes(b"partial")
    return owned, ticket, intent, staging, work


@pytest.mark.parametrize("read_request", ["device"], indirect=True)
@pytest.mark.parametrize("boundary", [RecoveryBoundary.HOST_LOCAL_SETTLED, RecoveryBoundary.HOST_POWER_CYCLE])
def test_unconfigured_original_read_saves_local_recovery_evidence(original_read, boundary):
    owned, ticket, intent, staging, _work = original_read
    driver = HasselbladDriver({}, BoundDirectories(staging), monotonic_ns=lambda: 0)
    runtime = SimpleNamespace(owned=owned, pending_read_results={}, pending_read_ends={},
        continuing_read_tickets={}, recovery_boundary=boundary, recovery_max_event_id=intent,
        recovery_evidence_for=lambda binding, operation: recovery_registry(driver, operation),
        operations=OperationRepository(), on_recovery_diagnostic=None, last_recovery_diagnostic=None)
    assert recover_unavailable_read(runtime, int(ticket.target_id), DeviceBinding("cam-1", DRIVER_ID), 1_750_000_000_000_020)
    result = owned.connection.execute("SELECT result_json FROM operation_attempts WHERE run_id=?", (ticket.run_id,)).fetchone()[0]
    assert json.loads(result)["settlement"]["evidence"]["type"] == "local_read_recovery"


@pytest.mark.asyncio
@pytest.mark.parametrize("read_request", ["device"], indirect=True)
@pytest.mark.parametrize("boundary,horizon_delta,removed", [
    (RecoveryBoundary.HOST_LOCAL_SETTLED, 0, True),
    (RecoveryBoundary.HOST_POWER_CYCLE, 0, True),
    (RecoveryBoundary.UNCONFIRMED, 0, False),
    (RecoveryBoundary.HOST_LOCAL_SETTLED, -1, False),
])
async def test_cleanup_requires_original_ticket_and_confirmed_startup_boundary(original_read, boundary, horizon_delta, removed):
    import importlib.util
    assert importlib.util.find_spec("camctl.bootstrap.download_cleanup") is not None, "启动维护应提供原 READ 下载责任核实"
    from camctl.bootstrap import download_cleanup

    owned, ticket, intent, staging, work = original_read
    assert hasattr(download_cleanup, "ReadDownloadCleanup"), "启动维护应核实原 READ 下载责任"
    cleanup = download_cleanup.ReadDownloadCleanup(staging, limit=10, boundary=boundary)
    cleanup.initialize(owned, horizon=intent + horizon_delta)
    await cleanup.run(owned)
    assert work.exists() is not removed
    assert running_read_ticket(owned, int(ticket.target_id))[0] == ticket


@pytest.mark.parametrize("read_request", ["device"], indirect=True)
@pytest.mark.parametrize("boundary,delta", [(RecoveryBoundary.UNCONFIRMED, 0),
    (RecoveryBoundary.HOST_LOCAL_SETTLED, -1), (RecoveryBoundary.HOST_POWER_CYCLE, -1)])
def test_unconfigured_read_cannot_save_recovery_without_original_boundary(original_read, boundary, delta):
    owned, ticket, intent, staging, _work = original_read
    driver = HasselbladDriver({}, BoundDirectories(staging), monotonic_ns=lambda: 0)
    runtime = SimpleNamespace(owned=owned, pending_read_results={}, pending_read_ends={},
        continuing_read_tickets={}, recovery_boundary=boundary, recovery_max_event_id=intent + delta,
        recovery_evidence_for=lambda binding, operation: recovery_registry(driver, operation),
        operations=OperationRepository(), on_recovery_diagnostic=None, last_recovery_diagnostic=None)
    assert not recover_unavailable_read(runtime, int(ticket.target_id), DeviceBinding("cam-1", DRIVER_ID), 1_750_000_000_000_020)
    assert running_read_ticket(owned, int(ticket.target_id))[0] == ticket


@pytest.mark.asyncio
@pytest.mark.parametrize("read_request", ["device"], indirect=True)
async def test_completed_read_is_cleaned_without_camera_or_recovery_assumption(original_read):
    from camctl.bootstrap.download_cleanup import ReadDownloadCleanup
    owned, ticket, intent, staging, work = original_read
    result = CallOutcome(effect=EffectState.UNKNOWN,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("read_returned", 1, {})))
    receipt = OperationRepository().finish_attempt(AttemptFinish(ticket,
        validate_outcome(ticket, result, EVIDENCE), 1_750_000_000_000_020), new_operation_key(), owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    unknown = staging / "read-downloads" / "999-1"
    unknown.mkdir()
    (unknown / "keep").write_bytes(b"unknown")
    cleanup = ReadDownloadCleanup(staging, limit=10, boundary=RecoveryBoundary.UNCONFIRMED)
    cleanup.initialize(owned, horizon=intent)
    await cleanup.run(owned)
    assert not work.exists()
    assert (unknown / "keep").read_bytes() == b"unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("read_request", ["device"], indirect=True)
async def test_current_actual_download_owner_prevents_recovery_deletion(original_read):
    from camctl.bootstrap.download_cleanup import ReadDownloadCleanup
    from camctl.host_files.downloads import DownloadWorkspace
    owned, ticket, intent, staging, work = original_read
    (work / "partial").unlink()
    work.rmdir()
    workspace = DownloadWorkspace(staging, ticket)
    await workspace.prepare()
    (work / "partial").write_bytes(b"actual open download")
    cleanup = ReadDownloadCleanup(staging, limit=10, boundary=RecoveryBoundary.HOST_LOCAL_SETTLED)
    cleanup.initialize(owned, horizon=intent)
    try:
        await cleanup.run(owned)
        assert (work / "partial").read_bytes() == b"actual open download"
    finally:
        await workspace.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("read_request", ["device"], indirect=True)
async def test_work_file_runtime_cleans_original_download_before_media_resume(original_read):
    from camctl.bootstrap.download_cleanup import ReadDownloadCleanup
    from camctl.bootstrap.work_file_assembly import WorkFileRuntime
    from camctl.outputs.work_files import WorkFileLimits
    owned, _ticket, _intent, staging, work = original_read
    runtime = WorkFileRuntime(staging=staging, limits=WorkFileLimits(10, 10),
        download_cleanup=ReadDownloadCleanup(staging, limit=10, boundary=RecoveryBoundary.HOST_LOCAL_SETTLED))
    runtime.initialize(owned)
    await runtime.resume_media_results(owned)
    assert not work.exists()
    await runtime.settle()


@pytest.mark.asyncio
@pytest.mark.parametrize("read_request", ["device"], indirect=True)
async def test_unknown_prefix_never_hides_known_responsibility_across_runs(original_read):
    from camctl.bootstrap.download_cleanup import ReadDownloadCleanup
    owned, ticket, intent, staging, work = original_read
    payload = (work / "partial").read_bytes()
    (work / "partial").unlink()
    work.rmdir()
    unknown = work.parent / "unknown-keep"
    unknown.mkdir()
    (unknown / "keep").write_bytes(b"private unknown bytes")
    work.mkdir()
    (work / "partial").write_bytes(payload)
    for _ in range(3):
        cleanup = ReadDownloadCleanup(staging, limit=1, boundary=RecoveryBoundary.HOST_LOCAL_SETTLED)
        cleanup.initialize(owned, horizon=intent)
        await cleanup.run(owned)
        if not work.exists():
            break
    assert not work.exists(), "未知目录永久保留时，已知责任仍必须可达"
    assert (unknown / "keep").read_bytes() == b"private unknown bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize("read_request", ["device"], indirect=True)
async def test_last_budget_original_ticket_waits_for_maintenance_then_resumes(granted, tmp_path):
    from camctl.bootstrap.download_cleanup import ReadDownloadCleanup
    from camctl.capture.media_flow import DriverReadSessions
    from camctl.capture.input_copy import InputPhase
    from camctl.devices.read_session import ReadPreparationError
    from camctl.persistence.repositories.outputs import OutputsRepository
    from camctl.operations.attempts import AttemptConfig, AttemptIntent, AttemptTarget, OperationKind, RetryWaitGate
    from camctl.operations.models import AttemptStatus
    from camctl.outputs.obtain_flow import _copy_delivery, _finish_read_attempt
    from camctl.outputs.read_attempts import acquire_read_attempt
    from ..devices.test_hasselblad_download import harness, source, RAW
    from decimal import Decimal
    owned, command, _, prepared = granted
    _later_attempt(owned, command, prepared)
    first, _ = running_read_ticket(owned, prepared.copy_id)
    outcome = CallOutcome(status=AttemptStatus.FAILED, effect=EffectState.UNKNOWN,
        error=ErrorValue("device_error", "read"),
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("read_returned", 1, {})))
    finished = OperationRepository().finish_attempt(AttemptFinish(first,
        validate_outcome(first, outcome, EVIDENCE), command.occurred_at + 2, retry_wait=True), new_operation_key(), owned)
    assert finished.kind is DbOutcomeKind.COMPLETED, finished.error
    _later_attempt(owned, command, prepared)
    ticket, intent = running_read_ticket(owned, prepared.copy_id)
    assert ticket.attempt_id == 2
    driver, transport, staging = harness(tmp_path)
    for name in ("deliveries", "read-downloads"):
        (staging / name).mkdir()
    earlier = staging / "read-downloads" / f"{first.run_id}-{first.attempt_id}"
    earlier.mkdir()
    (earlier / "old").write_bytes(b"earlier settled residue")
    work = staging / "read-downloads" / f"{ticket.run_id}-{ticket.attempt_id}"
    work.mkdir()
    (work / "old").write_bytes(b"old incomplete download")
    fixed = source()
    owned.connection.execute("UPDATE device_files SET identity_key=?,locator_json=?,size_bytes=? WHERE id=501",
        (fixed.file_id, json.dumps(dict(fixed.locator)), len(RAW)))
    owned.connection.execute("UPDATE file_copies SET source_size=? WHERE id=?", (len(RAW), prepared.copy_id))
    owned.connection.execute("UPDATE actions SET driver_id=? WHERE id=11", (DRIVER_ID,))
    owned.connection.commit()
    runtime = SimpleNamespace(owned=owned, directories=BoundDirectories(staging), segment_size=5,
        file_executor=None, pending_read_results={}, pending_read_ends={}, pending_read_business={},
        continuing_read_tickets={}, operations=OperationRepository(), occurred_at=lambda: command.occurred_at + 20,
        retry_gate=RetryWaitGate(), monotonic_ns=lambda: 0,
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED, recovery_max_event_id=intent,
        last_recovery_diagnostic=None, on_recovery_diagnostic=None)
    request = AttemptIntent("read", command.action_id, OperationKind.READ_FILE,
        AttemptTarget(copy_id=prepared.copy_id), None, AttemptConfig(2, Decimal("10"), Decimal("0")), runtime.occurred_at(), copy_round=1)
    runtime.outputs = OutputsRepository()
    facts = {"id": prepared.copy_id, "source_device_file_id": 501, "delivery_status": 1, "delivery_id": prepared.delivery_id}
    # 第一轮额度用于更早的原 READ，最后一次票据的目录尚未轮到。
    cleanup = ReadDownloadCleanup(staging, limit=1, boundary=runtime.recovery_boundary)
    cleanup.initialize(owned, horizon=intent)
    await cleanup.run(owned)
    assert work.exists(), "每 run 不能越过 limit 直接处理后面的责任"
    assert acquire_read_attempt(runtime, request) == (ticket, None)
    step = await _copy_delivery(runtime, DriverReadSessions(owned, driver, ticket, idle_timeout_s=Decimal("10")), facts, None)
    with pytest.raises(ReadPreparationError) as waiting:
        _finish_read_attempt(runtime, SimpleNamespace(evidence=EVIDENCE, max_read_attempts=2), facts, ticket, step)
    assert waiting.value is step.error
    assert step.phase is InputPhase.WAITING
    assert running_read_ticket(owned, prepared.copy_id)[0] == ticket
    assert not transport.calls, "未满足本地维护前提时不能派发相机调用"
    assert owned.connection.execute("SELECT status FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone()[0] != 4
    # 下一正常 run 从已保存游标继续，按原责任清理后仍用原最后一次 ticket。
    cleanup = ReadDownloadCleanup(staging, limit=1, boundary=runtime.recovery_boundary)
    cleanup.initialize(owned, horizon=intent)
    await cleanup.run(owned)
    assert not work.exists()
    assert acquire_read_attempt(runtime, request) == (ticket, None)
    session = await driver.open_read(fixed, 0, ticket, idle_timeout_s=Decimal("10"))
    session.request_stop()
    assert (await session.wait_stopped()).stopped
    assert len(transport.calls) == 3
    assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone()[0] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("read_request", ["device"], indirect=True)
@pytest.mark.parametrize("blocked", ["active", "unconfirmed", "missing_horizon", "late", "delete_failure"])
async def test_bounded_cursor_advances_past_retained_or_failed_known_responsibility(original_read, granted, monkeypatch, blocked):
    from dataclasses import replace
    from camctl.bootstrap import download_cleanup
    from camctl.host_files import downloads
    from ..outputs.test_grant_reuse import _save_grant
    owned, first, intent, staging, first_work = original_read
    command = granted[1]
    owned.connection.execute("UPDATE actions SET driver_id=?,status=3 WHERE id=12", (DRIVER_ID,))
    owned.connection.commit()
    second_command = replace(command, action_id=32, item_id=102, output_id=702, source_device_file_id=502)
    _, _, _, second_copy = _save_grant(owned, second_command)
    from camctl.operations.attempts import AttemptConfig, AttemptIntent, AttemptTarget, OperationKind
    from decimal import Decimal
    owned.connection.execute("UPDATE actions SET device_id='cam-2' WHERE id=12")
    owned.connection.execute("UPDATE file_copies SET slot_device_id='cam-2' WHERE id=?", (second_copy.copy_id,))
    owned.connection.commit()
    begun = OperationRepository().begin_attempt(AttemptIntent("read", 32, OperationKind.READ_FILE,
        AttemptTarget(copy_id=second_copy.copy_id), None, AttemptConfig(5, Decimal("10"), Decimal("0")),
        command.occurred_at + 10, copy_round=1), new_operation_key(), owned)
    assert begun.kind is DbOutcomeKind.COMPLETED, begun.error
    second, _ = running_read_ticket(owned, second_copy.copy_id)
    outcome = CallOutcome(effect=EffectState.UNKNOWN,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("read_returned", 1, {})))
    result = OperationRepository().finish_attempt(AttemptFinish(second,
        validate_outcome(second, outcome, EVIDENCE), command.occurred_at + 20), new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    second_work = staging / "read-downloads" / f"{second.run_id}-{second.attempt_id}"
    second_work.mkdir()
    (second_work / "settled").write_bytes(b"completed residue")
    active = downloads.DownloadWorkspace(staging, first)
    if blocked == "active":
        assert active.claim()
    boundary = RecoveryBoundary.UNCONFIRMED if blocked == "unconfirmed" else RecoveryBoundary.HOST_LOCAL_SETTLED
    horizon = None if blocked == "missing_horizon" else (intent - 1 if blocked == "late" else intent)
    real_remove = downloads.shutil.rmtree
    def remove(path):
        if path == first_work and blocked == "delete_failure":
            raise PermissionError("original directory delete denied")
        return real_remove(path)
    monkeypatch.setattr(downloads.shutil, "rmtree", remove)
    actual_recover = download_cleanup.recover_download
    examined = []
    async def record(staging, ticket):
        examined.append(ticket)
        return await actual_recover(staging, ticket)
    monkeypatch.setattr(download_cleanup, "recover_download", record)
    try:
        for run in range(2):
            before = len(examined)
            cleanup = download_cleanup.ReadDownloadCleanup(staging, limit=1, boundary=boundary)
            cleanup.initialize(owned, horizon=horizon)
            if blocked == "delete_failure" and run == 0:
                with pytest.raises(PermissionError, match="delete denied"):
                    await cleanup.run(owned)
            else:
                await cleanup.run(owned)
            assert len(examined) - before <= 1
            if run == 0:
                assert first_work.exists() and second_work.exists()
        assert first_work.exists() and not second_work.exists()
        assert (first_work / "partial").read_bytes() == b"partial"
        # 完成一圈后下一 run 回绕，先前保留项并未永久丢失。
        active.release()
        monkeypatch.setattr(downloads.shutil, "rmtree", real_remove)
        cleanup = download_cleanup.ReadDownloadCleanup(staging, limit=1, boundary=RecoveryBoundary.HOST_LOCAL_SETTLED)
        cleanup.initialize(owned, horizon=intent)
        await cleanup.run(owned)
        assert not first_work.exists()
    finally:
        active.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("read_request", ["device"], indirect=True)
async def test_cursor_write_failure_is_a_real_maintenance_error(original_read, monkeypatch):
    from camctl.bootstrap.download_cleanup import ReadDownloadCleanup
    from camctl.host_files import downloads
    owned, ticket, intent, staging, work = original_read
    def fail_replace(*_args):
        raise PermissionError("cursor replace denied")
    monkeypatch.setattr(downloads.os, "replace", fail_replace)
    cleanup = ReadDownloadCleanup(staging, limit=1, boundary=RecoveryBoundary.HOST_LOCAL_SETTLED)
    cleanup.initialize(owned, horizon=intent)
    with pytest.raises(PermissionError, match="cursor replace denied"):
        await cleanup.run(owned)
    assert not work.exists()
    assert running_read_ticket(owned, int(ticket.target_id))[0] == ticket


@pytest.mark.parametrize("read_request", ["device"], indirect=True)
@pytest.mark.parametrize("configured_roots", [True, False])
def test_known_actual_owner_blocks_both_resume_and_unavailable_recovery(original_read, configured_roots):
    from camctl.host_files.downloads import DownloadWorkspace
    from camctl.outputs.read_attempts import acquire_read_attempt
    from camctl.operations.attempts import AttemptConfig, AttemptIntent, AttemptTarget, OperationKind
    from decimal import Decimal
    owned, ticket, intent, staging, work = original_read
    owner = DownloadWorkspace(staging, ticket)
    assert owner.claim()
    driver = HasselbladDriver({}, BoundDirectories(staging), monotonic_ns=lambda: 0)
    runtime = SimpleNamespace(owned=owned, pending_read_results={}, pending_read_ends={},
        continuing_read_tickets={}, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED, recovery_max_event_id=intent,
        recovery_evidence_for=lambda binding, operation: recovery_registry(driver, operation),
        operations=OperationRepository(), on_recovery_diagnostic=None, last_recovery_diagnostic=None)
    if configured_roots:
        runtime.directories = BoundDirectories(staging)
    request = AttemptIntent("read", 31, OperationKind.READ_FILE, AttemptTarget(copy_id=int(ticket.target_id)),
        None, AttemptConfig(5, Decimal("10"), Decimal("0")), 1_750_000_000_000_020, copy_round=1)
    try:
        assert acquire_read_attempt(runtime, request) == (None, "recovery_blocked")
        assert not recover_unavailable_read(runtime, int(ticket.target_id), DeviceBinding("cam-1", DRIVER_ID), request.occurred_at)
        assert running_read_ticket(owned, int(ticket.target_id))[0] == ticket
        assert (work / "partial").read_bytes() == b"partial"
    finally:
        owner.release()

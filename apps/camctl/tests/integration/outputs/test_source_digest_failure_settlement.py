"""源摘要查询失败的原 READ 结束与交付收场。"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from dataclasses import replace

import pytest

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError
from camctl.capture.recovery import RecoveryBoundary
from camctl.capture.media_flow import run_recording_media
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult
from camctl.devices.read_session import ReadChunk, ReadSession
from camctl.outputs.obtain_flow import advance_obtain
from camctl.persistence.repositories.acceptance import register_acceptance_guards
from camctl.persistence.repositories.cancellation import register_cancellation_guards
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.operations import OperationRepository, register_operation_guards
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.models import DbOutcome, DbOutcomeKind

from .test_qualification import (
    _NOW,
    _seed_selection_and_item,
)
from .test_read_associations import read_targets
from ..support.capture import capture_database
from ..support.outputs import obtain_factory, output_drivers
from .test_cleanup_query_commit_recovery import _CommitFaultConnection

register_acceptance_guards()
register_cancellation_guards()
register_capture_guards()
register_operation_guards()

pytestmark = pytest.mark.asyncio

_CONTENT = b"0123456789"


class _ReadStream:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.position = 0

    def read(self, limit: int) -> bytes:
        data = self.content[self.position:self.position + limit]
        self.position += len(data)
        return data

    def stop(self) -> None:
        return None

    def close(self) -> None:
        return None


class _ReadDriver:
    def __init__(self) -> None:
        self.offsets: list[int] = []
        self.ends = []
        self.recovery_diagnostics = []
        self.digests = {}

    async def open_read(self, source, offset, ticket, *, idle_timeout_s):
        self.offsets.append(offset)
        session = ReadSession(source, offset, _ReadStream(_CONTENT[offset:]), idle_timeout_s)
        wait_stopped = session.wait_stopped
        async def wait():
            end = await wait_stopped()
            self.ends.append(end)
            return end
        session.wait_stopped = wait
        return session

    async def control(self, request):
        raise AssertionError("内部读取不发出新拍摄")

    async def digest(self, request):
        result = await self.digests[int(request.params["file_id"])].read_digest()
        if result.error is not None:
            return DeviceCallResult((), {"reason": result.error})
        return DeviceCallResult((DeviceObservation("file_digest", 1, {
            "file_id": request.params["file_id"], "sha256": result.digest,
        }),), None)



class _Digest:
    def __init__(self, *, digest: str | None = None, error: str | None = None) -> None:
        self.digest = digest
        self.error = error
        self.calls = 0

    async def read_digest(self):
        from camctl.outputs.copy import SourceDigest

        self.calls += 1
        return SourceDigest(digest=self.digest, error=self.error)


async def _runtime(owned, tmp_path, *, seed=True, sibling=True,
             recovery_boundary=RecoveryBoundary.UNCONFIRMED,
             recovery_max_event_id=None):
    connection = owned.connection
    if seed:
        connection.execute("UPDATE actions SET status=3 WHERE id IN (11, 12, 32)")
        connection.execute(
            "UPDATE device_files SET size_bytes=10, checksum_support=2, sha256=NULL"
            " WHERE id IN (501, 502)")
        if sibling:
            _seed_selection_and_item(
                connection, dependency_id=33, selection_id=33, item_id=103,
                owner_action_id=31, source_action_id=12, output_id=702)
        connection.commit()

    staging = tmp_path / "staging"
    for name in ("deliveries", "recording-inputs", "derived"):
        (staging / name).mkdir(parents=True, exist_ok=True)
    ready = tmp_path / "ready"
    processing = tmp_path / "processing"
    ready.mkdir(exist_ok=True)
    processing.mkdir(exist_ok=True)
    device_id, driver_id = connection.execute(
        "SELECT device_id,driver_id FROM actions WHERE id=11").fetchone()
    evidence = EvidenceRegistry((
        EvidenceContract("read_returned", 1, "read", frozenset()),
        EvidenceContract("file_digest", 1, "digest", frozenset({"file_id", "sha256"}), identity_field="file_id"),
        EvidenceContract("local_read_recovery", 1, "read", frozenset()),
    ))
    failed_digest = _Digest(error="source SHA query failed")
    good_digest = _Digest(digest=hashlib.sha256(_CONTENT).hexdigest())
    driver = _ReadDriver()
    driver.digests = {501: failed_digest, 502: good_digest}
    factory = obtain_factory(
        devices={device_id: {"driver": driver_id, "copy": {
            "retry_interval_s": Decimal(0), "max_read_attempts": 3,
            "read_idle_timeout_s": Decimal(10),
        }}},
        drivers=output_drivers(driver, evidence, driver_id=driver_id,
                               read=True, digest=True, local_read_recovery=True),
        staging=staging, ready=ready, processing=processing, segment_size=10,
        monotonic_ns=lambda: 20_000_000_000, occurred_at=lambda: _NOW + 23_000_000,
        recovery_boundary=recovery_boundary,
        recovery_max_event_id=None if recovery_max_event_id is None else lambda: recovery_max_event_id,
        on_recovery_diagnostic=driver.recovery_diagnostics.append,
    )
    runtime = factory(await capture_database(owned))
    return runtime, failed_digest, good_digest, driver


async def test_source_digest_failure_finishes_original_read_and_preserves_sibling_success(
        read_targets, tmp_path):
    owned = read_targets
    runtime, failed_digest, good_digest, _driver = await _runtime(owned, tmp_path)

    await advance_obtain(runtime)

    states = owned.connection.execute(
        "SELECT c.source_device_file_id,c.committed_bytes,c.verification_state,"
        " c.source_sha256,c.target_sha256,c.verification_error_json,"
        " c.recopies_used,d.status,d.error_json,i.source_dependency,c.slot_device_id,"
        " r.status,r.error_json,r.attempts_used,r.retry_wait_required,t.status,t.result_json"
        " FROM file_copies c JOIN deliveries d ON d.id=c.delivery_id"
        " JOIN obtain_items i ON i.delivery_id=d.id"
        " JOIN operation_runs r ON r.copy_id=c.id"
        " JOIN operation_attempts t ON t.run_id=r.id ORDER BY c.source_device_file_id"
    ).fetchall()
    assert len(states) == 2
    failed, good = states
    verification = enum_for("file_copies.verification_state")
    delivery = enum_for("deliveries.status")
    run = enum_for("operation_runs.status")
    attempt = enum_for("operation_attempts.status")

    assert failed[:5] == (
        501, 10, int(verification.FAILED), None,
        hashlib.sha256(_CONTENT).hexdigest(),
    )
    assert parse_exact_json(failed[5]) == {"reason": str({"reason": failed_digest.error})}
    assert failed[6] == 0
    assert failed[7] == int(delivery.FAILED)
    expected_error = {
        "code": "source_file_unconfirmed", "stage": "source_read",
        "details": {"output_id": "701"},
    }
    assert parse_exact_json(failed[8]) == expected_error
    assert failed[9:11] == (0, None)
    assert failed[11] == int(run.FAILED)
    assert parse_exact_json(failed[12]) == expected_error
    assert failed[13:15] == (1, 0)
    assert failed[15] == int(attempt.SUCCEEDED)
    assert failed[16] is not None
    assert failed_digest.calls == 1

    assert good[0:3] == (502, 10, int(verification.MATCHED))
    assert good[7] in (int(delivery.PREPARING), int(delivery.PREPARED))
    assert good[11] == int(run.SUCCEEDED)
    assert good[15] == int(attempt.SUCCEEDED)
    assert good_digest.calls == 1

    await advance_obtain(runtime)
    published = owned.connection.execute(
        "SELECT d.status FROM deliveries d JOIN file_copies c ON c.delivery_id=d.id"
        " WHERE c.source_device_file_id=502").fetchone()[0]
    assert published == int(delivery.PUBLISHED)


async def test_unknown_attempt_finish_reuses_original_key_without_requery(
        read_targets, tmp_path):
    owned = read_targets
    runtime, failed_digest, _good_digest, _driver = await _runtime(owned, tmp_path, sibling=False)
    finish = runtime.operations.finish_attempt
    calls = []

    def unknown_after_commit(request, key, connection):
        if request.run_finish is not None and request.run_finish.error is not None \
                and request.run_finish.error.code == "source_file_unconfirmed":
            calls.append(str(key))
            if len(calls) == 1:
                connection = replace(connection, connection=_CommitFaultConnection(
                    connection.connection, after_commit=True))
            return finish(request, key, connection)
        return finish(request, key, connection)

    runtime.operations.finish_attempt = unknown_after_commit
    with pytest.raises(ConsistencyError, match="原读取结果未可靠保存"):
        await advance_obtain(runtime)
    original_key = calls[0]
    await advance_obtain(runtime)

    assert calls == [original_key, original_key]
    transactions = owned.connection.execute(
        "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
        (original_key,),
    ).fetchone()[0]
    assert transactions == 1
    assert failed_digest.calls == 1
    state = owned.connection.execute(
        "SELECT r.status,t.status,d.status FROM operation_runs r"
        " JOIN operation_attempts t ON t.run_id=r.id"
        " JOIN file_copies c ON c.id=r.copy_id"
        " JOIN deliveries d ON d.id=c.delivery_id"
        " WHERE c.source_device_file_id=501").fetchone()
    assert state == (
        int(enum_for("operation_runs.status").FAILED),
        int(enum_for("operation_attempts.status").SUCCEEDED),
        int(enum_for("deliveries.status").FAILED),
    )


async def test_unknown_delivery_failure_reuses_original_key_without_duplicate_event(
        read_targets, tmp_path):
    owned = read_targets
    runtime, failed_digest, _good_digest, _driver = await _runtime(owned, tmp_path, sibling=False)
    fail_delivery = runtime.outputs.fail_read_delivery
    calls = []

    def unknown_after_commit(request, key, connection):
        calls.append(str(key))
        if len(calls) == 1:
            connection = replace(connection, connection=_CommitFaultConnection(
                connection.connection, after_commit=True))
        return fail_delivery(request, key, connection)

    runtime.outputs.fail_read_delivery = unknown_after_commit
    with pytest.raises(ConsistencyError, match="原读取业务结果未可靠保存"):
        await advance_obtain(runtime)
    original_key = calls[0]
    await advance_obtain(runtime)

    assert calls == [original_key, original_key]
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
        (original_key,),
    ).fetchone()[0] == 1
    assert failed_digest.calls == 1


class _ReadFinishGate(OperationRepository):
    """在原 READ 的真实结果保存入口暂时拒绝，记录完整原申请。"""

    def __init__(self):
        self.enabled = True
        self.calls = []

    def finish_attempt(self, request, key, owned):
        if request.ticket.operation == "read":
            self.calls.append((request, key))
            if self.enabled:
                return DbOutcome(DbOutcomeKind.ROLLED_BACK, error=RuntimeError("READ 保存中断"))
        return super().finish_attempt(request, key, owned)


async def test_cross_process_without_original_read_end_waits_for_boundary_then_confirms_eof(
        read_targets, tmp_path, capture_resources):
    import camctl.outputs.obtain_flow as obtain_flow

    owned = read_targets
    first, failed_digest, _good_digest, _first_driver = await _runtime(
        owned, tmp_path, sibling=False)
    gate = _ReadFinishGate()
    first.operations = gate
    with pytest.raises(ConsistencyError):
        await advance_obtain(first)
    copy_id, = owned.connection.execute(
        "SELECT id FROM file_copies WHERE source_device_file_id=501").fetchone()
    assert _first_driver.ends and all(end.stopped and end.error is None for end in _first_driver.ends)
    assert owned.connection.execute(
        "SELECT verification_state FROM file_copies WHERE id=?", (copy_id,)
    ).fetchone()[0] == int(enum_for("file_copies.verification_state").FAILED)
    assert owned.connection.execute(
        "SELECT status FROM operation_attempts WHERE run_id="
        "(SELECT id FROM operation_runs WHERE copy_id=?)", (copy_id,)
    ).fetchone()[0] == int(enum_for("operation_attempts.status").RUNNING)

    await first.database.close()
    await capture_resources.restart_database(owned)
    unconfirmed, unconfirmed_digest, _good, blocked_driver = await _runtime(
        owned, tmp_path, seed=False, sibling=False,
        recovery_boundary=RecoveryBoundary.UNCONFIRMED)
    await advance_obtain(unconfirmed)
    assert blocked_driver.offsets == []
    assert unconfirmed_digest.calls == 0
    assert blocked_driver.recovery_diagnostics
    assert blocked_driver.recovery_diagnostics[-1].reason.value == "unconfirmed_boundary"
    assert owned.connection.execute(
        "SELECT status FROM operation_attempts WHERE run_id="
        "(SELECT id FROM operation_runs WHERE copy_id=?)", (copy_id,)
    ).fetchone()[0] == int(enum_for("operation_attempts.status").RUNNING)

    horizon, = owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()
    recovered, recovered_digest, _good, recovered_driver = await _runtime(
        owned, tmp_path, seed=False, sibling=False,
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
        recovery_max_event_id=horizon)
    await advance_obtain(recovered)
    assert recovered_driver.offsets == [10]
    assert failed_digest.calls == 1
    assert recovered_digest.calls == 0
    assert owned.connection.execute(
        "SELECT status FROM operation_attempts WHERE run_id="
        "(SELECT id FROM operation_runs WHERE copy_id=?)", (copy_id,)
    ).fetchone()[0] == int(enum_for("operation_attempts.status").SUCCEEDED)


async def test_held_clean_read_end_retries_saved_verification_failure_without_digest_requery(
        read_targets, tmp_path, capture_resources):
    import camctl.outputs.obtain_flow as obtain_flow

    owned = read_targets
    runtime, failed_digest, _good_digest, driver = await _runtime(
        owned, tmp_path, sibling=False)
    gate = _ReadFinishGate()
    runtime.operations = gate
    with pytest.raises(ConsistencyError):
        await advance_obtain(runtime)
    copy_id, = owned.connection.execute(
        "SELECT id FROM file_copies WHERE source_device_file_id=501").fetchone()
    assert driver.ends and all(end.stopped and end.error is None for end in driver.ends)
    assert owned.connection.execute(
        "SELECT status FROM operation_attempts WHERE run_id="
        "(SELECT id FROM operation_runs WHERE copy_id=?)", (copy_id,)
    ).fetchone()[0] == int(enum_for("operation_attempts.status").RUNNING)

    gate.enabled = False
    await advance_obtain(runtime)

    assert driver.offsets == [0]
    assert failed_digest.calls == 1
    state = owned.connection.execute(
        "SELECT r.status,t.status,d.status FROM operation_runs r"
        " JOIN operation_attempts t ON t.run_id=r.id"
        " JOIN file_copies c ON c.id=r.copy_id"
        " JOIN deliveries d ON d.id=c.delivery_id"
        " WHERE c.id=?", (copy_id,)).fetchone()
    assert state == (
        int(enum_for("operation_runs.status").FAILED),
        int(enum_for("operation_attempts.status").SUCCEEDED),
        int(enum_for("deliveries.status").FAILED),
    )


async def test_reliable_cancellation_wins_after_saved_source_digest_failure(
        read_targets, tmp_path, capture_resources, monkeypatch):
    import camctl.outputs.obtain_flow as obtain_flow

    owned = read_targets
    runtime, failed_digest, _good_digest, _driver = await _runtime(
        owned, tmp_path, sibling=False)
    from ..bootstrap.test_output_binding_changes import Catalog
    from ..support.outputs import prepare_cancel_target
    save_verification = OutputsRepository.save_verification
    applied = False

    def cancel_after_verification(repository, request, key, current):
        nonlocal applied
        receipt = save_verification(repository, request, key, current)
        if receipt.kind is DbOutcomeKind.COMPLETED and not applied:
            applied = True
            prepare_cancel_target(current, catalog=Catalog(), now=_NOW + 23_000_000, action_id=31)
        return receipt

    monkeypatch.setattr(OutputsRepository, "save_verification", cancel_after_verification)
    await advance_obtain(runtime)

    row = owned.connection.execute(
        "SELECT r.status,r.error_json,t.status,d.status,d.error_json,"
        " i.source_dependency,c.slot_device_id"
        " FROM operation_runs r JOIN operation_attempts t ON t.run_id=r.id"
        " JOIN file_copies c ON c.id=r.copy_id"
        " JOIN deliveries d ON d.id=c.delivery_id"
        " JOIN obtain_items i ON i.delivery_id=d.id"
        " WHERE c.source_device_file_id=501").fetchone()
    assert row[0] == int(enum_for("operation_runs.status").CANCELED)
    assert row[1] is None
    assert row[2] == int(enum_for("operation_attempts.status").SUCCEEDED)
    assert row[3] != int(enum_for("deliveries.status").FAILED)
    assert row[4] is None
    assert row[5] == 1
    assert row[6] is not None
    assert failed_digest.calls == 1


async def test_internal_read_finishes_successfully_and_fails_processing_on_source_digest_error(
        read_targets, tmp_path):
    from camctl.capture.processing import CheckPhase

    owned = read_targets
    connection = owned.connection
    connection.execute("UPDATE actions SET status=3 WHERE id IN (12, 32)")
    connection.execute(
        "UPDATE device_files SET size_bytes=10, checksum_support=2, sha256=NULL"
        " WHERE id=501")
    connection.execute(
        "UPDATE recording_processing SET check_basis_json=? WHERE id=5",
        ('{"reason":2,"target_duration_ms":60000}',))
    connection.commit()
    staging = tmp_path / "staging"
    for name in ("deliveries", "recording-inputs", "derived"):
        (staging / name).mkdir(parents=True)
    digest = _Digest(error="internal source SHA query failed")

    class _NoTools:
        async def probe(self, _input):
            raise AssertionError("来源摘要失败后不得检查未确认副本")

        async def repair(self, _input, _output, *, trim_s):
            raise AssertionError("来源摘要失败后不得修复未确认副本")

    from camctl.bootstrap.capture_assembly import session_capture_assembly
    from camctl.capture.timelapse import CaptureWaitConfig
    from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
    from camctl.devices.ports import DriverDeclaration

    driver = _ReadDriver()
    driver.digests = {501: digest}
    evidence = EvidenceRegistry((
        EvidenceContract("read_returned", 1, "read", frozenset()),
        EvidenceContract("file_digest", 1, "digest", frozenset({"file_id", "sha256"}), identity_field="file_id"),
    ))
    drivers = DriverRegistry((DriverEntry("camctl-adb", driver, DriverDeclaration(
        control_supported=True, stop_supported=False, query_supported=False,
        result_supported=False, read_supported=True, digest_supported=True, delete_supported=False),
        evidence, DriverStatus.SOFTWARE_CONTRACT_VERIFIED),))
    factory = session_capture_assembly(
        devices={"cam-1": {"driver": "camctl-adb", "copy": {"retry_interval_s": Decimal(0)}}},
        drivers=drivers, staging=staging, segment_size=10,
        wall_us=lambda: _NOW + 30_000_000,
        wait_config=lambda _action: CaptureWaitConfig(60000, 0),
    )
    runtime = await factory(await capture_database(owned), "cam-1")
    assert runtime is not None and runtime.media is not None
    flow = runtime.media
    flow.tools = _NoTools()

    step = await run_recording_media(flow, 11, 5, 501)

    assert step.phase.value == "verification_failed"
    copy_id, = connection.execute(
        "SELECT id FROM file_copies WHERE processing_id=5").fetchone()
    row = connection.execute(
        "SELECT c.committed_bytes,c.verification_state,c.source_sha256,c.target_sha256,"
        " c.verification_error_json,r.status,r.error_json,a.status,a.result_json,"
        " c.slot_device_id"
        " FROM file_copies c JOIN operation_runs r ON r.copy_id=c.id"
        " JOIN operation_attempts a ON a.run_id=r.id WHERE c.id=?", (copy_id,)
    ).fetchone()
    assert row[:4] == (
        10, int(enum_for("file_copies.verification_state").FAILED), None,
        hashlib.sha256(_CONTENT).hexdigest(),
    )
    assert parse_exact_json(row[4]) == {"reason": str({"reason": digest.error})}
    assert row[5] == int(enum_for("operation_runs.status").FAILED)
    assert parse_exact_json(row[6]) == {
        "code": "source_file_unconfirmed", "stage": "source_read", "details": {}}
    assert row[7] == int(enum_for("operation_attempts.status").SUCCEEDED)
    assert row[8] is not None
    assert row[9] is None
    assert connection.execute(
        "SELECT check_state FROM recording_processing WHERE id=5").fetchone()[0] == CheckPhase.FAILED.value
    assert digest.calls == 1

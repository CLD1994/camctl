"""真实完整 ReadEnd 已持有时，绑定缺失不能以 UNKNOWN 覆盖原读取。"""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.bootstrap.capture_assembly import session_capture_assembly
from camctl.bootstrap.obtain_assembly import session_obtain_assembly
from camctl.capture.handlers import capture_handler
from camctl.capture.media_flow import RecordingInputCopies, run_recording_media
from camctl.capture.recovery import RecoveryBoundary
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.contracts.values import ConsistencyError
from camctl.contracts.enums import enum_for
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DriverDeclaration
from camctl.outputs.copy import CopyCompletionError
from camctl.outputs.obtain_flow import advance_obtain

from ..capture.test_capture_contract import ResultsDouble
from .test_media_assembly import _SessionDriver, _EVIDENCE
from .test_output_binding_changes import _CONTENT, _NOW, _registry, environment
from .test_output_binding_transactions import pending_read
from .test_read_execution_runtime import ReadRequests, _MEDIA_CONTENT, _SETTINGS, _real_recording_media_flow

pytestmark = pytest.mark.asyncio


def _fail_local_completion_once(monkeypatch, *, source_checksum_supported=False):
    complete = RecordingInputCopies.complete
    completed = []

    async def first_unavailable(copies, copy_id, digest):
        completed.append(copy_id)
        if len(completed) == 1 and not source_checksum_supported:
            raise CopyCompletionError('target_sha256', '本地摘要读取暂不可用')
        return await complete(copies, copy_id, digest)

    monkeypatch.setattr(RecordingInputCopies, 'complete', first_unavailable)
    if source_checksum_supported:
        from camctl.persistence.repositories.outputs import OutputsRepository
        from camctl.persistence.models import DbOutcomeKind
        from ..cancellation.test_report_sync_lifecycle import _fault_owned
        original = OutputsRepository.save_prepared
        prepared = []

        def fail_after_source_saved(repository, request, key, owned):
            prepared.append(request)
            if len(prepared) == 1:
                faulty = _fault_owned(owned, 'UPDATE intermediate_files')
                result = original(repository, request, key, faulty)
                assert faulty.connection.failed and result.kind is DbOutcomeKind.ROLLED_BACK
                return result
            return original(repository, request, key, owned)

        monkeypatch.setattr(OutputsRepository, 'save_prepared', fail_after_source_saved)
    return completed


def _held_original(owned, runtime, reader, *, source_checksum_supported=False):
    assert len(reader.requests) == 1
    ticket = reader.requests[0][0]
    assert owned.connection.execute('SELECT status,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?',
        (ticket.run_id, ticket.attempt_id)).fetchone() == (1, None)
    progress = owned.connection.execute('SELECT committed_bytes,source_size,round,recopies_used FROM file_copies WHERE id=?',
        (int(ticket.target_id),)).fetchone()
    assert progress[0] == progress[1]
    held = runtime.pending_read_ends[int(ticket.target_id)]
    assert held.ticket == ticket and held.end.stopped and held.end.error is None
    assert runtime.pending_read_results == {}
    # 源端能力来自真实声明登记；支持时所需摘要已由真实 digest 保存。
    support = enum_for('device_files.checksum_support')
    assert owned.connection.execute('SELECT f.checksum_support FROM file_copies c JOIN device_files f ON f.id=c.source_device_file_id WHERE c.id=?',
        (int(ticket.target_id),)).fetchone() == (int(support.SUPPORTED if source_checksum_supported else support.UNSUPPORTED),)
    if source_checksum_supported:
        import hashlib
        assert owned.connection.execute('SELECT source_sha256,target_sha256 FROM file_copies WHERE id=?',
            (int(ticket.target_id),)).fetchone() == (hashlib.sha256(reader.content).hexdigest(),) * 2
    return ticket, progress


def _actual_result_saved(owned, reader, ticket, progress, completions):
    import json
    assert len(completions) == 2
    row = owned.connection.execute('SELECT status,error_json,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?',
        (ticket.run_id, ticket.attempt_id)).fetchone()
    assert row[0] == 2 and row[1] is None
    result = json.loads(row[2])
    assert result['settlement'] == {'basis': 'observed', 'evidence': {'type': 'read_returned', 'version': 1, 'data': {}}}
    assert len(reader.requests) == 1
    assert owned.connection.execute('SELECT attempts_used FROM operation_runs WHERE id=?',
        (ticket.run_id,)).fetchone() == (ticket.attempt_id,)
    assert owned.connection.execute('SELECT committed_bytes,source_size,round,recopies_used FROM file_copies WHERE id=?',
        (int(ticket.target_id),)).fetchone() == progress


async def test_obtain_held_actual_end_binding_loss_checks_locally_without_unknown_or_new_read(pending_read, monkeypatch):
    cfg, owned, _binding = pending_read
    reader = ReadRequests(owned, _CONTENT)
    entry = _registry(reader).entry('camctl-adb')
    from .test_output_binding_changes import _EVIDENCE as ordinary_evidence
    entry = replace(entry, declaration=replace(entry.declaration,
        adb_foreground_recovery_operations=frozenset({'read'})), evidence=EvidenceRegistry(tuple(
            ordinary_evidence.contract(name, 1) for name in ('operation_returned', 'photo_taken', 'results_returned', 'result_files_listed',
                'read_returned', 'delete_returned', 'file_absent', 'file_presence')) + (
                    EvidenceContract('adb_foreground_recovery', 1, 'read', frozenset()),)))
    devices = {key: {**value, 'copy': _SETTINGS} for key, value in cfg.devices.items()}
    horizon = [None]
    factory = session_obtain_assembly(devices=devices, drivers=DriverRegistry((entry,)),
        staging=Path(cfg.paths.staging), ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing),
        segment_size=4, occurred_at=lambda: _NOW, monotonic_ns=lambda: 0,
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED, recovery_max_event_id=lambda: horizon[0])
    runtime = factory(owned)
    completions = _fail_local_completion_once(monkeypatch)
    with pytest.raises(ConsistencyError):
        await advance_obtain(runtime)
    ticket, progress = _held_original(owned, runtime, reader)
    source_device = owned.connection.execute('SELECT a.device_id FROM file_copies c JOIN device_files f ON f.id=c.source_device_file_id'
        ' JOIN actions a ON a.id=f.observer_action_id WHERE c.id=?', (int(ticket.target_id),)).fetchone()[0]
    horizon[0] = owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()[0]
    devices.pop(source_device)
    await advance_obtain(factory(owned))
    _actual_result_saved(owned, reader, ticket, progress, completions)
    await advance_obtain(factory(owned))
    delivery_id, status, error = owned.connection.execute('SELECT d.id,d.status,d.error_json FROM file_copies c JOIN deliveries d ON d.id=c.delivery_id WHERE c.id=?',
        (int(ticket.target_id),)).fetchone()
    assert status == int(enum_for('deliveries.status').PUBLISHED) and error is None
    assert (Path(cfg.paths.ready) / f'{delivery_id}.jpg').read_bytes() == _CONTENT
    assert owned.connection.execute('SELECT source_dependency FROM obtain_items WHERE delivery_id=?', (delivery_id,)).fetchone() == (0,)
    assert owned.connection.execute('SELECT status,error_json FROM operation_runs WHERE id=?', (ticket.run_id,)).fetchone() == (
        int(enum_for('operation_runs.status').SUCCEEDED), None)
    assert len(reader.requests) == 1


@pytest.mark.parametrize('source_checksum_supported', [False, True])
async def test_internal_held_actual_end_binding_loss_checks_locally_without_unknown_or_new_read(tmp_path, monkeypatch, source_checksum_supported):
    recording, source_id = await _real_recording_media_flow(tmp_path)
    reader = ReadRequests(recording.owned, _MEDIA_CONTENT)
    driver = _SessionDriver(_MEDIA_CONTENT)
    driver.open_read = reader.open_read
    evidence = EvidenceRegistry(tuple(_EVIDENCE.contract(name, 1) for name in (
        'operation_returned', 'start_confirmed', 'stop_returned', 'stop_confirmed', 'results_returned', 'result_files_listed', 'file_digest')) + (
            EvidenceContract('read_returned', 1, 'read', frozenset()),
            EvidenceContract('adb_foreground_recovery', 1, 'read', frozenset()),))
    entry = DriverEntry('camctl-adb', driver, DriverDeclaration(control_supported=True, stop_supported=True, query_supported=False,
        result_supported=True, read_supported=True, digest_supported=source_checksum_supported, delete_supported=False,
        adb_foreground_recovery_operations=frozenset({'read'})), evidence, DriverStatus.SOFTWARE_CONTRACT_VERIFIED)
    devices = {'cam-1': {'kind': 'camera', 'driver': 'camctl-adb', 'copy': _SETTINGS}}
    horizon = [None]
    factory = session_capture_assembly(devices=devices, drivers=DriverRegistry((entry,)), results=ResultsDouble({}),
        staging=recording.roots.staging, wall_us=lambda: _NOW, monotonic_ns=lambda: 0, segment_size=4,
        wait_config=lambda _action: CaptureWaitConfig(target_duration_ms=6000, driver_margin_ms=0),
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED, recovery_max_event_id=lambda: horizon[0])
    try:
        runtime = factory(recording.owned, 'cam-1')
        completions = _fail_local_completion_once(monkeypatch, source_checksum_supported=source_checksum_supported)
        with pytest.raises(ConsistencyError):
            await run_recording_media(runtime.media, 1, 1, source_id)
        ticket, progress = _held_original(recording.owned, runtime, reader, source_checksum_supported=source_checksum_supported)
        digest_calls = tuple(driver.calls)
        assert len(digest_calls) == int(source_checksum_supported)
        horizon[0] = recording.owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()[0]
        devices.pop('cam-1')
        await capture_handler('camera_record')(1, factory(recording.owned, 'cam-1'))
        _actual_result_saved(recording.owned, reader, ticket, progress, completions)
        assert tuple(driver.calls) == digest_calls
    finally:
        recording.owned.connection.close()

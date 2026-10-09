"""内部读取绑定收场按原 H 核实空变化组、原 slot 与原键输入。"""

from dataclasses import replace
from pathlib import Path

import pytest

from camctl.bootstrap.capture_assembly import session_capture_assembly
from camctl.capture.handlers import capture_handler
from camctl.capture.media import RecordingFailure
from camctl.capture.media_flow import run_recording_media
from camctl.capture.recovery import RecoveryBoundary
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.devices.drivers.registry import DriverRegistry
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import TransactionError

from ..capture.test_capture_contract import ResultsDouble
from ..cancellation.test_report_sync_lifecycle import _fault_owned
from .test_output_binding_changes import _NOW, _registry
from .test_output_read_recovery import _start_unended_read
from .test_read_execution_runtime import ReadRequests, _MEDIA_CONTENT, _real_recording_media_flow

pytestmark = pytest.mark.asyncio


async def _binding_ready(tmp_path, *, preclosed):
    flow, source_id = await _real_recording_media_flow(tmp_path)
    driver = ReadRequests(flow.owned, _MEDIA_CONTENT, fail=True)
    flow.sessions = flow.sessions.__class__(flow.owned, driver, None)
    flow.max_read_attempts = 1 if preclosed else 7
    await run_recording_media(flow, 1, 1, source_id)
    copy_id = flow.owned.connection.execute("SELECT id FROM file_copies WHERE processing_id=1").fetchone()[0]
    if preclosed:
        assert flow.owned.connection.execute(
            "SELECT check_state,repair_state,repair_error_json FROM recording_processing WHERE id=1").fetchone() == (4, 2, None)
        assert flow.owned.connection.execute(
            "SELECT status,retry_wait_required FROM operation_runs WHERE copy_id=?", (copy_id,)).fetchone() == (4, 0)
        assert flow.owned.connection.execute("SELECT slot_device_id FROM file_copies WHERE id=?", (copy_id,)).fetchone() == (None,)
    else:
        _start_unended_read(flow.owned, copy_id)
    horizon = flow.owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0]
    entry = _registry(driver).entry("camctl-adb")
    entry = replace(entry, declaration=replace(entry.declaration,
        adb_foreground_recovery_operations=frozenset({"read"})), evidence=EvidenceRegistry((
            EvidenceContract("adb_foreground_recovery", 1, "read", frozenset()),)))
    factory = session_capture_assembly(
        devices={}, drivers=DriverRegistry((entry,)), results=ResultsDouble({}), staging=flow.roots.staging,
        wall_us=lambda: _NOW, monotonic_ns=lambda: 0,
        wait_config=lambda _action: CaptureWaitConfig(target_duration_ms=6000, driver_margin_ms=0),
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED, recovery_max_event_id=lambda: horizon)
    return flow, driver, factory


@pytest.mark.parametrize("preclosed", [False, True])
async def test_internal_binding_unknown_commit_reuses_original_nulls_and_slot(tmp_path, monkeypatch, preclosed):
    flow, driver, factory = await _binding_ready(tmp_path, preclosed=preclosed)
    result_run = flow.owned.connection.execute(
        "SELECT id,status,retry_wait_required,error_json FROM operation_runs WHERE responsibility_key='results/1'").fetchone()
    assert result_run is not None and result_run[1:] == (2, 1, None)
    saved = []
    original = CaptureRepository.finish_binding_failure

    def once(repository, request, key, owned):
        saved.append((request, key))
        if len(saved) == 1:
            faulty = _fault_owned(owned, "COMMIT", after_commit=True)
            result = original(repository, request, key, faulty)
            assert faulty.connection.failed
            assert result.kind is DbOutcomeKind.UNKNOWN
            return result
        return original(repository, request, key, owned)

    monkeypatch.setattr(CaptureRepository, "finish_binding_failure", once)
    try:
        from camctl.contracts.values import ConsistencyError
        with pytest.raises(ConsistencyError):
            await capture_handler("camera_record")(1, factory(flow.owned, "cam-1"))
        path = flow.owned.connection.execute("PRAGMA database_list").fetchone()[2]
        before = tuple(flow.owned.connection.iterdump())
        flow.owned.connection.close()
        flow.owned = open_existing(Path(path), DbOpenMode.EXISTING_RW, DbConfig())
        await capture_handler("camera_record")(1, factory(flow.owned, "cam-1"))
        assert saved[0] == saved[1]
        assert tuple(flow.owned.connection.iterdump()) == before
        assert len(driver.requests) == 1
        types = flow.owned.connection.execute(
            "SELECT event_type FROM history_events WHERE transaction_id=(SELECT id FROM history_transactions WHERE operation_key=?) ORDER BY id",
            (str(saved[0][1]),)).fetchall()
        if preclosed:
            assert types == [(8,), (9,), (10,)]
            import json
            assert saved[0][0].responsibility_keys == ("results/1",)
            body = json.loads(flow.owned.connection.execute(
                "SELECT body_json FROM history_events WHERE transaction_id=(SELECT id FROM history_transactions WHERE operation_key=?) AND event_type=10",
                (str(saved[0][1]),)).fetchone()[0])
            assert body["rows"] == [{"table": "operation_runs", "id": result_run[0],
                "before": {"exists": True, "values": {"status": 2, "retry_wait_required": 1, "error_json": None}},
                "after": {"exists": True, "values": {"status": 4, "retry_wait_required": 0,
                    "error_json": {"code": "device_binding_unavailable", "stage": "execution", "details": saved[0][0].failure.details}}}}]
            assert flow.owned.connection.execute("SELECT status,retry_wait_required,json_extract(error_json,'$.code') FROM operation_runs WHERE id=?",
                (result_run[0],)).fetchone() == (4, 0, "device_binding_unavailable")
            assert flow.owned.connection.execute("SELECT status,json_extract(error_json,'$.code') FROM operation_runs WHERE responsibility_key='read/1'").fetchone() == (
                4, "read_attempts_exhausted")
        else:
            assert [value[0] for value in types][-3:] == [19, 18, 22]
    finally:
        flow.owned.connection.close()


@pytest.mark.parametrize("changed", ["time", "details", "responsibilities", "cancel"])
async def test_internal_binding_original_key_rejects_changed_semantic_input(tmp_path, monkeypatch, changed):
    flow, _driver, factory = await _binding_ready(tmp_path, preclosed=False)
    saves = []
    original = CaptureRepository.finish_binding_failure

    def capture(repository, request, key, owned):
        saves.append((request, key))
        return original(repository, request, key, owned)

    monkeypatch.setattr(CaptureRepository, "finish_binding_failure", capture)
    try:
        await capture_handler("camera_record")(1, factory(flow.owned, "cam-1"))
        request, key = saves[0]
        if changed == "time":
            changed_request = replace(request, occurred_at=request.occurred_at + 1)
        elif changed == "details":
            changed_request = replace(request, failure=RecordingFailure("device_binding_unavailable", {
                **request.failure.details, "reason": "mismatch", "actual_driver_id": "replacement"}))
        elif changed == "responsibilities":
            assert request.responsibility_keys
            changed_request = replace(request, responsibility_keys=())
        else:
            changed_request = replace(request, canceled=True)
        before = tuple(flow.owned.connection.iterdump())
        result = original(CaptureRepository(), changed_request, key, flow.owned)
        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert isinstance(result.error, TransactionError)
        assert tuple(flow.owned.connection.iterdump()) == before
    finally:
        flow.owned.connection.close()

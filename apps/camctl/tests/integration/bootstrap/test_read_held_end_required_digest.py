"""拥有者持有真实完整 End，必要设备摘要不可用时分别保存实际与业务结果。"""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.bootstrap.capture_assembly import session_capture_assembly
from camctl.bootstrap.obtain_assembly import session_obtain_assembly
from camctl.capture.handlers import capture_handler
from camctl.capture.media_flow import RecordingInputCopies, run_recording_media
from camctl.capture.result_inputs import RESULT_FILES_CONTRACT
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DriverDeclaration
from camctl.outputs.copy import CopyCompletionError
from camctl.outputs.obtain_flow import advance_obtain
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..cancellation.test_report_sync_lifecycle import _fault_owned
from ..capture.test_capture_contract import ResultsDouble, _entry
from .test_media_assembly import _EVIDENCE, _SessionDriver
from .test_output_binding_changes import _CONTENT, _NOW, _read_registry as _registry, environment
from .test_read_execution_runtime import ReadRequests, _MEDIA_CONTENT, _SETTINGS, _real_recording_media_flow
from .test_read_failure_original_groups import prepared_without_attempt

pytestmark = pytest.mark.asyncio


def _source_entry(reader, content, *, internal):
    driver = _SessionDriver(content)
    driver.open_read = reader.open_read
    contracts = [RESULT_FILES_CONTRACT, EvidenceContract("read_returned", 1, "read", frozenset()),
                 _EVIDENCE.contract("file_digest", 1)]
    if internal:
        contracts.extend(_EVIDENCE.contract(name, 1) for name in (
            "operation_returned", "start_confirmed", "stop_returned", "stop_confirmed", "results_returned"))
        declaration = DriverDeclaration(control_supported=True, stop_supported=True, query_supported=False,
            result_supported=False, read_supported=True, digest_supported=True, delete_supported=False)
    else:
        original = _registry(reader).entry("camctl-adb")
        declaration = replace(original.declaration, digest_supported=True)
    return driver, DriverEntry("camctl-adb", driver, declaration, EvidenceRegistry(tuple(contracts)),
                              DriverStatus.SOFTWARE_CONTRACT_VERIFIED)


def _binding_fault(devices, device_id, change):
    """当前会话配置声明缺失或切换到另一个已登记驱动。"""
    if change == "missing":
        devices.pop(device_id, None)
    else:
        devices[device_id] = {"kind": "camera", "driver": "alternate-camera"}


def _business_factory(actual, devices, *, internal=False, collections=None):
    from ..support.capture import capture_database

    async def build(owned):
        database = await capture_database(owned)
        return await actual(database, "cam-1") if internal else actual(database)

    async def advance(runtime):
        from camctl.capture.media_flow import resume_prepared_internal_reads
        await resume_prepared_internal_reads(runtime.database, **collections)
        await capture_handler("camera_record")(1, runtime)

    return SimpleNamespace(build=build, devices=devices, advance=advance if internal else advance_obtain)


def _drivers(entry):
    # mismatch 的当前驱动也具有真实、合法的登记；读取原 locator 不转交它。
    return DriverRegistry((entry, replace(entry, driver_id="alternate-camera")))


def _hold_before_digest(monkeypatch):
    complete, calls = RecordingInputCopies.complete, []

    async def once(copies, copy_id, digest):
        calls.append(copy_id)
        if len(calls) == 1:
            raise CopyCompletionError("host_digest_failed", "主机摘要暂不可用，源摘要尚未调用")
        return await complete(copies, copy_id, digest)

    monkeypatch.setattr(RecordingInputCopies, "complete", once)
    return calls


def _held_without_digest(owned, runtime, reader):
    assert len(reader.requests) == 1
    ticket = reader.requests[0][0]
    ends = [end for actual, end in reader.ends if actual == ticket]
    assert ends and ends[-1].stopped is True and ends[-1].error is None
    assert owned.connection.execute(
        "SELECT status,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
        (ticket.run_id, ticket.attempt_id)).fetchone() == (1, None)
    state = owned.connection.execute(
        "SELECT committed_bytes,source_size,round,recopies_used,source_sha256,target_sha256,verification_state,slot_device_id"
        " FROM file_copies WHERE id=?", (int(ticket.target_id),)).fetchone()
    assert state[0] == state[1] and state[4:6] == (None, None)
    assert state[6] == int(enum_for("file_copies.verification_state").NOT_PERFORMED) and state[7] is not None
    assert owned.connection.execute(
        "SELECT f.checksum_support FROM file_copies c JOIN device_files f ON f.id=c.source_device_file_id WHERE c.id=?",
        (int(ticket.target_id),)).fetchone() == (int(enum_for("device_files.checksum_support").SUPPORTED),)
    return ticket, state


def _finish_fault(monkeypatch, after_commit):
    saved = []
    original = OperationRepository.finish_binding_read

    def finish(repository, request, key, owned, *, current_identity=None):
        saved.append((request, key, current_identity))
        if len(saved) == 1:
            faulty = _fault_owned(owned, "COMMIT", after_commit=after_commit)
            result = original(repository, request, key, faulty, current_identity=current_identity)
            assert faulty.connection.failed and result.kind is DbOutcomeKind.UNKNOWN
            return result
        return original(repository, request, key, owned, current_identity=current_identity)

    monkeypatch.setattr(OperationRepository, "finish_binding_read", finish)
    return saved


def _assert_actual_and_binding(owned, ticket, original, driver, reader, completions, change, *, internal):
    import json

    row = owned.connection.execute(
        "SELECT status,error_json,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
        (ticket.run_id, ticket.attempt_id)).fetchone()
    assert row[0:2] == (2, None)
    result = json.loads(row[2])
    assert result["settlement"] == {"basis": "observed", "evidence": {"type": "read_returned", "version": 1, "data": {}}}
    status, error, used = owned.connection.execute(
        "SELECT status,error_json,attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone()
    assert status == int(enum_for("operation_runs.status").FAILED) and used == ticket.attempt_id
    assert json.loads(error)["code"] == "device_binding_unavailable"
    assert json.loads(error)["details"]["reason"] == change
    state = owned.connection.execute(
        "SELECT committed_bytes,source_size,round,recopies_used,source_sha256,target_sha256,verification_state,slot_device_id"
        " FROM file_copies WHERE id=?", (int(ticket.target_id),)).fetchone()
    assert state[:-1] == original[:-1] and state[-1] is None
    assert driver.calls == [] and len(reader.requests) == 1 and len(completions) == 1
    if internal:
        check, media, repair = owned.connection.execute(
            "SELECT check_state,media_json,repair_state FROM recording_processing WHERE id=1").fetchone()
        assert check == int(enum_for("recording_processing.check_state").FAILED)
        assert json.loads(media)["error"]["code"] == "device_binding_unavailable"
        assert repair == int(enum_for("recording_processing.repair_state").NOT_NEEDED)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (
            int(enum_for("actions.status").FAILED),)
    else:
        delivery_id, delivery_status, delivery_error = owned.connection.execute(
            "SELECT d.id,d.status,d.error_json FROM deliveries d JOIN file_copies c ON c.delivery_id=d.id WHERE c.id=?",
            (int(ticket.target_id),)).fetchone()
        assert delivery_status == int(enum_for("deliveries.status").FAILED)
        assert json.loads(delivery_error)["code"] == "device_binding_unavailable"
        assert owned.connection.execute("SELECT source_dependency FROM obtain_items WHERE delivery_id=?", (delivery_id,)).fetchone() == (0,)


async def _advance_binding_and_recover(owned, factory, advance, device, change,
        monkeypatch, after_commit, capture_resources):
    saved = None if after_commit is None else _finish_fault(monkeypatch, after_commit)
    _binding_fault(factory.devices, device, change)
    if saved is None:
        await advance(await factory.build(owned))
        return owned, False
    with pytest.raises(ConsistencyError):
        await advance(await factory.build(owned))
    assert len(saved) == 1
    path = owned.connection.execute("PRAGMA database_list").fetchone()[2]
    owned.connection.close()
    await capture_resources.restart_database(path)
    reopened = open_existing(Path(path), DbOpenMode.EXISTING_RW, DbConfig())
    try:
        await advance(await factory.build(reopened))
        assert len(saved) == 2 and saved[1] == saved[0]
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(saved[0][1]),)).fetchone() == (1,)
        return reopened, True
    except BaseException:
        reopened.connection.close()
        raise


_CASES = [("missing", None), ("mismatch", None), ("missing", False), ("missing", True)]


@pytest.mark.parametrize("change,after_commit", _CASES)
async def test_obtain_held_end_missing_required_sha_saves_actual_then_binding_failure(
        prepared_without_attempt, monkeypatch, change, after_commit, capture_resources):
    cfg, owned, _command = prepared_without_attempt
    reader = ReadRequests(owned, _CONTENT)
    driver, entry = _source_entry(reader, _CONTENT, internal=False)
    devices = {"cam-a": {**cfg.devices["cam-a"], "copy": _SETTINGS}}
    actual = session_obtain_assembly(devices=devices,
        drivers=_drivers(entry), staging=Path(cfg.paths.staging), ready=Path(cfg.paths.ready),
        processing=Path(cfg.paths.processing), segment_size=4, occurred_at=lambda: _NOW, monotonic_ns=lambda: 0)
    factory = _business_factory(actual, devices)
    runtime = await factory.build(owned)
    completions = _hold_before_digest(monkeypatch)
    with pytest.raises(ConsistencyError):
        await advance_obtain(runtime)
    ticket, state = _held_without_digest(owned, runtime, reader)
    assert driver.calls == []
    recovered, close = await _advance_binding_and_recover(owned, factory, advance_obtain,
        "cam-a", change, monkeypatch, after_commit, capture_resources)
    try:
        _assert_actual_and_binding(recovered, ticket, state, driver, reader, completions, change, internal=False)
        assert list(Path(cfg.paths.ready).iterdir()) == []
    finally:
        if close:
            recovered.connection.close()


async def _internal_runtime(tmp_path):
    recording, source = await _real_recording_media_flow(tmp_path)
    reader = ReadRequests(recording.owned, _MEDIA_CONTENT)
    driver, entry = _source_entry(reader, _MEDIA_CONTENT, internal=True)
    devices = {"cam-1": {"kind": "camera", "driver": "camctl-adb", "copy": _SETTINGS}}
    collections = {name: {} for name in (
        "pending_read_results", "pending_read_business", "pending_read_ends",
        "continuing_read_tickets", "pending_segment_inspection_results")}
    actual = session_capture_assembly(devices=devices, **collections,
        drivers=_drivers(entry), results=recording.runtime_options["results"],
        staging=recording.roots.staging, wall_us=lambda: _NOW, monotonic_ns=lambda: 0, segment_size=4,
        probe_request=recording.runtime_options["probe_request"],
        repair_request=recording.runtime_options["repair_request"],
        wait_config=lambda _action: CaptureWaitConfig(target_duration_ms=6000, driver_margin_ms=0))
    return recording, source, reader, driver, _business_factory(actual, devices, internal=True, collections=collections)


@pytest.mark.parametrize("change,after_commit", _CASES)
async def test_internal_held_end_missing_required_sha_saves_actual_then_binding_failure(
        tmp_path, monkeypatch, change, after_commit, capture_resources):
    recording, source, reader, driver, factory = await _internal_runtime(tmp_path)
    runtime = await factory.build(recording.owned)
    completions = _hold_before_digest(monkeypatch)
    reopened = None
    try:
        with pytest.raises(ConsistencyError):
            await run_recording_media(runtime.media, 1, 1, source)
        ticket, state = _held_without_digest(recording.owned, runtime, reader)
        assert driver.calls == []
        reopened, _close = await _advance_binding_and_recover(recording.owned, factory,
            factory.advance, "cam-1", change, monkeypatch, after_commit, capture_resources)
        _assert_actual_and_binding(reopened, ticket, state, driver, reader, completions, change, internal=True)
        assert not recording.tool_log.exists()
    finally:
        (recording.owned if reopened is None else reopened).connection.close()


@pytest.mark.parametrize("change", ["missing", "mismatch"])
async def test_obtain_held_end_reliable_sha_continues_local_publication(prepared_without_attempt, monkeypatch, change):
    from .test_read_held_end_binding import _fail_local_completion_once, _held_original, _actual_result_saved
    cfg, owned, _command = prepared_without_attempt
    reader = ReadRequests(owned, _CONTENT)
    driver, entry = _source_entry(reader, _CONTENT, internal=False)
    devices = {"cam-a": {**cfg.devices["cam-a"], "copy": _SETTINGS}}
    actual = session_obtain_assembly(devices=devices,
        drivers=_drivers(entry), staging=Path(cfg.paths.staging), ready=Path(cfg.paths.ready),
        processing=Path(cfg.paths.processing), segment_size=4, occurred_at=lambda: _NOW, monotonic_ns=lambda: 0)
    factory = _business_factory(actual, devices)
    runtime = await factory.build(owned)
    completions = _fail_local_completion_once(monkeypatch, source_checksum_supported=True)
    with pytest.raises(ConsistencyError):
        await advance_obtain(runtime)
    ticket, state = _held_original(owned, runtime, reader, source_checksum_supported=True)
    calls = tuple(driver.calls)
    assert len(calls) == 1
    _binding_fault(factory.devices, "cam-a", change)
    await advance_obtain(await factory.build(owned))
    _actual_result_saved(owned, reader, ticket, state, completions)
    _binding_fault(factory.devices, "cam-a", change)
    await advance_obtain(await factory.build(owned))
    assert tuple(driver.calls) == calls
    delivery_id, status, error = owned.connection.execute(
        "SELECT d.id,d.status,d.error_json FROM deliveries d JOIN file_copies c ON c.delivery_id=d.id WHERE c.id=?",
        (int(ticket.target_id),)).fetchone()
    assert status == int(enum_for("deliveries.status").PUBLISHED) and error is None
    assert (Path(cfg.paths.ready) / f"{delivery_id}.jpg").read_bytes() == _CONTENT


@pytest.mark.parametrize("change", ["missing", "mismatch"])
async def test_internal_held_end_reliable_sha_and_saved_results_continue_local_check(tmp_path, monkeypatch, change):
    from .test_read_held_end_binding import _fail_local_completion_once, _held_original, _actual_result_saved
    recording, source, reader, driver, factory = await _internal_runtime(tmp_path)
    try:
        runtime = await factory.build(recording.owned)
        # 原结果调用、实际事件和完整原片均已由真实winddown保存。
        queries = tuple(runtime.results.calls)
        original_results = recording.owned.connection.execute(
            "SELECT r.status,t.result_event_id,t.result_json,e.occurred_at FROM operation_runs r"
            " JOIN operation_attempts t ON t.run_id=r.id JOIN history_events e ON e.id=t.result_event_id"
            " WHERE r.kind=7 ORDER BY t.id DESC LIMIT 1").fetchone()
        assert original_results is not None and original_results[0] == 2
        import json
        result = json.loads(original_results[2])
        entries = [entry for observation in result["observations"]
            if observation["type"] == "result_files_listed"
            for entry in observation["data"]["entries"]]
        assert entries and all(entry["complete"] for entry in entries)
        assert original_results[1] is not None and original_results[3] == _NOW
        original_results = original_results[:3]
        assert tuple(runtime.results.calls) == queries
        completions = _fail_local_completion_once(monkeypatch, source_checksum_supported=True)
        with pytest.raises(ConsistencyError):
            await run_recording_media(runtime.media, 1, 1, source)
        ticket, state = _held_original(recording.owned, runtime, reader, source_checksum_supported=True)
        calls = tuple(driver.calls)
        assert len(calls) == 1
        _binding_fault(factory.devices, "cam-1", change)
        await capture_handler("camera_record")(1, await factory.build(recording.owned))
        _actual_result_saved(recording.owned, reader, ticket, state, completions)
        assert tuple(driver.calls) == calls
        assert tuple(runtime.results.calls) == queries
        check, media = recording.owned.connection.execute(
            "SELECT check_state,media_json FROM recording_processing WHERE id=1").fetchone()
        assert check == int(enum_for("recording_processing.check_state").COMPLETED)
        from camctl.contracts.json_values import parse_exact_json
        assert "error" not in parse_exact_json(media)
        assert recording.owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (
            int(enum_for("actions.status").SUCCEEDED),)
        settled_results = recording.owned.connection.execute(
            "SELECT r.status,t.result_event_id,t.result_json FROM operation_runs r JOIN operation_attempts t ON t.run_id=r.id"
            " WHERE r.kind=7 ORDER BY t.id DESC LIMIT 1").fetchone()
        # 单原片的可靠结果已经满足录制要求，RESULTS 与录像终态共同结束；
        # 原轮次的实际观察和结果事件保持，不重写为另一个尝试结果。
        assert settled_results is not None
        assert settled_results[0] == int(enum_for("operation_runs.status").SUCCEEDED)
        assert settled_results[1:] == original_results[1:]
    finally:
        recording.owned.connection.close()

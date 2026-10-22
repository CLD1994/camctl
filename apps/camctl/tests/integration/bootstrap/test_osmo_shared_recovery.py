"""正式 OSMO 共享恢复证据由拍摄与取回的原业务责任消费。"""
import asyncio
from dataclasses import replace
from decimal import Decimal
import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.bootstrap.capture_assembly import session_capture_assembly
from camctl.bootstrap.obtain_assembly import session_obtain_assembly
from camctl.outputs.obtain_flow import advance_obtain
from camctl.capture.media_flow import run_recording_media
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.capture.handlers import capture_handler, _stop_call, _begin_check_round
from camctl.capture.processing import CheckBasis, CheckDecisionChoice, CheckDecisionSave, CheckReason
from camctl.capture.result_inputs import RESULT_PAGE_CONTRACT
from camctl.capture.result_pages import ResultPageSave
from camctl.operations.validation import validate_outcome
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.contracts.enums import enum_for
from camctl.contracts.values import new_operation_key
from camctl.devices import definitions_runtime
from camctl.devices.bindings import DeviceBinding
from camctl.devices.catalog import build_catalog
from camctl.devices.drivers import runtime as drivers_runtime
from camctl.devices.drivers.adb_cameras.registration import register_builtin_camera_drivers
from camctl.operations.attempts import AttemptIntent, AttemptTarget, OperationKind
from camctl.operations.process import LocalExit, RawToolOutcome
from camctl.operations.recovery import RecoveryBoundary
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..acceptance.test_acceptance import _plan_body
from ..acceptance.test_adb_camera_definitions import _params
from ..capture.test_baseline_history import _fix
from ..capture.test_cancel_timelapse_exhaustion_files import _apply_public_cancel
from ..scheduling.test_start_action import owned, _accept_plan, _start, _SCHEDULED

pytestmark = pytest.mark.asyncio
BINDING = DeviceBinding("cam-1", "dji-osmo360-ii")
OSV_PATH = "/mnt/media_rw/emulated/DCIM/CAM001/CAM_20261022100000_0007_D.OSV"


class Channel:
    """只提供正式设备工具的发送回执；恢复后禁止重开旧调用。"""
    def __init__(self):
        self.calls = []
        self.forbidden = False
        self.directory_end = b"END"
        self.metadata_error = False
        self.streams = []
        self.allowed_ticket = None
        self.current_result_ticket = lambda: None
        self.result_tickets = []

    async def run(self, spec, stop):
        ticket = self.current_result_ticket()
        assert not self.forbidden or (ticket is not None and ticket == self.allowed_ticket), "旧前台调用不得重放"
        self.calls.append(spec)
        script = shlex.split(spec.argv[5])[2]
        if "CAMCTL-DIRECTORY/1" in script:
            self.result_tickets.append(ticket)
            return RawToolOutcome(LocalExit(exit_code=0), b"CAMCTL-DIRECTORY/1\0" + OSV_PATH.encode() + b"\0" + self.directory_end + b"\0",
                                  None, None, stderr=b"")
        if "stat -c %s" in script:
            return RawToolOutcome(LocalExit(exit_code=7 if self.metadata_error else 0), b"" if self.metadata_error else b"50\n",
                                  None, None, stderr=b"metadata error" if self.metadata_error else b"")
        return RawToolOutcome(LocalExit(exit_code=0), b"Resp message, len = 1, data:\n 00\n",
                              None, None, stderr=b"")

    async def run_stream(self, spec, stop, receive):
        assert not self.forbidden, "原READ不能重开源流"
        self.streams.append(spec)
        await receive(b"part")
        return RawToolOutcome(LocalExit(exit_code=7), None, None, None, stderr=b"source interrupted")


async def _world(owned, tmp_path, monkeypatch, *, finite=False, binding=BINDING,
                 single_original=False):
    monkeypatch.setattr(definitions_runtime, "_definitions", {})
    monkeypatch.setattr(drivers_runtime, "_entries", {})
    cfg = load_config({"paths": {name: str(tmp_path / name) for name in ("staging", "ready", "processing")},
        "devices": {"cam-1": {"kind": "camera", "driver": binding.driver_id,
            "adb": {"serial": "serial-1"}, "recording": {"start_retry_interval_s": "0"},
            "result_check": {"max_attempts": 1, "retry_interval_s": "0"},
            "copy": {"retry_interval_s": "0"}}}}, ConfigDefaults())
    from camctl.persistence.initialization import _prepare_directories
    _prepare_directories(tmp_path / "state.db", Path(cfg.paths.staging), Path(cfg.paths.ready), Path(cfg.paths.processing))
    if single_original:
        from camctl.devices.drivers.adb_cameras import osmo360ii_capture
        original_task = osmo360ii_capture._task

        def task(params):
            declared = original_task(params)
            return replace(declared, result_policy=None,
                product_rules=tuple({**rule, "exact_count": 1} for rule in declared.product_rules))

        monkeypatch.setattr(osmo360ii_capture, "_task", task)
    register_builtin_camera_drivers(cfg)
    registry = drivers_runtime.current_registry()
    entry = registry.entry(binding.driver_id)
    catalog = build_catalog(cfg, definitions_runtime.current_driver_definitions())
    kind = "camera_timelapse" if finite else "camera_record"
    body = _plan_body()
    body["actions"][0].update(type=kind, params=_params(binding.driver_id, kind))
    await _accept_plan(owned, tmp_path, body, catalog)
    frozen = json.loads(owned.connection.execute("SELECT execution_spec_json FROM actions WHERE id=1").fetchone()[0])
    assert frozen.get("result_policy") == (None if single_original else
        "osmo_empty_tail_classification_v1" if finite else "osmo360ii_record_segments_v1")
    started = _start(owned, 1, now=_SCHEDULED)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    activity_id = started.value.activity_id
    fixed = _fix(owned, activity_id)
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    channel = Channel()
    entry.driver.transport = channel
    world = SimpleNamespace(owned=owned, cfg=cfg, registry=registry, entry=entry, channel=channel, binding=binding,
        kind=kind, activity_id=activity_id, wall=[_SCHEDULED], mono=[0])
    channel.current_result_ticket = lambda: world.owned.connection.execute(
        "SELECT t.run_id,t.attempt_no FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
        " WHERE r.kind=7 AND t.status=1 ORDER BY t.id DESC LIMIT 1").fetchone()
    entry.driver.monotonic_ns = lambda: world.mono[0]
    return world


def _capture(world, **kwargs):
    factory = session_capture_assembly(devices=kwargs.pop("devices", world.cfg.devices),
        drivers=kwargs.pop("drivers", world.registry), staging=Path(world.cfg.paths.staging),
        wait_config=lambda action: CaptureWaitConfig(action["execution_spec_json"]["target_duration_ms"], 30000, 1000),
        wall_us=lambda: world.wall[0], monotonic_ns=lambda: world.mono[0],
        media_enabled=kwargs.pop("media_enabled", False), segment_size=4, **kwargs)
    return factory(world.owned, world.binding.device_id)


def _reopen(world):
    path = Path(world.owned.connection.execute("PRAGMA database_list").fetchone()[2])
    world.owned.connection.close()
    world.owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    world.channel.forbidden = True
    world.wall[0] += 1000
    return world.owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0]


def _row(world, ticket):
    return world.owned.connection.execute("SELECT status,result_json,error_json FROM operation_attempts"
        " WHERE run_id=? AND attempt_no=?", (ticket.run_id, ticket.attempt_id)).fetchone()


def _assert_unknown(world, ticket):
    status, result, error = _row(world, ticket)
    assert status == 4
    assert json.loads(result) == {"format_version": 1, "observations": [], "settlement": {
        "basis": "assumed", "evidence": {"type": "adb_foreground_recovery", "version": 1, "data": {}}}}
    assert json.loads(error) == {"code": "result_not_saved", "stage": "recovery", "details": {}}
    assert world.owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (1,)


async def test_formal_registry_preserves_each_original_operation_and_result_page(owned, tmp_path, monkeypatch):
    world = await _world(owned, tmp_path, monkeypatch)
    runtime = _capture(world)
    for operation in ("control", "stop", "result", "read"):
        evidence = runtime.recovery_evidence_for(BINDING, operation)
        assert evidence is not None
        template = evidence.contract("adb_foreground_recovery", 1)
        assert template.operation == operation and not template.fields and template.identity_field is None
        assert evidence.contract(RESULT_PAGE_CONTRACT.type, RESULT_PAGE_CONTRACT.version) is world.entry.evidence.contract(
            RESULT_PAGE_CONTRACT.type, RESULT_PAGE_CONTRACT.version)
        returned = "result_returned" if operation == "result" else operation + "_returned"
        assert evidence.contract(returned, 1) is world.entry.evidence.contract(returned, 1)
    assert runtime.recovery_evidence_for(BINDING, "digest") is None
    assert runtime.recovery_evidence_for(BINDING, "query") is not None
    assert runtime.recovery_evidence_for(BINDING, "unsupported") is None


@pytest.mark.parametrize("operation", ["control", "stop"])
@pytest.mark.parametrize("finite_cancel", [False, True])
async def test_original_control_stop_recovery_preserves_activity_and_finite_cancel(
        owned, tmp_path, monkeypatch, operation, finite_cancel):
    world = await _world(owned, tmp_path, monkeypatch, finite=finite_cancel)
    runtime = _capture(world)
    if operation == "control":
        ticket, rejected = runtime.grant(runtime.action(1))
        assert ticket is not None, rejected
    else:
        await capture_handler(world.kind)(1, runtime)
        assert world.channel.calls
        assert world.owned.connection.execute("SELECT sent_at FROM device_activities").fetchone() == (_SCHEDULED,)
        begun = runtime.operations.begin_attempt(AttemptIntent("stop", 1, OperationKind.STOP,
            AttemptTarget(activity_id=world.activity_id), None, runtime.stop_config, _SCHEDULED + 1),
            new_operation_key(), world.owned)
        assert begun.kind is DbOutcomeKind.COMPLETED, begun.error
        ticket = begun.value.ticket
    if finite_cancel:
        _apply_public_cancel(world.owned, 1, _SCHEDULED + 2)
    activity = world.owned.connection.execute("SELECT * FROM device_activities").fetchall()
    calls = list(world.channel.calls)
    horizon = _reopen(world)
    try:
        resumed = _capture(world, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=lambda: horizon)
        assert resumed.pending_start_results == {}
        assert resumed.recover_attempt(ticket)
        _assert_unknown(world, ticket)
        assert world.channel.calls == calls
        assert world.owned.connection.execute("SELECT * FROM device_activities").fetchall() == activity
        assert world.owned.connection.execute("SELECT cancel_requested FROM actions WHERE id=1").fetchone() == (int(finite_cancel),)
        if finite_cancel:
            expected = 4 if operation == "stop" else 5
            assert world.owned.connection.execute("SELECT status FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (expected,)
        assert world.owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
    finally:
        world.owned.connection.close()


async def _result_attempt(world, *, elapsed_us=11_000_000):
    runtime = _capture(world)
    await capture_handler(world.kind)(1, runtime)
    world.wall[0] += elapsed_us
    world.mono[0] += elapsed_us * 1000
    stopped = await _stop_call(runtime, runtime.action(1), "stop_recording")
    assert stopped.phase == "confirmed", stopped
    begun = _begin_check_round(runtime, 1)
    assert begun.ticket is not None
    return runtime, begun.ticket


async def _save_actual_page(world, runtime, ticket, monkeypatch, page_kind):
    from ..capture.test_record_file_completion_wait import _stop_context
    context = {**_stop_context(world.owned, 1), "kind": "record_stop", "file_completion_wait_ms": 30000}
    world.channel.directory_end = b"MORE" if page_kind == "partial" else b"END"
    world.channel.metadata_error = page_kind == "terminal_error"

    async def elapsed(seconds):
        world.mono[0] += int(seconds * 1_000_000_000)
    monkeypatch.setattr(asyncio, "sleep", elapsed)
    page = await runtime.results.list_page(ticket, cursor=None, timeout_s=Decimal("40"),
        output_scope=runtime.action(1)["execution_spec_json"]["output_scope"], completion_context=context)
    saved = runtime.capture.save_result_page(ResultPageSave(ticket, 1, None,
        validate_outcome(ticket, page.outcome, world.entry.evidence), world.wall[0]),
        new_operation_key(), world.owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    original = runtime.capture.read_result_page(saved.value, world.owned)
    assert _row(world, ticket) == (1, None, None)
    return original


@pytest.mark.parametrize("pages", ["none", "partial", "terminal", "terminal_error"])
async def test_original_result_pages_are_consumed_before_new_listing(owned, tmp_path, monkeypatch, pages):
    world = await _world(owned, tmp_path, monkeypatch)
    runtime, ticket = await _result_attempt(world)
    original = None if pages == "none" else await _save_actual_page(world, runtime, ticket, monkeypatch, pages)
    calls = list(world.channel.calls)
    horizon = _reopen(world)
    try:
        resumed = _capture(world, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=lambda: horizon)
        assert resumed.pending_start_results == resumed.pending_result_scans == {}
        await capture_handler(world.kind)(1, resumed)
        if pages in ("none", "partial"):
            _assert_unknown(world, ticket)
        else:
            status, result, error = _row(world, ticket)
            assert status == int(enum_for("operation_attempts.status")[original.page.outcome.status.name])
            assert json.loads(result)["settlement"]["evidence"]["type"] == "result_returned"
            from camctl.operations.result_format import read_result_document
            effect = world.owned.connection.execute("SELECT effect_state FROM operation_attempts WHERE run_id=?", (ticket.run_id,)).fetchone()[0]
            assert read_result_document(status, effect, json.loads(result), None if error is None else json.loads(error)) == original.page.outcome
            assert world.owned.connection.execute("SELECT occurred_at FROM history_events WHERE id=(SELECT result_event_id"
                " FROM operation_attempts WHERE run_id=?)", (ticket.run_id,)).fetchone() == (original.occurred_at,)
        if original is not None:
            assert resumed.capture.read_result_page(original.ref, world.owned) == original
            assert world.owned.connection.execute("SELECT identity_key,size_bytes FROM device_files").fetchone() == (
                json.dumps([BINDING.device_id, BINDING.driver_id, OSV_PATH], separators=(",", ":")),
                None if pages == "terminal_error" else 50)
        else:
            assert world.owned.connection.execute("SELECT COUNT(*) FROM device_files").fetchone() == (0,)
        assert world.channel.calls == calls
        assert world.owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (1,)
        assert world.owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (int(pages in ("partial", "terminal")),)
        if pages == "terminal_error":
            assert world.owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (4,)
            assert world.owned.connection.execute(
                "SELECT COUNT(*) FROM segment_sets WHERE action_id=1 AND published_event_id IS NOT NULL"
            ).fetchone() == (1,)
        saved_attempt = _row(world, ticket)
        await capture_handler(world.kind)(1, resumed)
        assert _row(world, ticket) == saved_attempt
        assert world.owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (3 if pages == "terminal" else 4,)
    finally:
        world.owned.connection.close()


def _obtain(world, **kwargs):
    return session_obtain_assembly(devices=kwargs.pop("devices", world.cfg.devices),
        drivers=kwargs.pop("drivers", world.registry), staging=Path(world.cfg.paths.staging),
        ready=Path(world.cfg.paths.ready), processing=Path(world.cfg.paths.processing), segment_size=4,
        occurred_at=lambda: world.wall[0], monotonic_ns=lambda: world.mono[0], **kwargs)(world.owned)


async def _pending_read(world, tmp_path, monkeypatch, *, internal):
    from .test_output_read_recovery import _start_unended_read
    runtime, result_ticket = await _result_attempt(world, elapsed_us=5_000_000 if internal else 11_000_000)
    original = await _save_actual_page(world, runtime, result_ticket, monkeypatch, "terminal")
    from camctl.capture.result_scans import SavedResultEntries
    # 外部源沿本次实际返回完成；内部源同时复用已保存结果页的正式恢复路径。
    if internal:
        processing_id, = world.owned.connection.execute(
            "SELECT id FROM recording_processing WHERE action_id=1").fetchone()
        target_duration_ms = runtime.action(1)["execution_spec_json"]["target_duration_ms"]
        before, = world.owned.connection.execute(
            "SELECT check_decision FROM recording_processing WHERE id=?", (processing_id,)).fetchone()
        assert before == int(enum_for("recording_processing.check_decision").UNDETERMINED)
        required = runtime.capture.save_check_decision(CheckDecisionSave(
            processing_id, CheckDecisionChoice.REQUIRED,
            CheckBasis(CheckReason.INSUFFICIENT_TIMING, target_duration_ms), world.wall[0]),
            new_operation_key(), world.owned)
        assert required.kind is DbOutcomeKind.COMPLETED, required.error
        after, = world.owned.connection.execute(
            "SELECT check_decision FROM recording_processing WHERE id=?", (processing_id,)).fetchone()
        assert after == int(enum_for("recording_processing.check_decision").REQUIRED)
        horizon = world.owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0]
        runtime = _capture(world, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=lambda: horizon)
    else:
        runtime.hold_call_result(result_ticket, original.page.outcome, occurred_at=original.occurred_at,
            returned_ns=world.mono[0], result_listing=SavedResultEntries(runtime.capture, world.owned, original.ref))
    await capture_handler(world.kind)(1, runtime)
    source_id = world.owned.connection.execute("SELECT id FROM device_files").fetchone()[0]
    if internal:
        processing_id = world.owned.connection.execute("SELECT id FROM recording_processing WHERE action_id=1").fetchone()[0]
        from camctl.capture.models import ActivityConcludeSave
        from camctl.capture.processing import SourceFileSave
        for save, request in (
            (runtime.capture.conclude_activity, ActivityConcludeSave(1, world.wall[0])),
            (runtime.capture.save_source_file, SourceFileSave(processing_id, source_id, world.wall[0])),
        ):
            receipt = save(request, new_operation_key(), world.owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        runtime = _capture(world, media_enabled=True)
        assert runtime.media is not None
        await run_recording_media(runtime.media, 1, processing_id, source_id)
    else:
        assert world.owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (3,)
        catalog = build_catalog(world.cfg, definitions_runtime.current_driver_definitions())
        body = _plan_body(request_id="73", actions=[{"name": "精确取回OSV", "type": "obtain_action_outputs",
            "scheduled_at": "2026-01-15 09:00:00", "params": {"source": {"action_instance_id": "1"}, "output_ids": ["1"]}}])
        await _accept_plan(world.owned, tmp_path, body, catalog)
        await advance_obtain(_obtain(world))
    copy_id, committed, length = world.owned.connection.execute(
        "SELECT id,committed_bytes,source_size FROM file_copies").fetchone()
    assert (committed, length) == (4, 50)
    assert len(world.channel.streams) == 1
    first = world.owned.connection.execute("SELECT t.status FROM operation_attempts t JOIN operation_runs r"
        " ON r.id=t.run_id WHERE r.copy_id=?", (copy_id,)).fetchone()
    assert first == (3,)
    ticket = _start_unended_read(world.owned, copy_id)
    assert world.owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (2,)
    target = world.owned.connection.execute("SELECT f.relative_path FROM file_copies c JOIN intermediate_files f"
        " ON f.id=c.target_file_id WHERE c.id=?", (copy_id,)).fetchone()[0]
    assert (Path(world.cfg.paths.staging) / target).read_bytes() == b"part"
    return ticket, source_id, target


@pytest.mark.parametrize("internal", [False, True])
@pytest.mark.parametrize("change", ["missing", "mismatch"])
async def test_unavailable_original_read_keeps_progress_and_uses_own_consumer(
        owned, tmp_path, monkeypatch, internal, change):
    world = await _world(owned, tmp_path, monkeypatch, single_original=internal)
    ticket, source_id, target = await _pending_read(world, tmp_path, monkeypatch, internal=internal)
    source = world.owned.connection.execute("SELECT * FROM device_files WHERE id=?", (source_id,)).fetchone()
    progress = world.owned.connection.execute("SELECT committed_bytes,source_size,round,recopies_used,source_sha256,target_sha256,verification_state"
        " FROM file_copies WHERE id=?", (int(ticket.target_id),)).fetchone()
    history = world.owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()
    horizon = _reopen(world)
    devices = {} if change == "missing" else {BINDING.device_id: {"kind": "camera", "driver": "dji-action6", "adb": {"serial": "never"}}}
    try:
        kwargs = dict(devices=devices, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=lambda: horizon)
        if internal:
            resumed = _capture(world, **kwargs)
            await capture_handler(world.kind)(1, resumed)
            assert world.owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (4,)
            assert world.owned.connection.execute("SELECT check_state,json_extract(media_json,'$.error.code')"
                " FROM recording_processing WHERE action_id=1").fetchone() == (4, "device_binding_unavailable")
        else:
            resumed = _obtain(world, **kwargs)
            await advance_obtain(resumed)
            assert world.owned.connection.execute("SELECT status,json_extract(error_json,'$.code') FROM deliveries").fetchone() == (6, "device_binding_unavailable")
            assert world.owned.connection.execute("SELECT source_dependency FROM obtain_items").fetchone() == (0,)
        # 第一轮确实发生读取失败，第二张原票据恢复UNKNOWN，原次数不退还。
        status, result, error = _row(world, ticket)
        assert status == 4
        assert json.loads(result)["settlement"] == {"basis": "assumed", "evidence": {
            "type": "adb_foreground_recovery", "version": 1, "data": {}}}
        assert json.loads(error) == {"code": "result_not_saved", "stage": "recovery", "details": {}}
        assert world.owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (2,)
        assert world.owned.connection.execute("SELECT committed_bytes,source_size,round,recopies_used,source_sha256,target_sha256,verification_state"
            " FROM file_copies WHERE id=?", (int(ticket.target_id),)).fetchone() == progress
        assert world.owned.connection.execute("SELECT slot_device_id FROM file_copies WHERE id=?", (int(ticket.target_id),)).fetchone() == (None,)
        assert world.owned.connection.execute("SELECT * FROM device_files WHERE id=?", (source_id,)).fetchone() == source
        assert world.owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()[:len(history)] == history
        assert (Path(world.cfg.paths.staging) / target).read_bytes() == b"part"
        assert len(world.channel.streams) == 1
        assert resumed.last_recovery_diagnostic is None
    finally:
        world.owned.connection.close()


@pytest.mark.parametrize("consumer", ["capture", "read"])
@pytest.mark.parametrize("missing,reason", [("boundary", "UNCONFIRMED_BOUNDARY"),
    ("horizon", "MISSING_HORIZON"), ("authorization", "EVIDENCE_UNAVAILABLE")])
async def test_original_recovery_requires_boundary_horizon_and_authorization(
        owned, tmp_path, monkeypatch, consumer, missing, reason):
    from camctl.devices.drivers.registry import DriverRegistry
    world = await _world(owned, tmp_path, monkeypatch)
    if consumer == "capture":
        original = _capture(world)
        ticket, rejected = original.grant(original.action(1))
        assert ticket is not None, rejected
    else:
        ticket, _, _ = await _pending_read(world, tmp_path, monkeypatch, internal=False)
    horizon = _reopen(world)
    registry = world.registry
    if missing == "authorization":
        entry = replace(world.entry, declaration=replace(world.entry.declaration,
            adb_foreground_recovery_operations=world.entry.declaration.adb_foreground_recovery_operations - {ticket.operation}))
        registry = DriverRegistry((entry, world.registry.entry("dji-action6")))
    kwargs = dict(drivers=registry,
        recovery_boundary=RecoveryBoundary.UNCONFIRMED if missing == "boundary" else RecoveryBoundary.HOST_LOCAL_SETTLED,
        recovery_max_event_id=None if missing == "horizon" else lambda: horizon)
    before = tuple(world.owned.connection.iterdump())
    calls, streams = list(world.channel.calls), list(world.channel.streams)
    try:
        if consumer == "capture":
            resumed = _capture(world, **kwargs)
            assert resumed.recover_attempt(ticket) is False
        else:
            resumed = _obtain(world, devices={}, **kwargs)
            await advance_obtain(resumed)
        diagnostic = resumed.last_recovery_diagnostic
        assert diagnostic is not None
        assert (diagnostic.reason.name, diagnostic.run_id, diagnostic.attempt_id) == (reason, ticket.run_id, ticket.attempt_id)
        assert _row(world, ticket) == (1, None, None)
        assert tuple(world.owned.connection.iterdump()) == before
        assert world.channel.calls == calls and world.channel.streams == streams
    finally:
        world.owned.connection.close()


async def test_formal_read_recovery_reuses_original_finish_after_real_unknown_commit(
        owned, tmp_path, monkeypatch):
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.transaction import saved_transaction_events
    from ..cancellation.test_report_sync_lifecycle import _fault_owned
    world = await _world(owned, tmp_path, monkeypatch)
    ticket, source_id, target = await _pending_read(world, tmp_path, monkeypatch, internal=False)
    horizon = _reopen(world)
    runtime = _obtain(world, devices={}, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
        recovery_max_event_id=lambda: horizon)
    finish_attempt = OperationRepository.finish_attempt
    submissions = []

    def lost_commit(repository, finish, key, current):
        assert finish.ticket == ticket
        submissions.append((finish, key))
        if len(submissions) == 1:
            actual = finish_attempt(repository, finish, key, _fault_owned(current, "COMMIT", after_commit=True))
            assert actual.kind is DbOutcomeKind.UNKNOWN
            return actual
        return finish_attempt(repository, finish, key, current)

    monkeypatch.setattr(OperationRepository, "finish_attempt", lost_commit)
    try:
        with pytest.raises(ConsistencyError):
            await advance_obtain(runtime)
        assert len(submissions) == 1
        original = submissions[0]
        assert saved_transaction_events(world.owned.connection, original[1]) is not None
        saved = _row(world, ticket)
        _reopen(world)
        runtime.owned = world.owned
        world.wall[0] += 5000
        await advance_obtain(runtime)
        assert submissions == [original, original]
        assert _row(world, ticket) == saved
        assert original[0].occurred_at < world.wall[0]
        assert world.owned.connection.execute("SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(original[1]),)).fetchone() == (1,)
        assert world.owned.connection.execute("SELECT status,json_extract(error_json,'$.code') FROM deliveries").fetchone() == (6, "device_binding_unavailable")
        assert world.owned.connection.execute("SELECT committed_bytes,source_size FROM file_copies WHERE id=?", (int(ticket.target_id),)).fetchone() == (4, 50)
        assert (Path(world.cfg.paths.staging) / target).read_bytes() == b"part"
        assert len(world.channel.streams) == 1
        assert not runtime.pending_read_results
    finally:
        world.owned.connection.close()


@pytest.mark.parametrize("pages", ["none", "partial"])
async def test_new_result_round_has_new_ticket_and_keeps_original_budget_and_pages(
        owned, tmp_path, monkeypatch, pages):
    world = await _world(owned, tmp_path, monkeypatch)
    runtime, ticket = await _result_attempt(world)
    original = None if pages == "none" else await _save_actual_page(world, runtime, ticket, monkeypatch, pages)
    horizon = _reopen(world)
    declaration = world.cfg.devices[BINDING.device_id]
    world.cfg = replace(world.cfg, devices={BINDING.device_id: {**declaration,
        "result_check": {**declaration["result_check"], "max_attempts": 2}}})
    world.channel.directory_end = b"END"
    world.channel.allowed_ticket = (ticket.run_id, ticket.attempt_id + 1)
    original_calls = len(world.channel.result_tickets)
    try:
        resumed = _capture(world, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=lambda: horizon)
        if pages == "none":
            async def elapsed(seconds):
                world.mono[0] += int(seconds * 1_000_000_000)
            monkeypatch.setattr(asyncio, "sleep", elapsed)
        await capture_handler(world.kind)(1, resumed)
        status, result, _error = _row(world, ticket)
        assert status == 4 and json.loads(result)["settlement"]["evidence"]["type"] == "adb_foreground_recovery"
        assert world.channel.result_tickets[original_calls:] == [(ticket.run_id, 2)]
        assert world.owned.connection.execute("SELECT attempt_no,max_attempts_used FROM operation_attempts"
            " WHERE run_id=? ORDER BY attempt_no", (ticket.run_id,)).fetchall() == [(1, 1), (2, 2)]
        assert world.owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (2,)
        if original is not None:
            assert resumed.capture.read_result_page(original.ref, world.owned) == original
        assert world.owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (3,)
    finally:
        world.owned.connection.close()

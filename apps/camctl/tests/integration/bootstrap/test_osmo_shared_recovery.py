"""正式 OSMO 共享恢复证据由拍摄与取回的原业务责任消费。"""
import asyncio
from dataclasses import replace
from decimal import Decimal
import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.outputs.obtain_flow import advance_obtain
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.capture.handlers import capture_handler
from camctl.capture.result_inputs import RESULT_PAGE_CONTRACT
from camctl.contracts.enums import enum_for
from camctl.devices import definitions_runtime
from camctl.devices.bindings import DeviceBinding
from camctl.devices.catalog import build_catalog
from camctl.devices.drivers import runtime as drivers_runtime
from camctl.devices.drivers.adb_cameras.registration import register_builtin_camera_drivers
from camctl.operations.process import LocalExit, RawToolOutcome
from camctl.operations.recovery import RecoveryBoundary
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository

from ..acceptance.test_acceptance import _plan_body
from ..acceptance.test_adb_camera_definitions import _params
from ..support.bootstrap_driver_cases import (driver_world_capture, driver_world_obtain,
    close_driver_world, reopen_driver_database, request_driver_cancel, interrupt_start_grant,
    interrupt_driver_result, interrupt_stop_grant, recover_through_capture, interrupt_read_intent, record_driver_state)
from ..scheduling.test_start_action import _accept_plan, _start, _SCHEDULED
from ..support.bootstrap_driver_cases import driver_owned as owned

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("isolated_driver_registries")]
BINDING = DeviceBinding("cam-1", "dji-osmo360-ii")
OSV_PATH = "/mnt/media_rw/emulated/DCIM/CAM_001/CAM_20261022100000_0007_D.OSV"


class Channel:
    """只提供正式设备工具的发送回执；恢复后禁止重开旧调用。"""
    result_path = OSV_PATH

    def __init__(self):
        self.calls = []
        self.result_paths = [self.result_path]
        self.forbidden = False
        self.metadata_error = False
        self.streams = []
        self.allowed_ticket = None
        self.current_result_ticket = lambda: None
        self.result_tickets = []
        self.capture_started = False
        self.read_content = None

    def _record_call(self, spec):
        ticket = self.current_result_ticket()
        assert not self.forbidden or (ticket is not None and ticket == self.allowed_ticket), "旧前台调用不得重放"
        self.calls.append(spec)
        return ticket

    def _directory_frame(self):
        return b"\0".join((b"CAMCTL-DIRECTORY/1",
            *(path.encode() for path in sorted(self.result_paths if self.capture_started else ())), b"END", b""))

    async def run(self, spec, stop):
        ticket = self._record_call(spec)
        script = shlex.split(spec.argv[5])[2]
        if "CAMCTL-DIRECTORY/1" in script:
            self.result_tickets.append(ticket)
            return RawToolOutcome(LocalExit(exit_code=0), self._directory_frame(),
                                  None, None, stderr=b"")
        if "CAMCTL-METADATA/2" in script:
            lexer = shlex.shlex(script, posix=True, punctuation_chars=";")
            lexer.whitespace_split = True
            tokens = list(lexer)
            start = tokens.index("for") + 3
            paths = tokens[start:tokens.index(";", start)]
            assert paths and all(path in self.result_paths for path in paths)
            records = []
            for path in paths:
                records.extend((path.encode(), b"" if self.metadata_error else b"50\n",
                    b"metadata error" if self.metadata_error else b"",
                    b"7" if self.metadata_error else b"0"))
            output = b"\0".join((b"CAMCTL-METADATA/2", *records, b"END", b""))
            return RawToolOutcome(LocalExit(exit_code=0), output, None, None, stderr=b"")
        if "sha256sum" in script and self.read_content is not None:
            import hashlib
            return RawToolOutcome(LocalExit(exit_code=0), hashlib.sha256(self.read_content).hexdigest().encode() + b"  -\n", None, None, stderr=b"")
        if "stat -c %s" in script:
            return RawToolOutcome(LocalExit(exit_code=7 if self.metadata_error else 0), b"" if self.metadata_error else b"50\n",
                                  None, None, stderr=b"metadata error" if self.metadata_error else b"")
        if shlex.split(script)[-2:] in (["02", "01"], ["01", "01"]):
            self.capture_started = True
        return RawToolOutcome(LocalExit(exit_code=0), b"Resp message, len = 1, data:\n 00\n",
                              None, None, stderr=b"")

    async def run_stream(self, spec, stop, receive):
        script = shlex.split(spec.argv[5])[2]
        if "CAMCTL-DIRECTORY/1" in script:
            ticket = self._record_call(spec)
            self.result_tickets.append(ticket)
            await receive(self._directory_frame())
            return RawToolOutcome(LocalExit(exit_code=0), b"", None, None, stderr=b"")
        assert not self.forbidden, "原READ不能重开源流"
        assert script.startswith("dd if="), "源READ必须沿正式文件读取接口调用"
        self.streams.append(spec)
        if self.read_content is not None:
            await receive(self.read_content)
            return RawToolOutcome(LocalExit(exit_code=0), None, None, None, stderr=b"")
        await receive(b"part")
        return RawToolOutcome(LocalExit(exit_code=7), None, None, None, stderr=b"source interrupted")


async def _world(owned, tmp_path, *, finite=False, binding=BINDING,
                 single_original=False, result_attempts=1):
    cfg = load_config({"paths": {name: str(tmp_path / name) for name in ("staging", "ready", "processing")},
        "devices": {"cam-1": {"kind": "camera", "driver": binding.driver_id,
            "video": {"result_mode": "segmented" if binding == BINDING and not single_original else "single_file"},
            "adb": {"serial": "serial-1"}, "recording": {"start_retry_interval_s": "0"},
            "result_check": {"max_attempts": result_attempts, "retry_interval_s": "0"},
            "copy": {"retry_interval_s": "0"}}}}, ConfigDefaults())
    for directory in (cfg.paths.staging, cfg.paths.ready, cfg.paths.processing):
        Path(directory).mkdir(parents=True, exist_ok=True)
    register_builtin_camera_drivers(cfg)
    registry = drivers_runtime.current_registry()
    entry = registry.entry(binding.driver_id)
    catalog = build_catalog(cfg, definitions_runtime.current_driver_definitions())
    kind = "camera_timelapse" if finite else "camera_record"
    body = _plan_body()
    body["actions"][0].update(type=kind, params=_params(binding.driver_id, kind))
    await _accept_plan(owned, tmp_path, body, catalog)
    frozen = json.loads(owned.connection.execute("SELECT execution_spec_json FROM actions WHERE id=1").fetchone()[0])
    assert frozen.get("result_policy") == (None if single_original or binding.driver_id != BINDING.driver_id else
        "osmo_empty_tail_classification_v1" if finite else "osmo360ii_record_segments_v1")
    started = _start(owned, 1, now=_SCHEDULED)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    activity_id = started.value.activity_id
    channel = Channel()
    entry.driver.transport = channel
    world = SimpleNamespace(owned=owned, cfg=cfg, registry=registry, entry=entry, channel=channel, binding=binding,
        kind=kind, activity_id=activity_id, wall=[_SCHEDULED], mono=[0],
        diagnostics=[], obtain_supervisors=[])
    channel.current_result_ticket = lambda: world.owned.connection.execute(
        "SELECT t.run_id,t.attempt_no FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
        " WHERE r.kind=7 AND t.status=1 ORDER BY t.id DESC LIMIT 1").fetchone()
    entry.driver.monotonic_ns = lambda: world.mono[0]
    return world


async def _capture(world, **kwargs):
    return await driver_world_capture(world, **kwargs)


async def _reopen(world):
    for supervisor in world.obtain_supervisors:
        result = await supervisor.drain_required()
        assert not result.failed, result.failed
    world.obtain_supervisors.clear()
    world.owned = await reopen_driver_database(world.owned)
    world.channel.forbidden = True
    world.wall[0] += 1000
    return world.owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()[0]


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


async def test_formal_registry_preserves_each_original_operation_and_result_page():
    cfg = load_config({"devices": {"cam-1": {"kind": "camera", "driver": BINDING.driver_id,
        "adb": {"serial": "serial-1"}}}}, ConfigDefaults())
    register_builtin_camera_drivers(cfg)
    entry = drivers_runtime.current_registry().entry(BINDING.driver_id)
    from camctl.operations.recovery import recovery_registry
    for operation in ("control", "stop", "result", "read"):
        evidence = recovery_registry(entry, operation)
        assert evidence is not None
        template = evidence.contract("adb_foreground_recovery", 1)
        assert template.operation == operation and not template.fields and template.identity_field is None
        assert evidence.contract(RESULT_PAGE_CONTRACT.type, RESULT_PAGE_CONTRACT.version) is entry.evidence.contract(
            RESULT_PAGE_CONTRACT.type, RESULT_PAGE_CONTRACT.version)
        returned = "result_returned" if operation == "result" else operation + "_returned"
        assert evidence.contract(returned, 1) is entry.evidence.contract(returned, 1)
    assert recovery_registry(entry, "digest") is None
    assert recovery_registry(entry, "query") is not None
    assert recovery_registry(entry, "unsupported") is None


@pytest.mark.parametrize("operation", ["control", "stop"])
@pytest.mark.parametrize("finite_cancel", [False, True])
async def test_original_control_stop_recovery_preserves_activity_and_finite_cancel(
        owned, tmp_path, monkeypatch, operation, finite_cancel):
    world = await _world(owned, tmp_path, finite=finite_cancel)
    runtime = await _capture(world)
    if operation == 'control':
        ticket = await interrupt_start_grant(runtime, 1, world.kind, monkeypatch)
        if finite_cancel:
            await request_driver_cancel(world.owned, 1, _SCHEDULED + 2,
                build_catalog(world.cfg, definitions_runtime.current_driver_definitions()))
    else:
        await capture_handler(world.kind)(1, runtime)
        assert world.channel.calls
        assert world.owned.connection.execute('SELECT sent_at FROM device_activities').fetchone() == (_SCHEDULED,)
        if finite_cancel:
            await request_driver_cancel(world.owned, 1, _SCHEDULED + 2,
                build_catalog(world.cfg, definitions_runtime.current_driver_definitions()))
        else:
            world.wall[0] += 11_000_000
            world.mono[0] += 11_000_000_000
        ticket = await interrupt_stop_grant(runtime, 1, world.kind, monkeypatch)
    activity = world.owned.connection.execute("SELECT * FROM device_activities").fetchall()
    calls = list(world.channel.calls)
    horizon = await _reopen(world)
    try:
        factory = await _capture(world, factory_only=True,
            recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=lambda: horizon)
        resumed = await factory(world.owned)
        assert _row(world, ticket) == (1, None, None)
        finish, key = await recover_through_capture(world, resumed, ticket, monkeypatch, factory=factory)
        _assert_unknown(world, ticket)
        assert finish.ticket == ticket
        from camctl.persistence.repositories.history import HistoryRepository
        history = HistoryRepository(Path(world.owned.connection.execute('PRAGMA database_list').fetchone()[2]))
        boundary = history.current_boundary()
        restored = history.restore_entity('action', 1, boundary)
        row = world.owned.connection.execute(
            'SELECT id,status,effect_state,result_json,error_json,result_event_id FROM operation_attempts '
            'WHERE run_id=? AND attempt_no=?', (ticket.run_id, ticket.attempt_id)).fetchone()
        historical = restored[('operation_attempts', row[0])]
        assert historical['run_id'] == ticket.run_id
        assert historical['attempt_no'] == ticket.attempt_id
        assert historical['status'] == row[1]
        assert historical['effect_state'] == row[2]
        assert historical['result_json'] == json.loads(row[3])
        assert historical['error_json'] == json.loads(row[4])
        assert historical['result_event_id'] == row[5]
        assert world.owned.connection.execute('SELECT occurred_at FROM history_events WHERE id=?',
            (row[5],)).fetchone() == (finish.occurred_at,)
        assert world.channel.calls == calls
        assert world.owned.connection.execute("SELECT * FROM device_activities").fetchall() == activity
        assert world.owned.connection.execute("SELECT cancel_requested FROM actions WHERE id=1").fetchone() == (int(finite_cancel),)
        if finite_cancel:
            expected = 4 if operation == "stop" else 5
            assert world.owned.connection.execute("SELECT status FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (expected,)
        assert world.owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
    finally:
        await close_driver_world(world)


async def _result_attempt(world, monkeypatch, *, elapsed_us=11_000_000, pages='none'):
    return await interrupt_driver_result(world, monkeypatch, elapsed_us=elapsed_us, pages=pages)


def _saved_actual_page(world, ticket, page_kind):
    original = world.saved_page
    if page_kind == 'none':
        assert original is None
        return None
    assert original.ref.ticket == ticket
    page = original.page
    if page_kind == 'terminal_error':
        assert page.outcome.status.name == 'FAILED'
        assert page.outcome.error.code == 'adb_metadata_failed'
        assert not page.scan_complete
    else:
        assert page.outcome.status.name == 'SUCCEEDED', page.outcome.error
        assert page.outcome.error is None
        assert (page.next_cursor is not None) == (page_kind == 'partial')
        assert page.scan_complete == (page_kind == 'terminal')
    assert len(page.entries) == 1
    assert page.entries[0].identity == OSV_PATH
    assert page.entries[0].size_bytes == (None if page_kind == 'terminal_error' else 50)
    completion = page.outcome.settlement.evidence.data['file_completion']
    spec, = world.owned.connection.execute("SELECT execution_spec_json FROM actions WHERE id=1").fetchone()
    wait_ms = json.loads(spec)["file_completion_wait_ms"]
    assert completion['required_wait_ms'] == wait_ms
    assert completion['observed_wait_ns'] == wait_ms * 1_000_000
    assert completion['completed'] is True
    assert _row(world, ticket) == (1, None, None)
    return original


@pytest.mark.parametrize("pages", ["none", "partial", "terminal", "terminal_error"])
async def test_original_result_pages_are_consumed_before_new_listing(owned, tmp_path, monkeypatch, pages):
    world = await _world(owned, tmp_path)
    runtime, ticket = await _result_attempt(world, monkeypatch, pages=pages)
    original = _saved_actual_page(world, ticket, pages)
    calls = list(world.channel.calls)
    horizon = await _reopen(world)
    try:
        resumed = await _capture(world, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=lambda: horizon)
        assert _row(world, ticket) == (1, None, None)
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
        await close_driver_world(world)


async def _obtain(world, **kwargs):
    return await driver_world_obtain(world, **kwargs)


async def _pending_read(world, tmp_path, monkeypatch, *, internal, lower_result_limit=False):
    runtime, result_ticket = await _result_attempt(world, monkeypatch,
        elapsed_us=5_000_000 if internal else 11_000_000, pages='terminal')
    original = _saved_actual_page(world, result_ticket, 'terminal')
    if internal:
        from camctl.bootstrap.flows import winddown_flow
        async def same_runtime(database, device_id):
            return runtime
        async def elapsed(seconds):
            world.mono[0] += int(seconds * 1_000_000_000)
        await winddown_flow(capture_factory=same_runtime, wait_cap_s=Decimal(0), sleep=elapsed)(
            SimpleNamespace(database=runtime.database,
                clock=SimpleNamespace(utc_micros=lambda: world.wall[0])))
        record_driver_state(world, "winddown_saved_required_check_and_source")

    horizon = await _reopen(world)
    if lower_result_limit:
        declaration = world.cfg.devices[BINDING.device_id]
        world.cfg = replace(world.cfg, devices={BINDING.device_id: {**declaration,
            "result_check": {**declaration["result_check"], "max_attempts": 1}}})
        assert world.owned.connection.execute(
            "SELECT attempts_used,max_attempts_used FROM operation_runs WHERE responsibility_key=?",
            (f"results/{world.activity_id}",)).fetchone() == (1, 3)
    runtime = await _capture(world, media_enabled=internal,
        recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
        recovery_max_event_id=lambda: horizon)
    world.channel.forbidden = False
    if internal:
        for step in range(4):
            await capture_handler(world.kind)(1, runtime)
            record_driver_state(world, f"normal_media_consumer_step_{step + 1}")
            if world.owned.connection.execute('SELECT committed_bytes FROM file_copies').fetchone() == (4,):
                break
        processing = world.owned.connection.execute('SELECT check_decision,source_device_file_id FROM recording_processing WHERE action_id=1').fetchone()
        assert processing[0] == int(enum_for('recording_processing.check_decision').REQUIRED)
        assert processing[1] is not None
    else:
        await capture_handler(world.kind)(1, runtime)
        assert world.owned.connection.execute('SELECT status FROM actions WHERE id=1').fetchone() == (3,)
        catalog = build_catalog(world.cfg, definitions_runtime.current_driver_definitions())
        body = _plan_body(request_id='73', actions=[{'name': '精确取回OSV', 'type': 'obtain_action_outputs',
            'scheduled_at': '2026-01-15 09:00:00', 'params': {'source': {'action_instance_id': '1'}, 'output_ids': ['1']}}])
        await _accept_plan(world.owned, tmp_path, body, catalog)
        runtime = await _obtain(world)
        await advance_obtain(runtime)
    source_id, = world.owned.connection.execute('SELECT id FROM device_files').fetchone()
    copy = world.owned.connection.execute(
        'SELECT id,committed_bytes,source_size FROM file_copies').fetchone()
    assert copy is not None, {'stage': '目标READ尚未执行',
        'facts': record_driver_state(world, 'before_expected_first_read')}
    copy_id, committed, length = copy
    assert (committed, length) == (4, 50)
    assert len(world.channel.streams) == 1
    first = world.owned.connection.execute('SELECT t.status FROM operation_attempts t JOIN operation_runs r'
        ' ON r.id=t.run_id WHERE r.copy_id=?', (copy_id,)).fetchone()
    assert first == (3,)
    ticket = await interrupt_read_intent(world, runtime, monkeypatch, internal=internal)
    assert world.owned.connection.execute('SELECT attempts_used FROM operation_runs WHERE id=?', (ticket.run_id,)).fetchone() == (2,)
    target = world.owned.connection.execute('SELECT f.relative_path FROM file_copies c JOIN intermediate_files f'
        ' ON f.id=c.target_file_id WHERE c.id=?', (copy_id,)).fetchone()[0]
    assert (Path(world.cfg.paths.staging) / target).read_bytes() == b'part'
    return ticket, source_id, target


async def test_current_lower_result_limit_keeps_original_media_read(owned, tmp_path, monkeypatch):
    world = await _world(owned, tmp_path, single_original=True, result_attempts=3)
    try:
        ticket, _, target = await _pending_read(
            world, tmp_path, monkeypatch, internal=True, lower_result_limit=True)
        assert ticket.operation == "read"
        assert (Path(world.cfg.paths.staging) / target).read_bytes() == b"part"
        assert world.owned.connection.execute(
            "SELECT attempts_used,max_attempts_used FROM operation_runs WHERE responsibility_key=?",
            (f"results/{world.activity_id}",)).fetchone() == (1, 3)
        run_id, = world.owned.connection.execute(
            "SELECT id FROM operation_runs WHERE responsibility_key=?",
            (f"results/{world.activity_id}",)).fetchone()
        assert [value for value in world.channel.result_tickets if value is not None] == [(run_id, 1)]
    finally:
        await close_driver_world(world)


@pytest.mark.parametrize("internal", [False, True])
@pytest.mark.parametrize("change", ["missing", "mismatch"])
async def test_unavailable_original_read_keeps_progress_and_uses_own_consumer(
        owned, tmp_path, monkeypatch, internal, change):
    world = await _world(owned, tmp_path, single_original=internal)
    ticket, source_id, target = await _pending_read(world, tmp_path, monkeypatch, internal=internal)
    source = world.owned.connection.execute("SELECT * FROM device_files WHERE id=?", (source_id,)).fetchone()
    progress = world.owned.connection.execute("SELECT committed_bytes,source_size,round,recopies_used,source_sha256,target_sha256,verification_state"
        " FROM file_copies WHERE id=?", (int(ticket.target_id),)).fetchone()
    history = world.owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()
    horizon = await _reopen(world)
    devices = {} if change == "missing" else {BINDING.device_id: {"kind": "camera", "driver": "dji-action6", "adb": {"serial": "never"}}}
    try:
        kwargs = dict(devices=devices, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=lambda: horizon)
        if internal:
            resumed = await _capture(world, **kwargs)
            await capture_handler(world.kind)(1, resumed)
            assert world.owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (4,)
            assert world.owned.connection.execute("SELECT check_state,json_extract(media_json,'$.error.code')"
                " FROM recording_processing WHERE action_id=1").fetchone() == (4, "device_binding_unavailable")
        else:
            resumed = await _obtain(world, **kwargs)
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
        await close_driver_world(world)


@pytest.mark.parametrize("consumer", ["capture", "read"])
@pytest.mark.parametrize("missing,reason", [("boundary", "UNCONFIRMED_BOUNDARY"),
    ("horizon", "MISSING_HORIZON"), ("authorization", "EVIDENCE_UNAVAILABLE")])
async def test_original_recovery_requires_boundary_horizon_and_authorization(
        owned, tmp_path, monkeypatch, consumer, missing, reason):
    from camctl.devices.drivers.registry import DriverRegistry
    world = await _world(owned, tmp_path)
    if consumer == "capture":
        original = await _capture(world)
        ticket = await interrupt_start_grant(original, 1, world.kind, monkeypatch)
    else:
        ticket, _, _ = await _pending_read(world, tmp_path, monkeypatch, internal=False)
    horizon = await _reopen(world)
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
            resumed = await _capture(world, **kwargs)
            await capture_handler(world.kind)(1, resumed)
        else:
            resumed = await _obtain(world, devices={}, **kwargs)
            await advance_obtain(resumed)
        diagnostic = resumed.last_recovery_diagnostic
        assert diagnostic is not None
        assert (diagnostic.reason.name, diagnostic.run_id, diagnostic.attempt_id) == (reason, ticket.run_id, ticket.attempt_id)
        assert _row(world, ticket) == (1, None, None)
        assert tuple(world.owned.connection.iterdump()) == before
        assert world.channel.calls == calls and world.channel.streams == streams
    finally:
        await close_driver_world(world)


async def test_formal_read_recovery_reuses_original_finish_after_real_unknown_commit(
        owned, tmp_path, monkeypatch):
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.repositories.transactions import saved_transaction_events
    from ..cancellation.test_report_sync_lifecycle import _fault_owned
    world = await _world(owned, tmp_path)
    ticket, source_id, target = await _pending_read(world, tmp_path, monkeypatch, internal=False)
    horizon = await _reopen(world)
    runtime = await _obtain(world, devices={}, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
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
        await _reopen(world)
        runtime = await _obtain(world, reuse=True)
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
        assert world.owned.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts WHERE run_id=? AND status=1",
            (ticket.run_id,)).fetchone() == (0,)
    finally:
        await close_driver_world(world)


@pytest.mark.parametrize("pages", ["none", "partial"])
async def test_new_result_round_has_new_ticket_and_keeps_original_budget_and_pages(
        owned, tmp_path, monkeypatch, pages):
    world = await _world(owned, tmp_path)
    runtime, ticket = await _result_attempt(world, monkeypatch, pages=pages)
    original = _saved_actual_page(world, ticket, pages)
    horizon = await _reopen(world)
    declaration = world.cfg.devices[BINDING.device_id]
    world.cfg = replace(world.cfg, devices={BINDING.device_id: {**declaration,
        "result_check": {**declaration["result_check"], "max_attempts": 2}}})
    world.channel.result_paths = [OSV_PATH]
    world.channel.allowed_ticket = (ticket.run_id, ticket.attempt_id + 1)
    original_calls = len(world.channel.result_tickets)
    try:
        resumed = await _capture(world, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
            recovery_max_event_id=lambda: horizon)
        if pages == "none":
            async def elapsed(seconds):
                world.mono[0] += int(seconds * 1_000_000_000)
            monkeypatch.setattr(asyncio, "sleep", elapsed)
        # 原票据恢复与新一轮派发可以分轮推进；等待真实新调用，仍保留原预算。
        async with asyncio.timeout(2):
            while len(world.channel.result_tickets) == original_calls:
                await capture_handler(world.kind)(1, resumed)
                assert resumed.last_recovery_diagnostic is None
                if len(world.channel.result_tickets) == original_calls:
                    assert world.owned.connection.execute(
                        "SELECT status FROM actions WHERE id=1").fetchone() == (2,), record_driver_state(
                            world, "new_result_call_not_dispatched")
                    assert world.owned.connection.execute(
                        "SELECT attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)
                    ).fetchone()[0] <= 2
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
        await close_driver_world(world)

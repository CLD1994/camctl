"""已取得媒体申请在实际拥有者结束之后仍属于本会话收场责任。"""

import asyncio
from dataclasses import replace

import pytest
import pytest_asyncio

from camctl.bootstrap.capture_assembly import session_capture_assembly
from camctl.capture.handlers import capture_handler
from camctl.capture.media_flow import run_recording_media
from camctl.capture.processing import CheckPhase
from camctl.contracts.values import ConsistencyError
from camctl.devices.bindings import DeviceConfigurationError
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.ports import DriverDeclaration
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.session.clock import ClockBecameUntrusted
from camctl.session.locks import acquire_session, probe_admission
from camctl.session.service import StateDbFailure, run_session

from ..capture.media_retry_fixtures import _Catalog, _EVIDENCE, media_pipeline as pipeline  # noqa: F401
from ..capture.test_capture_contract import ResultsDouble
from ..capture.test_input_copy import _CONTENT, _NOW
from ..session.test_session import FixedClock
from .test_media_assembly import _SessionDriver, _config


from ..support.recording_consumer_cases import recording_cases  # noqa: F401


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def held_media(pipeline, capture_resources, monkeypatch):
    from types import SimpleNamespace
    from camctl.bootstrap.work_file_assembly import WorkFileRuntime
    from camctl.outputs.work_files import WorkFileLimits
    from ..support.bootstrap_consumers import consumer_world, TransactionFault
    from ..support.session import session_context

    pending = {}
    work = WorkFileRuntime(staging=pipeline[1].staging, limits=WorkFileLimits(32, 1),
        pending_media_results=pending)
    world = await consumer_world(pipeline, capture_resources, branch="check",
        collections={"pending_media_results": pending}, executor_factory=lambda _owner: work.executor)
    await work.initialize(world.database)
    original_save = CaptureRepository.save_check_result
    attempts, errors = [], []

    def save(repository, command, key, current):
        if command.phase is CheckPhase.RUNNING:
            return original_save(repository, command, key, current)
        attempts.append((command, key))
        fault = TransactionFault(current.connection, after_commit=True)
        receipt = original_save(repository, command, key, replace(current, connection=fault))
        assert receipt.kind is DbOutcomeKind.UNKNOWN and fault.commit_calls == 1
        errors.append(receipt.error)
        return receipt

    monkeypatch.setattr(CaptureRepository, "save_check_result", save)
    try:
        with pytest.raises(ConsistencyError):
            await run_recording_media(world.runtime.media, 1, 1, world.source_id)
        assert len(attempts) == 1 and world.tools() == ["probe"]
        assert not work.executor.unfinished_files()
        assert work.required_settlements() == 1
        assert work.supervisor.outstanding() == work.file_supervisor.outstanding() == ()
        context = session_context(world.path, catalog=_Catalog(), clock=FixedClock(_NOW + 100),
            local_work=work)
        yield SimpleNamespace(world=world, work=work, context=context, attempts=attempts, errors=errors)
    finally:
        work.stop_new_work()
        await world.close()
        world.owned.connection.close()


async def test_media_pending_save_is_required_after_actual_owner_has_ended(held_media):
    assert held_media.work.required_settlements() == 1


async def test_media_pending_save_cannot_be_declared_settled_or_blindly_retried(held_media):
    state = held_media
    with pytest.raises(StateDbFailure) as failed:
        await state.work.settle()
    assert str(state.errors[0]) in str(failed.value)
    assert failed.value.__cause__ is state.errors[0]
    assert len(state.attempts) == state.work.required_settlements() == 1
    assert state.world.tools() == ["probe"] and len(state.world.reader.requests) == 1


@pytest.mark.parametrize("primary", ["state", "configuration", "clock", "cancel"])
async def test_media_unconfirmed_save_preserves_fatal_primary_and_secondary_state(held_media, primary):
    state, context = held_media, held_media.context
    errors = {"state": StateDbFailure("primary-state"),
        "configuration": DeviceConfigurationError("primary-configuration"),
        "clock": ClockBecameUntrusted("primary-clock"),
        "cancel": asyncio.CancelledError("primary-cancel")}
    original = errors[primary]
    restricted = []

    async def fatal(_context):
        raise original

    async def later(_context):
        restricted.append(1)

    context.flows = {"fatal": fatal}
    context.restricted_flows = {"winddown": later}
    context.once_report = later
    if primary == "cancel":
        with pytest.raises(asyncio.CancelledError) as canceled:
            await run_session(context, None)
        assert canceled.value is original
        assert str(state.errors[0]) in str(getattr(canceled.value, "__notes__", ()))
    else:
        outcome = await run_session(context, None)
        assert outcome.reason == {"state": "state_db_error", "configuration": "configuration_error",
            "clock": "clock_invalid"}[primary]
        assert f"primary-{primary}" in str(outcome.details)
        secondary = outcome.details.get("secondary_errors", ())
        assert any(item["reason"] == "state_db_error" and
            str(state.errors[0]) in str(item["details"]) for item in secondary)
    assert not restricted
    assert len(state.attempts) == state.work.required_settlements() == 1
    assert state.world.tools() == ["probe"] and len(state.world.reader.requests) == 1
    assert probe_admission(context.paths.admission_lock).is_free
    lease = acquire_session(context.paths.session_lock)
    lease.close()


async def _second_recording_cycle(pipeline, capture_resources):
    """第二个受理动作通过实际START、worker重开和受限STOP取得媒体输入。"""
    from decimal import Decimal
    from pathlib import Path
    from types import SimpleNamespace
    from camctl.bootstrap.capture_assembly import execution_wait_config
    from camctl.bootstrap.flows import capture_flow, winddown_flow
    from ..capture.media_retry_fixtures import original_recording_entry
    from ..capture.test_capture_contract import _entry
    from ..support.capture import capture_database
    from ..support.session import accept_plan

    owned, roots, _first_source = pipeline
    database = await capture_database(owned)

    class Catalog(_Catalog):
        def device_exists(self, device_id):
            return device_id in {"cam-1", "cam-2"}

        def driver_id(self, device_id):
            return "camctl-adb" if self.device_exists(device_id) else None

    from datetime import datetime, timezone
    instant = datetime.fromtimestamp(_NOW // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await accept_plan(database, roots.staging.parent, Catalog(), {
        "request_id": "3", "created_at": instant, "name": "第二个真实媒体责任",
        "actions": [{"name": "第二实际录像", "type": "camera_record", "device_id": "cam-2",
            "scheduled_at": instant, "params": {"type": "video"},
            "policy": {"max_delay_ms": 5000}}]}, FixedClock(_NOW))
    action_id, = owned.connection.execute("SELECT id FROM actions WHERE name='第二实际录像'").fetchone()
    assert action_id != 1

    class Driver(_SessionDriver):
        async def read_directory(self, request, *, stop):
            from camctl.contracts.pages import Page
            from camctl.devices.directory import DirectoryRead
            return DirectoryRead(None, None, Page((), None))

        async def control(self, request):
            result = await super().control(request)
            return replace(result, observations=tuple(replace(observation,
                data={**observation.data, "activity_id": request.ticket.target_id})
                for observation in result.observations))

        async def stop(self, request):
            result = await super().stop(request)
            return replace(result, observations=tuple(replace(observation,
                data={**observation.data, "activity_id": request.ticket.target_id})
                for observation in result.observations))

    driver = Driver(_CONTENT)
    entry = DriverEntry("camctl-adb", driver, DriverDeclaration(
        control_supported=True, stop_supported=True, query_supported=False,
        result_supported=False, read_supported=False, digest_supported=False,
        delete_supported=False, directory_supported=True), _EVIDENCE, DriverStatus.SOFTWARE_CONTRACT_VERIFIED)
    original_config = _config(roots.staging.parent)
    config = replace(original_config, devices={**original_config.devices,
        "cam-2": {**original_config.devices["cam-1"]}})
    results = ResultsDouble({1: (original_recording_entry(),)})
    clock = {"ns": 7_000_000_000}
    options = dict(devices=config.devices, drivers=DriverRegistry((entry,)),
        results=results, staging=roots.staging, wait_config=execution_wait_config,
        wall_us=lambda: _NOW, monotonic_ns=lambda: clock["ns"], media_enabled=False)
    factory = session_capture_assembly(**options)
    # 已停止的首动作释放设备，原检查责任保持等待媒体执行资格。
    await capture_handler("camera_record")(1, await factory(database, "cam-1"))
    flow = capture_flow(factory)
    await flow(SimpleNamespace(database=database, clock=FixedClock(_NOW)))
    await flow.settle()
    started_outcome = await capture_handler("camera_record")(action_id, await factory(database, "cam-2"))
    activity_id, started = owned.connection.execute(
        "SELECT id,started_at IS NOT NULL FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
    assert started, started_outcome
    assert [operation for _kind, operation in driver.calls] == ["start_recording"]
    results.files_by_action[activity_id] = (_entry("original-recording-2", size=len(_CONTENT)),)
    database = await capture_resources.restart_database(Path(config.paths.state_db))
    clock["ns"] = 20_000_000_000
    cold = session_capture_assembly(**options)

    async def elapsed(seconds):
        clock["ns"] += int(seconds * 1_000_000_000)

    await winddown_flow(capture_factory=cold, wait_cap_s=Decimal("6"), sleep=elapsed)(
        SimpleNamespace(database=database, clock=FixedClock(_NOW)))
    processing_id, source_id, decision = owned.connection.execute(
        "SELECT id,source_device_file_id,check_decision FROM recording_processing WHERE action_id=?", (action_id,)).fetchone()
    assert processing_id != 1 and source_id is not None and decision == 3
    assert [operation for _kind, operation in driver.calls] == ["start_recording", "stop_recording"]
    return action_id, processing_id, source_id, config


@pytest_asyncio.fixture
async def two_held_media(pipeline, capture_resources, monkeypatch):
    from types import SimpleNamespace
    from camctl.bootstrap.work_file_assembly import WorkFileRuntime
    from camctl.outputs.work_files import WorkFileLimits
    from ..support.bootstrap_consumers import consumer_world, TransactionFault

    second_action, second_processing, second_source, configuration = await _second_recording_cycle(pipeline, capture_resources)
    pending = {}
    work = WorkFileRuntime(staging=pipeline[1].staging, limits=WorkFileLimits(32, 1),
        pending_media_results=pending)
    world = await consumer_world(pipeline, capture_resources, branch="check",
        collections={"pending_media_results": pending}, executor_factory=lambda _owner: work.executor)
    world.config = configuration
    world.factory = world.new_factory()
    world.runtime = await world.factory(world.database, "cam-1")
    second_runtime = await world.factory(world.database, "cam-2")
    await work.initialize(world.database)
    original_save = CaptureRepository.save_check_result
    attempts, errors = [], []

    def save(repository, command, key, current):
        if command.phase is CheckPhase.RUNNING:
            return original_save(repository, command, key, current)
        attempts.append((command, key))
        fault = TransactionFault(current.connection, after_commit=True)
        receipt = original_save(repository, command, key, replace(current, connection=fault))
        assert receipt.kind is DbOutcomeKind.UNKNOWN and fault.commit_calls == 1
        errors.append(receipt.error)
        return receipt

    monkeypatch.setattr(CaptureRepository, "save_check_result", save)
    try:
        for runtime, action_id, processing_id, source_id in ((world.runtime, 1, 1, world.source_id),
                (second_runtime, second_action, second_processing, second_source)):
            with pytest.raises(ConsistencyError):
                await run_recording_media(runtime.media, action_id, processing_id, source_id)
        assert len(attempts) == work.required_settlements() == 2
        assert world.tools() == ["probe", "probe"] and len(world.reader.requests) == 2
        yield SimpleNamespace(world=world, work=work, attempts=attempts, errors=errors,
            second=second_processing)
    finally:
        work.stop_new_work()
        await world.close()
        world.owned.connection.close()


@pytest.mark.parametrize("pipeline", [True], indirect=True)
async def test_settlement_reports_every_unverified_media_reason(two_held_media):
    state = two_held_media
    with pytest.raises(StateDbFailure) as failed:
        await state.work.settle()
    assert "processing_id=1" in str(failed.value) and f"processing_id={state.second}" in str(failed.value)
    assert failed.value.__cause__ is state.errors[0]
    assert len(state.attempts) == state.work.required_settlements() == 2
    assert state.world.tools() == ["probe", "probe"] and len(state.world.reader.requests) == 2


@pytest.mark.parametrize("pipeline", [True], indirect=True)
async def test_settlement_reports_maintenance_and_media_reasons_together(two_held_media):
    from camctl.session.supervision import OwnedTask
    state = two_held_media

    async def maintenance():
        raise OSError("维护清理收场失败: injected")

    task = asyncio.create_task(maintenance())
    token = state.work.supervisor.register(state.work)
    state.work.supervisor.handoff(token, OwnedTask(
        "cleanup/9", "delete", "维护清理", ("state_db",), task))
    with pytest.raises(StateDbFailure) as failed:
        await state.work.settle()
    message = str(failed.value)
    assert "维护清理收场失败: injected" in message
    assert "processing_id=1" in message and f"processing_id={state.second}" in message
    assert state.work.required_settlements() == 2 and len(state.attempts) == 2


async def test_default_session_reports_unconfirmed_media_save_after_real_probe(tmp_path, capture_resources, monkeypatch, recording_cases):
    from pathlib import Path
    import os
    import sqlite3
    from camctl.acceptance.service import CommandMode
    from camctl.bootstrap import lifecycle, capture_assembly
    from camctl.bootstrap.lifecycle import build_runtime, close_runtime
    from support.repository_ports import repository_port
    from ..support.bootstrap_consumers import TransactionFault
    from ..support.recording_consumer_cases import NOW, reconciliation_case
    from ..support.session import ResultDriver, run_default_session
    from ..session.test_session import FixedClock

    from ..support import recording_consumer_cases as recording_support
    from camctl.devices.evidence import DeviceObservation, EvidenceContract
    from camctl.devices.ports import DeviceCallResult
    from camctl.devices.tasks import StartReturn
    from camctl.devices.read_session import ReadSession
    from .test_media_assembly import _MemoryStream
    closed = []
    class TrackedStream(_MemoryStream):
        def close(self):
            closed.append(True)
            return super().close()
    class SentDriver(recording_support.RecordingDriver):
        async def open_read(self, source, offset, ticket, *, idle_timeout_s):
            self.calls.append(("read", source.file_id))
            return ReadSession(source, offset, TrackedStream(self._content[offset:]), idle_timeout_s)
        async def control(self, request):
            self.calls.append(("control", request.operation))
            return DeviceCallResult((DeviceObservation("recording_sent", 1,
                {"activity_id": str(request.ticket.target_id)}),), None)
    evidence = recording_support.EVIDENCE.with_contract(EvidenceContract(
        "recording_sent", 1, "control", frozenset({"activity_id"}), identity_field="activity_id"))
    monkeypatch.setattr(recording_support, "RecordingDriver", SentDriver)
    monkeypatch.setattr(recording_support, "EVIDENCE", evidence)
    class MarginCatalog(recording_support.ResultCatalog):
        def parameter_definition(self, device_id, action_type, parameter_type):
            definition = super().parameter_definition(device_id, action_type, parameter_type)
            if definition is None or action_type != "camera_record":
                return definition
            factory = definition.task_factory
            return replace(definition, task_factory=lambda params: replace(
                factory(params), startup_margin_ms=10_000, start_return_meaning=StartReturn.SENT))
    monkeypatch.setattr(recording_support, "ResultCatalog", MarginCatalog)
    from datetime import datetime, timezone
    from decimal import Decimal
    from camctl.host_files.media import MediaProbe, MediaStream
    from camctl.persistence.repositories.scheduling import SchedulingRepository, ObserveWindowRequest, StartActionRequest
    from ..capture.test_capture_contract import _entry
    from ..support.capture_media_cases import ScriptTools
    tools = ScriptTools(MediaProbe(Decimal("200"), None, (
        MediaStream(0, "video", "h264", 64, 64, Decimal("200"), False),)))
    case = recording_support.RecordingCase(tmp_path, tools=tools)
    started_at = NOW - 75_000_000
    case.wall[0] = started_at
    await case.build()
    instant = datetime.fromtimestamp(started_at // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await case.accept({"request_id": "1", "created_at": instant, "name": "录像消费者",
        "actions": [{"name": "拍摄", "type": "camera_record", "device_id": "cam-1",
            "scheduled_at": instant, "params": {"type": "video"}, "policy": {"max_delay_ms": 5000}}]})
    for method, request in ((SchedulingRepository().observe_window, ObserveWindowRequest(1, started_at, started_at)),
            (SchedulingRepository().start_action, StartActionRequest(1, started_at, started_at))):
        receipt = await case.write(method, request)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    await case.advance()
    assert case.row("SELECT status,execution_started FROM actions WHERE id=1") == (2, 1)
    case.files(_entry("clip-1", size=len(recording_support.CONTENT)))
    await case.restart(wall=NOW)
    cfg, db, driver, tools = case.config, case.path, case.driver, case.tools
    driver.list_results = ResultDriver(case.results).list_results
    entry = case.registry.entry("camctl-adb")
    entry = replace(entry, declaration=replace(entry.declaration, result_supported=True))
    tools_dir = tools.directory
    for name in ("ffprobe", "ffmpeg"):
        source = tools_dir / ("probe" if name == "ffprobe" else "repair")
        destination = tools_dir / name
        destination.write_text("#!/bin/sh\nexec " + str(source) + ' "$@"\n')
        destination.chmod(0o755)
    await case.close()
    await capture_resources.close()
    monkeypatch.setenv("PATH", str(tools_dir) + ":" + os.environ["PATH"])
    monkeypatch.setattr(lifecycle, "SystemClock", lambda: FixedClock(NOW))
    real_time = capture_assembly.time
    class TimePort:
        def monotonic_ns(self):
            return 80_000_000_000
        def time(self):
            return NOW / 1_000_000
        def __getattr__(self, name):
            return getattr(real_time, name)
    monkeypatch.setattr(capture_assembly, "time", TimePort())
    requests, errors = [], []

    class SaveObserver(CaptureRepository):
        def save_check_result(self, command, key, current):
            if command.phase is CheckPhase.RUNNING:
                return super().save_check_result(command, key, current)
            requests.append((command, key))
            fault = TransactionFault(current.connection, after_commit=True)
            result = super().save_check_result(command, key, replace(current, connection=fault))
            assert fault.commit_calls == 1 and result.kind is DbOutcomeKind.UNKNOWN
            errors.append(result.error)
            return result

    deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
    try:
        with repository_port(CaptureRepository, SaveObserver(), ("save_check_result",),
                matches=lambda _name, args, _kwargs: args[0].processing_id == 1):
            outcome = await run_default_session(deps, None, entry)
    finally:
        close_runtime(deps)
    assert not outcome.succeeded and outcome.reason == "state_db_error"
    assert len(requests) == len(errors) == 1
    assert str(errors[0]) in str(outcome.details)
    assert tools.calls == ["probe"]
    assert len([call for call in driver.calls if call[0] == "read"]) == 1 and len(closed) == 1
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (2,)
        assert connection.execute("SELECT check_state,check_decision FROM recording_processing WHERE id=1").fetchone() == (3, 3)
        assert connection.execute("SELECT committed_bytes FROM file_copies").fetchone() == (len(recording_support.CONTENT),)
    assert (Path(cfg.paths.staging) / "recording-inputs/1.mp4").read_bytes() == recording_support.CONTENT
    assert probe_admission(deps.admission_lock).is_free
    lease = acquire_session(deps.session_lock)
    lease.close()

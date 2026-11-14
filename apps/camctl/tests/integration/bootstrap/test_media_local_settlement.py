"""已取得媒体申请在实际拥有者结束之后仍属于本会话收场责任。"""

import asyncio
from dataclasses import replace

import pytest

from camctl.capture.processing import CheckPhase
from camctl.devices.bindings import DeviceConfigurationError
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.session.clock import ClockBecameUntrusted
from camctl.session.locks import acquire_session, probe_admission
from camctl.session.service import StateDbFailure, run_session

from ..capture.media_retry_fixtures import _Catalog
from ..capture.test_input_copy import _NOW
from ..session.test_session import FixedClock


from ..support.recording_consumer_cases import recording_cases  # noqa: F401


pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("primary", ["state", "configuration", "clock", "cancel"])
async def test_local_settlement_failures_preserve_fatal_primary(tmp_path, primary):
    from pathlib import Path
    from camctl.bootstrap.background_flow import CombinedLocalWork
    from ..support.session import session_config, session_context
    from camctl.persistence.initialization import InitOutcome, initialize_state

    class Owner:
        def __init__(self, name):
            self.error = StateDbFailure(name)
            self.settled = False

        def required_settlements(self):
            return 1

        def stop_new_work(self):
            pass

        async def settle(self):
            self.settled = True
            raise self.error

    owners = [Owner("settlement-first"), Owner("settlement-second")]
    cfg = session_config(tmp_path)
    path = Path(cfg.paths.state_db)
    assert initialize_state(cfg, path).outcome is InitOutcome.CREATED
    context = session_context(path, catalog=_Catalog(), clock=FixedClock(_NOW + 100),
        local_work=CombinedLocalWork(owners))
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
        details = str(getattr(canceled.value, "__notes__", ()))
    else:
        outcome = await run_session(context, None)
        assert outcome.reason == {"state": "state_db_error", "configuration": "configuration_error",
            "clock": "clock_invalid"}[primary]
        assert f"primary-{primary}" in str(outcome.details)
        secondary = outcome.details.get("secondary_errors", ())
        assert any(item["reason"] == "state_db_error" for item in secondary)
        details = str(secondary)
    assert details.index("settlement-first") < details.index("settlement-second")
    assert all(owner.settled for owner in owners)
    assert not restricted
    assert probe_admission(context.paths.admission_lock).is_free
    lease = acquire_session(context.paths.session_lock)
    lease.close()


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
    requests, receipts, errors = [], [], []

    class SaveObserver(CaptureRepository):
        def save_check_result(self, command, key, current):
            if command.phase is CheckPhase.RUNNING:
                return super().save_check_result(command, key, current)
            requests.append((command, key))
            if len(requests) == 1:
                fault = TransactionFault(current.connection, after_commit=True)
                result = super().save_check_result(command, key, replace(current, connection=fault))
                assert fault.commit_calls == 1 and result.kind is DbOutcomeKind.UNKNOWN
                errors.append(result.error)
            else:
                result = super().save_check_result(command, key, current)
            receipts.append(result.kind)
            return result

    deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
    try:
        with repository_port(CaptureRepository, SaveObserver(), ("save_check_result",),
                matches=lambda _name, args, _kwargs: args[0].processing_id == 1):
            outcome = await run_default_session(deps, None, entry)
    finally:
        close_runtime(deps)
    assert not outcome.succeeded and outcome.reason == "state_db_error"
    assert len(errors) == 1 and requests
    assert all(request == requests[0] for request in requests)
    assert receipts == [DbOutcomeKind.UNKNOWN] + [DbOutcomeKind.COMPLETED] * (len(requests) - 1)
    assert str(errors[0]) in str(outcome.details)
    assert tools.calls == ["probe"]
    assert len([call for call in driver.calls if call[0] == "read"]) == 1 and len(closed) == 1
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
                                  (requests[0][1],)).fetchone() == (1,)
        assert connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (2,)
        assert connection.execute("SELECT check_state,check_decision FROM recording_processing WHERE id=1").fetchone() == (3, 3)
        assert connection.execute("SELECT committed_bytes FROM file_copies").fetchone() == (len(recording_support.CONTENT),)
    assert (Path(cfg.paths.staging) / "recording-inputs/1.mp4").read_bytes() == recording_support.CONTENT
    assert probe_admission(deps.admission_lock).is_free
    lease = acquire_session(deps.session_lock)
    lease.close()

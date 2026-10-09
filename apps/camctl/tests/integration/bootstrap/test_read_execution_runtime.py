"""真实读取入口先提交原尝试，再把原票据和当次无数据阈值交给设备。"""

from decimal import Decimal
from pathlib import Path

import pytest

from camctl.bootstrap.capture_assembly import _media_flow_with
from camctl.bootstrap.obtain_assembly import obtain_flow, session_obtain_assembly
from camctl.capture.recovery import RecoveryBoundary
from camctl.capture.media_flow import run_recording_media
from camctl.devices.drivers.registry import DriverEntry, DriverStatus
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DriverDeclaration
from camctl.devices.read_session import ReadSession
from camctl.operations.attempts import RetryWaitGate, seconds_from_json
from camctl.outputs.obtain_flow import advance_obtain

from ..capture.test_input_copy import _CONTENT as _MEDIA_CONTENT, pipeline
from ..capture.test_media_flow import ProbeTools
from camctl.host_files.media import MediaProbe
from .test_output_binding_changes import (
    _CONTENT, _NOW, _accept, _registry, _save_photos, environment)
from .test_obtain_flow import _MemoryStream
from .test_output_binding_transactions import pending_read
from .test_output_read_recovery import _start_unended_read

pytestmark = pytest.mark.asyncio

_SETTINGS = {"max_read_attempts": 7, "read_idle_timeout_s": Decimal("1.25"),
             "max_recopies": 2, "retry_interval_s": Decimal("0")}
_READ_EVIDENCE = EvidenceRegistry((EvidenceContract(
    "read_returned", 1, "read", frozenset()),))


class ReadRequests:
    """真实 ReadSession 的端口替身；在调用时读取已经提交的原意图。"""

    def __init__(self, owned, content, *, fail=False):
        self.owned, self.content = owned, content
        self.requests = []
        self.fail = fail

    async def open_read(self, source, offset, ticket, *, idle_timeout_s: Decimal):
        snapshot = None
        if ticket is not None:
            snapshot = self.owned.connection.execute(
                "SELECT a.status,a.result_json,r.attempts_used,r.max_attempts_used,r.timeout_s_json"
                " FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
                " WHERE r.id=? AND a.attempt_no=?", (ticket.run_id, ticket.attempt_id)).fetchone()
        self.requests.append((ticket, idle_timeout_s, snapshot, offset))
        return ReadSession(source, offset,
                           _FailureStream() if self.fail else _MemoryStream(self.content[offset:]),
                           idle_timeout_s)


class _FailureStream(_MemoryStream):
    def __init__(self):
        super().__init__(b"")

    def read(self, limit):
        raise OSError("驱动明确报告读取失败")


async def _ordinary(environment):
    cfg, owned, context, capture_driver = environment
    await _save_photos(cfg, owned, context, capture_driver)
    _accept(owned, "2", [{"name": "取回", "type": "obtain_action_outputs",
        "scheduled_at": "2026-01-15 09:00:00", "params": {
            "source": {"plan_instance_id": "1", "group": "files"}}}])
    declarations = {device: {**declaration, "copy": _SETTINGS}
                    for device, declaration in cfg.devices.items()}
    driver = ReadRequests(owned, _CONTENT)
    factory = session_obtain_assembly(
        devices=declarations, drivers=_registry(driver), staging=Path(cfg.paths.staging),
        ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing), segment_size=4,
        occurred_at=lambda: _NOW, monotonic_ns=lambda: 0)
    await obtain_flow(factory)(context)
    return owned, driver


def _internal_flow(pipeline, *, fail=False, maximum=7, checksum_support=3):
    owned, roots = pipeline[0:2]
    # 夹具明确声明原片不提供摘要；内容完整时采用正式降级校验。
    owned.connection.execute("UPDATE device_files SET checksum_support=? WHERE id=11", (checksum_support,))
    owned.connection.commit()
    driver = ReadRequests(owned, _MEDIA_CONTENT, fail=fail)
    entry = DriverEntry("camctl-adb", driver,
        DriverDeclaration(False, False, False, False, True, False, False),
        _READ_EVIDENCE, DriverStatus.SOFTWARE_CONTRACT_VERIFIED)
    flow = _media_flow_with(
        owned, entry, "cam-1", "camctl-adb", ProbeTools(MediaProbe(Decimal("61"), None)),
        roots.staging, lambda: _NOW, {"copy": {**_SETTINGS, "max_read_attempts": maximum}}, RetryWaitGate(), lambda: 0)
    assert flow is not None
    return flow, driver


async def _internal(pipeline, *, fail=False, maximum=7):
    flow, driver = _internal_flow(pipeline, fail=fail, maximum=maximum)
    await run_recording_media(flow, 1, 1, 11)
    return flow.owned, driver


async def test_internal_read_exhaustion_finishes_required_check_and_releases_source(pipeline):
    owned, driver = await _internal(pipeline, fail=True, maximum=1)

    assert len(driver.requests) == 1
    assert owned.connection.execute(
        "SELECT attempts_used,status FROM operation_runs WHERE copy_id="
        " (SELECT id FROM file_copies WHERE processing_id=1)").fetchone() == (1, 4)
    assert owned.connection.execute(
        "SELECT check_state,repair_state FROM recording_processing WHERE id=1").fetchone() == (4, 2)
    assert owned.connection.execute(
        "SELECT slot_device_id FROM file_copies WHERE processing_id=1").fetchone() == (None,)


@pytest.mark.parametrize("new_round", [False, True])
async def test_actual_read_result_is_retained_and_retried_with_original_key(pending_read, monkeypatch, new_round):
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.persistence.repositories.operations import OperationRepository

    cfg, owned, command = pending_read
    driver = ReadRequests(owned, _CONTENT)
    factory = _configured_factory(cfg, driver, maximum=7)
    runtime = factory(owned)
    original = OperationRepository.finish_attempt
    saves = []

    def fail_once(repository, finish, key, connection):
        saves.append((finish, key))
        if len(saves) == 1:
            return DbOutcome(DbOutcomeKind.ROLLED_BACK, error=RuntimeError("结果保存回滚"))
        return original(repository, finish, key, connection)

    monkeypatch.setattr(OperationRepository, "finish_attempt", fail_once)
    with pytest.raises(ConsistencyError):
        await advance_obtain(runtime)
    assert len(driver.requests) == 1

    await advance_obtain(factory(owned) if new_round else runtime)

    assert len(driver.requests) == 1
    assert len(saves) == 2
    assert saves[1] == saves[0]
    assert owned.connection.execute(
        "SELECT status,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
        (saves[0][0].ticket.run_id, saves[0][0].ticket.attempt_id)).fetchone()[1] is not None


async def test_internal_actual_read_result_is_retained_before_check(pipeline, monkeypatch):
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.persistence.repositories.operations import OperationRepository

    flow, driver = _internal_flow(pipeline)
    original = OperationRepository.finish_attempt
    saves = []

    def fail_once(repository, finish, key, connection):
        saves.append((finish, key))
        if len(saves) == 1:
            return DbOutcome(DbOutcomeKind.ROLLED_BACK, error=RuntimeError("结果保存回滚"))
        return original(repository, finish, key, connection)

    monkeypatch.setattr(OperationRepository, "finish_attempt", fail_once)
    with pytest.raises(ConsistencyError):
        await run_recording_media(flow, 1, 1, 11)
    assert flow.tools.calls == []

    await run_recording_media(flow, 1, 1, 11)

    assert len(driver.requests) == 1
    assert len(saves) == 2
    assert saves[1] == saves[0]
    assert flow.tools.calls == ["probe"]


@pytest.mark.parametrize("consumer", ["obtain", "media"])
async def test_actual_read_call_carries_current_idle_threshold(request, consumer):
    fixture = request.getfixturevalue("environment" if consumer == "obtain" else "pipeline")
    _owned, driver = await (_ordinary(fixture) if consumer == "obtain" else _internal(fixture))

    assert driver.requests
    assert all(request[1] == Decimal("1.25") for request in driver.requests)


@pytest.mark.parametrize("consumer", ["obtain", "media"])
async def test_actual_read_call_has_committed_original_ticket_and_current_budget(request, consumer):
    fixture = request.getfixturevalue("environment" if consumer == "obtain" else "pipeline")
    owned, driver = await (_ordinary(fixture) if consumer == "obtain" else _internal(fixture))

    assert driver.requests
    for ticket, _idle, snapshot, _offset in driver.requests:
        assert ticket is not None
        assert ticket.operation == "read"
        assert ticket.responsibility_key.startswith("read/")
        assert snapshot is not None
        assert snapshot[:4] == (1, None, 1, 7)
        assert seconds_from_json(snapshot[4]) == Decimal("1.25")
        assert owned.connection.execute(
            "SELECT status,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone()[1] is not None


def _configured_factory(cfg, driver, *, maximum, idle=Decimal("1.25")):
    declarations = {device: {**declaration, "copy": {
        "max_read_attempts": maximum, "read_idle_timeout_s": idle,
        "max_recopies": 1, "retry_interval_s": Decimal("0")}}
        for device, declaration in cfg.devices.items()}
    return session_obtain_assembly(
        devices=declarations, drivers=_registry(driver), staging=Path(cfg.paths.staging),
        ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing), segment_size=4,
        occurred_at=lambda: _NOW, monotonic_ns=lambda: 0)


@pytest.mark.parametrize("maximum,additional", [(1, 0), (5, 3)])
async def test_later_run_uses_current_budget_after_two_real_read_failures(pending_read, environment, maximum, additional):
    cfg, owned, command = pending_read
    context = environment[2]
    driver = ReadRequests(owned, _CONTENT, fail=True)
    await obtain_flow(_configured_factory(cfg, driver, maximum=3))(context)
    assert owned.connection.execute(
        "SELECT attempts_used,status FROM operation_runs WHERE copy_id=?", (command.copy_id,)).fetchone() == (2, 2)
    before = owned.connection.execute(
        "SELECT attempt_no,status,result_json,error_json FROM operation_attempts WHERE run_id="
        " (SELECT id FROM operation_runs WHERE copy_id=?) ORDER BY attempt_no", (command.copy_id,)).fetchall()
    driver.requests.clear()
    factory = _configured_factory(cfg, driver, maximum=maximum)

    for _ in range(4):
        await obtain_flow(factory)(context)

    assert len(driver.requests) == additional
    assert owned.connection.execute(
        "SELECT attempts_used,status FROM operation_runs WHERE copy_id=?", (command.copy_id,)).fetchone() == (2 + additional, 4)
    assert owned.connection.execute(
        "SELECT attempt_no,status,result_json,error_json FROM operation_attempts WHERE run_id="
        " (SELECT id FROM operation_runs WHERE copy_id=?) AND attempt_no<=2 ORDER BY attempt_no",
        (command.copy_id,)).fetchall() == before
    assert owned.connection.execute(
        "SELECT committed_bytes,slot_device_id FROM file_copies WHERE id=?", (command.copy_id,)).fetchone() == (4, None)
    assert owned.connection.execute(
        "SELECT source_dependency FROM obtain_items WHERE delivery_id="
        " (SELECT delivery_id FROM file_copies WHERE id=?)", (command.copy_id,)).fetchone() == (0,)


async def test_later_run_resumes_original_running_ticket_even_when_new_budget_is_lower(pending_read, environment):
    cfg, owned, command = pending_read
    ticket = _start_unended_read(owned, command.copy_id)
    driver = ReadRequests(owned, _CONTENT)
    runtime = _configured_factory(cfg, driver, maximum=1, idle=Decimal("2.75"))(owned)
    runtime.recovery_boundary = RecoveryBoundary.HOST_LOCAL_SETTLED
    runtime.recovery_max_event_id = owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0]
    attempts_before = owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone()[0]

    await advance_obtain(runtime)

    assert len(driver.requests) == 1
    actual, idle, snapshot, offset = driver.requests[0]
    assert actual == ticket
    assert idle == Decimal("2.75")
    assert snapshot[:4] == (1, None, 2, 1)
    assert offset == 4
    assert owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone()[0] == attempts_before
    assert owned.connection.execute(
        "SELECT status,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
        (ticket.run_id, ticket.attempt_id)).fetchone()[1] is not None


async def test_cancelled_obtain_read_wait_keeps_actual_source_and_original_result_owned(pending_read):
    await _assert_cancelled_read_owned(pending_read, "obtain")


async def test_cancelled_media_read_wait_keeps_actual_source_and_original_result_owned(pipeline):
    await _assert_cancelled_read_owned(pipeline, "media")


async def _assert_cancelled_read_owned(fixture, consumer):
    import asyncio

    from camctl.host_files.tasks import FileTaskExecutor
    from camctl.session.supervision import Supervisor

    supervisor = Supervisor()
    executor = FileTaskExecutor(supervisor)
    waiting, released = asyncio.Event(), asyncio.Event()

    class DelayedShutdown:
        def __init__(self, session):
            self.session = session

        def position(self):
            return self.session.position()

        def read_chunk(self, limit):
            return self.session.read_chunk(limit)

        def request_stop(self):
            self.session.request_stop()

        def poll_stopped(self):
            return self.session.poll_stopped()

        async def wait_stopped(self):
            waiting.set()
            await released.wait()
            return await self.session.wait_stopped()

    class Driver(ReadRequests):
        async def open_read(self, source, offset, ticket, *, idle_timeout_s):
            session = await super().open_read(source, offset, ticket, idle_timeout_s=idle_timeout_s)
            return DelayedShutdown(session)

    if consumer == "obtain":
        cfg, owned, _command = fixture
        driver = Driver(owned, _CONTENT)
        runtime = _configured_factory(cfg, driver, maximum=7)(owned)
        runtime.file_executor = executor
        invocation = advance_obtain(runtime)
    else:
        flow, _initial = _internal_flow(fixture)
        owned = flow.owned
        driver = Driver(owned, _MEDIA_CONTENT)
        flow.sessions = flow.sessions.__class__(owned, driver, None)
        flow.file_executor = executor
        invocation = run_recording_media(flow, 1, 1, 11)
    task = asyncio.create_task(invocation)
    try:
        await asyncio.wait_for(waiting.wait(), timeout=5)
        ticket = driver.requests[0][0]
        task.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done()
        assert owned.connection.execute(
            "SELECT status,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone() == (1, None)
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert owned.connection.execute(
            "SELECT status,result_json IS NOT NULL FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone() == (2, 1)
        assert len(driver.requests) == 1
    finally:
        released.set()
        if not task.done():
            try:
                await task
            except asyncio.CancelledError:
                pass


class MismatchingDigest:
    async def read_digest(self):
        from camctl.outputs.copy import SourceDigest
        return SourceDigest("0" * 64)


async def _mismatching_reader(request, consumer, maximum):
    from dataclasses import replace

    if consumer == "media":
        fixture = request.getfixturevalue("pipeline")
        flow, driver = _internal_flow(fixture, checksum_support=2)
        flow.digest_supported = True
        flow.digest = MismatchingDigest()
        flow.max_recopies = maximum
        return flow.owned, flow, driver, lambda: run_recording_media(flow, 1, 1, 11)
    cfg, owned, context, capture_driver = request.getfixturevalue("environment")
    await _save_photos(cfg, owned, context, capture_driver)
    _accept(owned, "2", [{"name": "取回", "type": "obtain_action_outputs",
        "scheduled_at": "2026-01-15 09:00:00", "params": {
            "source": {"plan_instance_id": "1", "group": "files"}}}])
    driver = ReadRequests(owned, _CONTENT)
    runtime = _configured_factory(cfg, driver, maximum=7)(owned)
    runtime.devices = {device: replace(assembly, digest_supported=True,
        digest_for=lambda _file_id: MismatchingDigest(), max_recopies=maximum)
        for device, assembly in runtime.devices.items()}
    return owned, runtime, driver, lambda: advance_obtain(runtime)


@pytest.mark.parametrize("consumer", ["obtain", "media"])
@pytest.mark.parametrize("maximum", [0, 1, 2])
async def test_real_checksum_mismatch_uses_separate_recopy_budget_and_original_attempt(request, consumer, maximum):
    import json

    owned, _runtime, driver, advance = await _mismatching_reader(request, consumer, maximum)
    for _ in range(maximum + 1):
        await advance()
    copies = owned.connection.execute("SELECT id,recopies_used,round,slot_device_id FROM file_copies").fetchall()
    for copy_id, used, copy_round, slot in copies:
        assert (used, copy_round, slot) == (maximum, maximum + 1, None)
        calls = [call for call in driver.requests if int(call[0].target_id) == copy_id]
        assert len(calls) == maximum + 1
        assert len({call[0] for call in calls}) == 1
        assert owned.connection.execute(
            "SELECT attempts_used,status FROM operation_runs WHERE copy_id=?", (copy_id,)).fetchone() == (1, 4)
    if consumer == "obtain":
        for status, error_raw in owned.connection.execute("SELECT status,error_json FROM deliveries"):
            assert status == 6
            assert json.loads(error_raw) == {"code": "checksum_mismatch", "stage": "source_read",
                "details": {"max_recopies": maximum, "recopies_used": maximum}}
        assert owned.connection.execute("SELECT DISTINCT source_dependency FROM obtain_items").fetchall() == [(0,)]
    else:
        assert owned.connection.execute(
            "SELECT check_state,repair_state FROM recording_processing WHERE id=1").fetchone() == (4, 2)


@pytest.mark.parametrize("consumer", ["obtain", "media"])
async def test_registered_recopy_continues_when_later_budget_is_lower(request, consumer):
    from dataclasses import replace

    owned, runtime, driver, advance = await _mismatching_reader(request, consumer, 2)
    await advance()
    before = owned.connection.execute("SELECT id,recopies_used,round FROM file_copies").fetchall()
    assert all((used, copy_round) == (1, 2) for _copy, used, copy_round in before)
    if consumer == "media":
        runtime, _unused = _internal_flow(request.getfixturevalue("pipeline"), checksum_support=2)
        runtime.sessions = runtime.sessions.__class__(owned, driver, None)
        runtime.digest_supported = True
        runtime.digest = MismatchingDigest()
        runtime.max_recopies = 0
        advance = lambda: run_recording_media(runtime, 1, 1, 11)
    else:
        cfg = request.getfixturevalue("environment")[0]
        runtime = _configured_factory(cfg, driver, maximum=7)(owned)
        runtime.devices = {device: replace(assembly, max_recopies=0) for device, assembly in runtime.devices.items()}
        runtime.devices = {device: replace(assembly, digest_supported=True,
            digest_for=lambda _file_id: MismatchingDigest()) for device, assembly in runtime.devices.items()}
        advance = lambda: advance_obtain(runtime)
    runtime.recovery_boundary = RecoveryBoundary.HOST_LOCAL_SETTLED
    runtime.recovery_max_event_id = owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0]

    await advance()

    assert owned.connection.execute("SELECT id,recopies_used,round FROM file_copies").fetchall() == before
    for copy_id, _used, _round in before:
        calls = [call for call in driver.requests if int(call[0].target_id) == copy_id]
        assert len(calls) == 2
        assert calls[0][0] == calls[1][0]
        assert owned.connection.execute(
            "SELECT attempts_used,status FROM operation_runs WHERE copy_id=?", (copy_id,)).fetchone() == (1, 4)


@pytest.mark.parametrize("missing,expected", [
    ("boundary", "unconfirmed_boundary"),
    ("horizon", "missing_horizon"),
    ("declaration", "evidence_unavailable"),
])
async def test_internal_binding_failure_preserves_running_read_and_reports_reason(pipeline, missing, expected):
    from camctl.bootstrap.capture_assembly import session_capture_assembly
    from camctl.capture.handlers import _handle_binding_failure
    from camctl.capture.timelapse import CaptureWaitConfig
    from camctl.contracts.values import new_operation_key
    from camctl.operations.attempts import AttemptConfig, AttemptIntent, AttemptTarget, OperationKind
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.operations import OperationRepository

    flow, driver = _internal_flow(pipeline, fail=True)
    await run_recording_media(flow, 1, 1, 11)
    copy_id, copy_round = flow.owned.connection.execute(
        "SELECT id,round FROM file_copies WHERE processing_id=1").fetchone()
    result = OperationRepository().begin_attempt(AttemptIntent(
        "read", 1, OperationKind.READ_FILE, AttemptTarget(copy_id=copy_id), None,
        AttemptConfig(7, Decimal("1.25"), Decimal("0")), _NOW, copy_round=copy_round),
        new_operation_key(), flow.owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    ticket = result.value.ticket
    assert ticket is not None
    horizon = flow.owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0]
    diagnostics = []
    factory = session_capture_assembly(
        devices={}, drivers=_registry(driver), staging=flow.roots.staging,
        wall_us=lambda: _NOW, monotonic_ns=lambda: 0,
        wait_config=lambda _action: CaptureWaitConfig(target_duration_ms=60000, driver_margin_ms=0),
        recovery_boundary=(RecoveryBoundary.UNCONFIRMED if missing == "boundary" else RecoveryBoundary.HOST_LOCAL_SETTLED),
        recovery_max_event_id=(None if missing == "horizon" else lambda: horizon),
        on_recovery_diagnostic=diagnostics.append)
    runtime = factory(flow.owned, "cam-1")
    before = tuple(flow.owned.connection.iterdump())

    assert _handle_binding_failure(runtime, runtime.action(1)) is True

    assert tuple(flow.owned.connection.iterdump()) == before
    assert diagnostics
    assert diagnostics[-1].reason.value == expected
    assert (diagnostics[-1].run_id, diagnostics[-1].attempt_id) == (ticket.run_id, ticket.attempt_id)
    assert len(driver.requests) == 1
    assert flow.owned.connection.execute(
        "SELECT slot_device_id FROM file_copies WHERE id=?", (copy_id,)).fetchone() == ("cam-1",)


@pytest.mark.parametrize("consumer", ["obtain", "media"])
@pytest.mark.parametrize("stopped,error", [(False, None), (True, "failed")])
async def test_read_end_cannot_announce_success_without_confirmed_clean_end(request, consumer, stopped, error):
    from camctl.contracts.values import ConsistencyError
    from camctl.devices.read_session import ReadEnd

    owned, runtime, driver, advance = await _mismatching_reader(request, consumer, 1)
    original = driver.open_read

    class EndObservation:
        def __init__(self, session):
            self.session = session

        def position(self):
            return self.session.position()

        def read_chunk(self, limit):
            return self.session.read_chunk(limit)

        def poll_stopped(self):
            return self.session.poll_stopped()

        def request_stop(self):
            self.session.request_stop()

        async def wait_stopped(self):
            end = await self.session.wait_stopped()
            return ReadEnd(stopped, end.bytes_read, error)

    async def bad_end(source, offset, ticket, *, idle_timeout_s):
        return EndObservation(await original(source, offset, ticket, idle_timeout_s=idle_timeout_s))

    driver.open_read = bad_end
    if stopped:
        await advance()
    else:
        with pytest.raises(ConsistencyError):
            await advance()
    assert owned.connection.execute("SELECT SUM(recopies_used) FROM file_copies").fetchone() == (0,)
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
        " WHERE r.kind=3 AND a.status=2").fetchone() == (0,)
    if consumer == "media":
        assert runtime.tools.calls == []


def _apply_read_business_cancel(owned, target, *, occurred_at=_NOW):
    from camctl.cancellation.models import (
        ApplyCancelTarget, CancelApplyMode, CancellationEffect, FixCancelTargets,
        FixedCancelSet, FixedTarget, SelectionBasis, StartCancelAction,
    )
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.cancellation import CancellationRepository

    _accept(owned, "30", [{"name": "取消读取所属动作", "type": "cancel_task",
        "scheduled_at": "2026-01-15 09:00:00", "params": {
            "target": {"action_instance_id": str(target)}}}])
    origin = owned.connection.execute("SELECT id FROM actions WHERE type=6 ORDER BY id DESC").fetchone()[0]
    repository = CancellationRepository()
    started = repository.start_cancel_action(StartCancelAction(origin, occurred_at), new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    fixed = repository.fix_cancel_targets(FixCancelTargets(origin, FixedCancelSet((
        FixedTarget(target, SelectionBasis.DIRECT, CancellationEffect.NOT_APPLIED),)), occurred_at),
        new_operation_key(), owned)
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    applied = repository.apply_cancel_target(ApplyCancelTarget(
        fixed.value.item_ids[0], CancelApplyMode.WITH_STOP, occurred_at), new_operation_key(), owned)
    assert applied.kind is DbOutcomeKind.COMPLETED, applied.error


async def test_business_cancel_obtain_read_saves_actual_stopped_unknown(pending_read):
    await _assert_business_cancel_read(pending_read, "obtain")


async def test_business_cancel_media_read_saves_actual_stopped_unknown(tmp_path):
    flow, source_id = await _real_recording_media_flow(tmp_path)
    try:
        await _assert_business_cancel_read((flow, source_id), "media")
    finally:
        flow.owned.connection.close()


async def _real_recording_media_flow(tmp_path):
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import CommandMode
    from camctl.capture.handlers import capture_handler, advance_winddown, SessionRecordingState
    from camctl.capture.recording import RecordingPhase, RecordingFacts, decide_recording_next
    from camctl.capture.timelapse import CaptureWaitConfig
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput
    from camctl.persistence.repositories.scheduling import SchedulingRepository, StartActionRequest
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    from camctl.bootstrap.config import ConfigDefaults, load_config
    from camctl.persistence.initialization import initialize_state, InitOutcome
    from ..capture.test_capture_contract import _entry, _runtime, ResultsDouble
    from .test_media_assembly import _SessionDriver, _EVIDENCE
    from .test_recording_stop import _RecordCatalog, _record_plan

    path = tmp_path / "recording-state.db"
    configuration = load_config({"paths": {
        "state_db": str(path), "log_file": str(tmp_path / "recording.log"),
        "staging": str(tmp_path / "staging"), "ready": str(tmp_path / "ready"),
        "processing": str(tmp_path / "processing")}}, ConfigDefaults())
    initialized = initialize_state(configuration, path)
    assert initialized.outcome is InitOutcome.CREATED, initialized.detail
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    accepted = AcceptanceRepository().process_input(ProcessInput(ParsedInput(
        "plan.json", _record_plan("1", "2026-01-15 09:00:00")), _RecordCatalog(),
        CommandMode.RUN, _NOW), new_operation_key(), owned)
    assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
    action_id = owned.connection.execute("SELECT id FROM actions").fetchone()[0]
    started = SchedulingRepository().start_action(StartActionRequest(action_id, _NOW, _NOW),
                                                  new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    driver = _SessionDriver(_MEDIA_CONTENT)
    runtime = _runtime(owned, driver=driver,
                       results=ResultsDouble({action_id: (_entry("video", size=len(_MEDIA_CONTENT)),)}), wall=_NOW)
    runtime.evidence = _EVIDENCE
    runtime.stopper = driver
    runtime.wait_config = lambda _action: CaptureWaitConfig(target_duration_ms=6000, driver_margin_ms=0)
    runtime.monotonic_ns = lambda: 5_000_000_000
    await capture_handler("camera_record")(action_id, runtime)
    # 后续受限会话没有原单调钟锚点，实际保守停止会固定检查责任和完整原片。
    runtime.recording_state = SessionRecordingState(runtime)
    phase = decide_recording_next(runtime.recording_state.recording_state(action_id), RecordingFacts())
    assert phase.phase is RecordingPhase.RECONCILE_REQUIRED
    now_ns = [20_000_000_000]
    runtime.monotonic_ns = lambda: now_ns[0]

    async def elapsed(seconds):
        now_ns[0] += int(seconds * 1_000_000_000)

    progress = await advance_winddown(action_id, runtime, {}, wait_cap_s=Decimal("6"), sleep=elapsed)
    assert progress.phase == "progress_saved"
    phase = decide_recording_next(runtime.recording_state.recording_state(action_id), RecordingFacts())
    assert phase.phase is RecordingPhase.CONTROL_COMPLETE
    assert driver.calls == [("control", "start_recording"), ("stop", "stop_recording")]
    processing_id, source_id = owned.connection.execute(
        "SELECT id,source_device_file_id FROM recording_processing WHERE action_id=?", (action_id,)).fetchone()
    assert source_id is not None
    assert owned.connection.execute("SELECT COUNT(*) FROM device_activities WHERE action_id=?", (action_id,)).fetchone() == (1,)
    assert owned.connection.execute("SELECT check_decision FROM recording_processing WHERE id=?", (processing_id,)).fetchone() == (3,)
    staging = Path(configuration.paths.staging)
    entry = DriverEntry("camctl-adb", ReadRequests(owned, _MEDIA_CONTENT),
        DriverDeclaration(False, False, False, False, True, False, False),
        _READ_EVIDENCE, DriverStatus.SOFTWARE_CONTRACT_VERIFIED)
    flow = _media_flow_with(owned, entry, "cam-1", "camctl-adb", ProbeTools(MediaProbe(Decimal("61"), None)),
        staging, lambda: _NOW, {"copy": _SETTINGS}, RetryWaitGate(), lambda: 0)
    flow.segment_size = 4
    return flow, source_id


async def _assert_business_cancel_read(fixture, consumer):
    import asyncio
    import json
    import threading

    from camctl.contracts.enums import enum_for

    waiting, allowed = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    class PartialStream(_MemoryStream):
        def read(self, limit):
            data = super().read(min(limit, 4))
            if self._position == len(data):
                loop.call_soon_threadsafe(waiting.set)
                if not allowed.wait(timeout=5):
                    raise TimeoutError("测试未放行真实读取")
            return data

    class Driver(ReadRequests):
        async def open_read(self, source, offset, ticket, *, idle_timeout_s):
            await super().open_read(source, offset, ticket, idle_timeout_s=idle_timeout_s)
            return ReadSession(source, offset, PartialStream(self.content[offset:]), idle_timeout_s)

    if consumer == "obtain":
        cfg, owned, command = fixture
        driver = Driver(owned, _CONTENT)
        runtime = _configured_factory(cfg, driver, maximum=7)(owned)
        target = owned.connection.execute(
            "SELECT action_id FROM operation_runs WHERE copy_id=?", (command.copy_id,)).fetchone()[0]
        invocation = advance_obtain(runtime)
    else:
        runtime, source_id = fixture
        owned, target = runtime.owned, 1
        driver = Driver(owned, _MEDIA_CONTENT)
        runtime.sessions = runtime.sessions.__class__(owned, driver, None)
        invocation = run_recording_media(runtime, 1, 1, source_id)
    task = asyncio.create_task(invocation)
    try:
        await asyncio.wait_for(waiting.wait(), timeout=5)
        ticket = driver.requests[0][0]
        _apply_read_business_cancel(owned, target)
        assert owned.connection.execute(
            "SELECT status,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone() == (1, None)
        assert owned.connection.execute(
            "SELECT slot_device_id FROM file_copies WHERE id=?", (int(ticket.target_id),)).fetchone()[0] is not None
        allowed.set()
        await task
        result = owned.connection.execute(
            "SELECT status,error_json,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone()
        assert result[0] == int(enum_for("operation_attempts.status").UNKNOWN)
        assert json.loads(result[1]) == {"code": "read_stopped", "stage": "read", "details": {}}
        assert json.loads(result[2])["settlement"] == {"basis": "observed", "evidence": {
            "type": "read_returned", "version": 1, "data": {}}}
        assert owned.connection.execute(
            "SELECT status,attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (
                int(enum_for("operation_runs.status").CANCELED), ticket.attempt_id)
        assert len(driver.requests) == 1
    finally:
        allowed.set()
        if not task.done():
            await task


async def test_restart_finishes_original_failed_read_business_without_reopening(pending_read, monkeypatch):
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.persistence.repositories.outputs import OutputsRepository

    cfg, owned, command = pending_read
    driver = ReadRequests(owned, _CONTENT, fail=True)
    first = _configured_factory(cfg, driver, maximum=2)(owned)
    original = OutputsRepository.fail_read_delivery
    calls = []

    def interrupt_once(repository, request, key, connection):
        calls.append((request, key))
        if len(calls) == 1:
            return DbOutcome(DbOutcomeKind.ROLLED_BACK, error=RuntimeError("原业务结果尚未保存"))
        return original(repository, request, key, connection)

    monkeypatch.setattr(OutputsRepository, "fail_read_delivery", interrupt_once)
    with pytest.raises(ConsistencyError):
        await advance_obtain(first)
    assert owned.connection.execute(
        "SELECT status FROM operation_runs WHERE copy_id=?", (command.copy_id,)).fetchone() == (4,)
    restarted = _configured_factory(cfg, driver, maximum=9)(owned)

    await advance_obtain(restarted)

    assert len(driver.requests) == 1
    assert owned.connection.execute(
        "SELECT status,attempts_used,max_attempts_used FROM operation_runs WHERE copy_id=?",
        (command.copy_id,)).fetchone() == (4, 2, 2)
    assert owned.connection.execute(
        "SELECT status FROM deliveries WHERE id=(SELECT delivery_id FROM file_copies WHERE id=?)",
        (command.copy_id,)).fetchone() == (6,)
    assert owned.connection.execute(
        "SELECT source_dependency FROM obtain_items WHERE delivery_id=(SELECT delivery_id FROM file_copies WHERE id=?)",
        (command.copy_id,)).fetchone() == (0,)


async def test_restart_finishes_original_internal_read_failure_with_saved_configuration(pipeline, monkeypatch):
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.persistence.repositories.capture import CaptureRepository

    first, driver = _internal_flow(pipeline, fail=True, maximum=1)
    original = CaptureRepository.save_check_result
    calls = []

    def interrupt_once(repository, request, key, connection):
        calls.append(request)
        if len(calls) == 1:
            return DbOutcome(DbOutcomeKind.ROLLED_BACK, error=RuntimeError("原检查失败尚未保存"))
        return original(repository, request, key, connection)

    monkeypatch.setattr(CaptureRepository, "save_check_result", interrupt_once)
    with pytest.raises(ConsistencyError):
        await run_recording_media(first, 1, 1, 11)
    restarted, fresh_driver = _internal_flow(pipeline, fail=True, maximum=7)

    await run_recording_media(restarted, 1, 1, 11)

    assert len(driver.requests) == 1
    assert fresh_driver.requests == []
    assert first.owned.connection.execute(
        "SELECT status,attempts_used,max_attempts_used FROM operation_runs WHERE copy_id="
        " (SELECT id FROM file_copies WHERE processing_id=1)").fetchone() == (4, 1, 1)
    assert first.owned.connection.execute(
        "SELECT check_state,repair_state FROM recording_processing WHERE id=1").fetchone() == (4, 2)
    assert first.owned.connection.execute(
        "SELECT slot_device_id FROM file_copies WHERE processing_id=1").fetchone() == (None,)


@pytest.mark.parametrize("after_commit", [False, True])
async def test_obtain_failure_unknown_commit_retries_original_business_key(pending_read, monkeypatch, after_commit):
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.outputs import OutputsRepository
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    from ..cancellation.test_report_sync_lifecycle import _fault_owned

    cfg, owned, command = pending_read
    driver = ReadRequests(owned, _CONTENT, fail=True)
    factory = _configured_factory(cfg, driver, maximum=2)
    original = OutputsRepository.fail_read_delivery
    saves = []
    faults = []

    def uncertain_once(repository, request, key, connection):
        saves.append((request, key))
        if len(saves) == 1:
            faulty = _fault_owned(connection, "COMMIT", after_commit=after_commit)
            faults.append(faulty)
            result = original(repository, request, key, faulty)
            assert faulty.connection.failed
            assert result.kind is DbOutcomeKind.UNKNOWN
            return result
        return original(repository, request, key, connection)

    monkeypatch.setattr(OutputsRepository, "fail_read_delivery", uncertain_once)
    with pytest.raises(ConsistencyError):
        await advance_obtain(factory(owned))
    assert len(driver.requests) == 1
    owned.connection.close()
    recovered = open_existing(Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
    driver.owned = recovered
    try:
        await advance_obtain(factory(recovered))

        assert len(driver.requests) == 1
        assert len(saves) == 2
        assert saves[1] == saves[0]
        assert recovered.connection.execute(
            "SELECT status,json_extract(error_json,'$.code') FROM deliveries WHERE id=?",
            (saves[0][0].delivery_id,)).fetchone() == (6, "read_attempts_exhausted")
        assert recovered.connection.execute(
            "SELECT source_dependency FROM obtain_items WHERE delivery_id=?",
            (saves[0][0].delivery_id,)).fetchone() == (0,)
        assert recovered.connection.execute(
            "SELECT slot_device_id FROM file_copies WHERE id=?", (command.copy_id,)).fetchone() == (None,)
        assert recovered.connection.execute(
            "SELECT e.event_type FROM history_events e JOIN history_transactions t ON t.id=e.transaction_id"
            " WHERE t.operation_key=? ORDER BY e.id", (str(saves[0][1]),)).fetchall() == [(23,), (21,), (22,)]
    finally:
        recovered.connection.close()


@pytest.mark.parametrize("after_commit", [False, True])
async def test_internal_failure_unknown_commit_retries_original_business_key(pipeline, monkeypatch, after_commit):
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.capture import CaptureRepository
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    from ..cancellation.test_report_sync_lifecycle import _fault_owned

    flow, driver = _internal_flow(pipeline, fail=True, maximum=1)
    path = flow.owned.connection.execute("PRAGMA database_list").fetchone()[2]
    original = CaptureRepository.save_check_result
    saves = []

    def uncertain_once(repository, request, key, connection):
        saves.append((request, key))
        if len(saves) == 1:
            faulty = _fault_owned(connection, "COMMIT", after_commit=after_commit)
            result = original(repository, request, key, faulty)
            assert faulty.connection.failed
            assert result.kind is DbOutcomeKind.UNKNOWN
            return result
        return original(repository, request, key, connection)

    monkeypatch.setattr(CaptureRepository, "save_check_result", uncertain_once)
    with pytest.raises(ConsistencyError):
        await run_recording_media(flow, 1, 1, 11)
    assert len(driver.requests) == 1
    flow.owned.connection.close()
    recovered = open_existing(Path(path), DbOpenMode.EXISTING_RW, DbConfig())
    flow.owned, driver.owned = recovered, recovered
    try:
        await run_recording_media(flow, 1, 1, 11)

        assert len(driver.requests) == 1
        assert len(saves) == 2
        assert saves[1] == saves[0]
        assert recovered.connection.execute(
            "SELECT check_state,repair_state FROM recording_processing WHERE id=1").fetchone() == (4, 2)
        assert recovered.connection.execute(
            "SELECT slot_device_id FROM file_copies WHERE processing_id=1").fetchone() == (None,)
        assert recovered.connection.execute(
            "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(saves[0][1]),)).fetchone() == (1,)
    finally:
        recovered.connection.close()


@pytest.mark.parametrize("change", ["missing", "mismatch"])
@pytest.mark.parametrize("uncertain_after_commit", [None, False, True])
async def test_internal_original_read_unknown_recovery_finishes_original_binding_responsibility(
        tmp_path, monkeypatch, change, uncertain_after_commit):
    import json
    from dataclasses import replace
    from camctl.bootstrap.capture_assembly import session_capture_assembly
    from camctl.capture.handlers import capture_handler
    from camctl.capture.timelapse import CaptureWaitConfig
    from camctl.devices.drivers.registry import DriverRegistry
    from ..capture.test_capture_contract import ResultsDouble
    from .test_media_assembly import _SessionDriver, _EVIDENCE

    flow, source_id = await _real_recording_media_flow(tmp_path)
    try:
        driver = ReadRequests(flow.owned, _MEDIA_CONTENT, fail=True)
        flow.sessions = flow.sessions.__class__(flow.owned, driver, None)
        await run_recording_media(flow, 1, 1, source_id)
        copy_id = flow.owned.connection.execute("SELECT id FROM file_copies WHERE processing_id=1").fetchone()[0]
        ticket = _start_unended_read(flow.owned, copy_id)
        horizon = flow.owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0]
        original = _registry(driver).entry("camctl-adb")
        original = replace(original, declaration=replace(original.declaration,
            adb_foreground_recovery_operations=frozenset({"read"})), evidence=EvidenceRegistry((
                EvidenceContract("adb_foreground_recovery", 1, "read", frozenset()),)))
        devices = {} if change == "missing" else {"cam-1": {"kind": "camera", "driver": "replacement"}}
        diagnostics = []
        replacement_driver = _SessionDriver(_MEDIA_CONTENT)
        replacement = DriverEntry("replacement", replacement_driver,
            DriverDeclaration(True, True, False, False, True, True, False), _EVIDENCE,
            DriverStatus.SOFTWARE_CONTRACT_VERIFIED)
        factory = session_capture_assembly(
            devices=devices, drivers=DriverRegistry((original, replacement)), results=ResultsDouble({}), staging=flow.roots.staging,
            wall_us=lambda: _NOW, monotonic_ns=lambda: 0,
            wait_config=lambda _action: CaptureWaitConfig(target_duration_ms=6000, driver_margin_ms=0),
            recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED, recovery_max_event_id=lambda: horizon,
            on_recovery_diagnostic=diagnostics.append)
        runtime = factory(flow.owned, "cam-1")
        attempts_before = flow.owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone()[0]

        saves = []
        if uncertain_after_commit is not None:
            from camctl.contracts.values import ConsistencyError
            from camctl.persistence.models import DbOutcomeKind
            from camctl.persistence.repositories.capture import CaptureRepository
            from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
            from ..cancellation.test_report_sync_lifecycle import _fault_owned

            original_save = CaptureRepository.finish_binding_failure

            def uncertain_once(repository, request, key, connection):
                saves.append((request, key))
                if len(saves) == 1:
                    faulty = _fault_owned(connection, "COMMIT", after_commit=uncertain_after_commit)
                    saved = original_save(repository, request, key, faulty)
                    assert faulty.connection.failed
                    assert saved.kind is DbOutcomeKind.UNKNOWN
                    return saved
                return original_save(repository, request, key, connection)

            monkeypatch.setattr(CaptureRepository, "finish_binding_failure", uncertain_once)
            with pytest.raises(ConsistencyError):
                await capture_handler("camera_record")(1, runtime)
            path = flow.owned.connection.execute("PRAGMA database_list").fetchone()[2]
            flow.owned.connection.close()
            flow.owned = open_existing(Path(path), DbOpenMode.EXISTING_RW, DbConfig())
            runtime = factory(flow.owned, "cam-1")
        await capture_handler("camera_record")(1, runtime)
        if uncertain_after_commit is not None:
            assert len(saves) == 2 and saves[0] == saves[1]

        result = flow.owned.connection.execute(
            "SELECT status,effect_state,result_json,error_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone()
        from camctl.contracts.enums import enum_for
        assert result[:2] == (int(enum_for("operation_attempts.status").UNKNOWN),
                              int(enum_for("operation_attempts.effect_state").UNKNOWN))
        assert json.loads(result[2]) == {"format_version": 1, "observations": [], "settlement": {
            "basis": "assumed", "evidence": {"type": "adb_foreground_recovery", "version": 1, "data": {}}}}
        assert json.loads(result[3]) == {"code": "result_not_saved", "stage": "recovery", "details": {}}
        assert flow.owned.connection.execute(
            "SELECT status,json_extract(error_json,'$.code') FROM operation_runs WHERE id=?",
            (ticket.run_id,)).fetchone() == (4, "device_binding_unavailable")
        from camctl.contracts.workflow_errors import action_error_id
        assert flow.owned.connection.execute(
            "SELECT status,error_code FROM actions WHERE id=1").fetchone() == (4, action_error_id("device_binding_unavailable"))
        assert flow.owned.connection.execute(
            "SELECT check_state,repair_state FROM recording_processing WHERE id=1").fetchone() == (4, 2)
        assert flow.owned.connection.execute(
            "SELECT slot_device_id FROM file_copies WHERE id=?", (copy_id,)).fetchone() == (None,)
        assert flow.owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone()[0] == attempts_before
        assert len(driver.requests) == 1
        assert diagnostics == []
        assert replacement_driver.calls == []
    finally:
        flow.owned.connection.close()


@pytest.mark.parametrize("new_session", [False, True])
async def test_obtain_verify_preserves_original_end_or_resumes_at_full_offset(pending_read, monkeypatch, new_session):
    cfg, owned, _command = pending_read
    driver = ReadRequests(owned, _CONTENT)
    factory = _configured_factory(cfg, driver, maximum=7)
    runtime = factory(owned)
    await _assert_verify_end_recovery(owned, runtime, driver, lambda: advance_obtain(runtime),
        lambda: _configured_factory(cfg, driver, maximum=7)(owned) if new_session else factory(owned),
        lambda fresh: advance_obtain(fresh), monkeypatch, new_session)


@pytest.mark.parametrize("new_session", [False, True])
async def test_internal_verify_preserves_original_end_or_resumes_at_full_offset(pipeline, monkeypatch, new_session):
    flow, driver = _internal_flow(pipeline)

    def fresh():
        if not new_session:
            return flow
        reopened, _unused = _internal_flow(pipeline)
        reopened.sessions = reopened.sessions.__class__(flow.owned, driver, None)
        return reopened

    await _assert_verify_end_recovery(flow.owned, flow, driver, lambda: run_recording_media(flow, 1, 1, 11),
        fresh, lambda runtime: run_recording_media(runtime, 1, 1, 11), monkeypatch, new_session)


async def _assert_verify_end_recovery(owned, runtime, driver, first_advance, new_runtime, advance, monkeypatch, new_session):
    from camctl.capture.media_flow import RecordingInputCopies
    from camctl.contracts.values import ConsistencyError
    from camctl.outputs.copy import CopyCompletionError

    complete = RecordingInputCopies.complete
    calls = []

    async def fail_first_completion(copies, copy_id, digest):
        calls.append(copy_id)
        if len(calls) == 1:
            raise CopyCompletionError("target_sha256", "本地完整性校验暂不可用")
        return await complete(copies, copy_id, digest)

    monkeypatch.setattr(RecordingInputCopies, "complete", fail_first_completion)
    with pytest.raises(ConsistencyError):
        await first_advance()
    assert len(driver.requests) == 1
    original_ticket = driver.requests[0][0]
    copy_id = int(original_ticket.target_id)
    progress = owned.connection.execute(
        "SELECT committed_bytes,source_size,round,recopies_used FROM file_copies WHERE id=?", (copy_id,)).fetchone()
    assert progress[0] == progress[1]
    assert owned.connection.execute(
        "SELECT status,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
        (original_ticket.run_id, original_ticket.attempt_id)).fetchone() == (1, None)
    fresh = new_runtime()
    if new_session:
        fresh.recovery_boundary = RecoveryBoundary.HOST_LOCAL_SETTLED
        fresh.recovery_max_event_id = owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0]

    await advance(fresh)

    assert owned.connection.execute(
        "SELECT status,result_json IS NOT NULL FROM operation_attempts WHERE run_id=? AND attempt_no=?",
        (original_ticket.run_id, original_ticket.attempt_id)).fetchone() == (2, 1)
    assert owned.connection.execute(
        "SELECT attempts_used FROM operation_runs WHERE id=?", (original_ticket.run_id,)).fetchone() == (original_ticket.attempt_id,)
    assert owned.connection.execute(
        "SELECT committed_bytes,source_size,round,recopies_used FROM file_copies WHERE id=?", (copy_id,)).fetchone() == progress
    assert len(driver.requests) == (2 if new_session else 1)
    if new_session:
        assert driver.requests[1][0] == original_ticket
        assert driver.requests[1][3] == progress[1]


async def test_obtain_complete_actual_read_precedes_later_business_cancel(pending_read):
    cfg, owned, command = pending_read
    driver = ReadRequests(owned, _CONTENT)
    runtime = _configured_factory(cfg, driver, maximum=7)(owned)
    target = owned.connection.execute(
        "SELECT action_id FROM operation_runs WHERE copy_id=?", (command.copy_id,)).fetchone()[0]
    await _assert_complete_before_business_cancel(owned, runtime, driver, target,
                                                lambda: advance_obtain(runtime))


async def test_internal_complete_actual_read_precedes_later_business_cancel(tmp_path):
    flow, source_id = await _real_recording_media_flow(tmp_path)
    driver = ReadRequests(flow.owned, _MEDIA_CONTENT)
    flow.sessions = flow.sessions.__class__(flow.owned, driver, None)
    try:
        await _assert_complete_before_business_cancel(flow.owned, flow, driver, 1,
            lambda: run_recording_media(flow, 1, 1, source_id))
    finally:
        flow.owned.connection.close()


async def _assert_complete_before_business_cancel(owned, runtime, driver, target, advance):
    import asyncio
    from camctl.contracts.enums import enum_for

    ended, allowed = asyncio.Event(), asyncio.Event()
    original = driver.open_read
    observed = []

    class CompleteEnd:
        def __init__(self, session):
            self.session = session

        def position(self):
            return self.session.position()

        def read_chunk(self, limit):
            return self.session.read_chunk(limit)

        def request_stop(self):
            self.session.request_stop()

        def poll_stopped(self):
            return self.session.poll_stopped()

        async def wait_stopped(self):
            end = await self.session.wait_stopped()
            observed.append(end)
            ended.set()
            await allowed.wait()
            return end

    async def open_complete(source, offset, ticket, *, idle_timeout_s):
        return CompleteEnd(await original(source, offset, ticket, idle_timeout_s=idle_timeout_s))

    driver.open_read = open_complete
    task = asyncio.create_task(advance())
    try:
        await asyncio.wait_for(ended.wait(), timeout=5)
        ticket = driver.requests[0][0]
        assert observed[0].stopped is True and observed[0].error is None
        assert owned.connection.execute(
            "SELECT committed_bytes=source_size FROM file_copies WHERE id=?", (int(ticket.target_id),)).fetchone() == (1,)
        assert owned.connection.execute(
            "SELECT status,attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (
                int(enum_for("operation_runs.status").ACTIVE), ticket.attempt_id)
        assert owned.connection.execute(
            "SELECT status,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone() == (int(enum_for("operation_attempts.status").RUNNING), None)
        _apply_read_business_cancel(owned, target)
        assert owned.connection.execute(
            "SELECT status FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (
                int(enum_for("operation_runs.status").ACTIVE),)
        allowed.set()
        await task

        assert owned.connection.execute(
            "SELECT status,error_json,json_extract(result_json,'$.settlement.basis')"
            " FROM operation_attempts WHERE run_id=? AND attempt_no=?", (ticket.run_id, ticket.attempt_id)).fetchone() == (
                int(enum_for("operation_attempts.status").SUCCEEDED), None, "observed")
        assert owned.connection.execute(
            "SELECT status,attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (
                int(enum_for("operation_runs.status").CANCELED), ticket.attempt_id)
        assert len(driver.requests) == 1
    finally:
        allowed.set()
        if not task.done():
            await task

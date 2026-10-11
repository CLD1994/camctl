"""已取得媒体申请在实际拥有者结束之后仍属于本会话收场责任。"""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from camctl.bootstrap import lifecycle
from camctl.bootstrap.capture_assembly import session_capture_assembly
from camctl.capture.files import (
    FileCompletionSave, FileObservationSave, FilePresenceSave, OwnershipSave,
)
from camctl.capture.handlers import _stop_call, capture_handler
from camctl.capture.media_flow import DriverReadSessions, run_recording_media
from camctl.capture.models import ActivityConcludeSave
from camctl.capture.processing import (
    CheckBasis, CheckDecisionChoice, CheckDecisionSave, CheckPhase, CheckReason,
    SourceFileSave,
)
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.bindings import DeviceConfigurationError
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.ports import DriverDeclaration
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.scheduling import (
    ObserveWindowRequest, SchedulingRepository, StartActionRequest,
)
from camctl.session.clock import ClockBecameUntrusted
from camctl.session.locks import acquire_session, probe_admission
from camctl.session.service import StateDbFailure, run_session
from camctl.session.supervision import SupervisionResult, TaskFailure, TaskRecord

from ..capture.media_retry_fixtures import _EVIDENCE, media_pipeline as pipeline  # noqa: F401
from ..capture.test_capture_contract import ResultsDouble
from ..capture.test_input_copy import _CONTENT, _NOW
from ..capture.test_media_flow import ReadDriverDouble
from ..capture.test_media_result_retry import _ActualTools
from ..session.test_session import FixedClock
from .test_binding_transactions import _CommitFailure
from .test_media_assembly import _SessionDriver, _config
from .test_media_saved_result_consumers import _default_factories


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def held_media(pipeline, monkeypatch):
    owned, _roots, source_id = pipeline
    deps, factories, context = await _default_factories(pipeline, monkeypatch)
    driver, tools = ReadDriverDouble(_CONTENT), _ActualTools("check")
    flow = factories[0](owned, "cam-1").media
    assert flow is not None
    flow.sessions = DriverReadSessions(owned, driver, ticket=None)
    flow.tools = tools
    original_save = CaptureRepository.save_check_result
    attempts = []

    def save(repository, command, key, current):
        if command.phase is CheckPhase.RUNNING:
            return original_save(repository, command, key, current)
        attempts.append((command, key))
        return original_save(repository, command, key,
            replace(current, connection=_CommitFailure(current.connection, True)))

    monkeypatch.setattr(CaptureRepository, "save_check_result", save)
    try:
        with pytest.raises(ConsistencyError):
            await run_recording_media(flow, 1, 1, source_id)
        assert len(attempts) == 1 and tools.probes == 1
        assert not deps.work_files.executor.unfinished_files()
        assert len(deps.capture_media_results) == 1
        assert deps.work_files.pending_media_results is deps.capture_media_results
        assert deps.work_files.supervisor.outstanding() == ()
        assert deps.work_files.file_supervisor.outstanding() == ()
        context.clock = FixedClock(_NOW + 100)
        yield deps, context, driver, tools, attempts
    finally:
        lifecycle.close_runtime(deps)


async def test_media_pending_save_is_required_after_actual_owner_has_ended(held_media):
    deps, _context, _driver, _tools, _attempts = held_media
    assert deps.work_files.required_settlements() == 1


async def test_media_pending_save_cannot_be_declared_settled_or_blindly_retried(held_media):
    deps, _context, driver, tools, attempts = held_media
    with pytest.raises(StateDbFailure) as failed:
        await deps.work_files.settle()
    assert "injected commit response failure" in str(failed.value)
    assert failed.value.__cause__ is not None
    assert len(attempts) == 1 and len(deps.capture_media_results) == 1
    assert tools.probes == 1 and len(driver.opens) == 1


@pytest.mark.parametrize("primary", ["state", "configuration", "clock", "cancel"])
async def test_media_unconfirmed_save_preserves_fatal_primary_and_secondary_state(held_media, primary):
    deps, context, driver, tools, attempts = held_media
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
        assert "injected commit response failure" in str(getattr(canceled.value, "__notes__", ()))
    else:
        outcome = await run_session(context, None)
        assert outcome.reason == {"state": "state_db_error", "configuration": "configuration_error",
            "clock": "clock_invalid"}[primary]
        assert f"primary-{primary}" in str(outcome.details)
        secondary = outcome.details.get("secondary_errors", ())
        assert any(item["reason"] == "state_db_error" and
            "injected commit response failure" in str(item["details"]) for item in secondary)
    assert not restricted
    assert len(attempts) == 1 and len(deps.capture_media_results) == 1
    assert tools.probes == 1 and len(driver.opens) == 1
    assert probe_admission(context.paths.admission_lock).is_free
    lease = acquire_session(context.paths.session_lock)
    lease.close()


class _RewrittenActivityDriver:
    """把驱动替身的确认观察身份改写为指定活动（第二录像周期）。"""

    def __init__(self, driver, identity: str) -> None:
        self._driver = driver
        self._identity = identity

    def _rewrite(self, result):
        from dataclasses import replace as _replace

        return _replace(result, observations=tuple(
            _replace(observation, data={
                **observation.data, "activity_id": self._identity})
            if "activity_id" in observation.data else observation
            for observation in result.observations))

    async def control(self, request):
        return self._rewrite(await self._driver.control(request))

    async def stop(self, request):
        return self._rewrite(await self._driver.stop(request))


async def _second_recording_cycle(owned, roots) -> tuple[int, int]:
    """把观察者动作补全为第二个真实录像周期。

    与媒体 pipeline fixture 建立第一个录像的方式一致：真实窗口观
    察、启动、可靠停止、活动结束、源文件事实与检查决定，返回第二
    个内部处理的 (processing_id, 源设备文件身份)。
    """
    scheduling = SchedulingRepository()
    second = owned.connection.execute(
        "SELECT id FROM actions WHERE name=?", ("另一个录像动作",)).fetchone()
    assert second is not None, "pipeline 须以 observer 参数建立第二动作"
    action_id = second[0]
    assert action_id != 1
    for save, command in (
        (scheduling.observe_window, ObserveWindowRequest(action_id, _NOW, _NOW)),
        (scheduling.start_action, StartActionRequest(action_id, _NOW, _NOW)),
    ):
        outcome = save(command, new_operation_key(), owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    activity = owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id=?",
        (action_id,)).fetchone()
    assert activity is not None
    driver = _RewrittenActivityDriver(_SessionDriver(_CONTENT), str(activity[0]))
    registry = DriverRegistry((DriverEntry("camctl-adb", driver, DriverDeclaration(
        control_supported=True, stop_supported=True, query_supported=False,
        result_supported=False, read_supported=True, digest_supported=True,
        delete_supported=False), _EVIDENCE, DriverStatus.SOFTWARE_CONTRACT_VERIFIED),))
    runtime = session_capture_assembly(
        devices=_config(roots.staging.parent).devices, drivers=registry,
        results=ResultsDouble({}), staging=roots.staging,
        wait_config=lambda _action: None,
        wall_us=lambda: _NOW, monotonic_ns=lambda: 7_000_000_000)(owned, "cam-1")
    await capture_handler("camera_record")(action_id, runtime)
    stopped = await _stop_call(runtime, runtime.action(action_id))
    assert stopped.phase == "confirmed", stopped
    repository = CaptureRepository()
    concluded = repository.conclude_activity(
        ActivityConcludeSave(action_id, _NOW), new_operation_key(), owned)
    assert concluded.kind is DbOutcomeKind.COMPLETED, concluded.error
    source = repository.save_file_observation(FileObservationSave(
        action_id, "original-recording-2",
        {"path": "/DCIM/original-2.mp4"}, _NOW, "original-2.mp4", "video/mp4"),
        new_operation_key(), owned)
    assert source.kind is DbOutcomeKind.COMPLETED, source.error
    source_id = source.value.file_id
    for save, command in (
        (repository.save_file_presence, FilePresenceSave(
            source_id, 2, _NOW, locator={"path": "/DCIM/original-2.mp4"})),
        (repository.save_file_ownership, OwnershipSave(
            source_id, action_id, 1, 2, {"task": "original-recording-2"}, _NOW)),
        (repository.save_file_completion, FileCompletionSave(
            source_id, 3, _NOW, basis=1,
            observation={"stopped": True}, size_bytes=len(_CONTENT))),
        (repository.save_source_file, SourceFileSave(action_id, source_id, _NOW)),
        (repository.save_check_decision, CheckDecisionSave(
            action_id, CheckDecisionChoice.REQUIRED,
            CheckBasis(CheckReason.INSUFFICIENT_TIMING, 60000), _NOW)),
    ):
        outcome = save(command, new_operation_key(), owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    processing_id = owned.connection.execute(
        "SELECT id FROM recording_processing WHERE action_id=?",
        (action_id,)).fetchone()[0]
    assert processing_id != 1
    return processing_id, source_id


@pytest_asyncio.fixture
async def two_held_media(pipeline, monkeypatch):
    """两个真实录像的检查观察保存同时失败，两份申请均在待保存集合。"""
    owned, roots, first_source = pipeline
    second_processing, second_source = await _second_recording_cycle(owned, roots)
    deps, factories, context = await _default_factories(pipeline, monkeypatch)
    driver, tools = ReadDriverDouble(_CONTENT), _ActualTools("check")
    original_save = CaptureRepository.save_check_result
    attempts = []

    def save(repository, command, key, current):
        if command.phase is CheckPhase.RUNNING:
            return original_save(repository, command, key, current)
        attempts.append((command, key))
        return original_save(repository, command, key,
            replace(current, connection=_CommitFailure(current.connection, True)))

    monkeypatch.setattr(CaptureRepository, "save_check_result", save)
    try:
        flow = factories[0](owned, "cam-1").media
        flow.sessions = DriverReadSessions(owned, driver, ticket=None)
        flow.tools = tools
        for action_id, processing_id, source_id in (
            (1, 1, first_source),
            (owned.connection.execute(
                "SELECT action_id FROM recording_processing WHERE id=?",
                (second_processing,)).fetchone()[0],
             second_processing, second_source),
        ):
            with pytest.raises(ConsistencyError):
                await run_recording_media(flow, action_id, processing_id, source_id)
        assert len(attempts) == 2 and tools.probes == 2
        assert len(deps.capture_media_results) == 2
        context.clock = FixedClock(_NOW + 100)
        yield deps, context, driver, tools, attempts, second_processing
    finally:
        lifecycle.close_runtime(deps)


@pytest.mark.parametrize("pipeline", [True], indirect=True)
async def test_settlement_reports_every_unverified_media_reason(two_held_media):
    """两份未核实媒体申请的已知原因都在收场错误中呈现并保留。"""
    deps, _context, driver, tools, attempts, second = two_held_media
    with pytest.raises(StateDbFailure) as failed:
        await deps.work_files.settle()
    message = str(failed.value)
    assert "processing_id=1" in message
    assert f"processing_id={second}" in message
    assert failed.value.__cause__ is not None
    # 两份申请均保留待核实，不盲重试、不再执行工具。
    assert len(attempts) == 2 and len(deps.capture_media_results) == 2
    assert tools.probes == 2 and len(driver.opens) == 2


@pytest.mark.parametrize("pipeline", [True], indirect=True)
async def test_settlement_reports_maintenance_and_media_reasons_together(
        two_held_media, monkeypatch):
    """维护收场失败与媒体原保存失败并存时两类原因同时呈现。"""
    deps, _context, _driver, _tools, _attempts, second = two_held_media
    failure = TaskFailure(
        TaskRecord("cleanup/9", "delete", "维护清理", ("state_db",)),
        "维护清理收场失败: injected")
    original_drain = deps.work_files.supervisor.drain_required

    async def drain():
        result = await original_drain()
        return SupervisionResult(
            settled=result.settled, failed=(*result.failed, failure))

    monkeypatch.setattr(deps.work_files.supervisor, "drain_required", drain)
    with pytest.raises(StateDbFailure) as failed:
        await deps.work_files.settle()
    message = str(failed.value)
    assert "维护清理收场失败: injected" in message
    assert "processing_id=1" in message
    assert f"processing_id={second}" in message
    assert len(deps.capture_media_results) == 2

"""共享媒体任务实际结束、原业务保存及取消诊断属于同一拥有范围。"""

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.capture.media import (
    CheckContext, RepairContext, SaveDisposition, SaveReceipt, execute_check, execute_repair,
)
from camctl.capture.processing import CheckPhase
from camctl.host_files.media import MediaArtifact, MediaProbe
from camctl.host_files.models import FileObservation, FileObservationKind
from camctl.host_files.tasks import AsyncFileTask, FileTaskExecutor, FileTaskId
from camctl.session.supervision import Supervisor
from ..support.capture import capture_database
from ..support.task_wait import wait_for_signal, settle_tasks

from .test_media_execution import (
    _NOW, _POLICY, _RepositorySaves, _input_ref, _registered_repair_output,
    _seed_pending_repair, _status,
    pipeline,  # noqa: F401
)
from .media_retry_fixtures import media_pipeline  # noqa: F401
from ..support.capture_media_cases import MediaCase, ScriptTools, media_cases  # noqa: F401


class _ToolOwner:
    async def take_over(self, task):
        await task.pending.wait()


class _PausedTools:
    def __init__(self, executor):
        self.executor = executor
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.controls = []

    async def probe(self, input):
        async def body(control):
            self.controls.append(control)
            self.started.set()
            await self.release.wait()
            return MediaProbe(Decimal("61"), None)
        return await self.executor.run_async_file_task(AsyncFileTask(
            FileTaskId("actual-probe"), (input.file_id,), "media_probe", "原片检查", body), _ToolOwner())

    async def repair(self, input, output, *, trim_s):
        async def body(control):
            self.controls.append(control)
            self.started.set()
            await self.release.wait()
            return MediaArtifact("tool returned unsuccessful", FileObservation(
                FileObservationKind.MISSING, Path(output.root) / output.relative_path),
                postprocessing_stopped=True)
        return await self.executor.run_async_file_task(AsyncFileTask(
            FileTaskId("actual-repair"), (input.file_id, output.file_id),
            "media_repair", "原修复输出", body), _ToolOwner())


class _SaveBoundary(_RepositorySaves):
    def __init__(self, database, failed):
        super().__init__(database)
        self.failed = failed
        self.actual_results = []

    async def save_check_result(self, command):
        if command.media.phase is not CheckPhase.RUNNING:
            self.actual_results.append(command)
            if self.failed:
                return SaveReceipt(SaveDisposition.UNKNOWN, error=RuntimeError("original media result commit unknown"))
        return await super().save_check_result(command)

    async def save_repair_result(self, command):
        self.actual_results.append(command)
        if self.failed:
            return SaveReceipt(SaveDisposition.UNKNOWN, error=RuntimeError("original media result commit unknown"))
        return await super().save_repair_result(command)


@pytest.mark.asyncio
@pytest.mark.parametrize("media_pipeline", ["execution"], indirect=True)
@pytest.mark.parametrize("operation", ["check", "repair"])
@pytest.mark.parametrize("save_failed", [False, True])
async def test_canceled_media_keeps_actual_result_and_original_save(pipeline, operation, save_failed):
    owned, staging, _unused_tools = pipeline
    if operation == "repair":
        await _seed_pending_repair(owned)
    executor = FileTaskExecutor(Supervisor())
    tools = _PausedTools(executor)
    saves = _SaveBoundary(await capture_database(owned), save_failed)
    if operation == "check":
        call = execute_check(CheckContext(_status(owned), _input_ref(owned, staging), _POLICY,
            tools, saves, _NOW + 30, executor=executor))
        expected_files = (_input_ref(owned, staging).file_id,)
    else:
        call = execute_repair(RepairContext(_status(owned), _input_ref(owned, staging), "mp4",
            tools, saves, _NOW + 30, executor=executor))
        expected_files = None
    running = asyncio.create_task(call)
    try:
        await wait_for_signal(tools.started, running, timeout=1)
        if operation == "repair":
            output_id = _registered_repair_output(owned, 1)
            assert output_id is not None
            expected_files = (_input_ref(owned, staging).file_id, output_id)
        assert executor.unfinished_files() == expected_files
        running.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not running.done(), "取消不能绕过媒体实际结束及原业务保存"
        assert executor.unfinished_files() == expected_files
        assert owned.connection.execute("SELECT COUNT(*) FROM recording_processing").fetchone() == (1,)
        tools.release.set()
        await settle_tasks(running, timeout=1)
        with pytest.raises(asyncio.CancelledError) as canceled:
            await running
        assert len(saves.actual_results) == 1
        assert executor.unfinished_files() == ()
        if save_failed:
            assert any("original media result commit unknown" in note for note in getattr(canceled.value, "__notes__", ()))
        elif operation == "check":
            assert owned.connection.execute("SELECT check_state FROM recording_processing").fetchone() == (3,)
        else:
            assert owned.connection.execute("SELECT repair_state FROM recording_processing").fetchone() == (6,)
    finally:
        tools.release.set()
        await settle_tasks(running, timeout=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("save_method", ["save_check_result", "save_repair_decision", "save_repair_result"])
async def test_media_consumer_stops_on_unreliable_actual_business_save(media_pipeline, media_cases, monkeypatch, save_method):
    from dataclasses import replace
    from camctl.capture.media_flow import run_recording_media
    from camctl.contracts.values import ConsistencyError
    from camctl.host_files.media import MediaStream
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.capture import CaptureRepository
    from ..bootstrap.test_binding_transactions import _CommitFailure

    owned, roots, source_id = media_pipeline
    probe = MediaProbe(Decimal("75"), None, (
        MediaStream(0, "video", "h264", 64, 64, Decimal("75"), False),))
    tools = ScriptTools(probe, result="failed")
    case = await MediaCase(owned, roots, source_id, tools).start()
    original = getattr(CaptureRepository, save_method)
    outcomes = []

    def failed(repository, command, key, current):
        if save_method == "save_check_result" and command.phase is CheckPhase.RUNNING:
            return original(repository, command, key, current)
        connection = _CommitFailure(current.connection, after=True)
        outcome = original(repository, command, key, replace(current, connection=connection))
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(CaptureRepository, save_method, failed)
    with pytest.raises(ConsistencyError):
        await run_recording_media(case.runtime.media, 1, 1, source_id)
    assert len(outcomes) == 1 and outcomes[0].kind is DbOutcomeKind.UNKNOWN
    assert len(tools.probes) == 1
    assert len(tools.repairs) == (1 if save_method == "save_repair_result" else 0)

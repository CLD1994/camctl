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

from .test_media_execution import (
    _NOW, _POLICY, _RepositorySaves, _input_ref, _seed_pending_repair, _status,
    pipeline,  # noqa: F401
)
from .test_input_copy import pipeline as input_pipeline  # noqa: F401


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
    def __init__(self, owned, failed):
        super().__init__(owned)
        self.failed = failed
        self.actual_results = []

    def save_check_result(self, command):
        if command.media.phase is not CheckPhase.RUNNING:
            self.actual_results.append(command)
            if self.failed:
                return SaveReceipt(SaveDisposition.UNKNOWN, error=RuntimeError("original media result commit unknown"))
        return super().save_check_result(command)

    def save_repair_result(self, command):
        self.actual_results.append(command)
        if self.failed:
            return SaveReceipt(SaveDisposition.UNKNOWN, error=RuntimeError("original media result commit unknown"))
        return super().save_repair_result(command)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["check", "repair"])
@pytest.mark.parametrize("save_failed", [False, True])
async def test_canceled_media_keeps_actual_result_and_original_save(pipeline, operation, save_failed):
    owned, staging, _unused_tools = pipeline
    if operation == "repair":
        await _seed_pending_repair(owned)
    executor = FileTaskExecutor(Supervisor())
    tools = _PausedTools(executor)
    saves = _SaveBoundary(owned, save_failed)
    if operation == "check":
        call = execute_check(CheckContext(_status(owned), _input_ref(staging), _POLICY,
            tools, saves, _NOW + 30, executor=executor))
        expected_files = (701,)
    else:
        call = execute_repair(RepairContext(_status(owned), _input_ref(staging), "mp4",
            tools, saves, _NOW + 30, executor=executor))
        expected_files = (701, 702)
    running = asyncio.create_task(call)
    try:
        await asyncio.wait_for(tools.started.wait(), 1)
        assert executor.unfinished_files() == expected_files
        running.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not running.done(), "取消不能绕过媒体实际结束及原业务保存"
        assert executor.unfinished_files() == expected_files
        assert owned.connection.execute("SELECT COUNT(*) FROM recording_processing").fetchone() == (1,)
        tools.release.set()
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
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("save_method", ["save_check_result", "save_repair_decision", "save_repair_result"])
async def test_media_consumer_stops_on_unreliable_actual_business_save(input_pipeline, monkeypatch, save_method):
    from camctl.capture.media_flow import CaptureProcessingSaves, run_recording_media
    from camctl.contracts.values import ConsistencyError
    from camctl.host_files.tasks import FileTaskResult
    from .test_media_flow import ProbeTools, ReadDriverDouble, _flow
    from .test_input_copy import _CONTENT

    original = getattr(CaptureProcessingSaves, save_method)

    def failed(saves, command):
        if save_method == "save_check_result" and command.phase is CheckPhase.RUNNING:
            return original(saves, command)
        return SaveReceipt(SaveDisposition.UNKNOWN, error=RuntimeError(f"{save_method} commit unknown"))

    monkeypatch.setattr(CaptureProcessingSaves, save_method, failed)

    class Tools(ProbeTools):
        async def repair(self, input, output, *, trim_s):
            return FileTaskResult(FileTaskId("returned-repair"), ran=True,
                value=MediaArtifact("tool failed", FileObservation(
                    FileObservationKind.MISSING, Path(output.root) / output.relative_path),
                    postprocessing_stopped=True))

    tools = Tools(MediaProbe(Decimal("75"), None))
    with pytest.raises(ConsistencyError, match=f"{save_method} commit unknown"):
        await run_recording_media(_flow(input_pipeline, ReadDriverDouble(_CONTENT), tools), 1, 1, 11)

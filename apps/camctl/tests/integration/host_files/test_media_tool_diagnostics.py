"""媒体工具的原始返回和正常停止事实独立于业务文案保留。"""
from types import SimpleNamespace
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from camctl.host_files import media
from camctl.operations.process import LocalExit, RawToolOutcome, ToolStartError
from camctl.host_files.models import BoundDirectories, FilePurpose, FileRef
from camctl.host_files.tasks import FileTaskExecutor, FileTaskId
from camctl.session.supervision import Supervisor

pytestmark = pytest.mark.asyncio


class ProbeOwner:
    async def take_over(self, task):
        raise AssertionError("本用例等待实际结果，不应发生取消接手")


async def _public_probe(*, stopped=False):
    supervisor = Supervisor()
    executor = FileTaskExecutor(supervisor)
    if stopped:
        run = executor.run_async_file_task

        async def run_stopped(task, owner):
            async def body(control):
                control.request_stop()
                return await task.body(control)
            return await run(replace(task, body=body), owner)

        executor.run_async_file_task = run_stopped
    result = await media.probe_media(
        FileRef(1, FilePurpose.RECORDING_INPUT, "input.mp4", Path("/staging")),
        BoundDirectories(staging=Path("/staging")), media.ProbeRequest(),
        executor=executor, task_id=FileTaskId("probe-diagnostics"), owner=ProbeOwner())
    assert result.ran and result.error is None
    assert executor.unfinished_files() == ()
    assert not (await supervisor.drain_required()).failed
    return result.value


async def test_failed_probe_keeps_original_tool_result(monkeypatch):
    raw = RawToolOutcome(LocalExit(exit_code=7), b'private media stdout', None, None,
        stderr=b'RAW-MEDIA-ERROR\xff')
    monkeypatch.setattr(media, 'resolve_file', lambda *_: SimpleNamespace(path='/staging/input.mp4'))
    monkeypatch.setattr(media, '_execute_tool', AsyncMock(return_value=raw))
    probe = await _public_probe()
    assert probe.error is not None
    assert probe.tool_diagnostics.outcome is raw
    assert probe.tool_diagnostics.exception is None


async def test_probe_start_failure_keeps_original_exception_graph(monkeypatch):
    error = ToolStartError('tool launch failed')
    error.__cause__ = OSError(2, 'ffprobe missing')
    monkeypatch.setattr(media, 'resolve_file', lambda *_: SimpleNamespace(path='/staging/input.mp4'))
    monkeypatch.setattr(media, '_execute_tool', AsyncMock(side_effect=error))
    probe = await _public_probe()
    assert probe.tool_diagnostics.exception is error
    assert probe.tool_diagnostics.exception.__cause__.errno == 2


async def test_probe_stopped_before_start_has_explicit_control_fact(monkeypatch):
    monkeypatch.setattr(media, 'resolve_file', lambda *_: SimpleNamespace(path='/staging/input.mp4'))
    executor = AsyncMock(side_effect=AssertionError('must not run'))
    monkeypatch.setattr(media, '_execute_tool', executor)
    probe = await _public_probe(stopped=True)
    assert probe.tool_diagnostics.stopped_before_start is True
    assert probe.tool_diagnostics.outcome is None
    executor.assert_not_awaited()
